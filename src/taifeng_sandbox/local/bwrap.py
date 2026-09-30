"""Linux bubblewrap 后端：把 ``SandboxPolicy`` 翻译成 ``bwrap`` 命令行。

参照 codex ``linux-sandbox/src/bwrap.rs``（Apache-2.0）的挂载顺序：先铺只读视图，再叠可写
根目录，最后遮蔽不可读路径；后出现的挂载覆盖先出现的。差异：

- 只用 bubblewrap 的命名空间与挂载，不叠加 Landlock / seccomp；
- 不可读路径用空 tmpfs（目录）或 ``/dev/null``（文件）遮蔽：目录看起来是空的，文件读不出
  原内容（读到空内容或被拒绝，取决于内核对用户命名空间里设备节点的处理）；
- 出网只有开 / 关两档（``--unshare-net``），不接代理。

本模块只做纯计算（拼参数），不启动进程，便于在非 Linux 机器上单测。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence

    from taifeng_sandbox.policy import SandboxPolicy

# 固定位置查找，不走 PATH：避免工作区里放一个同名可执行文件顶替（codex 同样排除 cwd 内的 bwrap）
BWRAP_SEARCH_PATHS: tuple[str, ...] = ("/usr/bin/bwrap", "/bin/bwrap", "/usr/local/bin/bwrap")

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


def build_argv(
    policy: SandboxPolicy,
    command: Sequence[str],
    *,
    bwrap: str,
    cwd: str | None = None,
    unreadable_files: Collection[str] = (),
) -> list[str]:
    """拼出完整命令行：``bwrap <隔离参数> -- <命令>``。

    Args:
        policy: 隔离策略。
        command: 要在沙盒内执行的 argv。
        bwrap: bubblewrap 可执行文件路径。
        cwd: 沙盒内工作目录；None 则沿用启动时的目录。
        unreadable_files: ``policy.unreadable_roots`` 中属于普通文件的那些路径
            （需要文件系统探测，由调用方提供，保持本函数纯计算）。
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
    argv.append("--")
    argv.extend(command)
    return argv


__all__ = ["BWRAP_SEARCH_PATHS", "build_argv"]
