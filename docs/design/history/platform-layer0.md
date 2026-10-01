# Layer-0 bring-up on racknerd — 2026-09-02 (T3.2)

Registry, Auth and the Caddy gateway run as ams services under the live
`ams-harness.service`, beside the four services that were already there. The two
pilot Layer-1 services now register with the **replica** registry, heartbeat into
it, and are reachable through the gateway. Everything below is copied from a real
run; nothing is reconstructed.

Confidence marks: **[verified]** = observed directly, with n. **[weak]** = one
observation, plausible alternatives remain.

Artefacts: `src/ams/platform/layer0.py`, `scripts/platform-bootstrap.sh`,
`tests/test_platform_layer0.py`, `docs/design/history/diagnosis-layer0.md`.

---

## 1. What runs

`ams ctl status`, one row per service, after the final bring-up [verified]:

| id | status | healthy | pid | port | host uid | `memory.current` | `memory.max` | pids |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| auth | running | true | 2029729 | 20101 | 106144 | 99 426 304 (94.8 MiB) | 209 715 200 | 7 |
| caddy | running | true | 2029732 | 20180 | 107168 | 10 149 888 (9.7 MiB) | 125 829 120 | 7 |
| hello | running | true | 2029736 | 20000 | 100000 | 10 358 784 | 67 108 864 | 1 |
| kvservice | running | true | 2031479 | 20002 | 102048 | 53 145 600 (50.7 MiB) | 125 829 120 | 9 |
| pyhello | running | true | 2029743 | 20001 | 101024 | 11 284 480 | 67 108 864 | 1 |
| registry | running | true | 2029746 | 20100 | 105120 | 97 329 152 (92.8 MiB) | 209 715 200 | 9 |
| timeservice | running | true | 2031482 | 20003 | 103072 | 49 983 488 (47.7 MiB) | 157 286 400 | 9 |

Harness cgroup total `memory.current` = 397 295 616 (378.9 MiB) for all seven.

**Memory before / after** (`free -m`, MiB) [verified, n=1]:

| | total | used | free | buff/cache | available |
| --- | --- | --- | --- | --- | --- |
| before (4 services) | 1967 | 553 | 261 | 1350 | 1414 |
| after (7 services) | 1967 | 710 | 141 | 1312 | 1257 |

157 MiB for registry + auth + caddy. Store use went 973 M → 1.1 G (`df`), which
includes the bare mirror pushed to `<store>/upstream/api.git` and one canonical
checkout of the sha.

Registry and auth are the two biggest processes on the box at ~93–95 MiB each
against a 200 M cap. That is a **cold** number (first boot on a busy page cache);
the pilot showed cold `memory.current` runs ~35 MiB above steady because page
cache is charged to whoever faults a page in first. Do not treat 200 M as
generously sized — treat it as ~2× a cold peak measured once. Extrapolating
Q8's 14-process budget from these three is premature (n=1 per service).

## 2. Reachability

Direct, on loopback [verified]:

```
20100/health     -> 200   {"status":"ok","services_count":2}
20101 tcp        -> open  (auth has no health route; tcp is the honest probe)
20180/ams-health -> 200   ok
20002/health     -> 200
20003/now        -> 200
```

Through the gateway on `http://127.0.0.1:20180` [verified]:

```
/kv/health -> 200   {"status":"ok"}
/time/now  -> 200   {"now":"2026-09-02T18:35:54.144871+00:00"}
/time/     -> 200
/nope      -> 404   (the entry site's JSON catch-all)
```

The security headers survive the Jinja→Python port (PLAN-allin risk 2), checked
on a live response rather than only in the golden files:

```
$ curl -sI http://127.0.0.1:20180/kv/health
Content-Security-Policy: frame-ancestors 'none'
X-Content-Type-Options: nosniff
X-Frame-Options: DENY
```

Rendered `sites/kvservice.caddy`:

```
# kvservice — service on api.lishuyu.app/kv
handle_path /kv/* {
	header X-Frame-Options "DENY"
	header Content-Security-Policy "frame-ancestors 'none'"
	header X-Content-Type-Options "nosniff"
	reverse_proxy 127.0.0.1:20002
}
```

## 3. The replica registry knows about the pilot services

`GET /api/services` with the admin token read out of the SecretStore in process
(never printed, never in argv) [verified]:

