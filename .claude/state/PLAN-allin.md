# PLAN-allin — ams becomes the platform runtime (Phase A: full replica on racknerd)

Scope: the whole platform as a **replica on racknerd** — Layer 0 (registry, auth) and the Caddy
gateway run as ams services, rootless, high ports, plain HTTP on loopback. Production untouched.
Phase B (cutover) is listed, not scheduled. Inputs verified 2026-09-02: D1–D17, PROGRESS,
pilot-api.md, api-architecture.md, and a full read of `api/` (schema, templates, orchestrator,
appliers, SDK, bootstrap, 21 manifests).
**declaration** = ams `service.toml`; **manifest** = api `service.yaml`; **sidecar** = platform-only JSON.

---

## Waves

Every task ends with: local `pytest -q` + `ruff` green, `scripts/remote-test.sh <own-subdir>` green, a live
check where marked, a `PROGRESS.md` record, and every non-trivial choice in `DECISIONS.md` with its rejected
alternatives. `(live)` = touches racknerd; only one `(live)` task per wave writes the shared state dir.

### Wave 1 — foundations (4 parallel, no cross-deps)

**T1.1 source delivery** — S/M. Owns: `src/ams/platform/{__init__,sources}.py`, `tests/test_platform_sources.py`, `tests/linux/test_platform_sources.py` (subdir `ams-src`).
Build (Q1 below): bare mirror `<store>/repos/api.git`; `fetch(ref) -> sha`; `materialize(sha) ->
<store>/src/api/<sha>/` via `git archive | tar -x` (two Popen, no shell); `stage(sha, root)` =
`cp -a --reflink=auto` into `<root>/repo` **inside the admin ns** (`run_admin`), then chown to the block;
`gc(keep=2)`. Done: unit tests over a throwaway local bare repo; a Linux test asserting a second `stage`
of the same sha into a second root grows the filesystem by <2 MiB (`df` before/after) for a ~17 MiB tree —
i.e. reflink held. Re-staging the same sha is a no-op.

**T1.2 manifest translator** — L. Owns: `src/ams/platform/translate.py`, `tests/test_platform_translate.py`, `docs/{manifest-translation,platform-sidecars}.md`. Inputs: Q2; the deployer's `service.schema.json`; all 21 manifests.
**First deliverable, before any code: `docs/platform-sidecars.md`** fixing the JSON shape of `mount.json`,
`registry.json`, `platform-state.json` — waves 2/3 code against it, so it must land early.
Build: `translate(manifest_yaml_text, ctx) -> (ServiceDecl, MountSpec, RegistrySpec)`. Stdlib has no
YAML, so write a **restricted YAML subset parser** (block maps, block/flow seqs, scalars, quotes,
comments) that *rejects* everything else — sufficient for all 21 manifests; anchors/multi-doc raise.
Done: a table-driven test asserting the declaration for all 21 manifests byte-exact against golden files;
every unsupported construct raises `TranslateError` naming the field path; `ams validate` accepts all 21.

**T1.3 core gaps in ams** — M. Owns: `src/ams/{userns,schema,events,decision,supervisor,cli}.py`, `docs/service-declaration.md`, `tests/test_{schema,events,decision,supervisor}.py`, `tests/linux/test_userns.py` (subdir `ams-core`).
Build: (a) `ensure_service_root` always creates `<root>/data`, harness exports `AMS_DATA_DIR`;
(b) `[logging] format = "auto"|"level-prefix"|"json"|"plain"`, honoured by `events.classify` (json:
`level`/`severity` from a guarded `json.loads`, trusted for severity only);
(c) `severity_of(ServiceExited)` is INFO for an operator-initiated stop — kills the routine
ERROR-per-reload noise; (d) `DefaultPolicy` suppresses log lines caused by the harness's own probe.
Done: a unit test per item, each verified to fail against the pre-change code; a Linux test asserting
`AMS_DATA_DIR` exists and is writable from inside a spawned service.

**T1.4 api-repo branch: SDK log levels** — S. Owns **only** `api/components/sdk/src/sdk/fastapi.py`, on a new branch `ams-migration` in `api/`. Never pushed.
Build: in `setup_sdk`, if the root logger has no handlers, `logging.basicConfig(level=INFO,
format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)`. Additive; composes with `sdk/cls.py`
(which only adds a handler and lowers the level).
Done: `git -C api diff --stat` = one file, <10 lines; a test shows `logger.warning` reaching stderr as
`WARNING name: msg`; **no push**, confirmed by `git -C api status -sb`.

### Wave 2 — Layer 0, gateway, registry client, backup (4 parallel; all depend on W1)

