# Pool-merge fact-finding: "one manifest = one process" assumptions in ams

Read-only fact-finding, confidence: **high** (all claims below are read directly
from the source files named, with file:line references; nothing here is
inferred or guessed). Scope: `src/ams/` and `src/ams/platform/` as of the
current working tree (no git in this checkout — could not resolve a commit
sha; state as read on 2026-09-03).

---

## 1. `src/ams/platform/translate.py` — `Translation`/`TranslateContext`, manifest keys

### `TranslateContext` (translate.py:128-177)
Frozen dataclass, fields: `sha`, `services_dir`, `registry_url`, `auth_url`,
`extra_secret_names: tuple[str,...] = ()`, `default_memory_max`, `memory_floor`,
`cpu_max`, `pids_max`, `start_period_s`. `__post_init__` validates `sha` is a
lowercase hex git sha, and gates `registry_url`/`auth_url` to loopback only
(`_LOOPBACK_URL_RE`, translate.py:74,148-162) — a Phase-A safety rail (PLAN-allin
risk 5), not pool-related. `root_for(service_id)` (translate.py:175-177) returns
`<services_dir>/<service_id>/root` — **one service id, one root, unconditionally**.
There is no notion of a shared root or a "member id under a pool id" anywhere in
this dataclass.

### `Translation` (translate.py:180-193)
Frozen dataclass: `id: str`, `kind: str` (`"service"|"static"`), `decl:
ServiceDecl | None`, `mount: Mapping[str, Any]`, `registry: Mapping[str, Any] |
None`, `flags: Mapping[str, Any]`. **One `Translation` per manifest, one `id`
covering the declared service, its mount, and its registry record all at once**
— the three sidecars are not independently addressable by a different id than
`decl.id`. `kind: static` produces `decl=None` (translate.py:184-186,470-477).

### Manifest keys accepted (frozensets at translate.py:80-103)
```
_TOP_KEYS      = {schema_version, kind, name, display_name, audience, owner,
                   manual_restart, deploy, process, mount, acl, registry}
_DEPLOY_KEYS   = {source, install, target_dir, user}
_SOURCE_KEYS   = {type, repo, branch, path}
_PROCESS_KEYS  = {exec, working_dir, environment, restart, restart_sec, memory_max}
_MOUNT_KEYS    = {gateway, path, subdomain, port}
_ACL_KEYS      = {action, principal, effect}
_REGISTRY_KEYS = {capabilities, health_path}
```
No `pool`, `group`, `tag`, or any grouping key exists anywhere in this set.
Unknown top-level or nested keys are rejected by `_table()` (translate.py:199-205,
`"unknown keys {unknown}; allowed: {sorted(allowed)}"`) — **a manifest author
cannot add a `pool: foo` key today; `translate()` would raise `TranslateError`**.

`name` (translate.py:425-427) must match `SERVICE_ID_RE` and becomes
`service_id` — the single identity threaded through everything downstream:
the `ServiceDecl.id`, the mount sidecar `id`, the registry sidecar `id`, the
`ServiceRecord` key in `PlatformState`, the `mounts/<id>.json` /
`registry/<id>.json` filenames, the `<state>/services/<id>/root` path, the
`<state>/secrets/<id>/` path, and the Caddy `sites/<id>.caddy` filename. **One
manifest → exactly one `id` used as the primary key by every subsystem below.**

`mount.port` (translate.py:349-386) is required for `kind: service`, but its
*value is discarded* — the translator always sets `port_name = PORT_NAME =
"main"` (translate.py:59,371-372) and the real port comes from ams's own
allocator at runtime, resolved later by `gateway.resolve_ports` against
`ports.get(service_id)` keyed by that same `service_id` (gateway.py:246-266).
So today: **one manifest → one declared port name (`main`) → one allocated
port → one `reverse_proxy 127.0.0.1:<port>` target** (gateway.py:487,503). There
is no way for a mount sidecar to name a *path prefix on an existing service's
port* — `_mount()` always produces `port_name` for a fresh `service` mount, and
`resolve_ports` looks the id up in the allocator directly, one port per id.

`env` (`_build_env`, translate.py:584-629): starts from an `injected` dict
(`SVC_NAME`, `SVC_AUDIENCE`, `PORT`, `GIT_COMMIT`, `SVC_HEALTH_PATH`,
`SVC_M2M_PUBLIC_KEY_PATH`, `REGISTRY_URL`, `AUTH_URL`, plus conditionally
`SVC_CAPABILITIES`/`SVC_DISPLAY_NAME`/`SVC_OWNER`) that is *always keyed by the
one `service_id`* — e.g. `SVC_M2M_PUBLIC_KEY_PATH = f"{root}/etc/jwt-rs256.pub"`
where `root = ctx.root_for(service_id)` (translate.py:515,607). A manifest's own
`environment` values pointing at `/var/lib/<name>` are rewritten to
`<root>/data/...` **only when `<name> == service_id`** — `_rewrite_data_path`
(translate.py:632-650) raises if the value names a *different* service's
`/var/lib/<other>` path ("points at another service's state dir"). This is a
hard per-id isolation assumption baked into env rewriting.

