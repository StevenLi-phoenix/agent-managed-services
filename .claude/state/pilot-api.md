# Pilot: two real `api` services under the ams harness — 2026-09-02

Two services from the `StevenLi-phoenix/api` monorepo (`services/kvservice`,
`apps/timeservice`) run as replicas under the live `ams-harness.service` on
racknerd, alongside the existing `hello` / `pyhello` demos. Every number and log
line below is copied from a real run; nothing is reconstructed.

Confidence marks: **[verified]** = observed directly in this session, with n.
**[inferred]** = follows from something verified but was not itself measured.
**[weak]** = single observation, plausible alternative explanations remain.

Artefacts: `examples/api-pilot/{kvservice,timeservice}/service.toml`,
`scripts/pilot-api.sh`, `src/ams/runtime.py` (`runtime.sync`).

---

## 1. What worked

Everything the brief asked for, on the first attempt, with **zero escalations**.

| | kvservice | timeservice |
|---|---|---|
| port (`state/ports.json`) | 20002 | 20003 |
| host uid (`state/uidmap.json`) | 102048 | 103072 |
| `GET /health` | 200 `{"status":"ok"}` | 200 `{"ok":true}` |
| `memory.max` / `memory.swap.max` | 157286400 / 0 | 104857600 / 0 |
| `pids.max` / `pids.current` | 64 / 7 | 64 / 7 |

```
--- kvservice  port=20002  uid=102048
GET /health -> 200
{"status":"ok"}
102048 2010032 /home/harness/store/state/services/kvservice/root/repo/services/kvservice/.venv/bin/python \
  .../.venv/bin/uvicorn kvservice.main:build_app --factory --host 127.0.0.1 --port 20002
memory.max=157286400
memory.swap.max=0
pids.max=64
memory.current=78708736
--- timeservice GET /now
{"now":"2026-09-02T15:59:14.559692+00:00"}
--- all supervised services
['hello', 'kvservice', 'pyhello', 'timeservice']
```

**timeservice is fully functional** [verified, n=1]: `/now` returns live JSON,
`/` renders its 6915-byte HTML page, `/agents.md` returns 537 bytes. No route it
serves needs a principal, so nothing is degraded by the absence of auth.

**kvservice is functional as far as anonymous access allows** [verified, n=1]:
`/health` (200) and `/agents.md` (200, 921 bytes) work; `GET /` and
`PUT /pilot` both return **401**, which is the honest answer — `AuthClient`
cannot resolve a principal without a reachable Auth, so `_require_user_ns`
raises before ACL is even consulted. No JWT was faked. The database is real:

```
-rw-r--r-- 1 102048 102048 16384 Sep  2 16:01 kvservice.db
-rw-r--r-- 1 102048 102048 32768 Sep  2 16:02 kvservice.db-shm
-rw-r--r-- 1 102048 102048     0 Sep  2 16:02 kvservice.db-wal
# sqlite_master: ['idx_expires_at', 'kv_store', 'sqlite_autoindex_kv_store_1']
```

The service created and migrated its own SQLite file as its mapped uid, and the
file survived three harness restarts (4096 → 16384 bytes as WAL checkpointed).
**Even with a valid principal every kv route would still deny** [inferred, from
`components/sdk/src/sdk/acl.py`]: ACL rules are only served by Registry's
`/api/acl/<id>`, `refresh()` failed, `rules` stayed empty, and `evaluate()`
returns False when nothing matches. There is no local ACL fallback.

**A `systemctl restart ams-harness` keeps ports and uids stable** [verified, n=1]:

```
=== before restart ===
kvservice   port=20002 uid=102048 pid=2010032 health=200
timeservice port=20003 uid=103072 pid=2010038 health=200
=== after restart ===
kvservice   port=20002 uid=102048 pid=2010381 health=200
timeservice port=20003 uid=103072 pid=2010387 health=200
=== all four services healthy? ===
hello 20000 -> 200 | pyhello 20001 -> 200 | kvservice 20002 -> 200 | timeservice 20003 -> 200
```

`scripts/pilot-api.sh` is idempotent [verified, n=2]: the second run took 14.2 s
against 24.7 s for the first, `uv sync --frozen` was a 1.9 s no-op, and the
ports, uids and database were unchanged.

---

## 2. The env the SDK needed with no registry and no auth

Read from `components/sdk/src/sdk/config.py` and confirmed by the run.

