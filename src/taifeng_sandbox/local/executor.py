"""本机 OS 级隔离的 ``CommandExecutor`` 实现。

按 ADR 0002 决策 4，本机隔离不需要守护进程：直接把命令包进 ``sandbox-exec``（macOS）或
``bwrap``（Linux）再启动。审批、黑名单、env 白名单、超时、截断、取消仍由 taifeng 工具层负责
（ADR 0001 决策 2），这里只决定「以什么隔离方式运行」。

隔离后端不可用时构造即失败，不退回无隔离执行。
"""

from __future__ import annotations

import os
import shlex
import sys
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from taifeng_sandbox.errors import SandboxError, SandboxUnavailableError
from taifeng_sandbox.local import bwrap, seatbelt
from taifeng_sandbox.local.process import spawn_group

if TYPE_CHECKING:
    from taifeng import CommandExecutor, CommandProcess, CommandSpec

    from taifeng_sandbox.policy import SandboxPolicy

_SHELL = "/bin/sh"


def current_platform() -> str:
    """当前平台标识。

    经函数取值而不是直接判断 ``sys.platform``：类型检查器会按运行它的平台把另一个分支
    判成不可达，导致那部分代码不被检查。
    """
    return sys.platform


def command_argv(spec: CommandSpec) -> list[str]:
    """把 ``CommandSpec`` 展开成 argv：shell 模式交给 ``/bin/sh -c``，否则按 shell 词法切分。"""
    if spec.shell:
        return [_SHELL, "-c", spec.command]
    try:
        argv = shlex.split(spec.command)
    except ValueError as exc:
        raise SandboxError(f"命令无法切分为 argv：{exc}") from exc
    if not argv:
        raise SandboxError("命令为空")
    return argv


def resolve_policy_paths(policy: SandboxPolicy) -> SandboxPolicy:
    """把策略里的根目录解析为真实路径。

    seatbelt 与 bubblewrap 都按解析符号链接后的真实路径生效；macOS 上 ``/tmp``、``/var`` 是
    指向 ``/private`` 的符号链接，不解析会导致规则对不上。
    """
    return replace(
        policy,
        readable_roots=tuple(root.resolve() for root in policy.readable_roots),
        writable_roots=tuple(root.resolve() for root in policy.writable_roots),
        unreadable_roots=tuple(root.resolve() for root in policy.unreadable_roots),
    )


class SeatbeltCommandExecutor:
    """macOS seatbelt 隔离执行器。"""

    def __init__(
        self,
        policy: SandboxPolicy,
        *,
        sandbox_exec: str = seatbelt.DEFAULT_SANDBOX_EXEC,
    ) -> None:
        """
        Args:
            policy: 隔离策略；根目录在此解析为真实路径。
            sandbox_exec: ``sandbox-exec`` 可执行文件路径。

        Raises:
            SandboxUnavailableError: 当前平台不是 macOS，或找不到 ``sandbox-exec``。
        """
        if current_platform() != "darwin":
            raise SandboxUnavailableError("seatbelt 只在 macOS 上可用")
        if not Path(sandbox_exec).is_file():
            raise SandboxUnavailableError(f"找不到 sandbox-exec：{sandbox_exec}")
        self._policy = resolve_policy_paths(policy)
        self._sandbox_exec = sandbox_exec

    @property
    def policy(self) -> SandboxPolicy:
        """生效的隔离策略（根目录已解析）。"""
        return self._policy

    async def start(self, spec: CommandSpec) -> CommandProcess:
        """把命令包进 ``sandbox-exec`` 后启动。"""
        argv = seatbelt.build_argv(
            self._policy, command_argv(spec), sandbox_exec=self._sandbox_exec
        )
        return await spawn_group(argv, cwd=spec.cwd, env=spec.env, stdin=spec.stdin)


def find_bwrap() -> str | None:
    """在固定位置查找 bubblewrap；找不到返回 None。"""
    for candidate in bwrap.BWRAP_SEARCH_PATHS:
        if Path(candidate).is_file():
            return candidate
    return None


