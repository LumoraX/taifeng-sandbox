"""Linux bubblewrap 后端：把 ``SandboxPolicy`` 翻译成 ``bwrap`` 命令行。

参照 codex ``linux-sandbox/src/bwrap.rs``（Apache-2.0）的挂载顺序：先铺只读视图，再叠可写
根目录，最后遮蔽不可读路径；后出现的挂载覆盖先出现的。差异：

- 只用 bubblewrap 的命名空间与挂载，不叠加 Landlock / seccomp；
- 不可读路径用空 tmpfs（目录）或 ``/dev/null``（文件）遮蔽：目录看起来是空的，文件读不出
  原内容（读到空内容或被拒绝，取决于内核对用户命名空间里设备节点的处理）；
- 出网只有开 / 关两档（``--unshare-net``），不接代理；
- 目标进程的环境不经启动器的环境传递，而是以 ``--setenv`` 写进 ``--args`` 读取的 fd，
  启动器本身只拿 ``LAUNCHER_ENV``（见 ``env_args``）。经 ``--args`` 的封口 memfd 传参参照
  flatpak 的做法（LGPL-2.1+，只借鉴做法、不复用代码）；codex 把环境交给启动器，这里不同。

本模块只做纯计算（拼参数），不启动进程，便于在非 Linux 机器上单测。
"""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING

from taifeng_sandbox.errors import SandboxError

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence

    from taifeng_sandbox.policy import SandboxPolicy

# 固定位置查找，不走 PATH：避免工作区里放一个同名可执行文件顶替（codex 同样排除 cwd 内的 bwrap）
BWRAP_SEARCH_PATHS: tuple[str, ...] = ("/usr/bin/bwrap", "/bin/bwrap", "/usr/local/bin/bwrap")

# 沙盒外的 bwrap 启动器进程本身用的环境：固定为空（ADR 0005 决策 1）。
#
# bwrap 在主流发行版上不是 setuid 程序，glibc 照常处理启动器环境里的 ``LD_PRELOAD``、
# ``LD_LIBRARY_PATH``、``GCONV_PATH`` 等变量——把 ``CommandSpec.env`` 交给启动器，env 的提供方就能
# 在沙盒建好之前以宿主用户身份在沙盒外执行代码。目标进程的环境改由 ``env_args`` 在 bwrap 解析
# 参数时设置：那时动态链接早已结束，这些变量只对之后在沙盒里 exec 的目标进程生效。
#
# 启动器环境为空，所以不需要 ``--clearenv``（bubblewrap 0.5.0 才有，0.4.x 发行版上会直接报错
# 退出）；沙盒里的环境恰好是 ``CommandSpec.env``，外加 bwrap 自己设的 ``PWD``。
LAUNCHER_ENV: Mapping[str, str] = MappingProxyType({})

# 启动器的工作目录：固定为 ``/``，不用被隔离一方可控的目录。沙盒里的工作目录另经 ``--chdir`` 给出。
LAUNCHER_CWD = "/"

# read_scope="restricted" 时仍需可读的系统运行时目录
_SYSTEM_READABLE_ROOTS: tuple[str, ...] = (
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib32",
    "/lib64",
    "/etc",
    "/opt",
)


def _read_view_args(policy: SandboxPolicy) -> list[str]:
    """只读视图：全盘只读，或空根加系统目录与指定只读根。"""
    if policy.read_scope == "all":
        return ["--ro-bind", "/", "/"]
    args = ["--tmpfs", "/"]
    for root in _SYSTEM_READABLE_ROOTS:
        # 不同发行版目录不尽相同，用 -try 变体跳过不存在的
        args.extend(["--ro-bind-try", root, root])
    for readable in policy.readable_roots:
        args.extend(["--ro-bind", str(readable), str(readable)])
    return args


def _mask_args(policy: SandboxPolicy, file_paths: Collection[str]) -> list[str]:
    """遮蔽不可读路径：目录盖空 tmpfs，文件换成 ``/dev/null``。"""
    args: list[str] = []
    for blocked in policy.unreadable_roots:
        target = str(blocked)
        if target in file_paths:
            args.extend(["--ro-bind", "/dev/null", target])
        else:
            args.extend(["--tmpfs", target])
    return args


def _check_env_entry(name: str, value: str) -> None:
    """变量名非空、不含 ``=`` 与 NUL，值不含 NUL；否则 bwrap 的 ``setenv`` 会失败或截断。"""
    if not name or "=" in name or "\0" in name:
        raise SandboxError(f"环境变量名不合法：{name!r}")
    if "\0" in value:
        raise SandboxError(f"环境变量 {name} 的值含 NUL")


def env_args(env: Mapping[str, str]) -> list[str]:
    """把目标进程的完整环境翻译成 bwrap 参数：每个变量一组 ``--setenv 名 值``，保持顺序。

    这些参数应经 ``--args FD`` 交给 bwrap（``encode_args`` 编码），而不是拼进命令行：
    ``/proc/<pid>/cmdline`` 默认对本机所有用户可读，而原先经启动器环境传递时，
    ``/proc/<pid>/environ`` 只有同一用户（且有 ptrace 读权限）才读得到。fd 里的内容同样
    只有同一用户经 ``/proc/<pid>/fd`` 才碰得到，暴露面与原来相同。

    Raises:
        SandboxError: 变量名为空、含 ``=`` 或 NUL，或值含 NUL。
    """
    args: list[str] = []
    for name, value in env.items():
        _check_env_entry(name, value)
        args.extend(["--setenv", name, value])
    return args


def encode_args(args: Sequence[str]) -> bytes:
    """编码成 bwrap ``--args FD`` 读取的格式：UTF-8，每个参数以 NUL 结尾。"""
    return b"".join(arg.encode() + b"\0" for arg in args)


def build_argv(
    policy: SandboxPolicy,
    command: Sequence[str],
    *,
    bwrap: str,
    cwd: str | None = None,
    unreadable_files: Collection[str] = (),
    args_fd: int | None = None,
) -> list[str]:
    """拼出完整命令行：``bwrap <隔离参数> [--args FD] -- <命令>``。

    Args:
        policy: 隔离策略。
        command: 要在沙盒内执行的 argv。
        bwrap: bubblewrap 可执行文件路径。
        cwd: 沙盒内工作目录；None 则不传 ``--chdir``（bwrap 沿用启动器的目录）。
        unreadable_files: ``policy.unreadable_roots`` 中属于普通文件的那些路径
            （需要文件系统探测，由调用方提供，保持本函数纯计算）。
        args_fd: 装着额外参数（``env_args`` 的产物）的 fd；None 则不传 ``--args``。
    """
    if not command:
        raise ValueError("command 不能为空")
    argv = [bwrap, "--new-session", "--die-with-parent"]
    argv.extend(_read_view_args(policy))
    argv.extend(["--dev", "/dev", "--proc", "/proc"])
    for writable in policy.writable_roots:
        argv.extend(["--bind", str(writable), str(writable)])
    argv.extend(_mask_args(policy, unreadable_files))
    argv.extend(["--unshare-user", "--unshare-pid", "--unshare-ipc"])
    if not policy.network:
        argv.append("--unshare-net")
    if cwd is not None:
        argv.extend(["--chdir", cwd])
    if args_fd is not None:
        argv.extend(["--args", str(args_fd)])
    argv.append("--")
    argv.extend(command)
    return argv


__all__ = [
    "BWRAP_SEARCH_PATHS",
    "LAUNCHER_CWD",
    "LAUNCHER_ENV",
    "build_argv",
    "encode_args",
    "env_args",
]
