# Core mode: hosting the api core

How ams 1.1.0 runs the Cordis-based `api` core (api v3.x): one Node process,
kept alive by the harness, released by flipping a symlink, fed plugins through
its own control socket, fronted by Caddy and backed up to R2. This page covers
the whole thing: what ams does and what it leaves to core, where every file
lives, what one tick does, how releases, shipping, escalations, backup and the
security model work, and what has not been verified yet.

> **Status (2026-09-30).** Implemented and tested: 1608 portable tests pass,
> and a local end-to-end run against the real api on macOS in plain mode
> (`--no-isolation`) passed all six scenarios
> (`docs/design/history/evidence/core-e2e-local-2026-09-29.txt`). **The Linux
> isolation path (user namespaces, `run_as_service`, uid blocks) has never
> run live.** The racknerd host was torn down, and re-provisioning it is the
> next step. Read the security section with that in mind.

Core mode is the only platform mode since ams 2.0.0. The 1.0.0 manifest mode
(api v2.0.0: `service.yaml` translation, pools, Layer-0 registry/auth) was
deprecated in 1.1.0 and removed in 2.0.0; `CHANGELOG.md` lists what went.

Source: `src/ams/platform/core.py` (config, layout, bundle, declaration),
`coresync.py` (the tick), `corectl.py` (the control plane),
`assets/core_plan.mjs` (the planner), and the `ams platform core` group in
`platform/cli.py`. Decision record: `docs/design/DECISIONS.md` D31 (core
mode) and D32 (review fixes).

---

## Why core mode

api `main` replaced the platform that ams 1.0.0 (manifest mode) was built for:

| ams 1.0.0 (api `ams-platform`, v2.0.0) | api `main` (v3.x) |
|---|---|
| ~21 Python FastAPI services, one `service.yaml` each | no `service.yaml`; ~28 TypeScript plugins under `plugins/<id>/` |
| one process per service (pools: N per process) | **one** Node 24 process, `node dist/core/main.js`, on `cordis@4` |
| registry + auth as Layer-0 services on 20100/20101 | registry, auth, gateway, store, secrets and health are **plugins inside core** |
| ams stages, translates, provisions, declares and gates each service | core hot-installs plugins itself: isolated apply, health check, atomic swap, drain, 60 s probation, auto-revert |
| one Caddy site per mount sidecar | core's `gateway` plugin is the single HTTP entry (18080); Caddy only maps host to port |

Translating manifests no longer has anything to translate. What remains is
what systemd, `core-ship`, `core-release`, `core-daily-backup` and a
hand-written Caddyfile do on the production host (phm). Core mode does the
same jobs rootless and isolated, and reports through the harness's
escalation stream.

## Who does what

Core already owns per-plugin safety, so ams does not repeat it.

| concern | core | ams |
|---|---|---|
| keep the process alive | – | an ordinary ams service: userns + cgroup + `memory_max`, `restart = always` |
| Node + pnpm | – | managed toolchain in the store, checksum-verified download |
| release core itself | – | stage, install, build, stop, flip `current`, start, gate, flip back on failure |
| install / replace a plugin | isolated apply, health, swap, drain, probation, auto-revert | build the artifact, upload + deploy through `corectl`, wait for the verdict, record it |
| plugin dependency order | yes | ships in roster order (foundation first), nothing more |
| artifact validation | yes | none; core's refusal is recorded as a verdict |
| privileges (`ops.read`, `ops.deploy`) | enforces | grants what `core.toml` lists, restarts the plugin so a new generation holds them |
| public HTTP | the `gateway` plugin routes and authenticates | Caddy, host to loopback port |
| backup | – | online sqlite snapshots + immutable byte-store copies to R2 |
| telling a human | writes its own logs | one JSON line per failure on the escalation stream |

## The picture

```
systemd
├── ams-harness.service ─ ams run --policy platform        (Delegate=yes, TimeoutStopSec=50)
│   ├── caddy   :20180   http://<host>:20180 → 127.0.0.1:<site port>
│   └── core    :18080   node dist/core/main.js   (cwd = <root>/current → releases/<sha>)
│         └── plugins: secrets, store, gateway, auth, health, timeservice, …  (inside the process)
│               ▲ unix socket <root>/run/control.sock (0660, service-owned)
│               │
└── ams-core-sync.timer (60 s) → ams platform core sync      (one tick, then exit)
        ├── ctl reload/start/stop/restart ──► <state>/control.sock (the harness)
        └── node <tree>/scripts/corectl.mjs …  run as the service (run_as_service)
```

