# ams — agent managed services (rootless supervisor + platform runtime)

Linux-only harness that spawns agent-declared services as direct children, each
in its own user namespace + delegated cgroup, reads their stdout/stderr inline
and routes every event through an explicit suppress-or-fix decision interface.
The agent *is* the harness loop; systemd only keeps the harness alive. On top of
that core, `src/ams/platform/` turns ams into the runtime for the `api` monorepo:
mirror → translate → provision → declare → reload → route → register → gate.

Read first: `.claude/state/DECISIONS.md` (the why, D1–D29 + open items),
`.claude/state/PROGRESS.md` (what is done / next), `.claude/state/PLAN-allin.md`
(the four waves), `.claude/state/PLAN-pool.md` (the pools feature),
`docs/platform.md` (the end-to-end story), `docs/service-declaration.md`,
`docs/platform-sidecars.md`, `docs/manifest-translation.md`,
`docs/platform-pools.md` (N services sharing one process).

## Layout — core (src/ams, ~7k lines, stdlib only, Python 3.12)
- `schema.py` — `service.toml` → frozen dataclasses (`ServiceDecl`, `RuntimeSpec` kinds none|venv|uv|pnpm|bun|nix, `HealthSpec`, `StopSpec`, `LimitsSpec`, `RestartSpec`, `LoggingSpec`, top-level `depends_on`); `validate`, `loads/load`, `expand_ports`. Reserved env names rejected.
- `events.py` — `LogLine.from_raw` (severity heuristics, 64 KiB cap, `[logging] format` hint), `ServiceStarted/Exited` (with `expected`), `HealthChanged`, `OrphanReaped`.
- `decision.py` — `DecisionPolicy` / `Escalation` protocols, `DefaultPolicy` (WARNING+ escalates; exits follow restart policy; self-probe access lines suppressed), `JsonLinesEscalation` (stdout JSONL).
- `spawn.py` — `Spawner` protocol, `SpawnRequest` (argv/env/port expansion), `PlainSpawner` (no isolation; portable tests, `--no-isolation`).
- `supervisor.py` — single-threaded `selectors` loop: pipe reading, `waitpid(-1)` reaping (subreaper), timers (stop deadline, restart backoff, health, dependency re-check), `start/stop/restart/kill/shutdown/run_forever`, `waiting` status + `_dependency_layers` for reverse-topological shutdown. `_poll_timeout` must only fold in deadlines `_run_timers` acts on (busy-loop invariant).
- `health.py` — `check_tcp`, `check_http`, `HealthMonitor` (tcp/http/log/none, start period).
- `userns.py` — `fork_in_userns` handshake (unshare → newuidmap/newgidmap by parent → setresuid 1000 → no_new_privs → exec), `run_admin` (inner-root admin map for chown/rm), `ensure_service_root` (also creates `<root>/data`), prctl wrappers.
- `cgroup.py` — `CgroupRoot.discover` (delegated root, self-move to `harness/`), `ServiceCgroup` (limits, `set_swap_max`, `kill`, `wait_empty`, `remove`, `stats`).
- `isolated.py` — `IsolatedSpawner` (cgroup + userns + held pipes), `make_isolated_spawner(blocks)`.
- `uidmap.py` — `UidBlock` (1024-wide), `parse_subid_file`, `UidAllocator` (persisted JSON, overlap-checked, **re-reads on a cache miss**), `admin_map_args`.
- `ports.py` — `PortAllocator` (persisted, bind-probe conflict detection, ≥1024).
- `state.py` — `StateDir` layout (`services/<id>/{service.toml,root}`, `state/*.json`, `secrets/`, `logs/`), `write_json_atomic`, `read_json_checked`, `StateCorrupt`. `ensure()` treats 0750 as a **floor** so a deliberate `o+x` survives.
- `secrets.py` — write-only `SecretStore` (`<state>/secrets/<id>/NAME`, 0600 harness-owned, values never in argv, never printed), `make_extra_env_for` (the spawn-time injection point), `warn_missing_secrets`.
- `control.py` — `<state>/control.sock` (0600), newline-delimited JSON served from the supervisor's selector loop; `status|reload|start|stop|restart|kill|ping`.
- `runtime.py` — `provision` (uv/venv/pnpm/bun inside the admin ns as inner root, then chown to the service), `runtime_env`, `provisioning_env`, `RuntimeStore` (`AMS_STORE_DIR`).
- `hostcheck.py` — `ams check-host`: subuid, setuid helpers, cgroup v2 + delegation, AppArmor profile, traversal of state/store dirs.
- `cli.py` — the entry point; `build_supervisor` is the reusable assembly. stdout = escalation JSONL only, logs on stderr.