```
GET /api/services -> 200
{"id":"kvservice","audience":"kvservice","capabilities":["keyvalue"],"status":"healthy",
 "last_seen":1788374173,"endpoint":"https://api.lishuyu.app/kv","version":"5ea3572..."}
   last_seen age: 11 s
{"id":"timeservice","audience":"timeservice","capabilities":["time"],"status":"healthy",
 "last_seen":1788374173,"endpoint":"https://api.lishuyu.app/time","version":"5ea3572..."}
   last_seen age: 11 s

discover/keyvalue -> 200 kvservice
discover/time     -> 200 timeservice
acl/kvservice     -> 200 6 rules (read/write × admin, service:*, user:*)
acl/timeservice   -> 200 3 rules (read anon, read user:*, write admin)
```

`last_seen` 11 s old against a 30 s heartbeat means the SDK heartbeat thread is
running against the replica, not merely that registration succeeded [verified].
`version` is the sha the tree was staged from, so the registry's own record and
`GIT_COMMIT` agree by construction. `endpoint` is still the **production** URL —
it comes from the manifest's `mount`, which the translator copies verbatim; the
replica has no public hostname, so nothing consumes it here. Worth fixing before
Phase B, because a client that discovers a service and follows `endpoint` would
leave the replica.

## 4. A real M2M token, minted here and verified by kvservice

The strongest single check in this transcript. `service_policies` has no write
API by design (upstream manages it through migrations), so one row was inserted
into the replica's `registry.db` through the admin namespace — an operator act,
recorded here rather than in code:

```
INSERT OR IGNORE INTO service_policies (caller, callee, allowed)
VALUES ('timeservice','kvservice',1)
```

Then, minting with timeservice's own `SVC_SECRET` (read from the SecretStore in
process) and calling kvservice **through Caddy** [verified, n=1]:

```
POST /api/m2m/token -> 200
  header : {'alg': 'RS256', 'typ': 'JWT'}
  claims : {'sub': 'service:timeservice', 'aud': 'kvservice',
            'iss': 'http://127.0.0.1:20101', 'iat': 1788374233,
            'exp': 1788460633, 'typ': 'm2m'}
  sig len: 256 bytes

PUT  /kv/pilot   no token  -> 401 {"detail":"authentication required"}
PUT  /kv/pilot   M2M token -> 204
GET  /kv/pilot   no token  -> 401
GET  /kv/pilot   M2M token -> 200 {"key":"pilot","value":"from-timeservice",...}
GET  /kv/pilot   tampered  -> 401   (last 6 chars of the signature changed)
```

What that proves, end to end: registry signed with the private key placed at
`services/registry/root/etc/jwt-rs256.pem` (0400, uid 105120); kvservice built
its `M2MVerifier` from **its own copy** of the public key at
`services/kvservice/root/etc/jwt-rs256.pub` (0444, uid 102048) with
`issuer=AUTH_URL`; the issuer in the token is the replica's auth address, so
signer and verifier agree on a value neither can read from the other's
declaration; the ACL rules the bring-up upserted resolved `service:*` to allow;
and a one-signature change is rejected. The 401/204/200 split is the evidence
the brief asked for, and the tampered case rules out "any bearer token works".

Key placement, as designed in D22 (no shared group, one copy per service):

```
auth/root/etc/        -r--------  106144  jwt-rs256.pem
                      -r--r--r--  106144  jwt-rs256.pub
registry/root/etc/    -r--------  105120  jwt-rs256.pem
kvservice/root/etc/   -r--r--r--  102048  jwt-rs256.pub
timeservice/root/etc/ -r--r--r--  103072  jwt-rs256.pub
```

## 5. What the bring-up produced

`python -m ams.platform.layer0 --repo-url /home/harness/store/upstream/api.git --ref main`,
final (idempotent) run, all 17 stages [verified]:

```
"stages": [fetch, materialize, bootstrap, allocate, stage-layer0, keys-layer0,
           provision-layer0, stop-layer1, stage-layer1, translate-layer1,
           provision-layer1, gateway, reload, health-layer0, identities,
           start-layer1, health-layer1],
"failed_stage": null,
"health":     {"registry": true, "auth": true, "caddy": true,
               "kvservice": true, "timeservice": true},
"identities": {"kvservice": false, "timeservice": false},   # false = 409, already there
"bootstrap":  {"created": [], "updated": [], "existing": [8 items]},
"reload":     {"added": [], "changed": [], "errors": {}, "unchanged": 7},
"sha": "5ea357204eb1754ef71ed68ad139e8a36bd78c1b"
```

