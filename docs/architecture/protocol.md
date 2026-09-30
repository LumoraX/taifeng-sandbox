# 线协议第 1 版（活文档）

> 宿主侧客户端与沙盒内守护进程之间的协议。设计取舍见 [ADR 0003](../decisions/0003-protocol-v1-and-trust-boundaries.md)；
> 常量的权威定义在 `src/taifeng_sandbox/daemon/protocol.py`，守护进程的实现在同目录的 `server.py`。
>
> 想给别的执行环境写一个客户端、或者换一种语言实现守护进程，照着本文做。

## 传输与消息

- 任何可靠的双向字节流都可以：子进程的标准输入输出（`docker exec -i`、`kubectl exec -i`、`ssh`）、套接字。
- 一行一条消息，以 `\n` 结尾，UTF-8 编码的 JSON。单条消息上限 32 MiB。
- 消息形状是 JSON-RPC 2.0：请求带 `id`、`method`、`params`；响应带同一个 `id` 和 `result` 或 `error`；通知没有 `id`。
- 二进制内容（文件数据、进程输出）一律 base64。
- 守护进程的标准输出只用来发协议消息；诊断信息走标准错误。

```
→ {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1, "clientName": "taifeng-sandbox"}}
← {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1, "serverName": "…", "platform": "linux", "pythonVersion": "3.12.7", "root": "/workspace", "pid": 17}}
```

## 生命周期

1. 宿主在执行环境里启动守护进程：`python3 -c <源码> --root <根目录>`。`--root` 是文件方法能访问的范围，也是进程的缺省工作目录。
2. 宿主发 `initialize`，核对协议版本。握手之前发别的方法会得到 `-32000`。
3. 之后可以任意交错地发进程与文件请求；请求之间互不阻塞。
4. 宿主发 `shutdown`，或者直接关闭连接。**连接关闭时（含宿主崩溃），守护进程杀掉名下全部进程再退出。**

## 方法

### `initialize`

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `protocolVersion` | 整数 | 必须等于 `1`，否则 `-32001` |
| `clientName` | 字符串 | 可选，仅用于诊断 |

返回 `protocolVersion`、`serverName`、`platform`、`pythonVersion`、`root`、`pid`。

### `shutdown`

无参数。守护进程回复后杀掉全部进程并退出。

### `process/start`

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `processId` | 字符串 | 由宿主分配，连接内唯一。重复是 `-32012` |
| `argv` | 字符串数组 | 直接执行，不经 shell。要跑 shell 命令就传 `["/bin/sh", "-c", "…"]` |
| `env` | 字符串到字符串的映射 | 进程的**完整**环境。守护进程不把自己的环境传下去 |
| `cwd` | 字符串 | 可选。省略用根目录。不受根目录约束（见 ADR 0003 决策 5） |

返回 `{"pid": …}`。找不到可执行文件是 `-32010`，其他启动失败是 `-32011`。

进程以新会话启动，标准输入接 `/dev/null`。启动之后，守护进程主动发通知：

| 通知 | 参数 | 说明 |
| --- | --- | --- |
| `process/output` | `processId`、`stream`（`stdout` 或 `stderr`）、`data`（base64） | 一块输出，最大 32 KiB |
| `process/exited` | `processId`、`exitCode` | 进程已退出。**一定排在这个进程的全部输出通知之后**，收到它就可以认定输出完整 |

### `process/kill`

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `processId` | 字符串 | |

强杀整个进程组。返回 `{"killed": true}`；进程已经结束或不存在返回 `{"killed": false}`，不算错——强杀与自然退出本来就可能竞争。

### 文件方法

路径可以是绝对路径或相对根目录的路径。**解析符号链接之后必须仍在根目录之内**，否则 `-32020`。

| 方法 | 参数 | 返回 |
| --- | --- | --- |
| `fs/readFile` | `path`；可选 `offset`（默认 0）、`length`（默认且最大 16 MiB） | `data`（base64）、`size`（文件总大小）、`eof` |
| `fs/writeFile` | `path`、`data`（base64，单次最大 16 MiB）；可选 `append`、`createParents` | `bytesWritten` |
| `fs/readDirectory` | `path` | `entries`：每项 `name`、`isDirectory`、`isFile`、`isSymlink` |
| `fs/getMetadata` | `path` | 不存在时 `{"exists": false}`；存在时另有 `isDirectory`、`isFile`、`size`、`modifiedAt` |
| `fs/createDirectory` | `path`；可选 `recursive`（连同父目录，已存在不算错） | `{}` |
| `fs/remove` | `path`；可选 `recursive` | `{}`。根目录本身不允许删 |

大文件用 `offset` / `length` 分段读、用 `append` 分段写。

## 错误码

| 码 | 含义 |
| --- | --- |
| `-32700` | 不是合法的 JSON |
| `-32600` | 不是合法的请求 |
| `-32601` | 没有这个方法 |
| `-32602` | 参数不对 |
| `-32603` | 守护进程内部错误 |
| `-32000` | 还没握手 |
| `-32001` | 协议版本不支持 |
| `-32010` | 找不到可执行文件 |
| `-32011` | 进程启动失败 |
| `-32012` | `processId` 已存在 |
| `-32013` | 没有这个进程 |
| `-32020` | 路径在根目录之外，或没有权限 |
| `-32021` | 文件或目录不存在 |
| `-32022` | 其他读写错误 |
| `-32023` | 超过大小上限 |

宿主侧客户端把错误响应变成 `SandboxRemoteError`（带 `code`）；`DaemonWorkspace` 进一步把 `-32021` 还原成 `FileNotFoundError`、`-32020` 还原成 `PermissionError`。

## 版本

协议版本是一个整数，当前为 `1`。不兼容的变更升版本号；守护进程只接受与自己相同的版本。守护进程的源码随本包分发、由宿主在连接时注入，所以客户端与守护进程总是同一个版本，不存在新旧混用。
