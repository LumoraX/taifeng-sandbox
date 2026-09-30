"""``ScriptExecutor`` 的隔离实现：把 skill 脚本交给任意 ``CommandExecutor`` 运行。

taifeng 自带的 ``ShellScriptExecutor`` / ``PythonScriptExecutor`` 直接起本机子进程。本模块把
「启动」换成可注入的 ``CommandExecutor``（本机隔离、容器、远端沙盒均可），其余语义对齐
taifeng ``script-execution`` 契约：

- argv 数组展开，不拼 shell 字符串；
- 环境变量用构造时给定的最小集合，不继承宿主环境；
- 超时与取消都会终止进程，结果经 ``ScriptResult`` 返回而不是抛异常；
- stdout / stderr 各自按 ``max_output_bytes`` 截断。

与内核默认实现的差异：``CommandProcess`` 协议只有 ``kill``，所以终止是直接强杀，没有
SIGTERM 宽限期；输出在进程结束后一次性取回，不是流式读取。
"""

from __future__ import annotations

import asyncio
import contextlib
import shlex
import time
from typing import TYPE_CHECKING

from taifeng import CommandSpec, ScriptExecutionError, ScriptResult

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

    from taifeng import CommandExecutor, CommandProcess, ScriptInvocation

# 不继承宿主环境：脚本只拿到查找解释器所需的最小变量
DEFAULT_SCRIPT_ENV: Mapping[str, str] = {
    "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
    "LANG": "C.UTF-8",
}

# 强杀之后等待输出管道关闭的上限
_DRAIN_TIMEOUT_SECONDS = 2.0

# 与 taifeng 默认执行器一致：找不到可执行文件时的退出码
_SPAWN_NOT_FOUND_EXIT_CODE = -2
# 进程被强杀且拿不到真实退出码时的退出码（POSIX 下 SIGKILL 为 -9）
_KILLED_EXIT_CODE = -9


def ordered_args(inv: ScriptInvocation) -> list[str]:
    """按 ``args_schema.properties`` 的声明顺序展开参数，未声明的参数追加在后。"""
    properties = inv.descriptor.args_schema.get("properties") or {}
    ordered = [str(inv.args[key]) for key in properties if key in inv.args]
    ordered.extend(str(value) for key, value in inv.args.items() if key not in properties)
    return ordered


def _truncate(raw: bytes, limit: int) -> tuple[str, bool]:
    """按字节上限截断并有损解码。"""
    truncated = len(raw) > limit
    return raw[:limit].decode("utf-8", errors="replace"), truncated


