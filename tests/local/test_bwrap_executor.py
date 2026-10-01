"""bubblewrap 执行器的真实隔离测试：在 Linux 上真的起 ``bwrap``。"""

from __future__ import annotations

import asyncio
import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from taifeng import CommandExecutor, CommandProcess, CommandSpec

from taifeng_sandbox import SandboxPolicy, SandboxUnavailableError
from taifeng_sandbox.local import BwrapCommandExecutor, create_local_executor
from tests.conftest import requires_bwrap

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


# 冒充 bwrap 的启动器：报告 exec 时拿到的环境（读 /proc/self/environ，不受解释器自己改
# os.environ 的影响，如 PEP 538 补的 LC_CTYPE）、工作目录、命令行与 --args 的内容
FAKE_LAUNCHER = """\
#!{python}
import json, os, sys
argv = sys.argv[1:]
data = b""
if "--args" in argv:
    with os.fdopen(int(argv[argv.index("--args") + 1]), "rb") as handle:
        data = handle.read()
with open("/proc/self/environ", "rb") as handle:
    env = dict(item.split("=", 1) for item in handle.read().decode().split("\\0") if item)
report = {{"env": env, "cwd": os.getcwd(), "argv": argv, "args": data.decode()}}
print(json.dumps(report))
"""

PRELOAD_SOURCE = r"""
#include <fcntl.h>
#include <stdlib.h>
#include <unistd.h>

/* 被加载即把「谁加载了我」（/proc/self/exe）追加到 PRELOAD_MARKER 指向的文件 */
__attribute__((constructor)) static void mark(void) {
    const char *path = getenv("PRELOAD_MARKER");
    char exe[4096];
    ssize_t n;
    int fd;
    if (path == NULL) return;
    n = readlink("/proc/self/exe", exe, sizeof(exe) - 1);
    if (n < 0) return;
    exe[n] = '\n';
    fd = open(path, O_WRONLY | O_CREAT | O_APPEND, 0644);
    if (fd < 0) return;
    (void) write(fd, exe, (size_t) n + 1);
    close(fd);
}
"""


async def test_launcher_gets_fixed_env_and_cwd(tmp_path: Path) -> None:
    """沙盒外的启动器只拿固定的空环境、工作目录是 ``/``；目标环境经 ``--args`` 的 fd 传入。

    把启动器换成一个打印自身环境、工作目录与 ``--args`` 内容的脚本：调用方给的
    ``LD_PRELOAD`` 与密钥都不在启动器的环境与命令行里，只出现在 fd 里的 ``--setenv`` 中。
    """
    fake = tmp_path / "fake-bwrap"
    fake.write_text(FAKE_LAUNCHER.format(python=sys.executable))
    fake.chmod(0o755)
    package = tmp_path / "pkg"
    package.mkdir()
    env = {**ENV, "LD_PRELOAD": str(tmp_path / "missing.so"), "API_SECRET": "s3cret-v@lue"}
    executor = BwrapCommandExecutor(SandboxPolicy.read_only(), bwrap_path=str(fake))
    spec = CommandSpec(command="true", shell=True, cwd=str(package), env=env)  # noqa: S604
    code, out, err = await _run(executor, spec)
    assert code == 0, err
    report = json.loads(out)
    assert report["env"] == {}
    assert report["cwd"] == "/"
    assert "s3cret-v@lue" not in " ".join(report["argv"])
    assert report["argv"][report["argv"].index("--chdir") + 1] == str(package)
    assert report["args"].split("\0") == [
        part for name, value in env.items() for part in ("--setenv", name, value)
    ] + [""]