**T2.1 Layer-0 bootstrap** — L. Deps: T1.2 (sidecars), T1.3 (data dir). Owns: `src/ams/platform/bootstrap.py`, `examples/platform/layer0/{registry,auth}/service.toml`, `tests/test_platform_bootstrap.py`.
Build: `ams platform bootstrap` — RS256 keypair once via `openssl genpkey`/`rsa -pubout` (argv, no
shell) into `<store>/platform/`; `REGISTRY_ADMIN_TOKEN`, `AUTH_SESSION_SECRET`, `AUTH_PAT_VERIFY_TOKEN`,
`AUTH_M2M_SECRET` via `secrets.token_hex(32)` straight into the SecretStore (never printed, never argv);
write the registry/auth declarations (uv sync mode, `<root>/data/*.db`, `<root>/etc/jwt-rs256.{pem,pub}`
placed by `run_admin` at 0400/0444). Idempotent when `SecretStore.missing` is empty.
Done: two runs produce identical file hashes and no new secret writes; `ams validate` passes on both
declarations; a test asserts no generated value reaches stdout, stderr or a log record.

**T2.2 gateway generator + Caddy binary** — M. Deps: T1.2 (`mount.json`). Owns: `src/ams/platform/gateway.py`, `deploy/install-host.sh`, `examples/platform/caddy/service.toml`, `tests/test_platform_gateway.py`.
Build: render `<state>/gateway/Caddyfile` + `sites/<id>.caddy` (0644 in a 0755 harness dir — the Caddy
uid reads them with no chown). Path mounts → `handle_path /p/*` on one entry site; subdomain mounts → own
site blocks; static → `root` + `file_server` + SPA fallback. Port the deployer's `caddy.snippet.j2` header
block, `default_csp`, `csp_map` (locationservice, pages) and `admin_cors_map` **verbatim as data**.
`install-host.sh` gains a pinned, sha256-verified static Caddy download into `<store>/bin/caddy`.
Done: golden-file tests for a path mount, a subdomain mount and a static site asserting every security
header from the template survives; `caddy validate --config <rendered>` exits 0 on racknerd.

**T2.3 registry client** — M. Deps: T1.2 (`registry.json`). Owns: `src/ams/platform/registryclient.py`, `tests/test_platform_registryclient.py`.
Build: stdlib `urllib.request`. `create_identity(id, secret)` = `POST /api/services` with `X-Admin-Token`
+ `X-Service-Secret` (409 → success; replaces `svc-init.sh`); `upsert_acl(id, rules)` = `POST /api/acl`,
retrying only 502/503/504 on the deployer's `(1,2,4,8,15,30,60,60,60)` backoff; `wait_healthy(url, deadline)`.
Done: tests against a stdlib `http.server` fake covering 200/409/502-then-200/403, plus one asserting the
secret never reaches a log record or an exception message.

**T2.4 backup + restore drill** — M. Deps: T1.3 (data dir). Owns: `src/ams/platform/backup.py`, `deploy/ams-platform-backup.{service,timer}`, `tests/test_platform_backup.py`, `tests/linux/test_platform_backup.py` (subdir `ams-bk`).
Build: for every service with a non-empty `<root>/data`, `sqlite3 <db> ".backup <tmp>"` **inside the
admin ns** (the db is service-owned; the harness cannot read it otherwise) → `gzip -9` → `rclone copyto`
to `r2:$BACKUP_BUCKET/daily/<id>/<id>-YYYYMMDD.db.gz` → `rclone delete --min-age 14d`. rclone credentials
live in the SecretStore under pseudo-service `_platform` and are passed as `RCLONE_CONFIG_R2_*` **env
vars**, never a config file. rclone is a static download into `<store>/bin`.
Done: **the restore drill is the deliverable** — a Linux test that writes a row, backs up, deletes the db,
restores from the gz and asserts the row. A dry-run mode proves the rclone argv without network.

### Wave 3 — the sync loop and live Layer 0 (4 parallel; depend on W2)

**T3.1 `ams platform sync`** — L. Owns: `src/ams/platform/{sync,cli}.py`, the `platform` subparser block in `src/ams/cli.py`, `deploy/ams-platform-sync.{service,timer}`, `tests/test_platform_sync.py`. Build: one-shot process on a 60 s systemd timer as the harness user (not in the supervisor loop — D17
keeps blocking work out of it). Per-service state machine in `<state>/platform/state.json`:
`fetched → translated → provisioned → declared → reloaded → registered → healthy`, every transition
escalating on failure through the existing JSONL channel. Ends with `ams ctl reload`.
Done: a run against a fake repo + fake registry drives every state; a failure injected at each transition
leaves the state file at the right stage and emits exactly one escalation; a second run is a no-op.

