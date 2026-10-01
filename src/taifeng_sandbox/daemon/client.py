"""守护进程的宿主侧客户端：请求 / 响应配对，通知分发。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from importlib import resources
from typing import TYPE_CHECKING, Any

from taifeng_sandbox.daemon import protocol
from taifeng_sandbox.errors import SandboxProtocolError, SandboxRemoteError

if TYPE_CHECKING:
    from taifeng_sandbox.daemon.transport import Transport

logger = logging.getLogger(__name__)

NotificationHandler = Callable[[dict[str, Any]], None]

DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0


def daemon_source() -> str:
    """守护进程的源码文本，用于经 ``python3 -c`` 注入执行环境。"""
    return resources.files("taifeng_sandbox.daemon").joinpath("server.py").read_text("utf-8")


class DaemonClient:
    """一条到守护进程的连接。

    并发安全：多个协程可以同时发请求，响应按 ``id`` 配对；通知在读循环里同步分发给按
    ``(方法名, processId)`` 登记的处理函数，处理函数不得阻塞。
    """

    def __init__(
        self,
        transport: Transport,
        *,
        client_name: str = "taifeng-sandbox",
        request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        """
        Args:
            transport: 已建立的传输通道。
            client_name: 握手时上报的客户端名。
            request_timeout_seconds: 单次请求等待响应的上限。
        """
        self._transport = transport
        self._client_name = client_name
        self._timeout = request_timeout_seconds
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._handlers: dict[tuple[str, str], NotificationHandler] = {}
        self._disconnect_listeners: list[Callable[[], None]] = []
        self._send_lock = asyncio.Lock()
        self._reader_task: asyncio.Task[None] | None = None
        self._server_info: dict[str, Any] = {}
        self._closing = False
        self._closed = False
        self._failure: SandboxProtocolError | None = None

    @classmethod
    async def connect(
        cls,
        transport: Transport,
        *,
        client_name: str = "taifeng-sandbox",
        request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    ) -> DaemonClient:
        """建立连接并完成握手；握手失败会关闭传输。"""
        client = cls(
            transport, client_name=client_name, request_timeout_seconds=request_timeout_seconds
        )
        client._reader_task = asyncio.ensure_future(client._read_loop())
        try:
            client._server_info = await client.request(
                protocol.METHOD_INITIALIZE,
                {"protocolVersion": protocol.PROTOCOL_VERSION, "clientName": client_name},
            )
        except BaseException:
            await client.close()
            raise
        return client

    @property
    def server_info(self) -> dict[str, Any]:
        """握手时守护进程上报的信息（副本）。"""
        return dict(self._server_info)

    @property
    def closed(self) -> bool:
        """连接是否已关闭或已断开。"""
        return self._closed or self._failure is not None

    def on_notification(self, method: str, process_id: str, handler: NotificationHandler) -> None:
        """登记某个进程的通知处理函数。"""
        self._handlers[(method, process_id)] = handler

    def remove_handlers(self, process_id: str) -> None:
        """移除某个进程的全部通知处理函数。"""
        for key in [key for key in self._handlers if key[1] == process_id]:
            del self._handlers[key]

    def on_disconnect(self, listener: Callable[[], None]) -> Callable[[], None]:
        """登记连接断开时的回调；返回取消登记的函数。"""
        self._disconnect_listeners.append(listener)

        def _remove() -> None:
            with contextlib.suppress(ValueError):
                self._disconnect_listeners.remove(listener)

        return _remove

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout_seconds: float | None = None,
        no_timeout: bool = False,
    ) -> dict[str, Any]:
        """发请求并等待响应。

        Args:
            method: 方法名。
            params: 参数对象；None 视为空对象。
            timeout_seconds: 这次等待响应的上限；None 用连接的默认值。
            no_timeout: 为真时不限时等待响应。只给以背压为语义、可能合法地长时间不回复的请求用
                （``process/write``：进程不读标准输入时守护进程一直不回，与本机
                ``StreamWriter.drain`` 一样该无限等）。连接断开照样让它以 ``SandboxProtocolError``
                结束；时限由调用方经取消施加。不能与 ``timeout_seconds`` 同时给。

        Raises:
            SandboxRemoteError: 守护进程返回了错误。
            SandboxProtocolError: 连接已断、超时或响应畸形。
            ValueError: 同时给了 ``timeout_seconds`` 与 ``no_timeout``。
        """
        if no_timeout and timeout_seconds is not None:
            raise ValueError("timeout_seconds 与 no_timeout 不能同时给")
        if self._failure is not None:
            raise self._failure
        if self._closed:
            raise SandboxProtocolError("到守护进程的连接已关闭")
        request_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
        line = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        if len(line) > protocol.MAX_MESSAGE_BYTES:
            del self._pending[request_id]
            raise SandboxProtocolError(f"请求 {method} 超过单条消息长度上限")
        try:
            async with self._send_lock:
                await self._transport.send(line)
            if no_timeout:
                return await future
            limit = self._timeout if timeout_seconds is None else timeout_seconds
            return await asyncio.wait_for(future, timeout=limit)
        except TimeoutError as exc:
            raise SandboxProtocolError(f"请求 {method} 等待响应超时") from exc
        finally:
            self._pending.pop(request_id, None)

    async def _read_loop(self) -> None:
        """读循环：直到对端关闭或出错，然后让所有等待者失败。"""
        failure: SandboxProtocolError
        try:
            while True:
                line = await self._transport.receive()
                if line is None:
                    break
                self._dispatch(line)
            detail = self._transport.diagnostics()
            suffix = f"；守护进程输出：{detail}" if detail else ""
            failure = SandboxProtocolError(f"守护进程连接已断开{suffix}")
        except SandboxProtocolError as exc:
            failure = exc
        except asyncio.CancelledError:
            failure = SandboxProtocolError("到守护进程的连接已关闭")
            self._fail_all(failure)
            raise
        self._fail_all(failure)

    def _fail_all(self, failure: SandboxProtocolError) -> None:
        """连接不可用：让在途请求失败，并通知各进程句柄。"""
        self._failure = failure
        for future in self._pending.values():
            if not future.done():
                future.set_exception(failure)
        self._pending.clear()
        for listener in list(self._disconnect_listeners):
            listener()

    def _dispatch(self, line: bytes) -> None:
        """解析一行并分发：带 ``id`` 的是响应，否则是通知。"""
        try:
            message = json.loads(line)
        except ValueError:
            logger.warning("忽略守护进程发来的无法解析的消息")
            return
        if not isinstance(message, dict):
            logger.warning("忽略守护进程发来的非对象消息")
            return
        if "id" in message and "method" not in message:
            self._resolve(message)
            return
        method = message.get("method")
        params = message.get("params")
        if isinstance(method, str) and isinstance(params, dict):
            handler = self._handlers.get((method, str(params.get("processId"))))
            if handler is not None:
                handler(params)

    def _resolve(self, message: dict[str, Any]) -> None:
        """把响应交给对应的等待者。"""
        future = self._pending.get(message.get("id", -1))
        if future is None or future.done():
            return
        error = message.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            future.set_exception(
                SandboxRemoteError(
                    code if isinstance(code, int) else protocol.ERROR_INTERNAL,
                    str(error.get("message", "")),
                )
            )
            return
        result = message.get("result")
        if not isinstance(result, dict):
            future.set_exception(SandboxProtocolError("守护进程的响应缺少 result 对象"))
            return
        future.set_result(result)

    async def close(self) -> None:
        """关闭连接。守护进程在连接关闭时会杀掉它名下所有进程。"""
        if self._closed or self._closing:
            return
        self._closing = True
        if self._failure is None:
            # 先礼貌地请守护进程收尾；失败也无妨，关传输时它会因 EOF 自行退出
            with contextlib.suppress(SandboxProtocolError, SandboxRemoteError):
                await self.request(protocol.METHOD_SHUTDOWN, timeout_seconds=3.0)
        self._closed = True
        await self._transport.close()
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task


__all__ = ["DEFAULT_REQUEST_TIMEOUT_SECONDS", "DaemonClient", "daemon_source"]
