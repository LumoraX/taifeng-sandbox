"""seatbelt 执行器的真实隔离测试：在 macOS 上真的起 ``sandbox-exec``。"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import shlex
import socket
import sys
import tempfile
from pathlib import Path

import pytest
from taifeng import CommandExecutor, CommandProcess, CommandSpec

from taifeng_sandbox import SandboxError, SandboxPolicy, SandboxUnavailableError
from taifeng_sandbox.local import SeatbeltCommandExecutor, create_local_executor
from tests.conftest import requires_macos

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


# 经 sysctl(KERN_PROCARGS / KERN_PROCARGS2) 读另一个进程的参数与环境（ps eww 用的也是它），
# 报告每一种读法是否读到了标记
PROCARGS_PROBE = """\
import ctypes, ctypes.util, json, sys
libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
pid, markers = int(sys.argv[1]), [m.encode() for m in sys.argv[2:]]
report = {}
for name, node in (("KERN_PROCARGS", 38), ("KERN_PROCARGS2", 49)):
    mib = (ctypes.c_int * 3)(1, node, pid)
    size = ctypes.c_size_t(0)
    if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0:
        report[name] = "errno %d" % ctypes.get_errno()
        continue
    buf = ctypes.create_string_buffer(size.value)
    if libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0:
        report[name] = "errno %d" % ctypes.get_errno()
        continue
    report[name] = [m.decode() for m in markers if m in buf.raw[: size.value]]
print(json.dumps(report))
"""

# 依次试：TCP 连本机端口、连本机 Unix 套接字、连 DNS 用的 mDNSResponder 套接字、解析 localhost
NETWORK_PROBE = """\
import json, socket, sys
port, unix_path = int(sys.argv[1]), sys.argv[2]
def attempt(action):
    try:
        action()
        return "ok"
    except OSError as exc:
        return exc.strerror or str(exc)
def unix_connect(path):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(3)
        sock.connect(path)
print(json.dumps({
    "tcp": attempt(lambda: socket.create_connection(("127.0.0.1", port), timeout=3).close()),
    "unix": attempt(lambda: unix_connect(unix_path)),
    "dns_socket": attempt(lambda: unix_connect("/private/var/run/mDNSResponder")),
    "resolve": attempt(lambda: socket.getaddrinfo("localhost", 80)),
}))
"""


@requires_macos
@pytest.mark.xfail(
    strict=True,
    reason=(
        "已知限制：seatbelt 管不到 KERN_PROCARGS2，连 (deny default) 也挡不住（macOS 26.6.2 实测），"
        "沙盒里仍读得到同一用户下其他进程的参数与环境，见 ADR 0005 决策 2。"
        "哪天 macOS 开始拦截，这里会变成 XPASS 而失败，届时更新文档"
    ),
)
async def test_cannot_read_other_process_arguments_or_environment(tmp_path: Path) -> None:
    """期望：沙盒里读不到同一用户下其他进程的参数与环境。

    ``KERN_PROCARGS2`` 就是 ``ps eww`` 用的读法。先在沙盒外确认它读得到，再断言沙盒里读不到。
    sysctl 白名单拦不住它（内核对这一项不走沙盒的 sysctl 检查），所以本用例记录的是一条
    已知限制，不是已修复的行为。
    """
    probe = tmp_path / "procargs.py"
    probe.write_text(PROCARGS_PROBE)
    victim = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(30)", "argv-marker-7f3a",
        env={"PATH": "/usr/bin:/bin", "VICTIM_SECRET": "env-marker-9c1d"},
    )
    markers = [str(victim.pid), "argv-marker-7f3a", "env-marker-9c1d"]
    try:
        outside = await asyncio.create_subprocess_exec(
            sys.executable, str(probe), *markers, stdout=asyncio.subprocess.PIPE
        )
        stdout, _ = await asyncio.wait_for(outside.communicate(), timeout=30)
        assert json.loads(stdout)["KERN_PROCARGS2"] == markers[1:], "沙盒外应当读得到"

        executor = SeatbeltCommandExecutor(SandboxPolicy(network=True))
        command = shlex.join([sys.executable, str(probe), *markers])
        spec = CommandSpec(command=command, shell=False, cwd=str(tmp_path), env=dict(ENV))
        code, out, err = await _run(executor, spec)
        assert code == 0, err
        report = json.loads(out)
        assert "argv-marker-7f3a" not in out and "env-marker-9c1d" not in out
        assert all(isinstance(result, str) for result in report.values()), report
    finally:
        victim.kill()
        await victim.wait()


# 试几项 sysctl：进程列表、启动参数、内核 UUID（不在白名单）与系统版本（在白名单），报告 errno
SYSCTL_PROBE = """\
import ctypes, ctypes.util, json
libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
def by_mib(*mib):
    arr, size = (ctypes.c_int * len(mib))(*mib), ctypes.c_size_t(0)
    return 0 if libc.sysctl(arr, len(mib), None, ctypes.byref(size), None, 0) == 0 else ctypes.get_errno()
