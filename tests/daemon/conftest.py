"""守护进程测试夹具：在本机把守护进程作为子进程真的跑起来。

启动方式与容器后端一致——源码经 ``python -c`` 注入，而不是 import 本包——
这样同时验证了「守护进程能单文件运行」。
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import pytest

from taifeng_sandbox.daemon import DaemonClient, StdioTransport, daemon_source

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


def daemon_argv(root: Path) -> list[str]:
    """本机启动守护进程的命令行。"""
    return [sys.executable, "-u", "-c", daemon_source(), "--root", str(root)]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """守护进程的根目录。"""
    directory = tmp_path / "root"
    directory.mkdir()
    return directory.resolve()


@pytest.fixture
async def client(root: Path) -> AsyncIterator[DaemonClient]:
    """已握手的守护进程连接。"""
    transport = await StdioTransport.spawn(daemon_argv(root))
    connected = await DaemonClient.connect(transport)
    try:
        yield connected
    finally:
        await connected.close()
