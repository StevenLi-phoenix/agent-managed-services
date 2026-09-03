# Pools: N manifests, one process, distinguished by tag

The fleet's memory is dominated by a fixed per-process cost, not by the
services themselves: on racknerd, 16 Python uvicorn processes hold 44–74 MiB
each while `import fastapi` alone is 44 MiB, and 14 member packages imported
into one interpreter cost 59 MiB in total (n=1 each, import-time only). A
**pool** collapses N `api` services that would otherwise be N processes into
one ams declaration, one root, one venv and one process, and distinguishes the
members by the tag they already have — their own service id. See
`.claude/state/PLAN-pool.md` for the full design and rejected alternatives,
`.claude/state/spike-pool.md` for the runner's serving-topology spike, and
`.claude/state/DECISIONS.md` D29 for the decision record.

Read `docs/platform.md` first for the sync loop this extends,
`docs/manifest-translation.md` for the per-manifest translation a pool builds
on, and `docs/platform-sidecars.md` for the sidecar shapes a pool adds two
optional keys to.

## What a pool is

One ams declaration (`pool-<name>`) running N `api` services as N
`uvicorn.Server` instances on N ports in one asyncio event loop, via
`src/ams/platform/assets/pool_runner.py`. Each member keeps its own allocated
port, its own registry identity and ACL, its own Caddy route, its own
`/health` on its own port, its own `data/<member>/` directory, its own
secrets and its own `ServiceRecord` and change detection. Nothing new
distinguishes a member from any other service — it is still the same
`SVC_NAME`, the same registry id, the same mount id. The only new concept is a
*grouping* key saying which process a manifest runs in.

## Tag semantics

The grouping key is `pool = "<name>"` in `service.ams.toml`, the ams-only
overlay — **not** in `service.yaml`. The legacy deployer validates manifests
against a JSON schema with `"additionalProperties": false` at the root, so a
new `service.yaml` key would hard-fail it; the overlay is already the
ams-only channel and is already read before `translate()`.

```toml
# api/services/kvservice/service.ams.toml
pool = "core"
secrets = ["DEEPSEEK_API_KEY"]   # existing keys, unchanged
```

`src/ams/platform/static.py`'s `Overlay` dataclass carries a `pool: str |
None` field; `overlay_pool(manifest_dir)` reads it, mirroring
`overlay_secret_names`. Rules, enforced by `load_ams_overlay`:

| aspect | rule |
| --- | --- |
| value | must match `ams.schema.SERVICE_ID_RE`, `^[a-z][a-z0-9-]{0,31}$` |
| pool service id | `pool-<value>`, e.g. `pool-core` — the prefix keeps a pool id from ever colliding with a member id |
| reserved | `registry`, `auth`, `caddy`, and the literal `pool` (the runner's own admin port name) — `static._RESERVED_POOL_NAMES` |
| applies to | `kind: service` only; `pool` on `kind: static` raises |

Cross-manifest rules (a pool of one, two members mangling to the same env
suffix, a member id colliding with the reserved `pool` port name, two members
disagreeing on a shared non-identity env key) cannot be seen by the overlay
reader — it sees one file at a time — so they are raised by
`translate.build_pool` and `sync._check_pool`/`_pool_env`, not by
`load_ams_overlay`.

## What translate emits

`ams.platform.translate` gains `PoolMember`, `PoolTranslation`,
`build_pool(pool, members, ctx)`, `pool_member(translation, manifest_dir_rel)`
and `mangle_member(member_id)` (`"notification-svc"` → `"NOTIFICATION_SVC"`,
checked for injectivity across the pool by `build_pool`). A member's own
`translate()` call still runs with `TranslateContext.pool` set to the
unprefixed pool name, which redirects every absolute path (`root_for`,
`_rewrite_data_path`'s `data_subdir`) into the pool's root instead of the
member's own — a member never gets a root, a venv or a `service.toml` of its
own.

The golden pair for a two-member pool (`kvservice` + `timeservice`, from
`tests/golden/platform/pool/`) is reproduced below verbatim — this is the real
emitted output, not the plan's sketch, and it is a byte-exact golden the test
suite pins.

**`<state>/services/pool-core/service.toml`:**

```toml
id = "pool-core"
name = "pool core"
secrets = ["SVC_SECRET__KVSERVICE", "SVC_SECRET__TIMESERVICE"]
depends_on = ["registry"]

