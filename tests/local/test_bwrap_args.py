"""bubblewrap 命令行的纯计算测试（任何平台都能跑）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from taifeng_sandbox import SandboxPolicy
from taifeng_sandbox.local import bwrap


def _pairs(argv: list[str], flag: str) -> list[str]:
    """取出某个双参数挂载选项的目标路径列表。"""
    return [argv[i + 2] for i, item in enumerate(argv) if item == flag]


def test_read_only_policy_mounts_root_read_only_and_cuts_network() -> None:
    """只读策略：根只读挂载、无可写绑定、断网。"""
    argv = bwrap.build_argv(SandboxPolicy.read_only(), ["/bin/true"], bwrap="/usr/bin/bwrap")
    assert argv[:3] == ["/usr/bin/bwrap", "--new-session", "--die-with-parent"]
    assert argv[3:6] == ["--ro-bind", "/", "/"]
    assert "--bind" not in argv
    assert "--unshare-net" in argv
    assert argv[-2:] == ["--", "/bin/true"]


def test_network_enabled_keeps_network_namespace() -> None:
    """允许出网时不隔离网络命名空间。"""
    argv = bwrap.build_argv(SandboxPolicy(network=True), ["/bin/true"], bwrap="bwrap")
    assert "--unshare-net" not in argv
    assert "--unshare-user" in argv
    assert "--unshare-pid" in argv


def test_writable_bind_comes_after_read_view() -> None:
    """可写绑定必须在只读视图之后，才能覆盖它。"""
    policy = SandboxPolicy.workspace_write(Path("/work"))
    argv = bwrap.build_argv(policy, ["/bin/true"], bwrap="bwrap")
    assert argv.index("--bind") > argv.index("--ro-bind")
    assert _pairs(argv, "--bind") == ["/work"]


def test_restricted_read_starts_from_empty_root() -> None:
    """受限读从空根开始，只挂系统目录与指定只读根。"""
    policy = SandboxPolicy.workspace_only(Path("/work"), readable_roots=(Path("/data"),))
    argv = bwrap.build_argv(policy, ["/bin/true"], bwrap="bwrap")
    assert argv[3:5] == ["--tmpfs", "/"]
    assert "/usr" in _pairs(argv, "--ro-bind-try")
    assert _pairs(argv, "--ro-bind") == ["/data"]


def test_unreadable_directory_and_file_masked_differently() -> None:
    """不可读目录盖空 tmpfs，不可读文件换成 /dev/null；遮蔽排在可写绑定之后。"""
    policy = SandboxPolicy.workspace_write(
        Path("/work"), unreadable_roots=(Path("/home/u/.ssh"), Path("/etc/token"))
    )
    argv = bwrap.build_argv(
        policy, ["/bin/true"], bwrap="bwrap", unreadable_files={"/etc/token"}
    )
    ssh_at = argv.index("/home/u/.ssh")
    assert argv[ssh_at - 1] == "--tmpfs"
    token_at = argv.index("/etc/token")
    assert argv[token_at - 2 : token_at] == ["--ro-bind", "/dev/null"]
    assert ssh_at > argv.index("--bind")


def test_chdir_passed_through() -> None:
    """工作目录经 --chdir 传入沙盒。"""
    argv = bwrap.build_argv(SandboxPolicy(), ["/bin/true"], bwrap="bwrap", cwd="/work")
    at = argv.index("--chdir")
    assert argv[at + 1] == "/work"
    assert at < argv.index("--")


def test_empty_command_rejected() -> None:
    """空命令直接拒绝。"""
    with pytest.raises(ValueError, match="command"):
        bwrap.build_argv(SandboxPolicy(), [], bwrap="bwrap")