class BwrapCommandExecutor:
    """Linux bubblewrap 隔离执行器。"""

    def __init__(self, policy: SandboxPolicy, *, bwrap_path: str | None = None) -> None:
        """
        Args:
            policy: 隔离策略；根目录在此解析为真实路径。
            bwrap_path: bubblewrap 可执行文件路径；None 则在固定位置查找。

        Raises:
            SandboxUnavailableError: 当前平台不是 Linux，或找不到 bubblewrap。
        """
        if not current_platform().startswith("linux"):
            raise SandboxUnavailableError("bubblewrap 只在 Linux 上可用")
        resolved = bwrap_path if bwrap_path is not None else find_bwrap()
        if resolved is None or not Path(resolved).is_file():
            raise SandboxUnavailableError(
                "找不到 bubblewrap，请用系统包管理器安装（如 apt install bubblewrap）"
            )
        self._policy = resolve_policy_paths(policy)
        self._bwrap = resolved
        # 遮蔽文件与遮蔽目录的挂载方式不同，构造时探测一次
        self._unreadable_files = frozenset(
            str(path) for path in self._policy.unreadable_roots if path.is_file()
        )

    @property
    def policy(self) -> SandboxPolicy:
        """生效的隔离策略（根目录已解析）。"""
        return self._policy

    async def start(self, spec: CommandSpec) -> CommandProcess:
        """把命令包进 ``bwrap`` 后启动（ADR 0005 决策 1）。

        沙盒外的 bwrap 启动器只拿固定的空环境（``bwrap.LAUNCHER_ENV``），工作目录固定为
        ``/``：``spec.env`` 里的 ``LD_PRELOAD`` 之类影响不到它。``spec.env`` 写成
        ``--setenv`` 放进一个封口的 memfd，经 ``--args`` 交给 bwrap，只在沙盒里的目标进程上
        生效；值不进命令行（``/proc/<pid>/cmdline`` 对所有用户可读）。沙盒里的工作目录经
        ``--chdir`` 给出：``spec.cwd``，为 None 时取宿主进程的当前目录（与不隔离执行一致），
        沙盒里看不到它时 bwrap 报错退出。
        """
        command = command_argv(spec)
        encoded_env = bwrap.encode_args(bwrap.env_args(spec.env))
        cwd = spec.cwd if spec.cwd is not None else os.getcwd()
        args_fd = _sealed_memfd(encoded_env)
        try:
            argv = bwrap.build_argv(
                self._policy,
                command,
                bwrap=self._bwrap,
                cwd=cwd,
                unreadable_files=self._unreadable_files,
                args_fd=args_fd,
            )
            return await spawn_group(
                argv,
                cwd=bwrap.LAUNCHER_CWD,
                env=bwrap.LAUNCHER_ENV,
                stdin=spec.stdin,
                pass_fds=(args_fd,),
            )
        finally:
            # 子进程已继承自己的一份；bwrap 读完即关
            os.close(args_fd)


def _sealed_memfd(data: bytes) -> int:
    """把 ``data`` 写进一个匿名内存文件并封口，返回读位置在开头的 fd（带 close-on-exec）。

    不用管道：内容超过管道缓冲（默认 64 KiB）时，在 bwrap 开始读之前写入就会阻塞。不落盘：
    内容常含密钥。封口（禁止写入、增长、收缩）之后，即使有人经 ``/proc/<pid>/fd``
    打开它，也改不了 bwrap 将要读到的参数。
    """
    # 这里直接判断 sys.platform：memfd 与封口常量只在 Linux 的类型存根里，需要让类型检查器收窄
    if sys.platform != "linux":
        raise SandboxUnavailableError("memfd 只在 Linux 上可用")
    # fcntl 在这里才导入：守护进程执行器经 command_argv 导入本模块，不连带依赖类 Unix 专有模块
    import fcntl

    fd = os.memfd_create("taifeng-sandbox-bwrap-args", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
        fcntl.fcntl(
            fd,
            fcntl.F_ADD_SEALS,
            fcntl.F_SEAL_SEAL | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW | fcntl.F_SEAL_WRITE,
        )
        os.lseek(fd, 0, os.SEEK_SET)
    except BaseException:
        os.close(fd)
        raise
    return fd


def create_local_executor(policy: SandboxPolicy) -> CommandExecutor:
    """按当前平台选择本机隔离后端。

    Raises:
        SandboxUnavailableError: 当前平台没有受支持的本机隔离后端。
    """
    platform = current_platform()
    if platform == "darwin":
        return SeatbeltCommandExecutor(policy)
    if platform.startswith("linux"):
        return BwrapCommandExecutor(policy)
    raise SandboxUnavailableError(f"平台 {platform!r} 没有受支持的本机隔离后端")


__all__ = [
    "BwrapCommandExecutor",
    "SeatbeltCommandExecutor",
    "command_argv",
    "create_local_executor",
    "find_bwrap",
    "resolve_policy_paths",
]
