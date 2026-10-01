# 线协议第 2 版（活文档）

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
→ {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 2, "clientName": "taifeng-sandbox"}}
← {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 2, "serverName": "…", "platform": "linux", "pythonVersion": "3.12.7", "root": "/workspace", "pid": 17}}
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
| `protocolVersion` | 整数 | 必须等于 `2`，否则 `-32001` |
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
| `stdin` | 布尔 | 可选，省略或 `null` 视为 `false`；给了其他非布尔值是 `-32602`。为 `true` 时标准输入接管道，之后用 `process/write` 写、`process/closeStdin` 关；否则接 `/dev/null` |

返回 `{"pid": …}`。找不到可执行文件是 `-32010`，其他启动失败是 `-32011`。

进程以新会话启动（自成一个进程组）。启动之后，守护进程主动发通知：

| 通知 | 参数 | 说明 |
| --- | --- | --- |
| `process/output` | `processId`、`stream`（`stdout` 或 `stderr`）、`data`（base64） | 一块输出，最大 32 KiB |
| `process/exited` | `processId`、`exitCode` | 进程已退出。**一定排在这个进程的全部输出通知之后**，收到它就可以认定输出完整 |

「进程已经结束」指两路输出都读到 EOF、主进程也已退出——守护进程这时把它移出进程表，然后才发 `process/exited`。此后对这个 `processId` 的 `process/write`、`process/closeStdin` 是 `-32013`，`process/kill` 返回 `{"killed": false}`。

### `process/write`

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `processId` | 字符串 | |
| `data` | 字符串 | 要写进标准输入的内容，base64，可为空串。解码后单次最大 16 MiB，超过是 `-32023` |

返回 `{"bytesWritten": …}`。**背压**：守护进程写缓冲回落到水位线以下才回复，不代表进程已读到。进程一直不读时，这次写会一直等，直到进程读走数据，或者进程被杀、对端断开（这时返回 `-32022`）。

| 情形 | 错误码 |
| --- | --- |
| 没有这个进程（含已结束） | `-32013` |
| 启动时没给 `stdin: true`；`data` 不是合法 base64 | `-32602` |
| 标准输入已经关闭或对端已断开，或写入失败 | `-32022` |

**写入顺序由宿主保证**：每次写都等到响应再发下一次。守护进程为每条请求各起一个任务；每个进程有一把写锁，并发到达的写按到达顺序各自整块写入，不会交错，但宿主仍应串行写。

守护进程跑在 Python 3.9 上时，asyncio 给子进程标准输入接的是 Unix socketpair 而不是管道（3.12 起只在 AIX 上这样）。写入、关闭、对端断开的行为相同，只是缓冲大小不同（macOS 上 socketpair 8 KiB、管道 64 KiB），背压来得更早。

### `process/closeStdin`

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `processId` | 字符串 | |

关闭标准输入，立即返回 `{}`，进程读完已送达的数据后读到 EOF。

关闭是发起式的：守护进程缓冲里尚未进管道的数据在后台继续写，进程不读或提前退出时丢弃，与本机管道一致。守护进程不等关闭完成就回复（进程不读时那会一直等到进程被杀）；后台关闭出错只写进守护进程的标准错误，不再报给宿主。

没有这个进程是 `-32013`。启动时没要标准输入的进程、已经关过的标准输入再关，都返回 `{}`，不算错。

### `process/kill`

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `processId` | 字符串 | |

强杀整个进程组。主进程（例如 shell）已经退出、但它派生的子进程还占着输出管道时，进程仍在进程表里，同样按组杀到（与 taifeng ADR 0108 一致）。返回 `{"killed": true}`；进程已经结束或不存在返回 `{"killed": false}`，不算错——强杀与自然退出本来就可能竞争。

### 文件方法

路径可以是绝对路径或相对根目录的路径。**解析符号链接之后必须仍在根目录之内**，否则 `-32024`。

| 方法 | 参数 | 返回 |
| --- | --- | --- |
| `fs/readFile` | `path`；可选 `offset`（默认 0）、`length`（默认且最大 16 MiB） | `data`（base64）、`size`（文件总大小）、`eof` |
| `fs/writeFile` | `path`、`data`（base64，单次最大 16 MiB）；可选 `append`、`createParents` | `bytesWritten` |
| `fs/readDirectory` | `path` | `entries`：每项 `name`、`isDirectory`、`isFile`、`isSymlink`（不跟随符号链接判定），按名字排序 |
| `fs/getMetadata` | `path` | 不存在（含路径中间某一段是文件）时 `{"exists": false}`；存在时另有 `isDirectory`、`isFile`、`size`、`modifiedAt`（跟随符号链接） |
| `fs/createDirectory` | `path`；可选 `recursive`（连同父目录，已存在不算错） | `{}` |
| `fs/remove` | `path`；可选 `recursive` | `{}`。根目录本身不允许删（`-32020`）；非空目录不给 `recursive` 是 `-32022` |

`fs/writeFile` 不给 `append` 时是**原子的整文件替换**：守护进程在目标所在目录写一个 `.tmp-` 开头的临时文件，写完再改名到目标，读者只会看到旧内容或新内容；失败时删掉临时文件。替换后的权限与直接写入一致：覆盖保持原文件的权限（可执行位不丢），新建按守护进程的 umask。`append` 为真时直接追加，不经临时文件。`path` 是根目录本身时是 `-32022`（不会在根目录之外建临时文件）。

大文件用 `offset` / `length` 分段读、用 `append` 分段写。分段写只有第一段是原子替换，整体不是原子的。

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
| `-32020` | 没有权限：操作系统拒绝访问，或要删除根目录本身 |
| `-32021` | 文件或目录不存在 |
| `-32022` | 其他读写错误 |
| `-32023` | 超过大小上限 |
| `-32024` | 路径（解析符号链接之后）在根目录之外 |

宿主侧客户端把错误响应变成 `SandboxRemoteError`（带 `code`）；`DaemonWorkspace` 进一步把 `-32021` 还原成 `FileNotFoundError`、`-32024` 还原成 `taifeng.WorkspacePathError`、`-32020` 还原成 `PermissionError`（前者是后者的子类）。

## 版本

协议版本是一个整数，当前为 `2`。不兼容的变更升版本号；守护进程只接受与自己相同的版本。守护进程的源码随本包分发、由宿主在连接时注入，所以客户端与守护进程总是同一个版本，不存在新旧混用。

| 版本 | 相对上一版的变化 |
| --- | --- |
| `2` | 进程可以接标准输入：`process/start` 新增 `stdin` 参数；新增 `process/write`、`process/closeStdin`。路径越界从 `-32020` 中分出来，单独用 `-32024`；`fs/writeFile` 的整文件写入改为原子替换；`fs/getMetadata` 遇到路径中间某一段是文件时返回 `{"exists": false}` |
| `1` | 初版 |
