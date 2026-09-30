# Changelog

本项目所有显著变更都记录在本文件中。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [Unreleased]

## [1.1.0] - 2026-09-29

托管 api v3.x 的 Cordis core（一个 Node 进程 + 插件），即 **core mode**；1.0.0 的
manifest 模式原样保留并标记为 deprecated。端到端说明见 `docs/platform-core.md`，
设计取舍见 `.claude/state/DECISIONS.md` D31/D32。Linux 隔离路径尚未在真机上跑过
（racknerd 已拆除），本地 macOS `--no-isolation` 端到端 6 个场景全部通过。

### Added

- **core mode**（`ams platform core`）：把 api core 作为一个 ams 服务托管——harness
  让它活着（userns + cgroup + `memory_max`，restart always），60 s 定时器上的一次性
  tick 负责 fetch → stage → 按 `core_paths` 判定是否发布 core → 按内容键 ship 插件
  → 等 probation 判定 → 授权 → 渲染 Caddy → 仅在有变化时写 `core.json`。子命令：
  `config import`、`bootstrap`、`sync`、`status [--json] [--offline]`、
  `release --rollback`、`ship <id>… [--force]`；都支持 `--no-isolation`（开发 / macOS）。
- `src/ams/platform/core.py`：`<state>/platform/core.toml`（未知键拒绝；foundation
  插件 `secrets, store, gateway, auth, health` 须按序打头；`[[site]]` 在加载时试渲染；
  控制 socket 路径长度在加载时校验）、`CoreLayout`、config bundle 的
  `import_bundle`（`--rebase OLD=NEW|@root|@data|@etc`，harness 持有 0700/0600 母本，
  只返回文件名）与 `place_bundle`（按摘要放置）、`core_declaration`、`flip_current`、
  `check_tree_pins`（`core.node` 须满足树的 `engines.node`，`core.pnpm` 须等于
  `packageManager`）。在计划之外新增两个带默认值的键：`caddy_port`（20180）与
  `health_timeout_s`（90）。
- `src/ams/platform/coresync.py`：tick、`bootstrap`、`ship`、`rollback_release`、
  `status_view`。core release 是 stop → 翻转 `current` → start → 「不回退」健康门禁
  （之前 `/health` 为 200 则须再为 200；否则切换前在服务的插件须全部恢复；否则
  gateway 端口须应答；否则控制 socket 应答即可），失败则翻回上一个 release 并 hold
  住该 sha。插件以**内容键**（上游 `computeArtifactId` 去掉 buildInfo）判断变化，
  结论记为 `probation | live | failed | blocked`，相同条件下失败的内容键永不重发。
  新插件部署后立即授予配置的特权并重启，使其在 probation 前就持有特权；ship 之后
  运行 `corectl gc`；人工 deploy/revert 被报告为 drift 而不被覆盖。
- `src/ams/platform/corectl.py`：经 api 树自己的 `scripts/corectl.mjs` 调用 core
  控制面（不在 Python 里重实现协议），以服务身份运行（socket 为 0660 且属服务 uid）；
  `CoreControlError.code` 解析 core 的错误码。
- `src/ams/platform/assets/core_plan.mjs`（包数据，不被 import）：用树自己的 node
  构建 roster 插件并逐行输出内容键；单个插件的游离异步错误只记到该插件。
- 托管 Node 工具链（`runtime.ensure_node_toolchain`）：从 nodejs.org 下载
  `.tar.gz`，按同目录 `SHASUMS256.txt` 校验 sha256，tarfile `data` filter 解包，原子
  rename 到 `<store>/node/v<ver>`；pnpm 由托管 npm 以 `ignore-scripts` 装入
  `<store>/pnpm/<ver>`。Linux/darwin × x64/arm64。
- `schema`：kind=pnpm 时 `runtime.node` 取精确 `X.Y.Z` 即选用托管工具链；新增
  `runtime.pnpm`（精确版本，需精确 node）与 `runtime.build`（安装后运行的 argv）。
- `runtime.provision_tree`：就地安装（并构建）一个已 stage 的 pnpm 仓库树；有 uid
  块时每一步都以服务身份运行，pnpm store/缓存/HOME 在服务自己的 `<root>/.cache`。
- `userns.run_as_service`：以服务自身身份（runtime map，inner 1000，
  `no_new_privs`，独立 session）一次性运行命令；`run_admin` 新增 `env`、`cwd`、
  `mask` 参数。
