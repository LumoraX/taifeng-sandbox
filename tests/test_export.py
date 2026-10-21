"""有界文件快照不能经符号链接、特殊文件或路径交换越出指定根。"""

from __future__ import annotations

import os

import pytest

from taifeng_sandbox import SandboxPolicyError, export_files


async def test_export_content_and_limits(tmp_path):
    """读取确定的非空普通文件，并验证数量和总字节边界。"""
    (tmp_path / 'a.txt').write_bytes(b'result')
    result = await export_files(tmp_path, ['a.txt'], max_total_bytes=6)
    assert result['a.txt'].content == b'result'
    assert result['a.txt'].size == 6
    with pytest.raises(SandboxPolicyError):
        await export_files(tmp_path, ['a.txt'], max_total_bytes=5)
    (tmp_path / 'empty.txt').touch()
    with pytest.raises(SandboxPolicyError):
        await export_files(tmp_path, ['empty.txt'])


@pytest.mark.parametrize('kind', ['symlink', 'nested_symlink', 'hardlink', 'fifo', 'traversal'])
async def test_export_rejects_unsafe_files(tmp_path, kind):
    """每个路径组件不跟随链接，拒绝硬链接、FIFO以及越界路径。"""
    root = tmp_path / 'root'
    root.mkdir()
    secret = tmp_path / 'secret'
    secret.write_bytes(b'PRIVATE')
    path = 'bad'
    if kind == 'symlink':
        (root / path).symlink_to(secret)
    elif kind == 'nested_symlink':
        (root / path).symlink_to(tmp_path, target_is_directory=True)
        path = 'bad/secret'
    elif kind == 'hardlink':
        os.link(secret, root / path)
    elif kind == 'fifo':
        os.mkfifo(root / path)
    else:
        path = '../secret'
    with pytest.raises(SandboxPolicyError):
        await export_files(root, [path])


async def test_export_rejects_file_swapped_during_read(tmp_path, monkeypatch):
    """打开后目录项被交换，即使旧描述符仍安全，也不得当成当前稳定成果。"""
    import taifeng_sandbox.export as module

    path = tmp_path / 'result.txt'
    path.write_bytes(b'original')
    replacement = tmp_path / 'replacement.txt'
    replacement.write_bytes(b'changed')
    read = os.read
    changed = False

    def swap(descriptor, amount):
        """在读取第一个字节前交换路径，触发稳定性检查。"""
        nonlocal changed
        if not changed:
            changed = True
            replacement.replace(path)
        return read(descriptor, amount)

    monkeypatch.setattr(module.os, 'read', swap)
    with pytest.raises(SandboxPolicyError, match='变化'):
        await export_files(tmp_path, ['result.txt'])
