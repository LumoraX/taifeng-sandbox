# 接口参考（活文档）

> 本包对外的全部接口。用法示例见仓库根目录的 [README](../../README.md)，线协议见 [protocol.md](protocol.md)。

## 隔离策略：`SandboxPolicy`

与后端无关的「这个进程能碰什么」。默认拒绝写、拒绝出网。

| 字段 | 默认 | 含义 |
| --- | --- | --- |
| `read_scope` | `"all"` | `"all"`：整个文件系统可读。`"restricted"`：只有系统运行时目录（`/usr` 等）、`readable_roots`、`writable_roots` 可读 |
| `readable_roots` | 空 | `restricted` 下额外放开的只读目录 |
| `writable_roots` | 空 | 可写目录。可写就可读 |
| `unreadable_roots` | 空 | 无论如何都不可读写的目录，比如凭据目录 |
| `network` | `False` | 是否允许出网 |

路径都必须是绝对路径；可写目录落在不可读目录之内会在构造时报 `SandboxPolicyError`。

三个预设：

| 预设 | 读 | 写 | 适合 |
| --- | --- | --- | --- |
| `SandboxPolicy.read_only(unreadable_roots=…)` | 全盘 | 无 | 只做检查、不产生文件的命令 |
| `SandboxPolicy.workspace_write(workspace, extra_writable_roots=…, unreadable_roots=…, network=…)` | 全盘 | 工作区 | 单用户、受信任的开发机 |
| `SandboxPolicy.workspace_only(workspace, readable_roots=…, network=…)` | 系统目录 + 指定目录 + 工作区 | 工作区 | 多租户：别的租户的文件不可见 |

## 本机隔离：`taifeng_sandbox.local`

| 接口 | 说明 |
| --- | --- |
| `create_local_executor(policy)` | 按当前操作系统选后端：macOS 用 seatbelt，Linux 用 bubblewrap。后端不可用抛 `SandboxUnavailableError` |
| `SeatbeltCommandExecutor(policy)` | macOS。用系统自带的 `sandbox-exec` |
| `BwrapCommandExecutor(policy, bwrap_path=None)` | Linux。要求装了 `bubblewrap`，且允许创建用户命名空间 |

两个执行器都实现 taifeng 的 `CommandExecutor`：`await executor.start(CommandSpec) → CommandProcess`。

| 行为 | 说明 |
| --- | --- |
| 环境变量 | `CommandSpec.env` 就是进程的完整环境，不叠加宿主环境 |
| 进程组 | 以新会话启动；`kill()` 作用于整个进程组，shell 派生的子进程一起终止 |
| 输出 | 进程结束后一次性取回，不是流式 |
| 资源限制 | 不限制内存与 CPU。需要的话用容器后端 |

两个后端对同一份策略的翻译：

| | seatbelt（macOS） | bubblewrap（Linux） |
| --- | --- | --- |
| 读范围 | 配置文件里的允许规则 | 只挂载允许的目录 |
| 写范围 | 同上 | 可写目录以读写方式挂载，其余只读 |
| 出网 | 拒绝网络操作 | `--unshare-net` |
| 命名空间 | 无 | 用户、进程、IPC 各自独立；`network=False` 时网络也独立 |
| 已知差异 | 受限读模式下仍可对任意路径取元数据（不含内容） | 没有叠加 seccomp、Landlock |

## 脚本执行：`SandboxedScriptExecutor`

把 taifeng 的 skill 脚本交给任意 `CommandExecutor` 运行，实现 taifeng 的 `ScriptExecutor`。

| 接口 | 说明 |
| --- | --- |
| `shell_script_executor(executor, shell="/bin/sh", env=None, path_mapper=None)` | `language: shell` 的脚本 |
| `python_script_executor(executor, python="python3", env=None, path_mapper=None)` | `language: python` 的脚本。解释器带 `-u`（不缓冲输出）与 `-I`（隔离用户站点目录） |
| `SandboxedScriptExecutor(executor, interpreter=…, env=None, path_mapper=None)` | 自定义解释器 |

| 行为 | 说明 |
| --- | --- |
| 参数 | 按 `args_schema.properties` 的声明顺序展开成位置参数，未声明的追加在后。不拼 shell 字符串 |
| 工作目录 | 脚本所在的目录 |
| 环境变量 | `env` 就是完整环境；不给时只有 `PATH` 与 `LANG`。不继承宿主环境 |
| 超时、取消 | 到时或被取消时强杀进程；结果经 `ScriptResult` 返回，不抛异常 |
| 输出 | 两路各自按脚本声明的 `max_output_bytes` 截断 |
| `path_mapper` | 把宿主机上的脚本路径换成执行环境里的路径。容器里没有按相同路径挂载 skill 目录时要给 |

与 taifeng 自带执行器的差异：终止是直接强杀，没有 SIGTERM 宽限期；输出在进程结束后一次性取回。

## 容器：`taifeng_sandbox.docker`

```python
async with await DockerEnvironment.create(config) as sandbox:
    sandbox.executor()     # DaemonCommandExecutor，实现 CommandExecutor
    sandbox.workspace()    # DaemonWorkspace，文件访问
    sandbox.name           # 容器名
```