- `sources`：`SourceMirror.stage(..., dest=)`（core 用 `releases/<sha>`）与
  `stage_plain`（无 namespace 的开发模式）。
- `gateway.render_core` / `CoreSite`：core mode 的 Caddy 前端——每个 `[[site]]` 一个
  `sites/<host>.caddy`，纯 HTTP，host → `reverse_proxy 127.0.0.1:<port>`；严格小写
  RFC 1123 主机名；golden 在 `tests/golden/gateway/core/`。
- `backup`：不可变 byte store（`data/**` 下的 `blobs`、`oss-bytes`、`pages-content`，
  以及 core 顶层的 `data/artifacts`）以 `rclone copy --immutable` 复制到永不被
  retention 清理的 `bytes/<id>/<data 下路径>`，在所有 sqlite 快照之后进行；新增
  `ByteStoreSynced` / `ByteStoreFailed` 记录。
- `deploy/ams-core-sync.{service,timer}`：每 60 s 一个 tick，与 legacy sync unit 互斥
  （`Conflicts=`），由进程自己持 `core.lock`（不用 `flock(1)`）。
- `docs/platform-core.md`（core mode 端到端说明）与 DECISIONS D31/D32。

### Changed

- harness 优雅停机预算 `SHUTDOWN_BUDGET_S` 25 s → 45 s，`ams-harness.service` 的
  `TimeoutStopSec` 30 → 50，以容纳 core 的 40 s drain；预算改为在**停机时**按当时的
  声明计算（`Supervisor.run_forever` 接受 callable），bootstrap 之后才加入的 core 也能
  拿到 drain 时间。
- `ams run --no-isolation` 也加载 runtime 层：托管 node 的服务在 plain 模式下同样能在
  PATH 上找到 `node`（provisioning 本身仍只在隔离模式下进行）。
- `run_admin` / `run_as_service` 共用一个截止时间覆盖输出读取与等待（此前各自一个
  完整超时）；带独立 session 的子进程超时后整组 SIGKILL。
- legacy `provision()` 的 build 步骤改为以服务身份运行。
- `events`：Node 的
  ``(Use `node --trace-warnings ...` to show where the warning was created)`` 提示行
  不再因含 "warning" 一词被判为 WARNING（本地 e2e 中每次 core 启动都会升级一次）；
  它跟随的那条警告本身照常分级。

### Deprecated

- **legacy manifest mode**（api v2.0.0）：`service.yaml` 翻译（`yamlsubset`、
  `translate`）、`ams platform sync|status|bootstrap|rollback`、pools
  （`ams platform pool plan|adopt`、`assets/pool_runner.py`）、Layer-0 registry/auth
  （`bootstrap.py`、`layer0.py`、`registryclient.py`）、`ams-platform-sync.*` 与
  `scripts/platform-bootstrap.sh`。1.1.0 中原样保留、仍有测试覆盖；移除与否留给 2.0.0。

### Fixed

以下 core mode 条目修的是首版实现（a9fca88，未单独发布）在对抗评审与本地 e2e 中
暴露的问题，列出以便追溯：

- core sync：release / rollback / flip-back 的 health gate 在 harness 报告 core 为
  `failed`（重启策略耗尽，不会再拉起）时立即判失败，不再空等满 `health_timeout_s`
  ——启动即崩的 release 造成的中断从 ≈92 s 降到 ≈17 s。
- core sync：release 同时改变 core 声明时，reload 后 core 仍是 stopped 会被显式
  start（此前 gate 超时、好 sha 被错误 hold）；rollback 同理。
- core sync：core 拒收 artifact 字节（`invalid_manifest`/`artifact_too_large` …）记为
  FAILED 且 `planned_sha` 照常前进；只有传输失败进入 `ship_retry`，下一 tick 只重
  ship 这些插件，不再每 tick 重建整个 roster。
- core sync：关于 core 状态的拒绝（`dependency_unavailable`、`generation_conflict`
  …）记为 `blocked`，core 已装插件变化后重试一次；FAILED/blocked 记录带 release 与
  config bundle 摘要，core release 或 bundle 变更后重试一次。
- core sync：head 的 core 变更被 hold 时不从 head 树构建/ship 插件；stage 失败的 sha
  被 hold（不再每 tick 重跑 `pnpm install`），tick 其余部分（probation 判定、drift、
  privileges、gateway）照常运行；每次成功 stage 后回收旧 release 树。
