# Changelog

本项目所有显著变更都记录在本文件中。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### Fixed

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
- **security（issue #2）**：一条病态 manifest 不再能击穿整个 sync tick——
  `yamlsubset` 为 flow 嵌套加 64 层深度上限、整型/浮点字面量限长 64 位，
  不再让 `[[[[…` 与 6000 位整数以裸 `RecursionError`/`ValueError` 逃出
  per-manifest 错误处理；`translate()` 把解析期异常包装为 `TranslateError`；
  sync/rollback 的 except 集合补 `RecursionError`/`ValueError` 兜底，
  恢复「一条坏 manifest 只 fail 自己」。
- **security（issue #1）**：不可信构建/置备代码（inner root = harness uid 的读
  权限）不再能看到 harness 私有文件——`run_admin` 新增 `mask` 参数，在子进程
  私有 mount namespace 里把敏感路径盖成空挂载（目录 → 只读空 tmpfs，
  文件/socket → bind `/dev/null`）；static 构建（`_build_mask`）遮蔽
  `<state>` 下除 static 子树外的全部内容，置备工具（`provisioning_mask`）
  遮蔽 SecretStore、兄弟服务树（含 Layer-0 密钥材料）、平台状态与控制 socket。
- **security（issue #4）**：overlay（`service.ams.toml`）的 `[env]`/`secrets`
  名字禁止 harness 注入名（`SVC_*`、`REGISTRY_URL`、`AUTH_URL`、
  `GIT_COMMIT`）——overlay 在 translate 之后合并且无后续闸门，此前可把
  `REGISTRY_URL` 指向外部 URL，让服务把注入的 `SVC_SECRET` 发往第三方。
- **security（issue #5）**：static 发布前整树拒绝符号链接——`cp -a` 保留 repo
  中的 symlink，Caddy `file_server` 会跟随它逃出 docroot；现在发布失败并点名
  违规链接，而不是把逃逸口发布到公网。