[start]
argv = ["python", "/home/harness/state/services/pool-core/root/pool_runner.py"]
workdir = "repo"

[env]
AUTH_URL = "http://127.0.0.1:19101"
GIT_COMMIT = "0123456789abcdef0123456789abcdef01234567"
KV_DB_PATH = "/home/harness/state/services/pool-core/root/data/kvservice/kvservice.db"
POOL_ID = "core"
POOL_PORT_ADMIN = "${PORT_pool}"
POOL_PORT_KVSERVICE = "${PORT_kvservice}"
POOL_PORT_TIMESERVICE = "${PORT_timeservice}"
REGISTRY_URL = "http://127.0.0.1:19100"
SVC_M2M_PUBLIC_KEY_PATH = "/home/harness/state/services/pool-core/root/etc/jwt-rs256.pub"

[ports]
kvservice = 0
pool = 0
timeservice = 0

[runtime]
kind = "uv"
python = "3.12"
packages = ["uvicorn[standard]==0.52.4", "-e", "services/kvservice", "-e", "apps/timeservice"]

[health]
kind = "http"
port = "pool"
path = "/_pool/health"
start_period_s = 240.0

[logging]
format = "level-prefix"

[stop]
signal = "SIGTERM"
timeout_s = 10.0

[limits]
memory_max = "210M"
cpu_max = "100%"
pids_max = 80

