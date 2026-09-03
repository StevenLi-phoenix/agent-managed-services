# The platform layer

How a `git push` to the `api` monorepo becomes a running, routed, registered
service under the ams harness — and where every piece of that lives.

Read `docs/service-declaration.md` for the `service.toml` schema,
`docs/manifest-translation.md` for the manifest → declaration mapping and
`docs/platform-sidecars.md` for the JSON shapes. The *why* is
`.claude/state/DECISIONS.md`; the plan is `.claude/state/PLAN-allin.md`.

## What "agent managed runtime" means here

The agent **is** the supervisor loop (D7). Services are direct children of one
long-lived harness process, not per-service systemd units; their stdout and
stderr are pipes the harness reads inline, classifies by severity and routes
through an explicit `DecisionPolicy` → `Escalation` boundary
(`src/ams/decision.py`). What the loop can decide — restart, back off, give up —
it does. What it cannot leaves as one JSON line on stdout for the agent.
The platform layer sits *above* that core and is optional to it: nothing outside
`src/ams/platform/` imports the package.

## Three layers

| layer | what | under ams |
| --- | --- | --- |
| 0 | `registry` (catalogue, ACLs, M2M tokens), `auth` (JWT/PAT/OAuth) | ams services on **fixed** ports 20100 / 20101 (D22) |
| gateway | one pinned static Caddy binary | an ams service like any other, port 20180 in the replica (D21) |
| 1 | the `services/*` and `apps/*` fleet from the `api` monorepo | translated from `service.yaml`, one ams service each |

Layer 0's ports are literals because four things need a cross-service reference
a declaration cannot express — registry's `REGISTRY_AUTH_URL`, auth's
`AUTH_REGISTRY_URL`, the JWT issuer both sides sign and verify with, and the
`REGISTRY_URL` / `AUTH_URL` injected into all 20 Layer-1 declarations (D22).

## On disk

```
$AMS_STATE_DIR/                      (= /home/harness/store/state on racknerd)
  services/<id>/service.toml         the declaration; its content hash drives reload
  services/<id>/root/repo/           the monorepo tree at one sha (.ams-sha marks it)
  services/<id>/root/.venv/          the uv environment, service-owned
  services/<id>/root/data/           SQLite lives here; $AMS_DATA_DIR (D19/T1.3)
  services/<id>/root/etc/            jwt-rs256.pub 0444 (and .pem 0400 for signers)
  secrets/<id>/<NAME>                0600, harness-owned, never in a declaration (D16)
  state/{uidmap,ports}.json          allocators
  control.sock                       0600, harness-only (D17)
  gateway/{Caddyfile,sites/<id>.caddy}
  platform/state.json                the sync state machine, one file, all services
  platform/{mounts,registry}/<id>.json   the two sidecars
  platform/static/<id>/              built document roots for kind=static

$AMS_STORE_DIR/                      (= /home/harness/store, one XFS reflink=1 mount)
  repos/<name>.git                   the bare mirror; the only thing that fetches
  src/<name>/<sha>/                  one canonical checkout per commit
  platform/jwt-rs256.{pem,pub}       the RS256 keypair, generated once
  bin/{caddy,rclone}                 pinned, sha256-verified static binaries
  uv-cache/ python/ pnpm-store/ pnpm-home/ bun-cache/
```

State and store share one filesystem on purpose: a cross-device `cp --reflink`
fails, so venvs and caches must sit on the same XFS mount or provisioning
silently degrades to full copies (D8, D13).

## From a push to a running service

One tick of `ams platform sync` (`src/ams/platform/sync.py`), a one-shot process
on a 60 s timer — never inside the supervisor loop, because `git fetch` and
`uv sync` block for minutes and the loop is single-threaded (D17).

1. **mirror** — `git fetch` into `<store>/repos/api.git`
   (`src/ams/platform/sources.py`). Plain `https://` or a local path only; a URL
   carrying credentials is refused and the error names the host, not the secret.
