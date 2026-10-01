# PLAN-pool — N manifests, one process, distinguished by tag

Request (user, verbatim): 「优化内存占用，一大堆服务可以合并，只通过 tag 区分。」

Target: cut fleet RSS by running most Layer-1 FastAPI services of the `api`
monorepo inside **one** ams-declared process, with each logical service still
distinguishable, routable, registrable and separately health-gated.

Ground truth used: `docs/design/history/pool-facts-ams.md`, `docs/design/history/pool-facts-api.md`,
`CLAUDE.md`, `DECISIONS.md` D26/D27, `docs/platform.md`,
`docs/manifest-translation.md`, `docs/platform-sidecars.md`, plus the targeted
source reads cited inline. Every claim below is either a file:line read or is
marked as an inference with its confidence.

## Live evidence, racknerd, 2026-09-03 — what is settled

**T0 is DONE and PASSED.** Full report: `docs/design/history/spike-pool.md`; scripts
archived at `docs/design/history/evidence/{pool_spike2.py, pool_probe.py, envsurvey.py}`.
Setup: one scratch venv (`/home/harness/store/scratch-pool`, uv, reflink cache,
python 3.12.14, **uvicorn 0.52.4**) with all 15 Layer-1 projects + `sdk`
installed editable together, resolved with no conflicts.

| what | result | what it settles |
|---|---|---|
| **B's topology**: 6 members, 6 `uvicorn.Server`s on ports 21000–21005 in one event loop (`pool_spike2.py`, **n=3 identical runs**) | every `/health` 200, every `/openapi.json` its own title, **RSS 66 MiB**, 3–4 threads, SIGTERM → all stopped in 0.63–0.73 s, 0 failed | **the chosen design serves, and stops.** T0's remaining item is closed |
| 15 member projects editable into one venv | resolved, no conflicts | assumption 1 of §2's "what B assumes" |
| sequential env-swap around `build_app()` | works — but is **not sufficient** | see finding 1 below: the swap must also wrap lifespan |
| A's topology (`pool_probe.py`, 4 members, one port, Starlette `Mount`) | 200 on health/openapi/docs; correct `root_path` whether or not the member sets it; RSS 78 MiB | the fallback is viable too |
| `load_from_env()` without `SVC_AUDIENCE` | `KeyError` | `SVC_AUDIENCE` is **required**, not metadata |
| `SVC_DEV=1` still ran an ACL refresh against the default `REGISTRY_URL` | observed (401, harmless there) | `REGISTRY_URL`/`AUTH_URL` are **mandatory** per member |

### Three findings that changed the runner contract

1. **`load_from_env()` is not one-shot at `build_app()` time.** An AST survey
   over all 19 services + the SDK (`envsurvey.py`, **n=19, static**) found
   `timeservice`, `llmpricing`, `resume`, `displayservice` and `test-service`
   calling it **again inside their lifespan**. The env swap must therefore wrap
   `build_app()`, lifespan **startup** and lifespan **shutdown**. §4.2 is
   rewritten around the uvicorn split the spike verified.
2. **Request-time env reads exist, but under service-specific names**
   (`MESSAGE_INGEST_TOKEN`, `NOTIFY_EMAIL_*`, `LOCATION_*`, `MAILBOX_*`,
   `DEEPSEEK_API_KEY`, `RESEND_API_KEY`, `BOT_LLM_*`). So the process env can
   safely hold the **union** of all members' non-identity keys, and only the
   identity keys are swapped per phase. One identity key *is* read at request
   time — `SVC_ENDPOINT`, in `sdk/ui.py:266 login_redirect` and
   `mailbox/main.py:249 _sso_redirect` — which resolves to `""` in a pool. That
   is T11.
3. **A uvicorn startup failure calls `sys.exit(3)` inside the task**, and in one
   event loop that `SystemExit` propagates out of `asyncio.run` and kills every
   member. The runner must catch it per member. §4.5 and T2 cover it.

Also observed, an artefact rather than a finding, but it names a real hazard:
`pages` failed startup only because `SVC_DEV=1` disables M2M and its lifespan
calls `oss` — and that synchronous `urlopen` **on the loop thread blocked every
other member's server**. See §10 risk 7.

---

## 0. The one-line answer

**The tag already exists: it is the service's own `name` / `SVC_NAME` / ams id.**
Nothing new needs inventing to *distinguish* members. The only new thing is a
*grouping* key saying which process a manifest runs in, and it goes in
`service.ams.toml` (the ams-only overlay), not in `service.yaml`.

**Chosen shape (Alternative B): one declaration → one process → one venv → N
`uvicorn.Server` instances on N ports in one asyncio event loop.** Each member
keeps its own allocated port, so the gateway, the registry payload, the health
path and the per-member health gate stay structurally identical to today. Only
*who owns the port* changes, and that is one new optional field in `mount.json`.

---

## 1. Frozen-assumption audit

| # | Frozen assumption | Where it lives | Verdict |
|---|---|---|---|
| 1 | one manifest = one `ServiceDecl` = one OS process | `translate.Translation` (translate.py:180-193), `sync._Pending` (sync.py:568) | **replace** — N manifests fan into one pool `ServiceDecl`; members keep their `Translation` for the sidecars only |
| 2 | one id = one port, named `main` | `translate.PORT_NAME` (translate.py:59), `gateway.resolve_ports` (gateway.py:246-266) | **keep the 1:1 id↔port**; relax only *which allocator row* holds it (`port_owner`) |
| 3 | one route = one upstream process | `gateway._port_for` (gateway.py:508-515) | **keep** — B never multiplexes routes onto one port |
| 4 | one project = one venv, `uv sync --frozen` | `translate` hardcodes `sync=True` (translate.py:545); `runtime._provision_uv_sync` (runtime.py:378-428) | **replace for pools** — `kind="uv", sync=false, packages=["-e", "<rel>", …]`, one venv at `<pool-root>/.venv` via the existing non-sync path (runtime.py:462-497, which already accepts arbitrary `packages` argv entries; `validate()` constrains `packages` only by `kind`, schema.py:383-388) |
| 5 | one process env = one service's identity | `SpawnRequest.env()` (spawn.py:61-79) | **split** — the ~12 **identity** keys move into `<pool-root>/pool.json` and are swapped per member around build, lifespan startup and lifespan shutdown; every **non-identity** key stays in the process env as a union across members (T0 findings 1–2, §4.2). `load_from_env()` is *not* one-shot: 5 of 19 services re-read it in their lifespan |
| 6 | one log identity per process (`LogLine.service_id = st.id`, supervisor.py:818) | `events.py`, `policy.cause_key` (policy.py:161-168) | **keep the field; add the tag inside the message text.** The runner's root formatter emits `LEVEL [<member>] <logger>: <msg>`. Dedup stays correct because `cause_key` hashes the normalized text and the bracketed tag survives normalization (**verify in T7**) |
| 7 | one `uv.lock` per project, reproducible install | per-service `uv.lock` (facts-api §5, n=21/21) | **relax, knowingly** — a pool resolves at provision time with no lock. Named as an Open item; `uv pip compile` into a pool lock is the Phase-B fix |
| 8 | one root, one `data/`, one secret dir per id | `state.service_root` (state.py:75-78), `secrets.SecretStore.service_dir` (secrets.py:89-94) | **replace** — pool root owns `data/<member>/` and `secrets/<pool>/NAME__MEMBER`. Moving existing data is an explicit operator command, never a sync side effect |
| 9 | `FastAPI(root_path=…)` composes with `app.mount()` | was unverified (facts-api §4); **now verified by the probe**, n=1 | **moot for B** — B never mounts a sub-app. Kept in the table because it is the mechanism the fallback (Alternative A) rests on |
| 10 | one JWT key copy per service root | `bootstrap.place_jwt_key` (bootstrap.py:400-449) | **keep the function; call it once per pool** — 15 redundant copies collapse to 1 |
| 11 | rollback is "one id in, one id out" | rollback.py:14-16,36-37 | **replace** — a pool is the rollback unit; a member id is refused with a message naming the pool |
| 12 | change detection is per manifest directory | `_Run.target_sha` (sync.py:716-745), D26 | **relax** — a pool is at one sha; the pool moves to head iff **any** member is affected |
| 13 | secrets are isolated per service | D16 | **relax, explicitly** — see §6. Members of a pool share an address space, so they share a trust domain. Not fixable, only stateable |
| 14 | the per-process ~45 MiB FastAPI floor is irreducible | measurement, not code | **keep** — see Alternative E, which quantifies why attacking it buys ~2 MiB |

---

## 2. Alternatives

Measured baseline (racknerd, n=1 each, import-time only, no traffic):
16 Python uvicorn processes at 44–74 MiB; `import fastapi` alone = 44 MiB;
14 member packages imported into **one** interpreter = 59 MiB total, marginal
≈1 MiB/app.

### A. One uvicorn, one port, N `build_app()` results mounted under N prefixes

**Now demonstrated to work** (probe above): four apps under four `Mount()`s on
one port, correct `root_path` for all of them, `/docs` and `/openapi.json`
correct, regardless of whether the member sets `root_path` itself.

- **ams changes**: `translate` (pool decl + every member mount pointing at the
  pool's single port), `gateway.resolve_ports` **and** `_render_site` (a
  member's `handle_path /kv/*` strips the prefix today and must stop stripping,
  inverting the current rule), `sync._phase_health` (probe
  `http://127.0.0.1:<shared>/kv/health` — `health_path` must be prefixed),
  `registryclient` unchanged.
- **api changes**: none.
- **Memory**: same as B, within noise. Both hold N apps in one interpreter; A
  saves N-1 listening sockets and N-1 `uvicorn.Server` objects, which is not a
  measurable fraction of 78 MiB.
- **What still breaks, after the probe**:
  1. **The five subdomain-mounted members have no path prefix to mount under**
     (`pages`, `turingtest`, `locationservice`, `mailbox`, `displayservice`).
     Giving them one via a Caddy `rewrite` makes their `root_path` `/pages`
     while their public URL is `pages.lishuyu.app/` with no prefix — so the
     very `openapi.json` `servers` value the probe confirmed is *correct* for a
     path member is *wrong* for a subdomain member, and `/docs` breaks.
     A Starlette `Host("pages.lishuyu.app", app=…)` route avoids it, but that
     is a second, untested routing mode inside the runner.
  2. **The prefix-stripping inversion touches every pooled member's Caddy
     block**, and its failure modes (double prefix, missing prefix) are the
     kind that pass a health check and break one route.
  3. **The two websocket routes** (`files:/p2p/ws`, `turingtest:/ws`) sit behind
     a Starlette `Mount` whose websocket-scope rewriting the probe did not
     exercise (it tested three HTTP paths only).
  4. **Leaving a pool becomes a routing migration.** See the decisive argument
     below.
