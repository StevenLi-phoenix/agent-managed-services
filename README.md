![ams](./docs/ams.webp)

# ams — agent managed services

一个 **rootless 的 Linux 服务监管器**：把你声明的服务当作自己的直接子进程拉起来，
每个服务一个独立的 user namespace（独占一段 subuid）和一个委托的 cgroup v2 子树；
它们的 stdout/stderr 由监管器直接持有和逐行读取，每条 warning/error 都要经过一个显式
的**抑制（suppress）或修复（fix）**决策点，规则处理不了的写进一个 escalation 日志，
交给操作者——人，或者一个由人启动的 agent 会话（比如 Claude Code）——去处理。

不用 Docker，没有 per-service systemd unit，监管器本身不需要 root。

```
systemd  (只负责让 harness 活着，Delegate=yes)
└── harness  (非特权用户；单线程 selector 循环；child subreaper)
    ├── 服务 A   user namespace，uid 166560..，cgroup svc-a（memory.max / pids.max / cpu.max）
    ├── 服务 B   user namespace，uid 167584..，cgroup svc-b
    └── ...      stdout/stderr → 决策点 → 日志 / 重启 / escalation
                                                     │
                     <state>/logs/escalations.jsonl ◄┘  ← `ams escalations`（agent / 人读这里）
```

## 这是给谁用的

给**愿意自己管一台 Linux 机器的操作者**，以及替他干活的 agent。它不是给不懂技术的人
"一键托管"的产品：装好之后日常确实只需要写 `service.toml`、看 `ams escalations`、
敲 `ams ctl`，但第一次装宿主需要 root、需要理解 subuid 和 AppArmor 在做什么。
`deploy/install-host.sh` 把这一步收敛成一条命令。

仓库里有两层，可以只用第一层：

| 层 | 是什么 | 依赖 |
|---|---|---|
| **supervisor 核心**（`src/ams/*.py`，~8.6k 行，纯标准库） | 通用的 rootless 监管器：声明、隔离、日志决策、健康检查、重启策略、依赖顺序、只写 secrets、控制 socket、escalation 日志 | Python 3.12；Linux 隔离需要下面的宿主条件 |
| **platform 层**（`src/ams/platform/`，~6.8k 行） | **core mode**：把作者的 `api` 项目（一个 Cordis/Node 进程 + 热安装插件）作为一个服务托管——按 commit 发布 core、按内容键 ship 插件、Caddy 前端、R2 备份 | 绑定 `api` 仓库的约定；对其他项目没有用 |

核心不依赖 platform：没有任何核心模块在 import 时引入 `ams.platform`；把
`src/ams/platform/` 整个删掉，`ams` 仍是完整的监管器，只是少了 `ams platform` 子命令。
这条边界由 `tests/test_core_boundary.py` 锁住。

## 当前状态（2026-10-01，ams 2.0.0）

| | |
| --- | --- |
| 可移植测试（macOS，无隔离） | 918 passed / 90 skipped（`.venv/bin/python -m pytest -q`，~40 s） |
| Linux 实机，全部测试 | **998 passed / 10 skipped**：Ubuntu 24.04.5，kernel 7.0，cgroup v2，`apparmor_restrict_unprivileged_userns=1`，以 `harness` 身份在委托 cgroup 里跑（`scripts/linux-test.sh`）。10 个 skip 是"该宿主没有 XFS reflink / 系统 node / `../api` 检出 / PATH 上的 caddy"这类环境条件 |
| Linux 实机，core mode 端到端（隔离模式） | 7 个场景全过：首次发布 71 s、空闲 tick 0.15 s 不写文件、单插件内容变更只 ship 它、坏插件被拒只升级一次且不重试、core release（`/health` 断约 2 s）、回滚 3 s、harness 重启后 1 s 恢复。core 以 uid 166560 运行、CapEff 0、NoNewPrivs 1、harness uid 不在映射里、`memory.max`/`swap.max=0` 生效、读不到 harness 的 secret store。证据：`docs/design/evidence/core-e2e-linux-2026-10-01.txt` |
| CI | `.github/workflows/ci.yml`：可移植测试（Linux + macOS）+ 在 `ubuntu-24.04` runner 上用同样两步装宿主、以 harness 身份跑全部测试。2026-10-02 全绿：macOS 与 Ubuntu 各 918 passed，隔离任务 999 passed / 9 skipped |
| 外部安全审计 | 没有。隔离层（~1.6k 行 fork/unshare/newuidmap/cgroup 代码）只有自己的测试和实机验证 |
| 生产 | 没有。作者的 api 生产环境仍是另一台机器上的 systemd unit，不在 ams 范围内，ams 从不连接它 |

