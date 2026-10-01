![ams](./docs/ams.webp)

# ams — agent managed services

一个面向**智能体托管运行时**的 rootless 监管器（supervisor）核心：智能体启动
它自己声明的服务，把这些服务当作直接子进程持有，内联读取它们的 stdout/stderr，
并对每条 warning/error 做出明确的**抑制（suppress）或修复（fix）**决策。
不用 Docker，没有 per-service systemd unit，不需要 root。

```
systemd (keeps the harness alive, Delegate=yes)
├── harness  (unprivileged user, PR_SET_CHILD_SUBREAPER, the agent loop lives here)
│   ├── caddy   :20180   (host → loopback port, an ams service like any other)
│   └── core    :18080   (one Node 24 process; the api plugins live inside it)
└── ams-core-sync.timer  (60 s: fetch → stage → release core → ship changed plugins)
```

在这套核心之上是 **platform 层**（`src/ams/platform/`），1.1.0 起有两种模式：

- **core mode（1.1.0，当前）**——托管 api v3.x 的 Cordis core：一个 Node 进程，
  插件由 core 自己热安装、探活、试用期、自动回滚。ams 负责让它活着、在 core 代码
  变化时发布 core（stage → 构建 → 停 → 翻转 `current` → 起 → 健康门禁，失败则
  翻回）、通过 core 自己的控制 socket ship **内容**变了的插件、渲染 Caddy 前端、
  备份到 R2、把每个失败升级成一行 JSON。端到端故事：`docs/platform-core.md`。
- **legacy manifest mode（1.0.0，1.1.0 起 deprecated）**——api v2.0.0 的
  `service.yaml` 清单翻译、pools、Layer-0 registry/auth。原样保留，
  `docs/platform.md`。移除与否是 2.0.0 的决定。

两种模式绝不能跑在同一个 state 目录上（`ams-core-sync.service` 对 legacy sync
unit 设了 `Conflicts=`）。

## 你能得到什么

- **无需特权的隔离。** 每个服务运行在自己的 user namespace 里，通过
  `newuidmap`/`newgidmap` 映射进 harness 的 `/etc/subuid` 块，零 capability 且
  `no_new_privs`。限额（`memory.max`、`memory.swap.max=0`、`cpu.max`、
  `pids.max`）写入委托的 cgroup v2 子树；`cgroup.kill` 原子性地杀掉整棵树。
- **日志是 fd，不是 journald。** 每一行被打上 `[svc:stream]` 标签，按严重度
  分类，经过显式的 `DecisionPolicy` → `Escalation` 接口路由。`--policy platform`
  在默认策略之上叠加按原因去重、sync 后健康门禁和 Caddy 专用规则。
- **不可信代码不以 harness 身份运行。** core mode 下 `pnpm install`（含依赖的
  lifecycle 脚本）、`scripts/build.mjs`、插件构建和每次 `corectl` 调用都以
  **服务自己的身份**运行（`run_as_service`：runtime map 里根本没有 harness uid），
  pnpm store/缓存/HOME 在服务自己的 `<root>/.cache`。
- **托管工具链。** `runtime.node = "24.20.0"` 这种精确版本会从 nodejs.org 下载
  并按 `SHASUMS256.txt` 校验 sha256，装进 store；`runtime.pnpm` 用托管的 npm 安装
  （`ignore-scripts`）。树里的 `engines.node` / `packageManager` 必须与之一致。
- **按内容 ship，失败不重试。** 插件以内容键（artifactId 去掉 buildInfo）判断是否
  变化；core 拒收或自动回滚过的内容键在相同条件下永不重发（没有重试风暴），core
  release、config bundle 或（对 `blocked`）core 已装插件变了才再试一次。
- **用声明，不用 unit 文件。** 每个服务一个 `service.toml`（启动 argv、
  workdir、env、secret *名字*、端口、runtime、健康检查、优雅停止、限额、
  重启策略、`depends_on`）——由智能体、core mode 或清单翻译器生成。见
  `docs/service-declaration.md`。
- **只写不读的 secrets。** `ams secret set <id> NAME` 从 stdin 读入值，以 0600
  harness 属主存储，并在 spawn 时注入；core 的 config bundle（`plugins.json`、
  JWT 密钥、字体）同理，`config import` 与 `status` 只打印名字。
- **每服务独立控制。** 一个 0600 unix socket，直接由 supervisor 自己的
  selector 循环服务：`ams ctl reload|start|stop|restart|kill <id>`。声明变更
  不再需要重启 unit。

## 当前状态（2026-09-30，ams 1.1.0）

| | |
| --- | --- |
| 可移植测试 | 1608 passed / 167 skipped（`.venv/bin/python -m pytest -q`） |
| core mode 本地端到端 | macOS、`--no-isolation`、真实 api（v3.1.0 的 scratch clone）：6 个场景全过——bootstrap 74 s、无变化 tick 0.17 s 且不写任何文件、单插件内容变更只 ship 该插件、坏插件被拒且只升级一次、core release（`/health` 断约 2 s）、`release --rollback` 3 s。证据：`docs/design/history/evidence/core-e2e-local-2026-09-29.txt` |
| Linux 隔离路径 | **从未在真机上跑过。** racknerd 复刻环境已拆除，重建它是下一步（由用户决定） |
| 生产 | 仍是 phm 上的 systemd（不在 ams 范围内，ams 从不连接 phm） |