The tick is **a process, not a loop**. `pnpm install`, the build and the
artifact builds block for seconds to minutes, and the supervisor loop is
single-threaded (D17), so provisioning never runs inside it. The tick talks
to the harness over `<state>/control.sock` and to core through the upstream
client `scripts/corectl.mjs`. ams never re-implements core's protocol.

## Layout

Service id `core`, root `R = <state>/services/core/root`:

| path | what | owner, mode |
|---|---|---|
| `R/releases/<sha>/` | staged api tree + `node_modules` (+ `dist/` once built) | service, `releases/` 0755 |
| `R/current` → `releases/<sha>` | relative symlink; the declaration's `start.workdir` | service |
| `R/data/` | `CORE_STATE_DIR`: `core.sqlite`, `artifacts/`; the store plugin's data conventionally in `R/data/data/` | service, 0750 |
| `R/etc/` | placed config bundle: `plugins.json`, `jwt.pem`, `jwt.pub`, `fonts/` | service, 0700 / 0600 |
| `R/run/control.sock` | `CORE_SOCKET` | service, 0660 |
| `R/build/artifacts/<sha>/` | artifacts built by this tick's plan; `R/build/core_plan.mjs` | service, 0750 |
| `R/.cache/` | the service's own pnpm store, npm cache, XDG cache and HOME for provisioning | service, 0700 |
| `<state>/services/core/service.toml` | the generated declaration | harness |
| `<state>/services/core/.etc.stage`, `.current.new` | staging for `place_bundle` / `flip_current` (renamed into `R`) | harness dir |
| `<state>/platform/core.toml` | core-mode config, written by the operator | harness |
| `<state>/platform/core.json` | the record (release shas, per-plugin verdicts, escalated causes) | harness |
| `<state>/platform/core.lock` | `flock` held by a tick, a ship or a rollback | harness |
| `<state>/secrets/core/bundle/` | harness master copy of the config bundle | harness, 0700 / 0600 |
| `<state>/secrets/core/bundle.placed` | digest of the bundle last placed into `R/etc` | harness, 0600 |
| `<state>/logs/core-provision.log` | output of every install and build step | harness |
| `<store>/node/v<ver>/`, `<store>/pnpm/<ver>/` | the managed toolchain, world-readable | harness |

The core socket path must stay under 104 bytes on macOS and 108 on Linux
(`sun_path`, NUL included). This is checked when the config loads, because
a long `AMS_STATE_DIR` otherwise fails only when core starts. On the target
host the path is `/home/harness/store/state/services/core/root/run/control.sock`
(61 bytes).

## Configuration: `core.toml`

`<state>/platform/core.toml`, written by the operator. Unknown keys are
rejected rather than guessed at, and every error names the key.

```toml
[core]
mirror = "api"                                  # SourceMirror name (<store>/repos/api.git)
url = "/home/harness/store/upstream/api.git"    # the LOCAL bare mirror; validate_url rules
ref = "main"
node = "24.20.0"                                # exact → managed download
pnpm = "11.19.0"                                # exact, must equal the tree's packageManager
gateway_port = 18080                            # must equal plugins.json plugins.gateway.config.port
extra_ports = [18081]                           # other fixed listeners (pages)
caddy_port = 20180                              # the Caddy front's fixed listen port
memory_max = "900M"
log_level = "info"
probation_timeout_s = 120
health_timeout_s = 90
plugins = ["secrets", "store", "gateway", "auth", "health", "timeservice"]
core_paths = ["packages/core/", "package.json", "pnpm-lock.yaml", "tsconfig.json", "scripts/build.mjs"]

[core.privileges]
health = ["ops.read"]
"auto-ops" = ["ops.read", "ops.deploy"]

[[site]]
host = "api.lishuyu.app"
port = 18080

[[site]]
host = "pages.example.com"
port = 18081
```

