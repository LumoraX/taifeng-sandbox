"""seatbelt 执行器的真实隔离测试：在 macOS 上真的起 ``sandbox-exec``。"""

from __future__ import annotations

import asyncio
import sys
from typing import TYPE_CHECKING

import pytest
from taifeng import CommandExecutor, CommandProcess, CommandSpec

from taifeng_sandbox import SandboxError, SandboxPolicy, SandboxUnavailableError
from taifeng_sandbox.local import SeatbeltCommandExecutor, create_local_executor
from tests.conftest import requires_macos

if TYPE_CHECKING:
    from pathlib import Path

ENV = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C.UTF-8"}


def _shell(command: str, cwd: Path) -> CommandSpec:
    """构造 shell 模式的命令。"""
    return CommandSpec(command=command, shell=True, cwd=str(cwd), env=dict(ENV))


async def _run(executor: CommandExecutor, spec: CommandSpec) -> tuple[int, str, str]:
    """启动并等待结束，返回退出码与输出文本。"""
    proc = await executor.start(spec)
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    assert proc.returncode is not None
    return proc.returncode, stdout.decode(), stderr.decode()


@requires_macos
async def test_satisfies_kernel_protocols(tmp_path: Path) -> None:
    """执行器与返回的进程都满足 taifeng 协议。"""
    executor = SeatbeltCommandExecutor(SandboxPolicy.workspace_write(tmp_path))
    assert isinstance(executor, CommandExecutor)
    proc = await executor.start(_shell("true", tmp_path))
    assert isinstance(proc, CommandProcess)
    assert await proc.wait() == 0


@requires_macos
async def test_write_inside_workspace_allowed(tmp_path: Path) -> None:
    """工作区内可写。"""
    executor = SeatbeltCommandExecutor(SandboxPolicy.workspace_write(tmp_path))
    code, out, _ = await _run(executor, _shell("echo hello > note.txt && cat note.txt", tmp_path))
    assert code == 0
    assert out.strip() == "hello"
    assert (tmp_path / "note.txt").read_text().strip() == "hello"


