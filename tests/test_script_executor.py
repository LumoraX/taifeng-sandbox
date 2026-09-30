"""``SandboxedScriptExecutor``：对齐 taifeng ``script-execution`` 契约的行为测试。

用 taifeng 自带的 ``LocalCommandExecutor`` 作为启动后端，验证的是本包的监督逻辑
（超时 / 取消 / 截断 / 参数展开）；与隔离后端组合的用例在 macOS 上用 seatbelt 真跑。
"""

from __future__ import annotations

import asyncio
import sys
import time
from typing import TYPE_CHECKING, Any

import pytest
from taifeng import (
    CancellationToken,
    CommandSpec,
    LocalCommandExecutor,
    ScriptDescriptor,
    ScriptExecutionError,
    ScriptExecutor,
    ScriptInvocation,
)

from taifeng_sandbox import (
    SandboxedScriptExecutor,
    SandboxPolicy,
    python_script_executor,
    shell_script_executor,
)
from taifeng_sandbox.local import SeatbeltCommandExecutor
from tests.conftest import requires_macos

if TYPE_CHECKING:
    from pathlib import Path

    from taifeng import CommandProcess


def _script(
    tmp_path: Path,
    body: str,
    *,
    name: str = "run.sh",
    language: str = "shell",
    timeout: float = 10.0,
    max_output: int = 1024,
    schema: dict[str, Any] | None = None,
) -> ScriptDescriptor:
    """在临时目录写一个脚本并返回其描述。"""
    path = tmp_path / name
    path.write_text(body)
    return ScriptDescriptor(
        skill_id="demo",
        name=name,
        path=path,
        language=language,  # type: ignore[arg-type]
        args_schema=schema or {"type": "object"},
        timeout_seconds=timeout,
        max_output_bytes=max_output,
    )


def _invoke(
    descriptor: ScriptDescriptor,
    args: dict[str, Any] | None = None,
    cancel: CancellationToken | None = None,
) -> ScriptInvocation:
    """构造一次调用。"""
    return ScriptInvocation(
        descriptor=descriptor, args=args or {}, cancel=cancel or CancellationToken()
    )


def test_satisfies_kernel_protocol() -> None:
    """满足 taifeng ``ScriptExecutor`` 协议。"""
    assert isinstance(shell_script_executor(LocalCommandExecutor()), ScriptExecutor)


async def test_success_result(tmp_path: Path) -> None:
    """正常退出：退出码 0，输出取回。"""
    descriptor = _script(tmp_path, "echo out; echo err 1>&2\n")
    result = await shell_script_executor(LocalCommandExecutor()).execute(_invoke(descriptor))
    assert result.ok
    assert result.stdout.strip() == "out"
    assert result.stderr.strip() == "err"
    assert not result.truncated


async def test_nonzero_exit_is_a_result_not_an_exception(tmp_path: Path) -> None:
    """非零退出码走结果返回，不抛异常。"""
    descriptor = _script(tmp_path, "exit 7\n")
    result = await shell_script_executor(LocalCommandExecutor()).execute(_invoke(descriptor))
    assert result.exit_code == 7
    assert not result.ok
    assert not result.killed


async def test_args_follow_schema_order(tmp_path: Path) -> None:
    """参数按 schema 声明顺序展开，未声明的追加在后；含空格与元字符的值保持完整。"""
    descriptor = _script(
        tmp_path,
        'for a in "$@"; do echo "[$a]"; done\n',
        schema={"type": "object", "properties": {"first": {}, "second": {}}},
    )
    args = {"extra": "z", "second": "b; rm -rf /", "first": "a b"}
    result = await shell_script_executor(LocalCommandExecutor()).execute(_invoke(descriptor, args))
    assert result.stdout.splitlines() == ["[a b]", "[b; rm -rf /]", "[z]"]


async def test_working_directory_is_script_directory(tmp_path: Path) -> None:
    """工作目录是脚本所在目录。"""
    descriptor = _script(tmp_path, "pwd -P\n")
    result = await shell_script_executor(LocalCommandExecutor()).execute(_invoke(descriptor))
    assert result.stdout.strip() == str(tmp_path.resolve())


async def test_timeout_kills_and_flags(tmp_path: Path) -> None:
    """超时：强杀并标记 ``is_timeout`` 与 ``killed``。"""
    descriptor = _script(tmp_path, "echo begin; exec sleep 30\n", timeout=0.5)
    started = time.monotonic()
    result = await shell_script_executor(LocalCommandExecutor()).execute(_invoke(descriptor))
    assert time.monotonic() - started < 10
    assert result.is_timeout
    assert result.killed
    assert not result.ok


async def test_cancel_kills_without_timeout_flag(tmp_path: Path) -> None:
    """取消：强杀并标记 ``killed``，但不算超时。"""
    descriptor = _script(tmp_path, "exec sleep 30\n", timeout=30)
    cancel = CancellationToken()
    executor = shell_script_executor(LocalCommandExecutor())
    task = asyncio.ensure_future(executor.execute(_invoke(descriptor, cancel=cancel)))
    await asyncio.sleep(0.3)
    cancel.cancel()
    result = await asyncio.wait_for(task, timeout=10)
    assert result.killed
    assert not result.is_timeout


