# ams — agent managed services (rootless supervisor + platform runtime)

Linux-only harness that spawns agent-declared services as direct children, each
in its own user namespace + delegated cgroup, reads their stdout/stderr inline
and routes every event through an explicit suppress-or-fix decision interface.
The agent *is* the harness loop; systemd only keeps the harness alive. On top of
that core, `src/ams/platform/` turns ams into the runtime for the `api` monorepo
in one of two modes (never both on one state dir):

- **core mode (1.1.0, current)** — api v3.x is one Cordis-based Node process,
  `core`, that hot-installs its own plugins. ams keeps it alive, releases core
  (stage → install → build → stop → flip `current` → start → gate → flip back),
  ships plugins whose *content* changed through core's control socket, fronts
  it with Caddy, backs it up and escalates. `docs/platform-core.md`.
- **legacy manifest mode (1.0.0, deprecated in 1.1.0)** — api v2.0.0:
  mirror → translate → provision → declare → reload → route → register → gate,
  plus pools and Layer-0 registry/auth. `docs/platform.md`. Kept untouched;
  removal is a 2.0.0 decision.

Read first: `docs/design/DECISIONS.md` (the why, D1–D32 + open items; core
mode is D31, its review fixes D32), `docs/design/PROGRESS.md` (done / next),
`docs/design/PLAN-core.md` (core mode's plan and interfaces),
`docs/platform-core.md` (core mode end to end), `docs/service-declaration.md`.
Legacy: `docs/design/history/PLAN-allin.md`, `docs/design/history/PLAN-pool.md`,
`docs/platform.md`, `docs/platform-sidecars.md`, `docs/manifest-translation.md`,
`docs/platform-pools.md`. `CHANGELOG.md` (zh, Keep a Changelog) per release.

## Layout — core (src/ams, ~8k lines, stdlib only, Python 3.12)
- `schema.py` — `service.toml` → frozen dataclasses (`ServiceDecl`, `RuntimeSpec` kinds none|venv|uv|pnpm|bun|nix, `HealthSpec`, `StopSpec`, `LimitsSpec`, `RestartSpec`, `LoggingSpec`, top-level `depends_on`); `validate`, `loads/load`, `expand_ports`. Reserved env names rejected. `RuntimeSpec.node` exact `X.Y.Z` on kind=pnpm = managed toolchain (`managed_node`); `pnpm` (exact, needs exact node) and `build` (argv) are pnpm-only.
- `events.py` — `LogLine.from_raw` (severity heuristics, 64 KiB cap, `[logging] format` hint), `ServiceStarted/Exited` (with `expected`), `HealthChanged`, `OrphanReaped`. `_NOISE_PATTERNS`: exact, anchored lines that trip a marker word but carry no signal (Node's ``(Use `node --trace-warnings ...` ...)`` hint → INFO).
- `decision.py` — `DecisionPolicy` / `Escalation` protocols, `DefaultPolicy` (WARNING+ escalates; exits follow restart policy; self-probe access lines suppressed), `JsonLinesEscalation` (stdout JSONL).
- `spawn.py` — `Spawner` protocol, `SpawnRequest` (argv/env/port expansion), `PlainSpawner` (no isolation; portable tests, `--no-isolation`).
- `supervisor.py` — single-threaded `selectors` loop: pipe reading, `waitpid(-1)` reaping (subreaper), timers (stop deadline, restart backoff, health, dependency re-check), `start/stop/restart/kill/shutdown/run_forever`, `waiting` status + `_dependency_layers` for reverse-topological shutdown. `_poll_timeout` must only fold in deadlines `_run_timers` acts on (busy-loop invariant). `run_forever(shutdown_timeout_s=)` takes a float or a callable evaluated at shutdown.
- `health.py` — `check_tcp`, `check_http`, `HealthMonitor` (tcp/http/log/none, start period).
- `userns.py` — `fork_in_userns` handshake (unshare → newuidmap/newgidmap by parent → setresuid 1000 → no_new_privs → exec). Two one-shot runners, both a bare fork sharing `_run_in_userns` (one deadline for drain + wait): `run_admin(argv, block, *, env, cwd, mask)` (admin map, inner root; `mask` overmounts harness-private paths empty in a private mount ns — issue #1) and `run_as_service(argv, block, *, env, cwd)` (runtime map, inner 1000, own session, env complete and must carry PATH). `ensure_service_root` (also creates `<root>/data`), prctl wrappers.
- `cgroup.py` — `CgroupRoot.discover` (delegated root, self-move to `harness/`), `ServiceCgroup` (limits, `set_swap_max`, `kill`, `wait_empty`, `remove`, `stats`).
- `isolated.py` — `IsolatedSpawner` (cgroup + userns + held pipes), `make_isolated_spawner(blocks)`.
- `uidmap.py` — `UidBlock` (1024-wide), `parse_subid_file`, `UidAllocator` (persisted JSON, overlap-checked, **re-reads on a cache miss**), `admin_map_args`.
- `ports.py` — `PortAllocator` (persisted, bind-probe conflict detection, ≥1024).
- `state.py` — `StateDir` layout (`services/<id>/{service.toml,root}`, `state/*.json`, `secrets/`, `logs/`), `write_json_atomic`, `read_json_checked`, `StateCorrupt`. `ensure()` treats 0750 as a **floor** so a deliberate `o+x` survives.
- `secrets.py` — write-only `SecretStore` (`<state>/secrets/<id>/NAME`, 0600 harness-owned, values never in argv, never printed), `make_extra_env_for` (the spawn-time injection point), `warn_missing_secrets`.
- `control.py` — `<state>/control.sock` (0600), newline-delimited JSON served from the supervisor's selector loop; `status|reload|start|stop|restart|kill|ping`.
- `runtime.py` — `provision` (legacy: uv/venv/pnpm/bun installs inside the admin ns as inner root with `provisioning_mask`, build step as the service, then chown), `runtime_env`, `provisioning_env`, `RuntimeStore` (`AMS_STORE_DIR`). Node toolchain: `NodeToolchain`, `node_toolchain` (pure paths), `ensure_node_toolchain` (as the harness; `.tar.gz` from nodejs.org, sha256 vs `SHASUMS256.txt`, tarfile `data` filter, atomic rename; pnpm via the managed npm with `ignore-scripts`). `provision_tree(tree, spec, *, block, store, run_build, env)` — pnpm trees only; with a block **every step runs as the service** (`service_provisioning_env`: pnpm store/cache/HOME in `<root>/.cache`), without one a plain subprocess (`clone-or-copy`). `provisioning_mask` / `harness_private_paths` (`<store>/{platform,upstream,repos,src}` + HOME credential dotfiles).
- `hostcheck.py` — `ams check-host`: subuid, setuid helpers, cgroup v2 + delegation, AppArmor profile, traversal of state/store dirs.
- `cli.py` — the entry point; `build_supervisor` is the reusable assembly (loads the runtime layer with or without isolation). stdout = escalation JSONL only, logs on stderr. `SHUTDOWN_BUDGET_S = 45`; `shutdown_grace(asm)` computes the budget at shutdown from the current declarations.

## Layout — platform, core mode (src/ams/platform)
- `core.py` — `CoreConfig` (`<state>/platform/core.toml`; unknown keys rejected; foundation order `secrets, store, gateway, auth, health`; `[[site]]` test-rendered at load; socket path length checked), `CoreLayout` (`R/{releases,current,data,etc,run/control.sock,build}`), `ensure_layout` (mkdir **as the service**, refuses a symlinked entry → `CoreLayoutError`), `flip_current` (link built in harness-owned `<state>/services/core/.current.new`, `mv -T` in), `check_tree_pins` / `node_satisfies` (`engines.node`, `packageManager`), `core_declaration` (fixed ports, workdir `current`, health http `/health` start 60 s, stop SIGTERM 40 s, restart always), `import_bundle` (validate, `--rebase OLD=NEW|@root|@data|@etc`, master copy `<state>/secrets/core/bundle/`, returns names), `bundle_digest`, `place_bundle` (digest marker `bundle.placed`; staged in `<state>/services/core/.etc.stage`, chmod → chown → rename).
- `corectl.py` — `CoreControl` over upstream `node <tree>/scripts/corectl.mjs --socket …` (**never re-implement the protocol**): `status upload deploy restart gc privileges transitions failures ping`. `Runner` protocol, `isolated_runner` (`run_as_service`; SpawnError → rc 127), `plain_runner`. `CoreControlError.code` = core's error code parsed from `corectl: <code>:`; a transition with `outcome != ok` is a result, not an error.
- `coresync.py` — `tick` (the timer's one-shot), `bootstrap`, `ship`, `rollback_release`, `live_status`, `status_view`. `CoreRecord` (`<state>/platform/core.json`, change-gated flush) with `staged_sha release_sha previous_release_sha release_failed_sha stage_failed_sha planned_sha planned_roster ship_retry privilege_restart plugins build_failures escalated`. Plugin outcomes `probation | live | failed | blocked`, each entry carrying `content_key artifact_id sha release bundle` (+ `blocked_on` fingerprint). `ARTIFACT_REFUSALS` / `STATE_REJECTIONS` classify core's refusals. `locked()` = non-blocking flock on `core.lock`. Release gate = "no regression" (`/health` 200 if it was; else every plugin serving before serves again; else gateway answers; else socket answers) and fails fast when the harness reports core `failed`. Everything with a side effect goes through `Hooks`.
- `assets/core_plan.mjs` — package **data**: `node core_plan.mjs <tree> <outDir> <id>…` → one JSON line per plugin with `contentKey` = upstream `computeArtifactId` minus buildInfo. Copied into `R/build/` as the service. Pins a stray async error on its plugin and exits explicitly.
- `gateway.py` — `CoreSite`, `render_core(sites, cfg)` (plain HTTP, host → `reverse_proxy 127.0.0.1:<port>`, one import per site; goldens `tests/golden/gateway/core/`), plus the legacy `render()`; `write()` shared.
- `sources.py` — `SourceMirror`: bare mirror `<store>/repos/<n>.git`, canonical checkout `<store>/src/<n>/<sha>/` (`git archive | tar`), `stage(sha, root, block, *, dest="repo")` (`dest="releases/<sha>"` for core) inside the admin ns, `stage_plain(sha, dest_dir)`, `.ams-sha` marker, `gc(keep)`, `changed_paths()`, `validate_url` (no credentials, no ssh).
- `backup.py` — `discover → snapshot → upload → prune`, plus `restore`; stdlib sqlite backup in the admin ns, rclone via `RCLONE_CONFIG_R2_*` env. Byte stores (`blobs`, `oss-bytes`, `pages-content` under `data/**`, plus core's top-level `data/artifacts`) → `rclone copy --immutable --exclude *.tmp` to `bytes/<id>/<path under data>`, after the snapshots, never pruned (`BackupConfig` refuses overlapping prefixes); `ByteStoreSynced/Failed` records. `Target.label` keeps a pooled member's R2 key byte-identical.
- `policy.py` — `PlatformPolicy` (dedupe by normalized cause, post-sync health gate, Caddy rules, registry-heartbeat suppression), `EscalationDeduper`, `DedupingEscalation`, `cause_key` (also core-sync's dedupe key).
- `static.py` — `publish_static` for `kind: static` (refuses any symlink in the tree, issue #5), and `load_ams_overlay` (the **only** reader of `service.ams.toml`; env/secret names may not be harness-injected names, issue #4).
- `cli.py` — `ams platform core …` plus the legacy verbs.

## Layout — platform, legacy manifest mode (deprecated in 1.1.0)
- `yamlsubset.py` — restricted YAML parser; every unsupported construct raises `YamlSubsetError` naming line + construct (flow depth ≤ 64, numeric literals ≤ 64 chars, `1_000` refused).
- `translate.py` — `translate(manifest, ctx) -> Translation(decl, mount, registry)`; `emit_toml`; loopback gate in `TranslateContext.__post_init__`; `DEPENDS_ON = ("registry",)`. Rejects rather than guesses; parse-time exceptions become `TranslateError`. Pools: `PoolMember`, `PoolTranslation`, `build_pool`, `pool_member`, `mangle_member`, `TranslateContext.pool*` — see `docs/platform-pools.md`.
- `sync.py` — the one-shot tick and `ServiceRecord` / `PlatformState` (`<state>/platform/state.json`). Stages `fetched → translated → provisioned → declared → reloaded → registered → healthy | failed`, monotonic; a tick that changes nothing writes nothing. Pools grouped by the `pool` overlay key; adoption guard. (`_write_if_changed`, `ctl_reload`, `ctl_restart`, `uid_allocator` are reused by core mode.)
- `bootstrap.py` — RS256 keypair once into `<store>/platform/`, Layer-0 secrets into the SecretStore, registry/auth declarations, `place_jwt_key` per service root.
- `layer0.py` — the 17-stage bring-up; `Layer0Error(stage, …)`. (`_caddy_declaration_text` is reused by core bootstrap.)
- `registryclient.py` — `create_identity` (409 = success), `upsert_acl`, `wait_healthy`.
- `rollback.py` — `rollback(state, store, id, *, cfg, to_sha=None)`; refuses a pooled member id; a pool id re-derives from its members' manifests.
- `pool.py` — `plan`/`adopt` (`ams platform pool plan|adopt <pool>`), two-hop staged move; idempotent, refuses while running.
- `assets/pool_runner.py` — package **data** (no `__init__.py` in `assets/`); `src/ams` only `read_bytes()`/`write_bytes()` it; a portable test asserts importing every `ams.*` module never pulls `fastapi`/`uvicorn`.

## CLI surface
`ams validate | run [--no-isolation] [--provision] [--policy default|platform] | provision [id…] | ctl <op> [id] | check-host | secret {set,rm,list,check} | platform core {config import, bootstrap, sync, status [--json] [--offline], release --rollback, ship <id>… [--force]} | platform {sync,status,bootstrap,rollback,pool {plan,adopt}}` (the last group is legacy). Core verbs take `--no-isolation` (plain mode) and `--state-dir/--store-dir/--config/--log-level`.

## Tests (~28k lines)
- `tests/*.py` portable: `.venv/bin/python -m pytest -q`. Some core-mode tests run the real `core_plan.mjs` with a local Node and against `../api` when present.
- `tests/linux/*` marked `linux`, only meaningful via `scripts/remote-test.sh [subdir] [args]` (rsync + pytest as `harness` under `systemd-run -p Delegate=yes`). Use a **distinct remote subdir per parallel agent**. No host exists right now (racknerd torn down).
- **No two test modules may share a basename.** pytest's default `prepend` import mode derives module names from the basename and `tests/` has no `__init__.py`, so a duplicate aborts collection for the whole suite. Convention: `tests/linux/test_<x>_live.py` or `_linux.py`.
- Goldens: `tests/golden/platform/` (21 manifests + `<id>.toml` / `.mount.json` / `.registry.json` — manifests **copied in**, because `api/` is gitignored and not rsynced to the Linux host), `tests/golden/platform/pool/`, `tests/golden/gateway/{path,subdomain,static,tls,logdir}` and `tests/golden/gateway/core/{basic,empty,logdir}`.
- Fixtures: `tests/fixtures/uv-e2e-app`, `tests/fixtures/pool-members`. `tests/conftest.py` skips `linux`-marked tests without a delegated cgroup.
- Core-mode modules (portable): `test_platform_core_config.py`, `test_platform_core_cli.py`, `test_platform_core_integration.py`, `test_platform_core_hardening.py`, `test_platform_core_pins.py`, `test_platform_corectl.py`, `test_platform_coresync.py`, `test_platform_coresync_retry.py`, `test_core_plan_asset.py`, `test_core_plan_isolation.py`, `test_platform_gateway_core.py`, `test_platform_backup_core.py`, `test_platform_sources_dest.py`, `test_runtime_node_toolchain.py`, `test_schema_runtime_pins.py`, `test_userns_run_as_service.py`, `test_provisioning_mask_store.py`; Linux-only: `tests/linux/test_run_as_service_live.py`, `tests/linux/test_run_admin_mask_live.py`.
- Pools modules (portable): `test_schema_portnames.py`, `test_pool_runner_static.py`, `test_platform_pool_translate.py`, `test_platform_overlay_pool.py`, `test_platform_gateway_pool.py`, `test_platform_sync_pool.py`, `test_platform_backup_pool.py`, `test_platform_policy_pool.py`, `test_platform_pool_adopt.py`; Linux-only: `tests/linux/test_pool_runner_live.py`, `tests/linux/test_pool_adopt_live.py`.
- Baseline: **1608 passed / 167 skipped** locally (2026-09-30, `.venv/bin/python -m pytest -q`, ~160 s), core mode + review fixes + e2e fixes landed.
- Local core e2e recipe (plain mode, macOS): scratch clone of `../api` (never the real checkout), `AMS_STATE_DIR`/`AMS_STORE_DIR` under a short `/tmp` path (socket limit), `ams run --no-isolation --policy platform` + `ams platform core bootstrap --no-isolation`. Evidence: `docs/design/history/evidence/core-e2e-local-2026-09-29.txt`.

## Target host (racknerd, Ubuntu 24.04 — torn down since 2026-09; facts for a rebuild)
- Harness user `harness` uid 1000, subuid/subgid `100000:65536`; `/home/harness`
  is 0711 (services must traverse into store/state).
- Interpreter MUST be `/home/harness/venv/bin/python*` (private `venv --copies`);
  AppArmor profile `ams-harness` grants `userns` to that path only
  (`kernel.apparmor_restrict_unprivileged_userns=1`).
- XFS reflink store at `/home/harness/store` (loop file `/var/lib/ams/store.img`):
  `state/` = `AMS_STATE_DIR`, caches `uv-cache/ python/ pnpm-store/ pnpm-home/ bun-cache/`,
  `node/ pnpm/` (managed toolchain), `bin/{caddy,rclone}`, `repos/`, `src/`,
  `upstream/api.git`, `platform/`. Everything reflink-related must stay on this one filesystem.
- Do NOT set `NoNewPrivileges=yes` or an empty `CapabilityBoundingSet=` on any
  unit that stages or provisions (breaks setuid newuidmap).
- Fixed ports: caddy 20180 (both modes); core gateway 18080 (+ pages 18081); legacy Layer-0 registry 20100, auth 20101.
- `api` is a **private** repo the harness holds no credential for: a bare mirror is pushed to `<store>/upstream/api.git`.
- Production of the api core is **phm** (systemd `core.service` + `core-ship`/`core-release`/`core-daily-backup`). Out of scope: never touch phm; the operator copies its `plugins.json`/keys over.

## Rules
- Stdlib only, 3.12-compatible, frozen dataclasses, type hints, `logging` never `print`.
- Never `shell=True` or shell strings; log text is untrusted input at the decision boundary.
- Every non-trivial decision goes to `DECISIONS.md` with rejected alternatives; weak (small-n) results stay in the Open section.
- **`run_admin` / `run_as_service` are a bare `os.fork()`. Nothing may add a thread to a process that forks into a user namespace** — the child gets only the forking thread, so a lock another thread held is held forever (`rm`, `mv`, `uv python install` hung into their own SIGKILL timeouts).
- **Nothing may be forked before `build_supervisor`.** `CgroupRoot.discover` moves self into `<delegated>/harness/` and then enables controllers; cgroup v2 refuses that (EBUSY, "no internal processes") while any process still sits in the root.
- **Provisioning never runs inside the supervisor loop.** `git fetch`, `uv sync`, `pnpm install` and builds block for minutes and the loop is single-threaded; they belong to `ams provision` / `ams platform [core] sync`, separate processes on a timer.
- **Untrusted code never runs with the harness uid mapped.** Package lifecycle scripts, a repo's build and plugin bundles run **as the service** (`run_as_service`), with service-owned caches. Under the admin map the only fix is a mount mask, which the masked process can `umount` or bypass via `/proc/<harness>/root`; legacy provisioning/static builds still rely on it (D32 open).
- **Inner root never writes through a service-controlled name.** The service runs while a tick works and can swap any name in its root for a symlink: prepare in harness-owned `<state>/services/<id>/`, chmod before chown, then `rename` in; or act as the service. Refuse a layout entry that is a symlink.
- **Stop a service before re-staging its tree / flipping `current`.** A legacy `stage()` swaps `<root>/repo` with the venv inside; core imports from the tree `current` resolves to. A process importing during the swap dies with an error that looks like a broken dependency.
- **A unix socket path must stay under 104 bytes (macOS) / 108 (Linux).** Core's is validated at config load; keep `AMS_STATE_DIR` short in tests and e2e runs.
- **Never retry a content key (or a release sha) that failed under the same conditions.** A verdict is keyed by content key + core release + bundle digest (+ core fingerprint for `blocked`); only transport failures (`ship_retry`) are retried. A retry storm is not a fix.
- **Never re-implement core's control protocol or artifact format in Python.** Go through the tree's own `scripts/corectl.mjs` and `scripts/build-artifact.mjs`; `core_plan.mjs`'s content key must stay byte-for-byte upstream `computeArtifactId` minus buildInfo.
- **Core mode and legacy mode never share a state dir.** Both render `<state>/gateway/` and both declare services; `ams-core-sync.service` has `Conflicts=` on the legacy sync units, and takes `core.lock` itself (no `flock(1)` wrapper — it would deadlock).
- A field that must survive a rewrite has to be declared on the record: legacy `PlatformState.load` keeps only the keys `ServiceRecord` declares; `CoreRecord.load` merges over `_empty_record()`, so a new core field needs a default there.
- Production tree on a box is `/home/harness/ams`, only via the deploy script.
- Legacy only: **create the registry identity before starting a translated Layer-1 service**; **a fresh host is `platform-bootstrap.sh --no-layer1`, then the sync timer** (`sync._phase_finish` two-pass, D30); **deploy the new ams before pushing `pool` overlays to the mirror**; **in a pool, a request-time read of `SVC_*` is a bug** (identity env exists only during a member's build/lifespan phases).
