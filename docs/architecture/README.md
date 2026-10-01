# 架构总览（活文档）

> 本目录只描述**当前生效的设计**；为什么这么定见 [../decisions/](../decisions/README.md)。
>
> 相关：[接口参考](reference.md) · [线协议第 2 版](protocol.md)

## 现状

第一版（尚未发布）：本机隔离（macOS seatbelt、Linux bubblewrap）、沙盒内守护进程与线协议第 2 版、Docker 环境三部分可用。两类后端都支持持续读写的流式进程；`DaemonWorkspace` 实现 taifeng `WorkspaceFS`，内核的文件类工具可以读写沙盒里的文件。K8s 与云沙盒适配未开始。

## 分层

```
taifeng 工具层（审批 / 黑名单 / env 白名单 / 超时 / 截断 / 取消 / 文件类工具的路径规则与上限）
        │  CommandExecutor · ScriptExecutor · WorkspaceFS（taifeng 协议）
        ▼
本仓宿主侧实现
  ├─ SandboxedScriptExecutor ── 把脚本交给下面任意一个 CommandExecutor
  ├─ 本机隔离：SeatbeltCommandExecutor / BwrapCommandExecutor
  │            直接包装本机进程启动，不需要守护进程
  └─ DaemonCommandExecutor ┐
     DaemonWorkspace ──────┴──线协议──▶ 沙盒内守护进程
        ▲                                   ▲
        └── 环境提供者只负责拉起环境并建立连接 ┘
            DockerEnvironment（docker run + docker exec -i）
```

## 模块

| 模块 | 内容 |
| --- | --- |
| `taifeng_sandbox.policy` | `SandboxPolicy`：可读范围、可写根、不可读路径、是否出网。与后端无关，默认拒绝写、拒绝出网 |
| `taifeng_sandbox.local` | `seatbelt` / `bwrap`：策略到命令行的纯计算；`executor`：两个本机执行器与按平台选择的工厂；`process`：进程组句柄 |
| `taifeng_sandbox.script` | `SandboxedScriptExecutor`：参数展开、超时、取消、截断，启动交给注入的 `CommandExecutor` |
| `taifeng_sandbox.daemon` | `protocol`：线协议常量；`server`：守护进程（单文件、只用标准库）；`transport` / `client`：宿主侧连接；`executor`：`DaemonCommandExecutor`；`workspace`：`DaemonWorkspace`，实现 `WorkspaceFS` 的文件视图 |
| `taifeng_sandbox.docker` | `config`：`DockerSandboxConfig` 与 `docker run` 参数；`environment`：`DockerEnvironment` |
| `taifeng_sandbox.errors` | 异常类型。启动类失败都是 `OSError` 子类 |

## 一次命令执行的路径

以容器后端为例：

1. 宿主调 `DockerEnvironment.create(config)`：`docker run` 拉起加固过的容器（去掉全部 capability、禁止提权、只读根文件系统、默认断网、限制内存 / CPU / 进程数），再用 `docker exec -i` 在容器里启动守护进程并握手。
2. 宿主把 `environment.executor()` 交给 taifeng 的工具或 `SandboxedScriptExecutor`。
3. taifeng 工具层完成审批、黑名单、env 白名单之后，调 `executor.start(CommandSpec)`。
4. `DaemonCommandExecutor` 发 `process/start`；守护进程以新会话启动进程，把两路输出分块发回，最后发退出通知。
5. 工具层的超时或取消触发 `process.kill()`，守护进程杀掉整个进程组。
6. `environment.close()` 断开连接并销毁容器。

本机后端没有第 1、4 步的远程环节：执行器把命令包进 `sandbox-exec` 或 `bwrap` 后直接启动。

## 信任边界

| 边界 | 由谁守 |
| --- | --- |
| 进程能碰什么（文件、出网） | 执行环境：seatbelt 配置、bubblewrap 挂载与命名空间、容器 |
| 宿主经文件方法能碰什么 | `DaemonWorkspace.resolve` 先按字面拦下 `..` 与根外绝对路径（产出权限 target）；守护进程按真实路径判断（解析符号链接之后），这才是真实边界，越界是 `-32024`。根目录约束守的是工具层权限 target 与审计的一致性，不是提权边界（见 [ADR 0004](../decisions/0004-streaming-processes-and-workspace-fs.md) 决策 5、9） |
| 命令该不该执行 | taifeng 工具层 |