- **Test cost**: every pooled member's gateway golden changes, plus prefix and
  health-composition tests, plus a websocket test.
- **Verdict: still rejected, on different grounds than before the probe.** The
  composition risk is gone; the routing-coupling cost is not.

### B. One process, one venv, N `uvicorn.Server` instances on N ports, one loop — **CHOSEN**

- **ams changes** (all listed with file paths in §5): `schema.PORT_NAME_RE`
  widened; `translate.build_pool` added; `mount.json` gains optional
  `port_owner`; `gateway.resolve_ports` honours it; `sync` groups members and
  drives one pool item; `backup.Target` gains a `label`; `policy` names members
  on a pool crash; `rollback` refuses a member id; new `platform/pool.py` for
  `adopt`; a `pool_runner.py` asset ams *places* into the pool root.
- **api changes**: **none.** No SDK change, no manifest change, no
  `pyproject.toml` change. The only api-side artefact is an optional
  `service.ams.toml` line per member, which the legacy deployer never reads.
- **Memory expectation**: 12 pooled members ≈ 60–80 MiB total vs
  12 × ~50 MiB ≈ 600 MiB today → **saving ≈500–540 MiB** (inference from the
  n=1 import measurement; no traffic, no per-app caches exercised —
  **treat as a hypothesis until T10 measures it**).
- **Breaks / costs**: one cgroup, so one member's memory spike OOM-kills all
  (mitigated by the exclusion list in §7); one restart blast radius; no
  independent member rollback; no per-member secret isolation; N daemon threads
  per member from the SDK (facts-api §6/§7) so `pids_max` must be raised.
- **Test cost**: moderate. Member mount/registry goldens stay byte-identical
  except the two new optional mount keys; the pool adds one new golden triple.

### C. Pooling as a first-class "multi-service app" inside the api SDK

- **ams changes**: none — ams sees one manifest.
- **api changes**: large. The SDK would own member discovery, per-member env,
  per-member registration, per-member routing and per-member health — i.e. all
  of `translate` + `sync` + `gateway` reimplemented in a library, and a new
  hand-written `service.yaml` for the pool that the legacy deployer must also
  be able to run.
- **Breaks**: members lose their own `service.yaml` identity, so per-member
  change detection (D26), per-member registry sidecars and per-member Caddy
  routes must all be re-derived from somewhere else. The pool composition
  becomes an api commit, so changing which services share a process needs a
  deploy of the api repo rather than an operator edit.
- **Verdict: rejected.** It puts a fleet-topology decision inside a library
  that Layer 0 also depends on, and it is the one option that makes the legacy
  deployer's job harder rather than leaving it untouched.

### D. Keep N processes; share memory by preforking a parent that imports FastAPI once (copy-on-write)

Relaxes a different assumption: that each member must *import* its own copy.

- **Why it does not work here, concretely**:
  1. **Per-service venvs make the shared import impossible without first
     solving the same one-venv problem B solves.** A parent can only pre-import
     FastAPI from *one* `sys.path`; members on separate venvs would each
     re-import. So D needs B's merged venv as a prerequisite and is strictly
     more work.
  2. **CoW decays fast in CPython.** Reference counts live in the object header,
     so touching a shared object dirties its page. The 44 MiB `import fastapi`
     footprint is overwhelmingly heap objects, not read-only code pages.
     Optimistic saving ≈30–50% of 45 MiB per member; B saves ≈95%.
  3. **It collides with two of this repo's hard rules.** `run_admin` /
     `fork_in_userns` is a bare `os.fork()` and **nothing may add a thread to a
     process that forks into a user namespace** (CLAUDE.md). The SDK starts a
     daemon heartbeat thread per `Registry` instance (facts-api §7), so a parent
     that has imported and built anything is already unsafe to fork.
  4. **Each member needs its own uid block and cgroup** to stay isolated; a
     fork from one parent lands all children in the parent's namespace, so the
     isolation D4/D8 buys would have to be given up to get the sharing.
- **Verdict: rejected on numbers and on two hard rules.**

### E. Do not pool; attack the ~45 MiB per-process floor itself

Relaxes the assumption that the floor is irreducible. Options: drop
`uvicorn[standard]` (httptools/uvloop/watchfiles), `python -OO`, trim SDK
imports, `-X frozen_modules=on`.

- **Quantified rejection from the existing n=1 measurement**: `import fastapi`
  alone is 44 MiB of the ~46 MiB reached by `fastapi,uvicorn,pydantic,httpx`.
  Everything E can remove lives in the ~2 MiB delta. Ceiling ≈2 MiB × 16 ≈
  32 MiB, versus ≈500 MiB for B.
- **Verdict: rejected as a substitute; harmless as a later independent tweak.**

### F. (complement, not an alternative) zram / swap on the box

Hides the footprint rather than removing it, and D12 pins
`memory.swap.max = 0` per service on purpose. One line here so it is on the
record as considered, not as a proposal.

### The decisive argument between A and B: the pool boundary must be cheap to move

This plan's escape hatch appears three times — a dependency conflict, a memory
spike, a member that must not be interrupted. In every case the remedy is
**"that member leaves the pool."** So the cost of moving a member across the
boundary is the property to optimise, and it is where A and B genuinely differ:

| moving one member in or out of a pool | B (N ports) | A (one port, N prefixes) |
|---|---|---|
| its Caddy site block | **byte-identical** — only the port number in `reverse_proxy` changes | flips between `handle_path` (strip) and `handle` (no strip) |
| its `root_path` semantics | unchanged | changes: prefixed inside the pool, bare outside |
| its `registry.json` | unchanged | unchanged |
| its health probe URL | `<own port>/health` either way | `<shared>/kv/health` inside, `<own>/health` outside |
| operator action | edit one overlay line, sync | edit one overlay line, sync, **and re-verify routing** |

Under B, pool membership is a pure deployment attribute with no routing
consequence. Under A it is a routing migration every time. A design whose own
safety valve is "leave the pool" should not make leaving expensive.

Secondary, same direction: B keeps per-member connection backpressure on
separate `uvicorn.Server` objects, and B's failure to bind a member's port is a
localised, observable event, where A's failure surfaces as a 404 on a prefix.

**A remains the documented fallback.** Switch to it if T0's remaining item fails
— i.e. if N `uvicorn.Server` instances in one event loop turn out not to work —
since the probe has already shown A's serving topology does. If that happens,
adopt A *with* Starlette `Host()` routes for the five subdomain members rather
than a Caddy rewrite, and add a websocket test for `files` and `turingtest`
before pooling either (both are on the standalone list anyway).

### Why B beats each rejected one, in one line each

- **over A**: same memory. The composition risk is gone, so the argument now
  rests entirely on **reversibility**, below.
- **over C**: keeps deployment topology in the runtime that owns deployment,
  and requires zero api commits.
- **over D**: ~95% vs ~30–50% of the floor recovered, and D violates the
  no-thread-before-fork and per-service-uid rules.
- **over E**: ~500 MiB vs ~32 MiB.

### What B assumes, and when it falls over

1. **Assumes** the 19 member projects resolve into one venv. Facts-api §5 read
   all 21 `pyproject.toml` files and found zero conflicting specifiers; a
   scratch venv installed the 15 Layer-1 projects editable together with no
   conflict, and the probe then imported and served 4 of them from one such
   venv (n=1 each). **Falls over** the day two members need incompatible pins —
   at which point that member leaves the pool by deleting one overlay line,
   which is the escape hatch B is designed around.
2. **Assumes** one member's memory or CPU misbehaviour is bounded. **Falls
   over** on an OOM: `memory.max` kills the whole pool. Mitigation is the
   exclusion list (§7), not a mechanism.
3. **Assumes** members tolerate sharing a process. Verified for the axes that
   matter: no `signal.signal`, no `sys.exit`, no module-level mutable SDK
   state, `ContextVar` not globals, per-instance heartbeat threads, distinct
   package names, distinct DB paths (facts-api §1/§6/§7, exhaustive).
   `auth` is the one known counter-example (module-level `_session_secret`) and
   it stays Layer 0, out of every pool.
4. **Assumes** a restart of all members is acceptable when any member deploys.
   **Falls over** for a member that must not be interrupted; that member leaves
   the pool.

---

## 3. Tag semantics

### 3.1 Where the key lives — settled by a read, not a preference

`api/components/deployer/schemas/service.schema.json:7` is
`"additionalProperties": false` at the manifest root, and
`deployer.manifest.parse_manifest` validates against it before building the
dataclass. **A `pool:` key added to `service.yaml` therefore hard-fails the
legacy deployer.** The brief's constraint ("must stay valid for the old
deployer") decides the question:

| candidate | verdict |
|---|---|
| new `pool:` key in `service.yaml` | **rejected** — breaks the legacy deployer's schema validation, and blurs the D7 line (a manifest describes the service, not the harness's process topology) |
| `service.ams.toml` overlay | **chosen** — already the ams-only channel, already read by sync before `translate()` (sync.py:801-804,842-844), already validated with reject-rather-than-guess (static.py:105-161) |

### 3.2 The key

```toml
# api/services/kvservice/service.ams.toml
pool = "core"
secrets = ["DEEPSEEK_API_KEY"]   # existing keys unchanged
```

| aspect | rule |
|---|---|
| key | `pool`, top-level, optional, string |
| allowed values | `ams.schema.SERVICE_ID_RE` = `^[a-z][a-z0-9-]{0,31}$` |
| pool service id | `pool-<value>`, e.g. `pool-core`. The prefix keeps the pool from ever colliding with a member id and makes it obvious in `ams ctl status` |
| forbidden values | `registry`, `auth`, `caddy`; any value equal to an existing member id; the literal `pool` (reserved as the admin port name) |
| applies to | `kind: service` only. `pool` on a `kind: static` manifest raises |

Errors, all `StaticError` naming the file (matching `load_ams_overlay`'s
existing style):

```
<path>: pool must be a string, got 3
<path>: pool 'Core' does not match '^[a-z][a-z0-9-]{0,31}$'
<path>: pool 'auth' is reserved
<path>: pool is not valid for kind: static
```

Cross-manifest errors are raised by `sync`, not by the overlay reader (it sees
one file at a time):

