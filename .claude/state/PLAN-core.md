# PLAN-core — ams 1.1.0: host the Cordis-based `api` core

Status: design, 2026-09-29. Owner: this session (ultracode workflow).

## 0. Why (the facts this plan rests on)

`../api` main (v3.1.0, 2026-09-26) replaced the platform ams 1.0.0 was built for.

| ams 1.0.0 assumed (api `ams-platform` branch, v2.0.0) | api main today |
|---|---|
| ~21 Python FastAPI services, one `service.yaml` each | **zero** `service.yaml`; 28 TypeScript plugins under `plugins/<id>/` |
| one process per service (pools: N per process) | **one** Node 24 process, `core` (`node dist/core/main.js`) built on `cordis@4.0.0-rc.10` |
| registry + auth as Layer-0 Python services on 20100/20101 | registry/auth/gateway/store/secrets/health are **plugins inside core** |
| ams stages, translates, provisions, declares, gates each service | core itself hot-installs/replaces plugins: isolated `apply` → health → atomic facade swap → drain → 60 s probation → `auto_revert` |
| Caddy site per mount sidecar | core's `gateway` plugin is the single HTTP entry on **18080** (pages on **18081**); Caddy is a thin host → port front |
| deployer webhook / systemd | phm: `core.service` (systemd) + `core-ship` (build+deploy plugins via `corectl`) + `core-release` (swap `/opt/core`, restart, health gate, roll back) + `core-daily-backup` |

Control plane: unix socket `CORE_SOCKET` (JSON lines `{"id","op","args"}`, file mode 0660 is the
only access control). Ops: `ping upload deploy revert restart enable disable remove gc status
transitions failures failureClasses artifacts privileges autodeploy quota`. Upstream client:
`scripts/corectl.mjs` (`status | artifacts | transitions [id] | failures [id] | upload <file> |
deploy <artifactId> | ship <plugin-dir> | revert|restart|enable|disable|remove <id> | privileges <id> ... | autodeploy on|off [id]`),
JSON on stdout, exit 1 when a transition's `outcome != ok`.

Artifacts: `scripts/build-artifact.mjs` → `buildArtifact(dir, {outDir})` returns
`{artifactId, pluginId, path, commit, dirty, file}`; `file = {format, manifest, bundle, docs, sources, buildInfo}`;
`artifactId = sha256(bundle ‖ canonical(manifest) ‖ docs ‖ sources ‖ canonical(buildInfo))` and
`buildInfo.commit` changes on **every** commit — so artifactId cannot be used for change detection.
A `git archive` tree has no `.git`: set `CORE_SOURCE_COMMIT=<40-hex sha>` so buildInfo carries it.
Plugin entry is `plugins/<id>/core.ts` if present else `index.ts`.

