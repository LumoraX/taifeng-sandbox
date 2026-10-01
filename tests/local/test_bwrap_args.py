"""bubblewrap 命令行的纯计算测试（任何平台都能跑）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from taifeng_sandbox import SandboxError, SandboxPolicy
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


def test_launcher_env_and_cwd_are_fixed() -> None:
    """沙盒外的 bwrap 启动器只用固定的空环境、固定的工作目录 ``/``。"""
    assert dict(bwrap.LAUNCHER_ENV) == {}
    assert bwrap.LAUNCHER_CWD == "/"


def test_env_becomes_setenv_args_in_order() -> None:
    """目标进程的环境逐个变成 ``--setenv 名 值``，保持顺序，值原样保留。"""
    env = {"PATH": "/usr/bin", "LD_PRELOAD": "/x.so", "ODD": "--bind / / =\n"}
    assert bwrap.env_args(env) == [
        "--setenv", "PATH", "/usr/bin",
        "--setenv", "LD_PRELOAD", "/x.so",
        "--setenv", "ODD", "--bind / / =\n",
    ]


def test_args_file_is_nul_terminated() -> None:
    """``--args`` 读取的内容：每个参数以 NUL 结尾。"""
    assert bwrap.encode_args(["--setenv", "A", ""]) == b"--setenv\0A\0\0"
    assert bwrap.encode_args([]) == b""


@pytest.mark.parametrize(
    ("name", "value"), [("", "x"), ("A=B", "x"), ("A\0", "x"), ("A", "x\0y")]
)
def test_invalid_env_rejected(name: str, value: str) -> None:
    """变量名为空、含 ``=`` 或 NUL，值含 NUL：启动前就拒绝（OSError 子类）。"""
    with pytest.raises(SandboxError, match="环境变量"):
        bwrap.env_args({name: value})


def test_args_fd_goes_before_command_and_env_stays_out_of_argv() -> None:
    """``--args FD`` 排在 ``--`` 之前；环境的值不出现在命令行里。"""
    argv = bwrap.build_argv(
        SandboxPolicy(), ["/bin/true"], bwrap="bwrap", cwd="/work", args_fd=7
    )
    at = argv.index("--args")
    assert argv[at + 1] == "7"
    assert at < argv.index("--")
    assert "--setenv" not in argv
    assert "--clearenv" not in argv
