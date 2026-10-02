"""Docker 用例的公共配置：docker 客户端的最小环境、测试用的沙盒配置。"""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING

from taifeng import CommandSpec

from taifeng_sandbox.docker import DockerSandboxConfig

if TYPE_CHECKING:
    from taifeng import CommandExecutor

IMAGE = "python:3.12-slim"
ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"}
LABELS = {"taifeng-sandbox.test": "1"}


def docker_env() -> dict[str, str]:
    """docker 客户端找到守护进程所需的最小环境。"""
    env = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"]}
    if "DOCKER_HOST" in os.environ:
        env["DOCKER_HOST"] = os.environ["DOCKER_HOST"]
    return env


def sandbox_config(**overrides: object) -> DockerSandboxConfig:
    """测试用配置：默认镜像、打测试标签、内存 256 MiB，其余按 ``overrides`` 覆盖。"""
    base: dict[str, object] = {
        "image": IMAGE,
        "docker_env": docker_env(),
        "labels": LABELS,
        "memory_mb": 256,
    }
    base.update(overrides)
    return DockerSandboxConfig(**base)  # type: ignore[arg-type]


async def run_shell(executor: CommandExecutor, command: str) -> tuple[int, str, str]:
    """在沙盒里跑一条 shell 命令，返回 ``(退出码, stdout, stderr)``。"""
    proc = await executor.start(CommandSpec(command=command, shell=True, cwd=None, env=dict(ENV)))
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
    assert proc.returncode is not None
    return proc.returncode, stdout.decode(), stderr.decode()