## Layout — platform (src/ams/platform, ~9k lines)
- `sources.py` — `SourceMirror`: bare mirror `<store>/repos/<n>.git`, canonical checkout `<store>/src/<n>/<sha>/` (`git archive | tar`), `stage()` = `cp -a --reflink=auto` into `<root>/repo` inside the admin ns, `.ams-sha` marker, `gc(keep)`, `changed_paths()`, `validate_url` (no credentials, no ssh).
- `yamlsubset.py` — restricted YAML parser; every unsupported construct raises `YamlSubsetError` naming line + construct.
- `translate.py` — `translate(manifest, ctx) -> Translation(decl, mount, registry)`; `emit_toml`; loopback gate in `TranslateContext.__post_init__`; `DEPENDS_ON = ("registry",)`. Rejects rather than guesses. Pools: `PoolMember`, `PoolTranslation`, `build_pool(pool, members, ctx)`, `pool_member(translation, manifest_dir_rel)`, `mangle_member(id)`, `TranslateContext.pool*` fields — see `docs/platform-pools.md`.
- `sync.py` — the one-shot tick and `ServiceRecord` / `PlatformState` (`<state>/platform/state.json`). Stages `fetched → translated → provisioned → declared → reloaded → registered → healthy | failed`, monotonic; a tick that changes nothing writes nothing. Pools: groups manifests sharing a `pool` overlay key into one `_Pending`/`ServiceRecord` (`pool`/`pool_members` fields), stages+provisions the pool once, and refuses to declare a pool with un-adopted legacy member data (the adoption guard, naming `ams platform pool adopt`).
- `cli.py` — `ams platform sync|status|bootstrap|rollback|pool`.
- `bootstrap.py` — RS256 keypair once into `<store>/platform/`, Layer-0 secrets into the SecretStore, registry/auth declarations, `place_jwt_key` per service root.
- `layer0.py` — the 17-stage bring-up; `Layer0Error(stage, …)` carries the partial report.
- `gateway.py` — renders `<state>/gateway/{Caddyfile,sites/<id>.caddy}` from the mount sidecars + live ports. Output is `caddy fmt`-canonical. `resolve_ports` follows a mount's optional `port_owner` (a pool id) so a pooled member's port comes off the pool's allocation.
- `static.py` — `publish_static` for `kind: static`, and `load_ams_overlay` (the **only** reader of `service.ams.toml`); `Overlay.pool` / `overlay_pool` for the pool grouping key.
- `registryclient.py` — `create_identity` (409 = success), `upsert_acl`, `wait_healthy`; stdlib urllib, deployer's retry backoff.
- `policy.py` — `PlatformPolicy` (dedupe by normalized cause, post-sync health gate, Caddy rules, registry-heartbeat suppression), `EscalationDeduper`, `DedupingEscalation`. `_pool_suffix` appends `" (pool of N: a, b, c)"` to a pooled crash-loop escalation.
- `backup.py` — `discover → snapshot → upload → prune`, plus `restore`; stdlib sqlite backup in the admin ns, rclone via `RCLONE_CONFIG_R2_*` env. `Target.label` (pool member id when found under `<root>/data/<member>/`) keeps a pooled member's R2 key byte-identical to its pre-pool key.
- `rollback.py` — `rollback(state, store, id, *, cfg, to_sha=None)`; stop → stage → declare → reload → restart → health gate. Refuses a pooled member id, naming the pool; rolling back a pool id re-derives it from its members' manifests at the target sha and gates per member.
- `pool.py` — `plan`/`adopt` (`ams platform pool plan|adopt <pool>`): moves a member's `data/`, secrets and stale declaration into its pool root via a two-hop staged move (member uid block → harness-owned staging dir → pool uid block, since one admin fork maps only one uid block). Idempotent, refuses while the pool is running, never deletes a legacy root. **`src/ams` never imports this pool's runner asset** (see `assets/` below) — `pool.py` itself is ordinary `ams` code; only `pool_runner.py` is fastapi/uvicorn-touching data.
- `assets/pool_runner.py` — package **data**, no `__init__.py` in `assets/`: the N-services-in-one-process runner a pool's own venv python executes. `src/ams` only ever `read_bytes()`/`write_bytes()` it; a portable test asserts importing every `ams.*` module never pulls `fastapi`/`uvicorn` into `sys.modules`.

## CLI surface
`ams validate | run [--no-isolation] [--provision] [--policy default|platform] | provision [id…] | ctl <op> [id] | check-host | secret {set,rm,list,check} | platform {sync,status,bootstrap,rollback,pool {plan,adopt}}`.

