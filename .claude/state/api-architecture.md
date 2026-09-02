# Project Hail Mary — 现状运维架构地图

只读勘察，2026-09-02，针对 `~/Codes/AgentManangedServices/api`（clone of
`StevenLi-phoenix/api`）。目标：为重新设计"运行/运维层"提供依据。所有结论均标注来源文件；无法直接
读到源码的地方（droplet 实际运行状态）标注为推断。

## 1. 服务清单

单一 Ubuntu droplet（DO，NYC1，tailnet 主机名 `platform`，IP `100.64.0.10`），单一 `deployer`
daemon 驱动全部 Layer-1 部署。所有服务：Python 3.12（`.python-version`），`uv` 管理 venv，
FastAPI + `uvicorn --factory` 起进程，监听 `127.0.0.1:<port>`（永不直接暴露公网口）。SQLite 是
唯一 DB 引擎（无 Postgres/MySQL）。全部服务经统一 `service.yaml` 声明（schema 见
`components/deployer/schemas/service.schema.json`），字段：`deploy`(source/install/target_dir/user)、
`process`(exec/environment/restart/memory_max)、`mount`(gateway/path 或 subdomain/port)、`acl`、
`registry.capabilities/health_path`。

### Layer 0（信任根，bootstrap 手动管理，*不*经 webhook 部署）
| 组件 | 端口 | 职责 | 部署方式 |
|---|---|---|---|
| `components/auth` | 8001 | OAuth/JWT/PAT/WebAuthn/Emergency access，唯一 RS256 私钥持有者 | `bootstrap/08-auth-init.sh` + `bootstrap/redeploy.sh auth` |
| `components/registry` | 8002 | 服务目录/heartbeat/discover/MCP/ACL | `bootstrap/09-registry-init.sh` + `redeploy.sh registry` |
| `components/deployer` | 8003 | webhook→部署流水线本体 | `bootstrap/07-deployer-init.sh`；**自我重部署手动**（work-tree 直跑，redeploy.sh 拒绝 `deployer` 参数） |

三者都无 `service.yaml`（`ls` 确认不存在），systemd 状态/env/dir 由各自 `0X-*-init.sh` 手写。

### Layer 1（`services/*` 平台服务，Deployer 全自动管理）
9 个（不含 `admin-frontend.caddy.disabled`），来源 `services/*/service.yaml`：

| 服务 | 端口 | root path | mem cap | 数据 | 备注 |
|---|---|---|---|---|---|
| commentservice | 9211 | /comments | 150M | sqlite | 依赖本地 AUTH_URL 回环 + DeepSeek 审核 |
| emailservice | 9212 | /email | 100M | sqlite | Resend provider，M2M-only |
| kvservice | 9203 | /kv | 100M | sqlite | 通用 KV |
| llmgateway | 9219 | /llm | (none) | sqlite | 计量 LLM 代理，provider key 手工加 env |
| logservice | 9202 | /logs | 200M | sqlite | 日志聚合 |
| messageservice | 9209 | /message | 100M | sqlite | 消息总线，被 deployer 失败告警复用 |
| notificationservice | 9213 | /notify | 100M | sqlite | Bark push，M2M-only |
| oss | 9204 | /oss | 200M | sqlite(元数据)+R2 | 对象存储代理 |
| secretsservice | 9205 | /secrets | 100M | sqlite | **注**：Layer-2 运行时 secret 拉取被判 YAGNI 未实现（见 §6），此服务的实际使用范围有限——弱信号，未深入验证 |
| wechatservice | 9214 | /wechat | 150M | 无DB(token cache json) | admin-only |

