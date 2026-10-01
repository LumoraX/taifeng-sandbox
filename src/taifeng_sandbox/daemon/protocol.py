"""宿主与沙盒内守护进程之间的线协议（版本 2）。

设计参照 codex ``exec-server-protocol``（Apache-2.0）：JSON-RPC 2.0 消息形状、先握手再调用、
进程输出与退出走通知。差异：协议由本仓自有并做版本管理（ADR 0002 否决了直接依赖 codex
内部协议）；传输是按行分隔的 JSON，一行一条消息；只保留进程与文件两组方法。

v2 相对 v1：进程可以接标准输入——``process/start`` 新增可选布尔参数 ``stdin``，为真时标准输入接管道
（v1 固定接 ``/dev/null``）；新增 ``process/write``（向标准输入写一段数据）与
``process/closeStdin``（关闭标准输入）两个方法。

消息形状::

    请求    {"jsonrpc": "2.0", "id": 1, "method": "process/start", "params": {...}}
    成功    {"jsonrpc": "2.0", "id": 1, "result": {...}}
    失败    {"jsonrpc": "2.0", "id": 1, "error": {"code": -32001, "message": "..."}}
    通知    {"jsonrpc": "2.0", "method": "process/output", "params": {...}}

二进制内容（进程输出、文件内容）一律 base64 编码放在 ``data`` 字段。

守护进程 ``server.py`` 必须能脱离本包单文件运行，所以它自带一份同样的常量；
``tests/daemon/test_protocol.py`` 守护两边一致。
"""

from __future__ import annotations

from typing import Final

PROTOCOL_VERSION: Final = 2

# --- 方法名 ---
METHOD_INITIALIZE: Final = "initialize"
METHOD_SHUTDOWN: Final = "shutdown"
METHOD_PROCESS_START: Final = "process/start"
METHOD_PROCESS_WRITE: Final = "process/write"
METHOD_PROCESS_CLOSE_STDIN: Final = "process/closeStdin"
METHOD_PROCESS_KILL: Final = "process/kill"
METHOD_FS_READ_FILE: Final = "fs/readFile"
METHOD_FS_WRITE_FILE: Final = "fs/writeFile"
METHOD_FS_READ_DIRECTORY: Final = "fs/readDirectory"
METHOD_FS_GET_METADATA: Final = "fs/getMetadata"
METHOD_FS_CREATE_DIRECTORY: Final = "fs/createDirectory"
METHOD_FS_REMOVE: Final = "fs/remove"

# --- 通知名（守护进程 → 宿主） ---
NOTIFY_PROCESS_OUTPUT: Final = "process/output"
NOTIFY_PROCESS_EXITED: Final = "process/exited"

# --- 错误码 ---
# JSON-RPC 保留段
ERROR_PARSE: Final = -32700
ERROR_INVALID_REQUEST: Final = -32600
ERROR_METHOD_NOT_FOUND: Final = -32601
ERROR_INVALID_PARAMS: Final = -32602
ERROR_INTERNAL: Final = -32603
# 本协议自定义段
ERROR_NOT_INITIALIZED: Final = -32000
ERROR_UNSUPPORTED_VERSION: Final = -32001
ERROR_SPAWN_NOT_FOUND: Final = -32010
ERROR_SPAWN_FAILED: Final = -32011
ERROR_PROCESS_EXISTS: Final = -32012
ERROR_PROCESS_UNKNOWN: Final = -32013
ERROR_ACCESS_DENIED: Final = -32020
ERROR_NOT_FOUND: Final = -32021
ERROR_IO: Final = -32022
ERROR_TOO_LARGE: Final = -32023

# 单条消息（一行）的字节上限；文件读写按此留出 base64 膨胀余量
MAX_MESSAGE_BYTES: Final = 32 * 1024 * 1024
# 单次文件读写的内容上限
MAX_FILE_BYTES: Final = 16 * 1024 * 1024

__all__ = [
    "ERROR_ACCESS_DENIED",
    "ERROR_INTERNAL",
    "ERROR_INVALID_PARAMS",
    "ERROR_INVALID_REQUEST",
    "ERROR_IO",
    "ERROR_METHOD_NOT_FOUND",
    "ERROR_NOT_FOUND",
    "ERROR_NOT_INITIALIZED",
    "ERROR_PARSE",
    "ERROR_PROCESS_EXISTS",
    "ERROR_PROCESS_UNKNOWN",
    "ERROR_SPAWN_FAILED",
    "ERROR_SPAWN_NOT_FOUND",
    "ERROR_TOO_LARGE",
    "ERROR_UNSUPPORTED_VERSION",
    "MAX_FILE_BYTES",
    "MAX_MESSAGE_BYTES",
    "METHOD_FS_CREATE_DIRECTORY",
    "METHOD_FS_GET_METADATA",
    "METHOD_FS_READ_DIRECTORY",
    "METHOD_FS_READ_FILE",
    "METHOD_FS_REMOVE",
    "METHOD_FS_WRITE_FILE",
    "METHOD_INITIALIZE",
    "METHOD_PROCESS_CLOSE_STDIN",
    "METHOD_PROCESS_KILL",
    "METHOD_PROCESS_START",
    "METHOD_PROCESS_WRITE",
    "METHOD_SHUTDOWN",
    "NOTIFY_PROCESS_EXITED",
    "NOTIFY_PROCESS_OUTPUT",
    "PROTOCOL_VERSION",
]
