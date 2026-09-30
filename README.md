# taifeng-sandbox

[taifeng](https://github.com/LumoraX/taifeng) 的执行隔离适配层：把 taifeng 定义的执行类协议接到本机隔离、容器与远端沙盒。

> **状态**：第一版。本机隔离（macOS seatbelt、Linux bubblewrap）与 Docker 后端可用；K8s 与云沙盒适配未开始。

## 定位

| 由谁负责 | 内容 |
| --- | --- |
| **taifeng**（内核） | 协议：`CommandExecutor`、`ScriptExecutor`、`WorkspaceFS`（待落地）；工具层保证：审批、命令黑名单、env 白名单、超时、输出截断、取消 |
| **taifeng-sandbox**（本仓） | 协议的隔离实现：进程在哪里、以什么隔离方式运行 |
| **上层平台** | 按租户 / 会话分配沙盒、配额、出网策略的配置 |

换执行器不会改变 taifeng 工具层的任何保证（taifeng ADR 0051）。

## 用法

### 本机隔离

```python
from pathlib import Path

import taifeng
from taifeng_sandbox import SandboxPolicy, python_script_executor, shell_script_executor
from taifeng_sandbox.local import create_local_executor

# 全盘可读，只有工作区可写，不出网
policy = SandboxPolicy.workspace_write(Path("/data/workspaces/s1"))
executor = create_local_executor(policy)   # macOS 用 seatbelt，Linux 用 bubblewrap

pool = await taifeng.EnginePool.create(
    skills_dir="/data/skills",
    storage_dir="/data/threads",
    model_client=model_client,
    # skill 脚本在沙盒里运行
    script_executors={
        "shell": shell_script_executor(executor),
        "python": python_script_executor(executor),
    },
    # 后台命令在沙盒里运行
    extra_tools=[
        taifeng.make_run_in_background_tool(
            registry=taifeng.BackgroundTaskRegistry(executor=executor),
            policy=permission_policy,
        ),
    ],
)
```

### Docker

```python
from pathlib import Path

from taifeng_sandbox import shell_script_executor
from taifeng_sandbox.docker import DockerEnvironment, DockerSandboxConfig, Mount

config = DockerSandboxConfig(
    image="python:3.12-slim",
    workspace_host_dir=Path("/data/workspaces/s1"),
    # skill 目录按相同路径只读挂载，脚本路径在容器内外一致
    mounts=(Mount(Path("/data/skills"), "/data/skills"),),
    docker_env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/home/app"},
)

async with await DockerEnvironment.create(config) as sandbox:
    scripts = shell_script_executor(sandbox.executor())
    await sandbox.workspace().write_text("input.txt", "...")
```

隔离后端不可用时构造即失败（`SandboxUnavailableError`），不会退回无隔离执行。

### 直接使用守护进程

`DockerEnvironment` 之外的环境（自己管理的容器、远端主机）可以直接用线协议的客户端：`daemon_source()` 取守护进程的源码（单文件、只用标准库、Python 3.9+），在目标环境里启动它，再用 `StdioTransport` 或自己实现的 `Transport` 连上去。

```python
from taifeng_sandbox.daemon import (
    DaemonClient,
    DaemonCommandExecutor,
    DaemonWorkspace,
    StdioTransport,
    daemon_source,
)

# 任何能把标准输入输出接到目标环境的命令都可以；--root 是文件访问的根目录
transport = await StdioTransport.spawn(
    ["docker", "exec", "-i", container_id, "python3", "-c", daemon_source(), "--root", "/work"]
)
client = await DaemonClient.connect(transport)     # 握手并核对协议版本（PROTOCOL_VERSION）
executor = DaemonCommandExecutor(client)           # 实现 taifeng.CommandExecutor，返回 RemoteProcess
workspace = DaemonWorkspace(client)                # 文件访问，返回 FileMetadata / DirectoryEntry
```

线协议见 [ADR 0003](docs/decisions/0003-protocol-v1-and-trust-boundaries.md)。

### 异常

| 异常 | 何时抛出 |
| --- | --- |
| `SandboxPolicyError`（`ValueError`） | 隔离策略本身不合法：相对路径、互相矛盾的根目录 |
| `SandboxError`（`OSError`） | 下面三种的基类。是 `OSError` 的子类，taifeng 工具层按「启动失败」处理 |
| `SandboxUnavailableError` | 隔离后端在当前环境不可用：缺可执行文件、平台不支持、守护进程连不上 |
| `SandboxProtocolError` | 与守护进程的交互失败：版本不兼容、响应畸形、连接中断 |
| `SandboxRemoteError` | 守护进程明确返回的错误，`code` 是线协议错误码 |

## 后端

每个后端一个 optional extra，核心包除 taifeng 外没有运行时依赖。

| extra | 后端 | 方式 | 状态 |
| --- | --- | --- | --- |
| `local` | 本机 OS 级隔离（Linux bubblewrap、macOS seatbelt） | 直接包装本机进程启动 | 可用 |
| `docker` | 容器 | 容器内运行本仓的守护进程 | 可用 |
| `k8s` | Pod | 同上 | 未开始 |
| `e2b` / `daytona` | 云沙盒 | 直接适配其 SDK（它们自带沙盒内守护进程） | 未开始 |

## 文档

| 文档 | 内容 |
| --- | --- |
| [接口参考](docs/architecture/reference.md) | 隔离策略的字段与预设、各执行器的行为、容器配置的全部字段、守护进程客户端、异常 |
| [线协议](docs/architecture/protocol.md) | 宿主与沙盒内守护进程之间的协议：给别的环境写客户端、或换一种语言实现守护进程时用 |
| [架构总览](docs/architecture/README.md) | 分层、信任边界、已知限制 |
| [决策记录](docs/decisions/README.md) | 分层取舍见 [ADR 0002](docs/decisions/0002-in-sandbox-daemon.md)、[ADR 0003](docs/decisions/0003-protocol-v1-and-trust-boundaries.md) |

## 开发

```bash
uv sync --extra dev
uv run pytest                                       # 隔离测试都是真跑，缺后端的用例自动跳过
uv run ruff check --select F,S108,I,TC src tests
uv run mypy src/
```

各组测试的前提：seatbelt 用例要 macOS；bubblewrap 用例要 Linux、装了 `bubblewrap` 且允许创建用户命名空间；Docker 用例要本机有可用的 Docker 守护进程。

## 许可

Apache-2.0
