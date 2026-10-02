# Changelog

本项目所有显著变更都记录在本文件中。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### Fixed

- `deploy/install-host.sh` 在 GitHub runner 上失败：runner 的 `/etc/environment` 设了
  `XDG_CONFIG_HOME=/home/runner/.config`，`su -l` 经 pam_env 带进 harness 会话，uv 安装器
  往别人的家目录写。以 harness 身份执行的命令现在先清掉 `XDG_*`（`as_harness`）。
- **staging 在带 POSIX ACL 的宿主上失败**：admin namespace 里的 `cp -a` 会保留 ACL，
  ACL 里点名的宿主用户在 namespace 中没有映射 → `preserving permissions: Invalid
  argument`，core 根本无法 stage。改为 `cp -dR --preserve=timestamps,links`
  （`sources.STAGE_CP_FLAGS`），不再复制 ACL/xattr；权限位仍来自源文件，属主由随后的
  chown 设置。GitHub runner 上发现，在测试宿主上加一条默认 ACL 复现并验证。
- 非阻塞探测的回归测试不再依赖子进程的打印速度：从 talker 打出第一行才开始计时，断言
  "行到达的最大间隔 < 1 s" 而不是"行数 ≥ 20"（macOS runner 上 talker 每秒只有约 9 行）；
  对旧的阻塞实现仍然失败（2.01 s）。
- Linux 对照测试 `test_memory_max_alone_does_not_bound_a_greedy_process`（演示有 swap 时
  `memory.max` 不是硬上限）在宿主来不及换出、直接 OOM 时改为 skip 并说明原因；它是宿主
  状态相关的观察，产品依赖的保证由 `set_swap_max(0)` 的测试覆盖。
- `test_lines_are_tagged_assembled_and_flushed_at_eof` 在慢 runner 上偶发失败：它等到进程
  被回收就返回，而管道按设计还要排空；改为等到 finalize。

## [2.0.0] - 2026-10-01

外部 review 的整改版本。**破坏性变更：删除了 1.1.0 起 deprecated 的 legacy manifest
mode**；core mode 成为唯一的 platform 模式，通用 supervisor 核心与 platform 层的边界由
测试锁定。Linux 隔离路径（含此前从未实机跑过的 core mode `run_as_service` /
`run_admin` mask 路径）首次在一台 Ubuntu 24.04 宿主上跑通全部 Linux 测试。

### Added

- **Linux 隔离模式 core e2e 证据**：`docs/design/evidence/core-e2e-linux-2026-10-01.txt`
  （7 个场景：bootstrap、空闲 tick、单插件 ship、坏插件被拒、core release、回滚、harness
  重启；core 以独立 subuid 运行、CapEff 0、NoNewPrivs 1、harness uid 不在映射内）。
- **CI**（`.github/workflows/ci.yml`）：`portable`（ubuntu-24.04 + macOS，ruff + pytest）与
  `linux-isolation`（ubuntu-24.04 runner 上 `deploy/install-host.sh` 建 harness 用户、
  subuid、AppArmor profile，再 `scripts/linux-test.sh` 以 harness 身份在委托 cgroup 里跑
  全部测试）。这两步与在测试宿主上手工执行的完全相同。
- `LICENSE`（MIT），`pyproject.toml` 声明 `license = "MIT"`。
- **escalation 日志**（`ams.escalations`）：每条 escalation 除了写 stdout，还追加到
  `<state>/logs/escalations.jsonl`（0600，单次 `O_APPEND` 写，超 8 MiB 轮转一代），
  带 `ts` 与 `source`（`harness` / `core-sync` / `backup`，备份只记失败）。
- `ams escalations [-n N] [--service ID] [--since ISO] [--json]`：读取该日志；默认的
  人类可读格式会剥掉终端控制字符（记录里是服务原样输出，不可信）。
- `docs/agent-loop.md`：决策层是确定性规则，agent 是由人启动、消费 escalation 的操作者
  会话；为什么不让 LLM 进 suppress-or-fix 循环（日志文本是攻击者可控输入等）；操作流程。
- `tests/test_core_boundary.py`：锁住"supervisor 核心不依赖 platform"——核心模块不在
  模块级 import `ams.platform`，只有 `cli` 会懒加载它；`ams.platform` 不可导入时 CLI
  照常 validate/run，`platform` 子命令自动消失，`--policy platform` 给出明确错误。
- `examples/hello/service.toml`：与 api 无关的最小通用服务示例。
- `scripts/linux-test.sh`：在宿主本地以 `harness` 身份、`systemd-run -p Delegate=yes`
  跑 Linux 测试；CI 与 `scripts/remote-test.sh` 共用。
- `deploy/install-host.sh` 新增开关：`AMS_STORE_FS=xfs|plain`（plain 不建 XFS loop
  文件）、`AMS_WITH_TOOLS=0`、`AMS_TEST_DEPS=1`、`AMS_INSTALL_UNIT=0`。

### Changed

- **README 重写**：写清这是给愿意自己管 Linux 机器的操作者及其 agent 的，不是"一键托管"；
  状态表逐项列出验证过什么、没验证什么（无生产使用、无外部审计、CI 尚未在 GitHub 跑过）；
  supervisor 核心与 platform 层分开介绍；"agent 在哪里"、"事件循环与慢操作"、宿主要求
  （必需 vs 可选）、"为什么不用 rootless podman / systemd --user"。
- 新增 `docs/event-loop.md`（循环里跑什么、什么绝不能进循环、仍可能停顿的有界操作）。
- `docs/platform-core.md` 状态更新为 Linux 隔离模式实测结果；`docs/design/DECISIONS.md`
  新增 D33–D37。
