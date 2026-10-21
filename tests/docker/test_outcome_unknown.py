"""Docker命令超时只能证明本地子进程退出，不能证明服务端未执行。"""

from __future__ import annotations

import sys

import pytest

from taifeng_sandbox import SandboxOperationOutcomeUnknownError
from taifeng_sandbox.docker.environment import _run_docker


async def test_command_timeout_preserves_unknown_outcome():
    """运行超时明确保留未知分类，宿主不得据此判定容器没有迟到创建。"""
    with pytest.raises(SandboxOperationOutcomeUnknownError):
        await _run_docker([sys.executable, '-c', 'import time; time.sleep(60)'], env={}, timeout_seconds=0.05)
