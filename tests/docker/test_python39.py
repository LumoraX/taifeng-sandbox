"""守护进程在 Python 3.9 镜像里真跑：握手、流式标准输入、原子写、删符号链接本身。

守护进程要注入任意镜像单文件运行，语法与行为须兼容 3.9（``server.py`` 的 ruff 配置不套用 UP
规则）。3.9 的 asyncio 给子进程标准输入接的是 socketpair 而不是管道，流式往返在这里实跑一遍。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from taifeng import CommandSpec

from taifeng_sandbox.daemon import StreamingRemoteProcess, protocol
from taifeng_sandbox.docker import DockerEnvironment
from tests.conftest import requires_docker
from tests.docker.conftest import ENV, run_shell, sandbox_config

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

pytestmark = requires_docker

IMAGE_PY39 = "python:3.9-slim"


@pytest.fixture
async def sandbox39() -> AsyncIterator[DockerEnvironment]:
    """Python 3.9 镜像里的容器沙盒。"""
    async with await DockerEnvironment.create(sandbox_config(image=IMAGE_PY39)) as environment:
        yield environment


async def test_handshake_reports_python_39(sandbox39: DockerEnvironment) -> None:
    """握手上报的解释器是 3.9，协议版本是 2。"""
    info = sandbox39.client.server_info
    assert str(info["pythonVersion"]).startswith("3.9.")
    assert info["protocolVersion"] == protocol.PROTOCOL_VERSION == 2


async def test_streaming_stdin_round_trip(sandbox39: DockerEnvironment) -> None:
    """``cat`` 经流式标准输入逐行往返；关闭后读到剩余输出、正常退出。"""
    proc = await sandbox39.executor().start(
        CommandSpec(command="cat", shell=False, cwd=None, env=dict(ENV), stdin=True)
    )
    assert isinstance(proc, StreamingRemoteProcess)
    assert proc.stdin is not None and proc.stdout is not None
    for line in (b"hello\n", b"world\n"):
        proc.stdin.write(line)
        await asyncio.wait_for(proc.stdin.drain(), 30)
        assert await asyncio.wait_for(proc.stdout.readline(), 30) == line
    proc.stdin.write(b"tail")
    assert await asyncio.wait_for(proc.communicate(), 30) == (b"tail", b"")
    assert proc.returncode == 0


async def test_write_bytes_replaces_atomically(sandbox39: DockerEnvironment) -> None:
    """``write_bytes`` 覆盖是原子替换：内容换新、权限位保持、不留临时文件。"""
    workspace = sandbox39.workspace()
    await workspace.write_bytes("run.sh", b"echo old\n")
    code, _, _ = await run_shell(sandbox39.executor(), "chmod 750 run.sh")
    assert code == 0
    await workspace.write_bytes("run.sh", b"echo new\n")
    code, out, _ = await run_shell(sandbox39.executor(), "stat -c '%a' run.sh; ls -A")
    assert code == 0
    assert out.split() == ["750", "run.sh"]
    assert await workspace.read_bytes("run.sh") == b"echo new\n"


async def test_remove_deletes_the_symlink_itself(sandbox39: DockerEnvironment) -> None:
    """``remove`` 删符号链接本身，不跟随到目标：指向文件、指向目录（加 recursive）都一样。"""
    workspace = sandbox39.workspace()
    await workspace.write_text("target.txt", "keep")
    await workspace.write_text("dir/inner.txt", "keep too")
    code, _, err = await run_shell(
        sandbox39.executor(), "ln -s target.txt link && ln -s dir dirlink"
    )
    assert code == 0, err
    await workspace.remove("link")
    await workspace.remove("dirlink", recursive=True)
    names = [entry.name for entry in await workspace.list_directory(workspace.root)]
    assert names == ["dir", "target.txt"]
    assert await workspace.read_text("target.txt") == "keep"
    assert await workspace.read_text("dir/inner.txt") == "keep too"
