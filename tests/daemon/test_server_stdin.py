"""线协议 v2：进程的标准输入，以及守护进程侧强杀在 shell 已退出后仍按组杀。

照 ``conftest.py`` 的方式把真守护进程跑起来（源码经 ``python -c`` 注入），用 ``DaemonClient``
直接发请求，不经宿主侧的执行器。
"""

from __future__ import annotations

import asyncio
import base64
import os
import sys
from typing import Any

import pytest

from taifeng_sandbox import SandboxRemoteError
from taifeng_sandbox.daemon import DaemonClient, protocol

ENV = {"PATH": "/usr/bin:/bin"}

# 远大于「管道（或 socketpair）容量 + 守护进程写缓冲水位线」，对端不读时写入必然停在背压上
BIG = 1 << 20


def _b64(data: bytes) -> str:
    """二进制内容按线协议编码。"""
    return base64.b64encode(data).decode("ascii")


async def _blocked_write(client: DaemonClient, process_id: str) -> asyncio.Task[dict[str, Any]]:
    """向不读标准输入的进程发一次大块写入，确认它停在背压上，返回尚未完成的请求。"""
    pending = asyncio.ensure_future(
        client.request("process/write", {"processId": process_id, "data": _b64(b"x" * BIG)})
    )
    # 只用于确认前提成立：对端不读，这次写在任何时长内都不该完成
    await asyncio.sleep(0.3)
    assert not pending.done()
    return pending


class _Output:
    """收集一个进程的输出与退出通知。"""

    def __init__(self, client: DaemonClient, process_id: str) -> None:
        """登记这个进程的通知处理函数。"""
        self.stdout = bytearray()
        self.exited = asyncio.Event()
        self._changed = asyncio.Event()
        client.on_notification(protocol.NOTIFY_PROCESS_OUTPUT, process_id, self._on_output)
        client.on_notification(protocol.NOTIFY_PROCESS_EXITED, process_id, self._on_exited)

    def _on_output(self, params: dict[str, Any]) -> None:
        """累积 stdout。"""
        if params["stream"] == "stdout":
            self.stdout.extend(base64.b64decode(params["data"]))
            self._changed.set()

    def _on_exited(self, params: dict[str, Any]) -> None:
        """记下退出。"""
        self.exited.set()

    async def wait_stdout(self, expected: bytes) -> None:
        """等到 stdout 累积到 ``expected``；调用方用 ``asyncio.timeout`` 限时。"""
        while bytes(self.stdout) != expected:
            self._changed.clear()
            await self._changed.wait()