1.0.0（已删除的 manifest mode）在 2026-09 的 racknerd 实测数字保留在
`docs/design/history/`，是历史观察值，不是规划常量。

## 你能得到什么

- **无需特权的隔离。** 每个服务一个 user namespace，通过 setuid 的
  `newuidmap`/`newgidmap` 映射进 harness 的 `/etc/subuid` 里一段独占的 1024 个 uid；
  **harness 自己的 uid 不在映射里**，所以服务里的 root 逃逸也碰不到 harness 的文件。
  零 capability、`no_new_privs`。限额（`memory.max`，同时 `memory.swap.max=0`；
  `cpu.max`；`pids.max`）写进委托的 cgroup v2 子树，`cgroup.kill` 原子地杀整棵树。
- **日志是 fd，不是 journald。** 每一行带 `[svc:stream]` 标签、按严重度分类，经过
  `DecisionPolicy` → `Escalation`：自探测访问日志、已知噪音被抑制，退出按重启策略处理，
  规则拿不准的升级。`--policy platform` 再叠加按原因去重和 Caddy 规则。
- **escalation 日志。** 升级的事件写 stdout，同时追加到
  `<state>/logs/escalations.jsonl`；`ams escalations` 按服务、时间过滤，默认格式会剥掉
  终端控制字符（记录里是服务的原样输出，不可信）。
- **健康检查不阻塞循环。** tcp/http 探测是挂在 selector 上的非阻塞状态机，一个永不应答的
  `/health` 只占一个 fd 和一个计时器，不会拖住其他服务（见下文"事件循环"）。
- **声明，不是 unit 文件。** 每个服务一个 `service.toml`：argv、workdir、env、secret
  *名字*、端口（`0` = 自动分配）、runtime（venv/uv/pnpm/bun，或精确版本的托管 Node）、
  健康检查、优雅停止、限额、重启策略、`depends_on`。见 `docs/service-declaration.md`。
- **只写不读的 secrets。** `ams secret set <id> NAME` 从 stdin 读值，0600 存储，spawn 时
  注入环境变量；从不出现在 argv、日志或 `status` 里。
- **每服务控制。** 0600 unix socket，由监管循环自己服务：
  `ams ctl status|reload|start|stop|restart|kill <id>`；改声明不需要重启 harness。

## agent 在哪里

决策层是**确定性的规则**，跑在监管循环里，微秒级，不联网、不调用模型。它只决定"这件事
需不需要一个判断"，不决定怎么修。

agent 不在 harness 进程里：它是一个**由人启动的操作者会话**，读 `ams escalations`、
`ams ctl status`、服务日志，然后用和人一样的 CLI 去修——改声明再 `ams ctl reload`、
`ams secret set`、修上游代码等下一个 tick、`ams platform core release --rollback`。

为什么不让一个 LLM 在循环里自动判断 suppress/fix：服务打印的每一行都是该服务及其依赖
写的，也就是**攻击者可控的输入**；把它喂给一个运行在持有 harness uid、secret store 和
uid 映射的进程里的模型，就是给任意一个被投毒的依赖开了 prompt injection 通道。而且模型
相对规则唯一能"多做"的判断是**抑制**——恰恰是最危险的那个。完整说明和操作流程：
`docs/agent-loop.md`。