Core env: `CORE_STATE_DIR` (core.sqlite + artifacts/), `CORE_SOCKET`, `CORE_PLUGIN_CONFIG`
(plugins.json, read by the `secrets` plugin: `{plugins:{<id>:{config,grants}}, redactionReaders:[...]}`),
`CORE_LOG_LEVEL`. Core logs JSON lines (observed on **stderr** in the 2026-09-30 e2e; the declaration's `format = "json"` classifies either stream). Requires Node `>=24.19.0 <25`, pnpm 11.19.0.

The racknerd replica of ams 1.0.0 has been torn down (no store, no venv, unit inactive); the DO
mock droplet is deleted. Production stays on phm under systemd and is **out of scope — never touch phm.**

## 1. What ams does for core (and what it must not)

core already owns per-plugin safety. ams must **not** re-implement probation, rollback of plugins,
dependency ordering of plugins or artifact validation. ams replaces what systemd + core-ship +
core-release + core-backup + hand-written Caddy do on phm — rootless, isolated, agent-visible:

1. **Keep core alive**: `core` is an ordinary ams service (userns + cgroup + `memory_max`), restart always.
2. **Toolchain**: managed Node 24 + pnpm 11 in the runtime store (checksum-verified download).
3. **Release core** when core-relevant paths change: stage tree at sha into `releases/<sha>`,
   `pnpm install --frozen-lockfile`, `node scripts/build.mjs`, stop → flip `current` → start →
   HTTP health gate on the gateway port → on failure flip back, start, escalate.
4. **Ship plugins** whose *content* changed: build every roster plugin's artifact from the staged
   tree, compare a content key (artifact minus buildInfo) with what ams last attempted, upload +
   deploy the changed ones through the control socket, wait for probation to settle
   (live / auto_reverted / rejected), record, escalate failures. **Never retry a content key that
   already failed** (no retry storm); a new commit with new content retries.
5. **Front** it with Caddy: host → port sites from config.
6. **Back up** core.sqlite + store state.sqlite (online sqlite backup) and the immutable byte stores.
7. **Escalate** through the existing JSONL escalation path.

The legacy manifest mode (translate / pools / Layer-0 registry+auth) stays in 1.1.0, marked
**deprecated**, untouched — it is what `api v2.0.0` needs and the racknerd box may be rebuilt from
it. Removal is a 2.0.0 decision, recorded as open.

## 2. Layout

Service id `core`, root `R = <state>/services/core/root`:

| path | what | owner |
|---|---|---|
| `R/releases/<sha>/` | staged api tree + node_modules (+ `dist/` once built) | service uid |
| `R/current` → `releases/<sha>` | symlink; the service's `start.workdir` is `current` | service uid |
| `R/data/` | `CORE_STATE_DIR` (core.sqlite, artifacts/); store plugin's data conventionally `R/data/data/` | service uid |
| `R/etc/` | config bundle: `plugins.json`, `jwt.pem`, `jwt.pub`, `fonts/` (0700/0600) | service uid |
| `R/run/control.sock` | `CORE_SOCKET` | service uid |
| `R/build/artifacts/<sha>/` | built artifacts for this tick | service uid |
| `<state>/platform/core.toml` | ams core-mode config (operator-written) | harness |
| `<state>/platform/core.json` | ams core-mode record (release sha, previous, per-plugin last attempt) | harness |
| `<state>/secrets/core/bundle/` | harness-held master copy of the config bundle (0700, never printed) | harness |

Socket path length must stay < 104 bytes (macOS) / 108 (Linux); validate at config load.

## 3. Config `<state>/platform/core.toml`

```toml
[core]
mirror = "api"            # SourceMirror name (<store>/repos/api.git)
url = "/home/harness/store/upstream/api.git"   # validate_url rules apply
ref = "main"
node = "24.20.0"          # exact version → managed download
pnpm = "11.19.0"
gateway_port = 18080      # must equal plugins.json plugins.gateway.config.port
extra_ports = [18081]     # other fixed listeners (pages); informational + port reservation
memory_max = "900M"
log_level = "info"
probation_timeout_s = 120
plugins = ["secrets", "store", "gateway", "auth", "health", "timeservice"]  # roster, install order
core_paths = ["packages/core/", "package.json", "pnpm-lock.yaml", "tsconfig.json", "scripts/build.mjs"]

[core.privileges]
health = ["ops.read"]
"auto-ops" = ["ops.read", "ops.deploy"]

[[site]]
host = "api.lishuyu.app"
port = 18080
```
Unknown keys rejected (reject rather than guess). `plugins` must start with the five foundation
plugins in order `secrets, store, gateway, auth, health` when they are present.

## 4. Interfaces (the contract between the parallel tasks — implement exactly)

### 4.1 Task A — runtime toolchain (`schema.py`, `runtime.py`, `userns.py`)

```python
# schema.py — RuntimeSpec additions (kind == "pnpm" only; validate rejects them elsewhere)
node: str | None      # existing field. NEW meaning: an exact "X.Y.Z" → managed toolchain; a bare major ("22") keeps the old host-node behaviour
pnpm: str | None = None           # exact "X.Y.Z" → managed pnpm installed with the managed node's npm; requires node exact
build: tuple[str, ...] = ()       # argv run in the workdir after install (e.g. ("node", "scripts/build.mjs")); no shell metachar check needed (argv, never a shell)

# runtime.py
@dataclass(frozen=True)
class NodeToolchain:
    node_version: str
    pnpm_version: str | None
    node_dir: Path            # <store>/node/v<ver>
    @property
    def bin_dirs(self) -> tuple[str, ...]   # (pnpm bin?, node bin) to PREPEND to PATH

def ensure_node_toolchain(store: RuntimeStore, node: str, pnpm: str | None = None, *,
                          fetch: Callable[[str], bytes] | None = None,
                          platform_tag: str | None = None) -> NodeToolchain
    # idempotent; downloads https://nodejs.org/dist/v<ver>/node-v<ver>-<os>-<arch>.tar.(xz|gz),
    # verifies sha256 against SHASUMS256.txt from the same dir, extracts into a temp dir beside the
    # target and renames into place (atomic); runs as the HARNESS (store is harness-owned,
    # world-readable); pnpm via `<node>/bin/npm install -g --prefix <store>/pnpm/<ver> pnpm@<ver>`.
    # Linux x64/arm64 and darwin x64/arm64 tags. stdlib only (urllib, tarfile, lzma, hashlib).

def provision_tree(tree: Path, spec: RuntimeSpec, *, block: UidBlock | None, store: RuntimeStore,
                   run_build: bool = True, log_path: Path | None = None,
                   env: Mapping[str, str] | None = None, timeout_s: float = 1800.0) -> None
    # pnpm only (ProvisionError otherwise). `pnpm install --frozen-lockfile` when pnpm-lock.yaml
    # exists, then spec.build if run_build. block given → inside the admin ns as inner root then
    # chown -R to the service (same pattern as _provision_node); block None → plain subprocess as
    # the current user (dev / --no-isolation / macOS). PATH = toolchain bin_dirs + provisioning PATH.
    # `env` is merged in (e.g. CORE_SOURCE_COMMIT). Raises ProvisionError with a stderr tail.

# runtime_env()/provisioning_env: for a managed node spec, PATH gets the toolchain bin dirs first.
# provision(decl, ...) keeps working and, for a managed spec, calls ensure_node_toolchain first and
# runs spec.build after install.

# userns.py
def run_admin(argv, block, *, harness_uid=None, harness_gid=None, timeout_s=60.0,
              env: Mapping[str, str] | None = None, cwd: str | None = None) -> AdminResult   # env merged over _ADMIN_ENV
def run_as_service(argv, block, *, env: Mapping[str, str], cwd: str | None = None,
                   timeout_s: float = 60.0) -> AdminResult
    # the SERVICE's own identity: runtime map (inner 1000 ↔ block), setresuid/setresgid 1000,
    # no_new_privs, execve. Used to build artifacts and to talk to R/run/control.sock (the socket
    # is 0660 and owned by the service uid — the harness itself cannot connect). exe resolved
    # against env["PATH"].
```
Tests: portable tests with a fake `fetch` (a tiny tar.gz with `bin/node` + matching SHASUMS),
checksum mismatch → ProvisionError and nothing left in place, idempotency, platform tag mapping,
schema validation (pnpm without exact node rejected; build/pnpm on non-pnpm kinds rejected),
provision_tree plain mode with a fake `pnpm` on PATH. Linux-marked test for run_as_service.

### 4.2 Task C — sources / gateway / backup

```python
# sources.py
SourceMirror.stage(sha, service_root, block, *, dest: str = "repo", harness_uid=None,
                   harness_gid=None, timeout_s=...) -> Path
    # dest is a relative path under service_root ("repo" default = unchanged behaviour,
    # "releases/<sha>" for core). No "..", not absolute. Parent dirs created owned by the block.
SourceMirror.stage_plain(sha, dest_dir: Path) -> Path   # no userns: cp -a of the canonical checkout (dev/--no-isolation)

# gateway.py
@dataclass(frozen=True)
class CoreSite:
    host: str        # strict hostname (reuse the existing hostname check)
    port: int        # 1024..65535
def render_core(sites: Sequence[CoreSite], cfg: GatewayConfig) -> dict[str, str]
    # same output shape as render(): {"Caddyfile": ..., "sites/<name>.caddy": ...}, caddy-fmt canonical,
    # plain HTTP like Phase A (TLS stays at Cloudflare/tunnel), reverse_proxy 127.0.0.1:<port>,
    # access log per site like existing sites. Goldens under tests/golden/gateway/core/.
# write(state, files) is reused unchanged.

# backup.py — the core service root is discovered like any other service; additionally:
#   * sqlite files under R/data/ recursively (core.sqlite, data/state.sqlite) — check discover()
#     already recurses; if not, extend it without changing any existing R2 key;
#   * byte stores: directories named blobs | oss-bytes | pages-content under R/data/** are
#     immutable content-named files → `rclone copy --immutable` to <prefix>/core/<name>/ (never deleted
#     remotely), copied AFTER the sqlite snapshots so a snapshot never references a missing blob;
#   * R/releases, R/build, R/etc, R/run are never backed up (etc holds secrets: write-only store, D16).
```

### 4.3 Task B — core mode (`platform/core.py`, `platform/corectl.py`, `platform/coresync.py`, `platform/assets/core_plan.mjs`, `platform/cli.py`, `deploy/`)

```python
# core.py
@dataclass(frozen=True) class CoreConfig: ...   # §3; load_config(path) -> CoreConfig; CoreConfigError
@dataclass(frozen=True) class CoreLayout: root, releases, current, data, etc, run, socket, build  # from StateDir
def import_bundle(state, plugins_json: Path, *, rebase: Sequence[tuple[str, str]] = (),
                  jwt_dir: Path | None = None, fonts_dir: Path | None = None, cfg: CoreConfig) -> list[str]
    # validates JSON shape, rebases string values that START with a given prefix (e.g.
    # /var/lib/core → R/data, /etc/core → R/etc), checks gateway port == cfg.gateway_port, writes the
    # harness master copy under <state>/secrets/core/bundle/ (0700/0600); returns the NAMES written, never values.
def place_bundle(state, layout, block | None) -> bool   # copy master → R/etc (admin ns + chown), only if changed
def core_declaration(cfg, layout) -> str                 # service.toml text for id "core":
    # exec ["node", "dist/core/main.js"], start.workdir "current", runtime pnpm + node/pnpm pins,
    # env CORE_STATE_DIR/CORE_SOCKET/CORE_PLUGIN_CONFIG/CORE_LOG_LEVEL, fixed ports (gateway + extras),
    # health http gateway /health start_period 60, stop SIGTERM 40 s, restart always,
    # limits memory_max, logging format json. Must pass schema.validate.

# corectl.py — the control plane, via upstream scripts/corectl.mjs (do not re-implement the protocol)
class Runner(Protocol):
    def __call__(self, argv: Sequence[str], *, env: Mapping[str, str], cwd: str | None, timeout_s: float) -> AdminResult: ...
def isolated_runner(block) -> Runner   # userns.run_as_service
def plain_runner() -> Runner           # subprocess.run, no shell (dev / --no-isolation)
class CoreControl:
    def __init__(self, runner: Runner, tree: Path, socket: Path, path_env: str): ...
    def status(self) -> dict            # corectl status
    def upload(self, artifact_file: Path) -> str
    def deploy(self, artifact_id: str) -> dict   # transition; outcome ok|rejected|failed|...
    def transitions(self, plugin_id: str, limit: int = 20) -> list[dict]
    def failures(self, plugin_id: str, limit: int = 5) -> list[dict]
    def privileges(self, plugin_id: str, privileges: Sequence[str]) -> dict
    def ping(self) -> bool

# assets/core_plan.mjs — package data (like pool_runner.py; src/ams never imports it). Run with the
# tree's node: `node core_plan.mjs <tree> <outDir> <pluginId>...`; imports <tree>/scripts/build-artifact.mjs,
# builds each plugin into outDir and prints one JSON line per plugin:
#   {"pluginId","dir","artifactId","contentKey","path","commit","dirty"}  or {"pluginId","error"}
# contentKey = sha256(bundle ‖ canonical(manifest) ‖ docs ‖ sources) — the artifactId formula minus buildInfo.

# coresync.py — one tick, a separate process on a timer (never inside the supervisor loop)
def tick(state, store, cfg, *, isolation: bool = True, escalation=stdout JSONL, now=time.time,
         control_factory=..., mirror_factory=..., sleep=time.sleep) -> CoreReport
  1. fetch mirror @ cfg.ref → head sha
  2. if head != record.staged_sha: stage tree → R/releases/<head>; provision_tree(run_build=False)
  3. release needed = no record.release_sha, or changed_paths(release_sha, head) ∩ cfg.core_paths:
       provision_tree(run_build=True); place_bundle; declare core (write service.toml, ctl reload)
       stop core → flip current → start → http gate on 127.0.0.1:gateway_port/health (timeout)
       fail → flip back to previous, start, gate, escalate "core_release_failed" (and "core_down" if that gate fails too)
  4. plan: core_plan.mjs over cfg.plugins in the head tree (run_as_service / plain) → content keys
  5. ship set = roster plugins whose contentKey != record.plugins[id].content_key (success OR failure);
     plugins not installed in core yet are shipped in roster order (foundation first)
  6. for each: upload + deploy; outcome != ok → record failure + escalate once
  7. poll status every 3 s until every deployed plugin leaves probation (≤ probation_timeout_s):
     active on the new artifact → live; anything else → failed (+ transitions/failures excerpt in the escalation)
  8. apply cfg.privileges idempotently (only when they differ from status)
  9. render + write gateway (render_core) — Caddy reload through the existing mechanism
 10. write core.json only if something changed (a tick that changes nothing writes nothing)
  Exit code reflects THIS tick (carried failures do not fail later ticks — same rule as 593daae).
  Every step logs; every failure is an escalation record {kind, service:"core", plugin?, cause, sha}.
  Deduped by normalized cause (reuse policy.EscalationDeduper if it fits).
def rollback_release(state, store, cfg, *, isolation=True) -> CoreReport   # flip to record.previous_release_sha, gate
def ship(state, store, cfg, plugin_ids, *, force=False, isolation=True)     # force ignores the content-key record

# cli.py — new group, legacy commands untouched:
#   ams platform core config import <plugins.json> [--rebase OLD=NEW]... [--jwt-dir D] [--fonts D]
#   ams platform core bootstrap   # declare caddy + core, first release, install roster
#   ams platform core sync [--no-isolation]
#   ams platform core status [--json]   # release sha, previous, per plugin: phase, live, artifact[:12], commit, ams last attempt, drift
#   ams platform core release --rollback
#   ams platform core ship <id>... [--force]
# deploy/ams-core-sync.{service,timer} (60 s, flock), mirroring ams-platform-sync.*
```

## 5. Invariants to keep (from CLAUDE.md, restated for the implementers)

- stdlib only, Python 3.12, frozen dataclasses, type hints, `logging` never `print`, never `shell=True`.
- Nothing adds a thread to a process that forks into a user namespace; nothing forks before `build_supervisor`.
- Provisioning/building/shipping never runs inside the supervisor loop.
- Stop core before flipping `current` (the process imports from it).
- No two test modules share a basename. Linux-only tests are `tests/linux/test_<x>_live.py`.
- Secrets: bundle values never logged, never in argv, never printed; status prints names only.
- `src/ams` never imports anything from `assets/`.

## 6. Verification (definition of done)

1. `.venv/bin/python -m pytest -q` green; baseline 1231 passed / 125 skipped rises by the new tests, no new skips except Linux-only.
2. Local end-to-end with the real `../api` checkout (macOS, `--no-isolation`, nvm node 24.20.0):
   mirror `../api` → `ams platform core bootstrap` + supervisor → `/health` 200 on the gateway port with
   the foundation plugins + timeservice live; a second tick with no change writes nothing; a content
   change to one plugin ships only that plugin and it goes live; a deliberately broken plugin build is
   rejected/auto-reverted, escalated once, and not retried on the next tick. Evidence in
   `.claude/state/evidence/core-e2e-local-2026-09-29.txt`.
3. Linux live (racknerd) is **not** part of this run: the host was torn down; re-provisioning it is a user decision.
4. Docs: `docs/platform-core.md` (new, the end-to-end story), `docs/platform.md` (legacy banner +
   pointer), `README.md`, project `CLAUDE.md`, `CHANGELOG.md` (new, Keep a Changelog, 1.0.0 + 1.1.0),
   `DECISIONS.md` D31, `PROGRESS.md` entry. Version 1.1.0 in `pyproject.toml`, tag `v1.1.0`, push.

## 7. TODO (release + publish, 2026-09-29)

- [x] 1. Implement A/B/C + integrate (workflow wf_f5809346-271) — A ✅ C ✅ B ✅ integrate ✅ (a9fca88)
- [x] 2. Merge `origin/security-audit-fixes` (8 commits, 2026-09-05: issues #1 #2 #4 #5 + docs #6–#12); core-mode admin-ns `pnpm install` gets `provisioning_mask` (1c0460e; superseded in step 3: core-mode installs and builds now run as the service, D32)
- [x] 3. Adversarial review of the full diff → fix (25 findings, 24 applied test-first; D32, CHANGELOG [1.1.0])
- [x] 4. Local e2e against the real `../api` (`--no-isolation`, node 24.20.0) → evidence file
  `.claude/state/evidence/core-e2e-local-2026-09-29.txt` (2026-09-30: all six scenarios pass;
  two fixes — gate fails fast on a harness-`failed` core; Node trace-warnings hint is INFO)
- [x] 5. Docs: `docs/platform-core.md`, `docs/platform.md` legacy banner, README, CLAUDE.md, CHANGELOG (merge the branch's), D31, PROGRESS; version 1.1.0 (2026-09-30; suite 1608 passed / 167 skipped)
- [x] 6. Commit on main
- [x] 7. `git filter-repo --replace-text` over all refs: 2 throwaway pilot `SVC_SECRET`s (06276ce, `examples/api-pilot/*/service.toml`) + 2 IPs (deleted DO droplet, tailnet)
- [x] 8. gitleaks + custom sweep re-scan → clean (only the known fake-key test fixture)
- [x] 9. Push rewritten `main` + tags `v1.0.0` (rewritten) and `v1.1.0` to the recreated repo (no force-push needed; `security-audit-fixes` is merged into main and kept local only)
- [x] 10. Repo → public (2026-09-30). Done as delete + recreate (refs/pull/13 kept the old history reachable by SHA; a force-push could not remove it): issues #1–#12 deleted first (#3 and review finding security-4 are recorded in DECISIONS Open), PR #13 gone with the old repo, releases v1.0.0/v1.1.0 recreated, secret scanning + push protection on
- [ ] Open (user): re-provision racknerd for Linux live verification of core mode
