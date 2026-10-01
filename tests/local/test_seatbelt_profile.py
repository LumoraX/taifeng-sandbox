"""seatbelt 配置与命令行的纯计算测试（任何平台都能跑）。"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from taifeng_sandbox import SandboxPolicy
from taifeng_sandbox.local import seatbelt


def test_profile_starts_closed_by_default() -> None:
    """配置以默认全拒开头。"""
    profile = seatbelt.build_profile(SandboxPolicy())
    assert profile.startswith("(version 1)\n(deny default)")


def test_read_only_policy_has_no_write_or_network_rule() -> None:
    """只读策略不出现任何写根或出网放行。"""
    profile = seatbelt.build_profile(SandboxPolicy.read_only())
    assert "(allow file-read*)" in profile
    assert "WRITABLE_ROOT" not in profile
    assert "network-outbound" not in profile


def test_writable_roots_are_parameterized() -> None:
    """可写根经参数传入，路径本身不进配置文本（避免转义问题）。"""
    policy = SandboxPolicy.workspace_write(Path('/work/with "quote'))
    profile = seatbelt.build_profile(policy)
    assert '(subpath (param "WRITABLE_ROOT_0"))' in profile
    assert "quote" not in profile
    assert seatbelt.build_params(policy) == [("WRITABLE_ROOT_0", '/work/with "quote')]


def test_restricted_read_omits_global_read() -> None:
    """受限读不放开全盘读，只放系统目录与指定根。"""
    policy = SandboxPolicy.workspace_only(Path("/work"), readable_roots=(Path("/data"),))
    profile = seatbelt.build_profile(policy)
    assert "(allow file-read*)" not in profile
    assert '(subpath "/usr")' in profile
    assert '(allow file-read* (subpath (param "READABLE_ROOT_0")))' in profile
    assert ("READABLE_ROOT_0", "/data") in seatbelt.build_params(policy)


def test_deny_rules_come_last() -> None:
    """不可读目录的 deny 必须排在所有 allow 之后才能生效。"""
    policy = SandboxPolicy.workspace_write(
        Path("/work"), unreadable_roots=(Path("/secret"),), network=True
    )
    profile = seatbelt.build_profile(policy)
    deny_at = profile.index("(deny file-read* file-write*")
    assert deny_at > profile.rindex("(allow ")


def test_network_rules_only_when_enabled() -> None:
    """出网开关控制网络规则是否出现。"""
    enabled = seatbelt.build_profile(SandboxPolicy(network=True))
    assert "(allow network-outbound (remote ip))" in enabled
    disabled = seatbelt.build_profile(SandboxPolicy())
    assert "network-outbound" not in disabled
    assert "network-inbound" not in disabled


def test_network_outbound_only_ip_and_dns_socket() -> None:
    """出网只放行 IP 远端与 DNS 用的 mDNSResponder 套接字，其余 Unix 套接字不放行。"""
    profile = seatbelt.build_profile(SandboxPolicy(network=True))
    lines = [line.strip() for line in profile.splitlines()]
    assert "(allow network-outbound)" not in lines
    assert "(allow network-inbound)" not in lines
    assert "(allow network*)" not in lines
    assert [line for line in lines if "unix-socket" in line] == [
        '(allow network-outbound (remote unix-socket (path-literal "/private/var/run/mDNSResponder")))'
    ]
    assert "(allow network-inbound (local ip))" in lines


# 读其他进程的信息、参数与环境，或读启动参数的 sysctl：一项都不能落进白名单
_FORBIDDEN_SYSCTLS = (
    "kern.procargs",
    "kern.procargs2",
    "kern.proc.pid.1",
    "kern.proc.all",
    "kern.proc.pgrp.1",
    "kern.proc.uid.501",
    "kern.bootargs",
)


@pytest.mark.parametrize("name", _FORBIDDEN_SYSCTLS)
def test_sysctl_allowlist_excludes_process_and_boot_args(name: str) -> None:
    """白名单的精确名字与前缀都覆盖不到读其他进程参数、环境的项。"""
    assert name not in seatbelt.SYSCTL_READ_NAMES
    prefixes = seatbelt.SYSCTL_READ_PREFIXES + seatbelt.SYSCTL_READ_NETWORK_PREFIXES
    assert [prefix for prefix in prefixes if name.startswith(prefix)] == []


@pytest.mark.parametrize("network", [False, True])
def test_sysctl_read_is_allowlisted(network: bool) -> None:
    """配置里只有逐项放行的 sysctl 读取，没有整体放开，也没有写；网卡列表只在出网时放行。"""
    profile = seatbelt.build_profile(SandboxPolicy(network=network))
    lines = [line.strip() for line in profile.splitlines()]
    assert "(allow sysctl-read)" not in lines
    assert "sysctl-write" not in profile
    assert "sysctl-name-regex" not in profile
    rules = re.findall(r'\((sysctl-name(?:-prefix)?) "([^"]+)"\)', profile)
    prefixes = seatbelt.SYSCTL_READ_PREFIXES
    if network:
        prefixes += seatbelt.SYSCTL_READ_NETWORK_PREFIXES
    assert sorted(rules) == sorted(
        [("sysctl-name", name) for name in seatbelt.SYSCTL_READ_NAMES]
        + [("sysctl-name-prefix", prefix) for prefix in prefixes]
    )
    assert "kern.osversion" in seatbelt.SYSCTL_READ_NAMES
    assert "hw." in seatbelt.SYSCTL_READ_PREFIXES


def test_argv_layout() -> None:
    """命令行：sandbox-exec -p 配置 -D参数 -- 命令。"""
    policy = SandboxPolicy.workspace_write(Path("/work"))
    argv = seatbelt.build_argv(policy, ["/bin/echo", "hi"])
    assert argv[0] == seatbelt.DEFAULT_SANDBOX_EXEC
    assert argv[1] == "-p"
    assert argv[3] == "-DWRITABLE_ROOT_0=/work"
    assert argv[-3:] == ["--", "/bin/echo", "hi"]


def test_empty_command_rejected() -> None:
    """空命令直接拒绝。"""
    with pytest.raises(ValueError, match="command"):
        seatbelt.build_argv(SandboxPolicy(), [])
