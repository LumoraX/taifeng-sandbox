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

## 后端

每个后端一个 optional extra，核心包除 taifeng 外没有运行时依赖。

| extra | 后端 | 方式 | 状态 |
| --- | --- | --- | --- |
| `local` | 本机 OS 级隔离（Linux bubblewrap、macOS seatbelt） | 直接包装本机进程启动 | 可用 |
| `docker` | 容器 | 容器内运行本仓的守护进程 | 可用 |
| `k8s` | Pod | 同上 | 未开始 |
| `e2b` / `daytona` | 云沙盒 | 直接适配其 SDK（它们自带沙盒内守护进程） | 未开始 |

设计与已知限制见 [docs/architecture/](docs/architecture/README.md)，分层取舍见 [ADR 0002](docs/decisions/0002-in-sandbox-daemon.md)、[ADR 0003](docs/decisions/0003-protocol-v1-and-trust-boundaries.md)。

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