async def test_ld_preload_only_loads_inside_sandbox(tmp_path: Path) -> None:
    """env 里的 ``LD_PRELOAD`` 只在沙盒里的目标进程生效，沙盒外的 bwrap 启动器不加载它。

    修复前 bwrap 启动器继承整份 env，glibc 在建沙盒之前就把这个 .so 载入启动器，
    env 提供方的代码以宿主用户身份在沙盒外执行。
    """
    compiler = shutil.which("cc") or shutil.which("gcc")
    if compiler is None:
        pytest.skip("需要 C 编译器来编出测试用的 LD_PRELOAD 库")
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "preload.c").write_text(PRELOAD_SOURCE)
    subprocess.run(  # noqa: S603 —— 固定参数编译测试库
        [compiler, "-shared", "-fPIC", "-o", str(lib / "preload.so"), str(lib / "preload.c")],
        check=True,
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()
    marker = workspace / "loaded-by.txt"
    env = {**ENV, "LD_PRELOAD": str(lib / "preload.so"), "PRELOAD_MARKER": str(marker)}
    executor = BwrapCommandExecutor(SandboxPolicy.workspace_only(workspace, readable_roots=(lib,)))
    spec = CommandSpec(command="true", shell=True, cwd=str(lib), env=env)  # noqa: S604
    code, _, err = await _run(executor, spec)
    assert code == 0, err
    loaded_by = marker.read_text().splitlines()
    assert loaded_by, "目标进程应当拿到 LD_PRELOAD"
    assert [exe for exe in loaded_by if "bwrap" in exe] == []


async def test_target_sees_exact_env_and_cwd(tmp_path: Path) -> None:
    """目标进程的环境恰好是 ``CommandSpec.env``（外加 bwrap 设的 ``PWD``），值原样保留。"""
    env = {
        **ENV,
        "WITH_SPACE": "a b",
        "MULTILINE": "x\ny",
        "WITH_EQUALS": "k=v",
        "LOOKS_LIKE_OPTION": "--bind / /",
        "EMPTY": "",
    }
    probe = (
        f"{shlex.quote(sys.executable)} -c "
        "'import json, os; print(json.dumps([dict(os.environ), os.getcwd()]))'"
    )
    executor = BwrapCommandExecutor(SandboxPolicy.read_only())
    spec = CommandSpec(command=probe, shell=False, cwd=str(tmp_path), env=env)
    code, out, err = await _run(executor, spec)
    assert code == 0, err
    seen_env, seen_cwd = json.loads(out)
    assert seen_env == {**env, "PWD": str(tmp_path)}
    assert seen_cwd == str(tmp_path)


async def test_cwd_none_uses_host_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``cwd=None`` 时沙盒里的工作目录是宿主进程的当前目录（与不隔离执行一致）。"""
    monkeypatch.chdir(tmp_path)
    executor = BwrapCommandExecutor(SandboxPolicy.read_only())
    spec = CommandSpec(command="pwd", shell=True, cwd=None, env=dict(ENV))  # noqa: S604
    code, out, err = await _run(executor, spec)
    assert (code, out.strip()) == (0, str(tmp_path)), err


async def test_env_values_not_visible_on_launcher(tmp_path: Path) -> None:
    """启动器的命令行与初始环境里都没有 env 的值。

    ``/proc/<pid>/cmdline`` 默认对本机所有用户可读，``environ`` 只对同用户可读；值放进
    ``--args`` 的 fd 而不是命令行，就不会因为改走 ``--setenv`` 而多暴露给其他用户。
    """
    executor = BwrapCommandExecutor(SandboxPolicy.read_only())
    spec = CommandSpec(  # noqa: S604
        command="sleep 30", shell=True, cwd=str(tmp_path),
        env={**ENV, "API_SECRET": "s3cret-v@lue"},
    )
    proc = await executor.start(spec)
    try:
        cmdline = Path(f"/proc/{proc.pid}/cmdline").read_bytes()
        environ = Path(f"/proc/{proc.pid}/environ").read_bytes()
        assert b"--chdir" in cmdline
        assert b"s3cret-v@lue" not in cmdline
        assert b"s3cret-v@lue" not in environ
    finally:
        proc.kill()
        await asyncio.wait_for(proc.wait(), timeout=5)


def test_missing_bwrap_fails_closed(tmp_path: Path) -> None:
    """找不到 bubblewrap 时构造即失败，不退回无隔离执行。"""
    with pytest.raises(SandboxUnavailableError):
        BwrapCommandExecutor(SandboxPolicy(), bwrap_path=str(tmp_path / "missing"))


def test_factory_picks_bwrap_on_linux() -> None:
    """工厂在 Linux 上选 bubblewrap。"""
    assert isinstance(create_local_executor(SandboxPolicy()), BwrapCommandExecutor)
