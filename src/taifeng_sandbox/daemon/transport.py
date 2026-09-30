"""宿主到守护进程的传输层：按行收发字节。

线协议不关心字节怎么到达守护进程。当前只有一种传输——子进程的 stdin / stdout；
``docker exec -i``、``kubectl exec -i``、``ssh`` 都能归到这一种，区别只在启动命令。
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Protocol

from taifeng_sandbox.daemon.protocol import MAX_MESSAGE_BYTES
from taifeng_sandbox.errors import SandboxProtocolError, SandboxUnavailableError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

# 保留守护进程 stderr 的末尾若干字节，用于连接失败时的诊断
_STDERR_TAIL_BYTES = 8 * 1024
_CLOSE_GRACE_SECONDS = 3.0


class Transport(Protocol):
    """按行收发的双向通道。"""

    async def send(self, line: bytes) -> None:
        """发送一行（调用方保证以换行结尾）。"""
        ...

    async def receive(self) -> bytes | None:
        """接收一行；对端关闭返回 None。"""
        ...

    async def close(self) -> None:
        """关闭通道并释放资源。"""
        ...

    def diagnostics(self) -> str:
        """对端留下的诊断信息（如 stderr 末尾），用于报错。"""
        ...


class StdioTransport:
    """经子进程 stdin / stdout 通信的传输。"""

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        """包装一个 stdin / stdout / stderr 都已接管道的子进程。"""
        if proc.stdin is None or proc.stdout is None:
            raise SandboxProtocolError("守护进程子进程必须接好 stdin / stdout 管道")
        self._proc = proc
        self._stdin = proc.stdin
        self._stdout = proc.stdout
        self._stderr_tail = bytearray()
        self._stderr_task: asyncio.Task[None] | None = None
        if proc.stderr is not None:
            self._stderr_task = asyncio.ensure_future(self._drain_stderr(proc.stderr))
        self._closed = False

    @classmethod
    async def spawn(
        cls,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
    ) -> StdioTransport:
        """启动承载守护进程的子进程。

        Args:
            argv: 启动命令，如 ``["docker", "exec", "-i", 容器, "python3", "-c", 源码, ...]``。
            env: 子进程环境；None 表示空环境（不继承宿主环境）。
            cwd: 子进程工作目录。

        Raises:
            SandboxUnavailableError: 启动命令不存在或无法执行。
        """
        if not argv:
            raise SandboxUnavailableError("守护进程启动命令为空")
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=dict(env) if env is not None else {},
                cwd=cwd,
                limit=MAX_MESSAGE_BYTES,
                start_new_session=True,
            )
        except OSError as exc:
            raise SandboxUnavailableError(f"无法启动守护进程：{exc}") from exc
        return cls(proc)

    async def _drain_stderr(self, stream: asyncio.StreamReader) -> None:
        """持续读 stderr，只留末尾一段；不读的话管道写满会卡住守护进程。"""
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                return
            self._stderr_tail.extend(chunk)
            overflow = len(self._stderr_tail) - _STDERR_TAIL_BYTES
            if overflow > 0:
                del self._stderr_tail[:overflow]

    async def send(self, line: bytes) -> None:
        """写一行到守护进程 stdin。"""
        try:
            self._stdin.write(line)
            await self._stdin.drain()
        except (ConnectionError, RuntimeError) as exc:
            raise SandboxProtocolError(f"向守护进程写入失败：{exc}") from exc

    async def receive(self) -> bytes | None:
        """从守护进程 stdout 读一行；EOF 返回 None。"""
        try:
            line = await self._stdout.readline()
        except ValueError as exc:
            raise SandboxProtocolError("守护进程发来的消息超过长度上限") from exc
        return line or None

    def diagnostics(self) -> str:
        """守护进程 stderr 的末尾内容。"""
        return self._stderr_tail.decode("utf-8", errors="replace").strip()

    async def close(self) -> None:
        """关 stdin 让守护进程自行收尾；超时未退出则强杀。"""
        if self._closed:
            return
        self._closed = True
        if not self._stdin.is_closing():
            self._stdin.close()
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=_CLOSE_GRACE_SECONDS)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                self._proc.kill()
            await self._proc.wait()
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._stderr_task


__all__ = ["StdioTransport", "Transport"]
