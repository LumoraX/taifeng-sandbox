"""宿主侧流式进程：``CommandSpec.stdin=True`` 时经守护进程持续对话（内核 ``McpStdioClient`` 要用）。

照 ``conftest.py`` 的方式把真守护进程跑起来；等状态一律用事件或带超时的等待，不用固定 sleep。
"""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
import sys
from typing import TYPE_CHECKING, Any

import pytest
import taifeng

from taifeng_sandbox import SandboxError, SandboxProtocolError
from taifeng_sandbox.daemon import (
    DaemonClient,
    DaemonCommandExecutor,
    RemoteProcess,
    StdioTransport,
    StreamingRemoteProcess,
    protocol,
    streaming,
)
from taifeng_sandbox.daemon.streaming import _StreamInput
from tests.daemon.conftest import daemon_argv

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

ENV = {"PATH": "/usr/bin:/bin"}

MCP_SERVER = r'''
import json, sys
for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue
    if msg["method"] == "initialize":
        result = {"protocolVersion": msg["params"]["protocolVersion"], "capabilities": {"tools": {}},
                  "serverInfo": {"name": "t", "version": "1"}}
    elif msg["method"] == "tools/list":
        result = {"tools": [{"name": "ping", "description": "p", "inputSchema": {"type": "object"}}]}
    elif msg["method"] == "tools/call":
        result = {"content": [{"type": "text", "text": "pong"}]}
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}) + "\n")
    sys.stdout.flush()
'''

# 逐行回显到 stdout；读到 EOF 后再写一行 bye 并正常退出
ECHO = (
    "import sys\n"
    "for line in sys.stdin:\n"
    "    sys.stdout.write('got:' + line)\n"
    "    sys.stdout.flush()\n"
    "sys.stdout.write('bye\\n')\n"
)

# 逐行回显到 stderr
ERR_ECHO = (
    "import sys\n"
    "for line in sys.stdin:\n"
    "    sys.stderr.write('err:' + line)\n"
    "    sys.stderr.flush()\n"
)

# 读到 EOF 后把收到的字节原样写回 stdout
CAT = "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"


def _python(code: str) -> taifeng.CommandSpec:
    """以 ``stdin=True`` 跑一段 Python 的命令（与内核 ``McpStdioClient`` 一样走 shell=False）。"""
    return taifeng.CommandSpec(
        command=shlex.join([sys.executable, "-c", code]), shell=False, cwd=None, env={}, stdin=True
    )


async def _start(executor: DaemonCommandExecutor, code: str) -> StreamingRemoteProcess:
    """启动流式进程并确认拿到的是流式句柄。"""
    proc = await executor.start(_python(code))
    assert isinstance(proc, StreamingRemoteProcess)
    assert isinstance(proc, taifeng.StreamingCommandProcess)
    return proc


async def _send(proc: StreamingRemoteProcess, data: bytes) -> None:
    """写一段并等它被守护进程接收。"""
    assert proc.stdin is not None
    proc.stdin.write(data)
    await asyncio.wait_for(proc.stdin.drain(), 10)


async def _readline(proc: StreamingRemoteProcess) -> bytes:
    """带超时地从 stdout 读一行。"""
    assert proc.stdout is not None
    return await asyncio.wait_for(proc.stdout.readline(), 10)


async def _until(condition: Callable[[], bool]) -> None:
    """让出事件循环直到条件成立，最多等 5 秒。"""
    async with asyncio.timeout(5):
        while not condition():  # noqa: ASYNC110 —— 轮询白盒状态，没有可等的事件
            await asyncio.sleep(0)


class _Recording:
    """包一层执行器，记下启动过的进程，便于事后核对退出码。"""

    def __init__(self, inner: DaemonCommandExecutor) -> None:
        """包装 ``inner``。"""
        self._inner = inner
        self.started: list[taifeng.CommandProcess] = []

    async def start(self, spec: taifeng.CommandSpec) -> taifeng.CommandProcess:
        """转交 ``inner`` 启动并记下。"""
        proc = await self._inner.start(spec)
        self.started.append(proc)
        return proc