退出 `async with`（或调 `close()`）时断开连接并销毁容器。

### `DockerSandboxConfig`

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `image` | 必填 | 镜像。里面必须有 Python 3.9+ |
| `workdir` | `/workspace` | 容器内工作目录，也是守护进程的根目录 |
| `workspace_host_dir` | `None` | 挂到 `workdir` 的宿主机目录（可写）。不给则 `workdir` 是容器内的 tmpfs，容器销毁即丢失 |
| `mounts` | 空 | 额外的绑定挂载，`Mount(host_path, container_path, read_only=True)` |
| `network` | `False` | 是否允许出网。不允许时容器没有网络 |
| `memory_mb` | `1024` | 内存上限（同时禁用 swap）；`None` 不限 |
| `cpus` | `1.0` | CPU 配额；`None` 不限 |
| `pids_limit` | `256` | 进程数上限 |
| `read_only_rootfs` | `True` | 根文件系统只读 |
| `tmpfs` | `/tmp`，256 MiB | 额外的 tmpfs |
| `workdir_tmpfs_size` | `1g` | `workdir` 为 tmpfs 时的大小 |
| `user` | `None` | 容器内的运行身份 `uid[:gid]`；不给用镜像默认 |
| `python` | `python3` | 容器内的解释器 |
| `docker_binary` | `docker` | 宿主机上的 docker 命令 |
| `docker_env` | 空 | 运行 docker 命令时的环境。不继承宿主环境，所以要给出 `PATH`，以及 `HOME` 或 `DOCKER_HOST` |
| `labels` | 空 | 容器标签，方便事后按标签清理 |
| `startup_timeout_seconds` | `300` | 拉起容器的时限，含首次拉取镜像 |

无论怎么配，容器都去掉全部 capability、禁止提权、带 `--init`，销毁时自动删除。

## 守护进程客户端：`taifeng_sandbox.daemon`

| 接口 | 说明 |
| --- | --- |
| `PROTOCOL_VERSION` | 本包使用的线协议版本，当前为 `1` |
| `daemon_source()` | 守护进程的源码文本。单文件、只用标准库、兼容 Python 3.9+ |
| `StdioTransport.spawn(argv, env=None, cwd=None)` | 启动一个子进程，把它的标准输入输出当作连接。`env=None` 是空环境 |
| `Transport` | 传输的协议：`send(line)`、`receive()`、`close()`、`diagnostics()`。自己实现它就能走别的通道 |
| `DaemonClient.connect(transport, client_name=…, request_timeout_seconds=30)` | 握手并返回客户端。握手失败会关闭传输 |
| `client.request(method, params)` | 发请求。守护进程返回错误时抛 `SandboxRemoteError`，连接断开抛 `SandboxProtocolError` |
| `client.server_info` | 握手时守护进程上报的信息 |
| `client.close()` | 发 `shutdown` 并关闭 |
| `DaemonCommandExecutor(client, default_cwd=None, max_buffer_bytes=16 MiB)` | 实现 `CommandExecutor`。每路输出在宿主侧最多保留 `max_buffer_bytes`，超出丢弃并计数 |
| `RemoteProcess` | `DaemonCommandExecutor.start` 返回的进程句柄，实现 `CommandProcess` |

### `DaemonWorkspace(client)`

经守护进程访问它根目录之内的文件。

| 方法 | 说明 |
| --- | --- |
| `read_bytes(path)` / `read_text(path, encoding="utf-8")` | 读文件。大文件自动分段 |
| `write_bytes(path, data, create_parents=True)` / `write_text(…)` | 写文件。大文件自动分段 |
| `list_directory(path)` | 返回 `DirectoryEntry` 列表：`name`、`is_directory`、`is_file`、`is_symlink` |
| `metadata(path)` | 返回 `FileMetadata`：`exists`、`is_directory`、`is_file`、`size`、`modified_at` |
| `create_directory(path, recursive=True)` | 建目录 |
| `remove(path, recursive=False)` | 删除 |

文件不存在抛 `FileNotFoundError`，路径在根目录之外抛 `PermissionError`。

这是本包自己的接口：taifeng 的 `WorkspaceFS` 协议尚未落地，落地后会对齐。

## 异常

| 异常 | 基类 | 何时抛出 |
| --- | --- | --- |
| `SandboxPolicyError` | `ValueError` | 策略或配置本身不合法 |
| `SandboxError` | `OSError` | 下面三种的基类 |
| `SandboxUnavailableError` | `SandboxError` | 隔离后端不可用：缺可执行文件、平台不支持、守护进程连不上 |
| `SandboxProtocolError` | `SandboxError` | 与守护进程的交互失败：版本不兼容、响应畸形、连接中断 |
| `SandboxRemoteError` | `SandboxError` | 守护进程明确返回的错误，`code` 是线协议错误码 |

执行类失败都是 `OSError` 的子类，taifeng 工具层会把它们当作「进程启动失败」处理。**隔离后端不可用时只会失败，不会退回无隔离执行。**