[restart]
policy = "always"
backoff_s = 10.0
```

Notes on the numbers: `memory_max = pool_memory_base(150M) + N ×
pool_memory_per_member(30M)`; `pids_max = pool_pids_base(48) + N ×
pool_pids_per_member(16)` — the SDK runs up to three daemon threads per
member and cgroup v2 counts a thread as a pid, so the per-service default of
64 would be wrong; `cpu_max = "100%"` is one budget for what used to be N ×
40%; `start_period_s = 240` for N sequential `build_app()` calls on one vCPU.
All four are `TranslateContext` fields, so an operator can override them, and
**all four are 2026-09-03 starting values derived from an n=1 import
measurement, not facts** — see D29's Open section and §7.3 below.
`restart.policy = "always"` unconditionally: "never restart" would take every
member down for one member's crash, which is exactly the outage a pool must
not create. `[logging] format = "level-prefix"` turns off the severity
heuristic in favour of the runner's own level token (§ Logging below).

The pool venv pins `uvicorn[standard]==0.52.4` ahead of every member's
editable install (`packages[0]`), overriding each member's own `>=0.27` range
— see § Uvicorn pin below for why.

**`<state>/services/pool-core/root/pool.json`** (written by `_phase_declare`,
mode 0640, harness-owned then chowned to the pool's uid block):

```json
{
  "version": 1,
  "pool": "core",
  "sha": "0123456789abcdef0123456789abcdef01234567",
  "members": [
    {
      "id": "kvservice",
      "app": "kvservice.main:build_app",
      "factory": true,
      "port_name": "kvservice",
      "port_env": "POOL_PORT_KVSERVICE",
      "health_path": "/health",
      "secret_env": { "SVC_SECRET": "SVC_SECRET__KVSERVICE" },
      "env": {
        "AMS_DATA_DIR": "/home/harness/state/services/pool-core/root/data/kvservice",
        "AUTH_URL": "http://127.0.0.1:19101",
        "GIT_COMMIT": "0123456789abcdef0123456789abcdef01234567",
        "KV_DB_PATH": "/home/harness/state/services/pool-core/root/data/kvservice/kvservice.db",
        "REGISTRY_URL": "http://127.0.0.1:19100",
        "SVC_AUDIENCE": "kvservice",
        "SVC_CAPABILITIES": "keyvalue",
        "SVC_HEALTH_PATH": "/health",
        "SVC_M2M_PUBLIC_KEY_PATH": "/home/harness/state/services/pool-core/root/etc/jwt-rs256.pub",
        "SVC_NAME": "kvservice",
        "SVC_OWNER": "steven",
        "SVC_ROOT_PATH": "/kv"
      }
    },
    { "id": "timeservice", "...": "same shape" }
  ]
}
```

`app`/`factory` are parsed straight out of the member's `process.exec`
(`uvicorn <mod>:<attr> [--factory] …`), so a module-level `app` (`resume`)
comes out `factory: false` with no special case, and any uvicorn option
besides `--factory`/`--host 127.0.0.1`/`--port` — anything that would have
changed how the server is built — makes `translate` raise rather than
silently drop it. `env` is the member's `identity_env` and `shared_env`
merged, plus `AMS_DATA_DIR` rewritten to the member's own `data/<member>/`
subdirectory (the pool *process* env's `AMS_DATA_DIR` is the shared parent, so
a member reading it directly would see its neighbours' state — this is why
`AMS_DATA_DIR` is in the identity set and comes from `pool.json`, not from
`[env]`). **No secret value is ever in this file** — `secret_env` maps a
member's own env name to the *mangled* name the runner reads from the process
environment (`SVC_SECRET` → `SVC_SECRET__KVSERVICE`); the value arrives
through the pool declaration's `secrets` list exactly like any other
declaration's secrets (D16).

**`<state>/platform/mounts/kvservice.json`** — two new, additive, optional
keys; everything else byte-identical to a standalone mount:

```json
{
  "version": 1, "id": "kvservice", "kind": "service",
  "gateway": "api.lishuyu.app", "path": "/kv", "subdomain": null,
  "port_name": "kvservice", "port_owner": "pool-core",
  "static_root": null, "build": [], "headers": {}
}
```

`port_owner` absent or null means "my own id" — today's behaviour for every
non-pooled service, so no existing mount golden changes and the sidecar stays
version 1 (the same additive precedent D26 set for `deployed_sha`). Every
pooled member's `port_name` is its own id, mangled to satisfy the widened
`PORT_NAME_RE` when the id contains a hyphen (`translate.pool_port_name`).

**`<state>/platform/registry/kvservice.json` is completely unchanged.** This
is the strongest property of the chosen design: the registry never learns
that pooling exists.

## The runner contract

`src/ams/platform/assets/pool_runner.py` is package **data**, not part of the
`ams` package — there is deliberately no `__init__.py` in `assets/`. It ships
with ams (`_phase_declare` copies its bytes to `<pool-root>/pool_runner.py`)
but it is the pool's own venv python that imports and executes it; `src/ams`
itself must never import `fastapi` or `uvicorn`, and a portable test asserts
that importing every `ams.*` module leaves both absent from `sys.modules`.

### Identity keys, swapped per phase

Fifteen members' `SVC_NAME`s cannot coexist in one process environment;
fifteen sequential reads under a swapped environment can. The runner reads
`Path(__file__).with_name("pool.json")` and, for each member, swaps
`IDENTITY_KEYS` into `os.environ` (and restores the prior value exactly,
never just pops the key) around **three** phases:

1. **build** — import the module, resolve the attribute, call it if
   `factory` else use it directly (covers `resume`'s module-level `app`);
   construct `uvicorn.Config(app, host="127.0.0.1", port=<member port>,
   log_config=None, access_log=False)`, `config.load()`, `uvicorn.Server
   (config)`, `server.lifespan = config.lifespan_class(config)`.
2. **lifespan startup** — `await server.startup()`, run **sequentially**,
   one member at a time.
3. **lifespan shutdown** — `await server.shutdown()`, sequential, same
   reason.

The identity set:

```
SVC_NAME  SVC_AUDIENCE  SVC_SECRET  SVC_ROOT_PATH  SVC_ENDPOINT
SVC_CAPABILITIES  SVC_HEALTH_PATH  SVC_DISPLAY_NAME  SVC_OWNER  SVC_LOCATION
PORT  AMS_DATA_DIR
```

Startup and shutdown are sequential, not just build, because an AST survey of
all 19 `api` services plus the SDK (n=19, static — `spike-pool.md` finding 1)
found `timeservice`, `llmpricing`, `resume`, `displayservice` and
`test-service` calling `sdk.config.load_from_env()` again **inside their
lifespan**, not only at import time. A runner that swapped only around
`build_app()` would hand four of those five a neighbour's identity at
startup.

Everything **not** in `IDENTITY_KEYS` is folded once into the process
environment as a union across all members (`_apply_non_identity_union`) and
never restored — every request-time env read outside the identity set uses a
service-specific name (`MESSAGE_INGEST_TOKEN`, `LOCATION_*`,
`DEEPSEEK_API_KEY`, `RESEND_API_KEY`, `BOT_LLM_*`, …), which is what makes the
union safe. `translate.build_pool` rejects a pool whose members bind one
non-identity key to two different values, naming both members and the value.
`REGISTRY_URL`/`AUTH_URL` are non-identity but mandatory and identical for
every member — the SDK issues an ACL refresh against the *default* registry
URL when they are unset, so omitting them points a member at the wrong
registry rather than at none.

The one identity key read at **request** time, not only at build/lifespan
time, is `SVC_ENDPOINT` — `sdk/ui.py:266 login_redirect` and
`mailbox/main.py:249 _sso_redirect` (the login return-to). The env swap
cannot cover a request-time read, so it resolves to `""` in a pool unless the
`api`-side fix lands (deriving the return-to from the request instead of a
build-time constant — the one api-repo change this design makes, tracked as
T11).

### `SystemExit`, not just `Exception`

A uvicorn startup failure calls `sys.exit(3)` inside the task, and in one
event loop that `SystemExit` — a `BaseException`, invisible to a bare `except
Exception` — propagates out of `asyncio.run` and takes every member down with
it (verified in the spike). `_build_member`, `_startup_member` and
`_shutdown_member` each catch `(Exception, SystemExit)` and log one `ERROR
[<member>] pool: <phase> failed: <type>: <msg>` line rather than letting it
escape. A member that fails to build or start is recorded in `failed` and
skipped; the rest of the pool keeps serving. The process itself exits
non-zero only when **zero** members started.

### `/_pool/health`

A bare ASGI callable, no framework, on `POOL_PORT_ADMIN`
(`make_admin_app`). `GET /_pool/health` returns 200 with `{"pool", "ok",
"failed"}` when at least one member is serving and 503 when none is;
anything else is 404. This is deliberately *not* "all members healthy" — the
endpoint drives the supervisor's restart policy and the `depends_on` health
gate (D27), and an all-or-nothing gate would let one bad member flap eleven
good ones. Per-member readiness is judged by `sync._phase_health`, per
member, on that member's own port — a pool never claims a member is up by
proxy.

### Logging

The runner owns the root logger (`logging.StreamHandler` + a formatter)
before importing any member, so the SDK's own `logging.basicConfig()` — gated
on `if not logging.getLogger().handlers` — is a no-op. Format:

```
LEVEL [member] logger: msg
```

`_MemberTagFilter` maps each record's top-level logger package to the member
id that owns it (`kvservice.main` → `kvservice`), falling back to `"pool"`
for the runner's own control-plane lines and for uvicorn's module-global
loggers (each `uvicorn.Config` gets `log_config=None` so it never
reconfigures the root). Consequence, stated plainly: `LogLine.service_id` is
`pool-core` for every line the harness sees — the member tag lives in the
message text only, not in a new event field. Dedup stays member-granular
because `policy.cause_key`'s text normalization preserves the bracketed
`[member]` token (checked directly by a test, per T7 — see D29).

### Uvicorn pin

Steps 3–8 of the startup sequence use six uvicorn internals —
`Config.load`, `Config.lifespan_class`, `Server.lifespan`, `Server.startup`,
`Server.main_loop`, `Server.shutdown` — none of them public API. The pool
venv pins `uvicorn[standard]==0.52.4` ahead of every member's own `>=0.27`
range specifically so this stays stable under the version the spike verified
against. `_check_uvicorn_api` (`main()`, before any member is built)
`hasattr`-checks all six and exits non-zero with `FATAL pool: uvicorn <ver>
lacks <Class>.<attr>; pool runner requires the 0.52.x internal API` on any
miss — a loud refusal at second zero beats a pool that dies halfway through
startup. Any uvicorn bump is a deliberate revisit of this file, never a
routine dependency update.

## Sync, gateway, registry, health, backup, rollback

- **`sync.py`** groups every manifest whose overlay declares `pool = "<name>"`
  into one `_PoolGroup`; `_check_pool` enforces the two cross-manifest rules
  the translator cannot see on its own (duplicate member ids, the pool name
  colliding with a member id), then `build_pool` builds the one `_Pending`
  for the pool. `ServiceRecord` gains two additive fields — `pool: str |
  None` on a member record (the pool it runs inside) and `pool_members:
  list[str]` on the pool record (its members, sorted) — declared on the
  dataclass because `PlatformState.load` drops any key the dataclass does not
  declare; `as_json` omits both when unset, so a fleet with no pools keeps
  the exact record shape it had before pools existed. A pool moves to head
  iff **any** member is affected by the commit range (D26's per-service rule,
  applied at pool granularity); if members disagree about their deployed
  commit, the pool moves to head. `_phase_materialize` stages and provisions
  the pool exactly **once** — a pooled member has neither a tree nor a venv
  of its own, so twelve `cp --reflink` + twelve `uv sync` become one of each.
  A pooled member's two sidecars (mount, registry) are still written per
  member, like any other service; what it does *not* get is a
  `service.toml` — `_unlink_member_decl` removes a stale one left over from
  before it joined the pool, leaving the member's root (and its data) alone.
- **`gateway.py`**: `resolve_ports` gained the whole change. A mount's
  `port_owner` (default: the mount's own `id`) says which allocator row
  actually holds the port; `_render_site`, `_port_for`, every header/CSP/CORS
  table and every Caddyfile golden are untouched. A pooled member's site
  block is byte-identical to its standalone form — only the port number
  differs. `_phase_gateway` needs no change of its own: it already renders
  from the sidecars on disk, and `resolve_ports` following `port_owner` is
  where the behaviour actually lives.
- **`_phase_finish`** (the health gate) looks the port up on `item.port_owner
  or item.id`, so a pooled member's probe URL is still `http://127.0.0.1:
  <its own port>{its own health_path}` — B never shares a port, so this
  needed no other change. A member whose pool failed to build is marked
  `failed` with a message naming the pool, not separately escalated — one
  process failing is one cause, and per-member escalations for it would be
  exactly the noise `_Run.fail`'s dedupe exists to prevent.
- **`backup.py`**: `Target` gained `label: str = ""` (empty = `service_id`);
  `archive_name`/the remote key use `effective_label` (`label or
  service_id`). `discover` reads `<root>/pool.json` when present and labels a
  database found at `data/<member>/x.db` with that member's id when `<member>`
  is a known member; anything else (a bare `data/x.db`, or a subdir not in
  the member list) keeps the pool's own id, exactly like an unpooled service.
  Result: **R2 keys are byte-identical before and after pooling** for every
  logical service — a member's key is `<member>-<db-stem>-<stamp>.db.gz`
  whether or not it happens to share a process with anyone else.
- **`policy.py`**: `_pool_suffix(record)` reads a failed record's
  `pool_members` (when present) and appends `" (pool of N: a, b, c)"` to the
  crash-loop escalation, so "one process crashed" reads as "N services are
  down" without an operator having to go look up the roster.
- **`rollback.py`**: a member id is **refused** — `"kvservice runs inside
  pool 'pool-core': its members share one process, one tree and one venv, so
  the pool is the rollback unit. Roll the whole pool back with 'ams platform
  rollback pool-core'"` — because a pool's "one id in, one id out" contract
  cannot hold when one process runs N builds. Rolling back the pool id itself
  re-derives the pool **as it was at that commit**: there is no manifest for
  a pool, so `_find_pool_translation` re-translates every member whose
  overlay named that pool at the target sha (with `TranslateContext.pool`
  set) and calls `build_pool` over the result, all or nothing — a member
  that fails to translate at that sha means the pool cannot reach that
  commit at all. The health gate afterwards runs per member
  (`_step_pool_health`), because the pool's own admin probe answering only
  proves the runner is up, not that every member's app built and mounted.

