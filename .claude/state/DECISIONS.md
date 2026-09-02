# DECISIONS

Format: decision, why it beat the alternatives, rejected alternatives + why,
assumptions / when it breaks. Two sections: **Facts** (verified) and
**Open / weak signals** (n, suspicion, next test).

## Facts

### D1. Python 3.12, stdlib only, single package `ams`
- Why: target box is 1 vCPU / 2 GB with system Python 3.12 and no uv; zero
  deps means deploy = rsync. tomllib covers TOML input; JSON for state.
- Rejected: pydantic (dep + import cost for a schema this small); Go/Rust
  rewrite (the agent loop that hosts this is Python; keep one runtime).
- Assumes: harness interpreter is `/home/harness/venv/bin/python*` (pinned by
  the AppArmor profile, see D3). Local dev venv is pinned to 3.12 to avoid
  3.14-only syntax.

### D2. Userns via manual fork handshake in Python, not util-linux `unshare`
- Verified 2026-09-02 on racknerd (util-linux 2.39.3): `unshare --map-users=1000:100000:1024`
  writes `uid_map` directly and silently does not use newuidmap for ranges the
  caller does not own; `--map-auto` maps the whole subuid range as inner 0..65535
  (one map per user, not per service). Neither gives per-service blocks.
- Chosen: child `unshare(CLONE_NEWUSER)` -> signal parent over a pipe -> parent
  runs `newuidmap <pid> 1000 <block> 1024` and `newgidmap` -> ack -> child
  `setgroups([])`, `setresgid/uid(1000)`, `PR_SET_NO_NEW_PRIVS`, exec. Verified
  end-to-end (probe script, n=1 host).
- Assumes: `newuidmap`/`newgidmap` setuid-root from `uidmap` package; harness has
  a `/etc/subuid` range. Breaks if the systemd unit sets `NoNewPrivileges=yes`
  or an empty `CapabilityBoundingSet=` (setuid helpers could not regain caps).

### D3. Ubuntu 24.04 AppArmor userns restriction: targeted profile, not sysctl
- Symptom: after mapping, `setresuid`/`setgroups`/`chown` in the ns fail with
  EPERM; `/proc/self/attr/current` shows `unprivileged_userns (enforce)`.
  Cause: `kernel.apparmor_restrict_unprivileged_userns=1`.
- Chosen: `profile ams-harness /home/harness/venv/bin/python* flags=(unconfined) { userns, }`
  on a private interpreter copy (`venv --copies`). Only the harness gains userns;
  system python stays restricted. Verified: attr becomes `ams-harness (unconfined)`
  and the whole probe passes.
- Rejected: `sysctl kernel.apparmor_restrict_unprivileged_userns=0` (host-wide
  weakening); profiling `/usr/bin/python3.12` (every python on the box).
- Consequence: service processes are unconfined descendants, so they can create
  nested userns (verified `NESTED_USERNS_OK`). Accepted for now; a per-service
  AppArmor profile would close it (YAGNI until needed).
- Gotcha: `venv --copies` makes `python`, `python3`, `python3.12` three separate
  files; the attachment glob must cover the one actually exec'd.

### D4. Map layout: 1024-uid blocks, inner uid 1000, harness uid unmapped at runtime
- Runtime map: inner `1000..2023` <- host `<block_start>..+1024`. Service runs as
  inner 1000. Harness's own uid is NOT mapped, so harness-private files are
  unreadable from inside (verified).
- Admin map (harness ops only, e.g. chown/rm of service-owned files): inner 0 <-
  harness uid, plus the block. Needed because host-side files land on
  100000+n and the harness cannot touch them otherwise.
- 65536/1024 = 64 services per harness user. Enough; revisit if it isn't.
- Rejected: mapping harness uid as inner 0 for the runtime map (podman default)
  — an inner-root escape would be the harness user.

### D5. Rootless supervisor = the agent loop; systemd is the only init layer
- Carried over from the handoff. Harness unit: `User=harness`, `Delegate=yes`,
  `DelegateSubgroup=harness` (systemd 255 has it; code must still self-move into
  a leaf cgroup when run via `systemd-run` without it, because of the cgroup v2
  no-internal-processes rule — hit during probing).

### D6. Tests: Linux-marked tests only run through `scripts/remote-test.sh`
- An interactive ssh session's cgroup is not delegated; the script runs pytest
  inside `systemd-run --uid=harness -p Delegate=yes`. Agents working in parallel
  pass distinct remote subdirs to avoid rsync clobbering.

## Open / weak signals

- `cgroup.kill` observed to reap `sh` + two `sleep`s within 0.3 s (n=1). Assume
  it is synchronous enough; keep a waitpid loop rather than trusting timing.
- Socket activation for restart-without-connection-reset: not evaluated yet.
  Do not delete the option; evaluate when implementing ports.
- Nix runtime provisioning: no nix on racknerd. Design the interface, implement
  venv/uv first, leave nix as a clearly-marked NotImplemented with a test.

### D7. Product boundary: Linux + systemd only; agent is inside the harness (user-confirmed 2026-09-02)
- Services are direct children of the harness; logs come from held fds. A
  separate agent/supervisor process and per-service systemd `DynamicUser` units
  were rejected because the agent *is* the harness loop. macOS/launchd has no
  equivalent of userns + delegation; VPS is the target. `dsh` plugin packaging
  undecided — do not design for it.

### D8. Per-service venvs on an XFS reflink volume with a shared uv cache (user-confirmed)
- Chosen: block-level CoW via `reflink`. Store = XFS `reflink=1` loop file
  `/var/lib/ams/store.img` mounted at `/home/harness/store` (created 2026-09-02,
  6 GB, fstab `loop,nofail`). Layout under it: `uv-cache/` (`UV_CACHE_DIR`),
  `python/` (`UV_PYTHON_INSTALL_DIR`), `venvs/<service_id>/`. uv link mode
  `clone`. Verified: `cp --reflink=always` of 8 MB consumed ~0 extra space;
  cross-filesystem clone fails with "Invalid cross-device link" — so cache and
  venvs MUST share the mount or uv silently degrades to full copies.
- Why over alternatives: overlayfs needs `mount` inside the userns (blocked by
  the Ubuntu 24.04 AppArmor restriction, and copy_up is whole-file); hardlinked
  shared venvs protect the shared inode only if tools write via rename (a
  convention, not enforceable); modifying CPython to intercept writes is
  bypassable by C extensions and breaks uv's python-build-standalone supply.
- Consequences: each venv is owned and writable by its service uid, so adding a
  package later is normal `uv pip install`. Do not meter venv disk with `du`
  (shared extents are double counted). Never mount the store with DAX.
- Breaks if: someone moves the cache off the mount; the loop file fills (6 GB;
  grow with `truncate`/`xfs_growfs`).

### D9. Provisioning runs inside the ADMIN userns as inner root, then chown to the service
- Problem: the shared uv cache is harness-owned; service uids (100000+) cannot
  write it, and once a venv is chowned to its service the harness cannot write
  the venv. Running `uv` as inner root in the admin map (inner 0 <- harness,
  inner 1000 <- block) sees both as writable, cache stays harness-owned on the
  host, and a final `chown -R 1000:1000 <venv>` hands the venv to the service.
  One code path for first provisioning and for later package additions.
- Rejected: per-service `UV_CACHE_DIR` (no cross-service sharing → duplicated
  downloads/unpacks); world-writable shared cache (permission mess across uids).
- Observation/policy on writes (which service writes where) is deferred; if
  needed, seccomp user notification on `openat` is the path, not CPython patches.

### D10. Node runtimes: pnpm (clone import) and bun (copyfile backend) on the same reflink store
- Verified 2026-09-02 (n=1 host, express@4): second `pnpm install` with
  `npm_config_package_import_method=clone` + `npm_config_store_dir=<store>/pnpm-store`
  costs +0 MB and files have nlink=1; second `bun install --backend=copyfile`
  with `BUN_INSTALL_CACHE_DIR=<store>/bun-cache` also +0 MB (Linux
  `copy_file_range` reflinks on XFS). Both run inside the admin ns as inner root
  (same D9 path as uv), then `chown -R 1000:1000 <project>`; the service runs
  `node main.js` / `bun main.js` as inner 1000. Caches stay harness-owned.
- Why not bun's default hardlink backend: hardlinks share the inode, so the
  post-provision chown would flip ownership of cache files and cross-service
  writes would leak. `copyfile` + reflink gives the same disk economy safely.
- Tooling on the host: system node 22 + npm at /usr/bin; pnpm 11 standalone at
  `/home/harness/store/pnpm-home/bin` (PNPM_HOME, can `pnpm env use --global <v>`
  for other node versions); bun 1.4 at `/home/harness/.bun/bin` (needs `unzip`).
  All under paths the service uid can traverse (`/home/harness` is 0711).
- Schema: `runtime.kind` now `none|venv|uv|pnpm|bun|nix`; `runtime.node`
  version only for pnpm/bun; `packages` are npm specs for node kinds.

## Open / weak signals (cont.)

- racknerd already has a Docker container (`kinky-dungeon` kd-web, `node kd-web/server.mjs`,
  up 10+ days) whose process runs as uid 1000 = the new `harness` uid. It is in
  its own mount/pid namespaces, so it cannot see harness files, but host-side
  they share a uid (signals, /proc visibility from the harness side). Low risk;
  flagged for the user. Fix if wanted: recreate `harness` with a dedicated uid
  (e.g. 1500) via `deploy/install-host.sh` after `userdel`, or run that container
  with a different user. Do not touch the container without the user's say-so.

### D11. Supervisor behaviours (from the supervisor implementation, 2026-09-02)
- `health.kind = "none"` reports healthy the instant the child forks; that
  healthy transition does NOT reset `consecutive_failures`. Otherwise a crash
  loop never reaches `max_retries` (observed as an infinite restart loop in a
  smoke test before the unit tests existed). Only the running-time window
  (`max(10, start_period_s)` s) or a real tcp/http/log health success resets it.
- `ams run --state-dir` beats `$AMS_STATE_DIR`: explicit CLI argument wins over
  the environment (the remote test runner always exports the env var).
- Terminal state (retry exhaustion, `restart.policy=never` stop after crash)
  is escalated by the supervisor itself, independent of what `DefaultPolicy`
  returns, so retry exhaustion always reaches the agent. `DefaultPolicy` for a
  `ServiceExited` returns only RESTART/STOP; its docstring was corrected.