| key | default | meaning and validation |
|---|---|---|
| `url` | required | Mirror source. No credentials, no ssh (`sources.validate_url`). api is private and the harness holds no credential, so this is a bare mirror the operator pushes to. |
| `mirror` | `api` | Name under `<store>/repos/<name>.git`. |
| `ref` | `main` | Branch the tick follows. |
| `node` | required | Exact `X.Y.Z`. Must satisfy the tree's `engines.node`; checked at stage time. |
| `pnpm` | required | Exact `X.Y.Z`. Must equal the tree's `packageManager` `pnpm@X.Y.Z`; otherwise pnpm would switch to and download its own version inside provisioning. |
| `gateway_port` | 18080 | Core's HTTP entry. Declared as a fixed port (the harness reserves and bind-probes it); target of the release gate and of the harness health check. |
| `extra_ports` | `[]` | Other fixed core listeners. No duplicates. |
| `caddy_port` | 20180 | Caddy's listen port, fixed like Layer 0's because a tunnel points at it. Must not collide with a core listener. |
| `memory_max` | `900M` | cgroup `memory.max` for core (swap is 0, D12). |
| `log_level` | `info` | `CORE_LOG_LEVEL`: `debug`, `info`, `warn` or `error`. |
| `probation_timeout_s` | 120 | How long a tick waits for deployed plugins to leave probation. Still-pending plugins are settled by a later tick. |
| `health_timeout_s` | 90 | How long the release gate waits for core to come back. |
| `plugins` | required | The roster, in install order. Plugin ids only, no duplicates. The foundation plugins that are present must come first, in the order `secrets, store, gateway, auth, health` (the same list as upstream `core-ship`). |
| `core_paths` | as shown | Repo-relative paths that make a commit a **core release**. An entry ending in `/` is a prefix; anything else must match exactly. |
| `[core.privileges]` | `{}` | `plugin = [privilege]`, a subset of `ops.read`, `ops.deploy`. A plugin does not have to be in the roster (one installed by hand still gets its privileges). |
| `[[site]]` | none | `host` must be a strict lowercase RFC 1123 hostname with no wildcard. It is also a file name and an `import` argument, and Caddy matches case-insensitively. `port` must be `gateway_port` or one of `extra_ports`. Hosts are unique. The whole site list is test-rendered at load time. |

## The config bundle

Core reads its per-plugin configuration and grants from `plugins.json`
(`CORE_PLUGIN_CONFIG`, read by the `secrets` plugin), plus the JWT keypair
and fonts next to it. The bundle holds secrets, so ams treats it like the
write-only secret store (D16).

```bash
ams platform core config import plugins.json \
    --rebase /var/lib/core=@data --rebase /etc/core=@etc \
    --jwt-dir ./keys --fonts ./fonts
```

- **Validation.** Top level is `plugins` plus `redactionReaders` only. Each
  plugin entry is `config` plus `grants` only, and `grants` must be a list.
  If `gateway` is in the roster or in the file,
  `plugins.gateway.config.port` must equal `core.gateway_port`; a gateway
  bound where nothing routes is a silent outage. A JSON error reports the
  line and column only, never the offending text.
- **Rebase.** Every string value that starts with `OLD` at a path boundary is
  rewritten (`/var/lib/core` matches `/var/lib/core/x`, never
  `/var/lib/core2`). The longest prefix wins. `NEW` is an absolute path or
  one of `@root`, `@data`, `@etc` (the core layout). Absolute paths left
  outside the core root are logged by **key path**, never by value: core
  runs as the service uid and probably cannot reach them.
- **Storage.** The whole bundle replaces `<state>/secrets/core/bundle/`
  (0700 dirs, 0600 files) atomically, so a font removed upstream is removed
  here too. The command prints the **names** it stored, never values.
- **Placement.** Each tick copies the master into `R/etc` only when its
  digest differs from `bundle.placed`. In isolated mode `R/etc` is 0700 and
  service-owned, so the harness cannot read it back to compare. The copy is
  built in the harness-owned `<state>/services/core/.etc.stage`, chmodded,
  chowned and renamed into place, so core never sees a half-copied
  directory (see the security section). A changed bundle outside a release
  logs a warning to `corectl restart` the affected plugins: the `secrets`
  plugin re-reads `plugins.json` on mtime, but a plugin picks up its section
  with its next generation.

**Bringing the production bundle over.** On phm the bundle is
`/etc/core/{plugins.json,jwt.pem,jwt.pub,fonts/}` and core's state is
`/var/lib/core`. The operator copies those files to the ams host out of
band; ams never connects to phm. The two `--rebase` flags above then map
the production paths into the core root. ams does not migrate an existing
`core.sqlite`. Bootstrap starts from a fresh core and ships the roster.

ams does **not** back up the bundle (see backup). Keep the source files.

## CLI

| command | what it does |
|---|---|
| `ams platform core config import <plugins.json> [--rebase OLD=NEW]… [--jwt-dir D] [--fonts D]` | Validates and stores the bundle master copy. Prints names only. |
| `ams platform core bootstrap [--no-isolation]` | Writes the Caddy files, declares `caddy` on `caddy_port` and reloads the harness, then runs the first tick (first release + the whole roster). |
| `ams platform core sync [--no-isolation]` | One tick; this is what `ams-core-sync.timer` runs every 60 s. |
| `ams platform core status [--json] [--offline] [--no-isolation]` | Release, previous, staged and held shas; for each plugin, core's phase, live generation, artifact, commit and privileges, next to ams's last attempt and a `drift` flag. `--offline` reads the record only. Read-only: no lock, no download, no write. |
| `ams platform core release --rollback [--no-isolation]` | Flips `current` back to `previous_release_sha`, gated like a release. The sha rolled back from is held. |
| `ams platform core ship <id>… [--force] [--no-isolation]` | Builds and ships roster plugins from the staged tree now. `--force` ships even an unchanged or failed content key. |

