"""宿主侧的流式进程：经守护进程持续写标准输入、边到边读输出。

内核 ``McpStdioClient`` 经执行器起 MCP server 时要 ``CommandSpec.stdin=True``，拿到的进程须满足
taifeng ``StreamingCommandProcess``：提供 ``stdin`` / ``stdout`` / ``stderr`` 三个流。本模块把线协议
v2 的 ``process/write``、``process/closeStdin`` 与 ``process/output`` 通知包成这三个流：

- 写入端 ``_StreamInput``：``write`` 只进待发缓冲；``drain`` 把缓冲经 ``process/write`` 发出、等守护
  进程回复（守护进程写缓冲回落到水位线以下才回复，这就是背压）；``close`` 是发起式的，在后台
  先发完缓冲再发 ``process/closeStdin``。
- 读取端 ``_StreamOutput``：输出通知喂进来，``readline`` / ``read`` 取走，进程结束时喂 EOF。未读
  字节超过上限时杀掉进程、让读取方拿到错误——不交出被截断的数据。

接口与惯例参照 ``asyncio.StreamWriter`` / ``StreamReader``。差异：写入要经请求 / 响应往返；进程已
结束或连接已断开时 ``drain`` 抛 ``BrokenPipeError``；已 ``close`` 后再 ``write`` 直接抛
``BrokenPipeError``，不像 asyncio 那样静默丢弃。
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import TYPE_CHECKING

from taifeng_sandbox.daemon import protocol
from taifeng_sandbox.daemon.executor import DEFAULT_MAX_BUFFER_BYTES, RemoteProcess
from taifeng_sandbox.errors import SandboxError, SandboxProtocolError, SandboxRemoteError

if TYPE_CHECKING:
    import taifeng

    from taifeng_sandbox.daemon.client import DaemonClient

logger = logging.getLogger(__name__)

# 单次 process/write 的内容上限；更大的缓冲分块发出，逐块等回复
_WRITE_CHUNK_BYTES = protocol.MAX_FILE_BYTES

# 守护进程回这两种错误，说明写入端已经不通：
# 进程已结束（已移出进程表），或标准输入已关闭 / 对端已断开
_BROKEN_PIPE_CODES = frozenset({protocol.ERROR_IO, protocol.ERROR_PROCESS_UNKNOWN})


class _StreamOutput:
    """一路输出的读取端，满足 taifeng ``CommandOutput``。

    输出经 ``feed`` 喂进来，进程结束经 ``feed_eof`` 收尾。``fail`` 之后读取一律抛那个异常，已缓冲
    的数据一并丢弃：调用方不会把截断的数据当成完整的。
    """

    def __init__(self) -> None:
        """空缓冲，未结束。"""
        self._buffer = bytearray()
        self._changed = asyncio.Event()
        self._eof = False
        self._error: Exception | None = None

    @property
    def unread(self) -> int:
        """已到达、尚未被读走的字节数。"""
        return len(self._buffer)

    def feed(self, chunk: bytes) -> None:
        """追加一块输出。已失败的流不再收：读取方只会拿到错误。"""
        if self._error is not None:
            return
        self._buffer.extend(chunk)
        self._changed.set()

    def feed_eof(self) -> None:
        """输出结束：缓冲读完后读取返回空字节串。"""
        self._eof = True
        self._changed.set()

    def fail(self, exc: Exception) -> None:
        """让这一路失败：丢弃已缓冲的数据，此后读取抛 ``exc``。"""
        self._error = exc
        self._buffer.clear()
        self._changed.set()

    async def readline(self) -> bytes:
        """读一行（含换行符）；流结束时返回剩下的数据，读完返回空字节串。"""
        while True:
            if self._error is not None:
                raise self._error
            end = self._buffer.find(b"\n")
            if end >= 0:
                return self._take(end + 1)
            if self._eof:
                return self._take(len(self._buffer))
            await self._wait()

    async def read(self, n: int = -1) -> bytes:
        """``n < 0`` 读到流结束；否则有数据就返回，至多 ``n`` 字节。流结束返回空字节串。"""
        while True:
            if self._error is not None:
                raise self._error
            if n == 0:
                return b""
            if n > 0 and self._buffer:
                return self._take(n)
            if self._eof:
                return self._take(len(self._buffer))
            await self._wait()

    def _take(self, size: int) -> bytes:
        """从缓冲头部取走至多 ``size`` 字节。"""
        data = bytes(self._buffer[:size])
        del self._buffer[:size]
        return data

    async def _wait(self) -> None:
        """等下一次变化：新数据、EOF 或失败。

        先清再等：调用方刚检查过状态，中间没有让出，不会漏掉唤醒；多个读取方各自醒来后重新检查。
        """
        self._changed.clear()
        await self._changed.wait()


class _StreamInput:
    """标准输入的写入端，满足 taifeng ``CommandInput``。

    写入顺序由宿主保证（线协议约定）：``drain`` 持锁发送，每块都等到回复再发下一块。每块先从
    缓冲摘下再发，所以 ``drain`` 被取消时在途的那一块至多送达一次，不会重发。
    """

    def __init__(self, client: DaemonClient, process_id: str) -> None:
        """
        Args:
            client: 守护进程连接。
            process_id: 线协议里的进程标识。
        """
        self._client = client
        self._process_id = process_id
        self._pending = bytearray()
        self._lock = asyncio.Lock()
        self._closed = False
        self._broken = False
        self._close_task: asyncio.Task[None] | None = None

    def write(self, data: bytes) -> None:
        """追加到待发缓冲，``drain`` 时发出。

        Raises:
            BrokenPipeError: 已经 ``close``。
        """
        if self._closed:
            raise BrokenPipeError(f"进程 {self._process_id} 的标准输入已关闭，不能再写")
        self._pending.extend(data)

    async def drain(self) -> None:
        """把待发缓冲发给守护进程，等到它回复（背压）。缓冲为空时只检查写入端是否还通。

        Raises:
            BrokenPipeError: 进程已结束、标准输入已被关闭，或到守护进程的连接已断开；
                没发出的数据丢弃。
            SandboxError: 其他失败（如连接还在但等回复超时）：在途那一块是否送达不确定。
        """
        async with self._lock:
            await self._flush()

    def close(self) -> None:
        """关闭写入端：立即返回，后台先发完缓冲再发 ``process/closeStdin``。

        写入端已不通（进程已结束或连接已断开）时没有可关的，不再发请求。
        """
        if self._closed:
            return
        self._closed = True
        if self._broken:
            self._pending.clear()
            return
        self._close_task = asyncio.ensure_future(self._close_remote())

    def is_closing(self) -> bool:
        """已经 ``close``，或写入端已不通（与 asyncio 管道断开后的表现一致）。"""
        return self._closed or self._broken

    async def wait_closed(self) -> None:
        """等后台关闭结束；没调过 ``close`` 时立即返回。关闭的失败只进日志，这里不抛。"""
        if self._close_task is not None:
            # 等的一方被取消不该连带取消关闭本身
            await asyncio.shield(self._close_task)

    def mark_broken(self) -> None:
        """进程已结束或连接已断开：此后 ``drain`` 抛 ``BrokenPipeError``。"""
        self._broken = True

    async def _flush(self) -> None:
        """（持锁调用）把待发缓冲分块发出，逐块等回复。"""
        while self._pending:
            self._raise_if_broken()
            chunk = bytes(self._pending[:_WRITE_CHUNK_BYTES])
            del self._pending[: len(chunk)]
            await self._send(chunk)
        self._raise_if_broken()

    def _raise_if_broken(self) -> None:
        """写入端已不通时丢弃待发数据并抛 ``BrokenPipeError``。"""
        if self._broken:
            self._pending.clear()
            raise BrokenPipeError(
                f"进程 {self._process_id} 已结束或到守护进程的连接已断开，标准输入不再可写"
            )

    async def _send(self, chunk: bytes) -> None:
        """经 ``process/write`` 发一块并等回复；写入端不通的失败统一成 ``BrokenPipeError``。"""
        params = {"processId": self._process_id, "data": base64.b64encode(chunk).decode("ascii")}
        try:
            await self._client.request(protocol.METHOD_PROCESS_WRITE, params)
        except SandboxRemoteError as exc:
            if exc.code not in _BROKEN_PIPE_CODES:
                raise
            raise self._break(exc) from exc
        except SandboxProtocolError as exc:
            if not self._client.closed:
                # 连接还在（如等回复超时）：这一块是否送达不确定，如实上抛，写入端仍可用
                raise
            raise self._break(exc) from exc

    def _break(self, cause: SandboxError) -> BrokenPipeError:
        """标记写入端不通、丢弃待发数据，返回要抛出的 ``BrokenPipeError``。"""
        self.mark_broken()
        self._pending.clear()
        return BrokenPipeError(f"写进程 {self._process_id} 的标准输入失败：{cause}")

    async def _close_remote(self) -> None:
        """后台关闭：等在途的 ``drain``、发完缓冲，再发 ``process/closeStdin``。

        调用方早已返回，失败只能记日志；必须在这里取走，不能变成没人取的任务异常。
        """
        async with self._lock:
            if await self._flush_before_close():
                await self._send_close()

    async def _flush_before_close(self) -> bool:
        """关闭前发完缓冲；返回写入端是否还通（不通就不必再发关闭）。"""
        try:
            await self._flush()
        except BrokenPipeError as exc:
            logger.debug("进程 %s 的标准输入已不通，关闭前缓冲未发完：%s", self._process_id, exc)
            return False
        except SandboxError as exc:
            logger.warning("进程 %s 的标准输入关闭前发送缓冲失败：%s", self._process_id, exc)
        return not self._broken

    async def _send_close(self) -> None:
        """发 ``process/closeStdin``。守护进程总是立即回复。"""
        try:
            await self._client.request(
                protocol.METHOD_PROCESS_CLOSE_STDIN, {"processId": self._process_id}
            )
        except SandboxProtocolError as exc:
            if not self._client.closed:
                # 连接还在却失败（超时、响应畸形）：不是预期情形，如实告警
                logger.warning("关闭进程 %s 的标准输入失败：%s", self._process_id, exc)
                return
            # 连接已断：守护进程会杀掉名下进程，关不关都一样
            logger.debug("进程 %s 的标准输入关闭请求未送达：%s", self._process_id, exc)
        except SandboxRemoteError as exc:
            if exc.code != protocol.ERROR_PROCESS_UNKNOWN:
                logger.warning("关闭进程 %s 的标准输入失败：%s", self._process_id, exc)
                return
            # 进程已结束：没有可关的
            logger.debug("关闭标准输入时进程 %s 已结束", self._process_id)


class StreamingRemoteProcess(RemoteProcess):
    """守护进程里能持续对话的进程，满足 taifeng ``StreamingCommandProcess``。

    ``DaemonCommandExecutor.start`` 在 ``CommandSpec.stdin=True`` 时返回它。登记、强杀、退出码沿用
    ``RemoteProcess``；输出不再一次性收齐，而是喂进 ``stdout`` / ``stderr`` 两个读取端。

    ``max_buffer_bytes`` 是每路**未读**字节的上限：读取方跟不上、未读字节超过它时杀掉进程，这一路
    此后读取抛 ``SandboxError``。进程结束或连接断开时两路读到 EOF，写入端的 ``drain`` 抛
    ``BrokenPipeError``。
    """

    def __init__(
        self,
        client: DaemonClient,
        process_id: str,
        *,
        max_buffer_bytes: int = DEFAULT_MAX_BUFFER_BYTES,
    ) -> None:
        """先建好三个流，再由父类登记通知处理函数——最早到的输出也有地方放。"""
        self._stdin_stream = _StreamInput(client, process_id)
        self._stdout_stream = _StreamOutput()
        self._stderr_stream = _StreamOutput()
        super().__init__(client, process_id, max_buffer_bytes=max_buffer_bytes)

    @property
    def stdin(self) -> taifeng.CommandInput | None:
        """标准输入的写入端。"""
        return self._stdin_stream

    @property
    def stdout(self) -> taifeng.CommandOutput | None:
        """标准输出的读取端。"""
        return self._stdout_stream

    @property
    def stderr(self) -> taifeng.CommandOutput | None:
        """标准错误的读取端。"""
        return self._stderr_stream

    def _deliver(self, stream: str, chunk: bytes) -> None:
        """把一块输出喂给对应的读取端；未读字节超过上限就让这一路失败并杀掉进程。"""
        target = self._stderr_stream if stream == "stderr" else self._stdout_stream
        target.feed(chunk)
        if target.unread > self._max_buffer_bytes:
            target.fail(
                SandboxError(
                    f"进程 {self._process_id} 的 {stream} 未读输出超过宿主缓冲上限"
                    f"（{self._max_buffer_bytes} 字节），已强杀进程"
                )
            )
            self.kill()

    def _on_finished(self) -> None:
        """进程结束或连接断开：写入端不再通，两路读到 EOF。"""
        self._stdin_stream.mark_broken()
        self._stdout_stream.feed_eof()
        self._stderr_stream.feed_eof()

    async def communicate(self) -> tuple[bytes, bytes]:
        """先关标准输入（与 asyncio ``Process.communicate`` 一致），再读完两路剩余输出、等待退出。

        Raises:
            SandboxError: 某一路未读字节超过上限、进程已被强杀。
        """
        if not self._stdin_stream.is_closing():
            self._stdin_stream.close()
        stdout, stderr = await asyncio.gather(
            self._stdout_stream.read(), self._stderr_stream.read()
        )
        await self.wait()
        return stdout, stderr


__all__ = ["StreamingRemoteProcess"]
