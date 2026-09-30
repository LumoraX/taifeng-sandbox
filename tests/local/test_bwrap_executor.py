"""bubblewrap 执行器的真实隔离测试：在 Linux 上真的起 ``bwrap``。"""

from __future__ import annotations

import asyncio
import sys
from typing import TYPE_CHECKING

import pytest
from taifeng import CommandExecutor, CommandProcess, CommandSpec

from taifeng_sandbox import SandboxPolicy, SandboxUnavailableError
from taifeng_sandbox.local import BwrapCommandExecutor, create_local_executor
from tests.conftest import requires_bwrap

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = requires_bwrap

ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C.UTF-8"}


def _shell(command: str, cwd: Path) -> CommandSpec:
    """构造 shell 模式的命令。"""
    return CommandSpec(command=command, shell=True, cwd=str(cwd), env=dict(ENV))  # noqa: S604


async def _run(executor: CommandExecutor, spec: CommandSpec) -> tuple[int, str, str]:
    """启动并等待结束，返回退出码与输出文本。"""
    proc = await executor.start(spec)
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    assert proc.returncode is not None
    return proc.returncode, stdout.decode(), stderr.decode()


async def test_satisfies_kernel_protocols(tmp_path: Path) -> None:
    """执行器与返回的进程都满足 taifeng 协议。"""
    executor = BwrapCommandExecutor(SandboxPolicy.workspace_write(tmp_path))
    assert isinstance(executor, CommandExecutor)
    proc = await executor.start(_shell("true", tmp_path))
    assert isinstance(proc, CommandProcess)
    assert await proc.wait() == 0


async def test_write_inside_workspace_allowed(tmp_path: Path) -> None:
    """工作区内可写，且写入落到宿主机上的同一个目录。"""
    executor = BwrapCommandExecutor(SandboxPolicy.workspace_write(tmp_path))
    code, out, err = await _run(
        executor, _shell("echo hello > note.txt && cat note.txt", tmp_path)
    )
    assert code == 0, err
    assert out.strip() == "hello"
    assert (tmp_path / "note.txt").read_text().strip() == "hello"


async def test_write_outside_workspace_denied(tmp_path: Path) -> None:
    """工作区外不可写。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    executor = BwrapCommandExecutor(SandboxPolicy.workspace_write(workspace))
    code, _, err = await _run(executor, _shell(f"echo leak > {outside}", workspace))
    assert code != 0
    assert "Read-only file system" in err
    assert not outside.exists()


async def test_unreadable_directory_and_file_masked(tmp_path: Path) -> None:
    """不可读目录看起来是空的；不可读文件读不出原内容（读到空内容或直接被拒）。"""
    secret_dir = tmp_path / "secret"
    secret_dir.mkdir()
    (secret_dir / "token").write_text("s3cret-dir")
    secret_file = tmp_path / "key.txt"
    secret_file.write_text("s3cret-file")
    policy = SandboxPolicy.read_only(unreadable_roots=(secret_dir, secret_file))
    executor = BwrapCommandExecutor(policy)

    code, out, err = await _run(executor, _shell(f"ls -A {secret_dir}", tmp_path))
    assert (code, out.strip()) == (0, ""), err

    _, out, _ = await _run(executor, _shell(f"cat {secret_file}", tmp_path))
    assert out == ""


async def test_restricted_read_hides_unlisted_directories(tmp_path: Path) -> None:
    """受限读：未列出的目录在沙盒里不存在，列出的只读根读得到但写不了。"""
    workspace = tmp_path / "ws"
    shared = tmp_path / "shared"
    private = tmp_path / "private"
    for directory in (workspace, shared, private):
        directory.mkdir()
    (shared / "a.txt").write_text("shared-data")
    (private / "b.txt").write_text("private-data")
    policy = SandboxPolicy.workspace_only(workspace, readable_roots=(shared,))
    executor = BwrapCommandExecutor(policy)

    code, out, err = await _run(executor, _shell(f"cat {shared / 'a.txt'}", workspace))
    assert (code, out.strip()) == (0, "shared-data"), err

    code, out, _ = await _run(executor, _shell(f"cat {private / 'b.txt'}", workspace))
    assert code != 0
    assert "private-data" not in out

    code, _, _ = await _run(executor, _shell(f"echo x > {shared / 'a.txt'}", workspace))
    assert code != 0
    assert (shared / "a.txt").read_text() == "shared-data"


async def test_network_namespace_isolated_by_default(tmp_path: Path) -> None:
    """默认不出网：沙盒里连宿主机回环上的服务都连不上。"""
    server = await asyncio.start_server(lambda _r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    probe = (
        f"{sys.executable} -c \"import socket; "
        f"socket.create_connection(('127.0.0.1', {port}), timeout=3)\""
    )
    try:
        code, _, _ = await _run(
            BwrapCommandExecutor(SandboxPolicy.read_only()), _shell(probe, tmp_path)
        )
        assert code != 0
        code, _, err = await _run(
            BwrapCommandExecutor(SandboxPolicy(network=True)), _shell(probe, tmp_path)
        )
        assert code == 0, err
    finally:
        server.close()
        await server.wait_closed()


async def test_process_tree_is_isolated(tmp_path: Path) -> None:
    """进程命名空间隔离：沙盒里看不到宿主机的进程。"""
    code, out, err = await _run(
        BwrapCommandExecutor(SandboxPolicy.read_only()),
        _shell("ls /proc | grep -c '^[0-9]'", tmp_path),
    )
    assert code == 0, err
    assert int(out.strip()) < 10


async def test_environment_is_not_inherited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """宿主环境变量不会泄漏进沙盒。"""
    monkeypatch.setenv("HOST_ONLY_SECRET", "leak-me")
    executor = BwrapCommandExecutor(SandboxPolicy.read_only())
    code, out, _ = await _run(executor, _shell('echo "[${HOST_ONLY_SECRET}]"', tmp_path))
    assert (code, out.strip()) == (0, "[]")


async def test_kill_terminates_whole_process_group(tmp_path: Path) -> None:
    """kill 连同沙盒里派生的子进程一起终止。"""
    executor = BwrapCommandExecutor(SandboxPolicy.workspace_write(tmp_path))
    marker = tmp_path / "late.txt"
    proc = await executor.start(_shell(f"(sleep 2; echo late > {marker}) & wait", tmp_path))
    await asyncio.sleep(0.3)
    proc.kill()
    assert await asyncio.wait_for(proc.wait(), timeout=5) != 0
    await asyncio.sleep(2.2)
    assert not marker.exists()


def test_missing_bwrap_fails_closed(tmp_path: Path) -> None:
    """找不到 bubblewrap 时构造即失败，不退回无隔离执行。"""
    with pytest.raises(SandboxUnavailableError):
        BwrapCommandExecutor(SandboxPolicy(), bwrap_path=str(tmp_path / "missing"))


def test_factory_picks_bwrap_on_linux() -> None:
    """工厂在 Linux 上选 bubblewrap。"""
    assert isinstance(create_local_executor(SandboxPolicy()), BwrapCommandExecutor)
