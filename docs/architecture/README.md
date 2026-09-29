# 架构总览（活文档）

> 本目录只描述**当前生效的设计**；为什么这么定见 [../decisions/](../decisions/README.md)。

## 现状

工程骨架：包可安装、CI 门禁就位，尚无后端实现。

## 分层

```
taifeng 工具层（审批 / 黑名单 / env 白名单 / 超时 / 截断 / 取消）
        │  CommandExecutor · ScriptExecutor · WorkspaceFS（taifeng 协议）
        ▼
本仓宿主侧实现
  ├─ 本机隔离：直接包装本机进程启动（bwrap / Landlock / seatbelt）
  ├─ 守护进程客户端 ──线协议──▶ 沙盒内守护进程（docker / k8s）
  └─ 云沙盒 SDK 适配（E2B / Daytona）
```

## 组件状态

| 组件 | extra | 状态 |
| --- | --- | --- |
| 本机 OS 级隔离 | `local` | 未开始 |
| 线协议 + 沙盒内守护进程 | — | 未开始 |
| Docker 后端 | `docker` | 未开始 |
| K8s 后端 | `k8s` | 未开始 |
| E2B / Daytona 适配 | `e2b` / `daytona` | 未开始 |

## 依赖的 taifeng 协议

| 协议 | taifeng 位置 | 在 taifeng 稳定层 |
| --- | --- | --- |
| `CommandExecutor` / `CommandProcess` / `CommandSpec` | `taifeng.tool.command_executor` | taifeng main 已导出，**尚未发版**（PyPI 最新 2026.9.28.16 不含） |
| `ScriptExecutor` | `taifeng.skill.scripts.executor` | 是 |
| `WorkspaceFS` | 尚不存在 | — |

另：taifeng 目前没有 `py.typed` 标记，本仓代码一旦 import taifeng，mypy strict 会报 `import-untyped`。需要 taifeng 补上标记后发版。