async def test_cancelled_before_start_raises(tmp_path: Path) -> None:
    """启动前已取消：不启动进程，取消向上传播。"""
    descriptor = _script(tmp_path, "echo never\n")
    cancel = CancellationToken()
    cancel.cancel()
    with pytest.raises(asyncio.CancelledError):
        await shell_script_executor(LocalCommandExecutor()).execute(
            _invoke(descriptor, cancel=cancel)
        )


async def test_output_truncated_per_stream(tmp_path: Path) -> None:
    """stdout / stderr 各自按上限截断。"""
    descriptor = _script(
        tmp_path,
        "head -c 5000 /dev/zero | tr '\\0' 'a'; echo short 1>&2\n",
        max_output=100,
    )
    result = await shell_script_executor(LocalCommandExecutor()).execute(_invoke(descriptor))
    assert result.truncated
    assert len(result.stdout) == 100
    assert result.stderr.strip() == "short"


async def test_missing_interpreter_reports_spawn_failure(tmp_path: Path) -> None:
    """解释器不存在：按 taifeng 默认实现的口径返回 -2，而不是抛异常。"""
    descriptor = _script(tmp_path, "echo hi\n")
    executor = SandboxedScriptExecutor(
        LocalCommandExecutor(), interpreter=("/nonexistent/interpreter",)
    )
    result = await executor.execute(_invoke(descriptor))
    assert result.exit_code == -2
    assert result.stderr.startswith("spawn_failed")


async def test_system_level_failure_raises_script_execution_error(tmp_path: Path) -> None:
    """执行器自身不可用属系统级失败，抛 ``ScriptExecutionError``。"""

    class BrokenExecutor:
        """模拟隔离后端不可用。"""

        async def start(self, spec: CommandSpec) -> CommandProcess:
            """总是启动失败。"""
            raise PermissionError("sandbox backend unavailable")

    descriptor = _script(tmp_path, "echo hi\n")
    with pytest.raises(ScriptExecutionError) as caught:
        await shell_script_executor(BrokenExecutor()).execute(_invoke(descriptor))
    assert caught.value.descriptor is descriptor
    assert isinstance(caught.value.cause, PermissionError)


async def test_host_environment_not_inherited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """脚本拿不到宿主环境变量，只有构造时给定的那些。"""
    monkeypatch.setenv("HOST_ONLY_SECRET", "leak-me")
    descriptor = _script(tmp_path, 'echo "[${HOST_ONLY_SECRET}][${GIVEN}]"\n')
    executor = shell_script_executor(
        LocalCommandExecutor(), env={"PATH": "/usr/bin:/bin", "GIVEN": "ok"}
    )
    result = await executor.execute(_invoke(descriptor))
    assert result.stdout.strip() == "[][ok]"


async def test_path_mapper_rewrites_script_and_cwd(tmp_path: Path) -> None:
    """路径映射同时作用于脚本路径与工作目录。"""
    descriptor = _script(tmp_path, "echo hi\n")
    executor = shell_script_executor(
        LocalCommandExecutor(),
        path_mapper=lambda path: "/sandbox/" + path.name,
    )
    spec = executor.build_spec(_invoke(descriptor))
    assert spec.command == "/bin/sh /sandbox/run.sh"
    assert spec.cwd == "/sandbox/" + tmp_path.name
    assert spec.shell is False


async def test_python_script(tmp_path: Path) -> None:
    """Python 脚本经同一套监督逻辑运行。"""
    descriptor = _script(
        tmp_path,
        "import sys\nprint(sum(int(a) for a in sys.argv[1:]))\n",
        name="add.py",
        language="python",
        schema={"type": "object", "properties": {"a": {}, "b": {}}},
    )
    executor = python_script_executor(LocalCommandExecutor(), python=sys.executable)
    result = await executor.execute(_invoke(descriptor, {"a": 2, "b": 40}))
    assert result.ok, result.stderr
    assert result.stdout.strip() == "42"


@requires_macos
async def test_script_confined_by_seatbelt(tmp_path: Path) -> None:
    """与 seatbelt 组合：脚本只能写工作区，写到 skill 目录外即失败。"""
    skill_dir = tmp_path / "skill"
    workspace = tmp_path / "ws"
    skill_dir.mkdir()
    workspace.mkdir()
    escape = tmp_path / "escape.txt"
    descriptor = _script(
        skill_dir,
        f"echo ok > {workspace / 'out.txt'} || exit 3\necho leak > {escape} || exit 4\n",
    )
    backend = SeatbeltCommandExecutor(SandboxPolicy.workspace_write(workspace))
    result = await shell_script_executor(backend).execute(_invoke(descriptor))
    assert result.exit_code == 4
    assert (workspace / "out.txt").read_text().strip() == "ok"
    assert not escape.exists()
