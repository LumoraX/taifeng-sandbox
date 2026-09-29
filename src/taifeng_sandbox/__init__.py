"""taifeng-sandbox —— taifeng 的执行隔离适配层。

把 taifeng 定义的执行类协议（``CommandExecutor`` / ``ScriptExecutor``）接到具体隔离后端：
本机 OS 级隔离、容器、远端沙盒。审批、命令黑名单、env 白名单、超时、输出截断、取消等
保证留在 taifeng 工具层，本包只决定「进程在哪里、以什么隔离方式运行」（taifeng ADR 0051）。
"""

from __future__ import annotations

from importlib.metadata import version

__all__ = ["__version__"]

# 版本单一来源是 pyproject.toml：从发行元数据读取，避免两处手改漂移
__version__ = version("taifeng-sandbox")