- Timer scheduling is a linear scan over services (n is small, timers are
  rescheduled constantly); no heap.

### D12. `memory_max` implies `memory.swap.max = 0`
- Found by the isolation agent (n=1, racknerd has 1 GB swap): with swap present
  `memory.max` is a reclaim threshold, not a ceiling — a 200 MB allocation under
  a 32 MB limit survives by swapping. Users declaring `memory_max` mean a
  ceiling, so the isolated spawner writes `memory.swap.max=0` whenever
  `limits.memory_max` is set (`ServiceCgroup.set_swap_max`). Undeclared limits
  leave both untouched.
- Rejected: exposing `swap_max` in the declaration (agent-generated declarations
  should not need to know about host swap).
- Note for tests: `bytearray(n)` never faults its pages (calloc over fresh mmap)
  and cannot exercise a memory limit; touch the pages.

### D13. The whole state dir lives on the reflink store
- `AMS_STATE_DIR=/home/harness/store/state` (unit + install script). Service
  roots, python venvs (`<root>/.venv`) and node `node_modules` therefore sit on
  the same XFS filesystem as the uv/pnpm/bun caches, which is the hard
  requirement for reflink (cross-device clone fails, D8). One tree, one
  filesystem, no special-casing of "where does this env go".
- `AMS_STORE_DIR=/home/harness/store` still names the cache root
  (`uv-cache/`, `python/`, `pnpm-store/`, `pnpm-home/`, `bun-cache/`).
- Breaks if someone points `AMS_STATE_DIR` back at ext4: provisioning still
  works but silently degrades to full copies (uv warns "Failed to clone").
- (D11 addendum, after review-1) `restart.policy="always"`: a clean short-lived
  exit COUNTS toward the failure streak, so `max_retries` stays reachable and
  the agent is told when a service gives up; flooring the backoff was rejected
  because it only slows an infinite loop. Terminal detection keys off
  `ServiceState.last_exit_failed`, not the streak alone. `_poll_timeout` folds
  in only deadlines `_run_timers` will act on (repeat each branch's guard) —
  the invariant that prevents the two 100 %-CPU spins found in review-1.

## Open / weak signals (runtime layer, 2026-09-02)
- `runtime.node` is accepted by the schema but NOT honoured yet: pnpm/bun use
  the host node 22 (`/usr/bin/node`) and the provisioner logs a WARNING. Path
  when needed: `pnpm env use --global <ver>` into `PNPM_HOME` and prepend that
  node to the service PATH. Do not remove the field.
- `kind="nix"`: `provision()` refuses with ProvisionError; `runtime_env` raises
  NotImplementedError. No nix on racknerd; interface only.
- `run_admin` has no env/cwd parameters; the provisioner wraps tools as
  `env -i -C <workdir> K=V... <tool>` (coreutils, no shell). Fine, but if a
  third caller appears, add `env=`/`cwd=` to `run_admin` instead of a third wrapper.
- Reflink assertion in `tests/linux/test_runtime_provision.py` is the weakest
  result (n=1): second numpy venv holds >10 MB apparent while the fs grew <5 MB.

### D14. Pilot: run two `api` platform services under ams on racknerd (user-chosen 2026-09-02)
- Scope: `kvservice` + `timeservice` from github.com/StevenLi-phoenix/api as
  REPLICAS on racknerd (production droplet untouched, no REGISTRY_URL/AUTH_URL so
  no fake heartbeats reach the real registry). Goal: validate rootless userns +
  reflink uv envs + ams port allocation against real FastAPI/SQLite services
  before deciding on replacing the Layer-1 runtime layer (deployer + per-service
  systemd units + sudo whitelist). Findings go to `.claude/state/pilot-api.md`.
- Rejected for now: full Layer-1 replacement (too much at once); dropping the
  deployer entirely (product thesis, but needs the pilot first); doc-only.
- Needed a schema/runtime extension: `runtime.sync = true` (uv project mode:
  `uv sync --frozen` in workdir, venv at `<workdir>/.venv`) because api services
  depend on `../../components/sdk` via `[tool.uv.sources]` path deps. Whole
  monorepo (minus .git) is placed at `<service_root>/repo`, one copy per service
  (different owning uids; reflink makes the duplicate cheap).
- Assumes the SDK tolerates missing REGISTRY_URL/AUTH_URL (report says optional;
  n=0 verified — the pilot will confirm).

### D15. Pilot outcome (2026-09-02): ams can host api Layer-1 services; three gaps before any real migration
- Verified live on racknerd (n=1 host, 2 boots): `kvservice` (uid 102048, :20002)
  and `timeservice` (uid 103072, :20003) healthy under `ams-harness.service`
  next to the demos, limits applied, ports/uids stable across restarts, zero
  escalations, idempotent `scripts/pilot-api.sh` (14 s second run). Disk: the
  uv env cost 2.6 MiB of blocks for a 47 MiB environment (reflink), the
  monorepo source copy 31 MiB (does not reflink — it is rsynced, not cloned).
- Gap 1 — log severity is invisible: the SDK/uvicorn never configure the root
  logger, so a service `logger.warning(...)` reaches the harness via
  `logging.lastResort` as a bare line with no level token → classified INFO.
  Fix options (not chosen yet): (a) SDK configures a formatter with the level
  name; (b) harness accepts an optional `logging.format` hint per declaration;
  (c) treat *all* stderr from python services as WARNING (rejected: uvicorn
  access logs go to stderr). Do not delete (a)/(b) before deciding.
- Gap 2 — no hot reload / per-service restart from outside: a new declaration
  needs a full unit restart and `KillMode=control-group` takes every service
  down. Today's deployer restarts one service at a time, so ams is currently a
  blast-radius downgrade for the platform. Needed: a control channel (unix
  socket or SIGHUP-driven reload of `services/*/service.toml`) with add/
  remove/restart per id.
- Gap 3 — secrets: `SVC_SECRET` sits verbatim in the agent-written
  `service.toml`. Production keeps it in a root-owned file the deployer never
  reads. Needed: `env_file` indirection (0400 file owned by the service uid,
  or harness-readable and injected at spawn) before Layer 1.
- Unverified: kvservice write path (only 401 observed), cold uv cache timing,
  single-service boot time.

### D16. Secrets: Cloudflare-style write-only store held by the harness (user-chosen 2026-09-02)
- Declarations list secret NAMES only (`secrets = ["SVC_SECRET"]`, validated
  like env names, reserved names rejected, may not collide with `env`). Values
  are written with `ams secret set <id> KEY` from stdin/file (never argv), stored
  at `<state>/secrets/<id>/KEY` mode 0600 owned by the harness, and injected
  into the service environment at spawn through the same `extra_env_for` path
  as runtime activation. `list` shows names only; nothing ever prints a value.
- Why: the pilot put SVC_SECRET verbatim in an agent-written file. The harness
  uid is NOT mapped into service namespaces (verified: 0600 harness files are
  unreadable inside), so harness-private files are the right boundary rootless.
- Rejected for now: encryption at rest (key would sit on the same host as the
  data; only defends against same-uid processes — note the shared uid 1000 with
  a Docker container on racknerd). Future path: `systemd-creds` /
  `LoadCredentialEncrypted=` on the unit. Do not delete that option.
- Breaks if: the harness home/state dir becomes group/world readable, or a
  service is ever spawned with the harness uid mapped.

### D17. Control channel: unix socket + SIGHUP reload, per-service ops (user-chosen 2026-09-02)
- `<state>/control.sock` (0600, harness), newline-delimited JSON requests
  served from the supervisor's selector loop (no threads): `status`, `reload`
  (rescan `services/*/service.toml`: add new, stop removed, restart changed —
  decided by a content hash of the file), `start|stop|restart|kill <id>`.
  SIGHUP == `reload`. Client: `ams ctl <op> [id]`.
- Why: pilot showed a full-unit restart (KillMode=control-group) for every
  declaration change — a blast-radius downgrade vs the api deployer's
  per-service restart. Rejected: D-Bus (dependency, overkill), HTTP on
  localhost (another port + auth surface for a same-user operator).
- Provisioning stays outside the loop (`ams provision` then `ams ctl reload`).
- (D16 addendum) Removing a declaration (reload) does NOT delete its secrets:
  a mistakenly deleted service.toml must not destroy credentials. Cleanup is
  explicit (`ams secret rm <id> <NAME>`, or `SecretStore.remove_all` from a
  future `ams service remove`). Secrets outliving a service are harness-private
  files and cost nothing.
- (D17 addendum, implemented) Reload semantics: ports are released on
  removal, uid blocks are NOT (the service root on disk stays owned by the
  block; reusing it would hand a later service a leftover tree — a future
  `ams rm <id>` removes root + block + secrets explicitly). A changed
  declaration restarts the service unless an operator stopped it
  (`desired=down`), except a `failed` (crash-looped) service, which is revived
  because editing the declaration is how it gets fixed. Reload never provisions.
- Bug fixed on the way: `Supervisor.restart()` during the reaped-but-undrained
  window left a service down forever (`desired=up`, no timer). Regression test added.

## Open / weak signals (test flakiness, 2026-09-02)
- Full remote suite on racknerd (1 vCPU): 2 flakes in 4 full runs, different
  tests each time (`test_pids_max_caps_a_fork_storm` pids.events timing;
  `test_graceful_stop_of_a_well_behaved_child_needs_no_kill`). Both pass alone
  and the supervisor file passes 3/3 in isolation. Hypothesis: load-dependent
  timing thresholds during the 45 s full run. Next: widen the two timeouts or
  run Linux tests with `-p no:cacheprovider --timeout` per test; do not mark
  them skipped.

### D18. "All in" accepted (user: 2026-09-02): ams becomes the platform runtime, Phase A = full replica on racknerd
- Plan of record: `.claude/state/PLAN-allin.md` (4 waves, 16 tasks, Q1–Q9 with
  rejected alternatives). Key choices carried from it: bare mirror + canonical
  checkout per sha + reflink copy per service, polled `git fetch` from a one-shot
  60 s timer process (no webhook, no inbound port in Phase A); `service.yaml`
  translated by a restricted YAML-subset parser that rejects instead of guessing;
  mounts/ACLs live in sidecar JSON, not in `ServiceDecl`; static Caddy binary as
  an ams service on a high port; Layer-0 registry/auth under ams with per-root
  key copies; SDK gets a `basicConfig` with level tokens (branch `ams-platform`
  in `api/`, never pushed without the user); backup covers every data dir with a
  restore drill as a test. Production droplet untouched until Phase B (user-gated).

### D19. Source delivery: `git archive | tar` into one canonical checkout per sha, `cp --reflink` per service (T1.1, 2026-09-02)
- Implemented in `src/ams/platform/sources.py` (`SourceMirror`): bare mirror
  `<store>/repos/<name>.git`, canonical checkout `<store>/src/<name>/<sha>/`,
  per-service copy `<root>/repo` made with `cp -a --reflink=auto` **inside the
  admin ns** (D9 path), then `chown -R 1000:1000`. Measured on racknerd (n=1,
  17.0 MiB tree of incompressible files): extraction cost 17.01 MiB of free
  space, the first `stage` 0.01 MiB, the second `stage` into a different service
  root 0.01 MiB. The extraction is the control in the same test — without it a
  "0 MiB" reading would prove nothing about reflink.
- Rejected `git worktree` per service: worktree metadata is written by git as
  the harness uid while the checkout must be chowned to the service uid, so git
  would manage a tree it can no longer read; and each worktree is still a full
  copy of the files, so it buys nothing over `cp`.
- Rejected `git clone --depth 1` per service: a `.git` dir per service, a
  network fetch per service, and no extent sharing.
- `tar -x -f - -C <dir>` rather than `tar -x -C <dir>`: GNU tar's no-`-f`
  default is a compiled-in device, not stdin, and only *happens* to read stdin
  when it is not a tty. Being explicit works identically on GNU tar and bsdtar
  (macOS), which the portable test needs.
- Both children of the archive pipe write stderr to a `TemporaryFile`, not a
  pipe: with pipes, a tool emitting more than one pipe buffer of diagnostics
  while we block on the other child deadlocks.
- Atomicity: extraction goes to `.<sha>.tmp<pid>` then `os.rename`, so a crashed
  extraction can never be mistaken for a complete checkout; `stage` builds
  `<root>/repo.new`, then `mv repo repo.old; mv repo.new repo; rm -rf repo.old`
  (all in the admin ns). `<root>/repo/.ams-sha` records the commit; when it
  already equals the requested sha, `stage` returns without forking at all --
  that is the common case on every 60 s sync tick.
- `<store>/src/<name>/index.json` (last-use per sha) drives `gc(keep)`. A
  corrupt index **warns and falls back to directory mtimes** rather than raising
  `StateCorrupt` the way the uid/port allocators do: it is a usage hint for a
  cache, and nothing in it can hand out a resource twice. Rejected treating it
  as allocator state (would turn a cosmetic corruption into a stuck sync loop).
- URLs: plain `https://` only, plus `file://`/absolute paths so tests can drive
  a throwaway repo. `git@host:path`, `ssh://`, `git://` and `http://` are
  rejected with a clear error rather than silently depending on an agent key;
  an `https://` URL carrying credentials is refused and the error names the host
  only, never the secret. git runs with `GIT_TERMINAL_PROMPT=0`, an empty
  `GIT_ASKPASS` and `GIT_CONFIG_{GLOBAL,SYSTEM}=/dev/null`, so a private URL
  fails fast instead of burning the whole timeout on a credential prompt.
- Test-file naming: the Linux file is `tests/linux/test_platform_sources_linux.py`,
  not `test_platform_sources.py` as the task brief named it. pytest's default
  `prepend` import mode derives module names from the basename, and `tests/` has
  no `__init__.py`, so two files with the same basename abort collection for the
  whole suite ("import file mismatch"). Rejected adding `tests/linux/__init__.py`
  or switching to `--import-mode=importlib`: both are shared test infrastructure
  changes made mid-wave while three other agents were editing that tree. The
  repo already disambiguates this way (`tests/test_userns_portable.py`).

### D20. Registry client: `health_path` accepted but never sent (T2.3, 2026-09-02)
- `RegistryClient.create_identity(..., health_path=...)` matches the task
  brief's signature and `registry.json`'s shape (`docs/platform-sidecars.md`),
  but the value is validated (`must start with "/"`) and then discarded rather
  than put in the `POST /api/services` body: the registry's `RegisterRequest`
  (`api/components/registry/src/registry/api/services.py:109`) has no such
  field — `id, audience, display_name, location, endpoint, capabilities,
  version, owner, metadata, probe_url` only. Sending it would either be
  silently dropped by pydantic or need speculative server-side support that
  doesn't exist. The sync loop (T3.1) already has the value from the sidecar
  for the ams declaration's `[health]` block and for composing the URL it
  hands to `wait_healthy` directly, so nothing is lost by not putting it on
  this wire. *Rejected:* dropping the parameter from `create_identity` instead
  — would diverge from the brief's signature and from `from_sidecar`'s natural
  1:1 field mapping for no benefit, since the validation still catches a
  malformed value early.
- `upsert_acl` sends **no** `X-Service-Secret` header — only `X-Admin-Token` —
  matching `registry/api/acl.py:52`'s `Depends(require_admin_or_token)` (no
  service-secret path exists for ACL writes, unlike identity creation).