Every command takes `--state-dir`, `--store-dir`, `--config` and
`--log-level`. Logs go to stderr. Escalations are JSON lines on stdout.
`sync`, `ship`, `release` and `bootstrap` exit non-zero only when **this**
run failed something.

## First run

On a host prepared by `deploy/install-host.sh` (harness user, AppArmor
profile, the store -- XFS reflink or a plain directory -- and `<store>/bin/caddy`):

1. Push a bare mirror of api to `<store>/upstream/api.git`: api is private and
   the harness holds no credential, so `git clone --mirror` it where you have
   access and rsync the result there (owned by `harness`).
2. Write `<state>/platform/core.toml`.
3. `ams platform core config import …` as above.
4. Start the harness: `systemctl start ams-harness`
   (`ams run --policy platform`).
5. `ams platform core bootstrap`. The first run downloads Node (≈53 MB,
   sha256-checked against `SHASUMS256.txt`), installs pnpm, fills the
   service's pnpm store, builds core, starts it and ships the roster.
   Locally this took 74 s including the 60 s probation.
6. `systemctl enable --now ams-core-sync.timer`.

Check with `ams platform core status` and `curl -H 'Host: <site>'
http://127.0.0.1:<caddy_port>/health`.

## A tick, step by step

`coresync.tick()`. Every step logs, and every side effect goes through
`Hooks`, so the portable tests drive whole ticks with fakes.

1. **Lock and load.** Take `<state>/platform/core.lock` without blocking.
   A second run reports "another core operation is running" and exits.
   Load `core.json`; a corrupt or unknown-version record stops the tick.
2. **Prepare.** In isolated mode, allocate core's uid block and make the
   service root. `ensure_layout` creates missing `releases/ data/ run/
   build/ etc/` **as the service** and refuses any of them that is a
   symlink or not a directory (`CoreLayoutError`).
3. **Fetch** `cfg.ref` into the mirror → `head`.
4. **Stage** if `head` is new: copy the canonical checkout to
   `R/releases/<head>`, check the Node and pnpm pins against `package.json`,
   and run `pnpm install --frozen-lockfile` with `CORE_SOURCE_COMMIT=<head>`
   and no build. On success, garbage-collect mirror checkouts (keep 3) and
   release trees (keep release, previous and staged). On failure, escalate
   `core_stage_failed` and **hold** the sha (`stage_failed_sha`): no
   reinstall every 60 s, and the rest of the tick runs against the current
   release.
5. **Release** if needed (next section). A failed release ends the tick
   after the gateway step; no plugins ship in that tick.
6. **Held head.** If `head`'s core release failed and core still runs an
   older release, plugins built from `head` are not shipped onto the old
   core.
7. **Config drift without a release.** A changed declaration (for example
   `memory_max` or `log_level`) is written and the harness reloaded. A
   changed bundle is placed and a restart warning logged.
8. **Status.** `corectl status` from the released tree. If core is
   unreachable, escalate `core_unreachable`, render the gateway and stop.
9. **Settle old probations.** Plugins recorded `probation` by an earlier
   tick get their verdict now (`live` or `failed`).
10. **Drift.** For each plugin ams recorded `live`: if core now wants a
    different artifact (a manual `corectl deploy` or revert) or has it
    disabled, escalate `core_plugin_drift` and **leave it alone** until the
    plugin's content changes upstream.
11. **Plan.** Decide which plugins to build. On a new head or a changed
    roster, that is the whole roster. Otherwise it is only the plugins that
    need another look: last tick's transport failures (`ship_retry`), a
    plugin recorded live that core no longer has, one whose artifact core
    cannot read, and a failed or blocked verdict whose conditions changed.
    `core_plan.mjs` builds them in the head tree, as the service.
12. **Ship** the plugins whose content key changed (see content keys):
    upload, deploy, grant privileges before probation, then poll every 3 s
    until each leaves probation or `probation_timeout_s` runs out. After
    anything shipped, run `corectl gc`. `planned_sha` now advances whenever
    the plan ran; a failure is not re-planned every tick.