- core sync：release gate 记录切换前正在服务的插件，切换后须全部恢复（此前
  `/health` 已红时任何回归都能通过）；首个 release 容忍 `artifact_unreadable` 的
  gateway；`artifact_unreadable` 的插件被重新安装；授权后的 restart 失败会升级并
  在后续 tick 重试；ship 之后运行 `corectl gc`。
- `core_plan.mjs`：插件模块作用域的未处理 rejection/异常记到该插件而不再让整个
  planner 退出，结束时显式 `process.exit`；planner 在非零退出时保留已输出的完整行。
- core stage 时校验 `core.node` 满足树的 `engines.node`、`core.pnpm` 等于
  `packageManager`，不符则拒绝（pnpm 只会警告或自行切换版本）。
- backup：core 的 `data/artifacts/` 作为不可变 byte store 备份；byte store 复制排除
  进行中的 `*.tmp`。

相对 1.0.0 的修复：

- `yamlsubset`：`_SUSPECT_NUMERIC_RE` 补上裸下划线整数形态——此前 `1_000` 被
  当作普通字符串静默接受，与「reject rather than guess」的承诺及
  `docs/manifest-translation.md` 的拒绝列表不符；现在与 `0755`/`.inf` 一样抛
  `YamlSubsetError`（PyYAML 会把 `1_000` 解析成 int，正是要挡掉的形态）。
- 文档与实现对齐（issues #6、#7、#9–#12）：README 与
  `deploy/ams-platform-sync.service` 注释不再声称 `secretsservice` 被 `--only`
  排除（pools 之后命名任一成员即选中整池）；`docs/platform-sidecars.md` 补
  `health_failed_at` 字段并更正 sidecar 的实际写入路径与换行行为；
  `docs/platform-pools.md` 的 adopt 数据搬迁改为整目录 rename 的描述，并补上
  admin 监听失败这一非零退出路径；`docs/service-declaration.md` 的
  `level-prefix` 补可选 `[member]` 标签。

### Security

- **core mode（review 2026-09-29，security-1/2/6）**：不再以 inner root（= harness
  uid，带 CAP_CHOWN）在 service 可控的路径上做会跟随符号链接的操作——planner 以
  service 身份放置 `core_plan.mjs`；`ensure_layout` 以 service 身份 `mkdir`、拒绝被
  换成 symlink 的布局项；`place_bundle` 与 `flip_current` 在 harness 所有的
  `<state>/services/core/` 里构建新 `etc`/新链接再 rename 进 root。
- **core mode（security-3/4）**：`provision_tree` 的 `pnpm install`（含依赖 lifecycle
  脚本）与 `scripts/build.mjs` 改为以 service 身份运行（runtime map 里没有 harness
  uid，也就没有可 `umount` 的 mask），pnpm store/缓存/HOME 在 service 自己的
  `<root>/.cache`；legacy `provision()` 的 build 步骤同样改为 service 身份。
- **security-5**：`provisioning_mask` / static `_build_mask` 额外遮蔽 `<store>/{platform,
  upstream,repos,src}`（平台 RS256 私钥与私有 mirror）及 harness HOME 的凭据文件
  （`.ssh`、`.npmrc`、`.config` …）。
- **托管工具链**：Node 归档按官方 `SHASUMS256.txt` 校验、tarfile `data` filter 解包；
  pnpm 以 `ignore-scripts` 安装；core 的 config bundle 值绝不进日志、argv 或 stdout。
- **issue #1**：不可信构建/置备代码（inner root = harness uid 的读权限）不再能看到
  harness 私有文件——`run_admin` 新增 `mask` 参数，在子进程私有 mount namespace 里
  把敏感路径盖成空挂载（目录 → 只读空 tmpfs，文件/socket → bind `/dev/null`）；
  static 构建（`_build_mask`）遮蔽 `<state>` 下除 static 子树外的全部内容，置备工具
  （`provisioning_mask`）遮蔽 SecretStore、兄弟服务树（含 Layer-0 密钥材料）、平台
  状态与控制 socket。已知残留（D32 Open）：被遮蔽的进程可 `umount` 或经
  `/proc/<harness pid>/root` 绕过——core mode 因此不再在 admin map 下跑不可信代码，
  legacy 仍依赖 mask。
