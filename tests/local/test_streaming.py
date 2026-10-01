"""本机后端的流式进程：持续写 stdin、按行读 stdout；按组杀进程。"""

from __future__ import annotations

import asyncio
import shlex
import sys
from typing import TYPE_CHECKING

import pytest
import taifeng

from taifeng_sandbox import SandboxPolicy
from taifeng_sandbox.daemon.process import DEFAULT_MAX_BUFFER_BYTES
from taifeng_sandbox.local import create_local_executor
from taifeng_sandbox.local.process import STREAM_LIMIT_BYTES

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.skipif(
    sys.platform not in ("darwin", "linux"), reason="只在有本机隔离后端的平台上跑"
)

# 用跑测试的解释器：系统里不一定有 /usr/bin/python3（如 python:3.12-slim 镜像只有 /usr/local/bin）
PYTHON = shlex.quote(sys.executable)

ECHO = "import sys\nfor line in sys.stdin:\n    sys.stdout.write('got:' + line)\n    sys.stdout.flush()\n"


async def test_stdin_and_stdout_stream(tmp_path: Path) -> None:
    """stdin=True 时可以一行一行地对话。"""
    script = tmp_path / "echo.py"
    script.write_text(ECHO)
    executor = create_local_executor(SandboxPolicy.workspace_write(tmp_path))
    proc = await executor.start(
        taifeng.CommandSpec(
            command=f"{PYTHON} {script}", shell=False, cwd=str(tmp_path),
            env={"PATH": "/usr/bin:/bin"}, stdin=True,
        )
    )
    assert isinstance(proc, taifeng.StreamingCommandProcess)
    assert proc.stdin is not None and proc.stdout is not None
    for word in ("a", "b"):
        proc.stdin.write(f"{word}\n".encode())
        await proc.stdin.drain()
        assert await asyncio.wait_for(proc.stdout.readline(), 10) == f"got:{word}\n".encode()
    proc.stdin.close()
    assert await asyncio.wait_for(proc.wait(), 10) == 0


def test_stream_limit_matches_the_daemon_executor_buffer() -> None:
    """本机流的单行上限与守护进程执行器每路缓冲的默认上限一致：两类后端能交出的单行一样长。"""
    assert STREAM_LIMIT_BYTES == DEFAULT_MAX_BUFFER_BYTES == 16 * 1024 * 1024


async def test_readline_reads_a_line_longer_than_the_asyncio_default(tmp_path: Path) -> None:
    """一次输出 200 KiB 的单行：readline 读到完整一行，不因 asyncio 默认的 64 KiB 上限抛错。

    经本机执行器起的 MCP 连接器，一条大响应就是这样的一行；超限时 readline 抛 ValueError，
    内核的读循环随之崩掉。
    """
    script = tmp_path / "big.py"
    script.write_text("import sys\nsys.stdout.write('x' * 204800 + '\\n')\nsys.stdout.flush()\n")
    executor = create_local_executor(SandboxPolicy.workspace_write(tmp_path))
    proc = await executor.start(
        taifeng.CommandSpec(
            command=f"{PYTHON} {script}", shell=False, cwd=str(tmp_path),
            env={"PATH": "/usr/bin:/bin"}, stdin=True,
        )
    )
    assert isinstance(proc, taifeng.StreamingCommandProcess)
    assert proc.stdin is not None and proc.stdout is not None
    line = await asyncio.wait_for(proc.stdout.readline(), 10)
    assert line == b"x" * 204800 + b"\n"
    proc.stdin.close()
    assert await asyncio.wait_for(proc.wait(), 10) == 0


async def test_without_stdin_flag_stdin_is_none(tmp_path: Path) -> None:
    """不要 stdin 的命令读到 EOF，stdin 属性为 None。"""
    executor = create_local_executor(SandboxPolicy.workspace_write(tmp_path))
    proc = await executor.start(
        taifeng.CommandSpec(command="cat", shell=True, cwd=str(tmp_path), env={"PATH": "/usr/bin:/bin"})
    )
    assert getattr(proc, "stdin", None) is None
    assert await asyncio.wait_for(proc.communicate(), 10) == (b"", b"")


async def test_kill_reaches_children_after_the_shell_exits(tmp_path: Path) -> None:
    """shell 先退出、它派生的子进程还占着管道：kill 仍按组杀到（内核 ADR 0108）。"""
    executor = create_local_executor(SandboxPolicy.workspace_write(tmp_path))
    proc = await executor.start(
        taifeng.CommandSpec(
            command="sleep 30 & echo started", shell=True, cwd=str(tmp_path),
            env={"PATH": "/usr/bin:/bin"},
        )
    )
    # shell 已退出，sleep 还在。asyncio 的 wait() 要等到管道关闭才返回，这里改为轮询 returncode
    async with asyncio.timeout(10):
        while proc.returncode is None:
            await asyncio.sleep(0.05)
    proc.kill()
    out, _ = await asyncio.wait_for(proc.communicate(), 10)  # sleep 被杀，管道关闭
    assert out == b"started\n"