### `apps/*`（业务服务 + 前端 + 静态站，Deployer 全自动管理，除标注）
| 服务 | kind | 端口/挂载 | 备注 |
|---|---|---|---|
| timeservice | service | 9200 /time | |
| resume | service | 9201 /resume | |
| files | service | 9206 /files | 匿名可写，有配额/限流 |
| displayservice | service | 9207 display.lishuyu.app (子域) | TRMNL 电子墨水屏 |
| locationservice | service | 9208 location.lishuyu.app | 隐私数据，无 anon |
| mailbox | service | 9215 mail.lishuyu.app | CF catch-all 收件箱后端 |
| pages | service | 9216 pages.shuyuli.com | 托管**不可信**用户 HTML，sandbox CSP；2026-08-30 才从关停状态复活并换域 |
| turingtest | service | 9217 turning-test.lishuyu.app | 匿名图灵测试游戏 |
| llmpricing | service | 9218 /llm-live-pricing | |
| llm-web | **static** | llm.lishuyu.app (子域) | 手写 HTML+JS，无构建步骤 |
| files-web | **static** | file.lishuyu.app (子域) | bun build，`kind: static` 走独立流水线 |
| test-service | — | 无 service.yaml | 纯本地 SDK 冒烟测试，从不部署 |
| iwatchpet | — | `service.yaml.disabled` | 已下线，代码/迁移仍在仓库 |

`frontend/{admin,home,task}`：静态 SPA，部署在 **Cloudflare Pages**，完全在 Deployer/droplet 之外（`docs/architecture.md` 组件表确认）。

**语言/运行时结论**：清一色 Python 3.12 + uv + FastAPI/uvicorn + SQLite，无 Node 后端服务（`llm-web`/`files-web` 是纯前端）。无状态性：所有 `kind: service` 均有本地 SQLite（`/var/lib/<name>/`），因此**均非无状态**——重启不丢数据但水平扩展不可行（单实例、单文件锁）。

## 2. 进程监管（systemd）

- **无 systemd `templates/`**，每服务一个具名 unit：`components/deployer/templates/systemd.service.j2` 渲染 `/etc/systemd/system/<name>.service`。
- `Type=simple`，`User=<manifest.deploy.user>`（每服务独立 unix 系统用户，`useradd -r -s /bin/false`，无 `DynamicUser=`——用户是持久创建的，不是每次运行动态分配）。
- 硬编码沙箱指令（同一模板对所有服务生效，无按服务差异化）：`NoNewPrivileges=true`、`PrivateTmp=true`、`ProtectSystem=strict`、`ReadWritePaths=<target_dir> /var/lib/<name>`。
- 重启策略来自 manifest：`restart: on-failure`、`restart_sec: 5`（全部服务一致）；`memory_max` 各服务不同（多数 100–250M，部分未设）。
- **日志**：无 `EnvironmentFile` 外的日志配置，即默认 **journald**（stdout/stderr 走 systemd-journal）。Caddy 侧另有独立 access log（`/var/log/caddy/<name>-access.log`，JSON，50MiB×10 roll，90d 保留）。**没有集中式应用日志聚合**——`logservice` 存在但从 manifest 看是被动接收 API，不是 systemd journal 的自动转发目标（推断，未读 logservice 源码确认）。
- Deployer 自身的 unit **手写**在 `bootstrap/07-deployer-init.sh`（不经模板），`NoNewPrivileges=false`（因为要 shell out 到 sudo），`ReadWritePaths` 故意放宽到 `/etc/systemd/system /etc/caddy /etc /srv /tmp`。
- 敏感值来自 `EnvironmentFile=/etc/<name>/env`（bootstrap 手写，mode 0640 root:`<name>`，只含 `SVC_SECRET` + operator 手加的第三方 key），非敏感值（`SVC_NAME/PORT/REGISTRY_URL/GIT_COMMIT/...`）烘焙进 unit 的 `Environment=` 行，每次部署重渲染。

## 3. 部署流水线（`components/deployer`）

入口：GitHub webhook（HMAC-SHA256 校验，`webhook.py`）→ `orchestrator.handle_push`（`orchestrator.py:113`）。

