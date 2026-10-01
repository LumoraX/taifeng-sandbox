"""Docker 环境提供者：拉起容器，在里面启动守护进程（ADR 0002 决策 2）。

本模块只经 docker 命令行操作，不依赖 Docker SDK：核心包因此保持零额外运行时依赖，
``docker`` extra 只是声明「需要宿主机上有 docker 命令」。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
from typing import TYPE_CHECKING, Self

from taifeng_sandbox.daemon.client import DaemonClient, daemon_source
from taifeng_sandbox.daemon.executor import DaemonCommandExecutor
from taifeng_sandbox.daemon.transport import StdioTransport
from taifeng_sandbox.daemon.workspace import DaemonWorkspace
from taifeng_sandbox.docker.config import (
    DockerSandboxConfig,
    build_exec_argv,
    build_run_argv,
    validate_container_name,
)
from taifeng_sandbox.errors import (
    SandboxError,
    SandboxProtocolError,
    SandboxUnavailableError,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from types import TracebackType

logger = logging.getLogger(__name__)

_REMOVE_TIMEOUT_SECONDS = 30.0


async def _run_docker(
    argv: Sequence[str], *, env: dict[str, str], timeout_seconds: float
) -> tuple[int, str, str]:
    """运行一条 docker 命令并等待结束，返回 ``(退出码, stdout, stderr)``。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except OSError as exc:
        raise SandboxUnavailableError(f"无法运行 docker 命令：{exc}") from exc
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except TimeoutError as exc:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        raise SandboxUnavailableError(f"docker 命令超时（{timeout_seconds:.0f}s）") from exc
    assert proc.returncode is not None
    return proc.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")


class DockerEnvironment:
    """一个正在运行的容器沙盒。

    用法::

        async with await DockerEnvironment.create(config) as sandbox:
            executor = sandbox.executor()          # 交给 taifeng 工具 / 脚本执行器
            await sandbox.workspace().write_text("input.txt", "...")
    """

    def __init__(self, config: DockerSandboxConfig, name: str, client: DaemonClient) -> None:
        """由 ``create`` 调用；不要直接构造。"""
        self._config = config
        self._name = name
        self._client = client
        self._closed = False

    @classmethod
    async def create(cls, config: DockerSandboxConfig, *, name: str | None = None) -> Self:
        """拉起容器并连上其中的守护进程。任何一步失败都会清理已创建的容器。

        Raises:
            SandboxUnavailableError: docker 不可用、镜像拉不到、容器起不来，或容器里的
                守护进程连不上（如镜像里没有 Python）。
        """
        container = validate_container_name(name or "tfsb-" + secrets.token_hex(8))
        env = dict(config.docker_env)
        code, _, stderr = await _run_docker(
            build_run_argv(config, container),
            env=env,
            timeout_seconds=config.startup_timeout_seconds,
        )
        if code != 0:
            raise SandboxUnavailableError(f"容器启动失败：{stderr.strip()}")
        try:
            transport = await StdioTransport.spawn(
                build_exec_argv(config, container, daemon_source()), env=env
            )
            client = await DaemonClient.connect(transport)
        except SandboxProtocolError as exc:
            await _remove_container(config, container)
            raise SandboxUnavailableError(f"容器内守护进程连不上：{exc}") from exc
        except BaseException:
            await _remove_container(config, container)
            raise
        return cls(config, container, client)

    @property
    def name(self) -> str:
        """容器名。"""
        return self._name

    @property
    def config(self) -> DockerSandboxConfig:
        """创建时使用的配置。"""
        return self._config

    @property
    def client(self) -> DaemonClient:
        """到容器内守护进程的连接。"""
        return self._client

    def executor(self) -> DaemonCommandExecutor:
        """在容器里执行命令的 ``CommandExecutor``。"""
        return DaemonCommandExecutor(self._client, default_cwd=self._config.workdir)

    def workspace(self) -> DaemonWorkspace:
        """容器工作目录的文件视图（``taifeng.WorkspaceFS``），可交给文件类工具。"""
        return DaemonWorkspace(self._client)

    async def close(self) -> None:
        """断开守护进程并销毁容器。可重复调用。"""
        if self._closed:
            return
        self._closed = True
        try:
            await self._client.close()
        finally:
            await _remove_container(self._config, self._name)

    async def __aenter__(self) -> Self:
        """进入上下文。"""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """退出上下文时销毁容器。"""
        await self.close()


async def _remove_container(config: DockerSandboxConfig, name: str) -> None:
    """强制删除容器。删除失败要报出来，否则会悄悄留下占资源的容器。"""
    code, _, stderr = await _run_docker(
        [config.docker_binary, "rm", "--force", "--volumes", name],
        env=dict(config.docker_env),
        timeout_seconds=_REMOVE_TIMEOUT_SECONDS,
    )
    if code != 0 and "No such container" not in stderr:
        raise SandboxError(f"容器 {name} 清理失败：{stderr.strip()}")


__all__ = ["DockerEnvironment"]