- Retry helper accepts an `extra_success_statuses` set (used to treat 409 as
  success for `create_identity` only) rather than special-casing 409 as a
  third outcome throughout the retry loop — keeps `upsert_acl` and any future
  caller sharing the same loop with a plain success/retry/fail three-way split.

### T1.2 translator choices (2026-09-02)

- **`/var/lib/<n>/…` env values are rewritten to an absolute
  `<services_dir>/<id>/root/data/…`, not to a token.** `ServiceDecl` expands
  `${PORT_*}` and nothing else, so a `${AMS_DATA_DIR}` token in `[env]` would
  reach the service verbatim and every stateful service would open a literal
  path with a dollar sign in it. *Rejected:* teaching `schema.expand_ports` a
  second token — schema.py belongs to another agent this wave, and a general
  substitution layer in the declaration is a feature nobody has asked for
  (YAGNI). *Rejected:* leaving `/var/lib/<n>` alone and letting `run_admin`
  bind-mount it — needs a mount namespace the harness does not create.
  *Breaks if:* the state dir moves after a declaration is written; the sync
  loop must re-translate, not re-use, on a state-dir change.
- **`TranslateContext` carries `services_dir`, not `service_root`.** The
  service id only exists once the manifest is parsed, so a per-service root
  cannot be supplied by the caller without pre-peeking at the YAML. `ctx.root_for(id)`
  derives it. (Deviation from the T1.2 brief, which said `service_root`.)
- **The loopback gate lives in `TranslateContext.__post_init__`, not in
  `translate()`.** A context that could reach production must not exist at all;
  putting the check on the call would let a caller construct one, log it, or
  hand it to a different code path. Accepts only `http://127.0.0.1:<port>` —
  `localhost` is refused because it resolves through the host's resolver and
  can be re-pointed.
- **The YAML subset rejects bare `yes/no/on/off/y/n`.** PyYAML resolves them to
  booleans (YAML 1.1), so `restart: no` — legal per the deployer's own JSON
  schema — already reaches the deployer as `False` and fails its enum check.
  Rejecting with "quote it" is strictly more correct than either behaviour.
  Same reasoning for `0755`/`1_000`/`.inf`: reject rather than diverge silently
  from the oracle. *Rejected:* implementing YAML 1.1 resolution — it would make
  the parser agree with PyYAML on a value the platform cannot use anyway.
- **`process.exec` first token: `.venv/bin/<tool>` → `<tool>`; any other
  absolute path under `/srv/` raises; absolute paths elsewhere pass through.**
  `/srv` does not exist under ams, so a `/srv/...` entry point that is not a
  venv tool has no correct rewrite. *Rejected:* stripping `/srv/<n>/` and
  hoping the file lands under `repo/` — that is the guess risk 1 is about.
- **`emit_toml` is hand-rolled and emits only the fields the translator sets.**
  Fixed table order, env/ports sorted, so goldens are byte-stable;
  `schema.loads(emit_toml(d)) == d` is asserted per manifest. *Rejected:* a
  generic dataclass→TOML dumper — it would emit every default and the goldens
  would churn every time a schema default changes.
- **The manifests are copied into `tests/golden/platform/manifests/`.** `api/`
  is gitignored and is not rsynced to the Linux host, so a test reading the
  clone would silently skip everywhere that matters. The copies are the test
  input; a skip-if-absent drift test compares them byte-for-byte to the clone.
- **`REGISTRY_URL`/`AUTH_URL` are the only injected env a manifest may also
  set, and the context still wins.** commentservice and wechatservice pin
  production's `http://127.0.0.1:8001`; the replica allocates its own auth
  port. Every other injected name in a manifest raises rather than being
  silently overridden.
- Open / weak signal: the translator is verified against exactly the 21
  manifests that exist today (n=21, one repo). The PyYAML oracle agrees on all
  of them plus 10 synthetic cases, but the YAML subset's divergence surface
  (plain-scalar resolution) is only probed by those cases. Do not treat
  "parses YAML" as established; a new manifest construct is expected to raise,
  and that is the designed outcome.

### D19. T1.3 core gaps: data dir, log-format hint, expected exits, self-probe noise (2026-09-02)
- **`<root>/data` is created by `ensure_service_root`, not by the service.** Mode
  0750, chowned to the block, exported as `AMS_DATA_DIR`. It is created by the
  *harness* with a plain `mkdir` while the root is still harness-owned (the
  existing recursive `chown` then claims it), and only through the admin ns when
  an already-handed-over root predates it. *Rejected:* always creating it in the
  admin ns — that adds a fork to the pre-spawn path, which D11/`ensure_service_root`
  deliberately keeps free (the warm path must cost one `stat`); the existing
  "no admin call on a warm root" test would have failed and the fix would have
  been to weaken it. *Rejected:* 0700 — a future group-shared data dir (backup
  helper, sidecar) would need a chmod of a service-owned tree; 0750 leaves the
  gid usable. Consequence, and it is intended: the harness uid cannot read the
  dir, so backup (T2.4) must go through the admin ns — asserted by a test.
