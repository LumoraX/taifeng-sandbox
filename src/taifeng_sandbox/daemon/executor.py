"""经守护进程执行命令的 ``CommandExecutor`` 实现。"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import secrets
from typing import TYPE_CHECKING, Any

from taifeng_sandbox.daemon import protocol
from taifeng_sandbox.errors import SandboxError, SandboxProtocolError, SandboxRemoteError
from taifeng_sandbox.local.executor import command_argv

if TYPE_CHECKING:
    from taifeng import CommandProcess, CommandSpec

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
    """守护进程里一个进程的宿主侧句柄，满足 taifeng ``CommandProcess`` 协议。"""

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
        """收到一块输出。"""
        try:
            chunk = base64.b64decode(str(params.get("data", "")), validate=True)
        except (binascii.Error, ValueError):
            logger.warning("进程 %s 的输出块不是合法 base64，已丢弃", self._process_id)
            return
        target = self._stderr if params.get("stream") == "stderr" else self._stdout
        target.append(chunk)

    def _on_exited(self, params: dict[str, Any]) -> None:
        """收到退出通知。守护进程保证它排在该进程所有输出之后。"""
        code = params.get("exitCode")
        self._finish(code if isinstance(code, int) else _LOST_EXIT_CODE)

    def _on_disconnect(self) -> None:
        """连接断开：守护进程会杀掉名下进程，这里按被杀处理。"""
        if self._returncode is None:
            self._finish(_LOST_EXIT_CODE)

    def _finish(self, code: int) -> None:
        """记录退出码并释放登记。"""
        self._returncode = code
        self._exited.set()
        self.abandon()

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


def _raise_spawn_error(exc: SandboxRemoteError) -> None:
    """把守护进程的启动失败还原成宿主侧的 ``OSError`` 子类。"""
    if exc.code == protocol.ERROR_SPAWN_NOT_FOUND:
        raise FileNotFoundError(str(exc)) from exc
    raise SandboxError(f"守护进程启动命令失败：{exc}") from exc


class DaemonCommandExecutor:
    """把命令交给守护进程执行。

    环境提供者（docker / k8s 等）只负责拉起环境并建立到守护进程的连接；所有这类后端共用
    本执行器（ADR 0002 决策 2）。
    """

    def __init__(
        self,
        client: DaemonClient,
        *,
        default_cwd: str | None = None,
        max_buffer_bytes: int = DEFAULT_MAX_BUFFER_BYTES,
    ) -> None:
        """
        Args:
            client: 已握手的守护进程连接。
            default_cwd: ``CommandSpec.cwd`` 为 None 时使用的工作目录；仍为 None 则用
                守护进程的根目录。
            max_buffer_bytes: 每路输出在宿主侧保留的字节上限。
        """
        self._client = client
        self._default_cwd = default_cwd
        self._max_buffer_bytes = max_buffer_bytes

    async def start(self, spec: CommandSpec) -> CommandProcess:
        """启动进程并立即返回。启动失败抛 ``OSError``（含子类）。"""
        process_id = "p_" + secrets.token_hex(8)
        process = RemoteProcess(
            self._client, process_id, max_buffer_bytes=self._max_buffer_bytes
        )
        cwd = spec.cwd if spec.cwd is not None else self._default_cwd
        try:
            result = await self._client.request(
                protocol.METHOD_PROCESS_START,
                {
                    "processId": process_id,
                    "argv": command_argv(spec),
                    "cwd": cwd,
                    "env": dict(spec.env),
                },
            )
        except SandboxRemoteError as exc:
            process.abandon()
            _raise_spawn_error(exc)
            raise  # pragma: no cover —— 上一行必然抛出，仅为类型收窄
        except BaseException:
            process.abandon()
            raise
        pid = result.get("pid")
        process.pid = pid if isinstance(pid, int) else None
        return process


__all__ = ["DEFAULT_MAX_BUFFER_BYTES", "DaemonCommandExecutor", "RemoteProcess"]
