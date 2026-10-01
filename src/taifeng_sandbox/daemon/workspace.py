"""经守护进程访问执行环境里的文件：实现 taifeng 的 ``WorkspaceFS``（内核 ADR 0113）。

内核的文件类工具（``make_file_read_tool`` 等）接受 ``workspace=``，把本类交给它们，模型读写的就是
沙盒里的文件，与 ``DaemonCommandExecutor`` 里跑的命令看到的是同一个世界。宿主往沙盒里放输入、取产物
也用它。

边界在两处校验：``resolve`` 按字面做纯路径计算（不跟随符号链接），每个方法发请求前都先调它；
守护进程收到请求后解析符号链接、按真实路径再校验一次（``PathGuard``）。
"""

from __future__ import annotations

import base64
import binascii
import posixpath
from typing import TYPE_CHECKING, Any

import taifeng

from taifeng_sandbox.daemon import protocol
from taifeng_sandbox.errors import SandboxProtocolError, SandboxRemoteError

if TYPE_CHECKING:
    from taifeng_sandbox.daemon.client import DaemonClient


def _translate(exc: SandboxRemoteError, path: str) -> OSError:
    """把线协议错误码还原成标准的文件系统异常。

    其余错误原样返回：``SandboxRemoteError`` 本身就是 ``OSError``。
    """
    if exc.code == protocol.ERROR_NOT_FOUND:
        return FileNotFoundError(f"{path}: {exc}")
    if exc.code == protocol.ERROR_OUTSIDE_ROOT:
        return taifeng.WorkspacePathError(f"{path}: {exc}")
    if exc.code == protocol.ERROR_ACCESS_DENIED:
        return PermissionError(f"{path}: {exc}")
    return exc


def _field(result: dict[str, Any], key: str, kinds: type | tuple[type, ...]) -> Any:
    """取响应里的必填字段；缺失或类型不对是协议错误，不拿默认值顶上。"""
    value = result.get(key)
    if not isinstance(value, kinds):
        raise SandboxProtocolError(f"守护进程的响应缺少字段 {key} 或类型不对")
    return value


def _entry(raw: object) -> taifeng.WorkspaceEntry:
    """把 ``fs/readDirectory`` 的一项转成内核的目录项。"""
    if not isinstance(raw, dict):
        raise SandboxProtocolError("守护进程返回的目录项不是对象")
    return taifeng.WorkspaceEntry(
        name=_field(raw, "name", str),
        is_directory=_field(raw, "isDirectory", bool),
        is_file=_field(raw, "isFile", bool),
        is_symlink=_field(raw, "isSymlink", bool),
    )


