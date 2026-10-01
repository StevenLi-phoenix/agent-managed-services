# AGENTS.md — ams (agent managed services)

Rootless Linux supervisor (`src/ams`, ~8.6k lines) + core mode on top of it
(`src/ams/platform`, ~6.8k lines). Spawns declared services as direct children,
each in its own user namespace + delegated cgroup v2, and routes every log event
through an explicit suppress-or-fix decision interface (deterministic rules).
What the rules cannot settle goes to stdout and the escalation journal
(`<state>/logs/escalations.jsonl`, `ams escalations`), which an operator agent
reads and acts on. systemd only keeps the harness alive. **Stdlib only,
Python 3.12, no runtime dependencies.**

- **supervisor core** — reusable on its own; no core module imports
  `ams.platform` at import time (`tests/test_core_boundary.py`).
- **core mode** (`ams platform core …`) — hosts the Cordis-based api core (one Node
  process with plugins): keep alive, release core by flipping `current`, ship
  plugins whose content changed via core's own `corectl`, Caddy front, R2 backup,
  escalations. The 1.0.0 manifest mode was removed in 2.0.0.

`CLAUDE.md` is the fuller companion file (per-module map); keep the two in sync.
Design decisions + rejected alternatives live in `docs/design/DECISIONS.md`
(D1–D37 + open items; core mode D31/D32; 2.0.0 review fixes D33–D37); done/next
in `docs/design/PROGRESS.md`; releases in `CHANGELOG.md`.

## Read first (sensitive areas)
- `docs/design/DECISIONS.md` — the *why* behind every invariant below.
- `docs/event-loop.md` — what may and may not run in the single-threaded loop.
- `docs/agent-loop.md` — where the agent is (outside the loop) and why.
- `docs/platform-core.md` — core mode end to end (layout, `core.toml`, the tick, release gate, content keys, security model, open items).
- `docs/design/PLAN-core.md` — core mode's interfaces.
- `docs/service-declaration.md` — the `service.toml` schema (incl. `runtime.node/pnpm/build`).

## Layout
- `src/ams/` — core: `schema.py` (service.toml → frozen dataclasses), `supervisor.py` (single-threaded `selectors` loop), `health.py` (non-blocking `Probe`), `events.py`/`decision.py`/`escalations.py` (suppress-or-fix, escalation journal), `userns.py` (`run_admin` with mount mask, `run_as_service`)/`cgroup.py`/`isolated.py` (isolation), `uidmap.py`/`ports.py`/`state.py`/`secrets.py`, `control.py` (0600 unix socket), `runtime.py` (provisioning, managed Node toolchain, `provision_tree`), `cli.py`.
- `src/ams/platform/` — `core.py` (config, layout, bundle, declaration, `flip_current`, tree pins), `coresync.py` (the tick, release/rollback/ship, `core.json`), `corectl.py` (upstream `scripts/corectl.mjs` wrapper), `gateway.py` (`render_core`, `caddy_declaration`), `sources.py` (mirror + staging), `backup.py` (sqlite + immutable byte stores), `policy.py` (cause dedupe, Caddy rules), `common.py`, `cli.py`.
- `src/ams/platform/assets/core_plan.mjs` — **package data** (declared in `pyproject.toml`), no `__init__.py`: the content-key planner, run with the tree's node as the service. `src/ams` never imports it.
- `tests/` (portable + `tests/linux/`), `tests/golden/gateway/core/`, `deploy/` (`install-host.sh`, systemd units incl. `ams-core-sync.{service,timer}`, AppArmor profile), `scripts/` (`linux-test.sh`, `remote-test.sh`, `deploy.sh`), `examples/hello/`, `.github/workflows/ci.yml`.