async def test_process_with_stdin_round_trip(client: DaemonClient) -> None:
    """stdin=True 启动后，process/write 写入、process/closeStdin 关闭，输出照常经通知回来。"""
    chunks: list[bytes] = []
    exited = asyncio.Event()
    client.on_notification(
        "process/output", "p1", lambda p: chunks.append(base64.b64decode(p["data"]))
    )
    client.on_notification("process/exited", "p1", lambda p: exited.set())
    # 空环境也能起 Python 解释器（实测）；这里正好顺带验证进程环境完全由请求给出
    await client.request(
        "process/start",
        {
            "processId": "p1",
            "argv": [sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"],
            "env": {},
            "stdin": True,
        },
    )
    reply = await client.request("process/write", {"processId": "p1", "data": _b64(b"hi")})
    assert reply == {"bytesWritten": 2}
    assert await client.request("process/closeStdin", {"processId": "p1"}) == {}
    await asyncio.wait_for(exited.wait(), 10)
    assert b"".join(chunks) == b"HI\n"


async def test_write_to_a_process_without_stdin_is_an_error(client: DaemonClient) -> None:
    """没要 stdin 的进程不能写：返回 ERROR_INVALID_PARAMS，而不是静默丢弃。"""
    await client.request("process/start", {"processId": "p2", "argv": ["sleep", "5"], "env": ENV})
    with pytest.raises(SandboxRemoteError) as caught:
        await client.request("process/write", {"processId": "p2", "data": ""})
    assert caught.value.code == protocol.ERROR_INVALID_PARAMS
    await client.request("process/kill", {"processId": "p2"})


async def test_write_to_unknown_process(client: DaemonClient) -> None:
    """未知进程：ERROR_PROCESS_UNKNOWN。"""
    with pytest.raises(SandboxRemoteError) as caught:
        await client.request("process/write", {"processId": "nope", "data": ""})
    assert caught.value.code == protocol.ERROR_PROCESS_UNKNOWN


@pytest.mark.parametrize("value", ["yes", 1, 0, []])
async def test_stdin_param_must_be_boolean(client: DaemonClient, value: object) -> None:
    """``stdin`` 给了却不是布尔值：ERROR_INVALID_PARAMS，进程不启动。"""
    params = {"processId": "p3", "argv": ["true"], "env": ENV, "stdin": value}
    with pytest.raises(SandboxRemoteError) as caught:
        await client.request("process/start", params)
    assert caught.value.code == protocol.ERROR_INVALID_PARAMS
    # 没有登记进进程表：同一个 processId 还能正常启动
    await client.request("process/start", {**params, "stdin": False})


@pytest.mark.parametrize("value", ["omitted", None, False])
async def test_stdin_omitted_null_or_false_means_no_stdin(
    client: DaemonClient, value: object
) -> None:
    """省略、null、false 都是不要标准输入（与其他可选参数一致）：启动成功，写入是参数错误。"""
    params: dict[str, Any] = {"processId": "p3n", "argv": ["sleep", "5"], "env": ENV}
    if value != "omitted":
        params["stdin"] = value
    await client.request("process/start", params)
    with pytest.raises(SandboxRemoteError) as caught:
        await client.request("process/write", {"processId": "p3n", "data": _b64(b"x")})
    assert caught.value.code == protocol.ERROR_INVALID_PARAMS
    await client.request("process/kill", {"processId": "p3n"})


async def test_write_rejects_invalid_base64(client: DaemonClient) -> None:
    """``data`` 不是合法 base64：ERROR_INVALID_PARAMS。"""
    await client.request(
        "process/start", {"processId": "p4", "argv": ["cat"], "env": ENV, "stdin": True}
    )
    with pytest.raises(SandboxRemoteError) as caught:
        await client.request("process/write", {"processId": "p4", "data": "@@@@"})
    assert caught.value.code == protocol.ERROR_INVALID_PARAMS
    await client.request("process/kill", {"processId": "p4"})


async def test_oversized_write_is_rejected(client: DaemonClient) -> None:
    """单次写入解码后超过上限：ERROR_TOO_LARGE，不写进管道。"""
    await client.request(
        "process/start", {"processId": "p5", "argv": ["cat"], "env": ENV, "stdin": True}
    )
    data = _b64(b"x" * (protocol.MAX_FILE_BYTES + 1))
    with pytest.raises(SandboxRemoteError) as caught:
        await client.request("process/write", {"processId": "p5", "data": data})
    assert caught.value.code == protocol.ERROR_TOO_LARGE
    await client.request("process/kill", {"processId": "p5"})


async def test_write_after_exit_is_an_error(client: DaemonClient) -> None:
    """收到退出通知之后再写：进程已移出进程表，是 ERROR_PROCESS_UNKNOWN。"""
    output = _Output(client, "p6")
    await client.request(
        "process/start",
        {"processId": "p6", "argv": ["/bin/sh", "-c", "exit 0"], "env": ENV, "stdin": True},
    )
    await asyncio.wait_for(output.exited.wait(), 10)
    with pytest.raises(SandboxRemoteError) as caught:
        await client.request("process/write", {"processId": "p6", "data": _b64(b"late\n")})
    assert caught.value.code == protocol.ERROR_PROCESS_UNKNOWN


async def test_write_after_close_stdin_is_an_error(client: DaemonClient) -> None:
    """标准输入关了之后再写：ERROR_IO，而不是被管道静默丢弃。"""
    await client.request(
        "process/start",
        {"processId": "p7", "argv": ["sleep", "5"], "env": ENV, "stdin": True},
    )
    assert await client.request("process/closeStdin", {"processId": "p7"}) == {}
    with pytest.raises(SandboxRemoteError) as caught:
        await client.request("process/write", {"processId": "p7", "data": _b64(b"x")})
    assert caught.value.code == protocol.ERROR_IO
    await client.request("process/kill", {"processId": "p7"})


async def test_close_stdin_is_idempotent_and_needs_a_known_process(client: DaemonClient) -> None:
    """没要 stdin 的进程、已关过的 stdin 再关都不算错；未知进程是 ERROR_PROCESS_UNKNOWN。"""
    await client.request("process/start", {"processId": "p8", "argv": ["sleep", "5"], "env": ENV})
    assert await client.request("process/closeStdin", {"processId": "p8"}) == {}
    await client.request(
        "process/start",
        {"processId": "p9", "argv": ["sleep", "5"], "env": ENV, "stdin": True},
    )
    assert await client.request("process/closeStdin", {"processId": "p9"}) == {}
    assert await client.request("process/closeStdin", {"processId": "p9"}) == {}
    with pytest.raises(SandboxRemoteError) as caught:
        await client.request("process/closeStdin", {"processId": "nope"})
    assert caught.value.code == protocol.ERROR_PROCESS_UNKNOWN
    for process_id in ("p8", "p9"):
        await client.request("process/kill", {"processId": process_id})


async def test_write_after_the_process_closed_its_stdin(client: DaemonClient) -> None:
    """进程自己关了标准输入（对端已关）再写：ERROR_IO。"""
    output = _Output(client, "p15")
    await client.request(
        "process/start",
        {
            "processId": "p15",
            "argv": ["/bin/sh", "-c", "exec 0<&-; echo ready; sleep 5"],
            "env": ENV,
            "stdin": True,
        },
    )
    async with asyncio.timeout(10):
        await output.wait_stdout(b"ready\n")
    with pytest.raises(SandboxRemoteError) as caught:
        await client.request("process/write", {"processId": "p15", "data": _b64(b"x")})
    assert caught.value.code == protocol.ERROR_IO
    await client.request("process/kill", {"processId": "p15"})


async def test_write_stuck_on_backpressure_fails_once_the_process_is_killed(
    client: DaemonClient,
) -> None:
    """对端不读、写入停在背压上：kill 之后这次写很快返回 ERROR_IO，不会一直挂着。"""
    output = _Output(client, "p16")
    await client.request(
        "process/start", {"processId": "p16", "argv": ["sleep", "30"], "env": ENV, "stdin": True}
    )
    pending = await _blocked_write(client, "p16")
    await client.request("process/kill", {"processId": "p16"})
    with pytest.raises(SandboxRemoteError) as caught:
        await asyncio.wait_for(pending, 5)
    assert caught.value.code == protocol.ERROR_IO
    await asyncio.wait_for(output.exited.wait(), 5)


async def test_close_stdin_with_unsent_data_returns_at_once(client: DaemonClient) -> None:
    """守护进程写缓冲里还有没进管道的数据：closeStdin 立即返回 ``{}``，之后 kill 照常收到退出。"""
    output = _Output(client, "p17")
    await client.request(
        "process/start", {"processId": "p17", "argv": ["sleep", "30"], "env": ENV, "stdin": True}
    )
    pending = await _blocked_write(client, "p17")
    reply = await client.request("process/closeStdin", {"processId": "p17"}, timeout_seconds=2)
    assert reply == {}
    assert await client.request("process/kill", {"processId": "p17"}) == {"killed": True}
    await asyncio.wait_for(output.exited.wait(), 5)
    # 缓冲里的数据没人收了：那次写如实失败
    with pytest.raises(SandboxRemoteError) as caught:
        await asyncio.wait_for(pending, 5)
    assert caught.value.code == protocol.ERROR_IO


async def test_concurrent_writes_both_succeed_without_interleaving(client: DaemonClient) -> None:
    """两个大块并发写（违反宿主串行约定）：两次都成功，各自整块落入管道，互不交错。"""
    output = _Output(client, "p18")
    await client.request(
        "process/start", {"processId": "p18", "argv": ["cat"], "env": ENV, "stdin": True}
    )
    first, second = b"a" * BIG, b"b" * BIG
    replies = await asyncio.gather(
        client.request("process/write", {"processId": "p18", "data": _b64(first)}),
        client.request("process/write", {"processId": "p18", "data": _b64(second)}),
    )
    assert replies == [{"bytesWritten": BIG}, {"bytesWritten": BIG}]
    await client.request("process/closeStdin", {"processId": "p18"})
    await asyncio.wait_for(output.exited.wait(), 20)
    assert bytes(output.stdout) in (first + second, second + first)


async def test_sequential_writes_keep_order(client: DaemonClient) -> None:
    """每次写都等到响应再发下一次：输出顺序与写入顺序一致。"""
    output = _Output(client, "p10")
    await client.request(
        "process/start", {"processId": "p10", "argv": ["cat"], "env": ENV, "stdin": True}
    )
    lines = [f"line-{index}\n".encode() for index in range(50)]
    for line in lines:
        await client.request("process/write", {"processId": "p10", "data": _b64(line)})
    await client.request("process/closeStdin", {"processId": "p10"})
    await asyncio.wait_for(output.exited.wait(), 10)
    assert bytes(output.stdout) == b"".join(lines)


async def test_process_exits_on_its_own_while_stdin_is_open(client: DaemonClient) -> None:
    """要了 stdin 但宿主一直不关：进程自己退出时照常收到退出通知，不会被开着的管道挂住。"""
    output = _Output(client, "p11")
    await client.request(
        "process/start",
        {"processId": "p11", "argv": ["/bin/sh", "-c", "echo bye"], "env": ENV, "stdin": True},
    )
    await asyncio.wait_for(output.exited.wait(), 10)
    assert bytes(output.stdout) == b"bye\n"


async def test_kill_reaches_children_after_the_shell_exits(client: DaemonClient) -> None:
    """shell 先退出、它派生的子进程还占着管道：守护进程侧 kill 仍按组杀到（内核 ADR 0108）。"""
    output = _Output(client, "p12")
    started = await client.request(
        "process/start",
        {"processId": "p12", "argv": ["/bin/sh", "-c", "sleep 30 & echo started"], "env": ENV},
    )
    async with asyncio.timeout(10):
        await output.wait_stdout(b"started\n")
        # 等到 shell 确实已退出并被回收；sleep 还占着 stdout / stderr，进程仍在进程表里
        while True:
            try:
                os.kill(started["pid"], 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.02)
    assert not output.exited.is_set()
    assert await client.request("process/kill", {"processId": "p12"}) == {"killed": True}
    await asyncio.wait_for(output.exited.wait(), 5)
