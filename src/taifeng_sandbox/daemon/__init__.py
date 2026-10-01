"""沙盒内守护进程、线协议与宿主侧客户端（ADR 0002）。"""

from __future__ import annotations

from taifeng_sandbox.daemon.client import DaemonClient, daemon_source
from taifeng_sandbox.daemon.executor import DaemonCommandExecutor
from taifeng_sandbox.daemon.process import RemoteProcess
from taifeng_sandbox.daemon.protocol import PROTOCOL_VERSION
from taifeng_sandbox.daemon.streaming import StreamingRemoteProcess
from taifeng_sandbox.daemon.transport import StdioTransport, Transport
from taifeng_sandbox.daemon.workspace import DaemonWorkspace

__all__ = [
    "PROTOCOL_VERSION",
    "DaemonClient",
    "DaemonCommandExecutor",
    "DaemonWorkspace",
    "RemoteProcess",
    "StdioTransport",
    "StreamingRemoteProcess",
    "Transport",
    "daemon_source",
]
