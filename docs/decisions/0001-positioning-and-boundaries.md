# ADR 0001：定位与边界 —— taifeng 执行类协议的隔离实现，独立于内核发布

- 状态：Accepted
- 日期：2026-09-29
- 关联：taifeng ADR 0017（规则③）、taifeng ADR 0051（`CommandExecutor` 对接口）、taifeng ADR 0009（`ScriptExecutor`）

## 背景

taifeng ADR 0051 把 shell 类工具的「启动进程」一步抽成 `CommandExecutor`，并明确不在内核做沙箱实现：隔离后端由宿主或独立扩展包提供。taifeng ADR 0017 规则③进一步禁止在 taifeng `src/` 内置任何具体后端，所以隔离后端也不能做成 taifeng 的 optional extra，只能独立成包。

codex 可作参照：隔离策略（`sandboxing`）、平台后端（`linux-sandbox`、seatbelt、`windows-sandbox`）、出网代理（`network-proxy`）、远端执行（`exec-server`）各自是独立 crate，与 `core` 分离。

## 决策

1. **只实现 taifeng 的执行类协议**：`CommandExecutor`、`ScriptExecutor`；taifeng 的 `WorkspaceFS` 对接口落地后同步接入。
2. **不重复内核保证**：审批、命令黑名单、env 白名单、超时、输出截断、取消都留在 taifeng 工具层。本仓只决定进程在哪里、以什么隔离方式运行。
3. **只依赖 taifeng 稳定层**（`taifeng.__all__`）。需要改 taifeng 内部才能实现时，回 taifeng 补对接口，不 monkeypatch、不 fork。
4. **零平台概念**：不出现租户、业务包等概念；按租户分配沙盒是上层平台的职责。
5. **每个后端一个 optional extra**，核心包除 taifeng 外没有运行时依赖。

## 后果

- 任何 taifeng 宿主都可以单独引入本包，不绑定某个平台。
- taifeng 协议变更会直接影响本仓；冒烟测试钉住所依赖协议在 taifeng 稳定层的可见性。
