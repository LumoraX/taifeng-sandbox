# 决策记录（ADR）

只增不改；要推翻旧决策，写新 ADR 并标 `Supersedes #NNNN`。旧 ADR 的正文不动，被后来的 ADR 取代或补充的条目在下表「状态」一栏标注。

| 编号 | 标题 | 状态 |
| --- | --- | --- |
| [0001](0001-positioning-and-boundaries.md) | 定位与边界 —— taifeng 执行类协议的隔离实现，独立于内核发布 | Accepted；决策 1 的「`WorkspaceFS` 落地后接入」已由 0004 闭环 |
| [0002](0002-in-sandbox-daemon.md) | 容器与远端后端走「沙盒内守护进程」范式 | Accepted；决策 1「`WorkspaceFS` 待落地」与后果第 2 条已由 0004 闭环 |
| [0003](0003-protocol-v1-and-trust-boundaries.md) | 线协议第 1 版与信任边界 | Accepted；决策 1、后果第 2 条被 0004 取代，决策 8 被 0004 部分取代，决策 5、7 由 0004 补充；决策 5 由 0005 补充 |
| [0004](0004-streaming-processes-and-workspace-fs.md) | 流式进程与工作区文件协议 —— 线协议第 2 版、`DaemonWorkspace` 实现 `WorkspaceFS`、原子替换 | Accepted |
| [0005](0005-local-launcher-and-seatbelt-hardening.md) | 本机后端收紧 —— bwrap 启动器不碰调用方环境 | Accepted |
