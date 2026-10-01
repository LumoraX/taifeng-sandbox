"""``DaemonWorkspace`` 满足内核 ``taifeng.WorkspaceFS``（内核 ADR 0113）。

内核的文件类工具经线协议读写沙盒。守护进程作为子进程真的跑起来（见 ``conftest.py``），不打桩。
内核没有现成的 ``WorkspaceFS`` 一致性检查函数（``taifeng.testing`` 里只有 Journal 的），这里对照
内核给 ``LocalWorkspaceFS`` 的用例逐条检查。
"""

from __future__ import annotations

import base64
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


# 绕过 DaemonWorkspace 直接发的文件请求：每个方法除 path 之外的参数
_RAW_PARAMS: dict[str, dict[str, Any]] = {
    protocol.METHOD_FS_READ_FILE: {},
    protocol.METHOD_FS_WRITE_FILE: {"data": base64.b64encode(b"leak").decode(), "createParents": True},
    protocol.METHOD_FS_REMOVE: {"recursive": True},
    protocol.METHOD_FS_CREATE_DIRECTORY: {"recursive": True},
    protocol.METHOD_FS_GET_METADATA: {},
    protocol.METHOD_FS_READ_DIRECTORY: {},
}


def _plant_outside(root: Path) -> None:
    """在根目录之外布置文件：父目录里的文件、同前缀的兄弟目录、根内链接指向的根外目录。"""
    base = root.parent
    (base / "outside.txt").write_text("s3cret")
    (base / f"{root.name}-evil").mkdir()
    (base / f"{root.name}-evil" / "f.txt").write_text("evil")
    (base / "outside-dir").mkdir()
    (base / "outside-dir" / "secret.txt").write_text("s3cret")
    (root / "link").symlink_to(base / "outside-dir")


def _outside_state(root: Path) -> dict[str, str]:
    """根目录之外的全部条目与文件内容（不进根目录）。"""
    state: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root.parent):
        dirnames[:] = [name for name in dirnames if os.path.join(dirpath, name) != str(root)]
        for name in dirnames:
            state[os.path.relpath(os.path.join(dirpath, name), root.parent)] = "<dir>"
        for name in filenames:
            path = os.path.join(dirpath, name)
            with open(path, encoding="utf-8") as handle:
                state[os.path.relpath(path, root.parent)] = handle.read()
    return state


@pytest.mark.parametrize(
    "escape",
    ["../outside.txt", "a/../../outside.txt", "{root}-evil/f.txt", "link/secret.txt"],
    ids=["dotdot", "nested-dotdot", "same-prefix-sibling", "symlink"],
)
async def test_daemon_rejects_escapes_on_its_own(
    client: DaemonClient, root: Path, escape: str
) -> None:
    """守护进程是第二道防线：绕过客户端 resolve 直接发请求，六个文件方法越界一律 ``-32024``。

    根外的文件一个字节都不变，也不多出东西。
    """
    _plant_outside(root)
    before = _outside_state(root)
    path = escape.format(root=root)
    for method, extra in _RAW_PARAMS.items():
        with pytest.raises(SandboxRemoteError) as caught:
            await client.request(method, {"path": path, **extra})
        assert caught.value.code == protocol.ERROR_OUTSIDE_ROOT == -32024, method
    assert _outside_state(root) == before


async def test_nul_in_path_is_invalid_params(client: DaemonClient) -> None:
    """路径里有 NUL：每个文件方法都回参数错误 ``-32602``，不是内部错误。"""
    for method, extra in _RAW_PARAMS.items():
        with pytest.raises(SandboxRemoteError) as caught:
            await client.request(method, {"path": "a\x00b", **extra})
        assert caught.value.code == protocol.ERROR_INVALID_PARAMS, method


@pytest.mark.parametrize(
    ("method", "flag"),
    [
        (protocol.METHOD_FS_WRITE_FILE, "append"),
        (protocol.METHOD_FS_WRITE_FILE, "createParents"),
        (protocol.METHOD_FS_REMOVE, "recursive"),
        (protocol.METHOD_FS_CREATE_DIRECTORY, "recursive"),
    ],
)
async def test_non_boolean_flags_are_invalid_params(
    client: DaemonClient, root: Path, method: str, flag: str
) -> None:
    """布尔开关给了非布尔值是参数错误，不按真假值宽松解释。"""
    (root / "x").write_text("keep")
    params = {"path": "x", **_RAW_PARAMS[method], flag: "yes"}
    with pytest.raises(SandboxRemoteError) as caught:
        await client.request(method, params)
    assert caught.value.code == protocol.ERROR_INVALID_PARAMS
    assert (root / "x").read_text() == "keep"


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
    """替换失败（目标是目录）时报错，临时文件被删掉；往根目录本身写同样报错，根外不多出文件。"""
    (root / "d").mkdir()
    ws = DaemonWorkspace(client)
    with pytest.raises(OSError):  # noqa: PT011 —— 具体子类随平台而异
        await ws.write_bytes("d", b"x")
    assert [p.name for p in root.iterdir()] == ["d"]
    assert list((root / "d").iterdir()) == []
    with pytest.raises(SandboxRemoteError) as caught:
        await ws.write_bytes(".", b"x")
    assert caught.value.code == protocol.ERROR_IO
    assert list(tmp_path.iterdir()) == [root]


@pytest.mark.skipif(os.geteuid() == 0, reason="root 用户不受目录权限约束，判别不了")
async def test_writing_the_root_never_touches_its_parent(
    client: DaemonClient, root: Path, tmp_path: Path
) -> None:
    """往根目录本身写，守护进程不会去根目录的父目录（根外）建临时文件。

    父目录设为只读：若守护进程试图在那里建临时文件，得到的会是 -32020 而不是 -32022。
    """
    tmp_path.chmod(0o555)
    try:
        with pytest.raises(SandboxRemoteError) as caught:
            await DaemonWorkspace(client).write_bytes(".", b"x")
        assert caught.value.code == protocol.ERROR_IO
    finally:
        tmp_path.chmod(0o755)