13. **Privileges.** Apply `[core.privileges]` wherever core's differ, and
    restart the plugin so a new generation holds them.
14. **Gateway.** Render and write the Caddy files; restart `caddy` if any
    file changed.
15. **Clean up and record.** Remove `R/build/artifacts/<sha>` directories
    other than head and planned. Forget escalated causes not seen again (only
    when the whole tick ran). Write `core.json` **only if it changed**.

A tick with nothing to do costs one `git fetch` and one `corectl status`,
and writes nothing (0.17 s locally, `core.json` mtime unchanged).

## Releasing core

A commit is a **core release** when it is the first release, or when
`git diff release..head` touches `core_paths`. A diff that cannot be
computed counts as a change. `coresync._Tick.release`:

1. Run the build: `node scripts/build.mjs`, as the service, in
   `R/releases/<head>`. Place the bundle.
2. Measure "before": is `/health` on `gateway_port` 200, and which plugins
   are serving (`live.phase == active`) on the running core.
3. **Stop core first.** The running process imports from the tree `current`
   resolves to. ams waits up to 60 s (the 40 s drain plus margin).
4. Flip `current` to `releases/<head>` (a relative symlink, renamed into
   place).
5. Start: if the declaration changed, write it and reload the harness. A
   reload leaves a stopped service down, since the operator's `ctl stop`
   wins, so core is then started explicitly if it still reads stopped or
   failed.
6. **Gate**, polling every 2 s up to `health_timeout_s`. The rule is **no
   regression**: `/health` belongs to the `health` plugin behind the
   `gateway` plugin, so a fresh core with no plugins cannot pass an HTTP
   check.

   | before the switch | the gate passes when |
   |---|---|
   | `/health` was 200 | `/health` is 200 again |
   | some plugins were serving, `/health` was not 200 | **every** plugin that was serving serves again (one already-red plugin must not turn the gate into "the port answers") |
   | nothing was serving, gateway installed and enabled | the gateway port answers HTTP at all |
   | nothing was serving, gateway not installed, or its artifact unreadable (a restore without `artifacts/`) | the control socket answers |

   If the harness reports core `failed` (restart policy used up, and it
   will not start core again), the gate fails **at once** instead of waiting
   out the timeout. Locally this cut a crashing release's outage from ≈92 s
   to ≈17 s.
7. **Pass:** `previous_release_sha ← release`, `release_sha ← head`, and old
   release trees are removed. **Fail:** escalate `core_release_failed`,
   flip back to the previous release, start and gate again. If that fails
   too, escalate `core_down`. The failed sha is **held**
   (`release_failed_sha`) and not released again; a new commit releases
   normally. A failed *first* release is not held, because nothing runs yet
   and a fresh host with an environment problem should heal on its own. It
   escalates `core_down` ("no release to flip back to").

A harness that cannot be reached is not a verdict on the sha: the tick
escalates and the release is retried next tick.

`ams platform core release --rollback` runs the same stop, flip, start and
gate cycle toward `previous_release_sha`, then swaps the two and holds the
sha rolled back from, so the timer does not release it again. Locally a
release was an ≈2 s `/health` gap, and a rollback took 3 s.

## Shipping plugins: content keys and verdicts

**Content key, not artifactId.** Upstream
`artifactId = sha256(bundle ‖ canonical(manifest) ‖ docs ‖ sources ‖ canonical(buildInfo))`,
and `buildInfo.commit` changes on every commit, so every plugin gets a new
artifactId on every commit. `core_plan.mjs` computes the same formula
**without buildInfo**, with the same parts, order, `\0` separators and
`canonical()`. That key changes only when a plugin's content does. A
`git archive` tree has no `.git`, so every build step gets
`CORE_SOURCE_COMMIT=<sha>`.

`core_plan.mjs` is package **data**; `src/ams` never
imports it. It is copied into `R/build/` as the service and run with the
tree's own Node:
`node core_plan.mjs <tree> <outDir> <pluginId>…`. It prints one JSON line per
plugin, `{"pluginId","dir","artifactId","contentKey","path","commit","dirty"}`
or `{"pluginId","error"}`. It moves bundle `console.*` output to stderr, so
stdout stays a protocol. A plugin's stray unhandled rejection or uncaught
exception is recorded against that plugin, instead of letting Node exit and
lose every plan. The run ends with `process.exit`, so a module-scope timer
cannot keep it alive. Complete lines survive a non-zero exit; a plugin with
no line becomes a build error.

**What gets shipped.** A plugin ships when it has no record, its content key
differs from ams's last attempt, core cannot read its artifact
(`artifact_unreadable`, reinstall), it is recorded live but missing from
core, or its old verdict may be retried (below). Roster order holds, with
the foundation first.

