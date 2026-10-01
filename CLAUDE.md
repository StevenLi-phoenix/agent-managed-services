# ams — agent managed services (rootless supervisor + platform runtime)

Linux-only harness that spawns declared services as direct children, each in its
own user namespace + delegated cgroup, reads their stdout/stderr inline and routes
every event through an explicit suppress-or-fix decision interface (deterministic
rules). What rules cannot settle is escalated to stdout and the escalation journal
(`<state>/logs/escalations.jsonl`, `ams escalations`), which an operator agent --
a human-started session, never a component of the loop -- reads and acts on
(`docs/agent-loop.md`). systemd only keeps the harness alive.

On top of that core, `src/ams/platform/` is **core mode** (the only platform mode
since 2.0.0): api v3.x is one Cordis-based Node process, `core`, that hot-installs
its own plugins. ams keeps it alive, releases core (stage → install → build →
stop → flip `current` → start → gate → flip back), ships plugins whose *content*
changed through core's control socket, fronts it with Caddy, backs it up and
escalates. `docs/platform-core.md`. The 1.0.0 manifest mode was removed in 2.0.0
(D35); its notes are in `docs/design/history/`.

Read first: `docs/design/DECISIONS.md` (the why, D1–D37 + open items; core mode
D31/D32; the 2.0.0 review fixes D33–D37), `docs/design/PROGRESS.md` (done / next),
`docs/design/PLAN-core.md`, `docs/platform-core.md`, `docs/service-declaration.md`,
`docs/event-loop.md`, `docs/agent-loop.md`. `CHANGELOG.md` (zh, Keep a Changelog).

