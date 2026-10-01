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
import contextlib
import json
import os
import shutil
import signal
import stat
import sys
import tempfile
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

PROTOCOL_VERSION = 2
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
ERROR_OUTSIDE_ROOT = -32024

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


def _optional_bool(params: Params, key: str) -> bool:
    """取可选布尔参数，未给（或为 null）时为假；给了却不是布尔值是参数错误，不按真假值宽松解释。"""
    value = params.get(key)
    if value is None:
        return False
    if not isinstance(value, bool):
        raise RpcError(ERROR_INVALID_PARAMS, "参数 %s 必须是布尔值" % key)
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
            raise RpcError(ERROR_OUTSIDE_ROOT, "路径在沙盒根目录之外：%s" % requested)
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
        """绑定路径守卫，并记下 umask（只能「设置并取回旧值」；此时还没有工作线程在建文件）。"""
        self._guard = guard
        self._umask = os.umask(0o077)
        os.umask(self._umask)

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
        """写文件；``append`` 追加，``createParents`` 自动建父目录。

        非追加写入是原子的：同目录写临时文件，再 ``os.replace`` 到目标，读者只会看到旧内容或新内容；
        失败时删掉临时文件。``mkstemp`` 建出的 0600 不带到目标上：覆盖保持原权限（可执行位不丢），
        新建按 umask，与直接 ``open`` 写入的结果一致。
        """
        path = self._guard.resolve(_require_str(params, "path"))
        if path == self._guard.root:
            # 否则临时文件会建到根目录的父目录里，即根目录之外
            raise RpcError(ERROR_IO, "根目录是目录，不能当文件写：%s" % path)
        try:
            data = base64.b64decode(_require_str_or_empty(params, "data"), validate=True)
        except ValueError as exc:
            raise RpcError(ERROR_INVALID_PARAMS, "data 不是合法的 base64") from exc
        if len(data) > MAX_FILE_BYTES:
            raise RpcError(ERROR_TOO_LARGE, "单次写入超过上限")
        append = bool(params.get("append"))
        create_parents = bool(params.get("createParents"))

        def _write() -> None:
            if create_parents:
                os.makedirs(os.path.dirname(path), exist_ok=True)
            if append:
                with open(path, "ab") as handle:
                    handle.write(data)
                return
            fd, temp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                os.chmod(temp, self._replaced_mode(path))
                os.replace(temp, path)
            except BaseException:
                with contextlib.suppress(OSError):  # 清理失败不掩盖原始错误
                    os.unlink(temp)
                raise

        try:
            await asyncio.get_event_loop().run_in_executor(None, _write)
        except OSError as exc:
            raise _map_os_error(exc, path) from exc
        return {"bytesWritten": len(data)}

    def _replaced_mode(self, path: str) -> int:
        """原子替换后目标的权限：已存在保持原权限，新建按 umask。"""
        try:
            return stat.S_IMODE(os.stat(path).st_mode)
        except FileNotFoundError:
            return 0o666 & ~self._umask

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
        """取元数据；不存在（含路径中间某段是文件）时返回 ``exists=False`` 而不是报错。"""
        path = self._guard.resolve(_require_str(params, "path"))

        def _stat() -> Optional[os.stat_result]:
            try:
                return os.stat(path)
            except (FileNotFoundError, NotADirectoryError):
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
    """进程方法：启动、写标准输入、转发输出、报告退出、强杀。"""

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
        # 要了标准输入的进程各有一把写锁，与进程同进同出进程表
        self._stdin_locks = {}  # type: Dict[str, asyncio.Lock]
        self._watchers = set()  # type: set[asyncio.Future[None]]
        self._stdin_reapers = set()  # type: set[asyncio.Future[None]]

    async def start(self, params: Params) -> Dict[str, Any]:
        """启动进程。以新会话启动，强杀时连同其子进程一起终止。

        ``stdin`` 为真时标准输入接管道，之后经 ``process/write`` 写入、``process/closeStdin``
        关闭；否则接 ``/dev/null``，进程一读就是 EOF。
        """
        process_id = _require_str(params, "processId")
        argv = _require_str_list(params, "argv")
        env = _require_str_map(params, "env")
        cwd = self._working_directory(_optional_str(params, "cwd"))
        wants_stdin = _optional_bool(params, "stdin")
        if process_id in self._processes:
            raise RpcError(ERROR_PROCESS_EXISTS, "processId 已存在：%s" % process_id)
        try:
            # Python 3.9 的 asyncio 给子进程 stdin 用的是 Unix socketpair 而不是管道（3.12 起
            # 只在 AIX 上这样）：写、关、对端断开的行为一致，只是缓冲大小不同（macOS 上
            # socketpair 8 KiB、管道 64 KiB），背压来得更早
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE if wants_stdin else asyncio.subprocess.DEVNULL,
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
        if wants_stdin:
            self._stdin_locks[process_id] = asyncio.Lock()
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
        """两路输出都读完、进程也退出之后，才发退出通知（保证输出不丢在退出之后）。

        进程在发退出通知之前移出进程表：宿主收到退出通知时这个 ``processId`` 已经不在表里，
        之后的写入、强杀一律按「没有这个进程」处理，不会再对已回收的进程组发信号。
        """
        try:
            await asyncio.gather(
                self._forward(process_id, "stdout", proc.stdout),
                self._forward(process_id, "stderr", proc.stderr),
            )
            exit_code = await proc.wait()
        finally:
            if self._processes.get(process_id) is proc:
                del self._processes[process_id]
                self._stdin_locks.pop(process_id, None)
            if proc.stdin is not None:
                self._release_stdin(process_id, proc.stdin)
        await self._notify("process/exited", {"processId": process_id, "exitCode": exit_code})

    def _release_stdin(self, process_id: str, stdin: asyncio.StreamWriter) -> None:
        """进程收尾：关掉标准输入（已关则无事），在后台取走关闭结果。

        关闭结果统一在这里取，不管宿主有没有发过 ``process/closeStdin``：对端断开时关闭结果
        带着异常，没人取的话 Python 3.9 会在回收时打出「Future exception was never retrieved」。
        后台取而不是就地等：进程派生的子进程可能还占着标准输入却不读，等关闭会挂住退出通知。
        """
        stdin.close()
        reaper = asyncio.ensure_future(_await_stdin_closed(process_id, stdin))
        self._stdin_reapers.add(reaper)
        reaper.add_done_callback(self._stdin_reapers.discard)

    def _known(self, process_id: str) -> asyncio.subprocess.Process:
        """取进程表里的进程；不在表里是 ``ERROR_PROCESS_UNKNOWN``。"""
        proc = self._processes.get(process_id)
        if proc is None:
            raise RpcError(ERROR_PROCESS_UNKNOWN, "没有这个进程：%s" % process_id)
        return proc

    async def write(self, params: Params) -> Dict[str, Any]:
        """向进程的标准输入写一段数据，等写缓冲回落到水位线以下（``drain``）才回复。

        回复是背压信号，不代表进程已经读到。启动时没要标准输入的进程不能写
        （``ERROR_INVALID_PARAMS``），不静默丢弃；标准输入已关闭或对端已断开时是 ``ERROR_IO``。

        写入顺序由宿主保证：每次写都等到响应才发下一次。守护进程为每条请求各起一个任务；
        每个进程一把写锁，保证并发到达的写按到达顺序整块落入管道，也避免 Python 3.9 上并发
        ``drain`` 的 AssertionError 掩盖已经写进缓冲的数据。宿主仍应串行写。
        """
        process_id = _require_str(params, "processId")
        try:
            data = base64.b64decode(_require_str_or_empty(params, "data"), validate=True)
        except ValueError as exc:
            raise RpcError(ERROR_INVALID_PARAMS, "data 不是合法的 base64") from exc
        if len(data) > MAX_FILE_BYTES:
            raise RpcError(ERROR_TOO_LARGE, "单次写入超过上限")
        stdin = self._known(process_id).stdin
        lock = self._stdin_locks.get(process_id)
        if stdin is None or lock is None:
            raise RpcError(ERROR_INVALID_PARAMS, "进程启动时没有要标准输入：%s" % process_id)
        async with lock:
            # 关闭中的管道会把写入静默丢掉，必须先拦下
            if stdin.is_closing():
                raise RpcError(ERROR_IO, "进程的标准输入已关闭或对端已断开：%s" % process_id)
            try:
                stdin.write(data)
                await stdin.drain()
            except OSError as exc:
                raise RpcError(
                    ERROR_IO, "写进程的标准输入失败：%s：%s" % (process_id, exc)
                ) from exc
        return {"bytesWritten": len(data)}

    async def close_stdin(self, params: Params) -> Dict[str, Any]:
        """发起关闭进程的标准输入，立即回复 ``{}``；进程读完已送达的数据后读到 EOF。

        关闭是发起式的，与本机管道一致：写缓冲里尚未进管道的数据在后台继续写，进程不读或提前
        退出时丢弃。不等关闭完成——进程不读时那会一直挂到进程被杀。关闭结果在进程收尾时取走
        （见 ``_release_stdin``）。没要标准输入的进程本来就读得到 EOF，重复关闭也一样，都不算错。
        不拿写锁：停在背压上的写持有写锁，拿锁会让关闭排在它后面。
        """
        stdin = self._known(_require_str(params, "processId")).stdin
        if stdin is not None:
            stdin.close()
        return {}

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


async def _await_stdin_closed(process_id: str, stdin: asyncio.StreamWriter) -> None:
    """等标准输入真正关闭并取走结果；出错只往 stderr 写一行诊断——数据已没人可收，宿主也已收尾。"""
    try:
        await stdin.wait_closed()
    except OSError as exc:
        sys.stderr.write("daemon: 进程 %s 的标准输入关闭时出错：%r\n" % (process_id, exc))


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    """对整个进程组发 SIGKILL。

    不看 ``returncode``：shell 先退出、它派生的子进程仍占着管道时，主进程已经有退出码，但组里还有
    成员要杀（与内核 ADR 0108 一致）。调用方只对还在进程表里的进程调用——监视任务收完两路输出、
    进程也退出之后才把它移出进程表，表里还在就说明组里可能还有成员，照样按组杀。

    进程组已不存在（``ProcessLookupError``）说明已经干净；无权按组杀（``PermissionError``）时
    退回只杀主进程，主进程已退出就无事可做。
    """
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except PermissionError:
        if proc.returncode is not None:
            return
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
            "process/write": self._processes.write,
            "process/closeStdin": self._processes.close_stdin,
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