## `ams platform pool plan | adopt`

`ams platform sync` never moves a byte of data — a sync tick is mechanical,
on a 60 s timer, and moving a live SQLite file is neither. So the first tick
after `pool = "core"` appears in the overlays stops at an **adoption guard**
in `_declare_pool`: if any member's pre-pool `services/<member>/root/data`
still holds something, the pool's declaration is refused and the tick
escalates once, naming the exact command to run
(`ams platform pool adopt core`). Sync never moves data; adoption is an
explicit, operator-run, idempotent step (`src/ams/platform/pool.py`):

```
ams platform pool plan core      # what would move; writes nothing
ams platform pool adopt core     # move it
```

For each member, `adopt`:

1. stops it over the control socket, and waits for the supervisor to confirm
   it is down — a process writing to a database is not a process whose
   database may move;
2. moves `services/<member>/root/data/*` into
   `services/pool-<name>/root/data/<member>/` and hands it to the pool's uid;
3. copies `secrets/<member>/<NAME>` to `secrets/pool-<name>/<NAME>__<MANGLED>`
   (never overwriting a value that is already there);
4. unlinks `services/<member>/service.toml` — the pool's declaration is what
   runs the member now.

It never deletes the legacy root: the root keeps the member's repo, its venv
and an empty `data/` after adoption, and an operator removes it by hand once
a backup cycle has proved the pool healthy.