流程：
1. **仓库/分支白名单**校验，非法直接 `OrchestratorError`。
2. `git_ops.ensure_repo`：拉取/reset 到 `work_dir`；**stale-ref 防护**——旧 webhook 重投递携带落后的 `head_sha` 会被拒绝而非回滚（2026-06-15 曾发生过真实回滚事故，见代码注释）。
3. Monorepo 变更检测（`changes.py`）：`shared/` 或 `components/` 改动 → **fan-out 到每个有 `service.yaml` 的 app/service**；否则只影响触碰到的服务目录。
4. 每服务一个非阻塞文件锁（`/var/lock/deployer-<service>.lock`，`flock` 非阻塞，冲突则整次部署标记 `skipped`，不排队等待）。
5. **单服务流水线**（`_run_pipeline`）：`stage → systemd → caddy → registry`（static kind 只走 `stage → caddy`）。
   - **stage**（`appliers/stage.py`）：`useradd`（若不存在）→ `sudo mkdir -p /srv/<name>` → `sudo chown deployer:deployer` → `rsync -a --delete`（源码进 target_dir，排除 `.venv/__pycache__/...`）→ 以 deployer 身份跑 `install` 命令（通常 `uv sync`）→ `sudo chown <svc>:<svc>` 交还所有权。static kind 走独立"暂存区外构建→一次性 rsync 发布"路径，避免半构建状态被 Caddy 提供服务。
   - **systemd**（`appliers/systemd.py`）：**前置检查** `/etc/<name>/env` 和 `/var/lib/<name>/` 必须已存在（`svc-init.sh` 产物），否则直接失败并给出修复命令，不会静默崩溃循环。渲染 unit → `/tmp` 暂存 → `sudo cp` 到位 → `daemon-reload` → `enable`（幂等）→ `restart`（除非 `manual_restart: true`）。
   - **caddy**（`appliers/caddy.py`，模板 `caddy.snippet.j2`）：path-mount 用 `handle_path` 反代；子域 mount 渲染完整 site block，含安全头（`X-Frame-Options: DENY` 全局，subdomain 服务额外拿完整 CSP，path-mount 只拿 frame/nosniff）、按服务名的 CSP/CORS 覆盖表（`csp_map`/`admin_cors_map`，目前只有 `locationservice`、`pages` 特化）。**未见到本次读到 caddy 是否也走 `sudo cp` + `caddy reload`**——由 sudoers 规则推断是（`06-deployer-sudoers.sh` 有 `cp ... /etc/caddy/services*/*` 和 `caddy reload`）。
   - **registry**（`appliers/registry.py`）：只做 **ACL upsert**（幂等），**不做服务身份创建/`SVC_SECRET` 处理**——那是 `svc-init.sh` 的一次性职责。对 Registry 502/503/504（自身重启期间）做 9 级指数退避重试（总计约 4 分钟），4xx 立即失败（不重试鉴权/配置错误）。
6. 部署结果写入 `deployments` 表（`triggered_by`：`webhook:github` 或 `manual:<user>`），失败会最佳努力经消息总线告警 operator（`_notify_deploy_failure`，best-effort，不遮蔽原始失败）。

**权限模型**（`bootstrap/06-deployer-sudoers.sh`）：`deployer` 用户精确白名单 sudo（无 `sudo bash`）：`systemctl {daemon-reload,enable,disable,restart,start,stop,status,is-active} *`、`useradd -r -s /bin/false *`、`mkdir -p /srv/*`、`chown [-R] *:* /srv/*`、`cp /tmp/deployer-* → /etc/systemd/system/*` 或 `/etc/caddy/services{,-api}/*`、`caddy reload --config /etc/caddy/Caddyfile`、`journalctl -u *`。安装前 `visudo -cf` 校验防止写坏 sudoers。**Deployer 永不触碰 `/etc/<name>/env`**（不读不写，代码注释+架构文档反复强调这是刻意的信任边界）。