**Outcomes** (`core.json` → `plugins.<id>.outcome`). Every record carries
the content key, the artifactId, the sha, the core `release` and the
`bundle` digest it was judged under.

| outcome | how it gets there |
|---|---|
| `probation` | deploy returned `ok`; waiting for core's verdict |
| `live` | observed `active` on the deployed artifact with no reason |
| `failed` | deploy `rejected`/`failed` for a reason about the artifact; core refused the bytes at upload (`invalid_manifest`, `invalid_artifact`, `artifact_too_large`, `manifest_mismatch`, `plugin_mismatch`); or not live after probation (auto-reverted, …) |
| `blocked` | rejected for a reason about **core's state**: `dependency_unavailable`, `dependency_failed`, `dependency_major_mismatch`, `generation_conflict`, `service_in_use`, `major_in_use`, `unknown_plugin`. Stored with a fingerprint of what core runs (every plugin's live artifact and phase). |
| *(none)* | transport failure (corectl could not reach core, timeout). Not a verdict: the plugin goes into `ship_retry` and the next tick re-plans only it. |

**The no-retry rule.** A content key that failed is never shipped again
under the same conditions. Deploying the same bytes every 60 s is a retry
storm, not a fix. A verdict holds under the conditions it was reached in,
so the same bytes are tried **once more** when:

- the core release changed since the verdict,
- the config bundle changed, or
- (`blocked` only) core's installed plugins changed, for example the missing
  provider went live.

A new commit with **new content** always ships. Records written before these
fields existed are never retried. `ship --force` overrides the rule for
named plugins.

**Privileges before probation.** Core accepts privileges only for a plugin
it already knows, so a first install starts without them. `health` without
`ops.read` then answers every `/health` probe with a 503, fails probation,
and has no revert target (seen in the first local run). So right after a
successful deploy, the tick grants the configured privileges and restarts
the plugin; the new generation holds them and gets a fresh probation. A
restart that corectl reports as failed or rejected is escalated, not
recorded as granted. The generation it left running is remembered in
`privilege_restart`, and later ticks restart again while that generation is
still live.

**Artifact store.** After a tick that shipped anything, `corectl gc` removes
artifacts nothing references. Upstream `corectl deploy` reads and hashes the
whole store first, so an unbounded store makes every deploy slower.

## Escalations

