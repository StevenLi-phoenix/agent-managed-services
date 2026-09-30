# AGENTS.md — ams (agent managed services)

Rootless Linux supervisor (`src/ams`, ~8k lines) + platform runtime on top of it
(`src/ams/platform`, ~17k lines). Spawns agent-declared services as direct children,
each in its own user namespace + delegated cgroup v2, and routes every log event
through an explicit suppress-or-fix decision interface. The agent *is* the harness
loop; systemd only keeps the harness alive. **Stdlib only, Python 3.12, no runtime
dependencies.**

The platform layer has two modes, never both on one state dir:
- **core mode (1.1.0, current)** — hosts the Cordis-based api core (one Node process
  with plugins): keep alive, release core by flipping `current`, ship plugins whose
  content changed via core's own `corectl`, Caddy front, R2 backup, escalations.
- **legacy manifest mode (1.0.0, deprecated)** — api v2.0.0 `service.yaml`
  translation, pools, Layer-0 registry/auth. Untouched; removal is a 2.0.0 decision.

`CLAUDE.md` is the fuller companion file (per-module map); keep the two in sync.
Design decisions + rejected alternatives live in `.claude/state/DECISIONS.md`
(D1–D32 + open items; core mode is D31/D32); done/next in `.claude/state/PROGRESS.md`;
releases in `CHANGELOG.md`.

## Read first (sensitive areas)
- `.claude/state/DECISIONS.md` — the *why* behind every invariant below.
- `docs/platform-core.md` — core mode end to end (layout, `core.toml`, the tick, release gate, content keys, security model, open items).
- `.claude/state/PLAN-core.md` — core mode's interfaces.
- `docs/service-declaration.md` — the `service.toml` schema (incl. `runtime.node/pnpm/build`).
- Legacy: `docs/platform.md`, `docs/platform-pools.md`, `docs/manifest-translation.md`, `docs/platform-sidecars.md`.