**T3.2 (live) Layer-0 bring-up** — M. Owns: `scripts/platform-bootstrap.sh`, `.claude/state/platform-layer0.md`, and on racknerd only `<state>/services/{registry,auth,caddy}`. Done: registry, auth and Caddy healthy under `ams-harness.service` beside the four existing services; a real
M2M token minted by registry and verified by a service against its own `jwt-rs256.pub`; `curl` through the
Caddy port reaches a pilot service; transcript with pids, uids, ports and `memory.current`.

**T3.3 platform policy + escalation** — M. Owns: `src/ams/platform/policy.py`, `tests/test_platform_policy.py`. Build: a `DecisionPolicy` over the sync state machine — a service failing its health gate for N minutes
after a sync escalates with both shas; a repeated translate failure escalates once, not per tick; Caddy JSON
logs classified via the T1.3 `[logging] format="json"` hint.
Done: table-driven tests over synthetic event streams; no cause ever escalates twice.

**T3.4 static sites + third-party secret names** — S/M. Owns: `src/ams/platform/static.py`, `api/apps/*/service.ams.toml` (branch `ams-migration`), `tests/test_platform_static.py`. Build: `kind=static` produces **no ams service** — the site is staged to `<state>/platform/static/<name>/`
(0755 harness) and the gateway serves it with `file_server`. Plus an optional `service.ams.toml` beside a
`service.yaml` listing extra secret **names** the manifest cannot express, folded into `secrets = [...]`.
Done: `files-web` and `llm-web` render and serve; a service declaring an unset extra secret fails only itself.

### Wave 4 — full fleet, soak, hardening (4 parallel)

**T4.1 (live) fleet bring-up + resource report** — L. Owns racknerd's state dir for all Layer-1 ids and `.claude/state/platform-fleet.md`. Done: the 10 `services/*` + timeservice up, registered, gateway-routed, `GET /health` 200 through Caddy for
each; a table of per-service `memory.current` and steady-state RSS; then the 7 remaining apps attempted, with
the outcome reported honestly (n=1 per service).

**T4.2 (live, read-only on services) backup drill** — M. Owns `<state>/platform/backup/` and `.claude/state/platform-backup-drill.md`. Done: a real R2 round trip for every service with a data dir, then a restore of one db into a scratch path
with a row-count comparison. Never writes a live service's data dir.

**T4.3 failure injection + rollback** — M. Owns `tests/linux/test_platform_e2e.py` (subdir `ams-e2e`) and `src/ams/platform/rollback.py`. Done: kill a service mid-sync, corrupt a manifest, point at a bad sha, exhaust a memory cap — each produces
one escalation and touches no other service; `rollback(id)` re-points one service at the previous sha and the
health gate goes green.

**T4.4 docs + Phase-B audit** — S. Owns `README.md`, `CLAUDE.md`, `docs/platform.md`, `.claude/state/phase-b-prereqs.md`. Done: `docs/platform.md` explains the loop end to end; the Phase-B list below becomes a checklist carrying
the evidence Phase A produced for each item.

---

## Q&A (recommendation, then rejected alternatives)

**Q1 Source delivery. Recommend: bare mirror + one canonical checkout per sha + reflink copy per service,
polled by `git fetch` every 60 s from the one-shot sync process.** The pilot's per-service `rsync` cost
31 MiB of blocks each (no shared extents) — 20 services would be ~620 MiB of a 6 GB store for 20 copies of
one 17 MiB tree. `cp -a --reflink=auto` from a canonical checkout on the same XFS mount shares extents (D8),
so the fleet costs ~1 tree. *Rejected:* `git worktree` per service — worktree metadata lives in the
harness-owned bare repo while the checkout must be chowned to the service uid, so git would manage a tree it
can no longer touch; no gain over a copy. *Rejected:* webhook-as-a-service — it needs an inbound port, a
Caddy route and an HMAC secret, and it runs as a mapped uid that **cannot** open the 0600 harness-only
control socket (D4/D16); a second looser socket invents an auth surface for a 60 s latency win.
*Assumption:* the harness reaches github.com over HTTPS. A fetch failure escalates; it does not crash.