**Why the move is a two-hop staging move, not a direct `mv`.** `run_admin`
maps exactly one uid block into the namespace it forks (inner 0 = the
harness, inner 1000 = *that* block). A member's `data/` is owned by the
member's block and the pool's `data/` by the pool's, so no single admin
namespace holds `CAP_DAC_OVERRIDE` over both, and a direct `mv` fails with
`EACCES` on whichever end is unmapped. The move is therefore two renames
through a harness-owned staging directory that is inner 0 in *both*
namespaces:

```
<member-root>/data/*  --(member block)-->  <state>/platform/adopt/<member>/
<state>/platform/adopt/<member>/*  --(pool block)-->  <pool-root>/data/<member>/
```

Both hops are renames on one filesystem, so a multi-gigabyte database moves
in constant time and is never copied. A run that dies between the hops
leaves files in staging; the next run drains them into the pool before
touching the member's own `data/`, which is what makes a half-finished
adoption recoverable by re-running `adopt`.

`adopt` refuses, before touching anything, if the pool process is currently
up, or if a member's target directory in the pool root already holds a
name that would collide (an operator decision, never a silent overwrite).

## `ams platform status`

`format_status` (`cli.py`) sorts pools first, each followed by its own
members (`_ordered`); everything else follows in its previous order. When any
record carries a pool key a `POOL` cell is added per row — `(N)` on a pool
record, the pool's name on a member record, `-` on anything else; a fleet
with no pools renders with **no** such column at all, so `ams platform
status` on an unpooled fleet is byte-identical to before pools existed.
**There is no header row and no `AGE` column** — every row prints
`since=<stage_since>` instead, consistent with the fleet-wide format that
predates pools. A member row not currently showing its pool (queried on its
own, e.g. `ams platform status kvservice`) gets one extra line: `pool:
pool-core (<stage>)`.