async def test_mcp_stdio_client_through_the_daemon(client: DaemonClient, tmp_path: Path) -> None:
    """内核的 McpStdioClient 经守护进程执行器起 MCP server：握手、列工具、调用都通，关闭后正常退出。

    关闭时 server 读到 EOF 自己退出（退出码 0），而不是等满 3 秒被杀。
    """
    (tmp_path / "server.py").write_text(MCP_SERVER)
    executor = _Recording(DaemonCommandExecutor(client, default_cwd=str(tmp_path)))
    mcp = await taifeng.McpStdioClient.spawn(
        [sys.executable, str(tmp_path / "server.py")], env={}, executor=executor
    )
    try:
        assert [tool["name"] for tool in await mcp.list_tools()] == ["ping"]
        result = await mcp.call_tool("ping", {})
        assert result["content"][0]["text"] == "pong"
    finally:
        await mcp.close()
    assert [proc.returncode for proc in executor.started] == [0]


async def test_unread_output_over_the_limit_kills_and_fails(client: DaemonClient) -> None:
    """读取方不读、输出超过上限：杀掉进程，读取方拿到错误而不是被截断的数据。"""
    executor = DaemonCommandExecutor(client, max_buffer_bytes=1024)
    proc = await _start(executor, "import sys; sys.stdout.write('x' * 100000); sys.stdin.read()")
    await asyncio.wait_for(proc.wait(), 10)
    assert proc.stdout is not None
    with pytest.raises(SandboxError, match="缓冲上限"):
        await asyncio.wait_for(proc.stdout.read(), 10)
    assert proc.dropped_bytes == 0


async def test_one_stream_over_the_limit_leaves_the_other_intact(client: DaemonClient) -> None:
    """stderr 超上限：stderr 读取抛错、进程被杀；stdout 已到的数据照常读完。"""
    executor = DaemonCommandExecutor(client, max_buffer_bytes=1024)
    proc = await _start(
        executor,
        "import sys; print('ok', flush=True); sys.stdin.readline(); "
        "sys.stderr.write('e' * 100000); sys.stderr.flush(); sys.stdin.read()",
    )
    await _send(proc, b"go\n")
    assert await asyncio.wait_for(proc.wait(), 10) != 0
    assert proc.stdout is not None and proc.stderr is not None
    with pytest.raises(SandboxError, match="stderr 未读输出超过宿主缓冲上限"):
        await asyncio.wait_for(proc.stderr.read(), 10)
    assert await asyncio.wait_for(proc.stdout.read(), 10) == b"ok\n"


async def test_communicate_over_the_limit_raises(client: DaemonClient) -> None:
    """communicate() 遇到某一路超上限：抛 SandboxError，不返回被截断的输出。"""
    executor = DaemonCommandExecutor(client, max_buffer_bytes=1024)
    proc = await _start(executor, "import sys; sys.stdout.write('x' * 100000)")
    with pytest.raises(SandboxError, match="缓冲上限"):
        await asyncio.wait_for(proc.communicate(), 10)
    await asyncio.wait_for(proc.wait(), 10)


async def test_undecodable_output_chunk_fails_the_stream(client: DaemonClient) -> None:
    """流式进程收到不是合法 base64 的输出块：这一路失败并强杀进程，不悄悄少一块。"""
    proc = await _start(DaemonCommandExecutor(client), "import sys; sys.stdin.read()")
    params = {"processId": proc.process_id, "stream": "stdout", "data": "@@@"}
    # 模拟守护进程发来畸形通知：经读循环的分发入口喂进去
    client._dispatch(  # noqa: SLF001
        json.dumps({"jsonrpc": "2.0", "method": "process/output", "params": params}).encode()
    )
    assert await asyncio.wait_for(proc.wait(), 10) != 0
    assert proc.stdout is not None and proc.stderr is not None
    with pytest.raises(SandboxError, match="无法解码"):
        await asyncio.wait_for(proc.stdout.read(), 10)
    assert await asyncio.wait_for(proc.stderr.read(), 10) == b""