2. **materialize** — `git archive <sha> | tar -x` into `<store>/src/api/<sha>/`,
   atomically via a temp name and `os.rename` (D19).
3. **translate** — every `service.yaml` through a restricted YAML-subset parser
   (`yamlsubset.py`) and `translate.py`, producing a `ServiceDecl` plus the two
   sidecars. The rule is **reject rather than guess**: an unknown key or an
   unrecognised install command raises `TranslateError` naming the field path.
   A service the commit range did not touch is re-translated at the sha it is
   already deployed at, so its declaration is byte-identical and nothing
   restarts it (D26).
4. **stage** — `cp -a --reflink=auto` from the canonical checkout into
   `<root>/repo` inside the admin namespace, then chown to the uid block. The
   second copy of a 17 MiB tree cost 0.01 MiB measured (D19).
5. **provision** — `uv sync --frozen` as inner root in the admin map, then hand
   the venv to the service uid (D9). The service is **stopped first**: the venv
   lives inside the tree `stage` swaps, and a process importing during the swap
   dies with `ModuleNotFoundError` (D24/T3.2, observed live).
6. **declare** — generate `SVC_SECRET` into the SecretStore if missing, write
   `service.toml` and both sidecars.
7. **reload** — one `ams ctl reload` for the tick. The harness rescans
   `services/*/service.toml`, adds what is new, stops what is gone, restarts
   what changed by content hash (D17).
8. **gateway** — render `<state>/gateway/` from the mount sidecars **on disk**,
   then `ams ctl restart caddy` if a file changed. Rendering from the tick's own
   list would delete the snippet of any service it skipped (D24/T3.1); the
   render is after the reload because a service the harness has never seen has
   no allocated port yet.
9. **register + gate** — `POST /api/services` with `X-Admin-Token` +
   `X-Service-Secret` (409 = already there), upsert the ACL rules, then wait on
   the health probe (`registryclient.py`).

Identities are created **before** a Layer-1 service starts: a translated
declaration has no `SVC_DEV`, so the SDK registers inside the FastAPI lifespan
and a 404 there is a uvicorn startup failure, i.e. a crash loop (D24/T3.2).

### The state machine

`<state>/platform/state.json`, one record per service, printed by
`ams platform status`:

```
fetched → translated → provisioned → declared → reloaded → registered → healthy
                                                                  ↘ failed
```

A stage never moves backwards, a `failed` record is never rewound, and a tick
that changes nothing writes nothing. Rewinding would move `stage_since` and
clear `escalated` every 60 s, turning "escalate once per cause" into "escalate
per tick" (D24/T3.1).

## Secrets

Declarations list secret **names** only. Values are written with
`ams secret set <id> NAME` from stdin or a file — never argv — stored 0600
harness-owned under `<state>/secrets/<id>/`, and injected at spawn (D16).
Nothing prints a value back. The harness uid is not mapped into a service
namespace (D4), so a 0600 harness file is genuinely unreadable from inside a
service — that is what makes the boundary real rootless.

Third-party keys (`DEEPSEEK_API_KEY`, `WECHAT_MP_APPSECRET`, `RESEND_API_KEY`,
`BARK_DEVICE_KEY`, the R2 trio) are absent from every manifest — production
appends them to `/etc/<n>/env` by hand. Under ams a `service.ams.toml` beside
the `service.yaml` names them and `static.py`'s `load_ams_overlay` feeds them
into `TranslateContext.extra_secret_names`; the value still arrives by one
`ams secret set` (D25). A service whose declared secret has no value fails only
itself. The RS256 keypair is generated once into `<store>/platform/` and
materialised per service as `<root>/etc/jwt-rs256.pub` (0444) — production's
shared `/etc/auth/jwt-rs256.pub` is unreachable for a mapped uid (D22).

## The gateway

`src/ams/platform/gateway.py` renders, `ams ctl restart caddy` applies. Nothing
talks to Caddy's admin API: an admin endpoint on shared loopback is open to every
local process, and a unix admin socket would be created by the Caddy uid, which
the harness uid cannot connect to (Q3). Config files are 0644 in a 0755 harness
directory, so the Caddy uid reads them with no chown.

