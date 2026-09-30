"""沙盒内守护进程 —— 在执行环境里提供进程执行与文件访问（ADR 0002）。

**本文件必须能单文件运行**：只用标准库、不 import 本包其他模块，兼容 Python 3.9+。宿主把它的
源码经 ``python3 -c`` 注入容器即可启动，容器镜像里不需要安装 taifeng-sandbox。

传输：stdin / stdout 上按行分隔的 JSON（线协议见 ``protocol.py``）。stderr 只写诊断日志。

安全边界：

- 文件方法只能访问 ``--root`` 之内的路径，解析符号链接之后再判断，防止借链接逃逸；
- 进程的环境变量完全由请求给出，不继承守护进程自己的环境；
- 连接断开（stdin EOF）时杀掉所有还在跑的进程再退出，不留孤儿。

守护进程不做审批、黑名单、超时——这些保证在 taifeng 工具层（ADR 0001 决策 2）。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import shutil
import signal
import stat
import sys
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

PROTOCOL_VERSION = 1
SERVER_NAME = "taifeng-sandbox-daemon"

ERROR_PARSE = -32700
ERROR_INVALID_REQUEST = -32600
ERROR_METHOD_NOT_FOUND = -32601
ERROR_INVALID_PARAMS = -32602
ERROR_INTERNAL = -32603
ERROR_NOT_INITIALIZED = -32000
ERROR_UNSUPPORTED_VERSION = -32001
ERROR_SPAWN_NOT_FOUND = -32010
ERROR_SPAWN_FAILED = -32011
ERROR_PROCESS_EXISTS = -32012
ERROR_PROCESS_UNKNOWN = -32013
ERROR_ACCESS_DENIED = -32020
ERROR_NOT_FOUND = -32021
ERROR_IO = -32022
ERROR_TOO_LARGE = -32023

MAX_MESSAGE_BYTES = 32 * 1024 * 1024
MAX_FILE_BYTES = 16 * 1024 * 1024
OUTPUT_CHUNK_BYTES = 32 * 1024

Params = Dict[str, Any]
Handler = Callable[[Params], Awaitable[Dict[str, Any]]]


class RpcError(Exception):
    """带线协议错误码的失败。"""

    def __init__(self, code: int, message: str) -> None:
        """记录错误码与消息。"""
        super().__init__(message)
        self.code = code
        self.message = message


def _require_str(params: Params, key: str) -> str:
    """取必填字符串参数。"""
    value = params.get(key)
    if not isinstance(value, str) or not value:
        raise RpcError(ERROR_INVALID_PARAMS, "参数 %s 必须是非空字符串" % key)
    return value


def _optional_str(params: Params, key: str) -> Optional[str]:
    """取可选字符串参数。"""
    value = params.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise RpcError(ERROR_INVALID_PARAMS, "参数 %s 必须是字符串" % key)
    return value


def _require_str_list(params: Params, key: str) -> List[str]:
    """取必填的非空字符串列表参数。"""
    value = params.get(key)
    if not isinstance(value, list) or not value:
        raise RpcError(ERROR_INVALID_PARAMS, "参数 %s 必须是非空列表" % key)
    if not all(isinstance(item, str) for item in value):
        raise RpcError(ERROR_INVALID_PARAMS, "参数 %s 的元素必须都是字符串" % key)
    return list(value)


def _require_str_map(params: Params, key: str) -> Dict[str, str]:
    """取必填的字符串映射参数（可为空映射）。"""
    value = params.get(key)
    if not isinstance(value, dict):
        raise RpcError(ERROR_INVALID_PARAMS, "参数 %s 必须是对象" % key)
    for name, item in value.items():
        if not isinstance(name, str) or not isinstance(item, str):
            raise RpcError(ERROR_INVALID_PARAMS, "参数 %s 的键和值必须都是字符串" % key)
    return dict(value)


class PathGuard:
    """把请求路径限制在根目录之内。"""

    def __init__(self, root: str) -> None:
        """根目录在此解析为真实路径。"""
        self.root = os.path.realpath(root)

    def resolve(self, requested: str) -> str:
        """解析为真实路径并确认没有逃出根目录。

        相对路径相对根目录解释。目标不存在时 ``realpath`` 仍会解析已存在的那部分父目录，
        所以借符号链接指向根外的写入同样会被拦下。
        """
        candidate = requested if os.path.isabs(requested) else os.path.join(self.root, requested)
        real = os.path.realpath(candidate)
        if real != self.root and not real.startswith(self.root.rstrip(os.sep) + os.sep):
            raise RpcError(ERROR_ACCESS_DENIED, "路径在沙盒根目录之外：%s" % requested)
        return real


def _map_os_error(exc: OSError, path: str) -> RpcError:
    """把文件系统异常归类到线协议错误码。"""
    if isinstance(exc, FileNotFoundError):
        return RpcError(ERROR_NOT_FOUND, "不存在：%s" % path)
    if isinstance(exc, PermissionError):
        return RpcError(ERROR_ACCESS_DENIED, "无权访问：%s" % path)
    return RpcError(ERROR_IO, "%s：%s" % (path, exc.strerror or str(exc)))


class FileService:
    """文件访问方法。磁盘 IO 放到线程池，不阻塞事件循环。"""

    def __init__(self, guard: PathGuard) -> None:
        """绑定路径守卫。"""
        self._guard = guard

    async def read_file(self, params: Params) -> Dict[str, Any]:
        """读文件；支持 ``offset`` / ``length`` 分段读取大文件。"""
        path = self._guard.resolve(_require_str(params, "path"))
        offset = int(params.get("offset") or 0)
        length = int(params.get("length") or MAX_FILE_BYTES)
        if offset < 0 or length < 1 or length > MAX_FILE_BYTES:
            raise RpcError(ERROR_INVALID_PARAMS, "offset / length 超出范围")

        def _read() -> Tuple[bytes, int]:
            with open(path, "rb") as handle:
                size = os.fstat(handle.fileno()).st_size
                handle.seek(offset)
                return handle.read(length), size

        try:
            data, size = await asyncio.get_event_loop().run_in_executor(None, _read)
        except OSError as exc:
            raise _map_os_error(exc, path) from exc
        return {
            "data": base64.b64encode(data).decode("ascii"),
            "size": size,
            "eof": offset + len(data) >= size,
        }

    async def write_file(self, params: Params) -> Dict[str, Any]:
        """写文件；``append`` 追加，``createParents`` 自动建父目录。"""
        path = self._guard.resolve(_require_str(params, "path"))
        try:
            data = base64.b64decode(_require_str_or_empty(params, "data"), validate=True)
        except ValueError as exc:
            raise RpcError(ERROR_INVALID_PARAMS, "data 不是合法的 base64") from exc
        if len(data) > MAX_FILE_BYTES:
            raise RpcError(ERROR_TOO_LARGE, "单次写入超过上限")
        mode = "ab" if params.get("append") else "wb"
        create_parents = bool(params.get("createParents"))

        def _write() -> None:
            if create_parents:
                os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, mode) as handle:
                handle.write(data)

        try:
            await asyncio.get_event_loop().run_in_executor(None, _write)
        except OSError as exc:
            raise _map_os_error(exc, path) from exc
        return {"bytesWritten": len(data)}

    async def read_directory(self, params: Params) -> Dict[str, Any]:
        """列目录（不递归），按名字排序。"""
        path = self._guard.resolve(_require_str(params, "path"))

        def _list() -> List[Dict[str, Any]]:
            entries = []
            with os.scandir(path) as scanner:
                for entry in scanner:
                    entries.append(
                        {
                            "name": entry.name,
                            "isDirectory": entry.is_dir(follow_symlinks=False),
                            "isFile": entry.is_file(follow_symlinks=False),
                            "isSymlink": entry.is_symlink(),
                        }
                    )
            return sorted(entries, key=lambda item: str(item["name"]))

        try:
            entries = await asyncio.get_event_loop().run_in_executor(None, _list)
        except OSError as exc:
            raise _map_os_error(exc, path) from exc
        return {"entries": entries}

    async def get_metadata(self, params: Params) -> Dict[str, Any]:
        """取元数据；不存在时返回 ``exists=False`` 而不是报错。"""
        path = self._guard.resolve(_require_str(params, "path"))

        def _stat() -> Optional[os.stat_result]:
            try:
                return os.stat(path)
            except FileNotFoundError:
                return None

        try:
            info = await asyncio.get_event_loop().run_in_executor(None, _stat)
        except OSError as exc:
            raise _map_os_error(exc, path) from exc
        if info is None:
            return {"exists": False}
        return {
            "exists": True,
            "isDirectory": stat.S_ISDIR(info.st_mode),
            "isFile": stat.S_ISREG(info.st_mode),
            "size": info.st_size,
            "modifiedAt": info.st_mtime,
        }

    async def create_directory(self, params: Params) -> Dict[str, Any]:
        """建目录；``recursive`` 连同父目录一起建，已存在不算错。"""
        path = self._guard.resolve(_require_str(params, "path"))
        recursive = bool(params.get("recursive"))

        def _mkdir() -> None:
            if recursive:
                os.makedirs(path, exist_ok=True)
            else:
                os.mkdir(path)

        try:
            await asyncio.get_event_loop().run_in_executor(None, _mkdir)
        except OSError as exc:
            raise _map_os_error(exc, path) from exc
        return {}

    async def remove(self, params: Params) -> Dict[str, Any]:
        """删除文件或目录；根目录本身不允许删。"""
        path = self._guard.resolve(_require_str(params, "path"))
        if path == self._guard.root:
            raise RpcError(ERROR_ACCESS_DENIED, "不允许删除沙盒根目录")
        recursive = bool(params.get("recursive"))

        def _remove() -> None:
            if os.path.isdir(path) and not os.path.islink(path):
                if recursive:
                    shutil.rmtree(path)
                else:
                    os.rmdir(path)
            else:
                os.remove(path)

        try:
            await asyncio.get_event_loop().run_in_executor(None, _remove)
        except OSError as exc:
            raise _map_os_error(exc, path) from exc
        return {}


def _require_str_or_empty(params: Params, key: str) -> str:
    """取必填字符串参数，允许空串（写空文件）。"""
    value = params.get(key)
    if not isinstance(value, str):
        raise RpcError(ERROR_INVALID_PARAMS, "参数 %s 必须是字符串" % key)
    return value


class ProcessService:
    """进程方法：启动、转发输出、报告退出、强杀。"""

    def __init__(
        self,
        guard: PathGuard,
        notify: Callable[[str, Dict[str, Any]], Awaitable[None]],
    ) -> None:
        """
        Args:
            guard: 路径守卫，提供缺省工作目录（根目录）。
            notify: 向宿主发通知的函数。
        """
        self._guard = guard
        self._notify = notify
        self._processes = {}  # type: Dict[str, asyncio.subprocess.Process]
        self._watchers = set()  # type: set[asyncio.Future[None]]

    async def start(self, params: Params) -> Dict[str, Any]:
        """启动进程。以新会话启动，强杀时连同其子进程一起终止。"""
        process_id = _require_str(params, "processId")
        argv = _require_str_list(params, "argv")
        env = _require_str_map(params, "env")
        cwd = self._working_directory(_optional_str(params, "cwd"))
        if process_id in self._processes:
            raise RpcError(ERROR_PROCESS_EXISTS, "processId 已存在：%s" % process_id)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
                close_fds=True,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise RpcError(ERROR_SPAWN_NOT_FOUND, str(exc)) from exc
        except OSError as exc:
            raise RpcError(ERROR_SPAWN_FAILED, str(exc)) from exc
        self._processes[process_id] = proc
        watcher = asyncio.ensure_future(self._watch(process_id, proc))
        self._watchers.add(watcher)
        watcher.add_done_callback(self._watchers.discard)
        return {"pid": proc.pid}

    def _working_directory(self, requested: Optional[str]) -> str:
        """进程的工作目录：缺省为根目录，相对路径相对根目录。

        这里不做根目录约束。根目录约束保护的是宿主经文件方法发起的访问；进程一旦启动，
        能碰什么由执行环境本身（容器、虚拟机）决定，约束它的起始目录并不增加隔离。
        """
        if requested is None:
            return self._guard.root
        if os.path.isabs(requested):
            return requested
        return os.path.join(self._guard.root, requested)

    async def _forward(
        self, process_id: str, stream_name: str, stream: Optional[asyncio.StreamReader]
    ) -> None:
        """把一路输出分块转发给宿主，直到 EOF。"""
        if stream is None:
            return
        while True:
            chunk = await stream.read(OUTPUT_CHUNK_BYTES)
            if not chunk:
                return
            await self._notify(
                "process/output",
                {
                    "processId": process_id,
                    "stream": stream_name,
                    "data": base64.b64encode(chunk).decode("ascii"),
                },
            )

    async def _watch(self, process_id: str, proc: asyncio.subprocess.Process) -> None:
        """两路输出都读完、进程也退出之后，才发退出通知（保证输出不丢在退出之后）。"""
        try:
            await asyncio.gather(
                self._forward(process_id, "stdout", proc.stdout),
                self._forward(process_id, "stderr", proc.stderr),
            )
            exit_code = await proc.wait()
            await self._notify(
                "process/exited", {"processId": process_id, "exitCode": exit_code}
            )
        finally:
            self._processes.pop(process_id, None)

    async def kill(self, params: Params) -> Dict[str, Any]:
        """强杀进程组。进程已结束不算错（宿主的强杀与自然退出可能竞争）。"""
        process_id = _require_str(params, "processId")
        proc = self._processes.get(process_id)
        if proc is None:
            return {"killed": False}
        _kill_group(proc)
        return {"killed": True}

    async def kill_all(self) -> None:
        """杀掉所有还在跑的进程并等监视任务收尾。"""
        for proc in list(self._processes.values()):
            _kill_group(proc)
        if self._watchers:
            await asyncio.wait(list(self._watchers), timeout=5)


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    """对进程组发 SIGKILL；进程组已不存在时无事可做。"""
    if proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except PermissionError:
        try:
            proc.kill()
        except ProcessLookupError:
            return


class Daemon:
    """消息循环：读请求、派发、写响应。"""

    def __init__(self, root: str, writer: asyncio.StreamWriter) -> None:
        """组装各项服务与方法表。"""
        self._guard = PathGuard(root)
        self._writer = writer
        self._write_lock = asyncio.Lock()
        self._initialized = False
        self._stopping = False
        self._tasks = set()  # type: set[asyncio.Future[None]]
        self._processes = ProcessService(self._guard, self._send_notification)
        files = FileService(self._guard)
        self._handlers = {
            "process/start": self._processes.start,
            "process/kill": self._processes.kill,
            "fs/readFile": files.read_file,
            "fs/writeFile": files.write_file,
            "fs/readDirectory": files.read_directory,
            "fs/getMetadata": files.get_metadata,
            "fs/createDirectory": files.create_directory,
            "fs/remove": files.remove,
        }  # type: Dict[str, Handler]

    async def _send(self, payload: Dict[str, Any]) -> None:
        """写出一条消息。加锁保证多条消息不会交错在同一行。"""
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        async with self._write_lock:
            self._writer.write(line.encode("utf-8"))
            await self._writer.drain()

    async def _send_notification(self, method: str, params: Dict[str, Any]) -> None:
        """发通知；连接已断时放弃（进程会在收尾阶段被杀）。"""
        try:
            await self._send({"jsonrpc": "2.0", "method": method, "params": params})
        except (ConnectionError, OSError):
            return

    async def _initialize(self, params: Params) -> Dict[str, Any]:
        """握手：确认协议版本。"""
        version = params.get("protocolVersion")
        if version != PROTOCOL_VERSION:
            raise RpcError(
                ERROR_UNSUPPORTED_VERSION,
                "守护进程只支持协议版本 %d，收到 %r" % (PROTOCOL_VERSION, version),
            )
        self._initialized = True
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "serverName": SERVER_NAME,
            "platform": sys.platform,
            "pythonVersion": "%d.%d.%d" % sys.version_info[:3],
            "root": self._guard.root,
            "pid": os.getpid(),
        }

    async def _call(self, method: str, params: Params) -> Dict[str, Any]:
        """按方法名派发。握手之前只接受 ``initialize``。"""
        if method == "initialize":
            return await self._initialize(params)
        if not self._initialized:
            raise RpcError(ERROR_NOT_INITIALIZED, "尚未完成 initialize 握手")
        if method == "shutdown":
            self._stopping = True
            return {}
        handler = self._handlers.get(method)
        if handler is None:
            raise RpcError(ERROR_METHOD_NOT_FOUND, "未知方法：%s" % method)
        return await handler(params)

    async def _handle(self, message: Dict[str, Any]) -> None:
        """处理一条请求并写回响应。通知（无 id）不回响应。"""
        request_id = message.get("id")
        method = message.get("method")
        params = message.get("params") or {}
        if not isinstance(method, str) or not isinstance(params, dict):
            await self._reply_error(
                request_id, ERROR_INVALID_REQUEST, "请求缺少 method 或 params 非对象"
            )
            return
        try:
            result = await self._call(method, params)
        except RpcError as exc:
            await self._reply_error(request_id, exc.code, exc.message)
            return
        except Exception as exc:  # noqa: BLE001 —— 守护进程不能因单个请求崩溃
            detail = "%s: %s" % (type(exc).__name__, exc)
            await self._reply_error(request_id, ERROR_INTERNAL, detail)
            return
        if request_id is not None:
            await self._send({"jsonrpc": "2.0", "id": request_id, "result": result})

    async def _reply_error(self, request_id: Any, code: int, message: str) -> None:
        """写回错误响应。"""
        if request_id is None:
            sys.stderr.write("daemon: 通知处理失败 %d %s\n" % (code, message))
            return
        await self._send(
            {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
        )

    async def serve(self, reader: asyncio.StreamReader) -> None:
        """主循环：每条请求各起一个任务，互不阻塞；EOF 或 shutdown 后收尾。"""
        try:
            while not self._stopping:
                try:
                    line = await reader.readline()
                except ValueError:
                    # 单行超过上限：无法恢复对齐，只能断开
                    sys.stderr.write("daemon: 消息超过长度上限，断开\n")
                    break
                if not line:
                    break
                message = self._parse(line)
                if message is None:
                    continue
                if message.get("method") == "shutdown":
                    # shutdown 同步处理，保证响应写出后才停止读取
                    await self._handle(message)
                    continue
                task = asyncio.ensure_future(self._handle(message))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
        finally:
            await self._processes.kill_all()
            if self._tasks:
                await asyncio.wait(list(self._tasks), timeout=5)

    def _parse(self, line: bytes) -> Optional[Dict[str, Any]]:
        """解析一行；畸形消息写诊断日志后跳过。"""
        try:
            message = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            sys.stderr.write("daemon: 跳过无法解析的消息\n")
            return None
        if not isinstance(message, dict):
            sys.stderr.write("daemon: 跳过非对象消息\n")
            return None
        return message


async def _open_stdio() -> Tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """把 stdin / stdout 接成异步流。"""
    loop = asyncio.get_event_loop()
    reader = asyncio.StreamReader(limit=MAX_MESSAGE_BYTES)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)
    transport, protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, sys.stdout.buffer
    )
    writer = asyncio.StreamWriter(transport, protocol, None, loop)
    return reader, writer


async def _run(root: str) -> None:
    """启动守护进程并服务到连接结束。"""
    reader, writer = await _open_stdio()
    await Daemon(root, writer).serve(reader)


def main(argv: Optional[List[str]] = None) -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="taifeng-sandbox 沙盒内守护进程")
    parser.add_argument("--root", required=True, help="文件访问与工作目录的根目录")
    args = parser.parse_args(argv)
    if not os.path.isdir(args.root):
        sys.stderr.write("daemon: 根目录不存在：%s\n" % args.root)
        return 2
    asyncio.run(_run(args.root))
    return 0


if __name__ == "__main__":
    sys.exit(main())
