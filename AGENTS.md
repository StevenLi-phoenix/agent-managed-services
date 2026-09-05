# AGENTS.md — ams (agent managed services)

Rootless Linux supervisor (`src/ams`, ~7k lines) + platform runtime on top of it
(`src/ams/platform`, ~9k lines). Spawns agent-declared services as direct children,
each in its own user namespace + delegated cgroup v2, and routes every log event
through an explicit suppress-or-fix decision interface. The agent *is* the harness
loop; systemd only keeps the harness alive. **Stdlib only, Python 3.12, no runtime
dependencies.**

`CLAUDE.md` is the fuller companion file (per-module map); keep the two in sync.
Design decisions + rejected alternatives live in `.claude/state/DECISIONS.md`
(D1–D29 + open items); done/next in `.claude/state/PROGRESS.md`.

## Read first (sensitive areas)
- `.claude/state/DECISIONS.md` — the *why* behind every invariant below.
- `docs/platform.md` — the end-to-end platform story (mirror → translate → provision → declare → reload → route → register → gate).
- `docs/platform-pools.md` — N services sharing one process; read before touching pools.
- `docs/service-declaration.md`, `docs/manifest-translation.md`, `docs/platform-sidecars.md`.

## Layout
- `src/ams/` — core: `schema.py` (service.toml → frozen dataclasses), `supervisor.py` (single-threaded `selectors` loop), `events.py`/`decision.py` (suppress-or-fix), `userns.py`/`cgroup.py`/`isolated.py` (isolation), `uidmap.py`/`ports.py`/`state.py`/`secrets.py`, `control.py` (0600 unix socket), `runtime.py`, `cli.py`.
- `src/ams/platform/` — `sources.py` (git mirror + reflink staging), `yamlsubset.py` (restricted YAML), `translate.py` (manifest → declaration), `sync.py` (one-shot tick, staged `ServiceRecord`), `bootstrap.py`/`layer0.py`, `gateway.py` (Caddyfile renderer), `static.py`, `registryclient.py`, `policy.py`, `backup.py`, `rollback.py`, `pool.py`.
- `src/ams/platform/assets/pool_runner.py` — **package data**, no `__init__.py` in `assets/`; executed by a pool's own venv. `src/ams` only ever `read_bytes()`es it; a portable test asserts importing `ams.*` never pulls `fastapi`/`uvicorn` into `sys.modules`.
- `tests/` (portable + `tests/linux/`), `tests/golden/`, `deploy/` (systemd units, AppArmor profile, `install-host.sh`), `scripts/`, `examples/platform/`.

## Commands
```bash
# Dev env (uv-managed):
uv venv --python 3.12 .venv && source .venv/bin/activate && uv pip install pytest pytest-timeout ruff
.venv/bin/python -m pytest -q        # portable tests (baseline: 1231 passed / 125 skipped, 2026-09-03)
.venv/bin/python -m ruff check .     # line-length 100, target py312, select E,F,I,UP,B
scripts/remote-test.sh [subdir]      # linux-marked tests, on the target host via rsync + systemd-run
```
- This workstation is Windows; the product and the linux-marked tests are Linux-only. Use a **distinct remote subdir per parallel agent**.
- **No two test modules may share a basename** (pytest `prepend` import mode, no `__init__.py` in `tests/` — a duplicate aborts the whole suite). Convention: `tests/linux/test_<x>_live.py` or `_linux.py`.
- Goldens in `tests/golden/platform/` have manifests **copied in** (the real `api/` repo is gitignored and never rsynced to the host).

## Hard rules (each has caused real breakage)
- Frozen dataclasses, type hints everywhere; `logging`, never `print`. Never `shell=True` or shell strings; log text is untrusted input at the decision boundary.
- **`run_admin` is a bare `os.fork()`. Nothing may add a thread to a process that forks into a user namespace** (the child gets only the forking thread → a lock held by another thread hangs forever).
- **Nothing may be forked before `build_supervisor`** — `CgroupRoot.discover` moves self into `harness/`, and cgroup v2 refuses controller enable (EBUSY) while any process sits in the root.
- **Provisioning never runs inside the supervisor loop** (`git fetch`/`uv sync` block for minutes); it belongs to `ams provision` / `ams platform sync` processes on a timer.
- **Stop a service before re-staging its tree** (the venv lives inside the swapped `<root>/repo`).
- Create the registry identity **before** starting a translated Layer-1 service (a 404 in the FastAPI lifespan is a uvicorn startup failure).
- `supervisor._poll_timeout` must only fold in deadlines `_run_timers` acts on (busy-loop invariant).
- A field that must survive a sync rewrite must be declared on `ServiceRecord` — `PlatformState.load` keeps only declared keys.
- Pools: identity env (`SVC_*`) exists only during a member's build/lifespan phases; **a request-time read of `SVC_*` is a bug**. `sync` registers every identity before opening any health gate (two-pass — keep it).

## Host & deploy (racknerd, Ubuntu 24.04)
- Harness user `harness` (uid 1000, subuid `100000:65536`); interpreter MUST be `/home/harness/venv/bin/python*` (AppArmor grants `userns` to that path only). XFS reflink store at `/home/harness/store` — everything reflink-related stays on that one filesystem.
- Do **not** set `NoNewPrivileges=yes` or an empty `CapabilityBoundingSet=` on units that stage/provision (breaks setuid `newuidmap`).
- Production tree `/home/harness/ams` is written **only** via `scripts/deploy-racknerd.sh`. **Deploy new ams before pushing `pool` overlays to the mirror** — an un-upgraded host marks every pooled member `failed` on its next sync tick.
- Fresh host: `scripts/platform-bootstrap.sh --no-layer1`, then the sync timer. Layer-0 ports are fixed: registry 20100, auth 20101, caddy 20180.
- Commits follow conventional-commit style (`feat(platform):`, `fix:`, `docs(state):`).
