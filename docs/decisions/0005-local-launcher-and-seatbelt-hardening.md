# ADR 0005：本机后端收紧 —— bwrap 启动器不碰调用方环境，seatbelt 逐项放行 sysctl、出网不放本机 Unix 套接字

- 状态：Accepted
- 日期：2026-10-02
- 关联：ADR 0002 决策 4（本机隔离直接包装进程启动）、ADR 0003 决策 5（信任边界）
- 参照：flatpak 经 `bwrap --args FD` 以封口 memfd 传参的做法（LGPL-2.1+，只借鉴做法、不复用代码）；codex `seatbelt_base_policy.sbpl` 与出网策略（Apache-2.0）；macOS 自带的 App Sandbox 配置 `/System/Library/Sandbox/Profiles/container.sb`（只参照其中放行的 sysctl 名字）

## 背景

本机后端被用来运行不受信任的长驻进程：调用方把第三方的 stdio MCP server 放进 `SandboxPolicy.workspace_only(…, network=True)`，`CommandSpec.env` 里是对方声明的环境变量与密钥。一次针对这种用法的审查实测出三处缺口：

**bwrap 启动器继承了调用方的整份环境。** `BwrapCommandExecutor.start` 把 `spec.env` 原样交给沙盒外的 `bwrap` 进程本身，启动器的工作目录还是 `spec.cwd`。主流发行版上 `bwrap` 不是 setuid 程序，glibc 照常处理 `LD_PRELOAD`、`LD_LIBRARY_PATH`、`GCONV_PATH` 等变量：env 里放一个 `LD_PRELOAD`，对方的代码就在**沙盒建好之前**以宿主用户的身份在沙盒外执行（bubblewrap 0.12.0 上复现：测试用的 .so 在构造函数里记下加载它的进程，记到的是 `/usr/bin/bwrap`）。

这条路径上调用方可以按名字拒绝 `LD_*`，但黑名单列不全（`GCONV_PATH`、`GLIBC_TUNABLES` 等都能影响启动器），根因在启动器拿了不该拿的环境。

**seatbelt 基础配置整体放开了 `sysctl-read`。** 沙盒里能列出本机全部进程、读启动参数等；审查同时认为它让沙盒能经 `KERN_PROCARGS2`（`ps eww` 用的读法）读到同一用户下其他进程的完整参数与环境——宿主进程的主密钥、数据库连接串之类按惯例放在环境变量里。后一点在修复时进一步查明**与 `sysctl-read` 无关**，见决策 2。

**seatbelt 出网档是不加过滤的 `network-outbound`。** 它连带放开了本机所有 Unix 套接字：沙盒里能连上 Docker 守护进程的套接字（等于接管宿主），ssh-agent、本地数据库的套接字同理（实测连上了 `/var/run/docker.sock`；套接字连接不受文件读规则约束，受限读模式也挡不住）。

## 决策

1. **bwrap 启动器只用固定的空环境，工作目录固定为 `/`；目标进程的环境在 bwrap 里设置。**
   - 启动器的环境是 `bwrap.LAUNCHER_ENV`（空），工作目录是 `bwrap.LAUNCHER_CWD`（`/`），与 `CommandSpec` 无关。
   - `spec.env` 的每个变量写成一组 `--setenv 名 值`，NUL 分隔写进一个 memfd，封口（禁止写入、增长、收缩）后经 `--args FD` 交给 bwrap。bwrap 在解析参数时 `setenv`，此时启动器的动态链接早已结束，这些变量只对之后在沙盒里 exec 的目标进程生效。bwrap 读完即关闭这个 fd，目标进程拿不到它。
   - 启动器环境为空，沙盒里的环境恰好是 `spec.env`，外加 bwrap 自己设的 `PWD`。
   - 沙盒里的工作目录一律经 `--chdir` 给出：`spec.cwd`；为 None 时取宿主进程当前的目录（与不隔离执行一致）。沙盒里看不到这个目录时 bwrap 报错退出，不再像 bwrap 自己那样悄悄退回 `$HOME` 或 `/`。
   - 变量名为空、含 `=` 或 NUL，值含 NUL，启动前抛 `SandboxError`（`OSError` 子类，工具层转成 `spawn_error`）。
   - **值不进命令行**：`/proc/<pid>/cmdline` 在默认挂载的 procfs 上对本机所有用户可读，而原来经启动器环境传递时，`/proc/<pid>/environ` 只有同一用户（且有 ptrace 读权限）读得到。memfd 只能经 `/proc/<pid>/fd` 打开，权限要求与 `environ` 相同，所以暴露面与修复前相同，没有因为改走 `--setenv` 而多暴露给其他用户。