## What the user loses

1. **No independent rollback of a pooled member.** The pool is the unit —
   `rollback.py` refuses a member id outright.
2. **A redeploy of any member restarts every member of its pool** (D26's
   whole-fleet avoidance keeps working *between* pools, not *within* one).
3. **One OOM domain.** One member's memory spike kills every member of its
   pool (cgroup `memory.max` applies to the pool process, not per member).
4. **No secret isolation between members of one pool** — see the trust-domain
   statement below.
5. **Escalations are attributed to the pool id**, not the member; the member
   is named in the escalation's message text, not in `LogLine.service_id`.

## The trust domain, stated plainly

Members of one pool run in **one address space**. Any member can read
`os.environ`, walk `sys.modules`, and reach every other member's
`app.state`. **Isolation between members of a pool is not achievable and is
not attempted.** One pool is one trust domain; that is the price of the
memory saving, and it is why the pool boundary is drawn on blast radius, not
on convenience — `displayservice` (image buffers via Pillow), `llmgateway`
(unbounded streaming response buffers), `files` (uploads, a websocket route)
and `oss` (the heaviest import in the fleet, multipart uploads) stay
standalone because their memory is bounded by workload, not by code, and one
OOM in a pool kills every member of it.

What *is* still enforced, unchanged from a standalone service:

