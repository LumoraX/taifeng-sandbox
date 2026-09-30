"""Docker 配置到命令行的翻译（纯计算，不需要 docker）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from taifeng_sandbox import SandboxPolicyError
from taifeng_sandbox.docker import DockerSandboxConfig, Mount
from taifeng_sandbox.docker.config import build_exec_argv, build_run_argv


def _value_after(argv: list[str], flag: str) -> list[str]:
    """取某个选项后面跟的所有取值。"""
    return [argv[i + 1] for i, item in enumerate(argv) if item == flag]


def test_hardening_flags_always_present() -> None:
    """加固参数不受配置影响，始终存在。"""
    argv = build_run_argv(DockerSandboxConfig(image="python:3.12-slim"), "box")
    assert _value_after(argv, "--cap-drop") == ["ALL"]
    assert _value_after(argv, "--security-opt") == ["no-new-privileges"]
    assert "--read-only" in argv
    assert "--init" in argv
    assert _value_after(argv, "--pids-limit") == ["256"]


def test_network_disabled_by_default() -> None:
    """默认断网；显式开启才不加 ``--network none``。"""
    closed = build_run_argv(DockerSandboxConfig(image="img"), "box")
    assert _value_after(closed, "--network") == ["none"]
    opened = build_run_argv(DockerSandboxConfig(image="img", network=True), "box")
    assert "--network" not in opened


def test_memory_limit_disables_swap() -> None:
    """内存上限同时钉死交换区。"""
    argv = build_run_argv(DockerSandboxConfig(image="img", memory_mb=512, cpus=0.5), "box")
    assert _value_after(argv, "--memory") == ["512m"]
    assert _value_after(argv, "--memory-swap") == ["512m"]
    assert _value_after(argv, "--cpus") == ["0.5"]


def test_workdir_is_tmpfs_without_host_dir() -> None:
    """没给宿主机目录时工作目录是 tmpfs。"""
    argv = build_run_argv(DockerSandboxConfig(image="img"), "box")
    assert any(entry.startswith("/workspace:rw") for entry in _value_after(argv, "--tmpfs"))
    assert "--mount" not in argv


def test_workspace_and_extra_mounts() -> None:
    """工作区可写挂载，额外挂载默认只读。"""
    config = DockerSandboxConfig(
        image="img",
        workspace_host_dir=Path("/host/ws"),
        mounts=(Mount(Path("/host/skills"), "/skills"),),
    )
    mounts = _value_after(build_run_argv(config, "box"), "--mount")
    assert mounts == [
        "type=bind,source=/host/ws,target=/workspace",
        "type=bind,source=/host/skills,target=/skills,readonly",
    ]


def test_image_and_keepalive_come_last() -> None:
    """镜像之后只有保活命令，选项不会被当成容器命令。"""
    argv = build_run_argv(DockerSandboxConfig(image="img", labels={"owner": "test"}), "box")
    at = argv.index("img")
    assert argv[at + 1 : at + 3] == ["python3", "-c"]
    assert len(argv) == at + 4
    assert _value_after(argv, "--label") == ["owner=test"]


def test_exec_argv_passes_source_and_root() -> None:
    """exec 命令行把守护进程源码与根目录传进去。"""
    argv = build_exec_argv(DockerSandboxConfig(image="img", workdir="/w"), "box", "print(1)")
    assert argv == [
        "docker", "exec", "--interactive", "box", "python3", "-u", "-c", "print(1)", "--root", "/w",
    ]


@pytest.mark.parametrize("name", ["--privileged", "", "a b", "x" * 200, "-box"])
def test_container_name_cannot_smuggle_options(name: str) -> None:
    """容器名必须是普通标识符，不能夹带选项。"""
    with pytest.raises(SandboxPolicyError):
        build_run_argv(DockerSandboxConfig(image="img"), name)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"image": ""},
        {"image": "--privileged"},
        {"image": "img", "workdir": "relative"},
        {"image": "img", "workspace_host_dir": Path("relative")},
        {"image": "img", "pids_limit": 0},
        {"image": "img", "memory_mb": 1},
        {"image": "img", "cpus": 0},
        {"image": "img", "labels": {"bad key": "v"}},
    ],
)
def test_invalid_config_rejected(kwargs: dict[str, object]) -> None:
    """不合法的配置在构造期就拒绝。"""
    with pytest.raises(SandboxPolicyError):
        DockerSandboxConfig(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("host", "container"),
    [("relative", "/x"), ("/host", "relative"), ("/ho,st", "/x"), ("/host", "/x,readonly=false")],
)
def test_invalid_mount_rejected(host: str, container: str) -> None:
    """挂载路径必须是绝对路径，且不能借逗号注入挂载选项。"""
    with pytest.raises(SandboxPolicyError):
        Mount(Path(host), container)