**Q2 Translation.** `name→id` (all 21 fit ams's 31-char limit; longest is 19), `display_name→name`.
`process.exec` split on spaces → argv, first token rewritten from `/srv/<n>/…/.venv/bin/uvicorn` to bare
`uvicorn` (runtime_env prepends the venv bin to PATH), `${PORT}`→`${PORT_main}`. `process.working_dir`
`/srv/<n>/<rel>` → `repo/<rel>`. `process.environment` → `[env]`, plus injected `SVC_NAME`, `SVC_AUDIENCE`,
`PORT=${PORT_main}` (plain `PORT` is not reserved; the `PORT_` *prefix* is), `GIT_COMMIT`,
`SVC_CAPABILITIES`, `SVC_DISPLAY_NAME`, `SVC_OWNER`, `SVC_HEALTH_PATH`,
`SVC_M2M_PUBLIC_KEY_PATH=<root>/etc/jwt-rs256.pub`, and replica-local `REGISTRY_URL`/`AUTH_URL`.
Any env value under `/var/lib/<n>/` is rewritten to `<root>/data/` — the pilot's finding #5; without it every
stateful service (all of them) needs hand-editing. `restart` `no|on-failure|always` →
`never|on-failure|always`; `restart_sec→backoff_s`; `memory_max→[limits].memory_max`;
`registry.health_path` → `[health] kind="http"`. `deploy.install` is matched against the one recognised form
`cd <dir> && uv sync` → `runtime.kind="uv", sync=true`; **any other install command raises** (D1 forbids
shell strings). `mount` and `acl` do **not** enter the declaration — they go to sidecars. `manual_restart`
becomes a sidecar flag the sync loop honours (skip the post-reload restart), never `restart.policy`.
*Location:* a `src/ams/platform/` subpackage. *Rejected:* a plugin — no loader exists, one is speculative
(YAGNI). *Rejected:* `[gateway]`/`[acl]` inside `ServiceDecl` — a supervisor that knows about HTTP mounts and
ACLs stops being a supervisor (D7), and every non-HTTP service carries dead tables.
*Secrets:* the sync loop generates `SVC_SECRET` into the SecretStore when missing (replacing `svc-init.sh`)
and creates the registry identity with it. Third-party keys are absent from the manifest (production appends
them to `/etc/<n>/env` by hand): they come from `service.ams.toml` (names) plus one `ams secret set`.

**Q3 Gateway. Recommend: a pinned static Caddy binary in `<store>/bin`, run as an ams service on an
ams-allocated high port, config regenerated into `<state>/gateway/` and applied by `ams ctl restart caddy`.**
Config files are 0644 in a 0755 harness dir, readable by the Caddy uid (a harness file is `nobody`-owned
inside the ns but world-readable; the 0600 secrets are not — D4). *Rejected:* `apt install caddy` — it brings
a root systemd unit, a `caddy` system user and 80/443 binding, none of which Phase A wants. *Rejected:*
`caddy reload` via the admin API — the admin endpoint on shared loopback is open to every local process, and
a unix admin socket is created by the Caddy uid, which the harness uid cannot connect to. Restart is <1 s and
reuses D17 machinery; the admin socket is the Phase-B refinement, not deleted. The deployer's header block,
`default_csp`, `csp_map` and `admin_cors_map` are ported verbatim and pinned by golden-file tests.

**Q4 Layer 0.** Registry: `REGISTRY_{DB_PATH,RUNTIME_DB_PATH,MIGRATIONS_DIR,JWT_PRIVATE_KEY_PATH,
JWT_ISSUER,ADMIN_TOKEN}`. Auth: `AUTH_{DB_PATH,MIGRATIONS_DIR,JWT_PRIVATE_KEY_PATH,JWT_PUBLIC_KEY_PATH,
JWT_ISSUER,SESSION_SECRET,PAT_VERIFY_TOKEN,COOKIE_DOMAIN}` + `AUTH_INSECURE_COOKIES=1` (plain HTTP replica)
+ GitHub OAuth ids it cannot exercise here (placeholders; OAuth is out of replica scope).
**`/etc/auth/jwt-rs256.pub` is unreachable for a mapped uid**, so the key is materialised per service at
`<root>/etc/jwt-rs256.pub` (0444) by `run_admin` and `SVC_M2M_PUBLIC_KEY_PATH` points there. Registry gets
its own 0400 copy of the private key under its own root rather than production's shared `auth` unix group —
stricter, and needs no group. *Rejected:* a `[files]` table mapping secrets to files — it duplicates the
SecretStore and needs supervisor spawn-path changes; revisit if a third caller appears.
`08/09-*-init.sh` → `ams platform bootstrap`; `svc-init.sh`'s identity creation → `create_identity`; the
deployer's ACL upsert → `upsert_acl`.

**Q5 Log severity. Recommend both, with the SDK doing the work.** (a) `setup_sdk` calls
`logging.basicConfig(format="%(levelname)s %(name)s: %(message)s")` when the root logger has no handlers —
~4 additive lines fixing every Python service at once. A level that was never printed cannot be recovered by
any heuristic, so the source is the only honest fix. (b) the per-declaration `[logging]` hint is still needed
because the **gateway is not an SDK service** — Caddy emits JSON. (c) all-stderr-is-WARNING stays rejected:
uvicorn access logs go to stderr.

**Q6 Data + backup.** `<root>/data/` per service, `AMS_DATA_DIR` exported, `sqlite3 .backup` in the admin ns,
gzip, rclone to R2 with `RCLONE_CONFIG_R2_*` from the SecretStore, 14-day retention, 04:10 UTC timer. Covers
**every** service with a data dir, not production's hardcoded three — which api-architecture.md names as the
platform's sharpest data risk. The restore drill is a Linux test, so "we have backups" becomes checked.

**Q7 The agent loop.** Mechanical (ams code): fetch, translate, provision, declare, reload, render gateway,
create identity, upsert ACL, health gate, escalate. Policy (the agent): whether a translate failure means
fix-the-manifest or extend-the-translator; whether a crash-looping service is rolled back or left failing
loudly; approving resource caps for a new service; anything touching production. The loop is a **one-shot
process on a 60 s timer**, not a branch inside `run_forever` — `git fetch` and `uv sync` block for seconds to
minutes and the supervisor loop is single-threaded (D17).

**Q8 Resource budget (1 vCPU / 2 GB; ~60 MB RSS per uvicorn, n=1 from the pilot).** Recommend **14
processes** for wave 3 + T4.1: registry, auth, caddy, the 10 `services/*`, timeservice (~900 MB); the
remaining 7 apps are then attempted as a measured soak and reported, not assumed. Caps: manifest `memory_max`
where present, else 150M, with a **120M floor** — the pilot raised kvservice from 100M to 150M precisely
because 100M sat near the page-cache line. `pids_max=64`, `cpu_max="40%"` (a ceiling, deliberately
over-committed across 14 services on one core). Uid blocks: 21 of 64. **Risk to watch:**
`Assembly.start_all()` starts everything at once and 20 simultaneous uvicorn imports on one core will blow
past `start_period_s` — set `start_period_s=120`, and treat a staggered start as a follow-up if T4.1 sees it.

**Q9 Phase B prerequisites — listed only, not scheduled.** TLS and 80/443 (rootless Caddy cannot bind them:
`CAP_NET_BIND_SERVICE` on the binary, a systemd socket unit, or an authbind-style front); DNS and the real
hostnames; migration of every Layer-1 `/var/lib/<n>/*.db` into `<root>/data` with a verified restore; the
real registry/auth DBs and the existing RS256 keypair, not fresh ones; the third-party keys hand-written in
`/etc/<n>/env`; a service deletion path (`ams rm <id>`: root + uid block + secrets + registry DELETE) that
exists in neither system today; and a decision on which health system is authoritative once the registry
heartbeat and the ams probe are both live. Rollback: leave the deployer and its systemd units installed but
disabled, with a documented one-command restore per service until a soak window passes.

---

## Risks

1. **The YAML subset parser silently mis-parses a manifest.** Golden-file tests over all 21 manifests;
   *reject* rather than guess on any construct outside the subset.
2. **CSP/security headers lost porting Jinja to Python.** Golden-file assertions per header per mount type —
   the one failure nothing else would catch.
3. **2 GB is not enough for 21 services.** Tiered bring-up with measured `memory.current` reported honestly;
   the 60 MB figure is n=1 and page-cache-contaminated (pilot §7), not a planning constant.
4. **Registry/auth may not start rootless with data outside `/var/lib`.** T3.2 is a live gate before any
   Layer-1 work depends on it; a failure there is a diagnosis file, not a re-plan.
5. **Auto-generated `SVC_SECRET` / identity creation could reach production** on a wrong `REGISTRY_URL`.
   The translator hard-fails on any registry/auth URL that is not loopback in Phase A.
6. **`git fetch` hangs.** Bounded subprocess timeout; sync is a separate process, so it cannot stall the loop.
7. **One live state dir, several agents.** One `(live)` task per wave, named service-id ownership.

## What stays manual

Third-party API keys (`ams secret set` once per service); GitHub OAuth in the replica (no public callback);
DNS/TLS/80/443; the production droplet in every respect; `ams rm <id>` (no service-deletion path exists in
either system — Phase B); approving resource caps and rollbacks; pushing the `ams-migration` branch in `api/`.