async def test_stdin_round_trip_then_close(client: DaemonClient) -> None:
    """逐行写入、逐行读回；close() 后进程读到 EOF 正常退出，communicate() 返回剩余输出。"""
    proc = await _start(DaemonCommandExecutor(client), ECHO)
    assert proc.stdin is not None
    for word in (b"a", b"b"):
        await _send(proc, word + b"\n")
        assert await _readline(proc) == b"got:" + word + b"\n"
    # 一次写多行
    await _send(proc, b"c\nd\n")
    assert [await _readline(proc), await _readline(proc)] == [b"got:c\n", b"got:d\n"]
    # 写了不等 drain 就关：缓冲里的数据先送达，进程再读到 EOF
    proc.stdin.write(b"last\n")
    proc.stdin.close()
    assert proc.stdin.is_closing()
    with pytest.raises(BrokenPipeError):
        proc.stdin.write(b"after close\n")
    assert await asyncio.wait_for(proc.communicate(), 10) == (b"got:last\nbye\n", b"")
    assert proc.returncode == 0


async def test_communicate_closes_stdin(client: DaemonClient) -> None:
    """communicate() 与 asyncio 一致：先关标准输入，进程读到 EOF 退出，返回剩余输出。"""
    proc = await _start(DaemonCommandExecutor(client), ECHO)
    await _send(proc, b"x\n")
    assert await asyncio.wait_for(proc.communicate(), 10) == (b"got:x\nbye\n", b"")
    assert proc.returncode == 0


async def test_write_larger_than_one_request_is_split(client: DaemonClient) -> None:
    """一次写入超过单次 ``process/write`` 的内容上限：分块发出，进程收到完整数据。"""
    proc = await _start(
        DaemonCommandExecutor(client), "import sys; print(len(sys.stdin.buffer.read()))"
    )
    assert proc.stdin is not None
    size = protocol.MAX_FILE_BYTES + 1
    proc.stdin.write(b"x" * size)
    await asyncio.wait_for(proc.stdin.drain(), 60)
    proc.stdin.close()
    stdout, _ = await asyncio.wait_for(proc.communicate(), 60)
    assert stdout == f"{size}\n".encode()


async def test_write_waits_beyond_the_request_timeout(root: Path) -> None:
    """``process/write`` 不受请求超时约束：进程迟迟不读、写入停在背压上超过请求时限，照样送达。"""
    transport = await StdioTransport.spawn(daemon_argv(root))
    daemon = await DaemonClient.connect(transport, request_timeout_seconds=1.0)
    try:
        proc = await _start(
            DaemonCommandExecutor(daemon),
            "import sys, time; time.sleep(2); print(len(sys.stdin.buffer.read()))",
        )
        assert proc.stdin is not None
        # 远大于「管道容量 + 守护进程写缓冲水位线」：进程开始读之前这次写必然停在背压上
        size = 4 * 1024 * 1024
        proc.stdin.write(b"x" * size)
        loop = asyncio.get_running_loop()
        started = loop.time()
        await asyncio.wait_for(proc.stdin.drain(), 20)
        # 确实等过了请求时限（否则这个用例证明不了什么）
        assert loop.time() - started > 1.0
        proc.stdin.close()
        stdout, _ = await asyncio.wait_for(proc.communicate(), 20)
        assert stdout == f"{size}\n".encode()
    finally:
        await daemon.close()


async def test_request_rejects_both_timeout_options(client: DaemonClient) -> None:
    """同时给 ``timeout_seconds`` 与 ``no_timeout`` 是调用方的错误。"""
    with pytest.raises(ValueError, match="no_timeout"):
        await client.request(
            protocol.METHOD_PROCESS_KILL, {"processId": "x"}, timeout_seconds=1, no_timeout=True
        )


