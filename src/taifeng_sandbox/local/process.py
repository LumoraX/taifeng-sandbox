"""本机进程封装：让 ``kill`` 作用于整个进程组。

``/bin/sh -c "a | b"`` 会再派生子进程；只杀直接子进程会留下孤儿继续占用资源、继续产生副作用。
本机后端统一以新会话启动（PID 即进程组号），``kill`` 时对整个进程组发信号。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    import taifeng

# 输出流的单行读取上限，也决定读缓冲暂停读取的水位（2 倍于它）。asyncio 默认 64 KiB：经本机执行器
# 起的 MCP 连接器单条响应超过它时，内核的 readline 抛 ValueError，读循环随之崩掉。取值与守护进程
# 执行器每路未读字节的默认上限（``daemon.process.DEFAULT_MAX_BUFFER_BYTES``）对齐——远端流式进程
# 一行也得整行放进那个缓冲才读得出——两类后端能交出的单行一样长。不 import 它，免得本机后端依赖
# 守护进程包；``tests/local/test_streaming.py`` 守护两边一致。
STREAM_LIMIT_BYTES = 16 * 1024 * 1024


class ProcessGroup:
    """满足 taifeng ``CommandProcess`` 与 ``StreamingCommandProcess`` 协议的进程组句柄。

    ``stdin`` 仅在以 ``stdin=True`` 启动时非 None；``stdout`` / ``stderr`` 始终是管道。
    """

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        """包装一个以 ``start_new_session=True`` 启动的子进程。"""
        self._proc = proc
        self._collected = False

    @property
    def pid(self) -> int:
        """子进程 PID（同时是进程组号）。"""
        return self._proc.pid

    @property
    def stdin(self) -> taifeng.CommandInput | None:
        """标准输入的写入端；未以 ``stdin=True`` 启动时为 None。"""
        return self._proc.stdin

    @property
    def stdout(self) -> taifeng.CommandOutput | None:
        """标准输出的读取端。"""
        return self._proc.stdout

    @property
    def stderr(self) -> taifeng.CommandOutput | None:
        """标准错误的读取端。"""
        return self._proc.stderr

    @property
    def returncode(self) -> int | None:
        """退出码；未结束为 None。"""
        return self._proc.returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        """读完 stdout / stderr 并等待退出。"""
        output = await self._proc.communicate()
        # 管道读到 EOF 且进程已退出：没有成员还需要杀，此后 kill 不再发信号
        self._collected = True
        return output

    async def wait(self) -> int:
        """等待退出，返回退出码。"""
        return await self._proc.wait()

    def kill(self) -> None:
        """对整个进程组发 SIGKILL；输出已收完的进程上是空操作。

        只看 ``_collected`` 不看 ``returncode``：shell 先退出、子进程仍占着管道时，
        ``returncode`` 已有值，但进程组里还有成员需要杀（内核 ADR 0108）。
        进程组不存在或无权按组杀时退回只杀主进程。
        """
        if self._collected:
            return
        try:
            os.killpg(self._proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            if self._proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    self._proc.kill()


async def spawn_group(
    argv: Sequence[str],
    *,
    cwd: str | None,
    env: Mapping[str, str],
    stdin: bool = False,
) -> ProcessGroup:
    """以新会话启动子进程，stdout / stderr 走管道。

    ``stdin=True`` 时 stdin 也走管道，可持续写入；否则关闭（读到 EOF）。``readline`` 的单行上限是
    ``STREAM_LIMIT_BYTES``。

    ``env`` 作为完整环境传入，不叠加宿主环境变量（taifeng ``CommandExecutor`` 契约）。
    启动失败抛 ``OSError``，由 taifeng 工具层转成 ``spawn_error``。
    """
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE if stdin else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=dict(env),
        close_fds=True,
        start_new_session=True,
        limit=STREAM_LIMIT_BYTES,
    )
    return ProcessGroup(proc)


__all__ = ["ProcessGroup", "spawn_group"]
