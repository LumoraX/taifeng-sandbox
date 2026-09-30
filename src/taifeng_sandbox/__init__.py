"""taifeng-sandbox —— taifeng 的执行隔离适配层。

把 taifeng 定义的执行类协议（``CommandExecutor`` / ``ScriptExecutor``）接到具体隔离后端：
本机 OS 级隔离、容器、远端沙盒。审批、命令黑名单、env 白名单、超时、输出截断、取消等
保证留在 taifeng 工具层，本包只决定「进程在哪里、以什么隔离方式运行」（taifeng ADR 0051）。

各后端在自己的子包里，按需导入：

- ``taifeng_sandbox.local``：本机 seatbelt / bubblewrap；
- ``taifeng_sandbox.daemon``：沙盒内守护进程、线协议与宿主侧客户端；
- ``taifeng_sandbox.docker``：容器环境（拉起容器并在其中启动守护进程）。
"""

from __future__ import annotations

from importlib.metadata import version

from taifeng_sandbox.errors import (
    SandboxError,
    SandboxPolicyError,
    SandboxProtocolError,
    SandboxRemoteError,
    SandboxUnavailableError,
)
from taifeng_sandbox.policy import SandboxPolicy
from taifeng_sandbox.script import (
    SandboxedScriptExecutor,
    python_script_executor,
    shell_script_executor,
)

__all__ = [
    "SandboxError",
    "SandboxPolicy",
    "SandboxPolicyError",
    "SandboxProtocolError",
    "SandboxRemoteError",
    "SandboxUnavailableError",
    "SandboxedScriptExecutor",
    "__version__",
    "python_script_executor",
    "shell_script_executor",
]

# 版本单一来源是 pyproject.toml：从发行元数据读取，避免两处手改漂移
__version__ = version("taifeng-sandbox")
