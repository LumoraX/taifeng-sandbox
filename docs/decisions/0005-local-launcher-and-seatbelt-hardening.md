# ADR 0005：本机后端收紧 —— bwrap 启动器不碰调用方环境

- 状态：Accepted
- 日期：2026-10-02
- 关联：ADR 0002 决策 4（本机隔离直接包装进程启动）、ADR 0003 决策 5（信任边界）
- 参照：flatpak 经 `bwrap --args FD` 以封口 memfd 传参的做法（LGPL-2.1+，只借鉴做法、不复用代码）

## 背景

本机后端被用来运行不受信任的长驻进程：调用方把第三方的 stdio MCP server 放进 `SandboxPolicy.workspace_only(…, network=True)`，`CommandSpec.env` 里是对方声明的环境变量与密钥。一次针对这种用法的审查实测出一处阻断级缺口：

**bwrap 启动器继承了调用方的整份环境。** `BwrapCommandExecutor.start` 把 `spec.env` 原样交给沙盒外的 `bwrap` 进程本身，启动器的工作目录还是 `spec.cwd`。主流发行版上 `bwrap` 不是 setuid 程序，glibc 照常处理 `LD_PRELOAD`、`LD_LIBRARY_PATH`、`GCONV_PATH` 等变量：env 里放一个 `LD_PRELOAD`，对方的代码就在**沙盒建好之前**以宿主用户的身份在沙盒外执行（bubblewrap 0.12.0 上复现：测试用的 .so 在构造函数里记下加载它的进程，记到的是 `/usr/bin/bwrap`）。

这条路径上调用方可以按名字拒绝 `LD_*`，但黑名单列不全（`GCONV_PATH`、`GLIBC_TUNABLES` 等都能影响启动器），根因在启动器拿了不该拿的环境。

## 决策

1. **bwrap 启动器只用固定的空环境，工作目录固定为 `/`；目标进程的环境在 bwrap 里设置。**
   - 启动器的环境是 `bwrap.LAUNCHER_ENV`（空），工作目录是 `bwrap.LAUNCHER_CWD`（`/`），与 `CommandSpec` 无关。
   - `spec.env` 的每个变量写成一组 `--setenv 名 值`，NUL 分隔写进一个 memfd，封口（禁止写入、增长、收缩）后经 `--args FD` 交给 bwrap。bwrap 在解析参数时 `setenv`，此时启动器的动态链接早已结束，这些变量只对之后在沙盒里 exec 的目标进程生效。bwrap 读完即关闭这个 fd，目标进程拿不到它。
   - 启动器环境为空，沙盒里的环境恰好是 `spec.env`，外加 bwrap 自己设的 `PWD`。
   - 沙盒里的工作目录一律经 `--chdir` 给出：`spec.cwd`；为 None 时取宿主进程当前的目录（与不隔离执行一致）。沙盒里看不到这个目录时 bwrap 报错退出，不再像 bwrap 自己那样悄悄退回 `$HOME` 或 `/`。
   - 变量名为空、含 `=` 或 NUL，值含 NUL，启动前抛 `SandboxError`（`OSError` 子类，工具层转成 `spawn_error`）。
   - **值不进命令行**：`/proc/<pid>/cmdline` 在默认挂载的 procfs 上对本机所有用户可读，而原来经启动器环境传递时，`/proc/<pid>/environ` 只有同一用户（且有 ptrace 读权限）读得到。memfd 只能经 `/proc/<pid>/fd` 打开，权限要求与 `environ` 相同，所以暴露面与修复前相同，没有因为改走 `--setenv` 而多暴露给其他用户。

### 修复前后

| | 修复前 | 修复后 |
| --- | --- | --- |
| 启动器的环境 | 整份 `spec.env`（含 `LD_PRELOAD` 等） | 空 |
| 启动器的工作目录 | `spec.cwd`（常是调用方指定的、对方可控的目录） | `/` |
| env 里的 `LD_PRELOAD` 在哪里生效 | 沙盒外的启动器（宿主用户、无隔离）与沙盒里的目标进程 | 只在沙盒里的目标进程 |
| 同一用户的其他进程能否读到 env 的值 | 能（启动器与目标进程的 `environ`） | 能（目标进程的 `environ`；memfd 被关闭之前经 `/proc/<pid>/fd`） |
| 其他用户能否读到 env 的值 | 不能 | 不能（值不在 `cmdline` 里） |

## 否决的方案

- **`--setenv` 直接放在命令行上**：值出现在 `/proc/<pid>/cmdline`，本机其他用户都能读到；bwrap 外层进程在沙盒整个生命期内都在，暴露是持续的。
- **加 `--clearenv`**：启动器环境本来就是空的，没有可清的；而 `--clearenv` 要 bubblewrap 0.5.0 才有，0.4.x（如 Debian 11 自带的 0.4.1）上 bwrap 直接报错退出。`--args` 从 0.1.2 起就有，不抬高最低版本。
- **经管道传参**：内容超过管道缓冲（Linux 默认 64 KiB）时，在 bwrap 开始读之前写入就会阻塞；要么另起线程写，要么限制大小。memfd 没有这个问题，也不落盘。
- **在本仓按名字拒绝 `LD_*`**：env 白名单是 taifeng 工具层与调用方的职责（ADR 0001 决策 2）；黑名单列不全；启动器不该拿到调用方环境才是根因。

## 后果

- 补充 ADR 0003 决策 5 的边界表：「进程能碰什么」由执行环境守，前提是执行环境的启动器本身不受被隔离一方的影响。
- `CommandSpec.cwd=None` 在 bwrap 后端上的语义从「bwrap 沿用启动目录，看不到就退回 `$HOME`」变成「宿主进程当前目录，看不到就失败」。
- bwrap 用到的参数仍然都是 0.1.x 就有的；本仓不探测 bwrap 版本。