async def test_cancelled_drain_still_delivers_the_inflight_chunk(
    client: DaemonClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """drain 在途时被取消：在途那一块照常送达，剩下的下一次 drain 接着发，字节完整有序。

    把分块上限调小，让一行分成多块；占住连接的发送锁（相当于同一连接上别的请求正在发），让在途
    的那次 ``process/write`` 停在发出之前——这时取消，若不隔离取消，这一块就丢了。
    """
    monkeypatch.setattr(streaming, "_WRITE_CHUNK_BYTES", 4)
    proc = await _start(DaemonCommandExecutor(client), CAT)
    stdin = proc._stdin_stream  # noqa: SLF001
    line = b"0123456789abcdef\n"
    stdin.write(line)
    async with client._send_lock:  # noqa: SLF001
        drain = asyncio.ensure_future(stdin.drain())
        await _until(lambda: stdin._inflight is not None)  # noqa: SLF001
        drain.cancel()
        with pytest.raises(asyncio.CancelledError):
            await drain
    await _send(proc, b"tail\n")
    stdout, _ = await asyncio.wait_for(proc.communicate(), 10)
    assert stdout == line + b"tail\n"


async def test_close_waits_for_the_drain_in_flight(
    client: DaemonClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """close 时恰有 drain 在途：后台关闭等它发完、再发完新追加的缓冲，最后才关。"""
    monkeypatch.setattr(streaming, "_WRITE_CHUNK_BYTES", 4)
    proc = await _start(DaemonCommandExecutor(client), CAT)
    stdin = proc._stdin_stream  # noqa: SLF001
    stdin.write(b"first line\n")
    async with client._send_lock:  # noqa: SLF001
        drain = asyncio.ensure_future(stdin.drain())
        await _until(lambda: stdin._inflight is not None)  # noqa: SLF001
        stdin.write(b"second\n")
        stdin.close()
    await asyncio.wait_for(drain, 10)
    await asyncio.wait_for(stdin.wait_closed(), 10)
    stdout, _ = await asyncio.wait_for(proc.communicate(), 10)
    assert stdout == b"first line\nsecond\n"
    assert proc.returncode == 0


async def test_without_stdin_the_process_is_not_streaming(client: DaemonClient) -> None:
    """``stdin=False`` 仍返回原来的 RemoteProcess：一次性收输出，没有流。"""
    proc = await DaemonCommandExecutor(client).start(
        taifeng.CommandSpec(command="echo hi; cat", shell=True, cwd=None, env=ENV)
    )
    assert type(proc) is RemoteProcess
    assert not isinstance(proc, taifeng.StreamingCommandProcess)
    # 标准输入照旧是空的：cat 立刻读到 EOF
    assert await asyncio.wait_for(proc.communicate(), 10) == (b"hi\n", b"")


@pytest.mark.parametrize("how", ["close_client", "kill_daemon"])
async def test_connection_loss_ends_the_streams(root: Path, how: str) -> None:
    """连接断开：挂着的 readline 拿到 EOF，wait() 返回，之后 write / drain 抛 BrokenPipeError。"""
    transport = await StdioTransport.spawn(daemon_argv(root))
    daemon = await DaemonClient.connect(transport)
    try:
        proc = await _start(DaemonCommandExecutor(daemon), ECHO)
        await _send(proc, b"ping\n")
        assert await _readline(proc) == b"got:ping\n"
        assert proc.stdout is not None and proc.stdin is not None
        pending = asyncio.ensure_future(proc.stdout.readline())
        await asyncio.sleep(0)
        assert not pending.done()
        if how == "close_client":
            await daemon.close()
        else:
            transport._proc.kill()  # noqa: SLF001 —— 模拟守护进程意外死掉
        assert await asyncio.wait_for(pending, 10) == b""
        assert await asyncio.wait_for(proc.wait(), 10) != 0
        with pytest.raises(BrokenPipeError):
            proc.stdin.write(b"late\n")
        with pytest.raises(BrokenPipeError):
            await proc.stdin.drain()
        assert proc.stdin.is_closing()
    finally:
        await daemon.close()


async def test_write_after_exit_is_a_broken_pipe(client: DaemonClient) -> None:
    """进程退出后再写：write() 与 drain() 都抛 BrokenPipeError。"""
    proc = await _start(DaemonCommandExecutor(client), "pass")
    assert await asyncio.wait_for(proc.wait(), 10) == 0
    assert proc.stdin is not None
    with pytest.raises(BrokenPipeError):
        proc.stdin.write(b"late\n")
    with pytest.raises(BrokenPipeError):
        await proc.stdin.drain()


async def test_write_after_the_process_closed_its_stdin(client: DaemonClient) -> None:
    """进程自己关了标准输入（守护进程回 ERROR_IO）：drain() 抛 BrokenPipeError，写入端按已断算。"""
    proc = await _start(
        DaemonCommandExecutor(client),
        "import os, time; os.close(0); print('ready', flush=True); time.sleep(30)",
    )
    assert await _readline(proc) == b"ready\n"
    assert proc.stdin is not None
    proc.stdin.write(b"x")
    with pytest.raises(BrokenPipeError) as caught:
        await asyncio.wait_for(proc.stdin.drain(), 10)
    # 消息不层层套娃：本端一句话，后面跟守护进程的原因
    message = str(caught.value)
    assert message.startswith(f"进程 {proc.process_id} 的标准输入已不通：")
    assert message.count("已不通") == 1
    assert proc.stdin.is_closing()
    with pytest.raises(BrokenPipeError):
        proc.stdin.write(b"y")
    proc.kill()
    assert await asyncio.wait_for(proc.wait(), 10) != 0


async def test_stderr_streams_too(client: DaemonClient) -> None:
    """stderr 也能流式读：readline 与 read(n)。"""
    proc = await _start(DaemonCommandExecutor(client), ERR_ECHO)
    assert proc.stdin is not None and proc.stderr is not None and proc.stdout is not None
    await _send(proc, b"x\n")
    assert await asyncio.wait_for(proc.stderr.readline(), 10) == b"err:x\n"
    # read(n)：有数据就返回，至多 n 字节
    await _send(proc, b"hello\n")
    pieces: list[bytes] = []
    while sum(map(len, pieces)) < len(b"err:hello\n"):
        pieces.append(await asyncio.wait_for(proc.stderr.read(4), 10))
        assert 0 < len(pieces[-1]) <= 4
    assert b"".join(pieces) == b"err:hello\n"
    proc.stdin.close()
    assert await asyncio.wait_for(proc.wait(), 10) == 0
    assert await asyncio.wait_for(proc.stderr.read(), 10) == b""
    assert await asyncio.wait_for(proc.stdout.read(), 10) == b""


async def test_kill_gives_readers_eof(client: DaemonClient) -> None:
    """kill() 之后挂着的读取方拿到 EOF，wait() 返回非零。"""
    proc = await _start(DaemonCommandExecutor(client), ECHO)
    await _send(proc, b"ping\n")
    assert await _readline(proc) == b"got:ping\n"
    assert proc.stdout is not None and proc.stderr is not None
    pending_out = asyncio.ensure_future(proc.stdout.readline())
    pending_err = asyncio.ensure_future(proc.stderr.read())
    await asyncio.sleep(0)
    assert not pending_out.done() and not pending_err.done()
    proc.kill()
    assert await asyncio.wait_for(pending_out, 10) == b""
    assert await asyncio.wait_for(pending_err, 10) == b""
    assert await asyncio.wait_for(proc.wait(), 10) != 0


class _LostConnection:
    """连接已断的客户端替身：任何请求都失败。"""

    closed = True

    def __init__(self) -> None:
        """记下收到过哪些请求。"""
        self.methods: list[str] = []

    async def request(self, method: str, params: dict[str, Any] | None = None, **_: Any) -> None:
        """一律按连接已断失败。"""
        self.methods.append(method)
        raise SandboxProtocolError("守护进程连接已断开")


@pytest.mark.parametrize("unsent", [b"", b"tail\n"])
async def test_close_on_a_lost_connection_retrieves_the_failure(
    caplog: pytest.LogCaptureFixture, unsent: bytes
) -> None:
    """连接已断时关闭写入端：后台关闭的失败被取走并记 debug 日志，不会变成没人取的任务异常。"""
    connection = _LostConnection()
    stdin = _StreamInput(connection, "p_lost")  # type: ignore[arg-type]
    if unsent:
        stdin.write(unsent)
    with caplog.at_level(logging.DEBUG, logger="taifeng_sandbox.daemon.streaming"):
        stdin.close()
        # wait_closed 透传后台任务的结果：任务若带着异常结束，这里会抛
        await asyncio.wait_for(stdin.wait_closed(), 5)
    assert stdin.is_closing()
    expected = protocol.METHOD_PROCESS_WRITE if unsent else protocol.METHOD_PROCESS_CLOSE_STDIN
    assert connection.methods == [expected]
    assert [record.levelno for record in caplog.records] == [logging.DEBUG]
    assert "p_lost" in caplog.text
