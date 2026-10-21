"""启动取消必须等待命令结算，并回收命令子进程和已知容器。"""

from __future__ import annotations

import asyncio
import sys

import pytest

from taifeng_sandbox.docker import DockerEnvironment, DockerSandboxConfig
from taifeng_sandbox.docker import environment as module


async def test_cancelled_command_reaps_child(monkeypatch):
    """取消后不能留下仍在运行的命令行子进程。"""
    spawn = asyncio.create_subprocess_exec
    entered = asyncio.Event()
    children = []

    async def capture(*args, **kwargs):
        """保留真实子进程供断言。"""
        child = await spawn(*args, **kwargs)
        children.append(child)
        entered.set()
        return child

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", capture)
    pending = asyncio.create_task(module._run_docker(
        [sys.executable, "-c", "import time; time.sleep(60)"], env={}, timeout_seconds=60,
    ))
    await entered.wait()
    pending.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert children[0].returncode is not None
    finally:
        if children[0].returncode is None:
            children[0].kill()
            await children[0].wait()


async def test_cancelled_start_waits_before_removal(monkeypatch):
    """取消先等待已发送的创建请求结算，再按预分配名称删除。"""
    entered, release = asyncio.Event(), asyncio.Event()
    removed = []

    async def run(*args, **kwargs):
        """模拟已发送但尚未回执的启动。"""
        entered.set()
        await release.wait()
        return 0, "container-id", ""

    async def remove(config, name):
        """确认删除发生在启动结束后。"""
        assert release.is_set()
        removed.append(name)

    monkeypatch.setattr(module, "_run_docker", run)
    monkeypatch.setattr(module, "_remove_container", remove)
    pending = asyncio.create_task(DockerEnvironment.create(
        DockerSandboxConfig(image="python:3.12-slim"), name="cancelled-start",
    ))
    await entered.wait()
    pending.cancel()
    await asyncio.sleep(0)
    assert not pending.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert removed == ["cancelled-start"]