- **`[logging] format` is a per-declaration hint, and `auto` is the default.**
  `level-prefix` and `json` read the level the process printed; anything without
  one falls back to the heuristics rather than being downgraded to INFO. Only
  `level`/`severity` are read out of a JSON line — the object is attacker-
  influenced data, and `json.loads` executes nothing. *Rejected:* making the
  hint mandatory (every existing declaration would need editing for no gain);
  *rejected:* trusting a JSON line's message text as a severity source (that is
  what the heuristics already do, and it is what the hint exists to stop);
  *rejected (again, from D15 gap 1):* all-stderr-is-WARNING — uvicorn access logs
  go to stderr. This complements, not replaces, the SDK-side fix (PLAN Q5a): the
  gateway is not an SDK service.
- **`expected` is a field on `ServiceExited`, not something the policy infers.**
  Only the supervisor knows operator intent (`desired == "down"` or
  `restart_pending`); from outside, a service killed by our own SIGTERM is
  indistinguishable from one that crashed with the same status. *Rejected:*
  letting the policy read it off `ServiceContext` — the context describes the
  service, not one event, and a queued exit event would be judged against
  whatever the desired state had become by the time the policy ran (a
  stop-then-start races itself). *Rejected:* suppressing the event entirely — a
  stop is a real state transition the agent loop needs; INFO + LOG keeps it.
  `DefaultPolicy` returns LOG for an expected exit rather than STOP/RESTART:
  the supervisor already ignores a policy RESTART for a service an operator
  stopped, and schedules a pending restart itself, so no restart behaviour
  depends on the returned action here.
- **Self-probe suppression is noise control, not a security boundary.** The line
  is written by the service, so a service could forge one to have it dropped —
  but a service that wants to hide output can simply not print it. Narrowed to
  loopback + GET/HEAD + exactly the declared `health.path` + 2xx/3xx, so the one
  line that matters (a 500 on the health path) still escalates. *Rejected:*
  suppressing by log *volume* or rate — it would drop real bursts and hide a
  crash loop's output; *rejected:* asking the service to be quiet (we do not own
  Caddy's or uvicorn's access log config).
- Open / weak signal: the two access-log shapes are the two we have seen
  (uvicorn, `http.server`), n=2 formats and 0 live services observed through the
  new code. Caddy's JSON access log is *not* covered by the regex — it will be
  classified by the `json` hint and escalate only on its own `level`, which is
  the intended behaviour but is unverified against a running Caddy. Do not add a
  third pattern speculatively; add it when a real fleet log shows one.

### D21 (T2.2). Gateway: pinned Caddy 2.11.4 static binary, plain HTTP in Phase A, config rendered not reloaded (2026-09-02)
- **Version pin: Caddy 2.11.4** (released 2026-06-03, the latest stable 2.x).
  `deploy/install-host.sh` §4c downloads `caddy_2.11.4_linux_amd64.tar.gz`,
  checks it against a pinned sha256 (`527fbf91…`, derived from the tarball whose
  upstream sha512 in `caddy_2.11.4_checksums.txt` was verified), then checks the
  extracted binary against a second pinned sha256 (`b7105518…`) before
  installing it 0755 harness-owned at `<store>/bin/caddy`. Re-runs skip on the
  binary hash, so idempotency does not depend on the network. Both hashes are
  pinned because only the second one describes the file that actually runs.
  *Rejected:* pinning only the upstream sha512 line — it verifies the archive,
  not the artifact, and a corrupted extract would install silently.
  *Rejected:* `apt install caddy` (Q3, unchanged): root unit, `caddy` system
  user, 80/443.
- **Phase A is plain HTTP, and that is one flag.** `GatewayConfig.plain_http`
  emits `auto_https off` plus `http://<host>:<listen_port>` site labels; setting
  it False emits bare hostnames and nothing else changes (golden scenario
  `tls` pins both forms). Subdomain mounts stay real site blocks in Phase A —
  they are reachable by Host header on the same high port — so the Phase-B flip
  needs no re-architecture, only DNS and port 80/443.