@requires_macos
async def test_write_outside_workspace_denied(tmp_path: Path) -> None:
    """工作区外不可写。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    executor = SeatbeltCommandExecutor(SandboxPolicy.workspace_write(workspace))
    code, _, err = await _run(executor, _shell(f"echo leak > {outside}", workspace))
    assert code != 0
    assert "not permitted" in err
    assert not outside.exists()


@requires_macos
async def test_read_only_policy_denies_all_writes(tmp_path: Path) -> None:
    """只读策略下连当前目录都不可写。"""
    executor = SeatbeltCommandExecutor(SandboxPolicy.read_only())
    code, _, _ = await _run(executor, _shell("echo x > denied.txt", tmp_path))
    assert code != 0
    assert not (tmp_path / "denied.txt").exists()


@requires_macos
async def test_unreadable_root_blocks_read(tmp_path: Path) -> None:
    """不可读目录即使在全盘可读策略下也读不到。"""
    secret_dir = tmp_path / "secret"
    secret_dir.mkdir()
    (secret_dir / "token").write_text("s3cret")
    policy = SandboxPolicy.read_only(unreadable_roots=(secret_dir,))
    code, out, _ = await _run(
        SeatbeltCommandExecutor(policy), _shell(f"cat {secret_dir / 'token'}", tmp_path)
    )
    assert code != 0
    assert "s3cret" not in out


@requires_macos
async def test_restricted_read_hides_unlisted_directories(tmp_path: Path) -> None:
    """受限读：未列出的目录读不到，列出的只读根读得到。"""
    workspace = tmp_path / "ws"
    shared = tmp_path / "shared"
    private = tmp_path / "private"
    for directory in (workspace, shared, private):
        directory.mkdir()
    (shared / "a.txt").write_text("shared-data")
    (private / "b.txt").write_text("private-data")
    policy = SandboxPolicy.workspace_only(workspace, readable_roots=(shared,))
    executor = SeatbeltCommandExecutor(policy)

    code, out, _ = await _run(executor, _shell(f"cat {shared / 'a.txt'}", workspace))
    assert (code, out.strip()) == (0, "shared-data")

    code, out, _ = await _run(executor, _shell(f"cat {private / 'b.txt'}", workspace))
    assert code != 0
    assert "private-data" not in out


@requires_macos
async def test_network_denied_by_default(tmp_path: Path) -> None:
    """默认不出网：连本机回环都连不上。"""
    server = await asyncio.start_server(lambda _r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    probe = (
        f"{sys.executable} -c \"import socket; "
        f"socket.create_connection(('127.0.0.1', {port}), timeout=3)\""
    )
    try:
        denied = SeatbeltCommandExecutor(SandboxPolicy.read_only())
        code, _, err = await _run(denied, _shell(probe, tmp_path))
        assert code != 0
        assert "not permitted" in err.lower()

        allowed = SeatbeltCommandExecutor(SandboxPolicy(network=True))
        code, _, err = await _run(allowed, _shell(probe, tmp_path))
        assert code == 0, err
    finally:
        server.close()
        await server.wait_closed()


@requires_macos
async def test_environment_is_not_inherited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """宿主环境变量不会泄漏进沙盒。"""
    monkeypatch.setenv("HOST_ONLY_SECRET", "leak-me")
    executor = SeatbeltCommandExecutor(SandboxPolicy.read_only())
    code, out, _ = await _run(executor, _shell('echo "[${HOST_ONLY_SECRET}]"', tmp_path))
    assert (code, out.strip()) == (0, "[]")


@requires_macos
async def test_argv_mode_does_not_interpret_shell_syntax(tmp_path: Path) -> None:
    """argv 模式下 shell 元字符只是普通参数。"""
    executor = SeatbeltCommandExecutor(SandboxPolicy.workspace_write(tmp_path))
    spec = CommandSpec(
        command="/bin/echo 'a; touch injected'", shell=False, cwd=str(tmp_path), env=dict(ENV)
    )
    code, out, _ = await _run(executor, spec)
    assert (code, out.strip()) == (0, "a; touch injected")
    assert not (tmp_path / "injected").exists()


@requires_macos
async def test_kill_terminates_whole_process_group(tmp_path: Path) -> None:
    """kill 连同 shell 派生的子进程一起终止。"""
    executor = SeatbeltCommandExecutor(SandboxPolicy.workspace_write(tmp_path))
    marker = tmp_path / "late.txt"
    proc = await executor.start(_shell(f"(sleep 2; echo late > {marker}) & wait", tmp_path))
    await asyncio.sleep(0.3)
    proc.kill()
    assert await asyncio.wait_for(proc.wait(), timeout=5) != 0
    await asyncio.sleep(2.2)
    assert not marker.exists()


@requires_macos
async def test_empty_command_is_spawn_error(tmp_path: Path) -> None:
    """空命令按启动失败处理（OSError），供 taifeng 工具层转成 spawn_error。"""
    executor = SeatbeltCommandExecutor(SandboxPolicy.read_only())
    spec = CommandSpec(command="   ", shell=False, cwd=str(tmp_path), env=dict(ENV))
    with pytest.raises(SandboxError):
        await executor.start(spec)


@requires_macos
def test_missing_sandbox_exec_fails_closed(tmp_path: Path) -> None:
    """找不到 sandbox-exec 时构造即失败，不退回无隔离执行。"""
    with pytest.raises(SandboxUnavailableError):
        SeatbeltCommandExecutor(SandboxPolicy(), sandbox_exec=str(tmp_path / "missing"))


@requires_macos
def test_factory_picks_seatbelt_on_macos() -> None:
    """工厂在 macOS 上选 seatbelt。"""
    assert isinstance(create_local_executor(SandboxPolicy()), SeatbeltCommandExecutor)


@pytest.mark.skipif(sys.platform.startswith("linux"), reason="只在非 Linux 平台验证")
def test_bwrap_unavailable_off_linux() -> None:
    """非 Linux 平台构造 bubblewrap 执行器直接失败。"""
    from taifeng_sandbox.local import BwrapCommandExecutor

    with pytest.raises(SandboxUnavailableError):
        BwrapCommandExecutor(SandboxPolicy())