**手动重部署路径**（`bootstrap/redeploy.sh`，仅 `registry`/`auth`，用于信任根代码变更）：快照当前 `/srv/<comp>` → 重跑对应 `bootstrap/0X-init.sh`（幂等）→ 健康检查（`registry` 要求 `/health`→200；`auth` 无 `/health`，用"任意 <500 响应"证明进程活着）→ 失败则自动 rsync 回滚并用更宽松的超时（30 次×2s vs 部署阶段 15 次×2s）重新探活 → exit code 语义化（0=成功，1=已回滚，3=回滚也失败需人工）。**这条路径不在 CI/webhook 自动触发范围内**——`docs/architecture.md` 提到有一个 `workflow_run` 挂在 CI 之后的自动化 workflow（`deploy-trust-root.yml`），但即便如此仍是"SSH 上去手动跑脚本"的模型，不是全自动。

## 4. 网络（Caddy 网关）

- `bootstrap/04-caddy-init.sh` 生成 `/etc/caddy/Caddyfile`：一个统一入口 `api.lishuyu.app`（path-based 路由，`import /etc/caddy/services-api/*.caddy`），若干独立子域各自一个 site block（`import /etc/caddy/services/*.caddy`）。**此脚本不在任何自动部署路径上**——改网关级配置（如 CORS 白名单加一个源）需要 SSH 上 droplet 手动重跑或手改文件（CHANGELOG 2026-08-30 两条记录都提到"手动重跑生效"）。
- 端口分配：手工在各 `service.yaml` 的 `mount.port` 里递增分配（9200–9219 目前已用），**无自动分配/冲突检测机制**——纯靠人工记账不撞号（推断：未见任何端口注册表或校验脚本，只见 schema 校验字段类型）。
- 路由约定：`mount.path` → `handle_path` 剥离前缀反代到 `127.0.0.1:<port>`；`mount.subdomain` → 独立子域 site block，无前缀剥离。两者互斥（manifest 语义）。
- 统一安全头：`X-Frame-Options: DENY` 全平台；子域服务额外拿完整 CSP（`default_csp` 常量），path-mount 只拿 frame/nosniff（避免破坏 Swagger 等 CDN 依赖页面）。按服务名硬编码的例外（`csp_map`/`admin_cors_map`）目前仅 2 条（`locationservice` 允许 `unpkg.com`/OSM tile；`pages` 用 `sandbox` CSP 隔离不可信 HTML + 仅 admin 源 CORS）。
- **Registry 角色**：纯目录/发现服务，**不参与请求路由**（`docs/architecture.md` §"关键路径与故障域"明确写"Registry 不在请求关键路径上"）。服务通过 SDK 定期心跳上报（默认 30s 间隔，`SVC_HEARTBEAT_INTERVAL`），Registry 记录 `last_seen`/健康状态供 `discover`/MCP 查询；对推不动心跳的边缘服务（如 CF Worker）新增了 opt-in 的**主动探测**模式（`probe_url` 字段，60s 轮询 GET，2026-08-30 新功能）。
- **Auth 角色**：颁发/验证 JWT（RS256，单私钥），JWKS 公开端点供各服务本地验签（不查 Auth，除非要验 PAT 或走 forward-auth 兜底）。M2M token 由 Registry 签发（`docs/architecture.md`：Registry 调 Auth API 签或共享私钥文件——具体机制未深入读代码，标注为文档转述而非源码核实）。

## 5. 跨服务 SDK 契约（`components/sdk`）

服务通过 `sdk.config.load_from_env()` 从环境变量装配 `SvcConfig`：必需 `SVC_NAME`/`SVC_AUDIENCE`/`SVC_SECRET`，可选 `REGISTRY_URL`/`AUTH_URL`/`SVC_HEARTBEAT_INTERVAL`(默认30s)/`SVC_CAPABILITIES`(逗号分隔)/`SVC_ENDPOINT`(显式覆盖)等。`sdk.fastapi` 提供中间件：优先本地 M2M JWT 验签（同步，无网络），失败则 fallback 到 forward-auth（远程调 `auth.lishuyu.app/api/users/me`），两者都无则匿名。`sdk.decorators`（`@require_admin` 等）经 ContextVar 读取当前请求 principal。`SVC_M2M_PUBLIC_KEY_PATH` 由部署时的 unit 模板固定注入 `/etc/auth/jwt-rs256.pub`（所有服务共享同一公钥文件路径读取）。

