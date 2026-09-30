"""macOS seatbelt 后端：把 ``SandboxPolicy`` 翻译成 SBPL 配置与 ``sandbox-exec`` 命令行。

参照 codex ``sandboxing/src/seatbelt.rs`` 与 ``seatbelt_base_policy.sbpl``（Apache-2.0）：
默认全拒、子进程继承、根目录经 ``-D`` 参数传入而不是拼进配置文本。差异：

- 基础配置是本仓自写的精简版，只放开进程运行必需的能力；``sysctl-read`` 整体放开
  （codex 逐项列举），换取不同 macOS 版本下的兼容性；
- 不可读目录用排在最后的 ``deny`` 规则表达（seatbelt 以最后匹配的规则为准）；
- 不处理代理、证书、Unix socket 白名单——出网只有开 / 关两档。

本模块只做纯计算（拼配置与参数），不启动进程，便于单测。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from taifeng_sandbox.policy import SandboxPolicy

DEFAULT_SANDBOX_EXEC = "/usr/bin/sandbox-exec"

# 进程运行必需的基础能力：默认全拒，再逐项放开
_BASE_PROFILE = """\
(version 1)
(deny default)

; 子进程继承同一份配置
(allow process-exec)
(allow process-fork)
(allow signal (target same-sandbox))
(allow process-info* (target same-sandbox))

; 运行时探测硬件 / 内核信息
(allow sysctl-read)

; 标准设备
(allow file-read* file-write-data file-ioctl
  (literal "/dev/null")
  (literal "/dev/zero")
  (literal "/dev/random")
  (literal "/dev/urandom")
  (literal "/dev/dtracehelper")
  (literal "/dev/tty"))

; 伪终端
(allow pseudo-tty)
(allow file-read* file-write* file-ioctl (literal "/dev/ptmx"))
(allow file-read* file-write* file-ioctl (regex #"^/dev/ttys[0-9]+"))

; 用户信息查询、信号量（Python multiprocessing 等依赖）
(allow mach-lookup (global-name "com.apple.system.opendirectoryd.libinfo"))
(allow ipc-posix-sem)

; 任何路径都允许取元数据：进程要沿父目录逐级 stat 才能打开深层文件
(allow file-read-metadata)
(allow file-test-existence)
"""

# read_scope="restricted" 时仍需可读的系统运行时目录（解释器、动态库、时区、证书等）
_SYSTEM_READABLE_ROOTS: tuple[str, ...] = (
    "/bin",
    "/sbin",
    "/usr",
    "/System",
    "/Library",
    "/opt",
    "/private/etc",
    "/private/var/db",
    "/dev",
    "/Applications/Xcode.app",
)

_NETWORK_PROFILE = """\
; 出网：放开套接字与解析 / 证书校验依赖的系统服务
(allow network-outbound)
(allow network-inbound)
(allow system-socket)
(allow mach-lookup
  (global-name "com.apple.bsd.dirhelper")
  (global-name "com.apple.system.opendirectoryd.membership")
  (global-name "com.apple.SecurityServer")
  (global-name "com.apple.networkd")
  (global-name "com.apple.ocspd")
  (global-name "com.apple.trustd.agent")
  (global-name "com.apple.SystemConfiguration.DNSConfiguration")
  (global-name "com.apple.SystemConfiguration.configd"))
"""


def _subpath_rules(operation: str, prefix: str, count: int) -> list[str]:
    """为 ``count`` 个参数化根目录生成 ``allow`` / ``deny`` 规则。"""
    return [f'({operation} (subpath (param "{prefix}_{index}")))' for index in range(count)]


def build_profile(policy: SandboxPolicy) -> str:
    """生成 SBPL 配置文本。根目录以 ``(param ...)`` 占位，取值由 ``build_params`` 给出。"""
    sections: list[str] = [_BASE_PROFILE]

    if policy.read_scope == "all":
        sections.append("; 全盘可读\n(allow file-read*)")
    else:
        system_rules = "\n".join(
            f'  (subpath "{root}")' for root in _SYSTEM_READABLE_ROOTS
        )
        sections.append(
            "; 受限读：系统运行时目录 + 根目录本身\n"
            f'(allow file-read*\n  (literal "/")\n{system_rules})'
        )
        readable = _subpath_rules("allow file-read*", "READABLE_ROOT", len(policy.readable_roots))
        if readable:
            sections.append("; 额外只读根目录\n" + "\n".join(readable))

    writable = _subpath_rules(
        "allow file-read* file-write*", "WRITABLE_ROOT", len(policy.writable_roots)
    )
    if writable:
        sections.append("; 可写根目录\n" + "\n".join(writable))

    if policy.network:
        sections.append(_NETWORK_PROFILE)

    # deny 必须排在最后：seatbelt 以最后匹配的规则为准
    blocked = _subpath_rules(
        "deny file-read* file-write*", "UNREADABLE_ROOT", len(policy.unreadable_roots)
    )
    if blocked:
        sections.append("; 不可读目录（覆盖前面的放行）\n" + "\n".join(blocked))

    return "\n".join(section.rstrip("\n") for section in sections) + "\n"


def build_params(policy: SandboxPolicy) -> list[tuple[str, str]]:
    """生成 ``-D`` 参数表。调用方须保证根目录已解析为真实路径（seatbelt 按真实路径匹配）。"""
    params: list[tuple[str, str]] = []
    for prefix, roots in (
        ("READABLE_ROOT", policy.readable_roots),
        ("WRITABLE_ROOT", policy.writable_roots),
        ("UNREADABLE_ROOT", policy.unreadable_roots),
    ):
        if prefix == "READABLE_ROOT" and policy.read_scope == "all":
            # 全盘可读时配置里没有 READABLE_ROOT 占位，不传多余参数
            continue
        params.extend((f"{prefix}_{index}", str(root)) for index, root in enumerate(roots))
    return params


def build_argv(
    policy: SandboxPolicy,
    command: Sequence[str],
    *,
    sandbox_exec: str = DEFAULT_SANDBOX_EXEC,
) -> list[str]:
    """拼出完整命令行：``sandbox-exec -p <配置> -Dk=v ... -- <命令>``。"""
    if not command:
        raise ValueError("command 不能为空")
    argv = [sandbox_exec, "-p", build_profile(policy)]
    argv.extend(f"-D{key}={value}" for key, value in build_params(policy))
    argv.append("--")
    argv.extend(command)
    return argv


__all__ = ["DEFAULT_SANDBOX_EXEC", "build_argv", "build_params", "build_profile"]
