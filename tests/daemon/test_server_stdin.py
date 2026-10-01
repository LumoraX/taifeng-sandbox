"""线协议 v2：进程的标准输入，以及守护进程侧强杀在 shell 已退出后仍按组杀。

照 ``conftest.py`` 的方式把真守护进程跑起来（源码经 ``python -c`` 注入），用 ``DaemonClient``
直接发请求，不经宿主侧的执行器。
"""

from __future__ import annotations

import asyncio
import base64
import sys
from typing import Any

import pytest

from taifeng_sandbox import SandboxRemoteError
from taifeng_sandbox.daemon import DaemonClient, protocol

ENV = {"PATH": "/usr/bin:/bin"}


def _b64(data: bytes) -> str:
    """二进制内容按线协议编码。"""
    return base64.b64encode(data).decode("ascii")


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
    """进程已退出后再写不能算成功。"""
    output = _Output(client, "p6")
    await client.request(
        "process/start",
        {"processId": "p6", "argv": ["/bin/sh", "-c", "exit 0"], "env": ENV, "stdin": True},
    )
    await asyncio.wait_for(output.exited.wait(), 10)
    with pytest.raises(SandboxRemoteError) as caught:
        await client.request("process/write", {"processId": "p6", "data": _b64(b"late\n")})
    assert caught.value.code in (protocol.ERROR_IO, protocol.ERROR_PROCESS_UNKNOWN)


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
    await client.request(
        "process/start",
        {"processId": "p12", "argv": ["/bin/sh", "-c", "sleep 30 & echo started"], "env": ENV},
    )
    async with asyncio.timeout(10):
        await output.wait_stdout(b"started\n")
    await asyncio.sleep(0.5)  # 让 shell 退出；sleep 还占着 stdout / stderr，进程仍在进程表里
    assert await client.request("process/kill", {"processId": "p12"}) == {"killed": True}
    await asyncio.wait_for(output.exited.wait(), 5)