`ServiceDecl` construction (translate.py:539-561): `id=service_id`,
`ports={PORT_NAME: 0}` (exactly one port, name `"main"`), `runtime=RuntimeSpec(kind="uv",
python="3.12", sync=True)` (always uv-sync, never anything else — the
translator hardcodes this, translate.py:545), `health=HealthSpec(kind="http",
port=PORT_NAME, path=health_path, ...)`, `secrets=("SVC_SECRET",
*ctx.extra_secret_names)`, `depends_on=DEPENDS_ON` (`("registry",)`, fixed).
**No manifest field selects `runtime.kind`; the translator always emits `uv` +
`sync=True`** — i.e. it always expects the manifest's own directory to be a uv
project (`deploy.install` must be exactly `cd <working_rel> && uv sync`,
enforced by `_runtime_rel`, translate.py:310-332, and `_INSTALL_RE`,
translate.py:76).

`deploy.target_dir` must equal `f"/srv/{service_id}"` exactly (translate.py:441-442)
and `process.working_dir` must be `/srv/<service_id>` or a sub-path under it
(`_rel_under_srv`, translate.py:298-307) — **the working directory is
structurally tied to the one service id**, becoming `workdir = f"repo/{runtime_rel}"`
relative to the service root.

`_acl` (translate.py:389-405) produces `[{action, principal, effect}]` from the
manifest's `acl` list, folded into the `registry.json` sidecar
(`{version, id, audience, display_name, owner, capabilities, health_path,
acl}`, translate.py:564-573) — again one `id`.

### `service.ams.toml` overlay (`static.py:load_ams_overlay`, static.py:105-161)
Reads `<manifest_dir>/service.ams.toml` if present. Top keys allowed: only
`{secrets, env}` (`_OVERLAY_TOP_KEYS`, static.py:85). `secrets` is a list of
extra secret *names* (folded into `TranslateContext.extra_secret_names` before
`translate()` is called — `sync.py:801-804,842-844`); `env` is a table of extra
non-secret env values folded into the *declaration* after translation by
`sync._apply_overlay_env` (`sync.py:773-783`, uses `dataclasses.replace(decl,
env={**decl.env, **env})`). **No pool/grouping key here either** — `unknown =
set(data) - _OVERLAY_TOP_KEYS` raises `StaticError` on anything else
(static.py:130-134).

---

## 2. `src/ams/platform/sync.py` — `ServiceRecord`, the tick, keying by id

### `ServiceRecord` fields (sync.py:262-283, all)
```python
sha: str | None = None
prev_sha: str | None = None
deployed_sha: str | None = None
stage: str = FAILED               # "fetched".."healthy"|"failed"
error: str | None = None
manual_restart: bool = False
rolled_back_from: str | None = None
escalated: bool = False
updated_at: str = ""
stage_since: str = ""
```
`PlatformState.records: dict[str, ServiceRecord]` is keyed by `service_id`
(sync.py:294-336). `PlatformState.load` keeps only fields the dataclass declares
(`known = {f: row[f] for f in ServiceRecord.__dataclass_fields__ if f in row}`,
sync.py:318) — **CLAUDE.md's own rule: "a field that must survive a sync rewrite
has to be declared on `ServiceRecord`"**. Any pool-related field (e.g. "which
pool process serves this id", "pool member index") would need to be added here
explicitly or it is silently dropped on the next flush.