async def test_overwrite_keeps_mode_and_new_files_follow_umask(
    client: DaemonClient, root: Path
) -> None:
    """临时文件的 0600 不带到目标上：覆盖保持原 rwx 权限位（可执行位不丢），新建按 umask。

    两种情形都与直接 ``open`` 写入的结果一致；setuid 这类特殊位不保留（只取 ``& 0o777``）。
    """
    ws = DaemonWorkspace(client)
    script = root / "run.sh"
    script.write_text("echo old\n")
    script.chmod(0o755)
    await ws.write_bytes("run.sh", b"echo new\n")
    assert stat.S_IMODE(script.stat().st_mode) == 0o755
    script.chmod(0o4755)
    await ws.write_bytes("run.sh", b"echo newer\n")
    assert stat.S_IMODE(script.stat().st_mode) == 0o755
    await ws.write_bytes("fresh.txt", b"x")
    assert stat.S_IMODE((root / "fresh.txt").stat().st_mode) == 0o666 & ~_umask()


async def test_overwrite_keeps_the_owner_and_does_not_fail(
    client: DaemonClient, root: Path
) -> None:
    """覆盖是替换（inode 变了），属主与属组不变，非 root 守护进程也不因属主报错。"""
    target = root / "owned.txt"
    target.write_text("old")
    before = target.stat()
    await DaemonWorkspace(client).write_bytes("owned.txt", b"new")
    after = target.stat()
    assert after.st_ino != before.st_ino
    assert (after.st_uid, after.st_gid) == (before.st_uid, before.st_gid)


@pytest.mark.skipif(os.geteuid() != 0, reason="只有 root 守护进程能把属主改回原值")
async def test_root_daemon_restores_the_original_owner(client: DaemonClient, root: Path) -> None:
    """守护进程以 root 运行：覆盖后保持原属主与原权限；新建文件属主不动（就是 root）。"""
    target = root / "owned.txt"
    target.write_text("old")
    os.chown(target, 12345, 23456)
    target.chmod(0o640)
    ws = DaemonWorkspace(client)
    await ws.write_bytes("owned.txt", b"new")
    info = target.stat()
    assert (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) == (12345, 23456, 0o640)
    await ws.write_bytes("fresh.txt", b"x")
    assert (root / "fresh.txt").stat().st_uid == 0


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


async def test_reading_a_directory_as_a_file_fails(client: DaemonClient, root: Path) -> None:
    """把目录当文件读：是 OSError，但既不是「不存在」也不是「无权」；内核 file_read 报不是文件。"""
    (root / "d").mkdir()
    ws = DaemonWorkspace(client)
    with pytest.raises(OSError) as caught:  # noqa: PT011 —— 具体子类随平台而异
        await ws.read_bytes("d")
    assert not isinstance(caught.value, FileNotFoundError | PermissionError)
    result = await _call(taifeng.make_file_read_tool(workspace=ws), path="d")
    assert result.is_error and result.data.get("reason") == "not_found"


class _Canned:
    """桩客户端：握手信息可定制，每个请求都返回同一份响应。"""

    def __init__(self, response: dict[str, Any], root: object = "/work") -> None:
        """记下要返回的响应与握手信息里的根目录。"""
        self.server_info: dict[str, Any] = {"root": root}
        self._response = response

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """不管发什么都返回同一份响应。"""
        return self._response


@pytest.mark.parametrize(
    ("operation", "response"),
    [
        ("metadata", {"exists": True, "isFile": True}),
        ("metadata", {"exists": "yes"}),
        ("list_directory", {"entries": "a.txt"}),
        ("list_directory", {"entries": ["a.txt"]}),
        ("list_directory", {"entries": [{"name": "a.txt", "isDirectory": False, "isFile": True}]}),
        ("read_bytes", {"data": 1, "eof": True}),
        ("read_bytes", {"data": "aGk="}),
        ("read_bytes", {"data": "not base64!", "eof": True}),
    ],
)
async def test_malformed_responses_are_protocol_errors(
    operation: str, response: dict[str, Any]
) -> None:
    """响应字段缺失或类型不对：SandboxProtocolError，不拿默认值顶上。"""
    ws = DaemonWorkspace(cast("DaemonClient", _Canned(response)))
    with pytest.raises(SandboxProtocolError):
        await getattr(ws, operation)("a.txt")


@pytest.mark.parametrize("bad_root", [None, "", "work", "//work", "/work/", "/work/../x", 7])
def test_root_must_be_a_canonical_absolute_path(bad_root: object) -> None:
    """握手信息里没有根目录、或它不是规范的绝对路径：SandboxProtocolError，不猜默认值。"""
    ws = DaemonWorkspace(cast("DaemonClient", _Canned({}, root=bad_root)))
    with pytest.raises(SandboxProtocolError):
        _ = ws.root
    with pytest.raises(SandboxProtocolError):
        ws.resolve("a.txt")


def test_resolve_collapses_leading_slashes() -> None:
    """开头的多个 ``/`` 规整成一个：``//work/x`` 就是 ``/work/x``，``//etc`` 照样越界。"""
    ws = DaemonWorkspace(cast("DaemonClient", _Canned({})))
    assert ws.resolve("//work/x") == ws.resolve("///work//x") == "/work/x"
    with pytest.raises(taifeng.WorkspacePathError):
        ws.resolve("//etc/passwd")
    assert DaemonWorkspace(cast("DaemonClient", _Canned({}, root="/"))).resolve("//x") == "/x"


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
