"""守护进程里一个进程的宿主侧句柄：通知登记、强杀、退出码，以及一次性收齐的输出。

流式进程（``taifeng_sandbox.daemon.streaming``）继承这里的 ``RemoteProcess``，只覆盖输出的去向。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
from typing import TYPE_CHECKING, Any

from taifeng_sandbox.daemon import protocol
from taifeng_sandbox.errors import SandboxProtocolError, SandboxRemoteError

if TYPE_CHECKING:
    from taifeng_sandbox.daemon.client import DaemonClient

logger = logging.getLogger(__name__)

# 宿主侧为每路输出保留的字节上限。taifeng 工具层还会再按自己的上限截断；
# 这里只是防止失控的输出把宿主内存吃光。
DEFAULT_MAX_BUFFER_BYTES = 16 * 1024 * 1024

# 连接断开导致拿不到真实退出码时使用的退出码（POSIX 下 SIGKILL 为 -9）
_LOST_EXIT_CODE = -9


class _Buffer:
    """有上限的输出缓冲：超出部分丢弃并记下丢了多少。"""

    def __init__(self, limit: int) -> None:
        """``limit`` 为保留的字节上限。"""
        self._limit = limit
        self._chunks: list[bytes] = []
        self._size = 0
        self.dropped = 0

    def append(self, chunk: bytes) -> None:
        """追加一块输出。"""
        room = self._limit - self._size
        if room <= 0:
            self.dropped += len(chunk)
            return
        kept = chunk[:room]
        self._chunks.append(kept)
        self._size += len(kept)
        self.dropped += len(chunk) - len(kept)

    def value(self) -> bytes:
        """已保留的全部输出。"""
        return b"".join(self._chunks)


class RemoteProcess:
    """守护进程里一个进程的宿主侧句柄，满足 taifeng ``CommandProcess`` 协议。

    输出一次性收齐：每路按 ``max_buffer_bytes`` 保留，超出丢弃并计数。子类经三个钩子改变
    输出的去向——``_deliver`` 接收每一块解码后的输出，``_on_undecodable`` 处理无法解码的输出块，
    ``_on_finished`` 在进程结束（退出或连接断开）时调用一次；登记、强杀、退出码的处理都在这里，
    子类不必重复。
    """

    def __init__(
        self,
        client: DaemonClient,
        process_id: str,
        *,
        max_buffer_bytes: int = DEFAULT_MAX_BUFFER_BYTES,
    ) -> None:
        """在发起启动请求之前构造并登记通知处理函数，避免漏掉最早的输出。"""
        self._client = client
        self._process_id = process_id
        self._max_buffer_bytes = max_buffer_bytes
        self._stdout = _Buffer(max_buffer_bytes)
        self._stderr = _Buffer(max_buffer_bytes)
        self._returncode: int | None = None
        self._exited = asyncio.Event()
        self._kill_task: asyncio.Task[None] | None = None
        self.pid: int | None = None
        client.on_notification(protocol.NOTIFY_PROCESS_OUTPUT, process_id, self._on_output)
        client.on_notification(protocol.NOTIFY_PROCESS_EXITED, process_id, self._on_exited)
        self._remove_disconnect = client.on_disconnect(self._on_disconnect)

    @property
    def process_id(self) -> str:
        """线协议里的进程标识。"""
        return self._process_id

    @property
    def returncode(self) -> int | None:
        """退出码；未结束为 None。"""
        return self._returncode

    @property
    def dropped_bytes(self) -> int:
        """因超过缓冲上限被丢弃的输出字节数（两路合计）。"""
        return self._stdout.dropped + self._stderr.dropped

    def _on_output(self, params: dict[str, Any]) -> None:
        """收到一块输出：解码后交给 ``_deliver``，解码失败交给 ``_on_undecodable``。"""
        stream = "stderr" if params.get("stream") == "stderr" else "stdout"
        try:
            chunk = base64.b64decode(str(params.get("data", "")), validate=True)
        except (binascii.Error, ValueError):
            self._on_undecodable(stream)
            return
        self._deliver(stream, chunk)

    def _deliver(self, stream: str, chunk: bytes) -> None:
        """处理一块输出（``stream`` 为 ``"stdout"`` 或 ``"stderr"``）：放进有上限的缓冲。"""
        target = self._stderr if stream == "stderr" else self._stdout
        target.append(chunk)

    def _on_undecodable(self, stream: str) -> None:
        """收到不是合法 base64 的输出块：记告警并丢弃这一块。"""
        logger.warning("进程 %s 的 %s 输出块不是合法 base64，已丢弃", self._process_id, stream)

    def _on_exited(self, params: dict[str, Any]) -> None:
        """收到退出通知。守护进程保证它排在该进程所有输出之后。"""
        code = params.get("exitCode")
        self._finish(code if isinstance(code, int) else _LOST_EXIT_CODE)

    def _on_disconnect(self) -> None:
        """连接断开：守护进程会杀掉名下进程，这里按被杀处理。"""
        if self._returncode is None:
            self._finish(_LOST_EXIT_CODE)

    def _finish(self, code: int) -> None:
        """记录退出码、调结束钩子，再唤醒等待者并释放登记。"""
        self._returncode = code
        # 先收尾再唤醒：wait() 返回时各路输出已经结束
        self._on_finished()
        self._exited.set()
        self.abandon()

    def _on_finished(self) -> None:
        """进程结束（退出或连接断开）时调用一次。一次性收输出无需额外收尾。"""

    def abandon(self) -> None:
        """撤销通知登记（进程结束或启动失败时调用）。"""
        self._client.remove_handlers(self._process_id)
        self._remove_disconnect()

    async def wait(self) -> int:
        """等待退出，返回退出码。"""
        await self._exited.wait()
        assert self._returncode is not None
        return self._returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        """等待退出并返回全部（受上限约束的）输出。"""
        await self._exited.wait()
        return self._stdout.value(), self._stderr.value()

    def kill(self) -> None:
        """强杀。协议要求同步返回，所以请求放到后台任务里发。"""
        if self._returncode is not None or self._kill_task is not None:
            return
        self._kill_task = asyncio.ensure_future(self._send_kill())

    async def _send_kill(self) -> None:
        """发强杀请求；连接已断时进程本就会被守护进程清理。"""
        try:
            await self._client.request(
                protocol.METHOD_PROCESS_KILL, {"processId": self._process_id}
            )
        except (SandboxProtocolError, SandboxRemoteError) as exc:
            logger.warning("强杀进程 %s 的请求失败：%s", self._process_id, exc)


__all__ = ["DEFAULT_MAX_BUFFER_BYTES", "RemoteProcess"]
