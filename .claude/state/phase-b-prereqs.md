# Phase B prerequisites — production cutover checklist

Phase A built a **replica on racknerd**. Phase B moves the production droplet
onto ams. This file turns PLAN-allin Q9's list, plus everything Phase A actually
learned, into items an operator can work through. It is **not scheduled** and
nothing here is authorised — several items need the user's explicit sign-off.

Each item: what it is · **Evidence** Phase A produced (cited) · **Still
required** · **Risk** · **Rollback**.

Confidence marks follow the repo convention: **[verified]** = observed, with n;
**[weak]** = one observation or a reasoned claim, alternatives remain.

---

## A. Edge and naming

### 1. TLS and ports 80/443

A rootless Caddy cannot bind privileged ports. Phase A runs plain HTTP on a
high port and that is one flag, not an architecture.

- **Evidence.** `GatewayConfig.plain_http` emits `auto_https off` plus
  `http://<host>:<port>` labels; setting it False emits bare hostnames and
  nothing else changes, pinned by the golden scenario `tls` (DECISIONS D21).
  Subdomain mounts are already real site blocks reachable by Host header, so
  the flip needs no re-architecture. Live: `.claude/state/platform-layer0.md`
  §2, gateway answering on `127.0.0.1:20180` [verified, n=1].
- **Still required.** Pick one of: `CAP_NET_BIND_SERVICE` on
  `<store>/bin/caddy` (a file capability the harness cannot set itself); a
  systemd socket unit passing 80/443 in; or an authbind-style front. Then a
  certificate story — ACME needs outbound plus a reachable challenge, and
  `auto_https off` has to come back on.
- **Risk.** A file capability on a binary in a harness-writable store directory
  is a privilege the harness can then re-point. A socket unit means systemd
  owns the listener and the Caddy service's restart semantics change.
- **Rollback.** Keep production's existing root Caddy installed and its config
  intact until the ams gateway has served real traffic through a soak window.

### 2. DNS, real hostnames, and the registry `endpoint` column

- **Evidence.** The replica's registry rows carry the **production** URL
  (`https://api.lishuyu.app/kv`) because it comes from the manifest's `mount`
  and the translator copies it verbatim; the replica has no public hostname so
  nothing consumes it (`.claude/state/platform-layer0.md` §3) [verified].
- **Still required.** Decide whether `endpoint` is derived from the gateway
  config or stays manifest-verbatim. A client that discovers a service and
  follows `endpoint` in a replica would leave the replica — harmless there,
  wrong the moment two environments exist at once.
- **Risk.** Cross-environment traffic that looks like it worked.
- **Rollback.** `endpoint` is registry data, updatable by an admin call; no
  service restart needed.

---

## B. Data

### 3. Migrating every `/var/lib/<n>/*.db` into `<root>/data`

- **Evidence.** The translator rewrites any env value under `/var/lib/<n>/` to
  `<root>/data/` with no hand-editing — confirmed live for
  `KV_DB_PATH` (`.claude/state/platform-layer0.md` §5) [verified, n=1].
  `<root>/data` is created 0750 by `ensure_service_root` and exported as
  `AMS_DATA_DIR` (D19/T1.3).
- **Still required.** For each stateful service: stop it, copy the production
  db into the ams service root through the admin namespace, chown to the uid
  block, start, verify row counts. The copy is an operator act; nothing in ams
  does it.
- **Risk.** The harness uid **cannot** read `<root>/data` by design, so a
  botched chown is invisible until the service fails to open its own database.
- **Rollback.** The production db is untouched until the old unit is disabled;
  keep both until the ams copy has served writes.

### 4. A verified restore, not just a backup

- **Evidence.** The restore drill is a Linux test: write a row, snapshot,
  delete the db, restore from the gzip, assert the row (D23, T2.4). `restore`
  only ever writes a scratch path and raises unless `PRAGMA integrity_check`
  is `ok`.
- **Still required.** The R2 **transport** is untested — no credentials were
  available and none were sought; `upload` and `prune` are pinned by argv
  equality only, **n=0 network hops** (D23 open signal). T4.2's live drill is
  the check. `api-architecture.md` §7 records that production has never done a
  full restore drill either, across ~2 months of daily snapshots.
- **Risk.** "We have backups" is currently a claim about snapshotting, not
  about the round trip.
- **Rollback.** None needed; the drill is read-only against live services.

---

## C. Trust root

### 5. The real registry and auth databases, and the existing RS256 keypair