1.0.0 legacy 模式在 racknerd 上的实测数字（Layer 0、20 个服务的舰队、pools）
保留在 `docs/design/history/platform-layer0.md`、`platform-fleet.md`、
`pool-migration.md`，它们是历史观察值，不是规划常量。

## 布局

| 路径 | 职责 |
|---|---|
| `src/ams/schema.py` | 声明 → frozen dataclass，校验，`depends_on`，`runtime.node/pnpm/build` 钉版本 |
| `src/ams/supervisor.py`、`health.py` | 单线程事件循环、生命周期、健康探测、`waiting` |
| `src/ams/decision.py`、`events.py` | 抑制或修复的决策边界 |
| `src/ams/isolated.py`、`userns.py`、`cgroup.py` | Linux 隔离层；`run_admin`（admin map，可带 mount mask）、`run_as_service`（以服务身份一次性运行） |
| `src/ams/uidmap.py`、`ports.py`、`state.py` | subuid 块、端口分配、状态目录 |
| `src/ams/runtime.py`、`secrets.py`、`control.py` | 供应、托管 Node 工具链、`provision_tree`、只写 secrets、控制 socket |
| `src/ams/cli.py` | `ams run/provision/validate/ctl/check-host/secret/platform` |
| `src/ams/platform/core.py` | core mode：`core.toml`、布局、config bundle、`service.toml` 生成、`current` 翻转、树钉版本检查 |
| `src/ams/platform/coresync.py` | core mode 的一次 tick、release/rollback/ship、`core.json` 记录 |
| `src/ams/platform/corectl.py`、`assets/core_plan.mjs` | 经上游 `scripts/corectl.mjs` 的控制面；内容键 planner（包数据，不被 import） |
| `src/ams/platform/sources.py` | 裸镜像、每 sha 规范检出、staging（`dest=`、`stage_plain`） |
| `src/ams/platform/gateway.py`、`static.py` | Caddyfile 渲染器（`render_core` 与 legacy `render`）、`kind: static` 发布 |
| `src/ams/platform/backup.py` | 每日 SQLite 快照 + 不可变 byte store（`bytes/` 前缀）到 R2 |
| `src/ams/platform/policy.py` | platform 决策策略、按原因去重 |
| `src/ams/platform/{yamlsubset,translate,sync,bootstrap,layer0,registryclient,rollback,pool}.py` | legacy manifest mode（deprecated） |
| `deploy/` | systemd unit（harness、`ams-core-sync` 定时器、legacy sync、backup）、AppArmor profile、`install-host.sh` |
| `scripts/` | `deploy-racknerd.sh`、`remote-test.sh`、`platform-bootstrap.sh`、`install-rclone.sh` |

## 宿主要求

Ubuntu 24.04（或任何具备 cgroup v2 + shadow `uidmap` 的 Linux）。
`deploy/install-host.sh` 创建 `harness` 用户，安装只对 harness 解释器授予
`userns` 的 AppArmor profile（24.04 限制非特权 user namespace），挂载 XFS
reflink 存储，并安装钉死版本的静态 Caddy 二进制。

## core mode 快速上手

以 `harness` 身份，在装好的宿主上：

```bash
# 1. 把 api 的裸镜像推到 <store>/upstream/api.git（api 是私有仓库，harness 没有凭据）
# 2. 写 <state>/platform/core.toml（完整参考：docs/platform-core.md）
ams platform core config import plugins.json \
    --rebase /var/lib/core=@data --rebase /etc/core=@etc \
    --jwt-dir ./keys --fonts ./fonts         # 只打印文件名，绝不打印值
systemctl start ams-harness                  # ams run --policy platform
ams platform core bootstrap                  # 声明 caddy，首次 release，装好整个 roster
systemctl enable --now ams-core-sync.timer   # 之后每 60 s 一个 tick
```

日常运维：

```bash
ams platform core status                     # release / previous / held，每个插件的 phase、artifact、ams 的上次尝试、drift
ams platform core sync                       # 手动跑一个 tick；定时器跑的是同一套代码
ams platform core release --rollback         # 翻回上一个 release，并 hold 住回滚前的 sha
ams platform core ship timeservice --force   # 立即从已 stage 的树 ship（--force 无视内容键记录）
ams ctl status                               # harness 视角：什么在跑、是否健康
```

本地开发（macOS 也行）：`ams run --no-isolation --policy platform` 加上各命令的
`--no-isolation`，全部以当前用户身份运行，没有 user namespace。

## legacy manifest mode

api v2.0.0 的路径（`ams platform sync|status|bootstrap|rollback|pool`、
`scripts/platform-bootstrap.sh`、`ams-platform-sync.timer`）在 1.1.0 中原样保留，
标记为 deprecated。文档：`docs/platform.md`、`docs/platform-pools.md`、
`docs/manifest-translation.md`、`docs/platform-sidecars.md`。

## 开发

```bash
uv venv --python 3.12 .venv && source .venv/bin/activate && uv pip install pytest pytest-timeout ruff
.venv/bin/python -m pytest -q       # 可移植测试
scripts/remote-test.sh              # linux 标记的测试，在目标宿主上跑
```

设计决策及其被否决的备选方案在 `docs/design/DECISIONS.md`（core mode 是
D31/D32）；进度在 `docs/design/PROGRESS.md`；core mode 的计划在
`docs/design/PLAN-core.md`；变更记录在 `CHANGELOG.md`。