```
pool 'core': member ids ['notification-svc', 'notification_svc'] mangle to the
same env suffix NOTIFICATION_SVC
pool 'core': member id 'pool' collides with the reserved admin port name
pool 'core': only one member ('kvservice') — refusing to create a pool of one
pool 'core': members 'emailservice' and 'notificationservice' both set
RESEND_FROM_ADDR to different values ('a@x' vs 'b@x'); a pool shares one
process env for non-identity keys, so one of them must change or leave the pool
```

The last one is T0 finding 2 made enforceable: the process env holds the
**union** of the members' non-identity keys (§4.2), which is only safe while the
union is unambiguous. Identity keys are exempt — they are swapped per phase and
are *expected* to differ.

### 3.3 What translate emits for a 2-member pool

Members: `kvservice` (path mount `/kv`) and `timeservice` (path mount `/time`).

**`<state>/services/pool-core/service.toml`** (new; the two member
`service.toml` files are **not** written):

```toml
id = "pool-core"
name = "pool core"
secrets = ["SVC_SECRET__KVSERVICE", "SVC_SECRET__TIMESERVICE"]
depends_on = ["registry"]

[start]
argv = ["python", "/home/harness/store/state/services/pool-core/root/pool_runner.py"]
workdir = "repo"

[env]
GIT_COMMIT = "9f1c0b2e4a6d8f0011223344556677889900aabb"
POOL_ID = "core"
POOL_PORT_ADMIN = "${PORT_pool}"
POOL_PORT_KVSERVICE = "${PORT_kvservice}"
POOL_PORT_TIMESERVICE = "${PORT_timeservice}"

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

[logging]
format = "level-prefix"
```

Notes on each derived value:

- **`argv[0] = "python"`** — `runtime_env` prepends `<root>/.venv/bin` to PATH,
  exactly as the existing translator relies on for `uvicorn` (docs/manifest-translation.md).
- **absolute `pool_runner.py` path** — consistent with the translator already
  baking absolute `<root>/data/...` and `SVC_M2M_PUBLIC_KEY_PATH` values. The
  runner finds its member file as `Path(__file__).with_name("pool.json")`, so
  there is exactly one absolute path in argv and none in the members file.
- **`workdir = "repo"`** — provisioning cwd for `uv pip install -e <rel>` and
  runtime cwd. The venv is at `<root>/.venv` (non-sync layout,
  runtime.python_venv_dir, runtime.py:158-176), i.e. **outside** `repo/`, which
  `stage()` swaps. Editable installs still point *into* `repo/`, so the "stop
  before re-stage" rule (CLAUDE.md) still applies unchanged.
- **`[ports] pool = 0`** — the runner's own admin/health listener. `PORT_NAME_RE`
  is `^[a-z][a-z0-9_]{0,15}$` (schema.py:33), which **rejects `notificationservice`
  (19 chars) and any id containing `-`**. T1 widens the length to 31 to match
  `SERVICE_ID_RE`; hyphens are mangled to `_` and the mangling is checked for
  injectivity across the pool.
- **`memory_max`** = `pool_memory_base + N × pool_memory_per_member`, defaults
  `150M` + `30M` → 210M for N=2, 510M for N=12. **Derived from the n=1 import
  measurement, so it is a starting value to be re-derived in T10**, not a fact.
- **`pids_max`** = `48 + 16 × N`. The SDK starts up to three daemon threads per
  member instance (heartbeat, M2M refresh, CLS forwarder — facts-api §7); cgroup
  v2 counts threads as pids, so the per-service default of 64 would be wrong.
- **`cpu_max = "100%"`** — one budget for what used to be N × 40% on a 1 vCPU box.
- **`start_period_s = 240`** — N sequential `build_app()` calls on one vCPU.
- **`[logging] format = "level-prefix"`** — the runner owns the root handler and
  emits a leading level token (see §4.7).

**`<state>/services/pool-core/root/pool.json`** (new sidecar, written by
`_phase_declare`, 0640, harness-owned then chowned to the pool block):

```json
{
  "version": 1,
  "pool": "core",
  "sha": "9f1c0b2e4a6d8f0011223344556677889900aabb",
  "members": [
    {
      "id": "kvservice",
      "app": "kvservice.main:build_app",
      "factory": true,
      "port_env": "POOL_PORT_KVSERVICE",
      "health_path": "/health",
      "secret_env": {"SVC_SECRET": "SVC_SECRET__KVSERVICE"},
      "env": {
        "SVC_NAME": "kvservice",
        "SVC_AUDIENCE": "kvservice",
        "SVC_HEALTH_PATH": "/health",
        "SVC_CAPABILITIES": "keyvalue",
        "SVC_M2M_PUBLIC_KEY_PATH": "/home/harness/store/state/services/pool-core/root/etc/jwt-rs256.pub",
        "REGISTRY_URL": "http://127.0.0.1:20100",
        "AUTH_URL": "http://127.0.0.1:20101",
        "KV_DB_PATH": "/home/harness/store/state/services/pool-core/root/data/kvservice/kv.sqlite3"
      }
    },
    {
      "id": "timeservice",
      "app": "timeservice.main:build_app",
      "factory": true,
      "port_env": "POOL_PORT_TIMESERVICE",
      "health_path": "/health",
      "secret_env": {"SVC_SECRET": "SVC_SECRET__TIMESERVICE"},
      "env": { "...": "..." }
    }
  ]
}
```

- `app` / `factory` are **parsed from the member's existing `process.exec`**
  (`uvicorn <mod>:<attr> [--factory] …`), so a module-level `app` member
  (`resume`, facts-api §2) is handled without a special case.
- `env` is the member's fully-injected env map, exactly what `translate` puts in
  a standalone member's `[env]` today, **minus** `PORT` (the runner sets it from
  `port_env`) and minus secrets (which arrive through the process env).
- **No secret values in this file, ever** — only the name mapping.