环境变量在每一层都是「给什么用什么」：执行器把 `CommandSpec.env` 当作完整环境，不叠加宿主环境；守护进程同样不把自己的环境传给子进程。

本机后端的启动器（沙盒外的 `bwrap` 进程）本身不受被隔离一方影响：它只拿固定的空环境、工作目录固定为 `/`；`CommandSpec.env` 经 `--args` 读取的封口 memfd 以 `--setenv` 交给 bwrap，只在沙盒里的目标进程上生效，值也不进命令行（[ADR 0005](../decisions/0005-local-launcher-and-seatbelt-hardening.md) 决策 1）。

seatbelt 配置逐项放行 sysctl（不含进程列表与启动参数），出网时只放行 IP 远端与 DNS 用的 mDNSResponder 套接字，本机其他 Unix 套接字（Docker 守护进程、ssh-agent、本地数据库）一律连不上（ADR 0005 决策 2、3）。

**macOS 本机后端不是对抗性隔离。** 沙盒里的进程仍能读到同一用户下所有非平台进程的完整参数与初始环境（`KERN_PROCARGS2`，seatbelt 管不到，见 ADR 0005 决策 2）；出网时能连本机任意 TCP 端口。运行不受信任的命令时，宿主不要把密钥放在同用户进程的环境变量里，或让命令以另一个系统用户运行，或改用容器后端。

## 组件状态

| 组件 | extra | 状态 | 验证方式 |
| --- | --- | --- | --- |
| macOS seatbelt | `local` | 可用 | macOS 上真跑 `sandbox-exec` |
| Linux bubblewrap | `local` | 可用 | Linux 上真跑 `bwrap`（需要可用的用户命名空间） |
| 线协议 + 守护进程 | — | 可用 | 守护进程作为子进程真跑 |
| Docker 后端 | `docker` | 可用 | 真的拉起容器 |
| K8s 后端 | `k8s` | 未开始 | — |
| E2B / Daytona 适配 | `e2b` / `daytona` | 未开始 | — |

## 验证

本机与 Linux 各跑一遍：seatbelt 用例只在 macOS 上跑，bubblewrap 用例只在 Linux 上跑。

```bash
# 本机（macOS 上覆盖 seatbelt；Linux 上覆盖 bubblewrap）
uv run pytest -q

# 在 macOS 上用 Docker 起一个 Linux 容器跑本机后端用例（bubblewrap 要 --privileged 才能建命名空间）。
# 仓库以只读方式挂进去，虚拟环境、uv 缓存与字节码都放在容器的 /tmp，不污染宿主仓库；
# 开发期 taifeng 是同级目录的路径依赖，所以一并挂载。gcc 用来编测试用的 LD_PRELOAD 库。
docker run --rm --privileged \
  -v "$PWD":/src/taifeng-sandbox:ro -v "$PWD/../taifeng":/src/taifeng:ro \
  -e UV_PROJECT_ENVIRONMENT=/tmp/venv -e UV_CACHE_DIR=/tmp/uv-cache \
  -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONPYCACHEPREFIX=/tmp/pycache \
  -w /src/taifeng-sandbox python:3.12-slim bash -c '
    apt-get update -qq && apt-get install -y -qq bubblewrap gcc libc6-dev >/dev/null &&
    pip install -q uv &&
    uv run --frozen --extra dev pytest tests/local -q -p no:cacheprovider'
```

容器里 seatbelt 用例全部跳过，bubblewrap 用例全部要跑到（没有跳过）。

## 已知限制

