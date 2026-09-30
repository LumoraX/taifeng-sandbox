"""经守护进程访问执行环境里的文件。

taifeng 的 ``WorkspaceFS`` 协议尚未落地（ADR 0002 后果一节）。在那之前，本类以自有接口提供
文件访问，供宿主往沙盒里放输入、取产物；协议落地后在其上加一层适配即可。
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from taifeng_sandbox.daemon import protocol
from taifeng_sandbox.errors import SandboxProtocolError, SandboxRemoteError

if TYPE_CHECKING:
    from taifeng_sandbox.daemon.client import DaemonClient


@dataclass(frozen=True)
class FileMetadata:
    """文件或目录的元数据。"""

    exists: bool
    is_directory: bool = False
    is_file: bool = False
    size: int = 0
    modified_at: float = 0.0


@dataclass(frozen=True)
class DirectoryEntry:
    """目录下的一个条目。"""

    name: str
    is_directory: bool
    is_file: bool
    is_symlink: bool


def _translate(exc: SandboxRemoteError, path: str) -> OSError:
    """把线协议错误码还原成标准的文件系统异常。"""
    if exc.code == protocol.ERROR_NOT_FOUND:
        return FileNotFoundError(f"{path}: {exc}")
    if exc.code == protocol.ERROR_ACCESS_DENIED:
        return PermissionError(f"{path}: {exc}")
    return exc


class DaemonWorkspace:
    """执行环境里的文件视图。所有路径都被守护进程限制在其根目录之内。"""

    def __init__(self, client: DaemonClient) -> None:
        """绑定一条已握手的守护进程连接。"""
        self._client = client

    async def _call(self, method: str, path: str, **params: Any) -> dict[str, Any]:
        """发文件类请求，并把远端错误还原成文件系统异常。"""
        try:
            return await self._client.request(method, {"path": path, **params})
        except SandboxRemoteError as exc:
            raise _translate(exc, path) from exc

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
                chunk = base64.b64decode(str(result.get("data", "")), validate=True)
            except (binascii.Error, ValueError) as exc:
                raise SandboxProtocolError("守护进程返回的文件内容不是合法 base64") from exc
            chunks.append(chunk)
            offset += len(chunk)
            if result.get("eof") or not chunk:
                return b"".join(chunks)

    async def read_text(self, path: str, *, encoding: str = "utf-8") -> str:
        """读取文本文件。"""
        return (await self.read_bytes(path)).decode(encoding)

    async def write_bytes(self, path: str, data: bytes, *, create_parents: bool = True) -> None:
        """写入文件（覆盖）；大文件自动分段追加。"""
        step = protocol.MAX_FILE_BYTES
        first = data[:step]
        await self._call(
            protocol.METHOD_FS_WRITE_FILE,
            path,
            data=base64.b64encode(first).decode("ascii"),
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

    async def list_directory(self, path: str) -> list[DirectoryEntry]:
        """列目录（不递归），按名字排序。"""
        result = await self._call(protocol.METHOD_FS_READ_DIRECTORY, path)
        entries = result.get("entries")
        if not isinstance(entries, list):
            raise SandboxProtocolError("守护进程返回的目录列表格式不对")
        return [
            DirectoryEntry(
                name=str(entry["name"]),
                is_directory=bool(entry.get("isDirectory")),
                is_file=bool(entry.get("isFile")),
                is_symlink=bool(entry.get("isSymlink")),
            )
            for entry in entries
            if isinstance(entry, dict) and "name" in entry
        ]

    async def metadata(self, path: str) -> FileMetadata:
        """取元数据；不存在时 ``exists=False``。"""
        result = await self._call(protocol.METHOD_FS_GET_METADATA, path)
        if not result.get("exists"):
            return FileMetadata(exists=False)
        return FileMetadata(
            exists=True,
            is_directory=bool(result.get("isDirectory")),
            is_file=bool(result.get("isFile")),
            size=int(result.get("size", 0)),
            modified_at=float(result.get("modifiedAt", 0.0)),
        )

    async def create_directory(self, path: str, *, recursive: bool = True) -> None:
        """建目录。"""
        await self._call(protocol.METHOD_FS_CREATE_DIRECTORY, path, recursive=recursive)

    async def remove(self, path: str, *, recursive: bool = False) -> None:
        """删除文件或目录。"""
        await self._call(protocol.METHOD_FS_REMOVE, path, recursive=recursive)


__all__ = ["DaemonWorkspace", "DirectoryEntry", "FileMetadata"]
