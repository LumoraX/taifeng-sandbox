"""本机进程封装：让 ``kill`` 作用于整个进程组。

``/bin/sh -c "a | b"`` 会再派生子进程；只杀直接子进程会留下孤儿继续占用资源、继续产生副作用。
本机后端统一以新会话启动（PID 即进程组号），``kill`` 时对整个进程组发信号。
"""

from __future__ import annotations

import asyncio
import os
import signal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


class ProcessGroup:
    """满足 taifeng ``CommandProcess`` 协议的进程组句柄。"""

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        """包装一个以 ``start_new_session=True`` 启动的子进程。"""
        self._proc = proc

    @property
    def pid(self) -> int:
        """子进程 PID（同时是进程组号）。"""
        return self._proc.pid

    @property
    def returncode(self) -> int | None:
        """退出码；未结束为 None。"""
        return self._proc.returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        """读完 stdout / stderr 并等待退出。"""
        return await self._proc.communicate()

    async def wait(self) -> int:
        """等待退出，返回退出码。"""
        return await self._proc.wait()

    def kill(self) -> None:
        """对整个进程组发 SIGKILL；进程组已不存在时退回只杀主进程。"""
        if self._proc.returncode is not None:
            return
        try:
            os.killpg(self._proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            # 进程组已经退出干净
            return
        except PermissionError:
            # 无权对进程组发信号（极少见）：至少杀掉主进程
            self._kill_main()

    def _kill_main(self) -> None:
        """只杀主进程；已退出则无事可做。"""
        try:
            self._proc.kill()
        except ProcessLookupError:
            return


async def spawn_group(
    argv: Sequence[str],
    *,
    cwd: str | None,
    env: Mapping[str, str],
) -> ProcessGroup:
    """以新会话启动子进程，stdout / stderr 走管道，stdin 关闭。

    ``env`` 作为完整环境传入，不叠加宿主环境变量（taifeng ``CommandExecutor`` 契约）。
    启动失败抛 ``OSError``，由 taifeng 工具层转成 ``spawn_error``。
    """
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=dict(env),
        close_fds=True,
        start_new_session=True,
    )
    return ProcessGroup(proc)


__all__ = ["ProcessGroup", "spawn_group"]