## 事件循环与慢操作

harness 是单进程、单线程、一个 `selectors` 循环。单线程是硬约束：harness 会裸
`os.fork()` 进 user namespace，fork 时别的线程持有的锁在子进程里永远不会释放（真实踩过：
`rm`、`mv`、`uv python install` 挂到被 SIGKILL）。所以：

- 健康探测是非阻塞 socket 状态机，不是线程池；
- `git fetch`、`pnpm install`、构建、插件发布、备份全部在**独立的一次性进程**里跑
  （定时器触发的 `ams platform core sync`、`ams provision`、备份 timer），通过控制 socket
  和 harness 说话，从不在循环里；
- 剩下仍可能在循环里停顿的都有上界：服务冷启动时一次 `chown -R`、退出后最多 2 s 等
  cgroup 清空、关机时 45 s 预算。

完整清单：`docs/event-loop.md`。

## 宿主要求

| | 必需？ | 说明 |
|---|---|---|
| Linux + systemd（委托 cgroup v2，`Delegate=yes`） | 必需 | 只在 Ubuntu 24.04 上验证过；systemd ≥ 255 有 `DelegateSubgroup=`，更老的版本 harness 会自己移进叶子 cgroup |
| shadow `uidmap`（setuid `newuidmap`/`newgidmap`）+ harness 的 `/etc/subuid` 区段 | 必需 | `install-host.sh` 安装并分配（取第一段空闲区段，不硬编码 100000） |
| AppArmor profile `ams-harness` | Ubuntu ≥ 23.10 必需 | 这些版本默认限制非特权 user namespace；profile 只对 harness 的私有解释器放行 `userns`，不放宽整机 |
| XFS `reflink=1` 存储 | **可选** | 有它时每个服务的 venv / `node_modules` 与共享缓存共享数据块，几乎不占额外空间；没有时（`AMS_STORE_FS=plain`）退化为普通复制，功能完全一样 |
| Caddy、R2（rclone 凭据） | 只有 platform 层要 | Caddy 是 core mode 的前端；R2 只用于备份 |
| macOS | 仅开发 | `--no-isolation`：不建 namespace，全部以当前用户运行 |

一台新的 Ubuntu 24.04 机器（root 执行一次）：

```bash
sudo deploy/install-host.sh                       # 默认：XFS loop 存储 + caddy 等工具
sudo AMS_STORE_FS=plain AMS_WITH_TOOLS=0 deploy/install-host.sh   # 最小：只要隔离
```

## 为什么不直接用 rootless podman / systemd --user

都考虑过（`docs/design/DECISIONS.md` D2、D4、D5、D7、D37）：

- **每个服务一段独立的 subuid。** `unshare --map-auto` 和 podman 的默认映射都是"一个用户
  一整段"，不是每服务一段；ams 自己做 `unshare` → 父进程 `newuidmap` 的握手。
- **harness 的 uid 在运行时不被映射。** podman 默认把调用者映射成容器里的 root，容器里
  root 逃逸出来就是那个用户；ams 的运行时映射里根本没有 harness uid。
- **服务是直接子进程，日志是 fd。** 每行输出都在同一个进程里过决策点；换成每服务一个
  unit 或容器，日志和生命周期就搬到了 journald / 容器运行时，决策点只能事后去读。

代价是自己维护约 1.6k 行 fork/unshare/newuidmap/cgroup 代码。它现在在真机和 CI 里跑，
但没有经过外部审计——这一点见上面的状态表。

## 快速上手：只用 supervisor 核心

```bash
uv pip install -e .                                # 提供 `ams` 命令（或者用 python -m ams）
export AMS_STATE_DIR=/tmp/ams-demo                 # macOS 上保持路径短（unix socket 104 字节上限）
mkdir -p $AMS_STATE_DIR/services/hello
cp examples/hello/service.toml $AMS_STATE_DIR/services/hello/
ams run --no-isolation &                           # Linux 宿主上去掉 --no-isolation
ams ctl status                                     # hello: running, healthy
ams escalations                                    # 规则处理不了的事
```