A second run creating nothing and reloading nothing is the idempotence property
[verified, n=2].

The translated kvservice declaration, showing the replica wiring:

```toml
[env]
AUTH_URL = "http://127.0.0.1:20101"
GIT_COMMIT = "5ea357204eb1754ef71ed68ad139e8a36bd78c1b"
KV_DB_PATH = "/home/harness/store/state/services/kvservice/root/data/kvservice.db"
REGISTRY_URL = "http://127.0.0.1:20100"
SVC_M2M_PUBLIC_KEY_PATH = ".../services/kvservice/root/etc/jwt-rs256.pub"
SVC_ROOT_PATH = "/kv"
```

`/var/lib/kvservice/kvservice.db` was rewritten to `<root>/data/` by the
translator (the pilot's finding #5) with no hand-editing [verified]. **No
`SVC_DEV`**: this is the first time these services have talked to a real registry
under ams.

## 6. Source delivery: the local mirror path, not GitHub

`StevenLi-phoenix/api` is **private**. From racknerd, anonymous HTTPS reaches
GitHub but gets a 404 for the repo, and `git ls-remote` asks for a username and
fails:

```
$ curl -o /dev/null -w '%{http_code}' https://github.com/StevenLi-phoenix/api
404
$ su -l harness -c "git ls-remote --heads https://github.com/StevenLi-phoenix/api main"
fatal: could not read Username for 'https://github.com': No such device or address
```

So the harness holds no credential for it and none was created — a token in a
fetch URL is exactly what `sources.validate_url` refuses. `scripts/platform-bootstrap.sh`
instead pushes a bare mirror from the workstation to
`/home/harness/store/upstream/api.git` and points `SourceMirror` at that local
path, a shape `validate_url` already accepts. Everything downstream is the real
code path: `git clone --mirror`, `git fetch`, `git archive | tar` into
`<store>/src/api/<sha>/`, `cp -a --reflink=auto` into each service root through
the admin namespace.

Phase A therefore runs upstream **`main`** (`5ea3572`), which does **not** carry
the T1.4 SDK root-logger patch — that lives on the local `ams-platform` branch and
was never pushed. Consequence, visible in §7: SDK `logger.warning` lines arrive
with no level token and are classified INFO, exactly as the pilot found. Uvicorn's
own lines still carry `ERROR:`, which is why the crash-loop tracebacks below did
escalate.

## 7. Escalations during the whole window (18:08 → 18:36)

47 JSONL lines on the harness's stdout. Every one is classified; none is
unexplained.

| n | service | reason | window | classification |
| --- | --- | --- | --- | --- |
| 18 | kvservice | ERROR on stderr | 18:21:50–18:30:42 | **real** |
| 17 | timeservice | ERROR on stderr | 18:21:49–18:23:07 | **real** |
| 7 | caddy | WARNING on stderr | 18:09:16–18:21:31 | **noise** |
| 2 | hello | HealthChanged | 18:09:05, 18:22:17 | **real** |
| 2 | kvservice | ≥ max_retries | 18:23:07, 18:30:42 | **real** |
| 1 | timeservice | ≥ max_retries | 18:23:07 | **real** |

**Caddy's 7 WARNINGs are noise to suppress.** All three distinct texts are
start-up statements of fact about a configuration we chose:

```
{"level":"warn","logger":"admin","msg":"admin endpoint disabled"}
{"level":"warn","logger":"http","msg":"HTTP/2 skipped because it requires TLS"}
{"level":"warn","logger":"http","msg":"HTTP/3 skipped because it requires TLS"}
{"level":"warn","msg":"exiting; byeee!! 👋","signal":"SIGTERM"}
```

`admin off` is D21/Q3's deliberate choice and plain HTTP is Phase A by design, so
Caddy warns once per start about two things the operator asked for. The SIGTERM
line is an operator-initiated stop — the same class T1.3 fixed for
`ServiceExited`. All four belong in T3.3's Caddy rules as suppressions. The
`[logging] format = "json"` hint worked: the level came out of the record, not
out of a text heuristic [verified].

**kvservice / timeservice ERRORs are real and were caused by this bring-up.**
Three distinct causes, all understood:

1. `sdk.registry.RegistryError: register failed: 404 for .../api/services/register`
   — a translated declaration has no `SVC_DEV`, so the SDK registers inside the
   FastAPI lifespan; a 404 there is a uvicorn startup failure. This is the
   ordering constraint `layer0` exists to enforce (identities before start). It
   fired because the *harness was restarted manually* during the repair in
   §8, which started both services outside `layer0`'s sequence.
2. `ModuleNotFoundError: No module named 'fastapi.datastructures'` /
   `'httpcore._async.connection_pool'` — `uv sync` was rewriting the venv while
   the process was importing from it. Same cause: the manual restart put the
   services back up inside the window `layer0` deliberately keeps them down.
   **This is the sharpest argument for the stop-before-provision rule** and worth
   remembering for T3.1: a sync that provisions a running service produces
   import errors that look like a broken dependency and are not.
3. `acl refresh failed: 401 Unauthorized for .../api/acl/kvservice` — the SDK's
   one-shot ACL load before the identity existed. Self-corrects; the M2M write in
   §4 proves the rules loaded afterwards.

**hello's two `HealthChanged` are real and diagnostic.**
`http 127.0.0.1:20000/ HTTP 404 File not found` at 18:09:05 and 18:22:17 — one
per bring-up run, before the fix in §8. `hello` is a `python -m http.server`
demo, and it 404s when it cannot traverse to its own workdir. Those two lines are
the traversability regression showing up in a service nobody was looking at,
which is precisely what the escalation channel is for. Both cleared; `GET /`
returns 200 now.

## 8. Two defects found by this gate, both fixed

This is **not** PLAN-allin risk 4. Registry and auth start rootless with their
data outside `/var/lib` on the first attempt, once the harness would let them be
registered. Full analysis in `docs/design/history/diagnosis-layer0.md`; summary:

1. **`UidAllocator` was not idempotent across processes.** The harness holds one
   allocator for its lifetime and never re-reads `uidmap.json`, so blocks carved
   by `layer0` (a second process) were re-carved differently on `ams ctl reload`.
   `ensure_service_root`'s recursive chown then ran in a namespace with no
   authority over the files already staged, and both Layer-0 services were
   skipped. `allocate()` now re-reads on a cache miss. **This would have broken
   T3.1 too** — its sync loop is `ams provision` (a separate process) then
   `ams ctl reload`.
2. **`StateDir.ensure()` re-narrowed `services/` to 0750**, silently removing the
   `o+x` that `ams.cli._ensure_traversable` adds so a service uid can resolve its
   own workdir by path. `ams.platform.bootstrap` calls `ensure()` *while services
   are running*, so every bring-up broke the next spawn of every service on the
   box with `PermissionError` on its own interpreter — and made `hello` 404.
   0750 is now a floor that preserves a deliberate `o+x`.

Live repair, with the unit stopped so the harness could not write over it:
`uidmap.json` rewritten to the mapping the on-disk trees actually have,
`services/caddy/root` (created empty under the wrong block) removed,
`chmod o+x services/`, restart, re-run.

## 9. Unverified / open

- **n=1 for every number here.** One box, one bring-up, no soak. The cold
  `memory.current` figures for registry and auth are single observations on a
  page-cache-contaminated host.
- **Auth is only TCP-probed.** It has no health route, so "healthy" means
  "accepting connections". No user token has been minted; OAuth is out of replica
  scope, so no account can be created and the user-JWT path is **untested** (n=0).
- **`registry-runtime.db`** exists and heartbeats land in it, but nothing has
  verified the runtime/catalog split beyond `last_seen` moving.
- **The gateway is loopback-only.** `entry_host=127.0.0.1`, no TLS, no 80/443, no
  subdomain sites (neither pilot manifest uses one), and no static mounts. The
  subdomain and static paths in `gateway.py` are still golden-file-only.
- **`endpoint` in the registry points at production** (§3). Harmless in the
  replica; wrong before Phase B.
- **The M2M policy row was inserted by hand.** There is no code path that seeds
  `service_policies`, in this repo or upstream outside migrations. T4.1 needs an
  answer for the fleet.
- **Caddy has never reloaded a changed config here** — `write()` reported no
  changes on the second run, so the restart-to-apply path (Q3) is exercised only
  by the initial start.