- Secret **values** never appear in argv, in `pool.json`, in a declaration or
  in any log line. They reach the process exactly as today, through
  `SecretStore.load` at spawn, for the one `decl.id` that is the pool.
- Storage: `<state>/secrets/pool-core/SVC_SECRET__KVSERVICE`, 0600,
  harness-owned, unreadable from inside the namespace (D4/D16).
- The runner's environment restore after each phase is **hygiene**, keeping
  a member from *accidentally* reading a neighbour's identity at import
  time. It is not a boundary and must not be described as one.

## Migration runbook

### Ordering constraint — read this before pushing anything

**Deploy the new ams to the target host BEFORE pushing any commit carrying
`pool = "..."` overlays to `<store>/upstream/api.git`.** The live overlay
reader on an un-upgraded host rejects the unknown `pool` key, and the 60 s
sync timer would mark every affected member `failed` on its very next tick.
Verified by the fleet dry check (`spike-pool.md`): `build_pool("core")` over
all 15 real manifests round-trips cleanly on the branch that carries the
overlay lines — the only failure mode is deploying the overlay ahead of the
ams build that understands it.

### Which services pool

A manifest joins a pool iff its `service.ams.toml` says so. Layer 0
(`registry`, `auth`) and `caddy` never pool — fixed ports, start-order root,
blast radius, and `auth` mints a module-level session secret at import.
**Recommendation: one pool, `pool-core`, with the low-risk Layer-1 Python
services** (`kvservice`, `logservice`, `messageservice`, `notificationservice`,
`emailservice`, `commentservice`, `wechatservice`, `secretsservice`,
`timeservice`, `pages`, `mailbox`, `locationservice`, `turingtest`,
`llmpricing`, `resume`); `displayservice`, `llmgateway`, `files`, `oss` stay
standalone (§ trust domain, above). `secretsservice` was failed at
measurement time and must be shown to start standalone before it is pooled.
`locationservice` and `mailbox` carry a caveat until T11 lands: their login
return-to degrades in a pool (`SVC_ENDPOINT` resolves to `""` at request
time) unless the api-side fix has already shipped.

### Steps

Run `ams platform sync --dry-run` before each state-changing step.

1. **Confirm the roster** with `ams platform status` + `ams ctl status`, and
   confirm `secretsservice` starts standalone.
2. **Back up first** (`systemctl start ams-platform-backup`), and confirm the
   R2 objects for every prospective member exist — adoption moves SQLite
   files, and a fresh snapshot is the undo.
3. **Add the overlay lines**, one `pool = "core"` per member, committed and
   pushed to `<store>/upstream/api.git` — **only after step 0's ordering
   constraint is satisfied.**
4. **Dry run**: `ams platform sync --dry-run` should report one new service
   `pool-core`, N members losing their declarations, N mount sidecars
   gaining a `port_owner`, and zero registry sidecar changes.
5. **Sync**, and let the adoption guard trip: the first real tick declares
   nothing for the pool and escalates once, naming the adopt command. This
   is the designed stop, not a failure.
6. **Adopt**: `ams platform pool adopt core`.
7. **Sync again**: pool declared → provisioned (one `uv pip install`,
   minutes on 1 vCPU with a warm cache) → reloaded → members registered
   (every `create_identity` returns 409 = success, since the rows already
   exist) → per-member health gate.
8. **Gateway re-render** happens inside the same tick; confirm
   `<state>/gateway/sites/kvservice.caddy` now points at the pool's port and
   that `caddy fmt` still round-trips.
9. **Verify externally** — every member's public URL through
   `api.lishuyu.app`, plus one M2M-token call across the pool boundary to
   prove the shared `<pool-root>/etc/jwt-rs256.pub` still signs and verifies.
10. **Measure** (below), then leave the legacy member roots in place for one
    backup cycle before deleting them by hand.

**Rollback of the migration itself is not one command.** Un-pooling means:
restore each member's `data/` from step 2's snapshot, drop the overlay
lines, sync. `ams platform pool adopt` has no inverse — state this plainly to
an operator rather than implying the migration is reversible in one step.

### Measurement table

