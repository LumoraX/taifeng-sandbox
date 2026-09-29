# taifeng-sandbox

[taifeng](https://github.com/LumoraX/taifeng) 的执行隔离适配层：把 taifeng 定义的执行类协议接到本机隔离、容器与远端沙盒。

> **状态**：工程骨架。包可安装、CI 门禁就位，尚无可用后端。

## 定位

| 由谁负责 | 内容 |
| --- | --- |
| **taifeng**（内核） | 协议：`CommandExecutor`、`ScriptExecutor`、`WorkspaceFS`（待落地）；工具层保证：审批、命令黑名单、env 白名单、超时、输出截断、取消 |
| **taifeng-sandbox**（本仓） | 协议的隔离实现：进程在哪里、以什么隔离方式运行 |
| **上层平台** | 按租户 / 会话分配沙盒、配额、出网策略的配置 |

换执行器不会改变 taifeng 工具层的任何保证（taifeng ADR 0051）。

## 规划中的后端

每个后端一个 optional extra，核心包除 taifeng 外没有运行时依赖。

| extra | 后端 | 方式 |
| --- | --- | --- |
| `local` | 本机 OS 级隔离（Linux bwrap / Landlock、macOS seatbelt） | 直接包装本机进程启动，隔离策略参照 codex `sandboxing` |
| `docker` | 容器 | 容器内运行本仓的守护进程 |
| `k8s` | Pod | 同上 |
| `e2b` / `daytona` | 云沙盒 | 直接适配其 SDK（它们自带沙盒内守护进程） |

分层取舍见 [ADR 0002](docs/decisions/0002-in-sandbox-daemon.md)。

## 开发

```bash
uv sync --extra dev
uv run pytest
uv run ruff check --select F,S108,I,TC src tests
uv run mypy src/
```

## 许可

Apache-2.0
