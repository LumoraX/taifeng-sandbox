"""从已停止写入的显式绑定目录导出有界快照；不执行后端推断或路径兜底。"""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
from dataclasses import dataclass
from typing import TYPE_CHECKING

from taifeng_sandbox.errors import SandboxPolicyError

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


@dataclass(frozen=True)
class ExportedFile:
    """经过类型、大小和稳定性检查的不可变文件内容。"""

    content: bytes
    sha256: str
    size: int


async def export_files(root: Path, paths: Sequence[str], *, max_total_bytes: int = 20 * 1024 * 1024,
                       max_files: int = 8) -> dict[str, ExportedFile]:
    """从调用者明确绑定的本地根读取，调用前必须停止全部写入者。

    只适用于真实本地根或已知容器绑定根；远端沙盒不得使用本函数当隐式宿主兜底。
    每级openat禁止跟随符号链接，读取限制不依赖模型或元数据的大小声明。
    """
    if not 0 < len(paths) <= max_files or len(set(paths)) != len(paths) or max_total_bytes < 1:
        raise SandboxPolicyError('导出数量、重复路径或字节上限不合法')
    worker = asyncio.create_task(asyncio.to_thread(_export, root, tuple(paths), max_total_bytes))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
        worker.result()
        raise


def _export(root: Path, paths: tuple[str, ...], maximum: int) -> dict[str, ExportedFile]:
    """固定根目录描述符，所有文件的实际字节共享同一总上限。"""
    result: dict[str, ExportedFile] = {}
    try:
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for path in paths:
                content = _read(descriptor, path, maximum)
                maximum -= len(content)
                result[path] = ExportedFile(content, hashlib.sha256(content).hexdigest(), len(content))
        finally:
            os.close(descriptor)
    except (OSError, ValueError) as exc:
        raise SandboxPolicyError(f'导出文件不安全或不可读：{exc}') from exc
    return result


def _read(root_fd: int, path: str, maximum: int) -> bytes:
    """不跟随任意路径组件的链接，拒绝特殊文件并检查读取期间替换。"""
    parts = path.split('/')
    if not path or '\\' in path or any(part in {'', '.', '..'} for part in parts):
        raise SandboxPolicyError('导出必须是安全相对路径')
    parent = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = child
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or not 0 < before.st_size <= maximum:
                raise SandboxPolicyError('成果必须是上限内的非空、无硬链接普通文件')
            chunks: list[bytes] = []
            size = 0
            while True:
                chunk = os.read(descriptor, min(65536, maximum + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > maximum:
                    raise SandboxPolicyError('成果实际字节超过上限')
            after = os.fstat(descriptor)
            current = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
            def identity(value: os.stat_result) -> tuple[int, ...]:
                """描述符和当前目录项必须指向同一稳定普通文件。"""
                return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns,
                        value.st_ctime_ns, value.st_nlink)
            if identity(before) != identity(after) or identity(after) != identity(current) or size != before.st_size:
                raise SandboxPolicyError('成果在读取期间变化')
            return b''.join(chunks)
        finally:
            os.close(descriptor)
    finally:
        os.close(parent)


__all__ = ['ExportedFile', 'export_files']