Path mounts become `handle_path /p/*` on one entry site, subdomain mounts get
their own site blocks, `kind: static` gets `root` + `file_server` + SPA
fallback. The deployer's security header block, `default_csp`, `csp_map` and
`admin_cors_map` are ported **verbatim as data** and pinned by golden files —
losing a CSP header in a Jinja→Python port is the one failure nothing else
would catch (PLAN risk 2). Phase A is plain HTTP behind one flag
(`GatewayConfig.plain_http`).

## Layer 0 under ams

`ams platform bootstrap` (`src/ams/platform/bootstrap.py`) replaces
`bootstrap/03-jwt-keys.sh`, `08-auth-init.sh` and `09-registry-init.sh`: keypair
once, `REGISTRY_ADMIN_TOKEN` / `AUTH_SESSION_SECRET` / `AUTH_PAT_VERIFY_TOKEN` /
`AUTH_M2M_SECRET` straight into the SecretStore, both declarations written.
Re-running creates nothing. `src/ams/platform/layer0.py` is the 17-stage
bring-up that orders it all; `scripts/platform-bootstrap.sh` drives it.

Auth is TCP-probed, not HTTP: it registers ten routers and no `/health`, and
probing `/.well-known/jwks.json` every 10 s would turn a liveness check into
load (D22). No GitHub OAuth placeholders are set — the app builds a provider
only when both id and secret are present, so unset is more honest than a client
id that renders a login button leading to an error page.

Live evidence the chain works: a real RS256 M2M token minted by the replica
registry for `timeservice`, accepted by `kvservice` through Caddy (204/200),
where anonymous and a one-character signature change both get 401
(`.claude/state/platform-layer0.md` §4).

## Start order

`depends_on = ["registry"]` is a top-level list in `service.toml`, injected into
every translated declaration (`translate.DEPENDS_ON`). A service waits in
`status="waiting"` until every dependency is running **and healthy** (D27). The
incident that forced it: the harness restarted, all 18 services started at once,
and 11 took `Connection refused` from `sdk.registry.start()` inside FastAPI
startup — fatal, not retried — burning five retries in 90 s. A gate that waited
only for a pid would have changed nothing. Shutdown is reverse topological
order; there is no cascade when a dependency dies later (Phase B).

## Backup and restore

`src/ams/platform/backup.py`, daily at 04:10 UTC ±15 min
(`deploy/ams-platform-backup.timer`). For **every** service with a non-empty
`<root>/data` — not production's hardcoded three, which `api-architecture.md`
names as the platform's sharpest data risk — it snapshots through the stdlib
SQLite online-backup API inside the admin namespace, gzips, `rclone copyto`s to
R2 with credentials as `RCLONE_CONFIG_R2_*` env vars plus
`RCLONE_CONFIG=/dev/null`, then prunes at 14 days. A failed prune is a failure,
not a warning: retention that silently stops working is an unbounded bill (D23).

The snapshot opens the source read-write and hands the WAL sidecars back to
their owner afterwards; as inner root they would otherwise land on the harness
uid and the service could no longer commit. `restore` only writes a scratch path
and raises unless `PRAGMA integrity_check` says `ok`.

## Rollback

`ams platform rollback <id> [--to SHA]` (`src/ams/platform/rollback.py`)
re-stages an earlier commit, re-provisions, re-translates the manifest **as it
was at that commit**, then stop → reload → restart → health gate. It moves the
tree, the venv, the declaration and both sidecars. It does **not** move
`<root>/data`, the secrets, the registry record or the gateway (D27/T4.3): a
rollback undoes code, not the migration the newer code ran, and reversing rows is
a restore from the backup with a different blast radius. The policy layer
(`policy.py`) *recommends* a rollback and never performs one — that is the
agent's call (Q7) — and the escalation carries both shas.