Taken before step 3 and after step 9, same method
(`systemd-cgls`/`memory.current` per service, `free -m`, `ps` RSS), n=3
samples 60 s apart, fleet idle at both ends.

| metric | before | after | target | met |
| --- | --- | --- | --- | --- |
| Python processes | 18 (n=3) | 5 (n=3) | — | — |
| pool cgroup `memory.current` MiB | n/a | 133–134 (n=3) | ≤ 150 MiB | yes |
| — reference: T0, 6 apps serving, one loop | n/a | 66 MiB (n=3) | extrapolates to ~85–100 MiB at N=15 | under by ~30% |
| sum of Layer-1 cgroups MiB | 712 (n=3) | 275 (n=3) | ≤ 300 MiB | yes |
| all-services cgroup total MiB | 866–867 (n=3) | 464–465 (n=3) | ≤ 600 MiB | yes |
| `free -m` available | 653–657 (n=3) | 1146–1147 (n=3) | ≥ 1200 MiB | **no**, short by 54 |
| pool thread count | n/a | 27 (n=3) | < `pids_max` (288) | yes |
| p50 latency, `/time/health` via Caddy | *(no valid number — see below)* | 5.39 ms (n=20) | no regression | yes vs control |
| cold start to all-members-healthy, s | 120–150 (n=1) | **21** (n=1) | < `start_period_s` (240) | yes |

Measured on racknerd 2026-09-03, before at 07:57–07:59 and after at
08:53–08:55, with the archived script `.claude/state/evidence/ams-measure.sh`.
The script behind `evidence/pool-before-2026-09-03.txt` was not kept and its
definitions could not be restated, so the before column was re-taken; that
older file reads 40–100 MiB higher on the same unchanged fleet three hours
earlier, which is method plus reclaim drift, not a real change. Full cutover
log in `.claude/state/pool-migration.md`.

**The latency row has no valid "before".** That script sent
`Host: api.lishuyu.app`, which matches no site block in the rendered Caddyfile
(the entry site is `http://127.0.0.1:20180`), so Caddy answered an empty 200
and both the before and after numbers timed Caddy's fallthrough handler rather
than a service. The after figure above is a corrected measurement with no Host
override. The control for it is `llmgateway`, a **non-pooled** service measured
in the same minute: p50 5.82 ms against the pool members' 5.39 and 5.46 ms. A
pooled member is not slower than a standalone one on an idle fleet. This says
nothing about the shared-event-loop coupling risk under concurrent load, which
was not tested.

**Both sizing formulas are far more generous than the fleet needs.** At N=15
the pool holds 130 MiB against a 600 MiB limit (22 %) and 27 pids against 288
(9 %) — about 1.8 threads per member, not the 3 that `pids_per_member = 16`
budgets for. The runbook's instruction is to re-derive the constants only when
the measurement lands materially *above* the formula, so they are left alone
and the measurement is recorded in D29's Open section instead. T0's 66 MiB at
N=6 was, as predicted, an under-estimate: it ran with `SVC_DEV=1` and so omitted
the per-member heartbeat, M2M-refresh and CLS-forwarder threads.

**Cost the migration adds.** A sync tick now takes about 3 minutes, because
`resume` and `secretsservice` are known-dead pool members that each burn a 90 s
health gate on every tick, and the 60 s timer runs ticks back to back. The
`--only` escape hatch the sync unit's comment relies on for `oss` does not work
here: naming any member selects the whole pool, so a dead member cannot be
dropped from a tick.

**The thread count is the one T0 number that must not be carried over.** T0
ran with `SVC_DEV=1`, which disables the SDK's registry calls, so its 3–4
threads at N=6 omit the per-member heartbeat, M2M-refresh and CLS-forwarder
threads a translated declaration (which never sets `SVC_DEV`) will start.
`pids_max = 48 + 16 × N` is still an unmeasured formula; T10 is the
measurement that settles it, and both memory reference points carry the same
caveat in the other direction — no `SVC_DEV` means T0's 66 MiB is if
anything an under-estimate. If the measured pool `memory.current` lands
materially above `150M + 30M × N`, re-derive the two constants from the
measurement and record it in D29's Open section rather than quietly widening
the limit.