日志/健康格式：`registry.health_path` 默认 `/health`（manifest 声明，多数服务用默认值）。**未见到 SDK 强制的结构化日志格式**——这是弱信号,未在 sdk 目录逐文件确认，标记为待查而非结论。

## 6. Secrets 处理（三层模型，`docs/architecture.md` §"服务密钥的三层模型"）

- **Layer 0**（信任根自身密钥）：JWT 私钥、webhook secret、registry admin token 等，bootstrap 脚本手动写入 `/etc/<name>/env`，从不经 Deployer。
- **Layer 1**（业务服务 `SVC_SECRET`）：`bootstrap/svc-init.sh <name> <audience>` 一次性生成并写 `/etc/<name>/env`（0640 root:`<name>`），同时 POST admin-gated `/api/services` 到 Registry 创建身份（防 TOFU 抢注）。**Deployer 全程不碰这个文件**——部署时只 `stat` 验证其存在。第三方 API key（DeepSeek/微信 appsecret/Bark 等）由 operator **手工**追加进同一个 env 文件，Deployer 重部署不会清掉。
- **Layer 2**（运行时 secret 拉取 API）：**明确判定 YAGNI，未实现**（TODO.md 确认："不实现的理由：目前没有任何 Layer 1 服务有这个需求"）。`secretsservice` 存在于仓库但其定位与此 Layer-2 设想的关系未核实，弱信号。

## 7. 备份（`bootstrap/05-db-backup.sh`）

- 每日 04:10 UTC（±15min jitter）systemd timer，`sqlite3 .backup` + gzip + `rclone` 单个 PutObject 上传 R2，14 天保留（`rclone delete --min-age 14d`）。
- **仅覆盖三个信任根 DB**：`registry.db`、`auth.db`、`deployer.db`。**所有 Layer-1 服务的 `/var/lib/<name>/*.db`（占大多数服务的实际业务数据）都不在这个备份范围内**——TODO.md 的"Ops / disaster recovery"条目也明确写"从未做过一次完整的平台级恢复演练"。这是本次勘察中最明确的一个数据风险点（高置信：直接读到脚本里的 `DBS=` 硬编码列表，只有 3 个 db）。
- 明确排除 `registry-runtime.db`（heartbeat 是易失状态，30s 内会自愈，故意不备份）。
- 该脚本是 litestream 的替代品（2026-07-02 因 litestream v0.5 的 L0 retention 轮询导致 R2 API 调用超免费额度而下线，见脚本头注释）。

## 8. 仓库里明确记录的痛点（TODO.md / CHANGELOG.md 原文转述，非我推断）

1. **服务删除生命周期缺失**：Registry 侧的级联删除 API 已就绪，但 Deployer 侧对 webhook `removed` 文件列表的自动 deinit 管线（`systemctl disable` + 清理 unit/caddy/`/srv`/`/etc` + `userdel` + Registry DELETE）**没有实现**——目前下线服务靠手工改 `.disabled` 后缀 + 手动 SSH 清理（`pages`/`iwatchpet` 的下线/复活记录印证了这一手工流程，CHANGELOG 2026-08-30）。
2. **部署后无健康轮询**：`deployments` 表标记为 `running` 后没有 worker 去查 Registry 的 `last_seen` 判定部署是否真正健康，只能靠人看。webhook handler 本身受 GitHub ~10s 超时限制做不了这件事。
3. **网关级配置(`04-caddy-init.sh`)/信任根组件不在自动部署路径上**：CORS 白名单变更、trust-root 代码更新都需要 SSH 到 droplet 手动执行（CHANGELOG 反复出现"手动在 droplet 重跑…生效"）。
4. **端口/服务命名无自动化校验**：新增服务靠人工递增端口号、遵守保留名单（`svc-init.sh` 里硬编码 `root|deployer|registry|auth|daemon|bin|sys|nobody` 黑名单）。
5. **远程控制能力（重启/停止服务）尚未实现**：TODO.md 已经把架构想清楚（应该走 Deployer 而非 SDK，因为 sudo 只在 Deployer），但端点本身未写。
6. **本地 JWKS 验签未做**：Registry/Deployer 对管理员 JWT 目前每次都要往返 Auth 验证（30s 缓存），尚未做本地 JWKS 校验以降延迟/降 Auth 负载。
7. **恢复演练从未做过**：R2 每日快照跑了近 2 个月，但没有一次完整还原演练，TODO.md 明确标记这是"性价比最高的下一步"。
8. **日志查看器缺失**：`GET /api/deployments/{id}/log` 有文档没实现，admin UI 目前看不到单次部署日志详情。