### Change detection (`_Run.changed_paths`/`target_sha`, sync.py:683-745)
`target_sha(manifest, service_id)` computes the sha to translate a service at:
if the service has nothing deployed, or the mirror can't diff, or the commit
range touches `own = manifest.parent.relative_to(checkout)/'` or one of
`cfg.shared_prefixes` (`("shared/", "components/sdk/")`, sync.py:120), the
service moves to head; otherwise it stays at its `deployed_sha`
(sync.py:716-745). This is **per manifest directory, one prefix check per
service** — nothing here batches multiple manifests' path-prefixes together.
A pool of N manifests would still each independently decide their own
`target_sha`, which could legitimately diverge (member A changed, member B
didn't) — a real tension with "N manifests run inside ONE process," since a
process can only be at one sha at a time.

`deployed_sha(service_id)` (sync.py:701-714) falls back to reading
`<state>/services/<id>/root/repo/.ams-sha` (or the static site's marker under
`gateway.static_root(state)/<id>`) when the record itself has none — **per-id,
one repo checkout per id assumed** (`state.service_root(service_id) / "repo"`,
sources.py `SHA_MARKER` written by `SourceMirror.stage`, sources.py:432-477).

### The tick's phases (sync.py:1230-1318, `sync()`)
1. `_phase_translate` (sync.py:796-871): one `_Pending` per selected manifest,
   `manifest.parent.name` used as the fallback id on a translate error
   (sync.py:811-817) — **id keying starts before the manifest even parses**.
2. Per pending item, `_phase_materialize` (sync.py:908-952) then
   `_phase_declare` (sync.py:955-1027) — both operate on exactly one
   `item.id`/`item.decl`/`run.state.service_root(item.id)`. `_phase_materialize`
   stages via `run.mirror.stage(item.sha, root, block)` into
   `state.service_root(item.id)` (one root per id, sync.py:914,923) and
   provisions via `provision(item.decl, root, run.store, block, ...)`
   (sync.py:939-945) — **one `uv sync` per id, into that id's own root**. No
   batching of multiple ids' provisioning into a shared venv exists anywhere in
   this phase.
3. One reload for the whole run (`_phase_reload`, sync.py:1030-1052) —
   already fleet-wide, not per-id; this part is pool-friendly as-is.
4. `_phase_gateway` (sync.py:1088-1155) — renders from *every* `mount.json`
   sidecar on disk (`load_mounts`, sync.py:1067-1085), not just this tick's
   subset — also already fleet-wide.
5. `_phase_finish`/`_phase_register`/`_phase_health` (sync.py:1158-1224) — one
   `client.create_identity(...)` + `client.upsert_acl(...)` **per id** (from
   `item.translation.registry`, sync.py:1159-1169), then one health probe per
   id at `http://127.0.0.1:{port}{item.health_path}` where `port =
   ports.get(item.id).get("main")` (sync.py:1176,1220) — **the health probe is
   keyed by the port allocated to `item.id`**; there is no notion of probing a
   path prefix on someone else's port.

### Places keyed by "service id" that would need to become "pool id + member id"
(exhaustive, from the read above)
- `TranslateContext.root_for` / `state.service_root(id)` — the repo checkout,
  venv, and JWT-key placement path (translate.py:175-177; runtime.py
  `python_venv_dir`; bootstrap.py `place_jwt_key` → `service_etc_dir`).
- `ServiceDecl.id`, `.ports`, `.health.port` — one declared process = one
  uid/cgroup/venv/port/health-probe.
- `PlatformState.records[id]` / `ServiceRecord` — one deploy-progress record.
- `mounts/<id>.json`, `registry/<id>.json` sidecar filenames and their `id`
  field (sync.py:958-967).
- `SecretStore.service_dir(id)` / `<state>/secrets/<id>/` (secrets.py:89-94).
- `PortAllocator.get(id)` / `ports.json` — one port table per id
  (gateway.py:246-266, sync.py:1215-1224).
- `RegistryClient.create_identity(service_id, ...)` / `upsert_acl(service_id,
  ...)` — one registry identity per id (registryclient.py:273-347).
- `gateway`'s `sites/<id>.caddy` filename and `reverse_proxy
  127.0.0.1:{port}` where `port = ports[service_id]` (gateway.py:508-515,
  651-658).
- `LogLine.service_id` as read from the supervisor (see §5 below) — **one
  process's stdout/stderr is attributed to exactly one id**, the id the
  process was declared and spawned under.

---

## 3. `src/ams/platform/gateway.py` — mount → site rendering

`resolve_ports` (gateway.py:246-266) resolves each **service** mount's
`port_name` (default `"main"`) against `allocator.get(service_id)` and builds
`{service_id: port}` — **the map's key and the mount's `id` are the same
string; there is no indirection that would let two different mount ids
resolve to the same port today.**

`_render_site` (gateway.py:430-505) — the actual `reverse_proxy` line:
```python
block.add(f"reverse_proxy 127.0.0.1:{_port_for(service_id, ports)}", 1)
```
(gateway.py:487 for subdomain sites, gateway.py:503 for path-mounted sites),
where `_port_for(service_id, ports)` (gateway.py:508-515) does
`port = ports.get(service_id)` — **strictly one lookup by the mount's own
`id`, sourced from `ports[service_id]`, i.e. from that same id's own port
allocation.** Nothing in `render()`/`_render_site()`/`resolve_ports()` takes a
port as an independent field of the mount sidecar — the port is *always*
looked up by the mount's `id`.

Can two mount sidecars point at the same upstream port with different path
prefixes today? **No, not without a code change.** Each mount's `id` must be
unique (`render()` raises `GatewayError(f"duplicate mount id {service_id!r}")`
at gateway.py:653-654 if two mounts share an id), and each id's target port is
always `ports.get(that same id)` — there is no field in the `mount.json`
sidecar schema (`version, id, kind, gateway, path, subdomain, port_name,
static_root, build, headers` — the full shape, translate.py:375-386) that lets
a mount say "proxy to *this other* id's port." Adding pool support to the
gateway would need either (a) a new sidecar field carrying an explicit
upstream id/port distinct from the mount's own id, or (b) `resolve_ports`
accepting an explicit port number that bypasses the `ports.get(service_id)`
lookup.

Everything else in this module (CSP/header rendering, CORS, `Caddyfile`
skeleton, `write()`'s changed-file diffing) is per-mount and orthogonal to the
one-process assumption — none of it assumes one process per port beyond the
`reverse_proxy` target line above.

---

## 4. `registryclient.py` / `bootstrap.py`

### Identity payload (`RegistryClient.create_identity`, registryclient.py:273-318)
`POST /api/services` body: `{id, audience, display_name, owner, capabilities}`
(registryclient.py:296-304). Headers: `X-Service-Secret: <secret>`,
`X-Admin-Token: <admin token>`. `id` and `audience` are each validated against
`^[a-z][a-z0-9-]*$`, ≤64 chars (`_validate_id`, registryclient.py:99-100,155-162).
`health_path` is accepted as a kwarg for symmetry with the sidecar but is
explicitly **not** sent on the wire (registryclient.py:19-23,286-288) —
it's used only locally to build the probe URL. **One identity = one `id`
string; nothing about this call structurally assumes one process**, so N
identities could in principle be created against N different `id`s that all
happen to share one backing port — the registry itself has no notion of
"process" at all, only `id`.

### ACL upsert (`upsert_acl`, registryclient.py:322-347)
`POST /api/acl` once per rule, body `{service_id, action, principal, effect}`
— straightforward, one call per rule per id, no process assumption.

### `wait_healthy` (registryclient.py:351-370)
`GET <url>` until 200 or deadline. The caller (`sync._phase_health`,
sync.py:1175-1182) builds `url = f"http://127.0.0.1:{port}{item.health_path}"`
where `port` again comes from `ports.get(item.id).get("main")`
(sync.py:1220). **The health probe is a raw TCP/HTTP hit on a port, with no
path-prefix awareness baked into `wait_healthy` itself** — so a pooled member
whose health path is actually `http://127.0.0.1:<shared-port>/<member-prefix>/health`
would work *if* `sync` composed that URL instead of the bare `{port}{health_path}`
it does today. Today it does not: `item.health_path` is joined directly to
the bare port with no prefix (sync.py:1176), which would be wrong for a
pooled member mounted at a sub-path unless `health_path` itself already
embeds that prefix (currently it's whatever `registry.health_path` says,
default `/health`, translate.py:511-513 — nothing prepends the mount path).

### `place_jwt_key` (bootstrap.py:400-449, full signature at bootstrap.py:400-409)
```python
def place_jwt_key(state: StateDir, service_id: str, block: UidBlock, *,
                   store: RuntimeStore, private: bool = False,
                   harness_uid: int | None = None, harness_gid: int | None = None) -> bool
```
Copies the JWT public (or, for `registry`/`auth` only, private) key from the
store into `service_etc_dir(state, service_id)/jwt-rs256.{pub,key}`
(bootstrap.py:430-435 — `service_etc_dir` not shown above but referenced;
it's `<root>/etc/` per the module's own docstring, bootstrap.py:26-29). **One
copy per service id, into that id's own `<root>/etc/`** — because a mapped
uid cannot read a harness-shared `/etc/auth/` (D4). If N pool members share
one process/root, they could in principle share one copy of the key (same
root), but if each member keeps its own `<state>/services/<id>/root` (per
§2's "no other field declares a shared root today"), `place_jwt_key` would
run N times, redundantly, into N different roots even though only one process
reads any of them — not broken, just wasted work, unless the pool's `root`
concept is unified.

---

## 5. `src/ams/platform/policy.py` — escalation keying, health gate, log attribution

**Central fact: `LogLine.service_id` is set by the supervisor from the
declared/spawned service id, not derived from log content.** Confirmed at
`src/ams/supervisor.py:818`:
```python
line = LogLine.from_raw(st.id, stream, raw, st.decl.logging.format)
```
where `st.id` is the one id under which that one process was declared and
spawned (one `ServiceDecl` → one spawn → one `st` record in the supervisor's
table). **If one process serves 16 logical services, every log line it
writes to stdout/stderr is attributed to that one process's single declared
id, regardless of which logical service actually emitted it** (unless the
process's own log format embeds a distinguishing field and something
downstream re-parses it — nothing in `ams` core or `ams.platform` does this
today).

Consequences for `PlatformPolicy` (policy.py):
- **Cause-key dedup** (`cause_key`, policy.py:161-168) is `f"{service_id}|{kind}|
  {normalized text}"`. With one process id for 16 logical services, two
  *different* logical services' identical-shaped errors (e.g. both hit a
  connection-refused on registry startup) collapse into **one** cause key and
  dedupe together — potentially hiding that member B failed while only
  member A's failure got escalated (or vice versa), since the escalation
  channel has no way to know they're different logical services.
- **Health gate** (`_health_gate`, policy.py:723-776) reads
  `<state>/platform/state.json` per **`ServiceRecord`** (i.e. per translated
  manifest id, from `sync.py`, not per process) — this part is fine as long as
  each pool *member* still gets its own `ServiceRecord`/`stage`/`sha` entry
  (§2 confirms `PlatformState` is keyed by whatever id `sync` assigns per
  manifest, independent of how many processes are actually running). So the
  health-gate logic itself doesn't inherently break — **but `_phase_health`
  in sync.py currently probes one URL per id at that id's own allocated port**
  (see §4), which does not exist for a pool member that shares a port with
  15 others; that call site would need to change to probe the pool's shared
  port with the member's own health path/prefix.
- **Crash-loop detection** (`_maybe_crash_loop`, policy.py:692-721) keys off
  `ServiceExited` events, which (like `LogLine`) are supervisor-level and
  keyed by `st.id` — the declared process id. A pooled process crashing kills
  all 16 logical services at once (one OS process), so `ServiceExited` is
  naturally "correct" here (it *is* one event, for one process, affecting all
  16) — but the crash-loop message names only the one process id + one sha
  pair, with no way to say "this is 16 services down," unless callers cross-
  reference the pool's member list themselves.
- **Caddy-specific rules** (`_caddy`, policy.py:621-664) match on
  `event.service_id == self.caddy_service_id` and Caddy's own JSON log
  (`logger`, `status`, `msg` fields) — orthogonal to pooling, not affected.
- **Registry heartbeat suppression** (`_is_early_heartbeat`, policy.py:666-671)
  also keys off `event.service_id` generically — with a pool, 16 logical
  services' heartbeat-refused lines would all report as the one pooled
  process's id, so the existing suppression (based on "have we seen the
  registry's own `HealthChanged`") still functions per-process, unaffected in
  kind, just coarser in which services it's suppressing for.

**What breaks, summarized:** nothing in `policy.py` raises or misbehaves
outright on a pooled process, but (a) cause-key dedup can conflate two
logical services' distinct failures into one suppressed cause, and (b) every
policy signal derived from `LogLine`/`ServiceExited`/`HealthChanged` is
*process-granular*, not *logical-service-granular* — there is no field
anywhere in `ams.events` carrying a sub-identity finer than the declared
`service_id`. Getting per-member log attribution would require either the
pooled process to prefix its own log lines with the member id (and a new
classifier/parser here to split them back out) or accept that policy-level
observability for a pool is only ever process-level.

---

## 6. `src/ams/schema.py` — `ServiceDecl`, grouping fields, reserved env

Full `ServiceDecl` field list (schema.py:155-179, all — see full dataclass in
§2's cross-reference too): `id`, `start: StartSpec`, `name`, `env`, `ports`,
`runtime: RuntimeSpec`, `health: HealthSpec`, `stop: StopSpec`, `limits:
LimitsSpec`, `restart: RestartSpec`, `logging: LoggingSpec`, `secrets:
tuple[str,...]`, `depends_on: tuple[str,...]`.

**No existing grouping/tag/pool field anywhere in `ServiceDecl` or any nested
spec.** `from_dict`/`_build` (schema.py:246-311) reject any top-level or
nested key not in the dataclass's own `fields()` — `unknown = sorted(set(data)
- allowed)` → `DeclError` (schema.py:288-291) — so a hand-added `pool = "foo"`
line in a `service.toml` would be rejected today, same as an unrecognized
manifest key in `translate.py`.

`RuntimeSpec` (schema.py:68-91): `kind: RuntimeKind = "none"` where
`RuntimeKind = Literal["none", "venv", "uv", "pnpm", "bun", "nix"]`
(schema.py:42). Fields: `python`, `node`, `requirements`, `sync: bool =
False`, `packages: tuple[str,...]`, `nix_packages: tuple[str,...]`.
uv-specific (`sync=True`): "workdir is a uv project (pyproject.toml +
uv.lock); provision runs `uv sync --frozen` there and the venv lives at
`<workdir>/.venv`" (schema.py:76-78, docstring). `validate()` enforces
`sync` only valid with `kind == "uv"` and mutually exclusive with
`requirements` (schema.py:401-405). **Nothing in `RuntimeSpec` supports
naming multiple project directories, multiple `pyproject.toml`s, or an
already-running venv to attach to** — the venv location is always a pure
function of one `ServiceDecl` (`python_venv_dir`, runtime.py:158-176):
`<workdir>/.venv` when `sync=True`, else `<service_root>/.venv`.

`ports: Mapping[str, int]` (schema.py:163) — port name → requested port, `0`
= allocate. `expand_ports()` (schema.py:468-474) substitutes `${PORT_<name>}`
tokens in env/argv strings against the allocated map — this mechanism itself
is generic (a decl *could* declare multiple named ports, e.g. `ports =
{main = 0, admin = 0}`, and `validate()` places no upper bound on how many
port names a decl may have — schema.py:348-354 just validates each entry
individually). **So the port-naming mechanism is not inherently one-port-only
at the schema level** — `translate.py` is what narrows every translated
manifest to exactly one port named `"main"` (translate.py:59,544). A pool
declaration authored directly in `service.toml` (bypassing `translate.py`)
could legally declare one port and have 16 `${PORT_main}` references baked
into 16 different env-var names if a future translator chose to do that.

`secrets: tuple[str,...]` (schema.py:172, `validate` at schema.py:356-365) —
flat list of env var names the harness injects at spawn from the
`SecretStore`; `validate()` requires each name be a valid env-var name, not
reserved, not already in `env`, and not duplicated. **No namespacing by
member** — if 16 pool members each need their own `SVC_SECRET`, they'd need
16 distinctly-named secret env vars in one flat list (e.g.
`SVC_SECRET_COMMENTSERVICE`, `SVC_SECRET_DISPLAYSERVICE`, ...) since there is
no per-member secret scoping mechanism.

`LimitsSpec` defaults (schema.py:115-131): `memory_max: str | None = None`,
`cpu_max: str | None = None`, `pids_max: int | None = None` — **all default to
`None`** (no limit) at the schema level; `translate.py`'s
`DEFAULT_MEMORY_MAX = "150M"` / `MEMORY_FLOOR = "120M"` / `cpu_max: str =
"40%"` / `pids_max: int = 64` (translate.py:60-62, `TranslateContext` fields
translate.py:144-145) are translator-level defaults applied per translated
manifest, not schema defaults.

`LoggingSpec.format: LogFormat = "auto"` (schema.py:134-144) — `LogFormat` is
imported from `ams.events` (schema.py:30); one format hint per declared
process (Caddy uses `"json"` explicitly, gateway.py caddy_declaration
`[logging] format = "json"`, gateway.py:802-806) — **one log-format hint per
process, consistent with "one process, potentially many logical services,"
since the classifier operates on the process's raw stdout/stderr stream as a
whole**, not per logical service.

`RESERVED_ENV = frozenset({"PATH", "HOME", "LANG", "PYTHONUNBUFFERED",
"VIRTUAL_ENV"})`; `RESERVED_ENV_PREFIXES = ("AMS_", "PORT_", "UV_", "PNPM_",
"BUN_", "npm_config_")` (schema.py:37-38). `SpawnRequest.env()`
(spawn.py:61-79) additionally always injects `AMS_SERVICE_ID` (=
`decl.id`) and `AMS_DATA_DIR` (= `<root>/data`) — **both singular, one value
per process**, so a pooled process sees one `AMS_SERVICE_ID` (presumably the
pool's own declared id, not any member's) and one `AMS_DATA_DIR` unless the
translator/declaration is changed to pass the member list some other way
(e.g. a new env var enumerating member ids + their own data subdirs).

---

## 7. `src/ams/secrets.py` / `src/ams/spawn.py` / `src/ams/state.py`

### `make_extra_env_for` (secrets.py:248-271)
```python
def lookup(decl: ServiceDecl) -> tuple[dict[str, str], tuple[str, ...]]:
    env, path_prepend = runtime_env_for(decl) if runtime_env_for is not None else ({}, ())
    if decl.secrets:
        env = {**env, **store.load(decl.id, decl.secrets)}
    return env, path_prepend
```
**One `decl.id` → `SecretStore.load(decl.id, decl.secrets)`** — one service id
maps to exactly one secret directory (`<state>/secrets/<id>/`, secrets.py:89-93).
For a pooled process there is exactly one `decl.id` (the pool's own id) at
spawn time, so **all pool members' secrets would have to live under that one
pool id's secret directory** (e.g. `<state>/secrets/<pool-id>/SVC_SECRET_MEMBER1`,
`SVC_SECRET_MEMBER2`, ...) unless `make_extra_env_for` itself is extended to
also read from each member's own former secret directory and merge — nothing
here does that today.

### `SpawnRequest` (spawn.py:41-79) — what the harness injects
`decl`, `root: Path` (service root), `ports: Mapping[str,int]` (allocated port
per declared name), `extra_env: Mapping[str,str]` (runtime activation etc,
from `extra_env_for`), `path_prepend: tuple[str,...]`. `.env()` builds:
`PATH`, `HOME=<root>`, `LANG`, `PYTHONUNBUFFERED`, `AMS_SERVICE_ID=decl.id`,
`AMS_DATA_DIR=<root>/data`, then `PORT_<name>=<port>` for each allocated
port, then `extra_env`, then finally the declaration's own `env` entries
(which win over everything, per the ordering at spawn.py:74-78 — declared env
is applied last). **Confirms**: one root, one data dir, one set of
`PORT_<name>` variables sourced from the *one* declaration's `ports` mapping
— all singular per spawned process, matching §6's schema-level observation
that multiplicity would have to come from a `ServiceDecl` authored to
describe N members explicitly (e.g. many port names, many env vars), not from
any existing per-process/per-member split.

### `StateDir` layout (state.py:59-168)
```
<state>/services/<id>/service.toml
<state>/services/<id>/root                    (HOME/workdir parent, isolation-layer owned)
<state>/services/<id>/runtime                 (service_runtime_dir, currently unused by grep hits above but declared)
<state>/state/uidmap.json, ports.json
<state>/logs/
```
`service_root(id)` = `service_dir(id)/root` (state.py:75-78) — **one root
directory per declared service id, hard-coded relationship, no indirection**.
`list_service_ids()` (state.py:127-142) enumerates `services/*/service.toml`
directly by directory name = id; `load_declarations()` (state.py:148-158)
loads one `ServiceDecl` per id, skipping (logging) any that fail to parse.
**A pool process would appear here as exactly one entry — one `service.toml`,
one root — regardless of how many logical services it hosts**, unless the
pool's member manifests each get their own `services/<member-id>/` directory
that is *not* spawned as its own process (i.e., a declaration-less presence
just for secrets/data-dir/JWT-key placement) — nothing in `StateDir` assumes
or prevents that; it's purely a filesystem convention layer.

---

## 8. `src/ams/runtime.py` `provision()` for kind uv

Command and directory (from `_provision_uv_sync`, runtime.py:378-428, called
via `_provision_python` when `rt.sync` is true, runtime.py:452-455): runs
```
uv sync --frozen        # or plain `uv sync` if no uv.lock, with a WARNING
```
**in `workdir`** (= `service_workdir(decl, service_root)`, i.e. `<service_root>/<decl.start.workdir>`,
runtime.py:143-151), as inner root via `_tool`/`_admin`/`run_admin` inside the
admin user namespace (runtime.py:267-328), with `env = provisioning_env(store)`
(runtime.py:208-241) — a fixed dict of `PATH/HOME/LANG/CI/UV_CACHE_DIR/
UV_PYTHON_INSTALL_DIR/UV_LINK_MODE=clone/UV_PYTHON_PREFERENCE=only-managed/
PNPM_HOME/...` (all pointing at the shared `RuntimeStore` caches, not at
anything service-specific). **One `uv sync` call, one `pyproject.toml`, one
resulting venv at `<workdir>/.venv`** (confirmed by `python_venv_dir`,
runtime.py:158-176, which returns exactly that path when `rt.sync`).

Could it install several projects into one venv? **Not via the existing
`_provision_uv_sync` path** — it requires exactly one `pyproject.toml` in
`workdir` (raises `ProvisionError` if absent, runtime.py:405-409) and runs
plain `uv sync --frozen` with no `--all-packages`/workspace-member selection
logic, no `-e` flags for multiple projects, and no loop over a list of
projects anywhere in this module. The non-sync Python path (`_provision_python`
else-branch, runtime.py:457-497, used when `runtime.sync=False`) *does*
support multiple `-e`/package specs via `rt.packages` (runtime.py:467,
`install += list(rt.packages)`, then one `uv pip install ... *install` call,
runtime.py:488-496) — **but this is the non-uv-project mode, which
`translate.py` never emits** (it always sets `sync=True`, translate.py:545).
So: a pool merge that wants "16 uv projects installed into one venv" would
need either (a) a new provisioning mode this module does not have today (a
uv workspace at the monorepo root, or repeated `uv pip install -e <path>`
calls folded into one venv), or (b) each pool manifest to keep being
provisioned into its own separate venv while only the *spawn* step is merged
into one process — i.e. pooling could plausibly be a spawn-time /
declaration-time concern only, leaving `provision()` untouched per manifest
(N separate `uv sync` calls into N separate `<workdir>/.venv`s, then one new
kind of process spawned that imports/mounts all N). This module offers no
evidence either way about which the planner should choose — it is a design
decision the fact-finding scope does not cover.

---

## 9. `src/ams/platform/backup.py` `discover`

```python
def discover(state: StateDir, allocator, *, only=None, run_admin_fn=run_admin,
             timeout_s=DISCOVER_TIMEOUT_S) -> list[Target]:
    ...
    for service_id in state.list_service_ids():
        ...
        data_dir = state.service_root(service_id) / DATA_DIRNAME
        if not data_dir.is_dir(): continue
        block = allocator.get(service_id)
        if block is None: continue
        result = run_admin_fn(_find_argv(data_dir), block, timeout_s=timeout_s)
        ...
        for path in _parse_find_output(...): targets.append(Target(service_id, path, block))
```
(backup.py:251-300, quoted above in full). **Iterates `state.list_service_ids()`
— i.e. one entry per `services/<id>/service.toml` directory** — and for each,
looks under that id's own `<root>/data` for `*.db`/etc files (`DB_SUFFIXES`,
not shown here but referenced at backup.py:305-309 via `_find_argv`).

**Would a pool with 16 data dirs be discovered?** Only if each of the 16
logical services still has its own `services/<member-id>/service.toml` (even
a "shadow" one with no live process) *and* its own `<root>/data` directory
distinct from the pool process's root. If instead the pool collapses to one
`services/<pool-id>/` directory with one shared `root/data`, `discover()`
would find **exactly one** data dir under the pool id and back up whatever
`*.db` files live in it (which could itself be fine if the pool process
namespaces its own subdirectories under that one `data/`, e.g.
`data/<member>/*.db` — `_find_argv` uses `-maxdepth 2`, backup.py:304, so a
one-level-deeper file would still be found by the `find` call as long as it's
within 2 levels of `data_dir` and has a matching suffix). **This module places
no other constraint** — it is purely a `list_service_ids()` + per-id `find`
walk, so its behavior with pooling is entirely a function of whichever
directory-layout decision the pool design makes (one root per member vs. one
shared root), not of anything hardcoded in `backup.py` itself.

---

## 10. `src/ams/platform/rollback.py` — keyed by service id

`ams platform rollback <id> [--to SHA]` (rollback.py module docstring,
rollback.py:14-16) is explicitly **"One service, one commit"** and explicitly
scoped: *"Other services — one id in, one id out. No fleet reload of anything
else."* (rollback.py:36-37, from the module docstring's "What a rollback does
NOT touch" list). It re-stages the tree, re-provisions, re-translates the
manifest **at the target commit**, rewrites the declaration + both sidecars,
restarts, and health-gates — all keyed by the one `id` argument
(`RollbackError`/`_Allocator` protocol at rollback.py:97-102; imports reuse
`sync.py`'s `mounts_dir`, `registry_dir`, `state_path`, `uid_allocator`,
`SVC_SECRET_NAME`, confirming it operates on the exact same per-id sidecar
files `sync.py` writes).

**What a pool member rollback would mean**: today, rolling back one manifest
means restarting *its own process* at the old commit while every other
service is untouched. With a pool, rolling back one member's manifest to an
older sha would either (a) require rebuilding and restarting the **entire
pooled process** at a commit where that one member's code is old but the
other 15 members' manifests point at the pool process's *current* commit —
an inherent conflict, since one OS process can only run one build of the
shared uvicorn app at a time — or (b) rollback would need to become a
pool-aware operation that rewrites just one member's mount inside the pool's
app registration and restarts the *whole* pool process, explicitly breaking
the module's own stated guarantee ("No fleet reload of anything else") for
every other member sharing that pool. This module gives no mechanism for
partial-process rollback; it assumes one process per id throughout (its own
`--to` targets one commit for one restart of one declared process).

---

## 11. Tests

### Golden files under `tests/golden/platform/` (names only, 65 entries total: 21 services × ~3 files each + 21 manifests + `manifests/` dir)
Per-service triples `<id>.mount.json`, `<id>.registry.json`, `<id>.toml`
(21 services: commentservice, displayservice, emailservice, files,
kvservice, llmgateway, llmpricing, locationservice, logservice, mailbox,
messageservice, notificationservice, oss, pages, resume, secretsservice,
timeservice, turingtest, wechatservice — plus `files-web.mount.json` and
`llm-web.mount.json` for the two `kind: static` sites, which have no
`.registry.json`/`.toml` since static manifests produce no `ServiceDecl`).
Subdirectory `tests/golden/platform/manifests/` holds 21 raw `.yaml` manifest
fixtures (one file listed and confirmed: `llmgateway.yaml`; the directory
listing above shows all 21 by service name).

### Golden files under `tests/golden/gateway/` (names only)
`logdir`, `path`, `static`, `subdomain`, `tls` — five golden-config
directories/files exercising the Caddyfile renderer's cases.

### Test modules covering translate/sync/gateway/registry (names only)
- `tests/test_platform_translate.py` — portable, translate.py's unit tests.
- `tests/test_platform_sync.py` — portable, sync.py's unit tests.
- `tests/test_platform_gateway.py` — portable, gateway.py's unit tests.
- `tests/test_platform_registryclient.py` — portable, registryclient.py's
  unit tests.
- `tests/linux/test_platform_gateway_live.py` — Linux-only, live gateway test
  (the only "live" test module found matching these four areas; `sync`/
  `translate`/`registryclient` appear to have no separate `_live.py`
  counterpart under `tests/linux/` — not verified exhaustively beyond a
  filename grep, so treat this one point as **lower confidence** than the
  rest of this document, n=1 grep pass).

These are the modules/goldens a planner adding pool support would need to
extend: golden fixtures per pooled manifest set (new `<pool-id>.pool.json`?
sidecar shape TBD by the planner), new translate-level tests for however a
`pool:` key gets accepted, new sync-level tests for the multi-manifest →
one-process staging/provisioning/health-probe path, and new gateway tests for
however path-prefix-to-shared-port resolution ends up being expressed.

---

## Open items / not verified (explicitly flagged, per this repo's own rule
against confident unverified claims)

- **Not a git repo** in this checkout (`Is a git repository: No` in the
  environment banner), so no commit sha could be cited; all line numbers are
  against the working tree as read.
- `service_etc_dir` (referenced by `place_jwt_key`, bootstrap.py:433) was not
  read directly — its definition presumably lives in `bootstrap.py` above the
  excerpted region or in `ams.state`; treated as `<root>/etc/` per the
  module's own docstring (bootstrap.py:26-29), not independently confirmed
  against its source line.
- `DB_SUFFIXES` (backup.py, referenced at backup.py:305,328) was not read
  directly — its value (which file extensions count as "a database") was not
  confirmed, only its usage pattern.
- Whether `tests/linux/` has live counterparts for `sync`/`translate`/
  `registryclient` beyond the one `gateway` live test found: **checked by one
  `find -iname` grep only (n=1 method)**; a planner should re-grep before
  relying on "gateway is the only one with a live test."
- No design proposal is offered anywhere above, per the task's read-only
  scope; every claim is either a direct quote/line-reference or an explicit
  inference labeled as such (the `provision()` "could it serve several
  projects" analysis in §8, and the rollback conflict analysis in §10, are
  the two places this document reasons beyond a literal quote — both labeled
  as such).