## Layout
- `src/ams/` — core: `schema.py` (service.toml → frozen dataclasses), `supervisor.py` (single-threaded `selectors` loop), `events.py`/`decision.py` (suppress-or-fix), `userns.py` (`run_admin` with mount mask, `run_as_service`)/`cgroup.py`/`isolated.py` (isolation), `uidmap.py`/`ports.py`/`state.py`/`secrets.py`, `control.py` (0600 unix socket), `runtime.py` (provisioning, managed Node toolchain, `provision_tree`), `cli.py`.
- `src/ams/platform/` core mode — `core.py` (config, layout, bundle, declaration, `flip_current`, tree pins), `coresync.py` (the tick, release/rollback/ship, `core.json`), `corectl.py` (upstream `scripts/corectl.mjs` wrapper), `gateway.py` (`render_core`), `sources.py` (mirror + staging), `backup.py` (sqlite + immutable byte stores), `policy.py`, `static.py`, `cli.py`.
- `src/ams/platform/` legacy — `yamlsubset.py`, `translate.py`, `sync.py`, `bootstrap.py`/`layer0.py`, `registryclient.py`, `rollback.py`, `pool.py`.
- `src/ams/platform/assets/` — **package data**, no `__init__.py`: `core_plan.mjs` (content-key planner, run with the tree's node as the service) and `pool_runner.py` (legacy pools). `src/ams` never imports them; a portable test asserts importing `ams.*` never pulls `fastapi`/`uvicorn` into `sys.modules`.
- `tests/` (portable + `tests/linux/`), `tests/golden/` (incl. `gateway/core/`), `deploy/` (systemd units incl. `ams-core-sync.{service,timer}`, AppArmor profile, `install-host.sh`), `scripts/`, `examples/platform/`.

## Commands
```bash
# Dev env (uv-managed):
uv venv --python 3.12 .venv && source .venv/bin/activate && uv pip install pytest pytest-timeout ruff
.venv/bin/python -m pytest -q        # portable tests (baseline: 1608 passed / 167 skipped, 2026-09-30)
.venv/bin/python -m ruff check .     # line-length 100, target py312, select E,F,I,UP,B
scripts/remote-test.sh [subdir]      # linux-marked tests, on the target host via rsync + systemd-run
```
- The product and the linux-marked tests are Linux-only; a workstation runs the portable suite and core mode in plain mode (`ams run --no-isolation`, `ams platform core … --no-isolation`). No Linux host exists at the moment (racknerd torn down); the isolation path of core mode has never run live. Use a **distinct remote subdir per parallel agent** once one exists.
- **No two test modules may share a basename** (pytest `prepend` import mode, no `__init__.py` in `tests/` — a duplicate aborts the whole suite). Convention: `tests/linux/test_<x>_live.py` or `_linux.py`.
- Goldens in `tests/golden/platform/` have manifests **copied in** (the real `api/` repo is gitignored and never rsynced to the host). Never modify `../api`; e2e runs use a scratch clone.

## Hard rules (each has caused real breakage or a review finding)
- Frozen dataclasses, type hints everywhere; `logging`, never `print`. Never `shell=True` or shell strings; log text is untrusted input at the decision boundary.
- **`run_admin` / `run_as_service` are a bare `os.fork()`. Nothing may add a thread to a process that forks into a user namespace** (the child gets only the forking thread → a lock held by another thread hangs forever).
- **Nothing may be forked before `build_supervisor`** — `CgroupRoot.discover` moves self into `harness/`, and cgroup v2 refuses controller enable (EBUSY) while any process sits in the root.
- **Provisioning never runs inside the supervisor loop** (`git fetch`/`uv sync`/`pnpm install`/builds block for minutes); it belongs to `ams provision` / `ams platform [core] sync` processes on a timer.
- **Untrusted code runs as the service, never with the harness uid mapped** (a mount mask under the admin map can be `umount`ed). **Inner root never writes through a service-controlled name**: stage in harness-owned `<state>/services/<id>/`, chmod before chown, `rename` in.
- **Stop a service before re-staging its tree or flipping `current`** (the process imports from it).
- **Unix socket paths stay under 104 bytes (macOS) / 108 (Linux)** — validated at core config load; keep `AMS_STATE_DIR` short.
- **Never retry a failed content key or release sha under unchanged conditions** (core release, bundle digest, core fingerprint for `blocked`); only transport failures retry.
- **Never re-implement core's control protocol or artifact format**; go through the tree's `scripts/corectl.mjs` / `build-artifact.mjs`.
- **Core mode and legacy mode never share a state dir** (`Conflicts=` in `ams-core-sync.service`; the tick takes `core.lock` itself, no `flock(1)` wrapper).
- `supervisor._poll_timeout` must only fold in deadlines `_run_timers` acts on (busy-loop invariant).
- A field that must survive a rewrite must be declared: legacy `PlatformState.load` keeps only `ServiceRecord`'s keys; a new `core.json` field needs a default in `coresync._empty_record()`.
- Legacy: create the registry identity **before** starting a translated Layer-1 service; pools: **a request-time read of `SVC_*` is a bug**; `sync` registers every identity before opening any health gate (two-pass — keep it).

## Host & deploy (racknerd, Ubuntu 24.04 — torn down; facts for a rebuild)
- Harness user `harness` (uid 1000, subuid `100000:65536`); interpreter MUST be `/home/harness/venv/bin/python*` (AppArmor grants `userns` to that path only). XFS reflink store at `/home/harness/store` — everything reflink-related stays on that one filesystem.
- Do **not** set `NoNewPrivileges=yes` or an empty `CapabilityBoundingSet=` on units that stage/provision (breaks setuid `newuidmap`).
- Production tree `/home/harness/ams` is written **only** via `scripts/deploy-racknerd.sh`.
- Core mode: `ams platform core config import` → `ams platform core bootstrap` → `ams-core-sync.timer`. Harness `TimeoutStopSec=50` ≥ `SHUTDOWN_BUDGET_S` 45 (core drains 40 s). Production of the api core is phm: **never touch phm.**
- Legacy: fresh host is `scripts/platform-bootstrap.sh --no-layer1`, then the sync timer; deploy new ams before pushing `pool` overlays. Fixed ports: caddy 20180, registry 20100, auth 20101 (core gateway 18080).
- Commits follow conventional-commit style (`feat(platform):`, `fix:`, `docs(state):`).