def by_name(name):
    size = ctypes.c_size_t(0)
    rc = libc.sysctlbyname(name.encode(), None, ctypes.byref(size), None, 0)
    return 0 if rc == 0 else ctypes.get_errno()
print(json.dumps({
    "kern.proc.all": by_mib(1, 14, 0),
    "kern.bootargs": by_name("kern.bootargs"),
    "kern.uuid": by_name("kern.uuid"),
    "kern.osversion": by_name("kern.osversion"),
    "hw.ncpu": by_name("hw.ncpu"),
}))
"""


@requires_macos
async def test_sysctl_outside_allowlist_denied(tmp_path: Path) -> None:
    """白名单之外的 sysctl 被拒（EPERM）：沙盒里列不出本机进程，读不到启动参数。

    修复前整体放开 ``sysctl-read``，这些都读得到。
    """
    probe = tmp_path / "sysctl.py"
    probe.write_text(SYSCTL_PROBE)
    executor = SeatbeltCommandExecutor(SandboxPolicy(network=True))
    spec = CommandSpec(
        command=shlex.join([sys.executable, str(probe)]), shell=False, cwd=str(tmp_path),
        env=dict(ENV),
    )
    code, out, err = await _run(executor, spec)
    assert code == 0, err
    assert json.loads(out) == {
        "kern.proc.all": 1,
        "kern.bootargs": 1,
        "kern.uuid": 1,
        "kern.osversion": 0,
        "hw.ncpu": 0,
    }


@requires_macos
async def test_network_reaches_ip_but_not_local_unix_sockets(tmp_path: Path) -> None:
    """出网时能连本机 TCP 端口、能做 DNS，但连不上本机的 Unix 套接字。

    修复前出网档是不加过滤的 ``network-outbound``，Docker 守护进程、ssh-agent、本地数据库的
    Unix 套接字都连得上。套接字文件本身可读（全盘可读策略），挡住它的是出网规则。
    """
    probe = tmp_path / "network.py"
    probe.write_text(NETWORK_PROBE)
    tcp = await asyncio.start_server(lambda _r, w: w.close(), "127.0.0.1", 0)
    port = tcp.sockets[0].getsockname()[1]
    # AF_UNIX 路径上限 104 字节，pytest 的 tmp_path 可能超长，另取一个短目录
    with tempfile.TemporaryDirectory(prefix="tfsb-") as short:
        unix_path = str(Path(short) / "s.sock")
        unix = await asyncio.start_unix_server(lambda _r, w: w.close(), path=unix_path)
        try:
            executor = SeatbeltCommandExecutor(SandboxPolicy(network=True))
            command = shlex.join([sys.executable, str(probe), str(port), unix_path])
            spec = CommandSpec(command=command, shell=False, cwd=str(tmp_path), env=dict(ENV))
            code, out, err = await _run(executor, spec)
            assert code == 0, err
            assert json.loads(out) == {
                "tcp": "ok",
                "unix": "Operation not permitted",
                "dns_socket": "ok",
                "resolve": "ok",
            }
        finally:
            for server in (tcp, unix):
                server.close()
                await server.wait_closed()


@requires_macos
async def test_runtimes_start_under_workspace_only(tmp_path: Path) -> None:
    """收紧 sysctl 后常见运行时照常启动：Python 读得到 CPU 数与系统版本，``sysctl`` 命令行可用。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    readable = tuple({Path(sys.prefix), Path(sys.base_prefix)})
    policy = SandboxPolicy.workspace_only(workspace, readable_roots=readable, network=True)
    executor = SeatbeltCommandExecutor(policy)
    script = (
        "import asyncio, json, multiprocessing, os, platform, socket, ssl, uuid; "
        "asyncio.run(asyncio.sleep(0)); "
        "print(json.dumps([os.cpu_count(), multiprocessing.cpu_count(), platform.mac_ver()[0], "
        "socket.gethostname()]))"
    )
    spec = CommandSpec(
        command=shlex.join([sys.executable, "-c", script]),
        shell=False, cwd=str(workspace), env=dict(ENV),
    )
    code, out, err = await _run(executor, spec)
    assert code == 0, err
    assert json.loads(out) == [
        os.cpu_count(), os.cpu_count(), platform.mac_ver()[0], socket.gethostname()
    ]

    code, out, err = await _run(executor, _shell("sysctl -n hw.ncpu kern.osversion", workspace))
    assert code == 0, err
    assert out.split()[0] == str(os.cpu_count())


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