class DaemonWorkspace:
    """执行环境里的文件视图，满足 ``taifeng.WorkspaceFS``。

    失败用标准 ``OSError`` 子类表达：不存在 ``FileNotFoundError``；越界
    ``taifeng.WorkspacePathError``；操作系统拒绝访问 ``PermissionError``；其余是
    ``SandboxRemoteError``（``OSError`` 子类，带线协议错误码）。连接断开、响应畸形是
    ``SandboxProtocolError``。
    """

    def __init__(self, client: DaemonClient) -> None:
        """绑定一条已握手的守护进程连接。"""
        self._client = client

    @property
    def root(self) -> str:
        """守护进程根目录的真实路径（握手时上报）；``resolve`` 返回的规范路径都以它为前缀。

        Raises:
            SandboxProtocolError: 握手信息里没有根目录，或它不是规范的绝对路径。
        """
        root = self._client.server_info.get("root")
        if not isinstance(root, str):
            raise SandboxProtocolError("守护进程的握手信息里没有根目录（root）")
        if not root.startswith("/") or root.startswith("//") or posixpath.normpath(root) != root:
            raise SandboxProtocolError(f"守护进程上报的根目录不是规范的绝对路径：{root!r}")
        return root

    def resolve(self, path: str) -> str:
        """把路径规范成沙盒里的绝对路径（纯路径计算，不发请求）。

        相对路径相对根目录；``..`` 按字面折叠（``posixpath.normpath``），开头的多个 ``/`` 规整成
        一个；**不跟随符号链接**——根内指向根外的链接在这里放行，由守护进程按真实路径再校验时拦下。

        Raises:
            taifeng.WorkspacePathError: 路径落在根目录之外。
        """
        root = self.root
        resolved = posixpath.normpath(posixpath.join(root, path))
        if resolved.startswith("//"):  # normpath 按 POSIX 保留恰好两个开头的 /
            resolved = "/" + resolved.lstrip("/")
        if resolved != root and not resolved.startswith(root.rstrip("/") + "/"):
            raise taifeng.WorkspacePathError(f"{path} 在沙盒根目录之外（{root}）")
        return resolved

    async def _call(self, method: str, path: str, **params: Any) -> dict[str, Any]:
        """先在本地校验边界，再发文件类请求，并把远端错误还原成文件系统异常。"""
        resolved = self.resolve(path)
        try:
            return await self._client.request(method, {"path": resolved, **params})
        except SandboxRemoteError as exc:
            raise _translate(exc, resolved) from exc

    async def read_bytes(self, path: str) -> bytes:
        """读取整个文件；大文件自动分段。"""
        chunks: list[bytes] = []
        offset = 0
        while True:
            result = await self._call(
                protocol.METHOD_FS_READ_FILE,
                path,
                offset=offset,
                length=protocol.MAX_FILE_BYTES,
            )
            try:
                chunk = base64.b64decode(_field(result, "data", str), validate=True)
            except (binascii.Error, ValueError) as exc:
                raise SandboxProtocolError("守护进程返回的文件内容不是合法 base64") from exc
            chunks.append(chunk)
            offset += len(chunk)
            if _field(result, "eof", bool) or not chunk:
                return b"".join(chunks)

    async def read_text(self, path: str, *, encoding: str = "utf-8") -> str:
        """读取文本文件。"""
        return (await self.read_bytes(path)).decode(encoding)

    async def write_bytes(self, path: str, data: bytes, *, create_parents: bool = True) -> None:
        """覆盖写入。不超过单次上限（16 MiB）时是原子的：守护进程写临时文件再替换。

        **超过单次上限的写入不是原子的**：第一段原子替换，之后的段逐段追加，读者可能看到只写了
        前几段的文件，中途失败也会留下它。线协议没有 rename，要原子地写大文件只能由上层自己约定
        （例如先写到临时路径，再经命令改名）。

        Raises:
            FileNotFoundError: 父目录不存在且 ``create_parents=False``。
            taifeng.WorkspacePathError: 路径在根目录之外。
        """
        step = protocol.MAX_FILE_BYTES
        await self._call(
            protocol.METHOD_FS_WRITE_FILE,
            path,
            data=base64.b64encode(data[:step]).decode("ascii"),
            createParents=create_parents,
            append=False,
        )
        for start in range(step, len(data), step):
            await self._call(
                protocol.METHOD_FS_WRITE_FILE,
                path,
                data=base64.b64encode(data[start : start + step]).decode("ascii"),
                append=True,
            )

    async def write_text(
        self, path: str, text: str, *, encoding: str = "utf-8", create_parents: bool = True
    ) -> None:
        """写入文本文件（覆盖）。"""
        await self.write_bytes(path, text.encode(encoding), create_parents=create_parents)

    async def list_directory(self, path: str) -> list[taifeng.WorkspaceEntry]:
        """列目录（不递归，不跟随符号链接判定类型），按名字排序。"""
        result = await self._call(protocol.METHOD_FS_READ_DIRECTORY, path)
        return [_entry(raw) for raw in _field(result, "entries", list)]

    async def metadata(self, path: str) -> taifeng.WorkspaceFileInfo:
        """取元数据（跟随符号链接）；不存在时 ``exists=False``，不抛异常。"""
        result = await self._call(protocol.METHOD_FS_GET_METADATA, path)
        if not _field(result, "exists", bool):
            return taifeng.WorkspaceFileInfo(exists=False)
        return taifeng.WorkspaceFileInfo(
            exists=True,
            is_directory=_field(result, "isDirectory", bool),
            is_file=_field(result, "isFile", bool),
            size=_field(result, "size", int),
            modified_at=float(_field(result, "modifiedAt", (int, float))),
        )

    async def create_directory(self, path: str, *, recursive: bool = True) -> None:
        """建目录（``WorkspaceFS`` 之外的附加方法）。"""
        await self._call(protocol.METHOD_FS_CREATE_DIRECTORY, path, recursive=recursive)

    async def remove(self, path: str, *, recursive: bool = False) -> None:
        """删除文件或目录；目录在 ``recursive=False`` 时须为空，根目录本身不允许删。"""
        await self._call(protocol.METHOD_FS_REMOVE, path, recursive=recursive)


__all__ = ["DaemonWorkspace"]