**`<state>/platform/mounts/kvservice.json`** (two new optional keys, everything
else byte-identical to today's golden):

```json
{
  "version": 1, "id": "kvservice", "kind": "service",
  "gateway": "api.lishuyu.app", "path": "/kv", "subdomain": null,
  "port_name": "kvservice", "port_owner": "pool-core",
  "static_root": null, "build": [], "headers": {}
}
```

`port_owner: null` (absent) means "my own id", which is the current behaviour, so
**no existing mount golden changes** and the sidecar stays `version: 1` — the
same additive precedent D26 set for `deployed_sha`.

**`<state>/platform/registry/kvservice.json`** — **completely unchanged.** This
is the strongest property of Alternative B: the registry never learns that
pooling exists.

### 3.4 `ams platform status`

```
ID                   STAGE      SHA       POOL       AGE
pool-core            healthy    9f1c0b2   (12)       4m
  kvservice          healthy    9f1c0b2   core       4m
  timeservice        healthy    9f1c0b2   core       4m
files                healthy    9f1c0b2   -          4m
```

Pool rows sort first and carry `(N)` = member count from `pool_members`;
members indent under their pool and carry the pool name. `ams platform status
kvservice` prints the member record plus one line naming the pool and its stage.
`ams ctl status` is untouched and shows only `pool-core` — it is a supervisor,
and one process is one row.

---

## 4. Runtime contract of the pool runner

### 4.1 Where it lives

**A standalone file ams places into the pool root**, shipped as package *data*:
`src/ams/platform/assets/pool_runner.py`, with **no `__init__.py` in `assets/`**.
`_phase_declare` copies its bytes to `<pool-root>/pool_runner.py`.

- *Rejected:* putting it in the api SDK (`python -m sdk.pool`). Which manifests
  share a process is a deployment decision owned by the runtime, not by a
  library that Layer 0 also depends on; and an SDK-owned runner means every
  runner fix is an api commit plus a fleet redeploy, where an ams-owned asset is
  re-placed on the next sync tick. It also keeps the api repo at **zero
  changes**, which matters because it is private and has a second consumer (the
  legacy deployer).
- **The hard rule this respects**: `src/ams` must never import fastapi/uvicorn.
  The asset is only ever `read_bytes()`+`write_bytes()` by ams. Enforced by a
  portable test (T2) that imports every `ams.*` module and asserts
  `"fastapi" not in sys.modules and "uvicorn" not in sys.modules`, plus an
  assertion that `assets/__init__.py` does not exist.

### 4.2 The identity env, and why the swap is per *phase* not per *build*

**The swap must wrap three phases, not one.** T0's AST survey (n=19, static)
found five services — `timeservice`, `llmpricing`, `resume`, `displayservice`,
`test-service` — calling `sdk.config.load_from_env()` **again inside their
lifespan**, not only inside `build_app()`. A runner that swapped only around
`build_app()` would give four of the five a *neighbour's* identity at startup.
So the contract is: **build, lifespan startup and lifespan shutdown each run
under the member's identity env.**

**Identity keys** — swapped per phase, exactly this set (from spike-pool.md
finding 2):

```
SVC_NAME  SVC_AUDIENCE  SVC_SECRET  SVC_ROOT_PATH  SVC_ENDPOINT
SVC_CAPABILITIES  SVC_HEALTH_PATH  SVC_DISPLAY_NAME  SVC_OWNER  SVC_LOCATION
PORT  AMS_DATA_DIR
```

`SVC_NAME` and `SVC_AUDIENCE` are **required** — `load_from_env()` raises
`KeyError` without either (probe). `SVC_ROOT_PATH` is not *injected* by the pool
under B; where a manifest sets it today it is passed through unchanged, since
the member owns its whole port. (The Alternative-A fallback would inject it.)

**Non-identity keys live in the process env, as a union.** T0 finding 2: every
request-time env read outside that list uses a service-specific name
(`MESSAGE_INGEST_TOKEN`, `NOTIFY_EMAIL_*`, `LOCATION_*`, `MAILBOX_*`,
`DEEPSEEK_API_KEY`, `RESEND_API_KEY`, `BOT_LLM_*`). So the process env holds the
union of all members' non-identity keys and nothing needs restoring at request
time. `REGISTRY_URL` and `AUTH_URL` are non-identity but **mandatory** and
identical for every member (the SDK issues an ACL refresh against the *default*
registry URL when they are unset, so omitting them points a member at the wrong
registry rather than at none). `SVC_M2M_PUBLIC_KEY_PATH` is likewise shared —
one `<pool-root>/etc/jwt-rs256.pub`. `<PREFIX>_DB_PATH` values are per member but
distinct by name, so they union cleanly after the `data/<member>/` rewrite.

`translate` **rejects** a pool whose members disagree on a non-identity key
(§3.2), because the union is only safe while it is unambiguous.

### 4.3 Startup sequence

The uvicorn `Server` split below is **verified** by `pool_spike2.py`
(6 members, n=3). It is internal API, so §4.4 pins the version and says how the
runner refuses an incompatible one.

1. Read `Path(__file__).with_name("pool.json")`; fail loudly on an unknown version.
2. Apply the union of non-identity keys to `os.environ`; snapshot it.
3. **Build phase**, per member in `pool.json` order, under its identity env:
   import the module, resolve the attribute, call it if `factory` else use it
   as-is (covers `resume`'s module-level `app`); then
   `config = uvicorn.Config(app, host="127.0.0.1", port=<member port>,
   log_config=None, access_log=False)`, `config.load()`,
   `server = uvicorn.Server(config)`,
   `server.lifespan = config.lifespan_class(config)`. Restore the snapshot.
4. **Startup phase**, per member **sequentially**, each under its identity env:
   `await server.startup()` — this is what runs the member's lifespan, and the
   reason it is sequential rather than gathered. Catch per member; see §4.5.
5. Build the admin listener (a bare ASGI callable, no FastAPI) on
   `POOL_PORT_ADMIN` serving `/_pool/health`, and start it the same way.
6. **Serve phase**: `await asyncio.gather(*(s.main_loop() for s in started))`,
   concurrently, with **no** identity env applied — nothing in the serve path
   reads an identity key except `SVC_ENDPOINT` (T11).
7. `SIGTERM`/`SIGINT` sets `should_exit` on every started server, so the
   existing `[stop] SIGTERM, 10 s` contract holds. Measured: 0.63–0.73 s for 6
   members.
8. **Shutdown phase**, per member sequentially, each **back under its identity
   env**: `await server.shutdown()`, since a lifespan shutdown may re-read
   `load_from_env()` for the same reason startup does.

### 4.4 Pinning the uvicorn internal API

Steps 3–8 use `Config.load()`, `Config.lifespan_class`, `Server.lifespan`,
`Server.startup()`, `Server.main_loop()` and `Server.shutdown()` — all internal,
none covered by uvicorn's public contract. **Verified against uvicorn 0.52.4**
(the version resolved into the spike venv).

- The pool's provisioning pins it: `packages` carries `uvicorn[standard]==0.52.4`
  ahead of the editable member entries, so the pool venv is pinned even though
  each member's own `pyproject.toml` says `>=0.27`. This is the one place the
  pool overrides a member's dependency range, and it is deliberate.
- **The runner refuses to start on an incompatible uvicorn** rather than failing
  halfway through startup. Before building any member it checks
  `hasattr` for each of the six names above plus `uvicorn.__version__`, and on
  any miss exits non-zero with a single line naming the missing attribute and
  the version found:
  `FATAL pool: uvicorn <ver> lacks Server.lifespan; pool runner requires the
  0.52.x internal API`. A loud refusal at second zero is a clean `failed`
  record; a half-started pool is not.
- Revisit on any uvicorn bump. This is a real coupling and the plan does not
  pretend otherwise.

### 4.5 Member build or startup failure — decided

**Skip the failed member, serve the rest, exit non-zero only if zero members
started.**

- The failed member's port is never bound → its per-member health probe in
  `sync._phase_health` fails → its own `ServiceRecord` goes `failed` → the
  policy health gate escalates *that member*. Per-member failure attribution
  survives.
- The runner emits one `ERROR [<member>] pool: <phase> failed: <type>: <msg>`
  line per failure, which reaches the agent through the normal escalation path.
- **`SystemExit` must be caught, not just `Exception`.** T0 finding 3: a uvicorn
  startup failure calls `sys.exit(3)` **inside the task**, and in one event loop
  that `SystemExit` propagates out of `asyncio.run` and takes down every member.
  `SystemExit` inherits from `BaseException`, so a bare `except Exception` does
  not catch it. The runner wraps each member's build **and** `await
  server.startup()` in `except (Exception, SystemExit)`, records the member
  failed, and continues. **Without this, one member's bad bind or bad lifespan
  is a full-pool outage** — the exact failure mode the skip-the-member decision
  exists to prevent. T2 tests it explicitly.
- *Rejected:* **fail the whole pool.** It converts one service's bad commit into
  a 12-service outage — the precise opposite of what the memory saving is worth,
  and strictly worse than today, where a bad commit takes down one service.
- *Rejected:* **retry the member in-process.** Import failures are
  deterministic; retrying turns a clean `failed` record into a log flood.

### 4.6 `/_pool/health`

`200` when **at least one** member is serving, with a JSON body
`{"pool": "core", "ok": ["kvservice"], "failed": {"timeservice": "..."}}`;
`503` when none is. Deliberately *not* "all members healthy": this endpoint
drives the supervisor's restart policy and the `depends_on` gate (D27), and an
all-or-nothing gate would make one bad member flap the whole pool. Per-member
readiness is judged by `sync`, per member, on that member's own port.

### 4.7 Logging

The runner owns the root logger before importing any member, so the SDK's
`logging.basicConfig` call is a no-op (it is gated on
`if not logging.getLogger().handlers` and is verified idempotent by the SDK's
own test — facts-api §7). Format:

```
%(levelname)s [%(ams_member)s] %(name)s: %(message)s
```

`ams_member` is injected by a `logging.Filter` that maps the record's top-level
logger package to a member id (`kvservice.main` → `kvservice`), falling back to
`pool`. Uvicorn's own loggers are module-global and cannot be per-server, so
they land on `pool`; each `uvicorn.Config` gets `log_config=None` so it does not
reconfigure the root.

Consequences, stated plainly: `LogLine.service_id` is `pool-core` for every
line. The member tag is in the message text only. Dedup remains
member-granular because `policy.cause_key` normalizes the *text* and the
bracketed tag survives (**T7 must assert this against the real `_normalize`**).
Adding a `member` field to `ams.events.LogLine` is a Phase-B option, not part
of this plan — it touches core for an observability nicety.

---

## 5. Subsystem deltas

Interfaces are frozen here so T3/T5/T6/T7 can be written in parallel against
them.

### 5.1 `src/ams/schema.py` (T1)

```python
PORT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")   # was {0,15}
```
Widening only; every existing name still matches, no golden changes.

### 5.2 `src/ams/platform/static.py` (T4)

```python
@dataclass(frozen=True)
class Overlay:
    secrets: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    pool: str | None = None            # NEW

_OVERLAY_TOP_KEYS = frozenset({"secrets", "env", "pool"})

def overlay_pool(manifest_dir: Path) -> str | None: ...   # NEW, mirrors overlay_secret_names
```

### 5.3 `src/ams/platform/translate.py` (T3)

```python
POOL_ADMIN_PORT_NAME = "pool"
POOL_ID_PREFIX = "pool-"
POOL_UVICORN_PIN = "uvicorn[standard]==0.52.4"   # §4.4; bump deliberately

#: Swapped per member around build / lifespan startup / lifespan shutdown.
#: Everything NOT in this set is unioned into the pool's process env.
IDENTITY_ENV = frozenset({
    "SVC_NAME", "SVC_AUDIENCE", "SVC_SECRET", "SVC_ROOT_PATH", "SVC_ENDPOINT",
    "SVC_CAPABILITIES", "SVC_HEALTH_PATH", "SVC_DISPLAY_NAME", "SVC_OWNER",
    "SVC_LOCATION", "PORT", "AMS_DATA_DIR",
})

@dataclass(frozen=True)
class PoolMember:
    id: str
    translation: Translation          # the member's own, for the sidecars
    runtime_rel: str                  # e.g. "services/kvservice"
    app: str                          # "kvservice.main:build_app"
    factory: bool
    identity_env: Mapping[str, str]   # the IDENTITY_ENV subset, minus secrets
    shared_env: Mapping[str, str]     # everything else, unioned into [env]
    secret_names: tuple[str, ...]     # unmangled, e.g. ("SVC_SECRET", "DEEPSEEK_API_KEY")

@dataclass(frozen=True)
class PoolTranslation:
    id: str                           # "pool-core"
    decl: ServiceDecl
    members: tuple[PoolMember, ...]
    pool_json: Mapping[str, Any]      # the <root>/pool.json document

def build_pool(pool: str, members: Sequence[PoolMember], ctx: TranslateContext) -> PoolTranslation: ...
def pool_member(t: Translation, manifest_dir_rel: str) -> PoolMember: ...   # exec/env extraction
def mangle_member(member_id: str) -> str: ...    # "a-b" -> "A_B"; caller checks injectivity
```

Other edits in this file:
- `_mount()` gains `port_owner` (default `None`) and takes `port_name` as an
  argument rather than the constant, so a pooled member's mount names its own id.
- `TranslateContext` gains `pool_memory_base="150M"`, `pool_memory_per_member="30M"`,
  `pool_pids_base=48`, `pool_pids_per_member=16`, `pool_cpu_max="100%"`,
  `pool_start_period_s=240.0`, `pool_runner_name="pool_runner.py"`,
  `pool_uvicorn_pin=POOL_UVICORN_PIN`.
- `_rewrite_data_path` gains an optional `data_subdir` so a pooled member's
  `/var/lib/<name>/x` becomes `<pool-root>/data/<member>/x`. The existing
  "points at another service's state dir" rejection is unchanged.
- **`build_pool` raises on a non-identity key collision**: it folds every
  member's `shared_env` into one dict and raises `TranslateError` with the §3.2
  message when two members bind the same name to different values. Keys in
  `IDENTITY_ENV` are exempt — differing is their job.
- `packages` is emitted as `[POOL_UVICORN_PIN, *("-e", rel) for each member]`,
  the pin first so uv resolves it before the editable members widen the range.

### 5.4 `src/ams/platform/gateway.py` (T5)

`resolve_ports` (gateway.py:246-266) — the whole change:

```python
owner = mount.get("port_owner") or service_id
allocated = allocator.get(owner)
if port_name not in allocated:
    raise GatewayError(
        f"{service_id}: no allocated port named {port_name!r} on {owner!r} "
        f"(have: {sorted(allocated)})"
    )
out[service_id] = allocated[port_name]
```

`_render_site`, `_port_for`, the header/CSP/CORS tables and every Caddyfile
golden are **untouched**. A pooled member's site block is byte-identical to its
standalone one, only the port number differs.

### 5.5 `src/ams/platform/sync.py` (T6)

`ServiceRecord` — two additive fields (they must be declared or
`PlatformState.load` drops them on the next flush, sync.py:318):

```python
pool: str | None = None            # on a member record: the pool name
pool_members: list[str] = field(default_factory=list)   # on a pool record
```
`STATE_VERSION` stays 1 (additive, `.get`-tolerant readers — the D26 precedent).

`_Pending` — three additive fields:
```python
pool: str | None = None            # member: the pool it belongs to
port_owner: str = ""               # "" means self
port_name: str = "main"
```

Phase changes:

| phase | change |
|---|---|
| `_phase_translate` (sync.py:796) | after translating each manifest, `overlay_pool(manifest.parent)`. Group pooled pendings by pool; **suppress the member's `decl`/`decl_text`** (its `service.toml` is not written); build one extra `_Pending` per pool from `translate.build_pool`. Raise the cross-manifest errors of §3.2 here |
| `target_sha` (sync.py:716-745) | a pool's sha = repo head iff **any** member is affected by the commit range, else the members' common `deployed_sha`; if members' deployed shas disagree, head. The per-member rule (D26) is otherwise unchanged and still decides "affected" |
| `_phase_materialize` (sync.py:908) | pooled members: **skip entirely** (no stage, no provision). The pool item stages once into `services/pool-<n>/root/repo` and provisions once (`kind="uv", sync=false, packages=[…]`). 12 `cp --reflink` + 12 `uv sync` become 1 + 1 |
| `_phase_declare` (sync.py:955) | pool item: write `service.toml`, `<root>/pool.json`, place `pool_runner.py`, `place_jwt_key(pool_id)` once, `mkdir <root>/data/<member>` per member, generate each member's `SVC_SECRET` into `<state>/secrets/pool-<n>/SVC_SECRET__<MEMBER>`. Member items: write `mounts/<id>.json` and `registry/<id>.json` as today, and **`unlink` a stale `services/<member>/service.toml`** (file only, never the root — the data lives there until `adopt` moves it) |
| `_phase_reload` (sync.py:1030) | unchanged |
| `_phase_gateway` (sync.py:1088) | unchanged (renders from sidecars on disk; `port_owner` does the work) |
| `_phase_register` (sync.py:1158) | unchanged except the secret lookup: `run.secrets.load(pool_id, [mangled])[mangled]` for a pooled member |
| `_phase_finish` (sync.py:1218-1224) | `ports.get(item.id).get("main")` → `ports.get(item.port_owner or item.id).get(item.port_name)`; error text names the owner |
| `_phase_health` (sync.py:1175) | unchanged — still `http://127.0.0.1:{port}{health_path}`, because B gives each member its own port |

**Adoption guard**: `_phase_declare` refuses to declare a pool whose member has
a non-empty legacy `<state>/services/<member>/root/data`, escalating once with
the exact command (`ams platform pool adopt core`). Sync never moves data.

### 5.6 `src/ams/platform/backup.py` (T7)

`Target` gains `label: str = ""` (empty = `service_id`); `archive_name` and the
`remote_dir`/`remote_object` call sites use `label`. `discover` reads
`<root>/pool.json` when present, and for a db found at `data/<member>/x.db`
whose `<member>` is in the member list, sets `label=<member>`. `find -maxdepth 2`
(backup.py:304) already reaches one level down, so **no find change is needed**.
Result: R2 keys stay `.../kvservice/kvservice-kv-<stamp>.db.gz` before and after
pooling, and every non-pooled service's key is byte-identical.

### 5.7 `src/ams/platform/policy.py` (T7)

- `_maybe_crash_loop`: when the exited id has `pool_members` in
  `platform/state.json`, append `"(pool of N: a, b, c)"` to the escalation so
  "one process crashed" reads as "N services are down".
- Assert in a test that `cause_key`'s normalization preserves a leading
  `[<member>]` token, so two members' identical errors do not collapse. If it
  does not, the runner's tag moves to a form that does — this is a T7 finding,
  not an assumption.
- `_health_gate` is unchanged: it reads per-`ServiceRecord` stages and members
  keep their own records.

### 5.8 `src/ams/platform/rollback.py` + `pool.py` + `cli.py` (T8)

- `rollback(state, store, id, …)`: if `id` is a pooled member, **refuse** with
  `RollbackError` naming the pool and stating that a pool is the rollback unit.
  If `id` is a pool id, roll the whole pool back (stage → declare → reload →
  restart → gate, all as today, with the health gate run **per member**).
- New `src/ams/platform/pool.py`:
  - `plan(state, …)` — what a pool would contain, for `--dry-run`.
  - `adopt(state, pool_id, *, uids, run_admin_fn)` — for each member with a
    legacy root: stop it, move `<member-root>/data/*` →
    `<pool-root>/data/<member>/` and chown to the pool block inside the admin
    ns, copy `<state>/secrets/<member>/NAME` → `<state>/secrets/<pool>/NAME__<MEMBER>`,
    then unlink the member's `service.toml`. Idempotent; refuses while the pool
    is running; never deletes the legacy root (an operator does that after
    verifying).
- `cli.py`: `ams platform pool {plan,adopt} <pool-id>`.

### 5.9 What the user loses (say it in the docs, not only here)

1. **No independent rollback of a pooled member.** The pool is the unit.
2. **A redeploy of any member restarts all members** (D26's whole-fleet
   avoidance still works *between* pools, not *within* one).
3. **One OOM domain**: one member's memory spike kills all members.
4. **No secret isolation between members** (§6).
5. **Escalations are attributed to the pool id**; the member is in the message
   text, not in `LogLine.service_id`.

---

## 6. Secrets and the trust domain — stated, not engineered

Members of one pool run **in one address space**. Any member can read
`os.environ`, walk `sys.modules`, and reach every other member's `app.state`.
**Isolation between members of a pool is not achievable and is not attempted.**
One pool = one trust domain. This is the price of the memory saving and it is
the reason the pool boundary in §7 is drawn on blast radius, not convenience.

What *is* still enforced, unchanged:

- Secret **values** never appear in argv, in `pool.json`, in a declaration or in
  any log line. They arrive exactly as today: `SecretStore.load(decl.id, decl.secrets)`
  at spawn (secrets.py:248-271), for the one `decl.id` that is the pool.
- Storage: `<state>/secrets/pool-core/SVC_SECRET__KVSERVICE`, 0600, harness-owned,
  unreadable from inside the namespace (D4/D16).
- Naming: `<NAME>__<MANGLED_MEMBER>`, `-` → `_`. Injectivity of the mangling is
  checked per pool and is an error, not a warning.
- `SVC_SECRET` is still auto-generated by `_phase_declare` when absent, so the
  only names an operator ever types are third-party keys, and only for the few
  members that have a `service.ams.toml` today.
- The runner restores the environment snapshot after each `build_app()`, which
  keeps a member from *accidentally* reading a neighbour's `SVC_SECRET` at
  import time. That is hygiene. It is not a boundary and must not be described
  as one.

---

## 7. Migration on racknerd

### 7.1 Which services pool

**Rule**: a manifest joins a pool iff its `service.ams.toml` says so. Layer 0
(`registry`, `auth`) and `caddy` never pool (fixed ports, start-order root,
blast radius — and `auth` mints a module-level session secret at import,
facts-api §2a).

**Recommendation: one pool, `pool-core`, with the low-risk Layer-1 Python
services; four stay standalone.**

| stays standalone | why |
|---|---|
| `displayservice` | pillow (C ext, image buffers) + paho-mqtt + a refresh loop — the most plausible memory spike, and an OOM takes the whole pool |
| `llmgateway` | streaming LLM proxy + `python-multipart` uploads; response buffering is unbounded by workload, not by code |
| `files` | uploads, a p2p websocket route, GC sweeps; same unbounded-by-workload argument |
| `oss` | boto3/botocore is the heaviest import in the fleet and does multipart uploads |

Everything else that is `kind: service` joins `core`: `kvservice`, `logservice`,
`messageservice`, `notificationservice`, `emailservice`, `commentservice`,
`wechatservice`, `secretsservice`, `timeservice`, `pages`, `mailbox`,
`locationservice`, `turingtest`, `llmpricing`, `resume`.

**Two members carry a caveat until T11 lands**: `locationservice` and `mailbox`
are the two whose **login return-to degrades** in a pool, because
`SVC_ENDPOINT` is the one identity key read at request time and resolves to `""`
there (`sdk/ui.py:266 login_redirect`, `mailbox/main.py:249 _sso_redirect`;
T0 finding 2). Either land T11 first, hold these two back to the second wave, or
accept a degraded return-to on their login flow. **Recommendation: land T11
first** — it is small, it is a correctness fix in its own right, and it removes
the only known request-time coupling.

**The roster reconciles.** The box's 24 declared services are
`registry` + `auth` + `caddy` + `hello` + `pyhello` (two harness fixtures)
+ 17 deployer-managed + 2 static sites. The 16 live Python processes are the
17 deployer-managed plus `registry` and `auth`, minus `resume`,
`displayservice`, `oss` and `secretsservice`, which were failed at measurement
time. Nothing is unaccounted for. Note that three of those four failures are
already on the standalone list, and the fourth (`secretsservice`) is a
prospective member — **T10 must confirm it starts standalone before pooling it**,
since a member that cannot start alone will not start in a pool either.

*Rejected: all 19 in one pool.* It buys roughly 135 MiB more (4 × ~45 MiB less
one process's worth of overhead) and puts an image processor, an upload proxy
and a streaming LLM gateway in the same OOM domain as the entire fleet. Revisit
after T10 has a measured per-member number under real traffic.

*Rejected: several pools by tier (e.g. `core` + `apps`).* Two pools of 7 save
~45 MiB less than one pool of 15 and double the operational surface, for a
blast-radius split that the exclusion list already provides more cheaply.

### 7.2 Cutover order — zero surprise

Run with `ams platform sync --dry-run` before each state-changing step.

1. **Confirm the roster and the four failures.** `ams platform status` +
   `ams ctl status` on the box; write the actual member list into
   `docs/design/history/pool-migration.md`. The 24/16 counts already reconcile (§7.1);
   what still needs confirming is that `secretsservice` starts **standalone**
   before it is pooled — a member that cannot start alone cannot start in a pool.
2. **Back up first.** Run the backup unit by hand
   (`systemctl start ams-platform-backup`) and confirm the R2 objects for every
   prospective member. Adoption moves SQLite files; a fresh snapshot is the
   undo.
3. **Add the overlay lines.** One `pool = "core"` per member, committed to the
   `api` branch `ams-platform` and pushed to `<store>/upstream/api.git`.
   The legacy deployer is unaffected (it never reads `service.ams.toml`).
4. **Dry run.** `ams platform sync --dry-run` must report: one new service
   `pool-core`, N members losing their declarations, N mount sidecars gaining a
   `port_owner`, zero registry sidecar changes.
5. **Sync with the adoption guard tripping.** The first real tick declares
   nothing for the pool and escalates once, naming `ams platform pool adopt core`.
   That is the designed stop, not a failure.
6. **Adopt.** `ams platform pool adopt core` — stops each member, moves
   `data/` and secrets, unlinks the member declarations.
7. **Sync again.** Pool declared → provisioned (one `uv pip install`, expect
   minutes on 1 vCPU with a warm uv cache) → reloaded → members registered
   (registry rows are unchanged, so every `create_identity` returns 409 =
   success) → per-member health gate.
8. **Gateway re-render** happens inside the same tick (`_phase_gateway` reads
   sidecars from disk); confirm `<state>/gateway/sites/kvservice.caddy` now
   points at the pool's port and that `caddy fmt` still round-trips.
9. **Verify externally**, not from the box's own loopback: every member's public
   URL through `api.lishuyu.app`, plus one M2M-token call (the
   `timeservice → kvservice` path from `platform-layer0.md` §4) to prove the JWT
   chain still works from one shared `<pool-root>/etc/jwt-rs256.pub`.
10. **Measure** (§7.3), then leave the legacy member roots in place for one
    backup cycle before deleting them by hand.

**Rollback of the migration itself**: delete the `pool = "core"` lines, sync,
then `ams platform pool adopt` in reverse — not implemented, so the honest
rollback is: restore each member's `data/` from step 2's snapshot, drop the
overlay lines, sync. State this in the docs; do not pretend the migration is
one-command reversible.

### 7.3 The measurement to commit to

Taken before step 3 and after step 9, same method (`systemd-cgls`/`cgroup
memory.current` per service, `free -m`, `ps` RSS), n=3 samples 60 s apart, and
**with the fleet idle at both ends** so the comparison is like-for-like.

| metric | before | after | target |
|---|---|---|---|
| Python processes | 16 | 5 (4 standalone + 1 pool) | — |
| pool cgroup `memory.current` MiB | n/a | measure | ≤ 150 MiB |
| — reference point: T0, **6 apps serving, one loop** | n/a | **66 MiB (n=3)** | extrapolates to ~85–100 MiB at N=15 |
| — reference point: A-variant, 4 apps, one port | n/a | 78 MiB (n=1) | fallback only |
| sum of Layer-1 cgroups MiB | measure (~700) | measure | ≤ 300 MiB |
| all-services cgroup total MiB | 1105 | measure | ≤ 600 MiB |
| `free -m` available | 694 | measure | ≥ 1200 MiB |
| pool thread count | n/a | **measure — do not reuse T0's number** | < `pids_max` |
| p50 latency, `/time/health` via Caddy | measure | measure | no regression |
| cold start to all-members-healthy, s | measure | measure | < `start_period_s` |

If `pool memory.current` lands materially above `150M + 30M × N`, re-derive
`pool_memory_base` / `pool_memory_per_member` from the measurement and say so in
D29's Open section rather than quietly widening the limit.

**The thread count is the one T0 number that must not be carried over.** T0 ran
with `SVC_DEV=1`, which disables the SDK's registry calls — so its 3–4 threads
at N=6 **omit the per-member heartbeat, M2M-refresh and CLS-forwarder threads**
that a translated declaration (which never sets `SVC_DEV`) will start. The
`pids_max = 48 + 16 × N` formula is still an unmeasured guess, and this is the
measurement that settles it. Both memory reference points carry the same caveat
in the other direction: no `SVC_DEV`, no registry client, means T0's 66 MiB is
if anything an under-estimate.

---

## 8. Task breakdown

Exclusive file ownership; no two tasks edit the same file. Every task is TDD:
tests first, then code. Portable tests go in `tests/`, Linux-only in
`tests/linux/`; **every basename below is checked distinct from the existing
suite** (pytest's `prepend` import mode makes a duplicate basename abort
collection for the whole suite — CLAUDE.md).

**Nothing is gated any more.** T0 is done, so **T1–T8 and T11 can all start
immediately** — they depend on the frozen interfaces in §5, not on each other.
T9 wants T1–T8 landed; T10 is last.

**Status as of 2026-09-03, verified by reading the tree, not by report:**

| task | state |
|---|---|
| T0 | ✅ done, passed (`docs/design/history/spike-pool.md`) |
| T1 `schema.py` | ✅ landed — `PORT_NAME_RE` widened, `tests/test_schema_portnames.py` |
| T4 `static.py` | ✅ landed — `pool` in `_OVERLAY_TOP_KEYS`, `_RESERVED_POOL_NAMES`, `tests/test_platform_overlay_pool.py` |
| T5 `gateway.py` | ✅ landed — `port_owner` in `resolve_ports`, `tests/test_platform_gateway_pool.py` |
| T7 `backup.py` + `policy.py` | ✅ landed — `tests/test_platform_{backup,policy}_pool.py` |
| T2, T3, T6, T8, T9, T10, T11 | not started |

`.venv/bin/python -m pytest -q` over those five new modules: **66 passed**.
`src/ams/platform/assets/` does not exist yet, so T2 is genuinely open. The
landed work follows the §5 interfaces as written; the sections below are the
contract for the rest.

### T0 — Spike: does B's serving topology work? ✅ **DONE, PASSED (2026-09-03)**

Report `docs/design/history/spike-pool.md`; scripts
`docs/design/history/evidence/{pool_spike2.py, pool_probe.py, envsurvey.py}`.
6 members on 6 ports in one event loop, n=3 identical runs: every `/health` 200,
RSS 66 MiB, 3–4 threads, SIGTERM → all stopped in 0.63–0.73 s. Produced the
three contract changes now folded into §4.2/§4.4/§4.5 and the T11 task below.
**No longer a gate on anything.**

### T1 — `PORT_NAME_RE` widening

- **Files**: `src/ams/schema.py`; `tests/test_schema_portnames.py` (new).
- **Tests first**: a 31-char port name validates; a 32-char one raises; every
  existing golden declaration still parses; `${PORT_<long_name>}` expands.
- **DoD**: `.venv/bin/python -m pytest -q` green, zero golden diffs.
- **Must NOT touch**: `translate.py`, any golden.

### T2 — The pool runner asset

- **Files**: `src/ams/platform/assets/pool_runner.py` (new, **no `__init__.py`**);
  `tests/test_pool_runner_static.py` (new);
  `tests/linux/test_pool_runner_live.py` (new).
- **Tests first**: portable — AST-parse the asset and assert it imports nothing
  from `ams`; import every `ams.*` module and assert `fastapi`/`uvicorn` are
  absent from `sys.modules`; assert `assets/__init__.py` does not exist; assert
  the asset parses under `ast.parse` with `feature_version=(3,12)`.
  Linux — build a two-member venv from `tests/fixtures/`, write a `pool.json`,
  run the runner, and assert:
  1. two ports serve distinct `SVC_NAME`s and `/_pool/health` is 200;
  2. a member whose **lifespan** re-reads `load_from_env()` sees its *own*
     identity, not its neighbour's — the T0 finding-1 regression test, using a
     fixture app that records `SVC_NAME` at startup **and** at shutdown;
  3. a member whose startup raises **`SystemExit(3)`** is marked failed while
     the other member keeps serving and the process does **not** exit — the T0
     finding-3 regression test, and the single most important assertion in this
     task;
  4. a deliberately broken *import* is skipped, one `ERROR [<member>]` line is
     printed, and the good member still serves;
  5. SIGTERM exits within the stop timeout;
  6. the runner refuses to start against a stubbed uvicorn missing
     `Server.lifespan`, with the §4.4 message and a non-zero exit.
- **DoD**: both modules green; assertions 2, 3 and 6 are asserted, not assumed.
- **Must NOT touch**: any existing `src/ams` file.

### T3 — translate: `build_pool`, `port_owner`, member extraction

- **Files**: `src/ams/platform/translate.py`;
  `tests/test_platform_pool_translate.py` (new);
  `tests/golden/platform/pool-core.toml`, `pool-core.pool.json` (new);
  updated `kvservice.mount.json` / `timeservice.mount.json` **only** in a new
  `tests/golden/platform/pool/` subdirectory (existing goldens stay untouched,
  proving `port_owner` is absent by default).
- **Tests first**: emitted `service.toml` matches the golden byte-for-byte;
  `schema.loads(emit_toml(d)) == d` for the pool decl (the invariant the
  existing suite asserts per manifest); `resume`'s module-level `app` yields
  `factory: false`; the five §3.2 cross-manifest errors raise with the exact
  message, **including the non-identity key collision**; a `pool` value on
  `kind: static` raises; `pool.json` contains no secret values (assert by
  scanning for any `SVC_SECRET` *value*-shaped string);
  `_rewrite_data_path` puts a pooled member's DB under `data/<member>/`;
  every key of `IDENTITY_ENV` present for a member lands in its
  `identity_env` and **never** in the pool declaration's `[env]`, and every
  other key lands in the union; `packages[0]` is the uvicorn pin.
- **DoD**: pytest green, goldens committed, every existing translate golden
  byte-identical.
- **Must NOT touch**: `sync.py`, `gateway.py`, `static.py`.
- **Depends on**: T1 for the widened port names. Assert `SVC_AUDIENCE`,
  `REGISTRY_URL` and `AUTH_URL` are present for every member — T0 showed all
  three are load-bearing, and the first is a `KeyError` if missing.

### T4 — overlay: the `pool` key

- **Files**: `src/ams/platform/static.py`; `tests/test_platform_overlay_pool.py` (new).
- **Tests first**: `pool` parses; the four value-validation errors raise with
  the file path in the message; an unknown top-level key still raises; an
  overlay with only `secrets`/`env` is unchanged (`pool is None`).
- **DoD**: pytest green; `tests/test_platform_static.py` (existing) untouched
  and still green.
- **Must NOT touch**: anything else.

### T5 — gateway: `port_owner`

- **Files**: `src/ams/platform/gateway.py`; `tests/test_platform_gateway_pool.py` (new).
- **Tests first**: a mount with `port_owner` resolves against the owner's
  allocation; without it, behaviour is byte-identical to today; a missing port
  on the owner raises naming both ids; a rendered site block for a pooled member
  differs from its standalone form **only in the port number**; duplicate mount
  ids still raise.
- **DoD**: pytest green; every `tests/golden/gateway/*` byte-identical.
- **Must NOT touch**: `sync.py`, the golden gateway configs.

### T6 — sync: grouping, records, phases

- **Files**: `src/ams/platform/sync.py`; `tests/test_platform_sync_pool.py` (new).
- **Tests first**: two pooled manifests produce one pool `_Pending` and zero
  member declarations; member `mounts/*.json` and `registry/*.json` are still
  written; a member's `registry/*.json` is byte-identical to the non-pooled
  golden; `pool`/`pool_members` survive a `PlatformState` load→flush round trip;
  a pool moves to head iff any member is affected and stays put otherwise (the
  D26 property, at pool granularity); `_phase_finish` looks the port up on the
  owner; a member with a non-empty legacy `data/` blocks the declare with the
  `adopt` escalation; a stale member `service.toml` is unlinked while its root
  survives; a tick that changes nothing still writes nothing.
- **DoD**: pytest green; `tests/test_platform_sync.py` (existing) green and
  unmodified.
- **Must NOT touch**: `translate.py`, `gateway.py`, `static.py`, `rollback.py`.
- **Depends on**: the frozen interfaces in §5.2/§5.3/§5.4 (stub them locally if
  T3/T4/T5 have not landed).

### T7 — backup label + policy pool awareness

- **Files**: `src/ams/platform/backup.py`, `src/ams/platform/policy.py`;
  `tests/test_platform_backup_pool.py` (new),
  `tests/test_platform_policy_pool.py` (new).
- **Tests first**: backup — a db at `data/<member>/x.db` in a root with a
  `pool.json` gets `label=<member>` and the historical remote key; a non-pooled
  service's key is byte-identical to today; a db under a subdir *not* in the
  member list keeps the pool id. policy — **first assert what `cause_key`'s
  normalization does to a `[member]` token**, then assert two members' identical
  errors produce different cause keys; a `ServiceExited` for a pool id yields an
  escalation naming the members; the health gate is unchanged for members.
- **DoD**: pytest green; existing backup/policy suites unmodified and green;
  the `cause_key` finding written into the task's summary regardless of outcome.
- **Must NOT touch**: `sync.py`, `translate.py`.

### T8 — `pool adopt`, rollback refusal, CLI

- **Files**: `src/ams/platform/pool.py` (new), `src/ams/platform/rollback.py`,
  `src/ams/platform/cli.py`; `tests/test_platform_pool_adopt.py` (new),
  `tests/linux/test_pool_adopt_live.py` (new).
- **Tests first**: portable — `adopt` is idempotent; it refuses while the pool
  is running; it never deletes a legacy root; secret files are copied with the
  mangled name and the originals are left in place; `rollback("kvservice")`
  raises naming `pool-core`; `rollback("pool-core")` reaches the per-member
  health gate. Linux — a real `adopt` in a delegated cgroup moves a real SQLite
  file and chowns it to the pool block, and the pool process can then open it
  read-write.
- **DoD**: both green; `ams platform pool --help` documents both subcommands.
- **Must NOT touch**: `sync.py`, `translate.py`, `gateway.py`.

### T11 — api side: derive the login return-to from the request (separate agent)

The one identity key read at **request** time, so the env swap cannot cover it.
In a pool `SVC_ENDPOINT` resolves to `""` and the login redirect loses its
return-to.

- **Repo**: the gitignored `api` clone, branch `ams-platform`. **Not this repo** —
  this is the only api-side change in the whole plan, and it is a correctness
  fix that stands on its own merits (deriving a return-to from the request is
  more correct than a build-time constant even without pooling).
- **Files**: `components/sdk/src/sdk/ui.py` (`login_redirect`, around line 266),
  `apps/mailbox/src/mailboxsvc/main.py` (`_sso_redirect`, around line 249),
  plus tests in the api repo's own suites.
- **Change**: when `SVC_ENDPOINT` is unset or empty, derive the return-to from
  the request — `str(request.base_url)` joined with `request.scope["root_path"]`
  — instead of falling back to `""`. When it is set, behaviour is unchanged, so
  nothing about the legacy deployer's deployment moves.
- **Tests first** (in the api repo): with `SVC_ENDPOINT` set, the redirect is
  byte-identical to today; unset, it is derived from the request and is
  absolute; unset **behind a `root_path`**, the derived URL includes the prefix.
- **DoD**: the api repo's own suite green on `ams-platform`; both call sites
  covered; no change to any `service.yaml`.
- **Must NOT touch**: anything in the ams repo.
- **Until T11 lands**: `locationservice` and `mailbox` are the two members whose
  login redirect degrades in a pool (the SDK helper and mailbox's own copy).
  Either keep them standalone or accept the degraded return-to — §7.1 lists them.

### T9 — docs and decisions

- **Files**: `docs/platform-pools.md` (new); edits to `docs/platform.md`,
  `docs/manifest-translation.md`, `docs/platform-sidecars.md`, `CLAUDE.md`,
  `docs/design/DECISIONS.md` (append D29 from §9), `docs/design/PROGRESS.md`.
- **DoD**: the five losses in §5.9 are stated in `docs/platform-pools.md`; the
  sidecar doc documents `port_owner` as optional-and-version-1; `CLAUDE.md`'s
  layout section names `pool.py` and the assets directory; D29 carries the n=1
  measurements in its Open section.
- **Must NOT touch**: any file under `src/` or `tests/`.

### T10 — Live verification on racknerd (last)

- **Files**: `docs/design/history/pool-migration.md` (new). No source edits.
- **Do**: (a) the **blocking-I/O audit** of §10 risk 7 — grep every prospective
  member for `urlopen`, `requests.`, sync `httpx.Client`, `sqlite3` outside a
  thread, and `time.sleep` in an `async def`, and report before the cutover;
  (b) confirm `secretsservice` starts standalone (it was failed at measurement
  time); (c) `scripts/remote-test.sh pool-live` (a subdir used by no other
  agent) for the full suite including `tests/linux/test_pool_*`; then
  `scripts/deploy-racknerd.sh`; then the §7.2 cutover, steps 1–10; then the
  §7.3 table with n=3 at both ends.
- **DoD**: the audit's findings written down whether or not they block; the
  completed §7.3 table with real numbers and n, **including a thread count taken
  with `SVC_DEV` unset** (T0's number does not transfer); every member's public
  URL returning 200 through `api.lishuyu.app`; one successful M2M call proving
  the shared JWT key path; `ams platform status` showing every member `healthy`;
  a login flow exercised on `locationservice` or `mailbox` to confirm T11's fix
  (or the degradation, if T11 has not landed); and an explicit statement of
  anything that regressed.
- **Must NOT touch**: any source file — a fix goes back to the owning task.

---

## 9. Draft `DECISIONS.md` entry

```markdown
### D29. Pools: N manifests, one process, distinguished by tag (2026-09-03)

The fleet's memory is dominated by a fixed per-process cost, not by the
services. On racknerd, 16 Python uvicorn processes hold 44–74 MiB each while
`import fastapi` alone is 44 MiB, and 14 member packages imported into one
interpreter cost 59 MiB in total. The services are ~1 MiB each; the interpreter
is everything. **So the unit of deployment, not the unit of code, is what has
to change.**

A **pool** is one ams declaration, one root, one venv, one process, hosting N
`api` services as N `uvicorn.Server` instances on N ports in one asyncio event
loop. Each member keeps its own allocated port, its own registry identity and
ACL, its own Caddy route, its own `/health` on its own port, its own
`data/<member>/` directory, its own secrets, its own `ServiceRecord` and its own
change detection. The **tag that distinguishes members is the service id it
already has** — `SVC_NAME`, the registry id, the mount id. Nothing new was
invented to tell members apart; the only new key says which process a manifest
runs in.

- **The grouping key is `pool = "<name>"` in `service.ams.toml`, not in
  `service.yaml`.** The legacy deployer validates manifests against
  `schemas/service.schema.json`, which sets `"additionalProperties": false` at
  the root, so a new manifest key would hard-fail the deployer that still owns
  production. The overlay is already the ams-only channel, already read before
  `translate()`, already reject-rather-than-guess. The pool's ams id is
  `pool-<name>` so it can never collide with a member id.
- **N ports, not one.** *Rejected:* one uvicorn with N `build_app()` results
  mounted under N path prefixes. A live probe (2026-09-03, n=1) **showed that
  shape works** — four apps under four Starlette `Mount()`s on one port, with
  the right `root_path`, `/docs` and `openapi.json` `servers` for members that
  set `root_path` themselves and for those that do not, because `Mount` sets
  `scope["root_path"]`. It was still rejected, on what the probe did not
  remove: it inverts the gateway's prefix-stripping rule for every pooled
  member, forces `health_path` to carry the mount prefix, has no prefix at all
  for the five subdomain-mounted members (a Caddy rewrite would make their
  `openapi.json` advertise a prefix their public URL does not have), and leaves
  the two websocket routes behind an untested `Mount` scope rewrite.
  **The decisive reason is reversibility.** This design's own safety valve —
  for a dependency conflict, a memory spike, or a service that must not be
  interrupted — is "that member leaves the pool". With one port per member,
  leaving is a pure deployment edit: the member's Caddy block is byte-identical
  in or out of the pool, and only the port number in `reverse_proxy` differs.
  With one shared port, leaving is a routing migration every time. A safety
  valve that is expensive to pull is not one. Keeping one port per member also
  leaves `gateway.py`'s renderer, every Caddyfile golden, `registryclient.py`
  and every `registry/<id>.json` sidecar byte-identical; the entire routing
  change is `resolve_ports` honouring one new optional `port_owner` field.
  The one-port shape stays documented as the fallback if N `uvicorn.Server`
  instances in one event loop turn out not to work.
- **The runner is an ams asset placed into the pool root, not SDK code.**
  `src/ams/platform/assets/pool_runner.py`, copied to `<root>/pool_runner.py`
  and run by the pool's own venv python. ams only ever reads its bytes, so the
  "nothing in `src/ams` imports fastapi" rule holds and is asserted by a test.
  *Rejected:* a `sdk.pool` module in the api repo. Which manifests share a
  process is a deployment decision owned by the runtime; putting it in a library
  that Layer 0 also depends on means every runner fix is an api commit plus a
  fleet redeploy, and it would have made the api repo — private, with a second
  consumer — carry a change this needs zero of.
- **The env splits into ~12 identity keys, swapped per member per phase, and
  everything else, unioned into the process env.** Fifteen members' `SVC_NAME`s
  cannot coexist in one env; fifteen sequential reads can. `load_from_env()` is
  **not** one-shot at `build_app()` time as both fact reports assumed: an AST
  survey of all 19 services plus the SDK (n=19, static) found `timeservice`,
  `llmpricing`, `resume`, `displayservice` and `test-service` calling it again
  **inside their lifespan**. So the swap wraps build, lifespan startup and
  lifespan shutdown, using the uvicorn split verified in the spike —
  `config.load()`, `lifespan = config.lifespan_class(config)`, sequential
  `await startup()` per member under its identity env, concurrent `main_loop()`,
  sequential `shutdown()` back under the identity env. Every request-time env
  read outside the identity set uses a service-specific name
  (`MESSAGE_INGEST_TOKEN`, `LOCATION_*`, `DEEPSEEK_API_KEY`, …), which is what
  makes the union safe; `translate` rejects a pool whose members bind one
  non-identity key to different values. Two keys turned out to be mandatory
  rather than metadata: `SVC_AUDIENCE` (`KeyError` without it) and
  `REGISTRY_URL`/`AUTH_URL` (the SDK refreshes ACLs against the *default*
  registry URL when they are unset, so omitting them points a member at the
  wrong registry rather than at none). The one identity key read at **request**
  time is `SVC_ENDPOINT`, in `sdk/ui.py:266` and `mailbox/main.py:249`; it
  resolves to `""` in a pool, so the login return-to is derived from the request
  instead — the single api-side change in this design, and a correctness fix in
  its own right. Secrets are the exception to `pool.json`: they arrive through
  the process env as `<NAME>__<MEMBER>` from `<state>/secrets/<pool-id>/`,
  because the spawn-time injection point is per declaration and there is exactly
  one declaration.
- **The runner catches `SystemExit` per member, not just `Exception`.** A
  uvicorn startup failure calls `sys.exit(3)` inside the task, and in one event
  loop that `SystemExit` — a `BaseException`, so invisible to
  `except Exception` — propagates out of `asyncio.run` and takes down every
  member. Verified in the spike. Without the catch, the skip-the-failed-member
  decision above is defeated by the most likely startup failure there is.
- **The pool venv pins `uvicorn[standard]==0.52.4`**, ahead of the editable
  member entries, overriding every member's own `>=0.27`. The startup split
  above uses six uvicorn internals (`Config.load`, `Config.lifespan_class`,
  `Server.lifespan`, `startup`, `main_loop`, `shutdown`), none of them public
  API. The runner `hasattr`-checks all six before building anything and exits
  with one line naming the missing attribute and the version found, so an
  incompatible bump is a clean `failed` record at second zero rather than a
  half-started pool. This is a real coupling; the plan does not pretend
  otherwise, and any uvicorn bump revisits it.
- **One pool is one trust domain, and that is stated rather than engineered
  around.** Members share an address space; any member can read another's
  environment and `app.state`. Isolation between members is not achievable, so
  it is not attempted. The runner's environment restore is hygiene, not a
  boundary. The pool boundary is therefore drawn on blast radius:
  `displayservice`, `llmgateway`, `files` and `oss` stay standalone because
  their memory is bounded by workload rather than by code, and one OOM kills
  every member of a pool.
- **A member that fails to build is skipped; the pool serves the rest.**
  *Rejected:* failing the whole pool, which converts one bad commit into a
  12-service outage — strictly worse than today, where it takes down one
  service. The skipped member's port never binds, so its own health probe
  fails, its own record goes `failed`, and per-member attribution survives.
  `/_pool/health` is 200 while **any** member serves, because it drives the
  supervisor's restart policy and the D27 dependency gate, and an
  all-or-nothing gate would let one bad member flap eleven good ones.
- **The pool is the rollback unit; a member id is refused.** `rollback.py`'s
  "one id in, one id out" cannot hold: one process runs one build. Refusing with
  a message naming the pool is honest; silently restarting twelve services
  because an operator named one is not. D26's change detection survives at pool
  granularity — the pool moves to head iff any member was touched — which means
  a commit to any member restarts all of them. That is the accepted price.
- **Provisioning is one `uv pip install -e <member>…` into one venv at
  `<root>/.venv`**, i.e. the existing non-sync uv path, not `uv sync --frozen`.
  Twelve `cp --reflink` and twelve `uv sync` calls become one of each.
  *Rejected:* a synthetic uv workspace at the pool root with
  `uv sync --all-packages`, which would need a new provisioning mode in
  `runtime.py` for a reproducibility gain that a `uv pip compile` lockfile can
  add later without changing the shape.
- **Adoption is an explicit operator command, never a sync side effect.**
  `ams platform pool adopt <pool>` stops each member, moves its `data/` under
  the pool root, chowns it to the pool's uid block and copies its secrets.
  Sync refuses to declare a pool whose member still has a non-empty legacy data
  directory and escalates once with the exact command. Moving a database is not
  something a 60 s timer should do.

**Assumptions.** (1) The member projects resolve into one venv — read from 21
`pyproject.toml` files with zero conflicting specifiers, installed together once
in a scratch venv, and six of them then built, served and stopped from it
(n=3). The escape hatch when this breaks is deleting one overlay line. (2) No member's
memory is unbounded — mitigated by the exclusion list, not by a mechanism.
(3) Members tolerate a shared process: no
`signal.signal`, no `sys.exit`, no module-level mutable SDK state, `ContextVar`
rather than globals, per-instance heartbeat threads, distinct package names,
distinct DB paths — all read exhaustively. `auth` is the known counter-example
(a module-level session secret minted at import) and stays out of every pool.

**Open / weak signals**
- **Memory saving is a projection, not a result.** No measurement so far has
  exercised request traffic, database connections or per-app caches, and every
  one of them ran with `SVC_DEV=1`, i.e. with the SDK's registry client and its
  per-member threads disabled. What exists: 16 standalone processes at
  44–74 MiB; `import fastapi` 44 MiB; 14 apps imported into one interpreter
  59 MiB, marginal ≈1 MiB/app (all n=1); and T0's **6 apps serving in one
  process at 66 MiB (n=3)**. The projected saving is ≈500 MiB for a 12-member
  pool. **Do not delete the standalone path or widen the pool on the strength of
  this** until §7.3's before/after table is filled in with n=3 idle samples on
  the real fleet.
- **T0's six members were the easy six** (`kvservice`, `timeservice`,
  `llmpricing`, `logservice`, `messageservice`, `commentservice`). It is
  evidence about the mechanism, not about the nine members it did not build.
  Nothing about a heavier member's import cost or lifespan behaviour follows.
- **`memory_max = 150M + 30M × N` and `pids_max = 48 + 16 × N` are still
  unmeasured formulas.** T0's 3–4 threads at N=6 **must not** be read as
  confirming the pids formula: `SVC_DEV=1` suppressed exactly the per-member
  heartbeat, M2M-refresh and CLS-forwarder threads the formula is sized for.
  Re-derive both from T10, where `SVC_DEV` is unset.
- **`policy.cause_key`'s normalization has not been checked against a
  `[<member>]` tag.** The claim that two members' identical errors stay
  distinct depends on that token surviving normalization. T7 asserts it first
  and reports the finding either way; if it does not survive, the tag's form
  changes, not the design.
- **`uv pip install -e` for a member whose `pyproject.toml` has a relative path
  dependency (`../../components/sdk`)** is verified for all 15 Layer-1 projects
  in a scratch venv (n=1), **not** through `runtime.provision`'s
  admin-namespace path with `provisioning_env`, and not with the
  `uvicorn==0.52.4` pin ahead of the editable entries. The wiring, not the
  mechanism, is what T3/T6 and the live run still have to prove.
- **Four services were failed at measurement time** — `resume`,
  `displayservice`, `oss`, `secretsservice`. Three are already on the standalone
  list; `secretsservice` is a prospective member and must be shown to start
  standalone before it is pooled. (The 24-declared / 16-live counts themselves
  reconcile — see §7.1 — so that is no longer open.)
- **A member's blocking I/O on the loop thread blocks every other member.**
  Observed in T0 as a spike artefact (`pages`' lifespan made a synchronous
  `urlopen` to `oss` and stalled the other servers). The artefact came from
  `SVC_DEV=1`, but the hazard is structural to a shared event loop and does not
  depend on it. n=1, no audit of the fleet for sync I/O in async paths has been
  done. See `PLAN-pool.md` §10 risk 7.
- **The migration is not one-command reversible.** Un-pooling means restoring
  each member's `data/` from a backup, dropping the overlay lines and syncing.
  A `pool evict` is not in scope.
```

---

## 10. Risks and the questions only the user can answer

1. **Confirm the pool roster.** The plan recommends 15 members in `core` with
   `displayservice`, `llmgateway`, `files`, `oss` standalone. The alternative
   (all 19 in one pool) buys ~135 MiB more and widens the OOM domain to the
   whole fleet. **Default if no answer: the 15-member roster.**
2. **Confirm the accepted losses in §5.9**, of which two are irreversible in
   character rather than in code: no independent member rollback, and no secret
   isolation between members. **Default: accept — the request explicitly asks
   for merged processes.**
3. **Backup key continuity.** The plan keeps R2 keys per logical service via
   `Target.label`. If keys should instead become `pool-core/<member>/…`, say so
   before T7; changing it afterwards orphans 14 days of objects.
4. **Risk, no question**: the box has 1 vCPU. A pool cold start imports 15 apps
   sequentially; `start_period_s = 240` is a guess that T10 must confirm against
   a real cold start. If it is short, the supervisor kills a pool that was
   merely slow, and it will look like a crash loop.
5. **Risk, no question**: the `api` clone is gitignored and not rsynced to the
   Linux host, so any new pool golden must copy its manifests into
   `tests/golden/platform/` like the existing 21 (CLAUDE.md).
6. **Risk, no question**: T0 is six members deep, all from the easy end of the
   fleet and all with `SVC_DEV=1`. It is strong evidence about the *mechanism*
   and none at all about the other nine members' import cost, thread count or
   lifespan behaviour under a live registry. T10 is where that gets tested;
   nothing between here and there should be described as proven.
7. **Risk, no question — the sharpest one T0 surfaced**: **a member doing
   blocking I/O on the loop thread stalls every other member.** T0 saw exactly
   this (`pages`' lifespan made a synchronous `urlopen` and the other servers
   stopped responding). The spike calls it an artefact of `SVC_DEV=1`, and the
   trigger was, but the hazard is structural: one event loop means one member's
   sync call is everyone's outage, where today it is only its own. Nobody has
   audited the fleet for sync I/O inside async paths. **Add that audit to T10**:
   grep the pooled members for `urlopen`, `requests.`, `httpx.Client` (the sync
   client, not `AsyncClient`), `sqlite3` outside a thread, and `time.sleep` in
   an async def, and report what turns up before the cutover rather than after.
8. **Risk, no question**: the pool venv pins `uvicorn==0.52.4` and the runner
   uses six of its internals. A future uvicorn bump is a deliberate revisit, not
   a routine dependency update. The `hasattr` gate makes an incompatible bump
   loud rather than silent, which is the most that can be done about it.
