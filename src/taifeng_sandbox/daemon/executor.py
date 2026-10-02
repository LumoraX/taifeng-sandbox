"""经守护进程执行命令的 ``CommandExecutor`` 实现。

进程句柄在 ``daemon/process.py``（一次性收输出的 ``RemoteProcess``）与 ``daemon/streaming.py``
（流式的 ``StreamingRemoteProcess``）。``RemoteProcess`` 与 ``DEFAULT_MAX_BUFFER_BYTES`` 仍从
这里转出，保持原有的导入路径可用。
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from typing import TYPE_CHECKING, Any

from taifeng_sandbox.daemon import protocol
from taifeng_sandbox.daemon.process import DEFAULT_MAX_BUFFER_BYTES, RemoteProcess
from taifeng_sandbox.daemon.streaming import StreamingRemoteProcess
from taifeng_sandbox.errors import SandboxError, SandboxProtocolError, SandboxRemoteError
from taifeng_sandbox.local.executor import command_argv

if TYPE_CHECKING:
    from taifeng import CommandProcess, CommandSpec

    from taifeng_sandbox.daemon.client import DaemonClient

logger = logging.getLogger(__name__)


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
        # 被放弃的启动请求的善后任务（见 _kill_if_started），持有引用免得被回收
        self._cleanups: set[asyncio.Future[None]] = set()

    async def start(self, spec: CommandSpec) -> CommandProcess:
        """启动进程并立即返回。启动失败抛 ``OSError``（含子类）。

        ``spec.stdin`` 为真时返回 ``StreamingRemoteProcess``（满足 ``StreamingCommandProcess``），
        否则返回一次性收输出的 ``RemoteProcess``。

        调用方取消或等启动响应超时：撤销通知登记，并在后台等启动请求落定后补发一次强杀
        （``_kill_if_started``），不在守护进程里留下没人认领的进程。
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
        # 启动请求单独成任务、经 shield 等待：调用方取消时请求照常收到结果，善后才知道该不该杀
        request = asyncio.ensure_future(
            self._client.request(protocol.METHOD_PROCESS_START, params)
        )
        try:
            result = await asyncio.shield(request)
        except SandboxRemoteError as exc:
            process.abandon()
            _raise_spawn_error(exc)
            raise  # pragma: no cover —— 上一行必然抛出，仅为类型收窄
        except BaseException:
            process.abandon()
            cleanup = asyncio.ensure_future(_kill_if_started(self._client, request, process_id))
            self._cleanups.add(cleanup)
            cleanup.add_done_callback(self._cleanups.discard)
            raise
        pid = result.get("pid")
        process.pid = pid if isinstance(pid, int) else None
        return process


async def _kill_if_started(
    client: DaemonClient, request: asyncio.Future[dict[str, Any]], process_id: str
) -> None:
    """启动请求被放弃（取消、等响应超时）之后的善后：守护进程可能已经起了进程，补发一次强杀。

    不杀的话，没人认领的进程要到连接关闭才被回收；流式进程更会一直挂在等标准输入上。先等启动
    请求落定再发强杀：守护进程为每条请求各起一个任务，紧跟着发的强杀可能赶在进程登记之前、扑空。
    守护进程明确回了错误说明没起来，不用杀；连接已断时守护进程本就会杀掉名下全部进程。强杀失败
    只记 debug——宿主这边已没有人等这个进程。
    """
    try:
        await request
    except SandboxRemoteError:
        return
    except SandboxProtocolError as exc:
        if client.closed:
            logger.debug("放弃的启动请求 %s 未完成，连接已断：%s", process_id, exc)
            return
    try:
        await client.request(protocol.METHOD_PROCESS_KILL, {"processId": process_id})
    except (SandboxProtocolError, SandboxRemoteError) as exc:
        logger.debug("强杀放弃启动的进程 %s 失败：%s", process_id, exc)


__all__ = ["DEFAULT_MAX_BUFFER_BYTES", "DaemonCommandExecutor", "RemoteProcess"]