## Layout — core (src/ams, ~8.6k lines, stdlib only, Python 3.12; never imports `ams.platform` at module level — `tests/test_core_boundary.py`)
- `schema.py` — `service.toml` → frozen dataclasses (`ServiceDecl`, `RuntimeSpec` kinds none|venv|uv|pnpm|bun|nix, `HealthSpec`, `StopSpec`, `LimitsSpec`, `RestartSpec`, `LoggingSpec`, top-level `depends_on`); `validate`, `loads/load`, `expand_ports`. Reserved env names rejected. `RuntimeSpec.node` exact `X.Y.Z` on kind=pnpm = managed toolchain (`managed_node`); `pnpm` (exact, needs exact node) and `build` (argv) are pnpm-only.
- `events.py` — `LogLine.from_raw` (severity heuristics, 64 KiB cap, `[logging] format` hint), `ServiceStarted/Exited` (with `expected`), `HealthChanged`, `OrphanReaped`. `_NOISE_PATTERNS`: exact, anchored lines that trip a marker word but carry no signal (Node's ``(Use `node --trace-warnings ...` ...)`` hint → INFO).
- `decision.py` — `DecisionPolicy` / `Escalation` protocols, `DefaultPolicy` (WARNING+ escalates; exits follow restart policy; self-probe access lines suppressed), `escalation_record`, `JsonLinesEscalation` (stdout JSONL, plus `journal=`).
- `escalations.py` — `EscalationJournal` (`<state>/logs/escalations.jsonl`, 0600, one `O_APPEND` write per record with `ts`/`source`, one rotated generation `.1`, `append` never raises), `read_records`, `format_record` (strips control chars; records are untrusted), `journal_path`.
- `spawn.py` — `Spawner` protocol, `SpawnRequest` (argv/env/port expansion), `PlainSpawner` (no isolation; portable tests, `--no-isolation`).
- `supervisor.py` — single-threaded `selectors` loop: pipe reading, `waitpid(-1)` reaping (subreaper), timers (stop deadline, restart backoff, health, dependency re-check), `start/stop/restart/kill/shutdown/run_forever`, `waiting` status + `_dependency_layers` for reverse-topological shutdown. `_poll_timeout` must only fold in deadlines `_run_timers` acts on (busy-loop invariant). `run_forever(shutdown_timeout_s=)` takes a float or a callable evaluated at shutdown.
- `health.py` — `Probe` (non-blocking tcp/http state machine: connect → send → status line; `open`, `advance`, `expire`, `close`, `run_blocking`), `HealthMonitor` (`begin` → `Probe` or verdict, `finish(probe)` applies the start period; `check` = blocking convenience), `check_tcp` / `check_http` (one-shot callers only). The supervisor registers a probe's socket in its selector (`_ProbeKey`), folds `probe.deadline` into `_poll_timeout`, and cancels (unregister, then close) on stop/exit/start/replace_decl.
- `userns.py` — `fork_in_userns` handshake (unshare → newuidmap/newgidmap by parent → setresuid 1000 → no_new_privs → exec). Two one-shot runners, both a bare fork sharing `_run_in_userns` (one deadline for drain + wait): `run_admin(argv, block, *, env, cwd, mask)` (admin map, inner root; `mask` overmounts harness-private paths empty in a private mount ns — issue #1) and `run_as_service(argv, block, *, env, cwd)` (runtime map, inner 1000, own session, env complete and must carry PATH). `ensure_service_root` (also creates `<root>/data`), prctl wrappers.
- `cgroup.py` — `CgroupRoot.discover` (delegated root, self-move to `harness/`), `ServiceCgroup` (limits, `set_swap_max`, `kill`, `wait_empty`, `remove`, `stats`).
- `isolated.py` — `IsolatedSpawner` (cgroup + userns + held pipes), `make_isolated_spawner(blocks)`.
- `uidmap.py` — `UidBlock` (1024-wide), `parse_subid_file`, `UidAllocator` (persisted JSON, overlap-checked, **re-reads on a cache miss**), `admin_map_args`.
- `ports.py` — `PortAllocator` (persisted, bind-probe conflict detection, ≥1024).
- `state.py` — `StateDir` layout (`services/<id>/{service.toml,root}`, `state/*.json`, `secrets/`, `logs/`), `write_json_atomic`, `read_json_checked`, `StateCorrupt`. `ensure()` treats 0750 as a **floor** so a deliberate `o+x` survives.
- `secrets.py` — write-only `SecretStore` (`<state>/secrets/<id>/NAME`, 0600 harness-owned, values never in argv, never printed), `make_extra_env_for` (the spawn-time injection point), `warn_missing_secrets`.
- `control.py` — `<state>/control.sock` (0600), newline-delimited JSON served from the supervisor's selector loop; `status|reload|start|stop|restart|kill|ping`.
- `runtime.py` — `provision` (generic `ams provision`: uv/venv/pnpm/bun installs inside the admin ns as inner root with `provisioning_mask`, build step as the service, then chown), `runtime_env`, `provisioning_env`, `RuntimeStore` (`AMS_STORE_DIR`). Node toolchain: `NodeToolchain`, `node_toolchain` (pure paths), `ensure_node_toolchain` (as the harness; `.tar.gz` from nodejs.org, sha256 vs `SHASUMS256.txt`, tarfile `data` filter, atomic rename; pnpm via the managed npm with `ignore-scripts`). `provision_tree(tree, spec, *, block, store, run_build, env)` — pnpm trees only; with a block **every step runs as the service** (`service_provisioning_env`: pnpm store/cache/HOME in `<root>/.cache`), without one a plain subprocess. pnpm's import method is `PNPM_IMPORT_METHOD = "clone-or-copy"` everywhere (XFS reflink optional). `provisioning_mask` / `harness_private_paths` (`<store>/{platform,upstream,repos,src}` + HOME credential dotfiles).
- `hostcheck.py` — `ams check-host`: subuid, setuid helpers, cgroup v2 + delegation, AppArmor profile, traversal of state/store dirs.
- `cli.py` — the entry point; `_platform_cli()` loads `ams.platform.cli` only if importable (no `platform` subcommand otherwise); `cmd_escalations`; `build_supervisor` is the reusable assembly (loads the runtime layer with or without isolation). stdout = escalation JSONL only, logs on stderr. `SHUTDOWN_BUDGET_S = 45`; `shutdown_grace(asm)` computes the budget at shutdown from the current declarations.

## Layout — platform, core mode (src/ams/platform)
- `core.py` — `CoreConfig` (`<state>/platform/core.toml`; unknown keys rejected; foundation order `secrets, store, gateway, auth, health`; `[[site]]` test-rendered at load; socket path length checked), `CoreLayout` (`R/{releases,current,data,etc,run/control.sock,build}`), `ensure_layout` (mkdir **as the service**, refuses a symlinked entry → `CoreLayoutError`), `flip_current` (link built in harness-owned `<state>/services/core/.current.new`, `mv -T` in), `check_tree_pins` / `node_satisfies` (`engines.node`, `packageManager`), `core_declaration` (fixed ports, workdir `current`, health http `/health` start 60 s, stop SIGTERM 40 s, restart always), `import_bundle` (validate, `--rebase OLD=NEW|@root|@data|@etc`, master copy `<state>/secrets/core/bundle/`, returns names), `bundle_digest`, `place_bundle` (digest marker `bundle.placed`; staged in `<state>/services/core/.etc.stage`, chmod → chown → rename).
- `corectl.py` — `CoreControl` over upstream `node <tree>/scripts/corectl.mjs --socket …` (**never re-implement the protocol**): `status upload deploy restart gc privileges transitions failures ping`. `Runner` protocol, `isolated_runner` (`run_as_service`; SpawnError → rc 127), `plain_runner`. `CoreControlError.code` = core's error code parsed from `corectl: <code>:`; a transition with `outcome != ok` is a result, not an error.
- `coresync.py` — `tick` (the timer's one-shot), `bootstrap`, `ship`, `rollback_release`, `live_status`, `status_view`. `CoreRecord` (`<state>/platform/core.json`, change-gated flush) with `staged_sha release_sha previous_release_sha release_failed_sha stage_failed_sha planned_sha planned_roster ship_retry privilege_restart plugins build_failures escalated`. Plugin outcomes `probation | live | failed | blocked`, each entry carrying `content_key artifact_id sha release bundle` (+ `blocked_on` fingerprint). `ARTIFACT_REFUSALS` / `STATE_REJECTIONS` classify core's refusals. `locked()` = non-blocking flock on `core.lock`. Release gate = "no regression" (`/health` 200 if it was; else every plugin serving before serves again; else gateway answers; else socket answers) and fails fast when the harness reports core `failed`. Everything with a side effect goes through `Hooks`.
- `assets/core_plan.mjs` — package **data**: `node core_plan.mjs <tree> <outDir> <id>…` → one JSON line per plugin with `contentKey` = upstream `computeArtifactId` minus buildInfo. Copied into `R/build/` as the service. Pins a stray async error on its plugin and exits explicitly.
- `gateway.py` — `CoreSite`, `render_core(sites, cfg)` (plain HTTP; entry site is the port-wide catch-all `http://:<port>` with `/ams-health` + JSON 404; host → `reverse_proxy 127.0.0.1:<port>`, one import per site; goldens `tests/golden/gateway/core/`), `write()` (sweeps stale snippets), `caddy_declaration(state, store, port)` (fixed port).
- `common.py` — `write_if_changed`, `uid_allocator`, `ctl_reload`, `ctl_restart` (test seams).
- `sources.py` — `SourceMirror`: bare mirror `<store>/repos/<n>.git`, canonical checkout `<store>/src/<n>/<sha>/` (`git archive | tar`), `stage(sha, root, block, *, dest="repo")` (`dest="releases/<sha>"` for core) inside the admin ns, `stage_plain(sha, dest_dir)`, `.ams-sha` marker, `gc(keep)`, `changed_paths()`, `validate_url` (no credentials, no ssh).
- `backup.py` — `discover → snapshot → upload → prune`, plus `restore`; stdlib sqlite backup in the admin ns, rclone via `RCLONE_CONFIG_R2_*` env. Byte stores (`blobs`, `oss-bytes`, `pages-content` under `data/**`, plus core's top-level `data/artifacts`) → `rclone copy --immutable --exclude *.tmp` to `bytes/<id>/<path under data>`, after the snapshots, never pruned (`BackupConfig` refuses overlapping prefixes); `ByteStoreSynced/Failed` records. Failures also go to the escalation journal (`source: backup`).
- `policy.py` — `PlatformPolicy` (dedupe by normalized cause, Caddy rules), `EscalationDeduper`, `cause_key` (also core-sync's dedupe key), `make_policy()`.
- `cli.py` — `ams platform core …` (the only verb group).

## CLI surface
`ams validate | run [--no-isolation] [--provision] [--policy default|platform] | provision [id…] | ctl <op> [id] | check-host | escalations [-n N] [--service ID] [--since ISO] [--json] | secret {set,rm,list,check} | platform core {config import, bootstrap, sync, status [--json] [--offline], release --rollback, ship <id>… [--force]}`. Core verbs take `--no-isolation` (plain mode) and `--state-dir/--store-dir/--config/--log-level`.

## Tests (~16.5k lines)
- `tests/*.py` portable: `.venv/bin/python -m pytest -q`. Some core-mode tests run the real `core_plan.mjs` with a local Node and against `../api` when present.
- `tests/linux/*` marked `linux`, only meaningful as `harness` in a delegated cgroup: on the host `sudo scripts/linux-test.sh [subdir] [pytest args]` (copies to `/home/harness/<subdir>`, `systemd-run -p Delegate=yes`), from a laptop `AMS_HOST=<ssh> scripts/remote-test.sh [subdir] [args]`. Host prep once: `AMS_STORE_FS=plain AMS_TEST_DEPS=1 AMS_INSTALL_UNIT=0 deploy/install-host.sh`. Use a **distinct subdir per parallel agent**. uid blocks come from the harness's real `/etc/subuid` (`tests/linux/linuxhost.py`: `block(i)`, `reflink_capable(dir)`) — never pin `100000`. CI (`.github/workflows/ci.yml`) runs the same two steps on `ubuntu-24.04`.
- **No two test modules may share a basename.** pytest's default `prepend` import mode derives module names from the basename and `tests/` has no `__init__.py`, so a duplicate aborts collection for the whole suite. Convention: `tests/linux/test_<x>_live.py` or `_linux.py`.
- Goldens: `tests/golden/gateway/core/{basic,empty,logdir}` (`AMS_UPDATE_GOLDEN=1` regenerates; read the diff). `tests/conftest.py` skips `linux`-marked tests without a delegated cgroup.
- Boundary and loop: `test_core_boundary.py` (core works with `ams.platform` unimportable), `test_health.py` (silent endpoint never stalls `run_once`; no spin while a probe is in flight), `test_escalations.py`.
- Core-mode modules (portable): `test_platform_core_config.py`, `test_platform_core_cli.py`, `test_platform_core_integration.py`, `test_platform_core_hardening.py`, `test_platform_core_pins.py`, `test_platform_corectl.py`, `test_platform_coresync.py`, `test_platform_coresync_retry.py`, `test_core_plan_asset.py`, `test_core_plan_isolation.py`, `test_platform_gateway_core.py`, `test_platform_backup_core.py`, `test_platform_sources_dest.py`, `test_runtime_node_toolchain.py`, `test_schema_runtime_pins.py`, `test_userns_run_as_service.py`, `test_provisioning_mask_store.py`; Linux-only: `tests/linux/test_run_as_service_live.py`, `tests/linux/test_run_admin_mask_live.py`.
- Baseline (2.0.0, 2026-10-01): macOS **918 passed / 90 skipped** (~40 s, `.venv/bin/python -m pytest -q`); Linux test host, everything as harness: **998 passed / 10 skipped**.
- Local core e2e recipe (plain mode, macOS): scratch clone of `../api` (never the real checkout), `AMS_STATE_DIR`/`AMS_STORE_DIR` under a short `/tmp` path (socket limit), `ams run --no-isolation --policy platform` + `ams platform core bootstrap --no-isolation`. Evidence: `docs/design/history/evidence/core-e2e-local-2026-09-29.txt`. Isolated Linux e2e (7 scenarios): `docs/design/evidence/core-e2e-linux-2026-10-01.txt` — driver: a transient `systemd-run --uid=harness -p Delegate=yes … ams run --policy platform`, every `ams platform core` verb as harness via `sudo -u harness`, api bare mirror rsynced to `<store>/upstream/api.git`.

## Target host (any Ubuntu 24.04 prepared by `deploy/install-host.sh`; racknerd torn down 2026-09)
- Harness user `harness`, subuid/subgid from useradd or the first free 65536 range
  (racknerd: uid 1000, `100000:65536`; the 2.0.0 test host: uid 1001, `165536:65536`);
  `/home/harness` is 0711 (services must traverse into store/state).
- Interpreter MUST be `/home/harness/venv/bin/python*` (private `venv --copies`);
  AppArmor profile `ams-harness` grants `userns` to that path only
  (`kernel.apparmor_restrict_unprivileged_userns=1`).
- Store at `/home/harness/store`: an XFS reflink loop volume (`AMS_STORE_FS=xfs`, default,
  `/var/lib/ams/store.img`) or a plain directory (`AMS_STORE_FS=plain`; copies instead of
  reflinks). `state/` = `AMS_STATE_DIR`, caches `uv-cache/ python/ pnpm-store/ pnpm-home/ bun-cache/`,
  `node/ pnpm/` (managed toolchain), `bin/{caddy,rclone}`, `repos/`, `src/`,
  `upstream/api.git`, `platform/`. For reflink, everything must stay on that one filesystem.
- Do NOT set `NoNewPrivileges=yes` or an empty `CapabilityBoundingSet=` on any
  unit that stages or provisions (breaks setuid newuidmap).
- Fixed ports: caddy 20180; core gateway 18080 (+ pages 18081).
- `api` is a **private** repo the harness holds no credential for: a bare mirror is pushed to `<store>/upstream/api.git`.
- Production of the api core is **phm** (systemd `core.service` + `core-ship`/`core-release`/`core-daily-backup`). Out of scope: never touch phm; the operator copies its `plugins.json`/keys over.

## Rules
- Stdlib only, 3.12-compatible, frozen dataclasses, type hints, `logging` never `print`.
- Never `shell=True` or shell strings; log text is untrusted input at the decision boundary.
- Every non-trivial decision goes to `DECISIONS.md` with rejected alternatives; weak (small-n) results stay in the Open section.
- **`run_admin` / `run_as_service` are a bare `os.fork()`. Nothing may add a thread to a process that forks into a user namespace** — the child gets only the forking thread, so a lock another thread held is held forever (`rm`, `mv`, `uv python install` hung into their own SIGKILL timeouts).
- **Nothing may be forked before `build_supervisor`.** `CgroupRoot.discover` moves self into `<delegated>/harness/` and then enables controllers; cgroup v2 refuses that (EBUSY, "no internal processes") while any process still sits in the root.
- **Provisioning never runs inside the supervisor loop.** `git fetch`, `uv sync`, `pnpm install` and builds block for minutes and the loop is single-threaded; they belong to `ams provision` / `ams platform [core] sync`, separate processes on a timer.
- **Untrusted code never runs with the harness uid mapped.** Package lifecycle scripts, a repo's build and plugin bundles run **as the service** (`run_as_service`), with service-owned caches. Under the admin map the only fix is a mount mask, which the masked process can `umount` or bypass via `/proc/<harness>/root`; generic `ams provision` still relies on it (D32 open).
- **Inner root never writes through a service-controlled name.** The service runs while a tick works and can swap any name in its root for a symlink: prepare in harness-owned `<state>/services/<id>/`, chmod before chown, then `rename` in; or act as the service. Refuse a layout entry that is a symlink.
- **Stop a service before re-staging its tree / flipping `current`.** Core imports from the tree `current` resolves to. A process importing during the swap dies with an error that looks like a broken dependency.
- **A unix socket path must stay under 104 bytes (macOS) / 108 (Linux).** Core's is validated at config load; keep `AMS_STATE_DIR` short in tests and e2e runs.
- **Never retry a content key (or a release sha) that failed under the same conditions.** A verdict is keyed by content key + core release + bundle digest (+ core fingerprint for `blocked`); only transport failures (`ship_retry`) are retried. A retry storm is not a fix.
- **Never re-implement core's control protocol or artifact format in Python.** Go through the tree's own `scripts/corectl.mjs` and `scripts/build-artifact.mjs`; `core_plan.mjs`'s content key must stay byte-for-byte upstream `computeArtifactId` minus buildInfo.
- **`ams-core-sync.service` takes `core.lock` itself** (no `flock(1)` wrapper — it would deadlock).
- **Health probes never block.** A tcp/http check is a `Probe` in the selector; nothing in the loop may wait on a socket, a subprocess or a thread (`docs/event-loop.md`).
- **Escalations are untrusted text.** Print them through `format_record`; never feed them to anything that executes or interprets them.
- A field that must survive a rewrite has to be declared on the record: `CoreRecord.load` merges over `_empty_record()`, so a new core field needs a default there.
- Production tree on a box is `/home/harness/ams`, only via `AMS_HOST=<host> scripts/deploy.sh`.
