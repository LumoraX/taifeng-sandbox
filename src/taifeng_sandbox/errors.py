"""本包的异常类型。

启动类失败一律继承 ``OSError``：taifeng 的 ``CommandExecutor`` 契约规定「启动失败 SHALL 抛
``OSError``」，工具层据此返回 ``spawn_error``。隔离后端不可用时必须失败，不允许悄悄退回到
无隔离执行（fail-closed）。
"""

from __future__ import annotations


class SandboxError(OSError):
    """隔离执行相关失败的基类。"""


class SandboxUnavailableError(SandboxError):
    """所需的隔离后端在当前环境不可用（缺可执行文件、平台不支持、守护进程连不上）。"""


class SandboxPolicyError(ValueError):
    """隔离策略本身不合法（相对路径、互相矛盾的根目录等）。"""


class SandboxProtocolError(SandboxError):
    """与沙盒内守护进程的线协议交互失败（版本不兼容、响应畸形、连接中断）。"""


class SandboxRemoteError(SandboxError):
    """守护进程明确返回的错误。

    Attributes:
        code: 线协议错误码（见 ``taifeng_sandbox.daemon.protocol``）。
    """

    def __init__(self, code: int, message: str) -> None:
        """记录错误码并把消息交给 ``OSError``。"""
        super().__init__(message)
        self.code = code


__all__ = [
    "SandboxError",
    "SandboxPolicyError",
    "SandboxProtocolError",
    "SandboxRemoteError",
    "SandboxUnavailableError",
]
