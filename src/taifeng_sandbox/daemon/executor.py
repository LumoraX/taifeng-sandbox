"""经守护进程执行命令的 ``CommandExecutor`` 实现。

进程句柄在 ``daemon/process.py``（一次性收输出的 ``RemoteProcess``）与 ``daemon/streaming.py``
（流式的 ``StreamingRemoteProcess``）。``RemoteProcess`` 与 ``DEFAULT_MAX_BUFFER_BYTES`` 仍从
这里转出，保持原有的导入路径可用。
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING, Any

from taifeng_sandbox.daemon import protocol
from taifeng_sandbox.daemon.process import DEFAULT_MAX_BUFFER_BYTES, RemoteProcess
from taifeng_sandbox.daemon.streaming import StreamingRemoteProcess
from taifeng_sandbox.errors import SandboxError, SandboxRemoteError
from taifeng_sandbox.local.executor import command_argv

if TYPE_CHECKING:
    from taifeng import CommandProcess, CommandSpec

    from taifeng_sandbox.daemon.client import DaemonClient


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
            max_buffer_bytes: 一次性收输出的进程：每路输出在宿主侧保留的字节上限，超出丢弃
                并计数。流式进程（``CommandSpec.stdin=True``）：每路**未读**字节的上限，超出
                就杀掉进程、让读取方拿到 ``SandboxError``。
        """
        self._client = client
        self._default_cwd = default_cwd
        self._max_buffer_bytes = max_buffer_bytes

    async def start(self, spec: CommandSpec) -> CommandProcess:
        """启动进程并立即返回。启动失败抛 ``OSError``（含子类）。

        ``spec.stdin`` 为真时返回 ``StreamingRemoteProcess``（满足 ``StreamingCommandProcess``），
        否则返回一次性收输出的 ``RemoteProcess``。
        """
        process_id = "p_" + secrets.token_hex(8)
        params: dict[str, Any] = {
            "processId": process_id,
            "argv": command_argv(spec),
            "cwd": spec.cwd if spec.cwd is not None else self._default_cwd,
            "env": dict(spec.env),
        }
        process: RemoteProcess
        if spec.stdin:
            process = StreamingRemoteProcess(
                self._client, process_id, max_buffer_bytes=self._max_buffer_bytes
            )
            params["stdin"] = True
        else:
            process = RemoteProcess(
                self._client, process_id, max_buffer_bytes=self._max_buffer_bytes
            )
        try:
            result = await self._client.request(protocol.METHOD_PROCESS_START, params)
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
