![ams](./docs/ams.webp)

# ams — agent managed services

一个面向**智能体托管运行时**的 rootless 监管器（supervisor）核心：智能体启动
它自己声明的服务，把这些服务当作直接子进程持有，内联读取它们的 stdout/stderr，
并对每条 warning/error 做出明确的**抑制（suppress）或修复（fix）**决策。
不用 Docker，没有 per-service systemd unit，不需要 root。

```
systemd (keeps the harness alive, Delegate=yes)
└── harness  (unprivileged user, PR_SET_CHILD_SUBREAPER, the agent loop lives here)
    ├── registry / auth  (Layer 0, fixed ports 20100/20101)
    ├── caddy            (the gateway, an ams service like any other)
    └── kvservice / timeservice / …   (translated from the api monorepo)
```

在这套核心之上现在还有一层 **platform 层**（`src/ams/platform/`）：它镜像一个
git 仓库，把 `service.yaml` 清单翻译成声明，在 reflink 存储上为每个服务做
provision 和 staging，渲染 Caddy 网关，创建 registry 身份并对健康状态设门禁
——全部由一个跑在 60 秒定时器上的一次性进程完成。`docs/platform.md` 是
端到端的完整故事。

## 你能得到什么

- **无需特权的隔离。** 每个服务运行在自己的 user namespace 里，通过
  `newuidmap`/`newgidmap` 映射进 harness 的 `/etc/subuid` 块，零 capability 且
  `no_new_privs`。限额（`memory.max`、`memory.swap.max=0`、`cpu.max`、
  `pids.max`）写入委托的 cgroup v2 子树；`cgroup.kill` 原子性地杀掉整棵树。
- **日志是 fd，不是 journald。** 每一行被打上 `[svc:stream]` 标签，按严重度
  分类，经过显式的 `DecisionPolicy` → `Escalation` 接口路由。`--policy platform`
  在默认策略之上叠加按原因去重、sync 后健康门禁和 Caddy 专用规则。
- **共享磁盘的依赖隔离。** 每个服务的环境（uv / venv / pnpm / bun）与各缓存
  一起放在 XFS `reflink=1` 卷上。实测：再复制一份 17 MiB 的树只多花 0.01 MiB。
- **用声明，不用 unit 文件。** 每个服务一个 `service.toml`（启动 argv、
  workdir、env、secret *名字*、端口、runtime、健康检查、优雅停止、限额、
  重启策略、`depends_on`）——由智能体或清单翻译器生成。见
  `docs/service-declaration.md`。
- **只写不读的 secrets。** `ams secret set <id> NAME` 从 stdin 读入值，以 0600
  harness 属主存储，并在 spawn 时注入。没有任何途径把它打印回来，而且
  harness 的 uid 不映射进服务的 namespace，所以服务真正无法从磁盘读回它。
- **每服务独立控制。** 一个 0600 unix socket，直接由 supervisor 自己的
  selector 循环服务：`ams ctl reload|start|stop|restart|kill <id>`。声明变更
  不再需要重启 unit。

## 当前在线（racknerd 复刻环境，2026-09-02）

registry、auth 和 Caddy 作为 ams 服务，与试点 Layer-1 服务一起运行在同一个
`ams-harness.service` 下。带全部数字的完整记录：`.claude/state/platform-layer0.md`。

| | |
| --- | --- |
| 在线且健康的服务 | 7（registry、auth、caddy、kvservice、timeservice、hello、pyhello） |
| harness cgroup `memory.current` | 共 378.9 MiB |
| registry / auth，冷态 | 92.8 / 94.8 MiB，上限 200 M |
| 宿主内存 used，Layer 0 前 → 后 | 553 → 710 MiB / 1967 |

端到端验证：由复刻 registry 为 `timeservice` 铸造的真实 RS256 M2M token，经
Caddy 被 `kvservice` 接受（204/200）；匿名请求得 401，签名被篡改也得 401。
上面每个数字都是页面缓存污染宿主上的 n=1——它们是观察值，不是规划常量。