在装好的 Linux 宿主上，`deploy/ams-harness.service` 以 `harness` 身份跑 `ams run`；
`systemctl reload ams-harness`（或 `ams ctl reload`）重新读取声明，只增删改变化了的服务。

## platform 层：core mode

托管作者的 `api` v3.x（Cordis core：一个 Node 进程，插件由 core 自己热安装、探活、试用、
自动回滚）。ams 负责：让 core 活着；core 代码变化时发布（stage → 构建 → 停 → 翻转
`current` → 起 → 健康门禁，失败翻回）；通过 core 自己的控制 socket ship **内容**变了的
插件（同样条件下失败过的内容键永不重试）；渲染 Caddy；备份到 R2；把每个失败写成一条
escalation。不可信的步骤（`pnpm install` 的 lifecycle 脚本、构建、每次 `corectl`）都以
**服务自己的身份**运行。

```bash
ams platform core config import plugins.json --rebase /var/lib/core=@data --rebase /etc/core=@etc --jwt-dir ./keys
systemctl start ams-harness                  # ams run --policy platform
ams platform core bootstrap                  # 声明 caddy，首次发布，装好整个 roster
systemctl enable --now ams-core-sync.timer   # 之后每 60 s 一个 tick
ams platform core status                     # release / previous / held，每个插件的状态
ams platform core release --rollback
```

端到端说明：`docs/platform-core.md`。

## 布局

| 路径 | 职责 |
|---|---|
| `src/ams/schema.py` | `service.toml` → frozen dataclass，校验，`depends_on`，runtime 钉版本 |
| `src/ams/supervisor.py`、`health.py` | 单线程事件循环、生命周期、非阻塞健康探测、`waiting` |
| `src/ams/decision.py`、`events.py`、`escalations.py` | 抑制或修复的决策边界；escalation 日志 |
| `src/ams/isolated.py`、`userns.py`、`cgroup.py`、`uidmap.py` | Linux 隔离层：`run_admin`（admin map + mount mask）、`run_as_service`、subuid 分块 |
| `src/ams/ports.py`、`state.py`、`secrets.py`、`control.py` | 端口分配、状态目录、只写 secrets、控制 socket |
| `src/ams/runtime.py` | 供应（uv/venv/pnpm/bun）、托管 Node 工具链、`provision_tree` |
| `src/ams/cli.py` | `ams run/provision/validate/ctl/check-host/secret/escalations/platform` |
| `src/ams/platform/` | core mode：`core.py` 配置与布局、`coresync.py` tick、`corectl.py` 控制面、`gateway.py` Caddy、`backup.py`、`policy.py`、`sources.py` |
| `deploy/` | `install-host.sh`、systemd unit（harness、`ams-core-sync`、备份）、AppArmor profile |
| `scripts/` | `linux-test.sh`（在宿主上跑 Linux 测试）、`remote-test.sh`（经 ssh）、`deploy.sh`、`install-rclone.sh` |
| `docs/` | `service-declaration.md`、`agent-loop.md`、`event-loop.md`、`platform-core.md`、`design/`（决策记录、计划、进度、历史与证据） |

## 开发

```bash
uv venv --python 3.12 .venv && source .venv/bin/activate && uv pip install pytest pytest-timeout ruff
.venv/bin/python -m pytest -q                         # 可移植测试
ruff check . && ruff format --check src tests
sudo scripts/linux-test.sh                            # Linux 宿主上（install-host.sh 时带 AMS_TEST_DEPS=1）
AMS_HOST=<ssh 主机> scripts/remote-test.sh            # 同上，从本机经 ssh
```

设计决策及被否决的备选方案：`docs/design/DECISIONS.md`；进度：`docs/design/PROGRESS.md`；
变更记录：`CHANGELOG.md`。

## 许可证

MIT，见 `LICENSE`。