- **Evidence.** Phase A generated a **fresh** keypair into `<store>/platform/`
  and fresh admin/session secrets (D22, T2.1). The JWT issuer is the replica's
  loopback auth URL, verified against `kvservice`'s
  `M2MVerifier(..., issuer=config.auth_url)` — signer and verifier agree only
  because Layer 0 issues under the same string (D22) [verified, n=1 service
  inspected; the other 20 unread on this point].
- **Still required.** Import production's keypair, `registry.db` and `auth.db`
  instead of generating new ones, and set the issuer to
  `https://auth.lishuyu.app`. `ensure_keypair` never rewrites an existing pair,
  so placing the real one first is sufficient — but note `place_jwt_key`'s warm
  path trusts owner+mode as a proxy for content (the 0400 private copy is
  unreadable to the harness by design), so if the store keypair is ever
  replaced, every per-service `<root>/etc/jwt-rs256.*` must be deleted too.
  **Nothing detects this today** (D22 open signal).
- **Risk.** A stale per-service public key breaks every M2M verification
  silently.
- **Rollback.** Keep the production `/etc/auth/` files; the ams copies are
  additive.

### 6. `service_policies` seeding

- **Evidence.** The one M2M policy row in the replica
  (`timeservice → kvservice`) was **inserted by hand** through the admin
  namespace. `service_policies` has no write API upstream by design; production
  seeds it through migrations (D24/T3.2, `.claude/state/platform-layer0.md` §4).
- **Still required.** A real answer for the fleet. It is arguably not ams's job
  — a policy is a statement about who may call whom, which is registry data,
  not supervisor data (D7). Options: keep it in registry migrations, or add a
  registry admin endpoint.
- **Risk.** Every cross-service call fails 401 until the row exists, and the
  failure looks like a token problem.
- **Rollback.** Rows are additive and reversible by SQL.

### 7. Health authority: registry heartbeat vs the ams probe

- **Evidence.** Both are live in the replica. The SDK heartbeats every 30 s
  (`last_seen` 11 s old, proving the thread runs against the replica, not just
  that registration succeeded — `.claude/state/platform-layer0.md` §3
  [verified]); ams probes independently and drives restarts. `auth` has **no**
  `/health` route and is TCP-probed only (D22).
- **Still required.** Decide which one an operator and a dashboard believe.
  They can disagree: a service can heartbeat while failing its ams probe, or
  the reverse.
- **Risk.** Two health systems is the same as none when they disagree.
- **Rollback.** N/A — this is a decision, not a change.

---

## D. Secrets and source

### 8. Third-party API keys

- **Evidence.** Absent from every manifest; production appends them to
  `/etc/<n>/env` by hand (`api-architecture.md` §6). Under ams a
  `service.ams.toml` overlay names them and `load_ams_overlay` feeds
  `TranslateContext.extra_secret_names` (D25). Five overlays exist on `api/`'s
  `ams-platform` branch, each citing its `os.environ[...]` source line:
  commentservice, wechatservice, notificationservice, emailservice, oss.
- **Still required.** One `ams secret set <id> NAME` per key on the production
  host, from stdin. `oss` and `secretsservice` are excluded from the replica's
  sync `--only` list precisely because they have no real credential:
  `oss` calls `ensure_bucket()` against R2 inside its FastAPI lifespan and dies
  on placeholders, and `secretsservice` requires `SECRETS_MASTER_KEY` which no
  manifest or overlay declares (`deploy/ams-platform-sync.service` comments).
- **Risk.** The overlay lists are **n=1 file per service, read once on
  2026-09-02** (D25 open signal). A provider change in any of those five needs
  its own re-grep; this is not a standing guarantee.
- **Rollback.** Secrets outlive a service by design (D16/D17 addendum), so a
  removed declaration does not destroy them.

### 9. Fetching a private repo

- **Evidence.** `StevenLi-phoenix/api` is private: from racknerd anonymous
  HTTPS gets a 404 and `git ls-remote` asks for a username
  (`.claude/state/platform-layer0.md` §6) [verified]. The harness holds no
  credential and none was created — `sources.validate_url` refuses a
  credential-bearing URL. Phase A pushes a bare mirror to
  `<store>/upstream/api.git` and points `SourceMirror` at that local path, a
  shape `validate_url` already accepts; everything downstream is the real code
  path (D24/T3.2).
- **Still required.** Choose one: a read-only deploy key on the box; a token in
  the SecretStore injected into the fetch; or keep the local mirror and push to
  it from a workstation or CI. A deploy key was **rejected** in Phase A because
  it makes the harness able to read a private repo forever to save one rsync.
- **Risk.** Anything that gives the harness standing read access to the whole
  monorepo widens what a harness compromise reaches.
- **Rollback.** The mirror path keeps working whatever is chosen.