## Tests (~16k lines)
- `tests/*.py` portable: `.venv/bin/python -m pytest -q`.
- `tests/linux/*` marked `linux`, only meaningful via `scripts/remote-test.sh [subdir] [args]` (rsync + pytest as `harness` under `systemd-run -p Delegate=yes`). Use a **distinct remote subdir per parallel agent**.
- **No two test modules may share a basename.** pytest's default `prepend` import mode derives module names from the basename and `tests/` has no `__init__.py`, so a duplicate aborts collection for the whole suite. Convention: `tests/linux/test_<x>_live.py` or `_linux.py` (`test_platform_sources_linux.py`, `test_platform_static_live.py`, `test_userns_portable.py`).
- Goldens: `tests/golden/platform/` (21 manifests + `<id>.toml` / `.mount.json` / `.registry.json` per service — the manifests are **copied in**, because `api/` is gitignored and not rsynced to the Linux host) and `tests/golden/gateway/{path,subdomain,static,tls,logdir}`. Pools: `tests/golden/platform/pool/` (a 2-member `kvservice`+`timeservice` pool: `pool-core.toml`, `pool-core.pool.json`, the two members' `.yaml` + `.mount.json`).
- Fixtures: `tests/fixtures/uv-e2e-app`, `tests/fixtures/pool-members` (a fixture pool venv/app pair for the runner's Linux test). `tests/conftest.py` skips `linux`-marked tests without a delegated cgroup.
- Pools test modules (portable): `test_schema_portnames.py`, `test_pool_runner_static.py`, `test_platform_pool_translate.py`, `test_platform_overlay_pool.py`, `test_platform_gateway_pool.py`, `test_platform_sync_pool.py`, `test_platform_backup_pool.py`, `test_platform_policy_pool.py`, `test_platform_pool_adopt.py`; Linux-only: `tests/linux/test_pool_runner_live.py`, `tests/linux/test_pool_adopt_live.py`.
- Baseline: **1193 passed / 122 skipped** locally (2026-09-03, `.venv/bin/python -m pytest -q`), pools T1–T8 landed.

## Target host (racknerd, Ubuntu 24.04)
- Harness user `harness` uid 1000, subuid/subgid `100000:65536`; `/home/harness`
  is 0711 (services must traverse into store/state).
- Interpreter MUST be `/home/harness/venv/bin/python*` (private `venv --copies`);
  AppArmor profile `ams-harness` grants `userns` to that path only
  (`kernel.apparmor_restrict_unprivileged_userns=1`).
- XFS reflink store at `/home/harness/store` (loop file `/var/lib/ams/store.img`):
  `state/` = `AMS_STATE_DIR`, caches `uv-cache/ python/ pnpm-store/ pnpm-home/ bun-cache/`,
  `bin/{caddy,rclone}`, `repos/`, `src/`, `upstream/api.git`, `platform/`.
  Everything reflink-related must stay on this one filesystem.
- Tools for harness: uv `~/.local/bin/uv`, pnpm `store/pnpm-home/bin`, bun `~/.bun/bin`, system node 22.
- Do NOT set `NoNewPrivileges=yes` or an empty `CapabilityBoundingSet=` on any
  unit that stages or provisions (breaks setuid newuidmap).
- Layer-0 ports are fixed: registry 20100, auth 20101, caddy 20180.
- `api` is a **private** repo the harness holds no credential for. Phase A fetches
  a bare mirror pushed to `<store>/upstream/api.git`.
- A pre-existing Docker container runs as uid 1000 on this box (not ours; leave it).

## Rules
- Stdlib only, 3.12-compatible, frozen dataclasses, type hints, `logging` never `print`.
- Never `shell=True` or shell strings; log text is untrusted input at the decision boundary.
- Every non-trivial decision goes to `DECISIONS.md` with rejected alternatives; weak (small-n) results stay in the Open section.
- **`run_admin` is a bare `os.fork()`. Nothing may add a thread to a process that forks into a user namespace** — the child gets only the forking thread, so a lock another thread held is held forever (`rm`, `mv`, `uv python install` hung into their own SIGKILL timeouts).
- **Nothing may be forked before `build_supervisor`.** `CgroupRoot.discover` moves self into `<delegated>/harness/` and then enables controllers; cgroup v2 refuses that (EBUSY, "no internal processes") while any process still sits in the root.
- **Provisioning never runs inside the supervisor loop.** `git fetch` and `uv sync` block for minutes and the loop is single-threaded; they belong to `ams provision` / `ams platform sync`, separate processes on a timer.
- **Stop a service before re-staging its tree.** `stage()` swaps `<root>/repo` and the venv lives inside it; a process importing during the swap dies with `ModuleNotFoundError` that looks like a broken dependency.
- **Create the registry identity before starting a translated Layer-1 service.** No `SVC_DEV` means the SDK registers inside the FastAPI lifespan, and a 404 there is a uvicorn startup failure.
- A field that must survive a sync rewrite has to be declared on `ServiceRecord`: `PlatformState.load` keeps only the keys the dataclass declares.
- Production tree on the box is `/home/harness/ams`, only via the deploy script.
- **Deploy the new ams before pushing `pool` overlays to the mirror.** An un-upgraded host's overlay reader rejects the unknown `pool` key, and the 60 s sync timer marks every affected member `failed` on its next tick (`docs/platform-pools.md`).
- **In a pool, identity env (`SVC_NAME`, `SVC_SECRET`, `SVC_ENDPOINT`, …) is only present during a member's build/lifespan phases; a request-time read of `SVC_*` is a bug.** The pool runner swaps identity keys in and back out around build, lifespan startup and lifespan shutdown only — outside those phases the process env holds the non-identity union, not any one member's identity. `SVC_ENDPOINT` was the one known request-time read (`sdk/ui.py login_redirect`, `mailbox/main.py _sso_redirect`); T11 fixed both by deriving the return-to from the request instead.