Required, or `load_from_env()` raises `KeyError`: `SVC_NAME`, `SVC_AUDIENCE`,
`SVC_SECRET`. Everything else has a default.

**`SVC_DEV=1` is the load-bearing variable.** It is consumed in exactly one
place, `Registry.start()`, which returns immediately in dev mode. Without it,
`start()` does a blocking `POST {REGISTRY_URL}/api/services/register` whose
failure raises `RegistryError` out of the FastAPI lifespan — uvicorn treats that
as a startup failure and the process exits. Under ams that would be a crash loop
to `max_retries`. It also suppresses the heartbeat thread, which otherwise logs
one WARNING on first failure and then an ERROR every 300 s
(`_FAILURE_LOG_INTERVAL`) for as long as the registry is unreachable.

**`REGISTRY_URL` / `AUTH_URL` must be overridden, not left at their defaults.**
The SDK defaults are the real `https://registry.lishuyu.app` and
`https://auth.lishuyu.app`. `SVC_DEV` does **not** gate kvservice's one-shot
`ACLClient.refresh()` in `setup_sdk()`, so a dev-mode kvservice with default
config still makes one real request to production Registry on every start. Both
are pointed at `http://127.0.0.1:1` so a replica cannot reach production even by
accident. Result, verbatim from the journal, 0.1 s and non-fatal:

```
[kvservice:stderr] acl refresh failed: [Errno 111] Connection refused
```

`SVC_ROOT_PATH` (`/kv`, `/time`) is only used for registry metadata and for
`sdk.fastapi._check_route_prefixes`. No route in either service starts with its
own mount prefix, so it produced no warning [verified].

`KV_DB_PATH` is required in practice: kvservice's default is
`/var/lib/{SVC_NAME}/kvservice.db`, and a service running as an unprivileged
mapped uid can neither create nor write `/var/lib`. Pointed at
`<service root>/data/kvservice.db`, deliberately **outside** `<root>/repo` so a
re-rsync of the monorepo cannot touch the data.

Not needed at all: `SVC_M2M_PUBLIC_KEY_PATH` (unset ⇒ no `M2MVerifier` is built),
`SVC_CAPABILITIES`, `GIT_COMMIT`, `SVC_ENDPOINT`, `SVC_PUBLIC_BASE`,
`SVC_HEARTBEAT_INTERVAL`. There is no JWKS fetch anywhere in the SDK, so nothing
else reaches the network at startup [verified by the zero-egress run].

---

## 3. Startup time and memory

Timings from the journal, spawn → uvicorn `Application startup complete`.

| | cold (first boot, 4 services at once) | warm restart |
|---|---|---|
| kvservice | 10.33 s | 6.03 s |
| timeservice | 9.63 s | 5.59 s |

`health=ok` follows startup by 0.16 s on the cold boot and by ~4.3 s on the
restart; the difference is the health poll landing between probes, not the
service. `start_period_s = 60` leaves generous margin over both. n=2 boots on a
1 vCPU box; another tenant's load would move these.

Memory, per cgroup:

| | first boot `memory.current` | steady `memory.current` | steady `anon` | `ps` RSS | limit |
|---|---|---|---|---|---|
| kvservice | 78 708 736 (75.1 MiB) | 42 663 936 (40.7 MiB) | 41 938 944 | 65 928 KiB | 150 M |
| timeservice | 73 596 928 (70.2 MiB) | 40 820 736 (38.9 MiB) | 40 267 776 | 61 504 KiB | 100 M |

The two columns differ because `memory.current` charges page cache to whichever
cgroup faults a page in first. On a genuinely cold box the first service to load
`libpython`, `pydantic_core` and `uvloop` pays for them; after a restart those
pages are already charged elsewhere. **The number that constrains the limit is
the cold one** [verified, n=1]: timeservice peaked at 70.2 MiB against its
100 M ceiling, ~30 % headroom. That is thin enough that a cold boot on a busier
box is the plausible failure. Recommendation: 128 M for a FastAPI +
`uvicorn[standard]` service, not 100 M. The upstream manifest's 100 M works
today only because systemd charges page cache differently than a fresh cgroup
per service does. `pids.current = 7` against a limit of 64 — comfortable.

---

## 4. Disk cost of the second monorepo copy and its venv

Free-space deltas via `statvfs` on `/home/harness/store` (the XFS reflink
volume), not `du` — shared extents make `du` meaningless here (D8).

Whole pilot, both services from nothing [verified, n=1]:

```
before: 5 595 566 080     after: 5 490 135 040     delta: 105 431 040  (100.5 MiB)
```

That includes first-time downloads into the shared `uv-cache` for ~20 packages
neither of the demo services needed (`pydantic-core`, `uvloop`, `watchfiles`,
`websockets`, `httptools`, …). To get the number that actually matters — what
one *more* replica costs once the cache is warm — a third copy was provisioned
into a throwaway state dir (`/home/harness/store/pilot-measure`, since removed)
and measured in two phases [verified, n=1]:

| phase | cost | apparent size |
|---|---|---|
| monorepo source copy | 32 903 168 B (31.4 MiB) | ~17.4 MiB |
| `uv sync --frozen` venv | 2 674 688 B (2.6 MiB) | 49 552 845 B (47.3 MiB) |

**The venv is effectively free: 2.6 MiB of new blocks for a 47.3 MiB
environment, 94.6 % shared.** D8's reflink design does exactly what it claims
for a uv project, not just for `uv pip install`.

**The source copy is not free, and it is now the dominant cost.** 31.4 MiB of
allocated blocks for a 17.4 MiB tree — thousands of small files rounded up to
4 KiB blocks, plus inode overhead. Nothing reflinks it, because `rsync` (and
`cp --reflink=never`, used to model it) writes fresh files. Per-service venv
apparent sizes for reference: kvservice 54 839 571 B, timeservice 54 417 304 B.

Consequence for a full migration [inferred]: ~19 Layer-1 services × ~35 MiB
≈ 665 MiB on a 6 GiB store, of which ~600 MiB is duplicated monorepo. Copying
the shared tree once and reflinking it per service (`cp -a --reflink=always`
from a harness-owned staging copy) would cut that to near zero, and is the one
optimisation worth doing before scaling this up.

---

## 5. Escalations

**Zero.** Across the entire pilot window (15:58 → 16:05, four services, three
harness restarts, two full script runs) the harness emitted **no escalation JSON
lines at all** on stdout:

```
# journalctl -u ams-harness.service --since '2026-09-02 15:58:00' -o cat | grep -c '^{'
0
```

Nothing to classify as noise-to-suppress, because nothing escalated. That is a
clean result for the two services — and also the finding below.

The only WARNING+ lines the harness itself logged were operator-initiated stops:

```
2026-09-02 16:01:03,127 ERROR ams.supervisor: timeservice exited code=None signal=15 uptime=120.07s
2026-09-02 16:01:03,148 ERROR ams.supervisor: kvservice  exited code=None signal=15 uptime=120.38s
```

Twelve such lines, all followed by `ignoring policy RESTART for <id>: operator
stopped it`. Correctly **not** escalated, but logged at ERROR. **Noise to fix at
the source, not to suppress**: a service the operator asked to stop exiting on
the signal the operator sent is the success path, and logging it at ERROR
trains a reader to ignore the level. It belongs at INFO when
`ServiceState.stopped_by_operator` is set.

### The real finding: a genuine service warning was classified INFO

The one line in the run that a human would want to see arrived like this:

```
2026-09-02 15:59:13,005 INFO ams.services: [kvservice:stderr] acl refresh failed: [Errno 111] Connection refused
```

`ams.events.classify` rated it INFO because the text contains no `WARN`/`ERROR`
token. The cause [verified by reading the source]: neither the SDK nor uvicorn
configures the root logger, so `sdk.acl`'s `logger.warning(...)` falls through to
`logging.lastResort`, which writes **the bare message with no level prefix**.
Uvicorn's own lines carry `INFO:` because uvicorn installs its own formatter for
its own loggers only.

So every `logger.warning`/`logger.error` from the SDK and from application code
in these two services is invisible to the decision policy. In this pilot that hid
one benign line. In production it would hide the heartbeat's
`heartbeat down for %.0fs; service still running but invisible to Registry`
ERROR, which is precisely the class of event the harness exists to escalate.
This is a **real gap, not noise** — see §6 item 6.

---

## 6. What a full Layer-1 migration would need

Grounded in `.claude/state/api-architecture.md` §2–§6 and in what this pilot hit.

