"""Docker 环境的真实测试：真的拉起容器、在里面跑守护进程。"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest
from taifeng import CancellationToken, ScriptDescriptor, ScriptInvocation

from taifeng_sandbox import SandboxUnavailableError, shell_script_executor
from taifeng_sandbox.docker import DockerEnvironment, Mount
from tests.conftest import requires_docker
from tests.docker.conftest import ENV, docker_env, run_shell, sandbox_config

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

pytestmark = requires_docker


def _container_exists(name: str) -> bool:
    """容器是否还在。"""
    probe = subprocess.run(  # noqa: S603
        ["docker", "ps", "--all", "--quiet", "--filter", f"name=^{name}$"],  # noqa: S607
        capture_output=True,
        env=docker_env(),
        timeout=30,
        check=True,
    )
    return bool(probe.stdout.strip())


@pytest.fixture
async def sandbox() -> AsyncIterator[DockerEnvironment]:
    """默认配置的容器沙盒。"""
    async with await DockerEnvironment.create(sandbox_config()) as environment:
        yield environment


async def test_command_runs_inside_container(sandbox: DockerEnvironment) -> None:
    """命令确实在容器里执行：是 Linux，工作目录是容器内的工作区。"""
    code, out, _ = await run_shell(sandbox.executor(), "uname -s; pwd")
    assert code == 0
    assert out.split() == ["Linux", "/workspace"]


async def test_root_filesystem_is_read_only(sandbox: DockerEnvironment) -> None:
    """根文件系统只读，工作区与 /tmp 可写。"""
    executor = sandbox.executor()
    code, _, err = await run_shell(executor, "echo x > /usr/evil")
    assert code != 0
    assert "Read-only file system" in err
    code, out, _ = await run_shell(executor, "echo ok > /workspace/a && cat /workspace/a")
    assert (code, out.strip()) == (0, "ok")
    code, _, _ = await run_shell(executor, "echo ok > /tmp/b")
    assert code == 0


async def test_network_is_cut(sandbox: DockerEnvironment) -> None:
    """默认断网：容器里只有回环接口，连不出去。"""
    probe = (
        "python3 -c \"import socket; "
        "socket.create_connection(('1.1.1.1', 53), timeout=3)\""
    )
    code, _, err = await run_shell(sandbox.executor(), probe)
    assert code != 0
    assert "unreachable" in err.lower() or "timed out" in err.lower()


async def test_capabilities_dropped(sandbox: DockerEnvironment) -> None:
    """capability 全部去掉：有效能力集为 0。"""
    code, out, _ = await run_shell(sandbox.executor(), "grep CapEff /proc/self/status")
    assert code == 0
    assert out.split()[1].strip("0") == ""


async def test_workspace_files_visible_to_commands(sandbox: DockerEnvironment) -> None:
    """经文件接口写入的内容，容器里的命令读得到；反之亦然。"""
    workspace = sandbox.workspace()
    await workspace.write_text("input.txt", "from-host")
    code, out, _ = await run_shell(sandbox.executor(), "cat input.txt; echo from-box > output.txt")
    assert (code, out.strip()) == (0, "from-host")
    assert (await workspace.read_text("output.txt")).strip() == "from-box"


async def test_root_daemon_overwrites_without_cap_chown(sandbox: DockerEnvironment) -> None:
    """容器里守护进程是 root 但没有任何 capability：覆盖自己的文件照常成功，权限位与属主保持。

    走的是守护进程 euid 为 0 的分支：原属主与临时文件属主相同，不调 ``fchown``。
    """
    workspace = sandbox.workspace()
    await workspace.write_text("run.sh", "echo old\n")
    code, _, _ = await run_shell(sandbox.executor(), "chmod 750 run.sh")
    assert code == 0
    await workspace.write_text("run.sh", "echo new\n")
    code, out, _ = await run_shell(sandbox.executor(), "stat -c '%u:%g %a' run.sh; ls -A")
    assert code == 0
    assert out.split() == ["0:0", "750", "run.sh"]
    assert await workspace.read_text("run.sh") == "echo new\n"


async def test_file_access_confined_to_workdir(sandbox: DockerEnvironment) -> None:
    """文件接口出不了工作区，哪怕目标在容器里。"""
    with pytest.raises(PermissionError):
        await sandbox.workspace().read_bytes("/etc/passwd")


async def test_host_workspace_and_read_only_skill_mount(tmp_path: Path) -> None:
    """宿主机目录挂作工作区可写；skill 目录只读挂载，脚本按相同路径运行。"""
    workspace_dir = tmp_path / "ws"
    skills_dir = tmp_path / "skills"
    workspace_dir.mkdir()
    (skills_dir / "demo").mkdir(parents=True)
    script = skills_dir / "demo" / "run.sh"
    script.write_text('echo "arg=$1" > /workspace/result.txt\necho tried\n')
    config = sandbox_config(
        workspace_host_dir=workspace_dir.resolve(),
        mounts=(Mount(skills_dir.resolve(), str(skills_dir.resolve())),),
    )
    async with await DockerEnvironment.create(config) as environment:
        descriptor = ScriptDescriptor(
            skill_id="demo",
            name="run",
            path=script.resolve(),
            language="shell",
            args_schema={"type": "object", "properties": {"value": {}}},
        )
        result = await shell_script_executor(environment.executor(), env=ENV).execute(
            ScriptInvocation(descriptor=descriptor, args={"value": "42"}, cancel=CancellationToken())
        )
        assert result.ok, result.stderr
        code, _, err = await run_shell(environment.executor(), f"echo x >> {script.resolve()}")
        assert code != 0
        assert "Read-only file system" in err
    assert (workspace_dir / "result.txt").read_text().strip() == "arg=42"


async def test_close_removes_container() -> None:
    """关闭后容器被销毁；重复关闭无害。"""
    environment = await DockerEnvironment.create(sandbox_config())
    name = environment.name
    assert _container_exists(name)
    await environment.close()
    await environment.close()
    assert not _container_exists(name)


async def test_missing_python_cleans_up_container() -> None:
    """镜像里没有 Python：创建失败，且不留下容器。"""
    name = "tfsb-test-nopython"
    with pytest.raises(SandboxUnavailableError):
        await DockerEnvironment.create(sandbox_config(image="busybox:latest"), name=name)
    assert not _container_exists(name)


async def test_unknown_docker_binary_is_unavailable() -> None:
    """找不到 docker 命令：按后端不可用处理。"""
    with pytest.raises(SandboxUnavailableError):
        await DockerEnvironment.create(sandbox_config(docker_binary="/no/such/docker"))
