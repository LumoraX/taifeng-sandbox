"""``DaemonWorkspace`` 满足内核 ``taifeng.WorkspaceFS``（内核 ADR 0113）。

内核的文件类工具经线协议读写沙盒。守护进程作为子进程真的跑起来（见 ``conftest.py``），不打桩。
内核没有现成的 ``WorkspaceFS`` 一致性检查函数（``taifeng.testing`` 里只有 Journal 的），这里对照
内核给 ``LocalWorkspaceFS`` 的用例逐条检查。
"""

from __future__ import annotations

import os
import stat
from typing import TYPE_CHECKING, Any, cast

import pytest
import taifeng

from taifeng_sandbox import SandboxProtocolError, SandboxRemoteError
from taifeng_sandbox.daemon import DaemonClient, DaemonWorkspace, protocol

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path


def _ctx() -> taifeng.ToolContext:
    """工具调用上下文，构造方式与内核的工具测试一致。"""
    return taifeng.ToolContext(call_id="c1", cancel=taifeng.CancellationToken(), thread_id="t")


async def _call(tool: taifeng.ToolSpec, **args: Any) -> taifeng.ToolResult:
    """像引擎那样调用工具的 handler。"""
    return await tool.handler(args, _ctx())


def _umask() -> int:
    """当前进程的 umask；守护进程是本进程的子进程，继承同一个值。"""
    current = os.umask(0o022)
    os.umask(current)
    return current


async def test_daemon_workspace_is_a_workspace_fs(client: DaemonClient, root: Path) -> None:
    """满足内核协议，内核的文件工具可以直接用它。"""
    ws = DaemonWorkspace(client)
    assert isinstance(ws, taifeng.WorkspaceFS)
    assert ws.root == str(root.resolve())
    assert ws.resolve("a/../b.txt") == str(root.resolve() / "b.txt")
    assert ws.resolve("") == ws.resolve(".") == ws.root
    assert ws.resolve(str(root / "x")) == str(root / "x")
    with pytest.raises(taifeng.WorkspacePathError):
        ws.resolve("../outside")
    with pytest.raises(taifeng.WorkspacePathError):
        ws.resolve(str(root) + "-evil/x")  # 同前缀的兄弟目录不算根内

    await ws.write_bytes("notes/a.txt", b"hello")
    info = await ws.metadata("notes/a.txt")
    assert isinstance(info, taifeng.WorkspaceFileInfo) and info.is_file and info.size == 5
    assert (await ws.metadata("notes")).is_directory
    entries = await ws.list_directory("notes")
    assert entries == [taifeng.WorkspaceEntry(name="a.txt", is_directory=False, is_file=True)]
    assert (await ws.metadata("notes/a.txt/deeper")).exists is False

    tool = taifeng.make_file_read_tool(workspace=ws)
    assert tool.name == "file_read"


@pytest.mark.parametrize("outside", ["/etc/passwd", "../outside.txt", "a/../../outside.txt"])
async def test_out_of_root_access_raises_workspace_path_error(
    client: DaemonClient, root: Path, outside: str
) -> None:
    """越界一律 WorkspacePathError（不必先调 resolve）：每个方法自己校验边界。"""
    ws = DaemonWorkspace(client)
    actions: list[Callable[[], Awaitable[object]]] = [
        lambda: ws.read_bytes(outside),
        lambda: ws.write_bytes(outside, b"leak"),
        lambda: ws.metadata(outside),
        lambda: ws.list_directory(outside),
        lambda: ws.remove(outside),
        lambda: ws.create_directory(outside),
    ]
    for action in actions:
        with pytest.raises(taifeng.WorkspacePathError):
            await action()
    assert not (root.parent / "outside.txt").exists()


async def test_daemon_reports_escape_with_its_own_error_code(client: DaemonClient) -> None:
    """线协议层面：越界是 ``-32024``，与「操作系统拒绝访问」的 ``-32020`` 分开。"""
    with pytest.raises(SandboxRemoteError) as caught:
        await client.request(protocol.METHOD_FS_READ_FILE, {"path": "/etc/passwd"})
    assert caught.value.code == protocol.ERROR_OUTSIDE_ROOT == -32024


@pytest.mark.skipif(os.geteuid() == 0, reason="root 用户不受文件权限约束")
async def test_os_permission_denied_is_not_a_workspace_path_error(
    client: DaemonClient, root: Path
) -> None:
    """根内的文件操作系统不让读：是 PermissionError，但不是表示越界的 WorkspacePathError。"""
    locked = root / "locked.txt"
    locked.write_text("x")
    locked.chmod(0)
    try:
        with pytest.raises(PermissionError) as caught:
            await DaemonWorkspace(client).read_bytes("locked.txt")
        assert not isinstance(caught.value, taifeng.WorkspacePathError)
        with pytest.raises(SandboxRemoteError) as remote:
            await client.request(protocol.METHOD_FS_READ_FILE, {"path": "locked.txt"})
        assert remote.value.code == protocol.ERROR_ACCESS_DENIED
    finally:
        locked.chmod(0o600)


