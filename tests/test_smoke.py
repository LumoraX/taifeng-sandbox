"""工程骨架冒烟测试：版本来源正确，所依赖的 taifeng 协议在稳定层可见。"""

from __future__ import annotations

from importlib.metadata import version

import taifeng

import taifeng_sandbox


def test_version_from_metadata_matches_distribution() -> None:
    """``__version__`` 必须等于发行元数据版本（pyproject 是唯一来源）。"""
    assert taifeng_sandbox.__version__ == version("taifeng-sandbox")


def test_stable_api_executor_protocols_exported() -> None:
    """本包要实现的执行类协议必须在 taifeng 稳定层 ``__all__`` 中。

    红线：适配包只依赖稳定层（ADR 0001 决策 3）。taifeng 若把它们移出 ``__all__``，
    这里先红，而不是等到实现代码 import 失败才发现。
    """
    required = {"CommandExecutor", "CommandProcess", "CommandSpec", "ScriptExecutor"}
    assert required <= set(taifeng.__all__)