## 9. As-Is 运行时拓扑（ASCII）

```
                              ┌─────────────────────────┐
GitHub push ─────────────────▶  api.lishuyu.app/deploy   │  (Caddy path-mount → 127.0.0.1:8003)
                              │  Deployer (systemd unit)  │  user=deployer, 精确 sudo 白名单
                              └───────────┬───────────────┘
                                          │ stage → systemd → caddy → registry (per-service, flock 串行化)
                    ┌─────────────────────┼──────────────────────────┐
                    ▼                     ▼                          ▼
        sudo mkdir/chown /srv/<n>   render+cp unit → /etc/     render+cp snippet →
        rsync src, uv sync,        systemd/system/<n>.service  /etc/caddy/services[-api]/
        chown svc:svc              daemon-reload; enable;      caddy reload
                                    restart <n>
                    │
                    ▼
        systemd 监管每个 <name>.service（独立 unix 用户，ProtectSystem=strict,
        NoNewPrivileges=true, ReadWritePaths=/srv/<n> /var/lib/<n>, Restart=on-failure）
                    │
                    ▼
        uvicorn(FastAPI) @ 127.0.0.1:<port> ── SQLite @ /var/lib/<n>/*.db
                    │  (SDK: heartbeat every 30s, M2M本地验签优先/回落forward-auth)
                    ▼
        ┌──────────────────────────────────────────────────────────┐
        │            Registry (8002) — 目录/发现/ACL，不路由流量      │
        │            Auth (8001)    — JWT/PAT/OAuth，JWKS 公钥分发   │
        └──────────────────────────────────────────────────────────┘

Caddy (公网 TLS 终止，80/443)
 ├─ api.lishuyu.app          → path 路由到各 services/*、部分 apps/*（services-api/*.caddy）
 ├─ auth.lishuyu.app         → 127.0.0.1:8001
 ├─ registry.lishuyu.app     → 127.0.0.1:8002
 ├─ display/location/mail/pages/turning-test/llm.lishuyu.app / pages.shuyuli.com
 │                            → 各自独立 site block（services/*.caddy，子域挂载）
 └─ /var/log/caddy/*-access.log（JSON，按服务分文件，50MiB×10 roll, 90d）

备份：systemd timer 04:10 UTC daily → sqlite .backup → gzip → rclone → R2
      （仅 registry/auth/deployer 三库；Layer-1 各服务库不在其中）

Cloudflare 侧（droplet 之外）：admin/home/task 三个 SPA 部署在 CF Pages；
mailbox 的 catch-all worker、邮件 AI 回复 worker 也在 CF，经 M2M/ingest token 回调 droplet API。
```

## 10. 未核实 / 弱信号（禁止当结论删除，下一步验证方向）

- Caddy applier（`appliers/caddy.py`）本身内容未读，只从模板+sudoers 推断其行为镜像 systemd applier（`/tmp` 暂存 + `sudo cp` + `caddy reload`）。
- `secretsservice` 的实际调用方/使用场景未核实，与"Layer 2 未实现"的关系是猜测而非读码确认。
- 是否存在应用日志转发到 `logservice` 的机制（journald → logservice pipeline）未核实，只是从 manifest 侧看不到证据，不能断言"不存在"。
- `components/registry/src/registry/worker.py` 的 M2M token 签发细节（"Registry 调 Auth API 签或共享私钥文件"）转述自 `docs/architecture.md`，未读 `worker.py`/`jwt_signer.py` 源码核实具体走的哪条。
