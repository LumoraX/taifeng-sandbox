"""Docker 环境的配置，以及到 ``docker run`` 参数的翻译。

加固参数参照 hermes-agent ``tools/environments/docker.py`` 与 openclaw
``src/agents/sandbox/docker.ts`` 的共同做法：去掉全部 capability、禁止提权、限制进程数、
只读根文件系统加 tmpfs。差异：默认断网；不含任何按会话 / 按用户的目录约定，挂载完全由
调用方给出（ADR 0001 决策 4）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from taifeng_sandbox.errors import SandboxPolicyError

if TYPE_CHECKING:
    from collections.abc import Mapping

_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
_LABEL_KEY_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")

# 容器主进程：只负责让容器活着，真正的工作由 exec 进去的守护进程完成
KEEPALIVE_PROGRAM = "import signal\nwhile True:\n    signal.pause()\n"


@dataclass(frozen=True)
class Mount:
    """一个绑定挂载。

    Attributes:
        host_path: 宿主机路径（绝对路径）。
        container_path: 容器内路径（绝对路径）。
        read_only: 是否只读挂载。默认只读。
    """

    host_path: Path
    container_path: str
    read_only: bool = True

    def __post_init__(self) -> None:
        """校验两端都是绝对路径，且不含会破坏参数解析的字符。"""
        if not self.host_path.is_absolute():
            raise SandboxPolicyError(f"挂载的宿主机路径必须是绝对路径：{str(self.host_path)!r}")
        if not PurePosixPath(self.container_path).is_absolute():
            raise SandboxPolicyError(f"挂载的容器内路径必须是绝对路径：{self.container_path!r}")
        for value in (str(self.host_path), self.container_path):
            if "," in value or "\n" in value:
                raise SandboxPolicyError(f"挂载路径不能包含逗号或换行：{value!r}")

    def to_argument(self) -> str:
        """``--mount`` 参数值。"""
        parts = ["type=bind", f"source={self.host_path}", f"target={self.container_path}"]
        if self.read_only:
            parts.append("readonly")
        return ",".join(parts)


@dataclass(frozen=True)
class DockerSandboxConfig:
    """一个容器沙盒的配置。

    Attributes:
        image: 镜像。镜像里必须有 ``python``（3.9+），守护进程靠它运行。
        workdir: 容器内工作目录，也是守护进程的根目录。
        workspace_host_dir: 挂到 ``workdir`` 的宿主机目录（可写）；None 则 ``workdir`` 是
            容器内的 tmpfs，容器销毁即丢失。
        mounts: 额外的绑定挂载（如只读挂载 skill 目录）。
        network: 是否允许出网。默认不允许。
        memory_mb: 内存上限（MiB）；None 不限制。
        cpus: CPU 配额；None 不限制。
        pids_limit: 进程数上限。
        read_only_rootfs: 根文件系统是否只读。
        tmpfs: 额外的 tmpfs 挂载（``路径:选项``）。
        workdir_tmpfs_size: ``workdir`` 为 tmpfs 时的大小。
        user: 容器内运行身份（``uid[:gid]``）；None 用镜像默认。
        python: 容器内 Python 解释器。
        docker_binary: 宿主机上的 docker 命令。
        docker_env: 运行 docker 命令时的环境变量（docker 客户端需要 ``HOME`` 或
            ``DOCKER_HOST`` 才能找到守护进程）；不继承宿主环境。
        labels: 容器标签，便于调用方事后清理。
        startup_timeout_seconds: 拉起容器的时限（含首次拉取镜像）。
    """

    image: str
    workdir: str = "/workspace"
    workspace_host_dir: Path | None = None
    mounts: tuple[Mount, ...] = ()
    network: bool = False
    memory_mb: int | None = 1024
    cpus: float | None = 1.0
    pids_limit: int = 256
    read_only_rootfs: bool = True
    tmpfs: tuple[str, ...] = ("/tmp:rw,nosuid,size=256m",)  # noqa: S108 —— 容器内路径
    workdir_tmpfs_size: str = "1g"
    user: str | None = None
    python: str = "python3"
    docker_binary: str = "docker"
    docker_env: Mapping[str, str] = field(default_factory=dict)
    labels: Mapping[str, str] = field(default_factory=dict)
    startup_timeout_seconds: float = 300.0

    def __post_init__(self) -> None:
        """构造期校验。"""
        if not self.image or self.image.startswith("-"):
            raise SandboxPolicyError(f"镜像名非法：{self.image!r}")
        if not PurePosixPath(self.workdir).is_absolute():
            raise SandboxPolicyError(f"workdir 必须是绝对路径：{self.workdir!r}")
        if self.workspace_host_dir is not None and not self.workspace_host_dir.is_absolute():
            raise SandboxPolicyError("workspace_host_dir 必须是绝对路径")
        if self.pids_limit < 1:
            raise SandboxPolicyError("pids_limit 必须为正数")
        if self.memory_mb is not None and self.memory_mb < 16:
            raise SandboxPolicyError("memory_mb 过小（至少 16）")
        if self.cpus is not None and self.cpus <= 0:
            raise SandboxPolicyError("cpus 必须为正数")
        for key in self.labels:
            if not _LABEL_KEY_PATTERN.match(key):
                raise SandboxPolicyError(f"标签名非法：{key!r}")


def validate_container_name(name: str) -> str:
    """校验容器名，防止把选项当成名字传给 docker。"""
    if not _NAME_PATTERN.match(name):
        raise SandboxPolicyError(f"容器名非法：{name!r}")
    return name


def _resource_args(config: DockerSandboxConfig) -> list[str]:
    """资源限制参数。"""
    args = ["--pids-limit", str(config.pids_limit)]
    if config.memory_mb is not None:
        # 交换区与内存同值 = 不允许使用交换区
        args.extend(["--memory", f"{config.memory_mb}m", "--memory-swap", f"{config.memory_mb}m"])
    if config.cpus is not None:
        args.extend(["--cpus", str(config.cpus)])
    return args


def _filesystem_args(config: DockerSandboxConfig) -> list[str]:
    """文件系统相关参数：只读根、tmpfs、工作目录、额外挂载。"""
    args: list[str] = []
    if config.read_only_rootfs:
        args.append("--read-only")
    for entry in config.tmpfs:
        args.extend(["--tmpfs", entry])
    if config.workspace_host_dir is not None:
        workspace = Mount(config.workspace_host_dir, config.workdir, read_only=False)
        args.extend(["--mount", workspace.to_argument()])
    else:
        options = f"rw,exec,nosuid,size={config.workdir_tmpfs_size}"
        args.extend(["--tmpfs", f"{config.workdir}:{options}"])
    for mount in config.mounts:
        args.extend(["--mount", mount.to_argument()])
    return args


def build_run_argv(config: DockerSandboxConfig, name: str) -> list[str]:
    """拼出 ``docker run`` 命令行。纯计算，便于单测。"""
    argv = [
        config.docker_binary,
        "run",
        "--detach",
        "--rm",
        "--init",
        "--name",
        validate_container_name(name),
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--workdir",
        config.workdir,
    ]
    if not config.network:
        argv.extend(["--network", "none"])
    argv.extend(_resource_args(config))
    argv.extend(_filesystem_args(config))
    if config.user is not None:
        argv.extend(["--user", config.user])
    for key, value in sorted(config.labels.items()):
        argv.extend(["--label", f"{key}={value}"])
    argv.extend([config.image, config.python, "-c", KEEPALIVE_PROGRAM])
    return argv


def build_exec_argv(config: DockerSandboxConfig, name: str, daemon_source: str) -> list[str]:
    """拼出在容器里启动守护进程的 ``docker exec`` 命令行。"""
    return [
        config.docker_binary,
        "exec",
        "--interactive",
        validate_container_name(name),
        config.python,
        "-u",
        "-c",
        daemon_source,
        "--root",
        config.workdir,
    ]


__all__ = [
    "KEEPALIVE_PROGRAM",
    "DockerSandboxConfig",
    "Mount",
    "build_exec_argv",
    "build_run_argv",
    "validate_container_name",
]