全舰队（full-fleet）拉起的数字在 `.claude/state/platform-fleet.md`。写这份
README 时它**还是一张没有填完的骨架表**——舰队规模的数字以它为准，不以本节
为准。这项工作已经定下的事：2 GB 加一个 vCPU 装不下全部 21 个服务，所以
`deploy/ams-platform-sync.service` 驱动一个显式 `--only` 列表。pools 之后这个
列表的语义变了：其中 8 个 id 是 `pool-core` 的成员，而命名任一成员 sync 就会
选中整个池（15 个成员）——所以凭据缺失、起不来的 `secretsservice` 仍随整池在
每个 tick 里被拉起，并烧掉自己的 90 s 健康门禁；真正留在 tick 之外的只有
standalone 的 `oss`。后果与量级见 `docs/platform-pools.md`。

## 布局

| 路径 | 职责 |
|---|---|
| `src/ams/schema.py` | 声明 → frozen dataclass，校验，`depends_on` |
| `src/ams/supervisor.py`、`health.py` | 单线程事件循环、生命周期、健康探测、`waiting` |
| `src/ams/decision.py`、`events.py` | 抑制或修复的决策边界 |
| `src/ams/isolated.py`、`userns.py`、`cgroup.py` | Linux 隔离层 |
| `src/ams/uidmap.py`、`ports.py`、`state.py` | subuid 块、端口分配、状态目录 |
| `src/ams/runtime.py`、`secrets.py`、`control.py` | 供应（provisioning）、只写 secrets、控制 socket |
| `src/ams/cli.py` | `ams run/provision/validate/ctl/check-host/secret/platform` |
| `src/ams/platform/sources.py` | 裸镜像、每 sha 规范检出、reflink staging |
| `src/ams/platform/yamlsubset.py`、`translate.py` | 受限 YAML 解析器、清单 → 声明 + 伴生文件 |
| `src/ams/platform/sync.py`、`cli.py` | sync tick 与 `ams platform` |
| `src/ams/platform/bootstrap.py`、`layer0.py` | Layer-0 密钥对/secrets/声明，17 阶段拉起 |
| `src/ams/platform/gateway.py`、`static.py` | Caddyfile 渲染器、`kind: static` 发布 |
| `src/ams/platform/registryclient.py`、`policy.py` | 身份 + ACL、platform 决策策略 |
| `src/ams/platform/backup.py`、`rollback.py` | 每日 SQLite 备份到 R2、单服务回滚 |
| `deploy/` | systemd unit（harness、sync 定时器、backup 定时器）、AppArmor profile、`install-host.sh` |
| `scripts/` | `deploy-racknerd.sh`、`remote-test.sh`、`platform-bootstrap.sh`、`install-rclone.sh` |
| `examples/platform/` | Layer-0 与 Caddy 声明 |

## 宿主要求

Ubuntu 24.04（或任何具备 cgroup v2 + shadow `uidmap` 的 Linux）。
`deploy/install-host.sh` 创建 `harness` 用户，安装只对 harness 解释器授予
`userns` 的 AppArmor profile（24.04 限制非特权 user namespace），挂载 XFS
reflink 存储，并安装钉死版本的静态 Caddy 二进制。

## 运维快速上手

在工作站上（两者都通过 ssh 驱动那台机器）：

```bash
scripts/deploy-racknerd.sh              # rsync + 安装 unit + 重启
scripts/platform-bootstrap.sh           # 推送源码镜像，然后 17 阶段
                                        # Layer-0 拉起。加 --no-deploy 跳过
                                        # rsync。幂等。
```

在机器上，以 `harness` 身份：

```bash
ams check-host                          # 每一项前置条件，附每项失败的修法
ams ctl status                          # 什么在跑、是否健康、在哪个端口
ams platform status                     # 每服务所处阶段、sha、上次健康的提交
ams platform sync --only kvservice      # 手动跑一个 tick；定时器跑的是同一套代码
ams platform rollback kvservice         # 回滚到记录里的 prev_sha，或 --to SHA
ams secret set commentservice DEEPSEEK_API_KEY < key.txt
systemctl reload ams-harness            # 等价于 ams ctl reload
systemctl list-timers 'ams-platform-*'  # 每 60 秒 sync，每天 04:10 UTC 备份
```

## 开发

```bash
uv venv --python 3.12 .venv && source .venv/bin/activate && uv pip install pytest pytest-timeout ruff
.venv/bin/python -m pytest -q       # 可移植测试
scripts/remote-test.sh              # linux 标记的测试，在目标宿主上跑
```

设计决策及其被否决的备选方案在 `.claude/state/DECISIONS.md`；进度在
`.claude/state/PROGRESS.md`；Phase-B（生产切换）清单在
`.claude/state/phase-b-prereqs.md`。