**The 60 s sync timer undoes a rollback.** The commit range from the rolled-back
sha to the head necessarily touches this service — that is why it was rolled
back — so the next tick redeploys it. The report's warnings and the escalation
both say so. There is no `pinned_sha`.

## The control socket, and the operator commands

`<state>/control.sock` is 0600 and serves newline-delimited JSON from the
supervisor's own selector loop — no threads (D17). `SIGHUP` (so
`systemctl reload ams-harness`) is `reload`. Reload never provisions, and
removing a declaration never deletes its secrets: a mistakenly deleted
`service.toml` must not destroy credentials.

```bash
ams check-host                     # every prerequisite, with the fix for each failure
ams validate <file>...             # declaration syntax and semantics
ams provision <id>                 # build a runtime, no cgroup needed
ams secret set|list|check|rm <id> [NAME]   # values from stdin, never argv, never printed
ams ctl status|reload|start|stop|restart|kill [<id>]
ams platform sync [--only <id>] [--dry-run] | status [<id>...] | bootstrap
ams platform rollback <id> [--to SHA]
systemctl reload ams-harness       # = ams ctl reload
systemctl list-timers 'ams-platform-*'     # sync every 60 s, backup daily 04:10 UTC
```

## Pools: N services, one process

A manifest whose `service.ams.toml` carries `pool = "<name>"` does not become
its own ams service: `sync.py` groups every manifest naming the same pool
into one declaration (`pool-<name>`), staged and provisioned once, and run by
a single ams-supplied asset (`src/ams/platform/assets/pool_runner.py`) as N
`uvicorn.Server` instances on N ports in one process. Each member keeps its
own port, registry identity, Caddy route, health probe and change detection —
only *which process* runs it changes. `mount.json` gains one optional key,
`port_owner`, for `gateway.resolve_ports` to follow; every other sidecar is
untouched, and an unpooled fleet's output is byte-identical to before pools
existed. Full contract, the runner's identity-env swap, the trust-domain
statement, `ams platform pool plan|adopt`, and the migration runbook:
`docs/platform-pools.md`. Decision record: D29.

## Known gaps

Each is an open item in `.claude/state/DECISIONS.md`, not a TODO invented here.
The production-cutover versions, with the evidence and a rollback for each, are
in `.claude/state/phase-b-prereqs.md`.

- **The `api` repo is private and the harness holds no credential for it.**
  Phase A pushes a bare mirror to `<store>/upstream/api.git`. A deploy key was
  rejected: it lets the harness read a private repo forever to save one rsync
  (D24/T3.2).
- **The uid allocator is still not a single writer.** `allocate` re-reads on a
  cache miss, narrowing the window that broke the first live bring-up without
  closing it. The answer is an allocation op on the control socket (Phase B).
- **`ams rm <id>` does not exist**, in ams or in the deployer. Removing a
  service root, its uid block, its secrets and its registry row is manual. Nor
  does anything seed `service_policies` — the replica's one M2M policy row was
  inserted by hand, and a policy is arguably registry data, not supervisor data.
- **Caddy's log rules have never seen a real Caddy line**, the SDK heartbeat
  patterns are guesses at its wording, and `window_s = 600` /
  `health_grace_s = 300` are derived from the plan rather than measured — all
  n=0 against a running fleet (D24/T3.3). Expect to raise the grace.
- **`runtime.node` is accepted but not honoured** (pnpm and bun use the host's
  node 22 and the provisioner warns); **`kind = "nix"`** refuses with
  `ProvisionError`. Both are interfaces, not features. Do not remove them.
- **A shared uid 1000** on racknerd: a pre-existing Docker container runs as the
  same uid as `harness`. Namespaced, so it cannot see harness files, but they
  share a uid host-side. Flagged, not touched.
- **`scripts/install-rclone.sh` is still separate** from
  `deploy/install-host.sh`, and two load-dependent timing tests flake roughly
  once per four full remote runs on one vCPU. They pass alone; do not widen the
  assertions without reproducing the mechanism.