- core mode 复用的小函数移到 `ams.platform.common`（`write_if_changed`、
  `uid_allocator`、`ctl_reload`、`ctl_restart`）；`gateway.caddy_declaration(state, store,
  port)` 直接生成固定端口的 Caddy 声明（取代 `layer0._caddy_declaration_text`）。
  已部署宿主上 caddy 的 `service.toml` 注释会变，首次 tick 会重写它并重启 caddy 一次。
- `scripts/deploy-racknerd.sh` → `scripts/deploy.sh`：`AMS_HOST` 必填，远端用 `sudo -n`。


- **tcp/http 健康探测不再阻塞 supervisor 循环。** 每次探测是一个 `ams.health.Probe`：
  非阻塞 socket + 小状态机（connect → send → 读状态行），注册进 supervisor 自己的
  selector，`deadline` 并入计时器；一个永不应答的 `/health` 只占一个 fd 和一个计时器。
  此前 http 探测内联调用 `http.client`，每次最多阻塞整个循环 `health.timeout_s`，
  期间所有服务的日志读取、重启和探测都被推迟。探测超时的 detail 为
  `timed out after Ns (<阶段>)`。`check_tcp` / `check_http` 保留给循环外的一次性调用者，
  复用同一状态机。
- **XFS reflink 存储变为可选。** pnpm 的 import method 统一为 `clone-or-copy`
  （`ams.runtime.PNPM_IMPORT_METHOD`）：XFS `reflink=1` 上照样 clone，ext4 等文件系统上
  复制。此前隔离路径用硬 `clone`，在非 reflink 文件系统上 `pnpm install` 直接失败——
  这是"必须 XFS"的唯一来源。uv 的 `clone` 本来就会回退到复制。
- `scripts/remote-test.sh` 不再写死 racknerd：`AMS_HOST=<ssh 主机>`，远端以有免密 sudo
  的登录用户运行 `scripts/linux-test.sh`。

### Removed

- `.claude/state/` 不再进版本库：设计记录移到 `docs/design/`（`DECISIONS.md`、
  `PLAN-core.md`、`PROGRESS.md`），1.0.0 时期的工作笔记与证据移到
  `docs/design/history/`，加 `docs/design/README.md` 索引；所有引用同步更新，个人
  绝对路径脱敏。`.gitignore` 改为整体忽略 `.claude/`（`CLAUDE.md` 除外）。

- **legacy manifest mode**（api v2.0.0）整体删除：`ams.platform.{yamlsubset, translate,
  sync, bootstrap, layer0, registryclient, rollback, pool, static}`、`assets/pool_runner.py`；
  CLI `ams platform {sync, status, bootstrap, rollback, pool}`；
  `deploy/ams-platform-sync.{service,timer}` 及 `ams-core-sync.service` 上的 `Conflicts=`；
  `scripts/platform-bootstrap.sh`、`scripts/pilot-api.sh`；`examples/api-pilot`、
  `examples/platform`；`docs/platform.md`、`docs/platform-pools.md`、
  `docs/manifest-translation.md`、`docs/platform-sidecars.md`；对应的测试、goldens
  （`tests/golden/platform/`、`tests/golden/gateway/{path,subdomain,static,tls,logdir}`）
  与 fixtures。约 9 千行源码、1.4 万行测试。
- `gateway.render()`（按 mount sidecar 渲染）及其 CSP/CORS/static 数据；`GatewayConfig`
  不再有 `static_root`。
- `PlatformPolicy` 的 post-sync 健康门禁、sync 后崩溃循环提示与 registry 心跳降噪——它们
  读的是 legacy 的 `<state>/platform/state.json`，core mode 下从不触发；
  `DedupingEscalation`（只有 legacy sync 用）。`make_policy()` 不再接受 state。
- backup 的 pool 成员标签（`Target.label`、`ByteStore.label`）。

### Fixed

- **wheel 缺少 `assets/core_plan.mjs`**：`pyproject.toml` 未声明 package data，pip 安装
  的 ams 跑 core mode 会找不到 planner（rsync 源码树部署不受影响，所以一直没暴露）。
- **gateway：未知 Host 不再得到 Caddy 默认的空 200。** 入口站点原为
  `http://127.0.0.1:<port>`，只匹配 Host 127.0.0.1；任何没有 `[[site]]` 的 Host 都拿到
  空 200，与正常路由无法区分。入口改为同端口 catch-all `http://:<port>`（具体 host 的
  站点优先匹配），未知 Host 得到 JSON 404。在 Linux 实机 e2e 中发现。
- `test_run_as_service_timeout_kills_the_whole_group` 在 Linux 上误报：前面的测试让
  pytest 进程成了 child subreaper，被杀的孙进程成了僵尸，`kill(pid, 0)` 仍成功；改为
  僵尸也算已退出。

- `deploy/install-host.sh` 在 useradd 没分配 subuid 时不再写死 `100000:65536`
  （宿主第一个登录用户通常已占用），改取所有现有区段之后的第一段。
- Linux 测试不再写死 uid 块 `100000`，改从 harness 真实的 `/etc/subuid` 推导
  （`tests/linux/linuxhost.py`）；在 harness 区段不是 100000 的宿主上它们原本全部失败。
- `test_files_it_writes_belong_to_the_block_on_the_host` 的断言本身是错的：harness
  按设计无法进入服务 0750 的 `data/`，改在 admin namespace 里 stat。
- reflink 省空间的测量从 staging 正确性测试中拆出，非 reflink 存储上只跳过测量。

## [1.1.0] - 2026-09-29

托管 api v3.x 的 Cordis core（一个 Node 进程 + 插件），即 **core mode**；1.0.0 的
manifest 模式原样保留并标记为 deprecated。端到端说明见 `docs/platform-core.md`，
设计取舍见 `docs/design/DECISIONS.md` D31/D32。Linux 隔离路径尚未在真机上跑过
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