async def test_symlink_escape_raises_workspace_path_error(
    client: DaemonClient, root: Path, tmp_path: Path
) -> None:
    """根内的符号链接指向根外：resolve 不跟随链接，守护进程按真实路径拦下。

    守护进程的拒绝还原成 WorkspacePathError。
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("s3cret")
    (root / "link").symlink_to(outside)
    (root / "file-link").symlink_to(outside / "secret.txt")
    ws = DaemonWorkspace(client)
    assert ws.resolve("link/secret.txt") == str(root / "link" / "secret.txt")
    actions: list[Callable[[], Awaitable[object]]] = [
        lambda: ws.read_bytes("link/secret.txt"),
        lambda: ws.read_bytes("file-link"),
        lambda: ws.write_bytes("link/new.txt", b"leak"),
        lambda: ws.write_bytes("file-link", b"leak"),
        lambda: ws.metadata("file-link"),
        lambda: ws.list_directory("link"),
    ]
    for action in actions:
        with pytest.raises(taifeng.WorkspacePathError):
            await action()
    assert not (outside / "new.txt").exists()
    assert (outside / "secret.txt").read_text() == "s3cret"
    # 列目录不跟随链接判定类型
    assert sorted(await ws.list_directory("."), key=lambda entry: entry.name) == [
        taifeng.WorkspaceEntry(name=name, is_directory=False, is_file=False, is_symlink=True)
        for name in ("file-link", "link")
    ]


async def test_whole_file_write_is_atomic(client: DaemonClient, root: Path) -> None:
    """覆盖写入不会留下半截文件：写入经临时文件再替换，目录里不残留临时文件。"""
    ws = DaemonWorkspace(client)
    await ws.write_bytes("f.txt", b"old")
    # 替换前打开的读者读到完整的旧内容；原地截断再写的话它会读到新内容或空文件
    with open(root / "f.txt", "rb") as reader:
        await ws.write_bytes("f.txt", b"new")
        assert reader.read() == b"old"
    assert (root / "f.txt").read_bytes() == b"new"
    assert sorted(p.name for p in root.iterdir()) == ["f.txt"]


async def test_failed_whole_file_write_removes_the_temp_file(
    client: DaemonClient, root: Path, tmp_path: Path
) -> None:
    """替换失败（目标是目录）时报错，临时文件被删掉；往根目录本身写不会在根外建临时文件。"""
    (root / "d").mkdir()
    ws = DaemonWorkspace(client)
    with pytest.raises(OSError):  # noqa: PT011 —— 具体子类随平台而异
        await ws.write_bytes("d", b"x")
    assert [p.name for p in root.iterdir()] == ["d"]
    assert list((root / "d").iterdir()) == []
    # 根目录的父目录设为只读：若守护进程试图在那里建临时文件，得到的会是 -32020 而不是 -32022
    tmp_path.chmod(0o555)
    try:
        with pytest.raises(SandboxRemoteError) as caught:
            await ws.write_bytes(".", b"x")
        assert caught.value.code == protocol.ERROR_IO
    finally:
        tmp_path.chmod(0o755)
    assert list(tmp_path.iterdir()) == [root]


async def test_overwrite_keeps_mode_and_new_files_follow_umask(
    client: DaemonClient, root: Path
) -> None:
    """临时文件的 0600 不带到目标上：覆盖保持原权限（可执行位不丢），新建按 umask。

    两种情形都与直接 ``open`` 写入的结果一致。
    """
    ws = DaemonWorkspace(client)
    script = root / "run.sh"
    script.write_text("echo old\n")
    script.chmod(0o755)
    await ws.write_bytes("run.sh", b"echo new\n")
    assert stat.S_IMODE(script.stat().st_mode) == 0o755
    await ws.write_bytes("fresh.txt", b"x")
    assert stat.S_IMODE((root / "fresh.txt").stat().st_mode) == 0o666 & ~_umask()


async def test_failures_are_standard_os_errors(client: DaemonClient, root: Path) -> None:
    """失败用标准 OSError 子类表达，与内核 LocalWorkspaceFS 一致。"""
    ws = DaemonWorkspace(client)
    with pytest.raises(FileNotFoundError):
        await ws.read_bytes("missing.txt")
    with pytest.raises(FileNotFoundError):
        await ws.list_directory("missing")
    with pytest.raises(FileNotFoundError):
        await ws.remove("missing.txt")
    with pytest.raises(FileNotFoundError):
        await ws.write_bytes("missing/x.txt", b"x", create_parents=False)
    assert not (root / "missing").exists()

    await ws.write_bytes("d/x.txt", b"x")
    with pytest.raises(OSError) as caught:  # noqa: PT011 —— 非空目录：具体子类随平台而异
        await ws.remove("d")
    assert not isinstance(caught.value, FileNotFoundError | PermissionError)
    assert (root / "d" / "x.txt").exists()
    await ws.remove("d", recursive=True)
    assert not (root / "d").exists()


def test_root_requires_handshake_info() -> None:
    """握手信息里没有根目录：抛 SandboxProtocolError，不猜默认值。"""

    class _NoRoot:
        server_info: dict[str, Any] = {}  # noqa: RUF012 —— 只读的桩

    ws = DaemonWorkspace(cast("DaemonClient", _NoRoot()))
    with pytest.raises(SandboxProtocolError):
        _ = ws.root
    with pytest.raises(SandboxProtocolError):
        ws.resolve("a.txt")


async def test_kernel_file_tools_write_then_read_through_the_daemon(
    client: DaemonClient, root: Path
) -> None:
    """内核的 file_write / file_read 经守护进程写入再读出；结果里的路径是沙盒里的规范路径。"""
    ws = DaemonWorkspace(client)
    content = "# 报告\n第二行"
    written = await _call(
        taifeng.make_file_write_tool(workspace=ws), path="out/report.md", content=content
    )
    assert not written.is_error, written.output
    assert written.data["path"] == str(root / "out" / "report.md")
    assert (root / "out" / "report.md").read_text(encoding="utf-8") == content

    read_tool = taifeng.make_file_read_tool(workspace=ws)
    assert ws.root in read_tool.description
    whole = await _call(read_tool, path="out/report.md")
    assert not whole.is_error, whole.output
    assert whole.output == content
    assert whole.data["path"] == str(root / "out" / "report.md")
    paged = await _call(read_tool, path="out/report.md", offset=1, limit=1)
    assert paged.output == "第二行"
    missing = await _call(read_tool, path="out/none.md")
    assert missing.is_error and missing.data.get("reason") == "not_found"


async def test_kernel_file_tools_cannot_reach_host_files(
    client: DaemonClient, root: Path, tmp_path: Path
) -> None:
    """越界的 file_read / file_write 返回错误结果，碰不到宿主上的文件。

    ``..`` 与根外绝对路径在 ``resolve`` 就拦下（``sandbox_violation``）；根内指向根外的符号链接在
    守护进程按真实路径拦下（工具层报读写失败）。
    """
    secret = tmp_path / "secret.txt"
    secret.write_text("s3cret")
    (root / "link.txt").symlink_to(secret)
    ws = DaemonWorkspace(client)
    read_tool = taifeng.make_file_read_tool(workspace=ws)
    write_tool = taifeng.make_file_write_tool(workspace=ws)
    for path in ("../secret.txt", str(secret)):
        read = await _call(read_tool, path=path)
        assert read.is_error and read.data.get("reason") == "sandbox_violation"
        assert "s3cret" not in read.output
        write = await _call(write_tool, path=path, content="leak")
        assert write.is_error and write.data.get("reason") == "sandbox_violation"
    via_link = await _call(read_tool, path="link.txt")
    assert via_link.is_error and "s3cret" not in via_link.output
    write_via_link = await _call(write_tool, path="link.txt", content="leak")
    assert write_via_link.is_error
    assert secret.read_text() == "s3cret"


async def test_kernel_patch_and_search_tools_work_through_the_daemon(
    client: DaemonClient, root: Path
) -> None:
    """apply_patch / glob / grep 也经守护进程工作；搜索工具在工作线程里经事件循环回调本工作区。"""
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("TOKEN = 1\n")
    (root / "build").mkdir()
    (root / "build" / "out.py").write_text("TOKEN = 2\n")
    (root / ".gitignore").write_text("build/\n")
    ws = DaemonWorkspace(client)

    patched = await _call(
        taifeng.make_apply_patch_tool(workspace=ws),
        patches=[
            {"path": "src/app.py", "old_text": "TOKEN = 1", "new_text": "TOKEN = 3"},
            {"path": "src/new.py", "new_text": "TOKEN = 4\n", "create": True},
        ],
    )
    assert not patched.is_error, patched.output
    assert (root / "src" / "app.py").read_text() == "TOKEN = 3\n"
    assert (root / "src" / "new.py").read_text() == "TOKEN = 4\n"

    globbed = await _call(taifeng.make_glob_tool(workspace=ws), pattern="**/*.py")
    assert not globbed.is_error, globbed.output
    assert sorted(globbed.output.splitlines()[:2]) == ["src/app.py", "src/new.py"]
    assert "build/out.py" not in globbed.output

    grepped = await _call(
        taifeng.make_grep_tool(workspace=ws), pattern="TOKEN", output_mode="content"
    )
    assert not grepped.is_error, grepped.output
    assert "src/app.py:1:TOKEN = 3" in grepped.output
    assert "src/new.py:1:TOKEN = 4" in grepped.output
    assert "build/out.py" not in grepped.output
