"""seatbelt 配置与命令行的纯计算测试（任何平台都能跑）。"""

from __future__ import annotations

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
    assert "(allow network-outbound)" in enabled


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
