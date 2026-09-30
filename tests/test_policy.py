"""``SandboxPolicy`` 的构造期校验与预设。"""

from __future__ import annotations

from pathlib import Path

import pytest

from taifeng_sandbox import SandboxPolicy, SandboxPolicyError


def test_default_policy_denies_write_and_network() -> None:
    """默认策略：全盘只读、不可写、不出网。"""
    policy = SandboxPolicy()
    assert policy.read_scope == "all"
    assert policy.writable_roots == ()
    assert policy.network is False


def test_relative_root_rejected() -> None:
    """根目录必须是绝对路径。"""
    with pytest.raises(SandboxPolicyError):
        SandboxPolicy(writable_roots=(Path("relative/dir"),))


def test_duplicate_roots_deduplicated_in_order() -> None:
    """重复的根目录去重，保持首次出现的顺序。"""
    policy = SandboxPolicy(writable_roots=(Path("/a"), Path("/b"), Path("/a")))
    assert policy.writable_roots == (Path("/a"), Path("/b"))


def test_writable_inside_unreadable_rejected() -> None:
    """可写根落在不可读目录内是矛盾策略。"""
    with pytest.raises(SandboxPolicyError):
        SandboxPolicy(writable_roots=(Path("/secret/work"),), unreadable_roots=(Path("/secret"),))


def test_invalid_read_scope_rejected() -> None:
    """``read_scope`` 只接受两个取值。"""
    with pytest.raises(SandboxPolicyError):
        SandboxPolicy(read_scope="everything")  # type: ignore[arg-type]


def test_workspace_presets() -> None:
    """两个工作区预设的读范围不同，写范围相同。"""
    workspace = Path("/work")
    wide = SandboxPolicy.workspace_write(workspace)
    narrow = SandboxPolicy.workspace_only(workspace, readable_roots=(Path("/data"),))
    assert wide.read_scope == "all"
    assert narrow.read_scope == "restricted"
    assert wide.writable_roots == narrow.writable_roots == (workspace,)
    assert narrow.readable_roots == (Path("/data"),)


def test_policy_is_immutable() -> None:
    """策略不可变，避免启动后被改写。"""
    policy = SandboxPolicy()
    with pytest.raises(AttributeError):
        policy.network = True  # type: ignore[misc]
