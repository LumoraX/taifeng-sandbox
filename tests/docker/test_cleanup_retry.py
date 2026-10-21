"""删除容器失败时保留重试责任，不把关闭标志当作退出证明。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from taifeng_sandbox import SandboxError
from taifeng_sandbox.docker import DockerEnvironment, DockerSandboxConfig
from taifeng_sandbox.docker import environment as module


async def test_close_retries_failed_removal_and_only_then_becomes_idempotent(monkeypatch):
    """第一次rm失败后，第二次close仍必须真正删除。"""
    attempts = []

    async def remove(config, name):
        """第一次返回故障，其后完成删除。"""
        attempts.append(name)
        if len(attempts) == 1:
            raise SandboxError("docker rm failed")

    async def close():
        """daemon连接已关闭也不代表容器删除成功。"""

    monkeypatch.setattr(module, "_remove_container", remove)
    sandbox = DockerEnvironment(DockerSandboxConfig(image="python:3.12-slim"), "owned-container",
                                 SimpleNamespace(close=close))
    with pytest.raises(SandboxError):
        await sandbox.close()
    await sandbox.close()
    await sandbox.close()
    assert attempts == ["owned-container", "owned-container"]


async def test_known_name_can_be_removed_after_startup_did_not_return_a_handle(monkeypatch):
    """宿主预分配名称，启动出错后仍可回收那个确定资源。"""
    attempts = []

    async def remove(config, name):
        """只记录明确名称，不扫描或删除无关容器。"""
        attempts.append(name)

    monkeypatch.setattr(module, "_remove_container", remove)
    config = DockerSandboxConfig(image="python:3.12-slim")
    await DockerEnvironment.remove(config, "known-container")
    assert attempts == ["known-container"]