1. **Deployer writes a `service.toml`, not a systemd unit.** The mapping is
   mostly mechanical: `process.exec` → `start.argv` (already a `--factory`
   uvicorn invocation), `working_dir` → `start.workdir`, `process.environment` →
   `[env]`, `restart`/`restart_sec` → `[restart]`, `memory_max` → `[limits]`,
   `registry.health_path` → `[health]`. Two things do not map. `${PORT}` becomes
   `${PORT_main}` and is allocated, not assigned. And the unit's hardcoded
   sandbox directives (`ProtectSystem=strict`, `PrivateTmp`, `ReadWritePaths`)
   have no declaration equivalent — the userns + service root replaces most of
   them, but that equivalence should be written down rather than assumed.
2. **Hot reload is missing and is the blocking gap.** A new or edited
   declaration is only picked up by `systemctl restart ams-harness`, and the unit
   is `KillMode=control-group`, so deploying one service restarts all of them
   [verified: three times in this pilot]. Today's Deployer restarts exactly the
   one service it deployed. Until the supervisor can add/replace a single service
   in place, ams is a downgrade in blast radius.
3. **Caddy snippet generation must read the allocated port.** Today `mount.port`
   is hand-assigned in `service.yaml` (9200–9219 used, no conflict detection —
   architecture map §4, pain point 4). Under ams the port comes from
   `state/ports.json` *after* registration. That is a genuine improvement (the
   allocator bind-probes) but it inverts the order: the caddy applier has to run
   after the harness has registered the service, and needs a way to read the
   allocation. A small `ams ports --json` subcommand would close this.
4. **`SVC_SECRET` delivery has no answer yet, and this pilot papered over it.**
   Production keeps it in `/etc/<name>/env` (0640 root:`<name>`) which the
   Deployer never reads — a deliberate trust boundary. This pilot put a
   throwaway secret verbatim into `service.toml`, which is world-traversable
   (0751) and agent-written. That is acceptable for a replica and unacceptable
   for Layer 1. The declaration needs an indirection — an `env_file` field the
   harness reads at spawn, or an `env_from` naming a harness-owned secrets dir —
   so the secret is never in a file the agent generates or logs.
5. **A data-directory convention.** `/var/lib/<name>` is unreachable for a mapped
   uid; every stateful service (per §1 of the architecture map, that is *all* of
   them) would need a `KV_DB_PATH`-style override, which is per-service manual
   work and easy to get wrong. Better: `ensure_service_root` always creates
   `<root>/data` and the harness exports `AMS_DATA_DIR`, so the convention is
   one env var per service instead of one absolute path per service. Note the
   knock-on: `bootstrap/05-db-backup.sh` globs `/var/lib/<name>/*.db` and would
   silently back up nothing after a migration — and Layer-1 DBs are already
   outside its `DBS=` list, which the architecture map flags as the platform's
   sharpest data risk.
6. **Services must log with a level prefix, or ams must parse per service.**
   See §5. Cheapest fix at the source: have `sdk.fastapi.setup_sdk` call
   `logging.basicConfig(format="%(levelname)s %(name)s: %(message)s")` when the
   root logger has no handlers. Do that before turning heartbeats on, or the
   first real production escalation this harness should catch will be scored
   INFO.
7. **Registry heartbeat means two health systems.** A migrated service runs with
   `SVC_DEV` unset, so it registers and heartbeats every 30 s while ams
   independently probes `/health` every 10 s. Decide which is authoritative
   before both are live; at minimum the harness's own probe traffic should be
   suppressed by the policy (already noted in PROGRESS.md) or it doubles the
   access-log volume Registry sees.
8. **Ownership handover on redeploy.** A provisioned service root belongs to its
   mapped uid, so the deployer cannot rsync into it without chowning back first
   — `scripts/pilot-api.sh` does exactly that as root. A rootless deployer would
   need `ams.userns.run_admin` (or an `ams stage <id>` subcommand) instead of
   `sudo chown`, which is the same shape as the existing narrow sudoers
   allowlist and should replace it rather than extend it.

---

## 7. Open / weak signals

- The 31.4 MiB source-copy figure is one measurement of one `cp -r
  --reflink=never` of one tree [weak]. XFS speculative preallocation and
  delayed allocation both move free-space deltas around; the direction (source
  copy ≫ venv) is solid, the exact ratio is not. Do not quote 31.4 MiB as a
  planning constant without a second run.
- `memory.current` on a cold boot vs a warm restart differed by ~34 MiB for both
  services [verified, n=1 each]. The page-cache-accounting explanation is
  consistent with `memory.stat` (`anon` barely moved: 41.9 MiB steady vs 75.1 MiB
  total on first boot) but was not isolated by a controlled experiment. Do not
  conclude "100 M is fine" from the steady-state column.