- **Site label is `mount.gateway`, not `mount.subdomain`.** `subdomain` is only
  the discriminator (`docs/platform-sidecars.md`: "the renderer emits a whole
  site block for `gateway`"), and the manifests confirm it: displayservice has
  `gateway: display.lishuyu.app` + `subdomain: display`. Using `subdomain` would
  have produced the site label `display`.
- **Explicit `import sites/<id>.caddy` per mount, never a glob.** The deployer
  needs two directories (`services-api/` imported *inside* the entry site,
  `services/` at root) because it globs. One `sites/` directory plus per-file
  imports gets the same scoping, and a stale file can never be picked up
  silently — `write()` deletes stale snippets, but a glob would have made that
  deletion load-bearing rather than belt-and-braces.
- **File access logs are OFF by default** (`GatewayConfig.log_dir=None`).
  Caddy runs as a mapped uid; harness-owned dirs are readable but never writable
  from inside the ns (D4), so `output file` into `<state>/gateway/logs` cannot
  work without a chown the harness would have to do through `run_admin`. With no
  `output`, Caddy's JSON log goes to stderr, which the supervisor already reads
  line by line and routes through the decision interface — the same pipeline
  every other service uses. `log_dir` still renders the deployer's
  `roll_size/roll_keep/roll_keep_for` block verbatim (golden scenario `logdir`)
  for a service-owned directory or for Phase B. *Rejected:* chowning a harness
  dir to the caddy uid — it makes the harness's own config directory writable by
  the service it configures.
- **Two hand-managed production rules were ported, not dropped**: the entry
  site's first-party SPA CORS block (origin regexp, methods, headers, max-age)
  and the `/pages/api/*` handle that lets the admin SPA's `.lishuyu.app` cookie
  reach pages without going cross-site. Both live in `bootstrap/04-caddy-init.sh`
  rather than the Jinja templates, so a template-only port would have lost them.
  The `/pages/api/*` rule renders only when a `pages` mount has a live port.
- **Renderer output is `caddy fmt`-canonical**, checked by a Linux test.
  `caddy validate` only *warns* about formatting, so nothing else would catch it;
  canonical output means an operator running `caddy fmt --overwrite` on the live
  config cannot make it diverge from the next render.
- **Rejected: reading the port from the declaration.** The declaration asks for
  `ports.main = 0` and the Caddyfile's listen address is whatever ams hands out,
  so `render()` is strictly after allocation. `resolve_ports()` is the only place
  that follows `mount.port_name` into the allocator; a mount with no live port
  raises rather than rendering a `reverse_proxy` to nothing.
- **Rejected: rendering anything unvalidated into the Caddyfile.** Header values
  are quote-checked (Caddy has no escape for `"` inside a quoted token), and
  site labels / paths are charset-checked because they are written *unquoted* —
  a stray `{` there changes the config's structure, not one string.

### Open / weak signals (gateway, 2026-09-02)
- `memory_max = "120M"` for the caddy service is a **guess, n=0**: no
  `memory.current` has been measured for Caddy under ams at any fleet size. The
  only live run so far was a zero-mount config answering three curls. Do not
  treat it as a validated cap — measure once the real mounts are up, and expect
  to raise it rather than to have confirmed it.
- ~~`[logging] format="json"` was not added~~ — RESOLVED same day: T1.3 landed
  `LoggingSpec`, so `caddy_declaration()` now emits `[logging] format = "json"`
  and a test asserts `schema.loads(...).logging.format == "json"`. Caddy is the
  reason that field exists (PLAN-allin Q5b): it is not an SDK service, it writes
  one JSON object per line to stderr, and the severity heuristic would otherwise
  guess from the text.
- The entry site's own 404 catch-all carries **no** security headers. Verified
  live (`curl -I /nope` returns none). That matches production verbatim
  (`04-caddy-init.sh` sets none on `api.lishuyu.app` itself), so it was left
  alone — but it is a real gap in both systems, not an ams regression.

### D22 (T2.1). Layer 0: fixed ports, per-service key copies, no OAuth placeholders (2026-09-02)
- **Layer-0 ports are fixed (`registry` 20100, `auth` 20101), not allocated.** A
  declaration can only expand its own `${PORT_<name>}` (`schema.port_refs`), so
  registry cannot write auth's port and vice versa. Four things need a literal
  cross reference: registry's `REGISTRY_AUTH_URL`, auth's `AUTH_REGISTRY_URL`,
  the JWT issuer both sides sign/verify with, and the `REGISTRY_URL`/`AUTH_URL`
  that `TranslateContext` injects into all 20 Layer-1 declarations. A literal
  needs a number known before anything starts. Fixed requests ≥1024 are already
  legal (`schema.validate`) and the `PortAllocator` bind-probes, so a clash is
  loud rather than silent. *Rejected:* cross-service port expansion
  (`${PORT_auth.main}` or similar) — it makes one declaration depend on another
  service's allocator state, which means the supervisor must order allocation by
  a dependency graph it does not have, for two services whose ports never change.
  Revisit if a third Layer-0 service appears. *Rejected:* allocating and then
  rewriting the other side's declaration — two writes per bootstrap and a window
  where the pair disagrees.
- **The JWT issuer is the replica's loopback auth URL (`http://127.0.0.1:20101`),
  not production's `https://auth.lishuyu.app`.** Verified, not assumed: a Layer-1
  service builds its verifier as `M2MVerifier(..., issuer=config.auth_url)`
  (`api/services/kvservice/src/kvservice/main.py:299`), and `config.auth_url` is
  the `AUTH_URL` that `translate.py` injects as the replica loopback. Signer and
  verifier therefore agree only if Layer 0 issues under the same string.
  *Rejected:* keeping production's issuer for maximum compatibility with any
  hardcoded verifier — it would break every translated service in the replica,
  and it erases the one property that makes a replica token obviously a replica
  token. n=1 service inspected (kvservice); the other 20 are unread on this point.
- **Auth's health check is `kind = "tcp"`.** Auth registers `users`, `pats`,
  `tokens`, `jwks`, `oauth`, `email_login`, `password_login`, `webauthn_login`,
  `emergency` and `audit` — and no `/health`. *Rejected:* probing
  `/.well-known/jwks.json` — it is a real route with real work behind it, so a
  10 s probe interval turns the liveness check into load, and a JWKS 500 during
  a migration would be read as "process dead". *Rejected:* adding a `/health`
  route to `api/components/auth` — T1.4 is the only api-repo branch this phase
  and it is one file. A TCP connect is the honest signal for "the listener is up";
  registry keeps `http /health` because it has one.
- **No GitHub OAuth placeholder secrets.** The brief expected
  `AUTH_GITHUB_CLIENT_{ID,SECRET}` to be required; reading
  `auth.main._build_oauth_providers` shows the opposite — it builds the provider
  only when *both* are set, so the app starts fine with neither. Setting
  `replica-unset` would construct a GitHubProvider, render a login button, and
  send the user to a GitHub error page. Unset is both simpler and more honest.
  Consequence: no account can be created in the replica, so
  `AUTH_OPEN_REGISTRATION=0` and OAuth stays out of scope (PLAN Q4).
- **No secret value is shared between the two service ids.** Checked rather than
  assumed: registry never calls auth's `/api/pat/verify` (the
  `X-Auth-Verify-Token` header appears only in `auth/api/pats.py`), so
  `AUTH_PAT_VERIFY_TOKEN` stays auth-only and `REGISTRY_ADMIN_TOKEN` stays
  registry-only. The one genuinely shared secret is the RS256 **private key**,
  and it is shared as a file (a 0400 copy in each signer's own `<root>/etc/`),
  not as a store entry under two ids. That mirrors production's "registry is in
  the auth group" without needing a group — which a rootless harness cannot use,
  because the harness uid is not mapped into a service namespace (D4).
- **`[logging] format` is spliced into the emitted TOML here, not in
  `emit_toml`.** `translate.emit_toml` predates the field and 21 golden files
  pin its output; `render_declaration` inserts the two lines ahead of `[stop]`
  (the slot `examples/platform/caddy/service.toml` uses) and the round trip
  through `schema.loads` is asserted per declaration. *Rejected:* teaching
  `emit_toml` to emit `[logging]` — it is T1.2's module and every golden would
  churn for a field only Layer 0 and Caddy set today.
- **A declaration that drifted from the generator is rewritten, and the run says
  so.** Declarations are generated data; a hand-edit that survives is a
  configuration the generator cannot reproduce. Secrets and keys are the
  opposite — never overwritten, because rotating `AUTH_SESSION_SECRET` logs
  every user out and rotating `REGISTRY_ADMIN_TOKEN` locks the sync loop out of
  its own registry. A half-written keypair (one file of two) *is* regenerated:
  a public key that does not match its private key breaks every M2M verification
  silently and the two cases are indistinguishable after the fact.
- Open / weak signal: **nothing here has started a process.** n=0 live starts of
  registry or auth under these settings; `uv sync --frozen` in
  `repo/components/{registry,auth}` resolving the `../sdk` path dependency out
  of the staged tree is reasoned from `pyproject.toml` + `SourceMirror.stage`,
  not observed. Bringing them up is T3.2's deliverable. Do not treat the env
  contract as confirmed until then; in particular `REGISTRY_MIGRATIONS_DIR` and
  the two DB paths are set explicitly *and* would default to the same places, so
  a wrong value here would not fail loudly.
- Open / weak signal: `place_jwt_key`'s warm path trusts owner+mode as a proxy
  for content, because the 0400 private copy is unreadable to the harness by
  design. That holds only because `ensure_keypair` never rewrites an existing
  pair. If the store keypair is ever deleted and regenerated, the per-service
  `<root>/etc/jwt-rs256.*` must be deleted too — nothing detects this today.

### D23. Backup: stdlib sqlite backup in the admin ns, env-var rclone, prune is fatal (T2.4, 2026-09-02)
Replaces `api/bootstrap/05-db-backup.sh`. `src/ams/platform/backup.py`:
`discover` → `snapshot` → `upload` → `prune`, plus `restore` and a JSON-lines
report in `JsonLinesEscalation`'s shape. Choices and what was rejected:

- **No `sqlite3` CLI on the target — the stdlib module is the snapshot engine.**
  Checked on racknerd 2026-09-02: `command -v sqlite3` is empty; the venv and
  system interpreters both carry `sqlite3` 3.45.1. So the admin-ns step is
  `run_admin([sys.executable, "-I", "-c", _SNAPSHOT_CODE, src, dst])` — a fixed
  code string with both paths as argv, which is not a shell string (D1): nothing
  is interpolated and no quoting rule can reach the arguments. Same SQLite
  library, same online-backup API as `.backup`. *Rejected:* `apt install
  sqlite3` — a host dependency added for one call that the interpreter already
  has, and one more thing `install-host.sh` would have to guarantee.
- **The snapshot opens the source read-write, then chowns the WAL sidecars
  back.** As inner root, any `-wal`/`-shm`/`-journal` the open creates lands on
  the harness uid, after which the *service* can no longer commit — a backup job
  that breaks the thing it protects. `_SNAPSHOT_CODE` stats the db first and
  hands the sidecars back to its owner. Asserted live by
  `test_snapshot_leaves_the_service_able_to_write_its_database`. *Rejected:*
  opening `file:...?mode=ro` — read-only cannot recover a hot WAL, so it would
  silently back up a stale snapshot, which is worse than the problem it avoids.
- **No chown of the snapshot is needed.** The admin map is inner 0 ← harness uid
  (D4), so a file inner root writes is already harness-owned on the host. The
  Linux test asserts `st_uid == os.getuid()` rather than trusting the reasoning.
- **gzip via the stdlib, not `gzip -9`.** Identical format, one fewer binary
  dependency, `mtime=0` makes the output byte-stable, and it runs on macOS so
  archive naming and the round trip are testable off the target host.
- **rclone credentials as `RCLONE_CONFIG_R2_*` env vars, plus
  `RCLONE_CONFIG=/dev/null`.** Values come from the SecretStore under pseudo-id
  `platform-backup`. Verified offline on racknerd (n=1): with only those vars and
  no config file, `rclone listremotes` prints `r2:`. The `/dev/null` pin is the
  part the shell script lacked — without it a stray
  `~/.config/rclone/rclone.conf` could redirect an upload. *Rejected:*
  `/etc/db-backup/env` (a 0600 root-owned credential file, which is exactly what
  D16 exists to stop) and argv (visible in `/proc`).
- **Pseudo-service id is `platform-backup`, not PLAN-allin Q6's `_platform`.**
  `SERVICE_ID_RE` requires a leading lowercase letter, so the store would have
  rejected `_platform` outright.
- **The bucket is NOT redacted; the access key, secret key and endpoint are.**
  `R2Credentials.redact` scrubs subprocess output before it reaches a log record,
  an exception or the report. The bucket is a locator, not an authenticator, and
  it is half of every object key printed — a report that will not say where the
  archive went is one an operator cannot restore from.
- **A failed retention prune is a failure, not the shell script's `WARN`.**
  Retention that silently stops working is unbounded object growth and an
  unbounded bill, invisible until it is expensive — which is precisely what the
  litestream incident was. Retention stays 14 days, schedule stays 04:10 UTC
  +15m jitter, `Persistent=true`, `--s3-no-check-bucket` kept (it drops a
  HeadBucket Class-A op per upload, the whole point of that script).
- **Every service with a `<root>/data` is covered, except `registry-runtime.db`.**
  api-architecture.md calls the hardcoded three the platform's sharpest data
  risk. The one carried-over exclusion is the heartbeat db: ephemeral liveness
  state, re-reported within 30s, and restoring it resurrects stale "healthy" rows.
- **One failing target never stops the sweep**; each produces one `BackupFailed`
  record and `run` exits 1. The argument for covering every data dir collapses if
  one bad database can take the whole run down.
- **`restore` only ever writes a scratch path** and raises when
  `PRAGMA integrity_check` is not `ok`. Putting a db back under a live service
  means stop → replace a file owned by another uid → start; that is an operator
  decision with a blast radius, and `ams rm`-shaped work that Phase B owns.
- **rclone lives in `scripts/install-rclone.sh`, not `deploy/install-host.sh`**
  (owned by T2.2 this wave): pinned v1.75.0 linux-amd64, zip sha256 verified
  against upstream `SHA256SUMS` *and* the extracted binary hash pinned
  separately, idempotent, installed to `<store>/bin/rclone` beside the pinned
  Caddy. T4.4 folds it into `install-host.sh`.
- Open / weak signal: **the R2 upload is untested (n=0).** No credentials were
  available and none were sought; `upload`/`prune` are pinned by argv equality
  only. The transport is T4.2's live drill. Do not treat "backups work" as
  covering the network hop yet — what is proven is that a service-owned database
  can be snapshotted through the admin namespace and restored with its rows.

### D24 (T3.3). Platform policy: dedupe by normalized cause, recommend-only rollback (2026-09-02)
- **The cause key is `(service_id, kind, normalized text)`, not the raw line.**
  `normalize_cause_text` replaces ISO/CLF/clock timestamps, ipv4(:port),
  `pid=<n>`, 32–64-char hex ids, shorter hex ids containing a letter, and any
  remaining number of three digits or more. Two lines that differ only in which
  request, which pid or which ephemeral port are one *cause*; the whole module is
  worthless if they are two. Numbers of one or two digits are left alone ("3
  retries", "5xx") — collapsing them loses more than it gains. `kind` is in the
  key so an exit and a log line that normalize to the same string stay distinct,
  and `service_id` is in it so one noisy service cannot silence the same symptom
  in another. *Rejected:* hashing the whole line — a per-request id makes every
  occurrence unique and the deduper never fires. *Rejected:* a fixed
  rate limit per service (n lines per minute) — it drops real bursts and hides a
  crash loop's output, which D19 already rejected for self-probe suppression.
  *Rejected:* dedupe by `(service_id, severity)` — one ERROR then silence for the
  next, different, ERROR.
- **The 32–64-char hex rule does not require a letter; the 7+ rule does.** A git
  sha that happens to be all digits (~1e-8 of commits) must still collapse
  against one that is not, or that commit escalates twice. Below 32 chars the
  letter requirement keeps a plain 7-digit decimal out of `<hex>` so it gets one
  placeholder rather than two depending on its digits. Found by a test, not by
  reasoning: the first version had one rule and the all-digit sha case failed.
- **`window_s = 600`, `health_grace_s = 300`.** 10 minutes is longer than the
  10 s health-probe interval and the 1 s restart backoff can refill, and short
  enough that a condition still true afterwards gets said again rather than going
  quiet forever. 5 minutes of grace sits above the fleet's `start_period_s = 120`
  (PLAN-allin Q8) with room for a slow first start plus one restart. Both are
  guesses against the *plan's* numbers, **n=0 against a running fleet** — see the
  open section below.
- **A closed window emits one summary escalation, not silence.** "Escalate once
  and drop the rest" loses the fact that it happened 300 times, which is the
  difference between a blip and an outage. A window with a single occurrence
  emits nothing: "repeated 1 times" is not news.
- **The health gate measures from `stage_since`, not `updated_at`.** The brief
  said `updated_at`; the sidecar contract says `updated_at` is touched on *every*
  write, so a service the sync loop retries every 60 s would have a fresh
  `updated_at` forever and a gate keyed on it would never fire. `updated_at` is
  the fallback for a record written before `stage_since` existed. The crash-loop
  rule does use `updated_at`, because there the question is "did we touch this
  service recently", not "how long has it been stuck".
- **The gate recommends a rollback; it never performs one.** `rollback(id)` is
  T4.3. A policy that acted would be making an irreversible fleet decision from
  inside a single-threaded supervisor loop, on the strength of one state file
  written by another process — and PLAN-allin Q7 puts "whether a crash-looping
  service is rolled back or left failing loudly" explicitly on the agent's side
  of the mechanical/policy line. What the escalation adds over the supervisor's
  own retry-exhaustion record is the **sha pair**: "this started when you moved
  it from X to Y" is the sentence that makes the fix obvious.
- **Health-gate and crash-loop escalations are queued, not returned.** One event
  yields one `Decision`, and the crash-loop case must still return the `RESTART`
  the supervisor needs — returning `ESCALATE` instead would silently stop
  restarting a service to reduce log volume, a far worse bug than the noise.
  They are drained by `flush(now)`, which `ams run --policy platform` wires into
  `run_forever(on_iteration=...)`. `flush` also returns what it emitted, so a
  caller driving the supervisor directly (and every test here) needs no sink.
- **The health gate uses a `(service_id, sha, stage)` marker, not the deduper.**
  The requirement is "escalate ONCE", and the condition re-arms when the sync
  loop moves the service to a new sha or stage, not when a clock runs out. The
  deduper's window would have re-escalated a permanently-failed service every 10
  minutes forever. It also deliberately ignores the record's own `escalated`
  flag: that belongs to the sync loop's per-transition dedupe, and a policy that
  read it would go quiet because a *different* component had already spoken.
- **Caddy access lines carry the status in the cause `kind`, not the text.** The
  normalizer turns any three-digit number into `<n>`, so `502` and `503` on one
  path would have been one cause. Only the path is normalized, which is what we
  want: `/files/1234567` collapses. Found by a test.
- **The TLS suppression matches the logger *and* the message.** Caddy names the
  subsystem in `logger` (`tls`, `tls.issuance`) and does not always repeat it in
  `msg` ("stapling OCSP: no OCSP server specified"). Warn-level only, and only in
  Phase A where `auto_https off` means there are no certificates to get (D21). A
  malformed or non-JSON Caddy line falls through to `DefaultPolicy` untouched
  rather than being guessed at.
- **Registry-heartbeat suppression latches on the registry's own
  `HealthChanged(healthy=True)`.** A policy is handed only the context of the
  service the event belongs to, so there is no way to ask "is the registry up?";
  the one registry fact that passes through `decide` is its own health
  transition. Latched on purpose: a registry that flaps *later* does not re-open
  the window, because by then 20 services failing to heartbeat is real news.
  *Rejected:* reading the registry's `start_period_s` — it is in another
  service's declaration, which the policy cannot see. *Rejected:* reading the
  registry's state from `state.json` — that file describes the *sync* stage, not
  whether the process is answering.
- **`state.json` is read with an mtime+size cache, and absence is normal.** The
  read happens on the supervisor's thread, so the common case must cost one
  `stat`. A missing file is the state before the first sync, not an error. A
  corrupt file or an unknown `version` logs once at ERROR and yields an empty
  document: the sidecar contract says fail loudly rather than guess, and loudly
  *here* cannot mean raising — that would take down supervision of every service
  to complain about a file nobody had asked for yet.
- **`EscalationDeduper` is standalone so the sync loop can adopt it.** T3.1's
  `PlatformSync` records never pass through a `DecisionPolicy` (they are JSONL on
  stdout from a one-shot process), so its repeated translate/provision failures
  need the same treatment from the other direction. `DedupingEscalation` wraps
  any `Escalation` sink with it. Provided, not wired: T3.1 owns that call site.

### Open / weak signals (platform policy, 2026-09-02)
- **`window_s=600` and `health_grace_s=300` are n=0 against a running fleet.**
  They are derived from the plan's `start_period_s=120` and the 10 s probe
  interval, not measured. The failure mode to watch in T4.1 is a fleet start
  where 14 uvicorns on one core take longer than 300 s to go healthy and the gate
  fires on services that were merely slow. Expect to raise `health_grace_s`
  rather than to have confirmed it.
- **The Caddy rules have never seen a real Caddy log line.** Every payload in the
  tests was written by hand from the documented shape (`logger`,
  `http.log.access`, `status`, `request.uri`, `level`, `msg`). D19 flagged the
  same gap for the `json` format hint and it is still open. The first live fleet
  logs are the check; do not add speculative shapes before then.
- **The heartbeat patterns are guesses at the SDK's wording** (`heartbeat`,
  `acl-refresh`, plus the requests/urllib connection-refused phrasings), n=0
  observed lines. A miss is benign — the line escalates as it does today — so
  this stays a pattern list, not a parser.

### D25 (T3.4). Static publishing runs entirely as the harness; extra secret names via `service.ams.toml` (2026-09-02)

- **Static builds run inside the admin ns (`ams.userns.run_admin`), never as
  the bare harness process, but never chown to a service uid either.**
  `kind: static` produces no ams service (`docs/platform-sidecars.md`), so
  there is no eventual owner to chown to -- unlike `sources.SourceMirror.stage`
  (D19) and `runtime.provision` (D9), which both run in the admin ns *because*
  the tree ends up service-owned. Here the reason is different and narrower:
  `deploy.install` executes manifest/repo-controlled code (`bun install`
  postinstall scripts, `bun run build`), and that is untrusted input in the
  same sense D1 already treats log text as untrusted at the decision boundary
  -- it must not run with the harness's own ambient process identity. The
  admin ns's inner-root exec (mapped to the harness uid) gives `no_new_privs`
  + a scoped capability set for the same host uid, which is the containment
  this buys; it does **not** hide files from the process (same numeric uid,
  same DAC checks). Consequence: `publish_static`'s `block: UidBlock | None`
  is required only when `mount["build"]` is non-empty, and **any** `UidBlock`
  works there -- it is never chowned to, so T3.1 does not need a per-static-id
  allocation and can pass one shared placeholder block for every static
  publish. `_copy_reflink` (the source -> scratch-dir copy) and the final
  atomic rename both run directly as the harness with no admin ns at all: they
  execute no repo-supplied code, only `cp -a --reflink=auto` and `os.rename`
  over paths this module built itself.
  *Rejected:* running builds directly as the harness (no namespace) -- the
  team brief's explicit instruction, and the reasoning above backs it:
  provisioning (D9) already establishes "harness never execs repo-controlled
  tooling directly" as the platform's convention; a second, unconfined code
  path for exactly the same category of command (an npm-ecosystem install +
  build) would be the inconsistency, not the ns wrapping.
  *Rejected:* chowning the scratch dir to a per-static-id `UidBlock` so the
  build "belongs" to something -- there is nothing for it to belong to (no
  service, no registry record), and it would force T3.1 to allocate and
  persist a uid block for an id that will never appear in `state.
  list_service_ids()`, for zero benefit (the published tree is harness-owned
  either way, per D4/D21's file_server requirement).
- **The static site's source directory is assumed to be `checkout/apps/<id>`,
  not read from `deploy.source.path`.** Checked, not assumed: `translate.
  _translate_static` (`src/ams/platform/translate.py:441`) never reads
  `deploy.source.path` at all -- it is accepted by the YAML-subset table
  validator (`_SOURCE_KEYS` includes `path`) but silently dropped, while
  `_translate_service` explicitly rejects it (`sub-tree staging is only
  supported for kind=static`). So `mount.json` (the only sidecar T3.4 is
  handed) carries no field for it. Both real static manifests set
  `deploy.source.path: apps/<name>` verbatim (`api/apps/files-web/service.yaml`,
  `api/apps/llm-web/service.yaml`), matching `mount["id"]` exactly, so
  `checkout / "apps" / id` is not a guess against the two manifests that
  exist -- but it is a convention this module owns, not something the
  translator promises. `publish_static` raises `StaticError` naming the
  missing directory if a future static manifest's app does not live there,
  rather than silently publishing nothing.
  *Rejected:* teaching `translate._translate_static` to read and forward
  `deploy.source.path` into `mount.json` -- that is T1.2's module, mid-wave
  edits to it would churn the 21 golden files another agent's tests already
  pin, and the plan gave T3.4 exactly the five fields in `docs/
  platform-sidecars.md`'s `mount.json` table, `static_root` and `build`
  included, `source.path` deliberately not.
- **Build steps run through an argv allowlist
  (`bun`/`pnpm`/`find`/`cp`/`rm`/`mv`/`mkdir`), not an exact per-command
  regex like T1.2's `_INSTALL_RE`.** Checked: none of the five real
  `deploy.install` lines across both manifests need a shell -- no `&&`, `|`,
  `;`, `>`, or `$()` -- `shlex.split` tokenizes every one into a plain argv a
  direct `execve` runs unmodified (verified against the golden manifests via
  `translate()`, not hand-copied). `_INSTALL_RE`'s single recognised shape
  exists because the *service* form genuinely uses `&&` (`cd <rel> && uv
  sync`) and needs a real shell to mean what it says; the static form is a
  YAML list of already-separate commands, so there is no chaining to
  disambiguate. Any token that is a shell metacharacter or contains `$(`/`` ` ``
  still raises `StaticError` naming it, so a future manifest that *does* need
  shell semantics fails loudly instead of running with the wrong meaning.
  *Rejected:* exact full-string matching per the two real commands (as
  `_INSTALL_RE` does for the one service form) -- the five static commands
  share no common structure to regex against, so this would be five brittle
  regexes standing in for what the allowlist does in one pass, for no extra
  safety (the allowlist already rejects everything else).
  *Rejected:* no allowlist at all, just "reject shell metacharacters" -- would
  let any coreutils/PATH binary run as a build step (e.g. `curl` in a future
  manifest, quietly succeeding as a no-op fetch-and-discard rather than
  failing loudly), which is a worse failure mode than a `StaticError` naming
  an unexpected tool.
- **`service.ams.toml` overlays exist for commentservice, wechatservice,
  notificationservice, emailservice, oss** -- all five checked against
  source, cited by file:line in each overlay and in the commit that adds
  them (`api/` branch `ams-platform`, not pushed):
  - commentservice: `DEEPSEEK_API_KEY` (`main.py:527`, required once the
    moderation loop starts; `main.py:515` makes the loop itself optional --
    listed anyway because without it the feature the manifest exists to
    provide silently never runs, which is a worse failure than a set-but-
    unused name).
  - wechatservice: `WECHAT_MP_APPID`, `WECHAT_MP_APPSECRET` (`main.py:212-213`,
    both `os.environ[...]`, no fallback). `WECHAT_MP_APPID` is not itself
    sensitive, but its value is WeChat-account-specific and this translator
    has no source for it, so it goes through the SecretStore like the appsecret
    rather than being invented as an `[env]` literal.
  - notificationservice: `BARK_DEVICE_KEY` (`main.py:355`, `.get(...) or
    None` -- optional in code, load-bearing for the feature). `BARK_SERVER_URL`
    (`main.py:354`) keeps its public default and is not listed.
  - emailservice: `RESEND_API_KEY` (`providers/resend.py:23`, no fallback;
    reached because `main.py:377` defaults `EMAIL_PROVIDER` to `"resend"`).
  - oss: `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`
    (`r2.py:16-21`, all `os.environ[...]`, no fallback; reached
    unconditionally from `create_app`'s default `r2_client=None` path,
    `main.py:151-152`).
  - **llmgateway gets no overlay.** The brief speculated it would need one;
    `grep -n "os\.environ\[" -e "os\.environ\.get(" services/llmgateway`
    (n=1 service, every result read) shows only `SVC_ROOT_PATH` and five
    `LLMGW_*` numeric tunables, all with defaults, none third-party. Do not
    add one speculatively -- the pattern to watch for is a *new* provider
    integration landing in that service later.
  - Open / weak signal: **n=1 file per service, read once, on 2026-09-02.**
    A future change to any of these five services' provider wiring (a new
    optional channel, a provider swap) needs its own re-grep; this is not a
    standing guarantee the overlay stays complete.

### D24 (T3.1). Sync loop: two-pass around the reload, monotonic stages, fleet-wide gateway (2026-09-02)
- **The reload comes before the gateway render, not after.** `mount.json` carries
  the port *name* and `gateway.resolve_ports` follows it into the live
  allocation (D21); a service the harness has never seen has no allocated port
  until `ams ctl reload` registers it, and `resolve_ports` raises rather than
  rendering a `reverse_proxy` to nothing. So one tick is: declare everything →
  one reload → render/write the gateway → `restart caddy` if any file changed →
  registry + health gate. *Rejected:* rendering first and re-rendering after —
  two renders and a window where the config points at the old fleet.
  *Rejected:* allocating ports in the sync process — the allocator is the
  harness's (`state/ports.json`) and a second writer would hand out a port the
  supervisor is about to bind.
- **The gateway is rendered from the mount sidecars on disk, not from the
  services this tick processed.** The gateway is fleet state: rendering from the
  run's own list would make `gateway.write`'s stale-snippet deletion drop
  `sites/<id>.caddy` for every service excluded by `--only`, or for one whose
  manifest broke this tick — taking a still-running service off the gateway
  because of an unrelated typo. A declared service with no allocated port yet is
  skipped with a warning rather than raised on, so one such service cannot stop
  the gateway being rendered for the other twenty.
- **A stage never moves backwards, and a `failed` record is never rewound.**
  Every tick re-walks every phase, so `advance()` only writes when the new stage
  sorts above the current one (`failed` sorts below all). Rewinding a failed
  record to `fetched` at the top of each tick would move `stage_since` and clear
  `escalated` every 60 s, turning "escalate once per cause" into "escalate per
  tick" — the exact failure T3.3 exists to prevent. For the same reason
  `set_stage` stamps `updated_at` only when `(sha, stage, error)` actually
  changes, and `PlatformState.flush` compares against what it loaded: **a tick
  that changes nothing writes nothing** (asserted by a whole-tree mtime snapshot).
  The escalate-once check reads a snapshot of the record taken *before* the tick
  touched it, not the live record, which `fail()` has already rewritten.
- **`error` is `"<transition>: <message>"` and `stage` is `failed`.** The sidecar
  doc says `failed` is the stage and "`error` says which", so the transition is
  encoded in the message rather than added as a second field — the shape stays
  the one `docs/platform-sidecars.md` documents, and the escalation record
  carries the transition in `event.stage` for a machine reader.
- **Provision is skipped only when the tree is already at the sha AND the venv
  exists.** `SourceMirror.stage` no-ops on a matching `.ams-sha`, so the marker
  is read before calling it to know whether anything moved. A missing venv with
  a matching marker still provisions: that is the "provision crashed last tick"
  case, and re-running `uv sync` is cheap and idempotent.
- **`manual_restart`: a changed declaration is not written, and the service is
  marked `failed` with `error="declare: declaration changed but manual_restart
  is set"`.** Writing it would make the reload restart the service on its
  declaration hash (D17), which is the one thing the flag exists to prevent. The
  *first* declaration is still written — nothing is running yet, so there is no
  restart to avoid. *Rejected:* writing it and recording `stage="declared"` with
  an informational escalation — the harness would restart the service on the
  next reload from any source (SIGHUP, another agent), so the flag would be
  honoured only by luck. *Rejected:* leaving the stage at `translated` with a
  note — an operator scanning for what needs attention greps `failed`, and this
  does need attention.
- **A run-level failure (fetch, reload) stops the tick with one escalation and
  leaves every record at its last reached stage**, rather than marking each
  service failed. Nothing is wrong with the services; the tick could not
  proceed. A failed reload leaves them at `declared`, and the next tick's
  "declared but not live" check re-issues the reload (regression-tested).
- **`SyncConfig.__post_init__` enforces the loopback gate by constructing a
  throwaway `TranslateContext`** rather than re-implementing the regex. There
  must be exactly one gate (PLAN-allin risk 5) and this way it fails once,
  before the fetch, instead of once per manifest — and the two can never
  disagree. *Rejected:* copying `_LOOPBACK_URL_RE` into sync.py.
- **`sync(state, store, cfg)`'s `store` is the `RuntimeStore`, and the
  SecretStore defaults to `store_for(state)`.** `provision`, `place_jwt_key` and
  `SourceMirror` all need the reflink store, which only `$AMS_STORE_DIR` knows;
  D16 fixes the secret store at `<state>/secrets`, so it is derivable. Both are
  still injectable for tests.
- **`--only` / `--skip` match either the manifest's directory name or the
  translated id.** The id only exists after the manifest parses, which is
  exactly what a filter cannot rely on when the manifest is the broken thing; a
  translate failure for a service that is not selected is silent.

- **T3.4 hooks: `load_ams_overlay`, not `overlay_secret_names`.** The brief named
  the secrets-only wrapper; the full reader is used instead because it returns
  both halves from one read of one file, and T3.4's own docstring assigns the
  `[env]` merge to its caller — which is this loop. `[env]` is applied with
  `dataclasses.replace`, whose `__post_init__` re-runs `schema.validate`, so a
  reserved or secret-colliding name fails that one service at `translate` rather
  than at spawn. sync.py's own `service.ams.toml` parser was deleted rather than
  left beside T3.4's: two readers of one file is how they drift.
- **`publish_static` is a static mount's `provisioned` step**, run before the
  gateway render and after nothing else — a published tree is what the
  `file_server` in the rendered site block points at, so publishing after the
  render would leave one tick where Caddy serves a directory that is not there.
  A uid block is allocated **only** when `mount["build"]` is non-empty: the tree
  is harness-owned and never chowned (D25), the block exists only so `run_admin`
  can build a two-range map, and a site with no build steps should not consume
  one of the 64 blocks.
- **`DedupingEscalation` (T3.3) was not adopted, deliberately.** It is not a
  drop-in: its `escalate(event, decision, ctx)` wants an `Event` and a
  `ServiceContext` carrying a `ServiceDecl`, and a translate failure is
  precisely the case with no declaration to put in one — synthesising placeholder
  objects to satisfy the protocol would be ceremony, not dedupe. More
  importantly its window is in-process wall time, and this is a one-shot process
  that exits between ticks, so it would never observe a repeat: the cross-tick
  dedupe is the persisted `escalated` flag on the record, which is what
  `docs/platform-sidecars.md` specifies and what the tests pin. Within a single
  tick a service escalates at most once by construction (every phase returns on
  failure), so there is nothing left for a window to collapse. Revisit if the
  sync loop ever becomes long-lived.

### Open / weak signals (sync loop, 2026-09-02)
- **`GIT_COMMIT` makes every commit restart every service.** `translate` injects
  `GIT_COMMIT=<sha>` into every declaration (PLAN-allin Q2) and the harness
  restarts on a declaration content hash (D17), so one unrelated commit to the
  monorepo rewrites all 21 `service.toml` files and restarts the whole fleet.
  Pinned by a test (`test_every_declaration_carries_the_commit_so_every_service_is_rewritten`)
  so it cannot change silently. This is a translation-layer decision and was
  deliberately **not** worked around here: diffing declarations modulo one key
  would leave the file on disk disagreeing with the running process, and
  dropping the variable is T1.2's call. Options if it bites at T4.1 scale: drop
  `GIT_COMMIT` from the declaration and inject it at spawn from `.ams-sha`, or
  accept a fleet restart per commit. n=0 measurements of what 21 simultaneous
  restarts cost on one vCPU.
- **A new sha re-stages and re-provisions every service, not just the changed
  one.** `stage` copies the whole monorepo, so every service's tree moves on
  every commit and `uv sync` re-runs (a commit can change a lockfile). Cheap on
  reflink (D19: 0.01 MiB per stage) but not free in time: 20 × `uv sync` on one
  vCPU is unmeasured (n=0). Also note the tree is swapped *under a running
  process*; `stage`'s two-`mv` swap is atomic for the directory but a process
  holding an open file keeps the old inode. Not observed to break anything —
  and not tested live either.
- The two-pass ordering, the reload, the restart and the health gate are all
  exercised against recorders. **n=0 against a real harness or registry**; T3.2
  and T4.1 are the live gates.
- **A static site republishes on every commit**, because `publish_static` is
  idempotent by sha and the sha changes with every commit to the monorepo — the
  same shape as the `GIT_COMMIT` finding above, and unmeasured for a real
  `bun run build` (the test fixture declares no build steps, so **n=0 static
  builds have ever run**). If T4.1 finds this expensive, the fix is a content
  hash of `apps/<id>` rather than the commit sha, which is T3.4's call.

### D24 (T3.2). Live Layer 0: fixed Caddy port, a local mirror instead of GitHub, and two allocator/permission fixes the gate forced (2026-09-02)

- **Caddy's port is fixed at 20180, not allocated.** `gateway.caddy_declaration`
  asks for `ports.main = 0`, which is right for a gateway whose config is
  rendered after the fact. This bring-up cannot do that: the entry site's own
  listen address lives *inside* the Caddyfile, and the Caddyfile must exist
  before the reload that starts Caddy. `layer0._caddy_declaration_text` rewrites
  that one line. *Rejected:* changing `gateway.py` to take a port — T2.2 owns it,
  its golden files pin the text, and one caller wanting a fixed port is not yet a
  reason to change a signature (a second caller would be). *Rejected:* two
  reloads (allocate, then render, then restart Caddy) — it starts a gateway
  against a config that does not exist, which is a crash-loop by construction.
  The rewrite is guarded: if `main = 0` ever leaves the generator, `layer0`
  raises "stale" rather than shipping a gateway whose config cannot know its own
  port.

- **Source is a bare mirror pushed to `<store>/upstream/api.git`, not
  `https://github.com/StevenLi-phoenix/api`.** The repo is private: from racknerd
  anonymous HTTPS gets a 404 and `git ls-remote` asks for a username. The harness
  holds no GitHub credential and none was created — `sources.validate_url` exists
  to refuse credential-bearing URLs, and widening the harness's reach to fetch a
  private repo is a decision for a human, not a side effect of a bring-up.
  `validate_url` already accepts an absolute local path, so every stage after the
  clone is the real code path. *Consequence, recorded rather than worked around:*
  Phase A runs upstream `main`, which lacks the T1.4 SDK root-logger patch (it
  lives on the local `ams-platform` branch, never pushed), so SDK
  `logger.warning` is still classified INFO. *Rejected:* a deploy key on the box —
  it makes the harness able to read a private repo forever to save one rsync.

- **Order is the deliverable, and it is not the obvious one.** Identities must be
  created *before* a Layer-1 service starts, because a translated declaration has
  no `SVC_DEV`: the SDK registers inside the FastAPI lifespan and a 404 there is
  a uvicorn startup failure, i.e. a crash loop. So the bring-up reloads with the
  Layer-1 services deliberately left down and starts them only after
  `create_identity`. And a service is stopped before its tree is re-staged,
  because `stage()` swaps `<root>/repo` and the venv lives inside it. Both were
  observed live when a manual restart put the services back up inside that
  window: `RegistryError: register failed: 404` and
  `ModuleNotFoundError: No module named 'fastapi.datastructures'`. T3.1's sync
  loop needs the same two rules.

- **`UidAllocator.allocate` now re-reads its state file on a cache miss.** The
  harness constructs one allocator at start and keeps it; `ams provision` and the
  platform bring-up allocate from a second process. Without the re-read the
  harness re-carved blocks that another process had already staged files under,
  and `ensure_service_root`'s recursive chown then ran in a namespace with no
  authority over those files — EPERM on every one, and the service skipped. This
  was **not** hypothetical: it is how the first live bring-up failed, and it
  would have broken T3.1's `ams provision` → `ams ctl reload` loop identically.
  The warm path is untouched (a known id never reaches the re-read). *Rejected:*
  restarting the unit whenever a Layer-0 declaration changes — that defeats D17's
  whole point. *Rejected:* having `layer0` predict the harness's numbers — it
  moves the race rather than removing it. **Open / weak signal:** this narrows
  the window, it does not close it. Two processes can still interleave
  `allocate` → `_save`. The real answer is an allocation op on the control
  socket, so the harness stays the single writer; Phase B.

- **`StateDir.ensure()` treats 0750 as a floor, preserving a deliberate `o+x`.**
  `ams.cli._ensure_traversable` adds `o+x` to `services/` so a service uid can
  resolve its own workdir by path; `ensure()` runs from every entry point that
  touches the state dir, including `ams.platform.bootstrap` *while services are
  running*. Re-imposing exactly 0750 took the bit back and the next spawn of
  every running service died with `PermissionError` on its own interpreter —
  and `hello` began answering 404, which is what its two `HealthChanged`
  escalations were. Only the execute bit survives, and only on a directory that
  already existed; `o+r` is still never granted, so these directories stay
  unlistable. *Rejected:* re-widening from `layer0` after calling `bootstrap` —
  it fixes one caller and leaves the landmine armed for every future one.

- **Both fixes are outside T3.2's named file scope** (`ams/uidmap.py`,
  `ams/state.py`). Taken anyway: the alternative was a bring-up that only works
  when nothing else is running, plus a defect that T3.1 would have rediscovered
  from scratch. `cli.py` and `schema.py` were not touched (T3.1 owns the former),
  and `bootstrap.py` needed no correction — Q4's env contract was right first
  time.

- **The M2M policy row was inserted by hand.** `service_policies` has no write
  API upstream (by design: "managed out of band"), and production seeds it
  through migrations. One row, `timeservice → kvservice`, was inserted into the
  replica's `registry.db` through the admin namespace to make the token test
  possible. Open: T4.1 needs a real answer for the fleet, and it is not obviously
  ams's job — a policy is a statement about who may call whom, which is registry
  data, not supervisor data (D7).

### D26 (T3.1). Per-service change detection: the deployer's path-prefix rule, applied per commit range (2026-09-02)
- **The problem.** `translate` injects `GIT_COMMIT=<sha>` into every declaration
  (PLAN-allin Q2) and the harness restarts a service when its declaration's
  content hash changes (D17). Translating all 21 manifests at the repo head
  therefore rewrote all 21 `service.toml` files on *any* commit and restarted
  the whole fleet -- 20 simultaneous uvicorn imports on one vCPU (Q8's named
  risk) for a README typo.
- **The fix: a service is translated at the sha it is already deployed at,
  unless the commit range actually touched it.** `SourceMirror.changed_paths`
  (new, T1.1's file) runs `git diff --name-only --no-renames <deployed> <head>`
  against the bare mirror; a service is **affected** iff it has no deployed sha,
  or the diff is unavailable, or any changed path is under its own manifest
  directory or under a shared prefix. An unaffected service is translated at its
  deployed sha, so its declaration is byte-identical, nothing is written, the
  reload restarts nothing, and it is not re-staged, re-provisioned, re-registered
  or re-probed. A docs-only commit is a whole-fleet no-op (tested).
- **`--no-renames`** on the diff: with git's default rename detection a file
  moved between two services' directories is reported under the destination
  only, and the service that *lost* it would look unaffected. Listing both sides
  cannot under-report.
- **The rule is `changes.py`'s, and the shared-prefix set is where they differ.**
  `api/components/deployer/src/deployer/changes.py` matches changed paths against
  `apps/`/`services/` roots and fans out only on `shared/` -- it **removed**
  `components/` as a shared prefix on 2026-07-02 because trust-root pushes
  (registry/auth/sdk) restarted every service, and it can afford that because a
  deploy rsyncs the whole work-tree onto the box, so a service picks up a new SDK
  at its own next deploy. **ams cannot afford it**: each service has its own
  `<root>/repo` copy, so an unaffected service keeps its old tree *entirely* and
  would never see the new SDK until something else changed it. So the default is
  `("shared/", "components/sdk/")` -- `shared/` verbatim from the deployer, plus
  the one prefix whose staleness ams (and not the deployer) would silently keep.
  `components/registry|auth|deployer` deliberately do **not** fan out, matching
  the deployer's 2026-07-02 decision. The set is `SyncConfig.shared_prefixes`, so
  reverting to the literal mirror is one argument, not a code change.
  *(The task brief asked to "mirror changes.py's rule exactly" and also to treat
  `components/sdk/` as fan-out; those two are not the same rule. This is the
  reconciliation, and both halves are tested.)*
- **`deployed_sha` is additive to the version-1 record**, alongside `sha` (the
  commit the service is being *driven to*) and `prev_sha` (the last one that
  reached `healthy`). The three genuinely differ: a service that failed at
  `register` has `sha` = head, `deployed_sha` = head, `prev_sha` = the older
  healthy commit; one that failed at `provision` has `deployed_sha` still at the
  old tree. It is set when the declaration is confirmed on disk, and falls back
  to the on-disk `.ams-sha` marker when absent, so a state file written before
  this field existed (or restored from a backup) heals on the next tick instead
  of redeploying the fleet. The version stays 1 because the shape only grew a
  key and every reader (T3.3's `policy.py` included) reads records with `.get`.
- **Two translate passes for a service that stays put**, rather than deriving the
  id from the directory name. The affected check is keyed by the service id, and
  the id only exists once the manifest parses; `translate` is pure and
  sub-millisecond, so translating at the head and then re-translating at the
  deployed sha is cheaper than the assumption that `name == dirname` -- which
  nothing else in the module makes. A re-translate that somehow fails falls back
  to the head rather than dropping the service.
- *Rejected:* **dropping `GIT_COMMIT` from the declaration** (inject it at spawn
  from `.ams-sha` instead). It removes the symptom and the signal together: the
  declaration is the artifact an operator reads to answer "what commit is this
  service running", and `ams validate` / a golden diff would no longer show a
  deploy at all. It is also T1.2's field to remove, not this loop's.
- *Rejected:* **masking it** -- comparing declarations modulo `GIT_COMMIT` and
  writing the new file anyway. The file on disk would then disagree with the
  running process about its own commit, and the next reload from any other source
  (SIGHUP, an operator, another agent) would restart the service anyway, so the
  suppression would hold only by luck.
- *Rejected:* **content-hashing each service's subtree** instead of diffing
  commits. It is strictly more work (hash 21 subtrees per tick vs one `git diff`)
  and answers a narrower question -- it cannot see a shared-prefix change at all
  without hashing those too, which is the fan-out case that matters most.