2. **seatbelt 的 `sysctl-read` 逐项放行。**
   - 放行清单在 `seatbelt.SYSCTL_READ_NAMES`（精确名字）与 `seatbelt.SYSCTL_READ_PREFIXES`（前缀：`hw.`、`machdep.cpu.`，以及 `sysctl` 命令行按 OID 反查名字与类型用的 `sysctl.name.`、`sysctl.oidfmt.`），取 codex 基础策略与 App Sandbox 配置里只读系统信息的项：CPU 数与型号、内存大小、页大小、系统版本、主机名、`kern.argmax`、`kern.boottime`、`vm.loadavg` 等。
   - 不放行：`kern.proc.*`（进程列表与进程信息；codex 放行了 `kern.proc.pid.*` / `kern.proc.pgrp.*`，这里不放）、`kern.procargs*`、`kern.bootargs`（App Sandbox 也只对苹果签名的程序放行后两类）、`kern.uuid` 等清单外的一切；没有任何 `sysctl-write`。苹果自带工具启动时都会查 `kern.bootargs`，被拒只是在系统日志里留一条记录，不影响运行。
   - 出网档另外放行 `net.routetable.*`（网卡与地址列表，`getifaddrs` 用；codex 出网档同样放行）。
   - 实测（macOS 26.6.2）：Python（含 `ssl`、`asyncio`、`multiprocessing`、`platform`）、`/usr/bin/python3`、Node、Go 编出的程序、Java、`sysctl -n`、`uname`、走 HTTPS 的 `curl` 在受限读 + 出网的策略下照常运行；`KERN_PROC_ALL`（进程列表）、`kern.bootargs`、`kern.uuid` 被拒（`EPERM`）。
   - **读不到其他进程的参数与环境，这一点做不到。** 实测发现内核对 `KERN_PROCARGS2` 与 `KERN_PROC_PID`（按 pid 查单个进程）不走沙盒的 sysctl 检查：修复前后都读得到，连只放行 `process-exec` 与文件读的 `(deny default)` 配置也挡不住；给目标程序加上 hardened runtime 签名也不行，只有苹果的平台二进制不暴露环境。seatbelt 配置里没有能拦住它的规则，所以这是 macOS 本机后端的**已知限制**：沙盒里的进程能读到同一用户下所有非平台进程的完整参数与初始环境。需要防住它的宿主，不要把密钥放在与不受信任命令同用户运行的进程的环境变量里（改从沙盒读不到的文件读取），或让不受信任的命令以另一个系统用户运行，或改用容器后端。用例以 `xfail(strict=True)` 记录这一点：哪天 macOS 开始拦截，用例变成 XPASS 而失败，提醒更新文档。

3. **seatbelt 出网只放行 IP 远端与 DNS 套接字。**
   - `network-outbound` 只放行 `(remote ip)`（含本机回环）与 `(remote unix-socket (path-literal "/private/var/run/mDNSResponder"))`——系统解析器经后者找 mDNSResponder，少了它连外网域名都解析不了（`localhost` 走 hosts 文件，不经它）。
   - `network-inbound` 只放行 `(local ip)`：可以监听 IP 端口（如本地回调），不能绑定 Unix 套接字。
   - 其余 Unix 套接字一律拒绝，连接与绑定都拒（`EPERM`），包括工作区里自己建的；`socketpair` 不受影响（asyncio、子进程通信照常）。
   - 实测：修复前连得上 `/var/run/docker.sock` 与测试自建的 Unix 套接字，修复后都是 `Operation not permitted`；本机 TCP 端口、外网域名解析与 HTTPS 照常。

4. **seatbelt 启动器仍拿 `spec.env`，不另做处理。** `sandbox-exec` 是受系统完整性保护（SIP）的平台二进制：dyld 忽略它环境里的 `DYLD_*`，并在 exec 目标进程之前把它们删掉（实测目标进程看不到 `DYLD_*`）。所以 C1 那类注入在 SIP 开着的机器上不成立；关掉 SIP 的机器上这个前提不成立，本仓不把这种机器当作隔离边界。沙盒里没有可靠的办法从 fd 设环境（没有 bwrap 那样的 `--setenv`），把值放进命令行又会让本机其他用户看到（macOS 上进程参数对所有用户可见），所以不改。