Each failure is one JSON line on stdout (the timer unit's journal),
deduplicated across ticks by normalized cause (`policy.cause_key`). A cause
that is not seen again in a tick that ran to the end is forgotten, so a
recurrence escalates again.

```json
{"kind": "CoreSync", "service_id": "core", "action": "escalate",
 "reason": "core_plugin_rejected: deploy failed: …",
 "event": {"kind": "core_plugin_rejected", "service": "core", "plugin": "timeservice",
           "cause": "deploy failed: …", "sha": "4e61caf3…"}}
```

| `event.kind` | when |
|---|---|
| `core_fetch_failed` | the mirror fetch failed |
| `core_stage_failed` | stage, pin check or `pnpm install` failed; the sha is held |
| `core_release_failed` | build, switch or gate failed (flipped back), a rollback failed, or the harness was unreachable at release time |
| `core_down` | the first release failed, or the flip back failed too |
| `core_unreachable` | `corectl status` against the released tree failed |
| `core_plan_failed` | `core_plan.mjs` could not run at all |
| `core_plugin_build_failed` | one plugin did not build |
| `core_plugin_rejected` | core refused the upload or rejected the deploy (`failed` or `blocked`) |
| `core_plugin_not_live` | the plugin left probation not live; includes the last three transitions and failures |
| `core_ship_error` | transport failure on upload or deploy (retried next tick) |
| `core_plugin_drift` | core wants a different artifact than ams shipped, or the plugin was disabled by hand |
| `core_privileges_failed` | a grant or the restart after it failed |
| `core_gateway_failed` | rendering the Caddy files or restarting `caddy` failed |
| `core_sync_error` | the tick crashed; it never takes the timer down |

The exit code reflects **this** run only (the 593daae rule): a failure
carried in the record does not fail every later tick.

The harness also escalates on its own: core's `level:"warn"` log lines, a
crash loop, "giving up on core". A broken plugin or a failed release is
therefore reported on two streams, each deduplicated separately. Merging
them is an open design question (D31).

## Gateway

`gateway.render_core()` writes `<state>/gateway/Caddyfile` plus one
`sites/<host>.caddy` per `[[site]]`; `gateway.write()` rewrites only what
changed and removes the snippet of a site that is gone.

- Plain HTTP only. TLS terminates at Cloudflare or the tunnel, and a
  non-plain config is refused.
- Each site is `reverse_proxy 127.0.0.1:<port>` with a JSON access log. The
  entry site answers the harness's `/ams-health` probe, and any unknown host
  gets a JSON 404 (`no such site`).
- One `import` per site, never a glob, so a stale file cannot be picked up
  silently. Output is `caddy fmt`-canonical and sorted by host; goldens are
  in `tests/golden/gateway/core/`.
- Refused: a duplicate host, a site on the entry host, and a site that
  proxies to Caddy's own port (a request loop).

Caddy is an ordinary ams service, declared by `bootstrap` on `caddy_port`.
The tick restarts it only when a file changed.

## Backup and restore

The existing daily job (`deploy/ams-platform-backup.*`,
`python -m ams.platform.backup run`) discovers the core root like any other
service root:

| what | how | R2 key |
|---|---|---|
| `R/data/core.sqlite`, `R/data/data/state.sqlite` | online stdlib sqlite backup inside the admin namespace, gzip, one object per db per day, 14-day retention | `daily/core/…` |
| byte stores: every `blobs`, `oss-bytes` or `pages-content` directory under `R/data/**`, plus core's `R/data/artifacts` (core service only, top level of `data/` only) | `rclone copy --immutable --exclude '*.tmp'`, run **after** every sqlite snapshot so a snapshot never names a blob the bucket lacks; only missing files are sent; nothing is ever overwritten or deleted remotely | `bytes/core/<path under data>/…` |
| `R/releases`, `R/build`, `R/run`, `R/etc`, `R/.cache` | **never** backed up: only `data/` is scanned. `etc/` holds secrets (D16). | – |

`bytes/` sits outside the retention prefix on purpose. `prune` runs
`rclone delete --min-age` over `daily/`, and rclone keeps source mtimes, so
a blob stored there would be deleted once it was two weeks old.
`BackupConfig` refuses a layout where either prefix contains the other.
Results are `ByteStoreSynced` / `ByteStoreFailed` records (a failure
escalates). Unlike phm's `core-daily-backup`, the writer is not stopped;
sqlite's online backup API gives a consistent snapshot while core runs.

**Restore** (operator steps; `restore()` only ever writes to a scratch
path):

1. `python -m ams.platform.backup restore <archive.db.gz> <scratch.db>` for
   `core.sqlite` and `state.sqlite`. This decompresses and runs
   `integrity_check`.
2. `ams ctl stop core`. Put the databases under `R/data` and copy
   `bytes/core/…` back with rclone. The files must end up owned by core's
   uid block. There is no ams command for this step yet; it is a manual
   admin-namespace job, and it has not been rehearsed for core.
3. `ams ctl start core`. If `artifacts/` was not restored, core boots every
   plugin `artifact_unreadable`. The release gate tolerates an unreadable
   gateway, and the next tick reinstalls each unreadable plugin from the
   current tree.

The config bundle is not in R2. Restore it with `config import` from the
operator's copy.

## Security model

Four identities touch the core root:

| identity | map | used for |
|---|---|---|
| harness | none | the tick itself, the store and toolchain download, the bundle master, `core.json`, staging under `<state>/services/core/` |
| inner root | admin map (inner 0 = harness uid, block at inner 1000) | coreutils only, on paths the harness prepared: `stage` copies and chown, `place_bundle` / `flip_current` renames, gc `rm -rf`, the backup snapshot and rclone |
| the service, one-shot | runtime map (`run_as_service`: inner 1000 only, `no_new_privs`) | `ensure_layout` mkdir, `pnpm install`, `node scripts/build.mjs`, `core_plan.mjs`, every `corectl` call (the socket is 0660 and service-owned; the harness cannot connect) |
| the service, long-running | runtime map + cgroup (`IsolatedSpawner`) | core itself, `memory_max`, restart always |

The rules that follow from it:

- **No untrusted code under the admin map in core mode.** Dependency
  lifecycle scripts, the repository's build and every plugin bundle
  evaluated by the planner run as the service. The harness uid is not
  mapped there, so the code can neither read the harness's 0600/0700 files
  nor write anything the harness owns, and there is no mount mask to
  `umount`. The price: pnpm's store, caches and HOME are the service's own
  under `R/.cache`, so the first install downloads everything once.
- **Inner root never writes through a service-controlled name.** Core runs
  while a tick works, so a compromised core can swap any name inside its
  root for a symlink. The bundle copy and the new `current` link are built
  in the harness-owned `<state>/services/core/` and only renamed into the
  root (`rename(2)` never follows a destination symlink). Chmod runs before
  chown, so the service never owns a half-prepared copy. The planner asset
  and the artifacts directory are placed as the service. `ensure_layout`
  refuses a layout entry that is a symlink.
- **Supply chain.** The Node tarball is sha256-verified against
  `SHASUMS256.txt` from the same release directory and extracted with
  tarfile's `data` filter, every entry under one top-level directory. pnpm
  is installed with the managed npm and `ignore-scripts`. The tree's
  `engines.node` and `packageManager` must agree with `core.toml`.
- **Secrets.** Bundle values never reach a log, argv or stdout. `import`
  and `status` print names. The master copy is 0700/0600 harness-owned; the
  placed copy is 0700/0600 service-owned. `env` passed to build steps never
  carries a secret.
- **Generic provisioning keeps the mask.** `run_admin(mask=…)` overmounts
  harness-private paths in a private mount namespace (issue #1):
  `provisioning_mask` for `ams provision`'s uv/pnpm/bun installs. Since 1.1.0
  it also covers `<store>/{platform,upstream,repos,src}` and the harness
  HOME's credential dotfiles (security-5).

**Known gaps** (DECISIONS D31/D32, Open):

- None of the isolation above has run on Linux. Only the argv and paths are
  checked by portable tests, plus two Linux-marked tests
  (`tests/linux/test_run_as_service_live.py`, `test_run_admin_mask_live.py`)
  that have not run on a host since they were written.
- `SourceMirror.stage` (`mkdir -p` / `cp -a` / `chown -R` on
  `R/releases/<sha>.new`) and the release gc (`find` / `rm -rf`) still run
  as inner root inside the service root. `ensure_layout`'s symlink refusal
  covers the non-racing case; a service that wins a race could still
  redirect them.
- Generic `ams provision` (a `service.toml` with `runtime.kind` uv/venv/pnpm/bun,
  not core) still runs package installs as inner root under the admin map.
  The mask can be removed from inside (`umount`) or bypassed through
  `/proc/<harness pid>/root`, and that provisioning's HOME is still the
  harness HOME. Core mode does not use it.

## Plain mode (`--no-isolation`)

For development and macOS: no user namespace, everything runs as the
current user. `stage_plain` copies the checkout, `provision_tree` runs pnpm
as a plain subprocess with `package-import-method=clone-or-copy` (a dev tree
and store need not share a filesystem), and corectl runs through
`plain_runner`. Pair it with `ams run --no-isolation --policy platform`,
which since 1.1.0 loads the runtime layer, so a managed-Node service finds
its `node` on PATH. The local end-to-end run used exactly this, with a
scratch clone of api; `~/Codes/api` itself was never touched.

## Deploying on a host

- `deploy/ams-core-sync.service` + `.timer`: a oneshot every 60 s
  (`OnUnitActiveSec`, `Persistent=false`), with the same environment block
  as `ams-harness.service`. There is no `flock(1)` wrapper, because the tick
  takes `core.lock` itself and a second flock on that file would deadlock.
  `TimeoutStartSec=1800` backs up the tighter per-step timeouts. There is no
  `Restart=`.
- The harness's shutdown budget is 45 s (`SHUTDOWN_BUDGET_S`) and the unit's
  `TimeoutStopSec` is 50, so core's 40 s drain fits. The budget is computed
  **at shutdown** from the declarations of that moment, so a core added by
  `bootstrap` to a running harness still gets its drain.
- As for every unit that stages or provisions: no `NoNewPrivileges=yes` and
  no empty `CapabilityBoundingSet=`, because `newuidmap` / `newgidmap` are
  setuid.

## Limits and open items

- **Linux live run outstanding.** It needs racknerd, or another host,
  re-provisioned (a user decision). Then run `scripts/remote-test.sh` and a
  live core end-to-end run.
- **Deploy cost.** `corectl deploy` reads and hashes every stored artifact.
  `gc` after each ship keeps the store small, but avoiding the hash would
  mean re-implementing corectl's protocol, which is ruled out.
- **Core's probation can pass a plugin whose health always fails.** With
  enough successful traffic the failure rate stays under 20% (12 of 71
  locally). That is upstream behaviour (`manager.ts` `startProbation`); ams
  records what core decides.
- **Reverting content re-ships it.** A commit that restores old content
  deploys a new artifactId with a fresh 60 s probation, even though core
  already served identical bytes. Harmless.
- **Two escalation streams** for one failure (see Escalations).
- **No production use yet.** Production of the api core is still a separate
  host's systemd units (out of ams's scope); ams has run core end to end
  locally only.