## Commands
```bash
# Dev env (uv-managed):
uv venv --python 3.12 .venv && source .venv/bin/activate && uv pip install pytest pytest-timeout ruff
.venv/bin/python -m pytest -q        # portable tests (baseline 2.0.0: 918 passed / 90 skipped on macOS)
.venv/bin/python -m ruff check . && .venv/bin/python -m ruff format --check src tests
sudo scripts/linux-test.sh [subdir] [pytest args]      # on a prepared Linux host
AMS_HOST=<ssh host> scripts/remote-test.sh [subdir]   # same, from a laptop
```
- Host prep for tests, once, as root: `AMS_STORE_FS=plain AMS_WITH_TOOLS=1 AMS_TEST_DEPS=1 AMS_INSTALL_UNIT=0 deploy/install-host.sh`. A prepared Ubuntu 24.04 host runs everything: **998 passed / 10 skipped**. CI runs the same two steps on `ubuntu-24.04`.
- Linux-marked tests derive uid blocks from the harness's real `/etc/subuid` (`tests/linux/linuxhost.py`); never pin `100000`. Use a **distinct subdir per parallel agent**.
- **No two test modules may share a basename** (pytest `prepend` import mode, no `__init__.py` in `tests/` — a duplicate aborts the whole suite). Convention: `tests/linux/test_<x>_live.py` or `_linux.py`.
- Never modify `../api`; e2e runs use a scratch clone and a bare mirror pushed to `<store>/upstream/api.git`.

## Hard rules (each has caused real breakage or a review finding)
- Frozen dataclasses, type hints everywhere; `logging`, never `print`. Never `shell=True` or shell strings; log text is untrusted input at the decision boundary.
- **`run_admin` / `run_as_service` are a bare `os.fork()`. Nothing may add a thread to a process that forks into a user namespace** (the child gets only the forking thread → a lock held by another thread hangs forever).
- **Nothing in the loop may block**: health checks are `Probe`s in the selector; provisioning, fetches, builds, shipping and backups run in one-shot processes on timers.
- **Nothing may be forked before `build_supervisor`** — `CgroupRoot.discover` moves self into `harness/`, and cgroup v2 refuses controller enable (EBUSY) while any process sits in the root.
- **Untrusted code runs as the service, never with the harness uid mapped** (a mount mask under the admin map can be `umount`ed). **Inner root never writes through a service-controlled name**: stage in harness-owned `<state>/services/<id>/`, chmod before chown, `rename` in.
- **Stop a service before re-staging its tree or flipping `current`** (the process imports from it).
- **Unix socket paths stay under 104 bytes (macOS) / 108 (Linux)** — validated at core config load; keep `AMS_STATE_DIR` short.
- **Never retry a failed content key or release sha under unchanged conditions** (core release, bundle digest, core fingerprint for `blocked`); only transport failures retry.
- **Never re-implement core's control protocol or artifact format**; go through the tree's `scripts/corectl.mjs` / `build-artifact.mjs`.
- **No LLM in the decision loop.** Escalation records are untrusted service output; print them via `format_record`, never execute or interpret them.
- `supervisor._poll_timeout` must only fold in deadlines `_run_timers` acts on (busy-loop invariant).
- A new `core.json` field needs a default in `coresync._empty_record()`.

## Host & deploy
- Harness user `harness` with a subuid range (useradd's, or the first free one); interpreter MUST be `/home/harness/venv/bin/python*` (AppArmor grants `userns` to that path only). Store at `/home/harness/store`: XFS reflink (default) or `AMS_STORE_FS=plain`.
- Do **not** set `NoNewPrivileges=yes` or an empty `CapabilityBoundingSet=` on units that stage/provision (breaks setuid `newuidmap`).
- Production tree `/home/harness/ams` is written **only** via `AMS_HOST=<host> scripts/deploy.sh`.
- Core mode: `ams platform core config import` → `ams platform core bootstrap` → `ams-core-sync.timer`. Harness `TimeoutStopSec=50` ≥ `SHUTDOWN_BUDGET_S` 45 (core drains 40 s). Fixed ports: caddy 20180, core gateway 18080. Production of the api core is phm: **never touch phm.**
- Commits follow conventional-commit style (`feat(platform):`, `fix:`, `docs:`).