### 10. The SDK log-level patch is not upstream

- **Evidence.** Phase A runs upstream `main` (`5ea3572`), which does **not**
  carry the T1.4 root-logger `basicConfig` patch — that lives on the local
  `ams-platform` branch and was never pushed. Consequence, observed: SDK
  `logger.warning` lines arrive with no level token and are classified INFO
  (`.claude/state/platform-layer0.md` §6, and the pilot's finding, D15 gap 1)
  [verified].
- **Still required.** Push the branch (user-gated: PLAN "what stays manual") or
  accept that a genuine service warning is invisible to the escalation channel.
- **Risk.** A real WARNING that never escalates is the failure the whole
  decision boundary exists to prevent.
- **Rollback.** The patch is ~4 additive lines and composes with `sdk/cls.py`.

---

## E. Lifecycle gaps

### 11. `ams rm <id>` does not exist

- **Evidence.** Neither system has a service-deletion path. In ams, reload
  releases ports on removal but **not** uid blocks, and never deletes secrets
  (D17 addendum) — deliberately, so a mistakenly deleted `service.toml` cannot
  destroy credentials. In the deployer, the Registry cascade-delete API exists
  but the deinit pipeline does not; services are retired by renaming a manifest
  to `.disabled` plus manual SSH cleanup (`api-architecture.md` §8 item 1).
- **Still required.** One command that removes the service root, the uid block,
  the secrets, the sidecars, the gateway snippet and the registry row, in an
  order where a failure part-way is recoverable.
- **Risk.** Leftover trees owned by a uid block that gets reused would hand a
  later service someone else's files — which is exactly why blocks are not
  released today.
- **Rollback.** N/A; the gap is the absence of an operation.

### 12. The uid allocator is still not a single writer

- **Evidence.** This broke the **first** live bring-up: the harness holds one
  `UidAllocator` for its process lifetime and never re-read `uidmap.json`, so
  blocks carved by a second process were re-carved differently on reload, and
  `ensure_service_root`'s recursive chown then ran in a namespace with no
  authority over the staged files — EPERM on every one
  (`.claude/state/diagnosis-layer0.md`, with the before/after block table)
  [verified]. `allocate()` now re-reads on a cache miss.
- **Still required.** An allocation op on the control socket so the harness
  stays the single writer. The re-read **narrows** the window; two processes
  can still interleave `allocate` → `_save` (D24/T3.2 open signal).
- **Risk.** Silent, and it looks like a permissions bug rather than a race.
- **Rollback.** N/A.

### 13. No stop/restart cascade when a dependency dies

- **Evidence.** `depends_on` gates **start** only: a dependency going unhealthy
  later leaves its dependents running untouched (D27). Deliberate — one
  registry restart would otherwise bounce 11 services, and the services
  tolerate a registry that comes and goes *after* startup; it is only startup
  that is fatal.
- **Still required.** A decision for production, where the blast radius is
  real traffic.
- **Risk.** A dependent serving against a dependency that is gone.
- **Rollback.** N/A; adding a cascade later is additive.

---

## F. Capacity and the cutover itself

### 14. Memory budget on the real droplet

- **Evidence.** Replica, 1 vCPU / 2 GB: 7 services at 378.9 MiB total harness
  cgroup, registry 92.8 MiB and auth 94.8 MiB **cold** against a 200 M cap;
  host used 553 → 710 MiB (`.claude/state/platform-layer0.md` §1)
  [verified, n=1]. The pilot showed cold `memory.current` runs ~35 MiB above
  steady because page cache is charged to whoever faults a page in first
  (`.claude/state/pilot-api.md` §7). PLAN Q8 budgeted 14 processes at ~900 MB
  from an n=1 60 MB-per-uvicorn figure, explicitly **not** a planning constant.
  Phase A's own `--only` list in `deploy/ams-platform-sync.service` is the
  measured verdict that 2 GB and one vCPU cannot hold all 21.
- **Still required.** Fleet numbers from `.claude/state/platform-fleet.md`
  (T4.1) applied to the production droplet's actual size, plus a decision on
  staggered start — `Assembly.start_all()` starts everything at once and 20
  simultaneous uvicorn imports on one core will blow past `start_period_s`.
- **Risk.** Extrapolating from n=1 per service is how a cutover OOMs.
- **Rollback.** Caps are per-declaration; raising one is a re-sync.

### 15. The `health_grace_s` / `window_s` constants are n=0 against a fleet

- **Evidence.** `window_s = 600` and `health_grace_s = 300` are derived from
  PLAN Q8's `start_period_s = 120` and the 10 s probe interval, **not measured**
  (D24/T3.3 open signals). The named failure mode: a fleet start where 14
  uvicorns on one core take longer than 300 s and the gate fires on services
  that were merely slow.
- **Still required.** Measure once, expect to raise the grace.
- **Risk.** False rollback recommendations at exactly the moment an operator is
  under pressure.
- **Rollback.** Both are constructor arguments.

### 16. Leave the deployer installed but disabled

- **Evidence.** PLAN Q9's stated rollback plan. The deployer's shape is
  documented in `api-architecture.md` §3: per-service systemd units with a
  precise sudo whitelist, `/srv/<n>` trees, `/etc/<n>/env` credential files,
  and Caddy snippets under `/etc/caddy/services[-api]/`.
- **Still required.** Disable, do not remove: units, sudoers entries, `/srv`
  trees and `/etc/<n>/env` files stay in place with a documented one-command
  restore per service until a soak window passes.
- **Risk.** Two systems that can both start the same service on the same port.
  Whichever is disabled must be disabled with `systemctl disable --now`, and
  the ams port allocator bind-probes, so a clash is loud rather than silent.
- **Rollback.** This item **is** the rollback.

### 17. Caddy config is applied by restart, not reload

- **Evidence.** `ams ctl restart caddy` is how a new config takes effect; the
  admin API was rejected because an admin endpoint on shared loopback is
  reachable by every local process and a unix admin socket is created by the
  Caddy uid, which the harness uid cannot connect to (Q3, D21). Restart is
  <1 s. Note that **Caddy has never reloaded a changed config in the replica** —
  the second bring-up run reported no changes, so only the initial start
  exercised the path (`.claude/state/platform-layer0.md` §9) [n=0].
- **Still required.** Either accept a sub-second gateway blip per config
  change, or implement the admin-socket refinement Q3 names as Phase B work.
- **Risk.** In production a restart drops in-flight connections.
- **Rollback.** N/A.

---

## G. Decisions the user must sign off

These are recorded as chosen but were made inside Phase A's replica scope. None
should carry into production silently.

### 18. D18 — "all in": ams becomes the platform runtime

The scope decision itself. Phase A deliberately left production untouched;
Phase B is where that stops being true. Re-confirm before any cutover step.

### 19. D22 — fixed Layer-0 ports (registry 20100, auth 20101, caddy 20180)

Chosen because four cross-service references need a literal known before
anything starts, and a declaration can only expand its own `${PORT_*}`.
Production uses 8001/8002 and 80/443. Either the fixed numbers move, or
cross-service port expansion gets built — the latter makes one declaration
depend on another service's allocator state, which the supervisor cannot order
today.

### 20. D26 — the shared-prefix fan-out set

ams defaults to `("shared/", "components/sdk/")`, deliberately **wider** than
the deployer's rule, which dropped `components/` on 2026-07-02. The deployer
can afford the narrow rule because a deploy rsyncs the whole work tree; ams
cannot, because each service holds its own `<root>/repo` copy and would never
see a new SDK. A production fleet makes the cost of the wider set concrete: an
SDK commit re-stages, re-provisions and restarts every service. It is one
argument (`SyncConfig.shared_prefixes`), not a code change.

### 21. Shared uid 1000 on racknerd

A pre-existing Docker container runs as uid 1000, the same as `harness`. It is
in its own mount/pid namespaces so it cannot see harness files, but host-side
they share a uid — signals, `/proc` visibility (D10 open signal). Flagged and
deliberately not touched. Verify the production droplet has no equivalent, or
give `harness` a dedicated uid there.

---

## Open, and honest about it

- **Auth's user-JWT path is untested, n=0.** No `/health` route, no OAuth in the
  replica (no public callback), so no account can be created
  (`.claude/state/platform-layer0.md` §9, D22).
- **`registry-runtime.db`** exists and heartbeats land in it, but nothing has
  verified the runtime/catalog split beyond `last_seen` moving.
- **The gateway is loopback-only** in Phase A: no TLS, no 80/443, no subdomain
  sites (neither pilot manifest uses one), no static mounts live. Those paths
  are golden-file-only.
- **Caddy's log rules and the SDK heartbeat patterns have never seen a real
  line** (D24/T3.3 open signals). The first live fleet logs are the check.
- **`runtime.node` is accepted but not honoured**; `kind = "nix"` refuses.
  Neither blocks Phase B, both are standing gaps.
- **`scripts/install-rclone.sh` is still separate** from
  `deploy/install-host.sh` (D23 notes T4.4 as the folder-in; not done, left to
  avoid a mid-wave edit of a file another task owns).