- 经守护进程执行时，`CommandSpec.stdin=False` 的命令在进程结束后一次性取回输出；`stdin=True` 时是流式的（标准输入持续写、输出边到边读，见 [`StreamingRemoteProcess`](reference.md#streamingremoteprocess)），每路未读输出有上限，超限强杀进程并报错。
- bubblewrap 后端只用命名空间与挂载，没有叠加 seccomp、Landlock。
- 同一用户的其他进程能读到沙盒里目标进程的环境（Linux `/proc/<pid>/environ`），与不隔离执行相同；需要对同用户隐藏密钥时，换用独立用户或容器后端。
- seatbelt 沙盒里的进程能读到同一用户下所有非平台进程的完整参数与初始环境（`KERN_PROCARGS2`；连 `(deny default)` 也挡不住，macOS 26.6.2 实测），也能按 pid 查单个进程的基本信息；进程列表、启动参数等其他 sysctl 被拒（ADR 0005 决策 2）。
- seatbelt 出网时只放行 IP 远端与 mDNSResponder 套接字：本机任意 TCP 端口（监听在回环上的数据库、开了 TCP 接口的 Docker 等）仍然连得上；需要自己建 Unix 套接字的程序会失败，`socketpair` 不受影响（ADR 0005 决策 3）。bubblewrap 后端不过滤 Unix 套接字：文件系统里的套接字只要所在目录被挂进沙盒就连得上，与是否出网无关（只读挂载挡不住连接；全盘可读时宿主的套接字都在，受限读时只有列出的目录里的）；出网时共享宿主网络命名空间，宿主的抽象 Unix 套接字也连得上（容器里实测）。在 Linux 上运行不受信任的命令请用受限读，且不要把放着宿主服务套接字的目录列为可读。
- seatbelt 启动器 `sandbox-exec` 拿到完整的 `CommandSpec.env`，依赖 SIP 让 dyld 忽略并删掉其中的 `DYLD_*`：目标进程也看不到 `DYLD_*`；关掉 SIP 的机器不能当作隔离边界（ADR 0005 决策 4）。
- bubblewrap 后端 `CommandSpec.cwd=None` 时取宿主进程当前目录，沙盒里看不到它就启动失败（bwrap 报错退出）。
- seatbelt 的受限读模式允许对任意路径取元数据（不含内容与目录列表），否则进程无法沿父目录打开深层文件。
- 出网只有开、关两档，没有域名白名单与出网代理；seatbelt 的「开」不含本机 Unix 套接字。
- 容器后端要求镜像里有 Python 3.9+。
- 经 `DaemonWorkspace` 写入超过 16 MiB 的文件不是原子的：线协议单次最多 16 MiB，第一段原子替换，之后逐段追加；线协议没有 rename。读取超过 16 MiB 的文件同样分段，不是快照：读的过程中文件被改，结果可能新旧混杂。
- 整文件写入是原子替换，代价是：父目录必须可写；断开硬链接；非 root 守护进程覆盖后属主变为守护进程的用户（root 时保持原属主）；只读文件可以被覆盖；单文件 bind mount 的目标得到 `EBUSY`；并发遍历能短暂看到 `.tmp-*`，守护进程中途被杀会残留；不 `fsync`（ADR 0004 决策 8）。
- 守护进程的路径检查与之后的打开、改名之间有竞争窗口（TOCTOU）：期间把某一级目录换成指向根外的链接，操作会落到根外。容器里守护进程与模型命令同 uid、同挂载命名空间，能抢赢竞争的模型本来就能用 shell 直接写，所以不加固；守护进程权限高于它执行的命令、或文件方法开放给沙盒命令之外的主体时，必须改用 dirfd 方案加固（ADR 0004 决策 9）。
- `DaemonWorkspace.resolve` 按字面校验、不跟随链接：内核工具的权限 target 与回显是字面路径，根内有链接时与实际读写的文件不一致，按路径写的权限规则可被根内链接绕开（ADR 0004 决策 5）。
- `DaemonWorkspace` 列目录不跟随符号链接，协议也不给链接目标：内核的 `glob` / `grep` 在非本机工作区上一律跳过符号链接，并在输出尾注里告知（taifeng ADR 0113）。

## 依赖的 taifeng 协议

| 协议 | taifeng 位置 | 在 taifeng 稳定层 |
| --- | --- | --- |
| `CommandExecutor` / `CommandProcess` / `CommandSpec` | `taifeng.tool.command_executor` | 是（taifeng ≥ 2026.9.30.1） |
| `ScriptExecutor` / `ScriptInvocation` / `ScriptResult` | `taifeng.skill.scripts` | 是 |
| `WorkspaceFS` / `WorkspaceFileInfo` / `WorkspaceEntry` / `WorkspacePathError` | `taifeng.tool.workspace` | 是（taifeng ≥ 2026.10.1.10） |

taifeng 自 2026.9.30.1 起带 `py.typed`，本仓对 taifeng 协议的使用受 mypy strict 检查。