- Startup times were measured with four services contending for one vCPU. A
  single-service boot was never timed, so 5.6 s is an upper bound of unknown
  tightness.
- `uv sync --frozen` completed in 1.9–3.6 s against a warm uv cache. A genuinely
  cold cache (fresh box) was never measured; the `DEFAULT_TIMEOUT_S = 900`
  ceiling is untested for this path.
- kvservice's write path was never exercised end to end, because doing so
  honestly requires a reachable Auth and non-empty ACL rules. `PUT` returning
  401 proves the request reached the handler's auth check and nothing beyond it.
  Do not read "kvservice works under ams" as covering its write path.
- `scripts/deploy-racknerd.sh` rsyncs the whole ams checkout including the
  gitignored `api/` clone (~18 MiB) to `/home/harness/ams` [verified, n=1]. It is
  on the root filesystem, not the store, and harmless, but the deploy script
  should exclude `api/`. Not fixed here: out of this task's file scope.

## Gap 3 closed: SVC_SECRET now comes from the write-only store (2026-09-02, D16)

Both pilot declarations dropped `SVC_SECRET` from `[env]` and now carry
`secrets = ["SVC_SECRET"]` (names only). `scripts/pilot-api.sh` generates a
value per service **on the box** with `openssl rand -hex 32` and pipes it
straight into `ams secret set <id> SVC_SECRET` as the harness user; it is
skipped when `ams secret check <id>` already passes, so the script stays
idempotent. The value is never an argv element, never echoed, and lands only in
`<state>/secrets/<id>/SVC_SECRET`.

Live proof after `scripts/deploy-racknerd.sh` + `scripts/pilot-api.sh`
(racknerd, n=1 host, 2026-09-02 16:33 UTC, harness pid 2013640-ish generation):

| check | kvservice | timeservice |
|---|---|---|
| `GET /health` | 200 | 200 |
| `ams secret list <id>` | `SVC_SECRET` | `SVC_SECRET` |
| store file mode / owner / size | 0600 harness 64 B | 0600 harness 64 B |
| `secrets/<id>/` dir mode | 0700 harness | 0700 harness |
| `^SVC_SECRET` assignments in the installed `service.toml` | 0 | 0 |
| `^secrets = ` lines in it | 1 | 1 |
| `SVC_SECRET=` present in `/proc/<pid>/environ` | yes (pid 2013661, uid 102048) | yes (pid 2013667, uid 103072) |
| `ams secret check <id>` | `OK (1 secret(s) set)` | `OK (1 secret(s) set)` |

`hello` and `pyhello` came back with the unit and answered 200, so all four
services are healthy. The two stored values differ from each other (`cmp`), so
the generation really is per service. Each file is exactly 64 hex characters
with **no** trailing newline, which is the `set` newline rule working on
`openssl rand -hex 32 | ...`.

Leak audit (the point of the feature): nothing that could be a value reached any
observable surface. `journalctl --since -2h | grep -Eo '\b[0-9a-f]{64}\b'` →
empty; `journalctl -u ams-harness | grep SVC_SECRET` → empty; a recursive grep
for a 64-hex token across the state tree, excluding `secrets/` and service
roots, → empty. The script's own stdout prints only `generated and stored` /
`already set`. I never learned the values myself, which is the intended
property, so the audit is necessarily shape-based (64-hex) rather than
value-based — that is its one weakness, and it is inherent.

Not verified: rotation under load (`ams secret rm` + re-set while the service
runs — the new value only reaches the process at the next start, by design, and
nothing tests that the old process keeps running); a secret larger than a few
KiB; behaviour when the state dir fills mid-write (the temp-file path is
exercised by a unit test with a faked `os.replace`, not by a real ENOSPC).

### Re-confirmed on the later boot (2026-09-02 16:39:53 UTC)

The ctl agent redeployed after this pilot run, so the pids above belong to a
boot that no longer exists. Re-probed against the current one: all four services
answer 200, `SVC_SECRET=` is present in `/proc/2015508/environ` (kvservice, uid
102048) and `/proc/2015646/environ` (timeservice, uid 103072), both store files
are still 0600 harness, and the journal for that boot contains no 64-hex token.
The store survives a harness restart untouched, which is the expected behaviour
(it is state, not runtime), and re-running the pilot script would have skipped
generation because `ams secret check` passes.
