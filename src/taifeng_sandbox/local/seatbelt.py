"""macOS seatbelt 后端：把 ``SandboxPolicy`` 翻译成 SBPL 配置与 ``sandbox-exec`` 命令行。

参照 codex ``sandboxing/src/seatbelt.rs`` 与 ``seatbelt_base_policy.sbpl``（Apache-2.0）：
默认全拒、子进程继承、根目录经 ``-D`` 参数传入而不是拼进配置文本。差异：

- 基础配置是本仓自写的精简版，只放开进程运行必需的能力；
- ``sysctl-read`` 与 codex 一样逐项放行（``SYSCTL_READ_NAMES`` / ``SYSCTL_READ_PREFIXES``），
  清单取 codex 基础策略与 macOS 自带的 App Sandbox 配置
  （``/System/Library/Sandbox/Profiles/container.sb``）里只读系统信息的那些项；不同于 codex，
  不放行 ``kern.proc.pid.*`` / ``kern.proc.pgrp.*``（ADR 0005 决策 2）。它挡不住沙盒读其他
  进程的参数与环境（内核对那一项不走沙盒检查），那是已知限制；
- 不可读目录用排在最后的 ``deny`` 规则表达（seatbelt 以最后匹配的规则为准）；
- 不处理代理与证书；出网档只放行 IP 远端与 DNS 用的 mDNSResponder 套接字，其余 Unix 套接字
  一律拒绝（codex 出网档不加过滤，ADR 0005 决策 3）。

本模块只做纯计算（拼配置与参数），不启动进程，便于单测。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from taifeng_sandbox.policy import SandboxPolicy

DEFAULT_SANDBOX_EXEC = "/usr/bin/sandbox-exec"

# 放行读取的 sysctl（精确名字）：运行时探测硬件、内核与系统版本用的只读信息项（ADR 0005 决策 2）。
#
# 逐项放行，不放行 ``kern.proc.*``（进程列表与进程信息）、``kern.procargs*``、``kern.bootargs``
# （启动参数）等；App Sandbox 也只对苹果签名的程序放行这几类。
#
# 注意：这挡不住沙盒读同一用户下其他进程的参数与环境。内核对 ``KERN_PROCARGS2`` 与按 pid 的
# ``KERN_PROC_PID`` 不走沙盒的 sysctl 检查，连 ``(deny default)`` 也拦不住（macOS 26.6.2 实测），
# 这是 macOS 本机后端的已知限制，见 ADR 0005 决策 2。
SYSCTL_READ_NAMES: tuple[str, ...] = (
    "kern.argmax",
    "kern.boottime",
    "kern.clockrate",
    "kern.hostname",
    "kern.hv_vmm_present",
    "kern.iossupportversion",
    "kern.maxfiles",
    "kern.maxfilesperproc",
    "kern.maxproc",
    "kern.ngroups",
    "kern.osproductversion",
    "kern.osrelease",
    "kern.ostype",
    "kern.osvariant_status",
    "kern.osversion",
    "kern.safeboot",
    "kern.secure_kernel",
    "kern.usrstack64",
    "kern.version",
    "machdep.ptrauth_enabled",
    "machdep.virtual_address_size",
    "security.mac.lockdown_mode_state",
    # 按名字查询要先把名字换成 OID（``sysctl`` 命令行等）
    "sysctl.name2oid",
    "sysctl.proc_cputype",
    "sysctl.proc_native",
    "sysctl.proc_translated",
    "vm.loadavg",
)

# 放行读取的 sysctl（名字前缀）：硬件信息、CPU 型号与特性；``sysctl`` 命令行按 OID 反查名字与类型
SYSCTL_READ_PREFIXES: tuple[str, ...] = ("hw.", "machdep.cpu.", "sysctl.name.", "sysctl.oidfmt.")


# 出网时额外放行读取的 sysctl（名字前缀）：网卡与地址列表（``getifaddrs``），codex 出网档同样放行
SYSCTL_READ_NETWORK_PREFIXES: tuple[str, ...] = ("net.routetable.",)


def _sysctl_rules(names: tuple[str, ...], prefixes: tuple[str, ...]) -> str:
    """逐项放行 sysctl 读取的 SBPL 规则。"""
    rules = [f'  (sysctl-name "{name}")' for name in names]
    rules += [f'  (sysctl-name-prefix "{prefix}")' for prefix in prefixes]
    return "(allow sysctl-read\n" + "\n".join(rules) + ")"


# 进程运行必需的基础能力：默认全拒，再逐项放开
_BASE_PROFILE = f"""\
(version 1)
(deny default)

; 子进程继承同一份配置
(allow process-exec)
(allow process-fork)
(allow signal (target same-sandbox))
(allow process-info* (target same-sandbox))

; 运行时探测硬件 / 内核信息：逐项放行，不含进程列表与启动参数
{_sysctl_rules(SYSCTL_READ_NAMES, SYSCTL_READ_PREFIXES)}

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

# DNS：系统解析器经这个 Unix 套接字找 mDNSResponder（/var/run 指向 /private/var/run）
_DNS_SOCKET = "/private/var/run/mDNSResponder"

_NETWORK_PROFILE = f"""\
; 出网：只放行 IP 远端（含本机回环）与 DNS 用的 mDNSResponder 套接字。
; 不加过滤的 network-outbound 会连带放开本机所有 Unix 套接字：Docker 守护进程（等于接管宿主）、
; ssh-agent、本地数据库都连得上。这里没放行的 Unix 套接字一律拒绝，包括绑定。
(allow network-outbound (remote ip))
(allow network-outbound (remote unix-socket (path-literal "{_DNS_SOCKET}")))
(allow network-inbound (local ip))
(allow system-socket)
{_sysctl_rules((), SYSCTL_READ_NETWORK_PREFIXES)}
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


__all__ = [
    "DEFAULT_SANDBOX_EXEC",
    "SYSCTL_READ_NAMES",
    "SYSCTL_READ_NETWORK_PREFIXES",
    "SYSCTL_READ_PREFIXES",
    "build_argv",
    "build_params",
    "build_profile",
]