- **issue #2**：一条病态 manifest 不再能击穿整个 sync tick——`yamlsubset` 为 flow
  嵌套加 64 层深度上限、整型/浮点字面量限长 64 位，不再让 `[[[[…` 与 6000 位整数以
  裸 `RecursionError`/`ValueError` 逃出 per-manifest 错误处理；`translate()` 把解析期
  异常包装为 `TranslateError`；sync/rollback 的 except 集合补
  `RecursionError`/`ValueError` 兜底，恢复「一条坏 manifest 只 fail 自己」。
- **issue #4**：overlay（`service.ams.toml`）的 `[env]`/`secrets` 名字禁止 harness
  注入名（`SVC_*`、`REGISTRY_URL`、`AUTH_URL`、`GIT_COMMIT`）——overlay 在
  translate 之后合并且无后续闸门，此前可把 `REGISTRY_URL` 指向外部 URL，让服务把
  注入的 `SVC_SECRET` 发往第三方。
- **issue #5**：static 发布前整树拒绝符号链接——`cp -a` 保留 repo 中的 symlink，
  Caddy `file_server` 会跟随它逃出 docroot；现在发布失败并点名违规链接，而不是把
  逃逸口发布到公网。

## [1.0.0] - 2026-09-03

首个版本：rootless supervisor 核心 + 把 api v2.0.0（`service.yaml` 清单的 Python
服务群）跑在它上面的 platform 层。在 racknerd（Ubuntu 24.04，1 vCPU / 2 GB）上
实跑：Layer 0 + 20 个服务的舰队，pools 之后 15 个成员共用一个进程。

### Added

- **supervisor 核心**：每个服务一个 user namespace（手写 fork 握手 +
  `newuidmap`/`newgidmap`，1024 宽 uid 块，inner uid 1000，运行时不映射 harness uid）
  与一个委托的 cgroup v2 子树（`memory.max` 连带 `memory.swap.max=0`、`cpu.max`、
  `pids.max`、`cgroup.kill`）；单线程 `selectors` 循环内联读取 stdout/stderr，经
  `DecisionPolicy` → `Escalation` 做抑制或修复决策，升级为 stdout JSONL；
  subreaper、重启退避、健康检查（tcp/http/log）、`depends_on` 启动门禁（`waiting`
  状态、环检测、逆拓扑停机）。
- **runtime provisioning**：venv / uv（含 `uv sync --frozen` 项目模式）/ pnpm / bun，
  在 admin namespace 里以 inner root 完成后 chown 给服务，环境放在 XFS reflink store
  上共享块。
- **控制与 secrets**：0600 控制 socket + SIGHUP 热重载（`ams ctl
  status|reload|start|stop|restart|kill`，声明变更不再重启 unit）；只写不读的
  SecretStore（`ams secret set|rm|list|check`），spawn 时注入。
- **platform 层（api v2.0.0）**：git 裸镜像 + 每 sha 规范检出 + reflink staging；
  受限 YAML 解析器与清单翻译器（拒绝而不猜测）；Layer-0 registry/auth 的 17 阶段
  拉起与 RS256 密钥；Caddy 网关渲染（固定端口 20180）；60 s 定时器上的 sync tick
  （单调阶段，一个不改变任何东西的 tick 不写任何东西）；按原因去重的 platform
  policy 与 sync 后健康门禁；`kind: static` 发布；每日 SQLite 备份到 R2 与恢复演练；
  单服务回滚到 `prev_sha`；`ams check-host`。
- **pools**：N 个 manifest 一个进程，按 tag 区分（D29），含 `ams platform pool
  plan|adopt` 数据迁移；racknerd 上 Layer-1 内存 712 → 275 MiB。
- `deploy/`：`ams-harness.service`（`Delegate=yes`）、sync 与 backup 定时器、
  AppArmor profile（只对 harness 解释器放开 `userns`）、`install-host.sh`。

### Fixed

- sync 的退出码只反映本次 tick，不再被记录中遗留的失败拖累（593daae）。
- pool adopt 在停止后整目录 rename 搬迁数据，容忍 pool 自有的数据目录；
  `level-prefix` 接受 pool 成员 tag；失败的健康门禁被 hold 900 s。
- 全新宿主部署（1 GB DO droplet 演练，D30）：`install-host.sh` 补 `libatomic1`；
  `layer0.py --no-layer1`；`sync._phase_finish` 先注册全部身份再开健康门禁。
- 健康门禁忽略处于终态的 static mount；一个失败的 `extra_env_for` 只让该服务失败。
