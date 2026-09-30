"""``DaemonWorkspace``：文件访问与根目录约束。"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from taifeng_sandbox import SandboxRemoteError
from taifeng_sandbox.daemon import DaemonClient, DaemonWorkspace, protocol

if TYPE_CHECKING:
    from pathlib import Path


async def test_write_then_read_roundtrip(client: DaemonClient, root: Path) -> None:
    """写入后原样读回，父目录自动创建；相对路径相对根目录。"""
    workspace = DaemonWorkspace(client)
    payload = bytes(range(256)) * 4
    await workspace.write_bytes("nested/dir/blob.bin", payload)
    assert (root / "nested" / "dir" / "blob.bin").read_bytes() == payload
    assert await workspace.read_bytes(str(root / "nested" / "dir" / "blob.bin")) == payload


async def test_text_helpers_and_overwrite(client: DaemonClient) -> None:
    """文本读写；再次写入是覆盖而不是追加。"""
    workspace = DaemonWorkspace(client)
    await workspace.write_text("note.txt", "第一版")
    await workspace.write_text("note.txt", "第二版")
    assert await workspace.read_text("note.txt") == "第二版"


async def test_empty_file(client: DaemonClient) -> None:
    """空文件可写可读。"""
    workspace = DaemonWorkspace(client)
    await workspace.write_bytes("empty", b"")
    assert await workspace.read_bytes("empty") == b""
    assert (await workspace.metadata("empty")).size == 0


async def test_chunked_read_and_write(
    client: DaemonClient, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """超过单次上限的内容自动分段，结果完整。"""
    monkeypatch.setattr(protocol, "MAX_FILE_BYTES", 1000)
    workspace = DaemonWorkspace(client)
    payload = bytes(index % 251 for index in range(3500))
    await workspace.write_bytes("big.bin", payload)
    assert (root / "big.bin").read_bytes() == payload
    assert await workspace.read_bytes("big.bin") == payload


async def test_list_directory_sorted(client: DaemonClient, root: Path) -> None:
    """列目录按名字排序并区分文件与目录。"""
    (root / "b.txt").write_text("b")
    (root / "a.txt").write_text("a")
    (root / "sub").mkdir()
    entries = await DaemonWorkspace(client).list_directory(".")
    assert [entry.name for entry in entries] == ["a.txt", "b.txt", "sub"]
    assert [entry.is_directory for entry in entries] == [False, False, True]


async def test_metadata_for_missing_path(client: DaemonClient) -> None:
    """不存在的路径返回 ``exists=False``，不报错。"""
    assert (await DaemonWorkspace(client).metadata("nothing-here")).exists is False


async def test_read_missing_file_raises_file_not_found(client: DaemonClient) -> None:
    """读不存在的文件还原成 ``FileNotFoundError``。"""
    with pytest.raises(FileNotFoundError):
        await DaemonWorkspace(client).read_bytes("nothing-here")


async def test_create_and_remove(client: DaemonClient, root: Path) -> None:
    """建目录、删文件、递归删目录。"""
    workspace = DaemonWorkspace(client)
    await workspace.create_directory("x/y/z")
    await workspace.write_text("x/y/z/f.txt", "data")
    await workspace.remove("x/y/z/f.txt")
    assert not (root / "x" / "y" / "z" / "f.txt").exists()
    await workspace.remove("x", recursive=True)
    assert not (root / "x").exists()


async def test_remove_nonempty_directory_without_recursive_fails(
    client: DaemonClient, root: Path
) -> None:
    """非空目录不加 recursive 删不掉。"""
    (root / "full").mkdir()
    (root / "full" / "f").write_text("x")
    with pytest.raises(SandboxRemoteError):
        await DaemonWorkspace(client).remove("full")
    assert (root / "full" / "f").exists()


async def test_root_itself_cannot_be_removed(client: DaemonClient, root: Path) -> None:
    """根目录本身不允许删除。"""
    with pytest.raises(PermissionError):
        await DaemonWorkspace(client).remove(".", recursive=True)
    assert root.is_dir()


@pytest.mark.parametrize("escape", ["../outside.txt", "a/../../outside.txt"])
async def test_relative_escape_denied(client: DaemonClient, root: Path, escape: str) -> None:
    """用 ``..`` 逃出根目录被拒绝。"""
    with pytest.raises(PermissionError):
        await DaemonWorkspace(client).write_text(escape, "leak")
    assert not (root.parent / "outside.txt").exists()


async def test_absolute_path_outside_root_denied(client: DaemonClient, tmp_path: Path) -> None:
    """根目录之外的绝对路径读写都被拒绝。"""
    outside = tmp_path / "secret.txt"
    outside.write_text("s3cret")
    workspace = DaemonWorkspace(client)
    with pytest.raises(PermissionError):
        await workspace.read_bytes(str(outside))
    with pytest.raises(PermissionError):
        await workspace.write_text(str(outside), "overwritten")
    assert outside.read_text() == "s3cret"


async def test_sibling_directory_with_same_prefix_denied(
    client: DaemonClient, root: Path
) -> None:
    """与根目录同前缀的兄弟目录不算在根目录之内。"""
    sibling = root.parent / (root.name + "-evil")
    sibling.mkdir()
    (sibling / "f.txt").write_text("x")
    with pytest.raises(PermissionError):
        await DaemonWorkspace(client).read_bytes(str(sibling / "f.txt"))


async def test_symlink_escape_denied(client: DaemonClient, root: Path, tmp_path: Path) -> None:
    """借根目录内的符号链接指向根外，读写都被拒绝。"""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("s3cret")
    (root / "link").symlink_to(outside)
    workspace = DaemonWorkspace(client)
    with pytest.raises(PermissionError):
        await workspace.read_bytes("link/secret.txt")
    with pytest.raises(PermissionError):
        await workspace.write_text("link/new.txt", "leak")
    assert not (outside / "new.txt").exists()