class SandboxedScriptExecutor:
    """经 ``CommandExecutor`` 运行脚本的 ``ScriptExecutor`` 实现。"""

    def __init__(
        self,
        executor: CommandExecutor,
        *,
        interpreter: Sequence[str],
        env: Mapping[str, str] | None = None,
        path_mapper: Callable[[Path], str] | None = None,
    ) -> None:
        """
        Args:
            executor: 负责启动进程的执行器（决定隔离方式）。
            interpreter: 解释器 argv 前缀，如 ``("/bin/sh",)``、``("python3", "-u")``；
                空序列表示直接执行脚本文件。
            env: 脚本的完整环境变量；None 用 ``DEFAULT_SCRIPT_ENV``。
            path_mapper: 把宿主机上的脚本路径映射成执行环境里的路径。容器后端若未按相同
                路径挂载 skill 目录，需要提供；None 表示两边路径一致。
        """
        self._executor = executor
        self._interpreter = tuple(interpreter)
        self._env = dict(env) if env is not None else dict(DEFAULT_SCRIPT_ENV)
        self._map_path = path_mapper

    def _target_path(self, path: Path) -> str:
        """脚本（或其目录）在执行环境里的路径。"""
        if self._map_path is None:
            return path.as_posix()
        return self._map_path(path)

    def build_spec(self, inv: ScriptInvocation) -> CommandSpec:
        """把一次脚本调用翻译成 ``CommandSpec``（argv 模式，工作目录为脚本所在目录）。"""
        script = self._target_path(inv.descriptor.path)
        argv = [*self._interpreter, script, *ordered_args(inv)]
        return CommandSpec(
            command=shlex.join(argv),
            shell=False,
            cwd=self._target_path(inv.descriptor.path.parent),
            env=dict(self._env),
        )

    async def execute(self, inv: ScriptInvocation) -> ScriptResult:
        """运行脚本，等待结束、超时或取消。"""
        inv.cancel.raise_if_cancelled()
        started = time.monotonic()
        try:
            proc = await self._executor.start(self.build_spec(inv))
        except FileNotFoundError as exc:
            return ScriptResult(
                exit_code=_SPAWN_NOT_FOUND_EXIT_CODE,
                stdout="",
                stderr=f"spawn_failed: {exc}",
                duration_ms=_elapsed_ms(started),
            )
        except OSError as exc:
            raise ScriptExecutionError(
                f"无法启动脚本 {inv.descriptor.full_target!r}：{exc}",
                descriptor=inv.descriptor,
                cause=exc,
            ) from exc

        stdout, stderr, is_timeout, killed = await _supervise(proc, inv)
        limit = inv.descriptor.max_output_bytes
        stdout_text, stdout_truncated = _truncate(stdout, limit)
        stderr_text, stderr_truncated = _truncate(stderr, limit)
        exit_code = proc.returncode
        if exit_code is None:
            exit_code = _KILLED_EXIT_CODE
        return ScriptResult(
            exit_code=exit_code,
            stdout=stdout_text,
            stderr=stderr_text,
            duration_ms=_elapsed_ms(started),
            truncated=stdout_truncated or stderr_truncated,
            is_timeout=is_timeout,
            killed=killed,
        )


def _elapsed_ms(started: float) -> int:
    """自 ``started`` 起经过的毫秒数。"""
    return int((time.monotonic() - started) * 1000)


async def _supervise(
    proc: CommandProcess, inv: ScriptInvocation
) -> tuple[bytes, bytes, bool, bool]:
    """等待进程结束；超时或取消时强杀。返回 ``(stdout, stderr, is_timeout, killed)``。"""
    output_task = asyncio.ensure_future(proc.communicate())
    cancel_task = asyncio.ensure_future(inv.cancel.wait_cancelled())
    is_timeout = False
    killed = False
    try:
        done, _ = await asyncio.wait(
            {output_task, cancel_task},
            timeout=inv.descriptor.timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if output_task not in done:
            killed = True
            is_timeout = cancel_task not in done
            proc.kill()
        stdout, stderr = await _collect(output_task)
    finally:
        cancel_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cancel_task
    return stdout, stderr, is_timeout, killed


async def _collect(output_task: asyncio.Future[tuple[bytes, bytes]]) -> tuple[bytes, bytes]:
    """取回输出；强杀后管道迟迟不关时放弃等待，按空输出处理。"""
    try:
        return await asyncio.wait_for(asyncio.shield(output_task), timeout=_DRAIN_TIMEOUT_SECONDS)
    except TimeoutError:
        output_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await output_task
        return b"", b""


def shell_script_executor(
    executor: CommandExecutor,
    *,
    shell: str = "/bin/sh",
    env: Mapping[str, str] | None = None,
    path_mapper: Callable[[Path], str] | None = None,
) -> SandboxedScriptExecutor:
    """``language="shell"`` 的隔离执行器。"""
    return SandboxedScriptExecutor(
        executor, interpreter=(shell,), env=env, path_mapper=path_mapper
    )


def python_script_executor(
    executor: CommandExecutor,
    *,
    python: str = "python3",
    env: Mapping[str, str] | None = None,
    path_mapper: Callable[[Path], str] | None = None,
) -> SandboxedScriptExecutor:
    """``language="python"`` 的隔离执行器（``-u`` 关闭输出缓冲，``-I`` 隔离用户站点目录）。"""
    return SandboxedScriptExecutor(
        executor, interpreter=(python, "-u", "-I"), env=env, path_mapper=path_mapper
    )


__all__ = [
    "DEFAULT_SCRIPT_ENV",
    "SandboxedScriptExecutor",
    "ordered_args",
    "python_script_executor",
    "shell_script_executor",
]
