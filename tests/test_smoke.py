"""工程骨架冒烟测试：版本来源正确，所依赖的 taifeng 协议在稳定层可见。"""

from __future__ import annotations

from importlib.metadata import version

import pytest
import taifeng

import taifeng_sandbox


def test_version_from_metadata_matches_distribution() -> None:
    """``__version__`` 必须等于发行元数据版本（pyproject 是唯一来源）。"""
    assert taifeng_sandbox.__version__ == version("taifeng-sandbox")


# CommandExecutor（taifeng ADR 0051）合入 taifeng main 晚于 PyPI 最新版 2026.9.28.16。
# strict=True：taifeng 发版、锁文件升级后本用例转为通过，xfail 会反过来报红，
# 逼着同时摘掉本标记并把 pyproject 的 taifeng 下限提到含 ADR 0051 的版本。
@pytest.mark.xfail(strict=True, reason="等 taifeng 发布含 CommandExecutor（ADR 0051）的版本")
def test_stable_api_executor_protocols_exported() -> None:
    """本包要实现的执行类协议必须在 taifeng 稳定层 ``__all__`` 中。

    红线：适配包只依赖稳定层（ADR 0001 决策 3）。taifeng 若把它们移出 ``__all__``，
    这里先红，而不是等到实现代码 import 失败才发现。
    """
    required = {"CommandExecutor", "CommandProcess", "CommandSpec", "ScriptExecutor"}
    assert required <= set(taifeng.__all__)