### 修复前后

| | 修复前 | 修复后 |
| --- | --- | --- |
| 启动器的环境 | 整份 `spec.env`（含 `LD_PRELOAD` 等） | 空 |
| 启动器的工作目录 | `spec.cwd`（常是调用方指定的、对方可控的目录） | `/` |
| env 里的 `LD_PRELOAD` 在哪里生效 | 沙盒外的启动器（宿主用户、无隔离）与沙盒里的目标进程 | 只在沙盒里的目标进程 |
| 同一用户的其他进程能否读到 env 的值 | 能（启动器与目标进程的 `environ`） | 能（目标进程的 `environ`；memfd 被关闭之前经 `/proc/<pid>/fd`） |
| 其他用户能否读到 env 的值 | 不能 | 不能（值不在 `cmdline` 里） |

| seatbelt | 修复前 | 修复后 |
| --- | --- | --- |
| sysctl 读取 | 全部放行 | 逐项放行；进程列表、启动参数等被拒 |
| 读同一用户其他进程的参数与环境（`KERN_PROCARGS2`） | 能 | **仍然能**（seatbelt 管不到，已知限制） |
| 出网时连本机 Unix 套接字（Docker、ssh-agent、数据库） | 能 | 不能 |
| 出网时连本机 TCP 端口、外网 IP、DNS | 能 | 能 |

## 否决的方案

- **`--setenv` 直接放在命令行上**：值出现在 `/proc/<pid>/cmdline`，本机其他用户都能读到；bwrap 外层进程在沙盒整个生命期内都在，暴露是持续的。
- **加 `--clearenv`**：启动器环境本来就是空的，没有可清的；而 `--clearenv` 要 bubblewrap 0.5.0 才有，0.4.x（如 Debian 11 自带的 0.4.1）上 bwrap 直接报错退出。`--args` 从 0.1.2 起就有，不抬高最低版本。
- **经管道传参**：内容超过管道缓冲（Linux 默认 64 KiB）时，在 bwrap 开始读之前写入就会阻塞；要么另起线程写，要么限制大小。memfd 没有这个问题，也不落盘。
- **在本仓按名字拒绝 `LD_*`**：env 白名单是 taifeng 工具层与调用方的职责（ADR 0001 决策 2）；黑名单列不全；启动器不该拿到调用方环境才是根因。
- **seatbelt 保留整体放开的 `sysctl-read`，只 deny 几项**：默认放行比默认拒绝更容易漏掉新版本加的项；逐项放行与 codex、App Sandbox 的做法一致。
- **按目录放行工作区里的 Unix 套接字**：会让工作区或可写目录恰好包含宿主服务的套接字时（例如可写根是家目录，Docker Desktop 的套接字在 `~/.docker/run/`）重新放开；目前没有用例需要，需要时再按参数化目录放行。
- **seatbelt 出网改走代理、按域名放行**：需要本仓接出网代理，超出这次修复的范围；出网仍是开、关两档。

## 后果

- 补充 ADR 0003 决策 5 的边界表：「进程能碰什么」由执行环境守，前提是执行环境的启动器本身不受被隔离一方的影响。
- `CommandSpec.cwd=None` 在 bwrap 后端上的语义从「bwrap 沿用启动目录，看不到就退回 `$HOME`」变成「宿主进程当前目录，看不到就失败」。
- bwrap 不需要新版本才有的参数；本机后端用例在 bubblewrap 0.4.1（自编）与 0.12.0 上都实测通过。本仓不探测 bwrap 版本。
- seatbelt 出网策略下，需要自己建 Unix 套接字的程序（如 `multiprocessing.Manager` 默认的连接方式）会失败；需要时再按目录参数化放行。
- bubblewrap 后端这次不动 Unix 套接字：文件系统里的套接字所在目录被挂进沙盒就连得上（与是否出网无关），出网时宿主的抽象套接字也连得上（容器里实测）。受限读不挂 `/run`、`/var/run`，所以受限读策略下宿主服务的套接字默认不可见；要过滤得靠 seccomp 或 Landlock，ADR 0003 后果里已记为未做。
- 取代 `local/seatbelt.py` 原先的注释里「`sysctl-read` 整体放开，换取兼容性」与「出网只有开 / 关两档、不处理 Unix socket 白名单」的取舍（那两条没有写进 ADR）。
