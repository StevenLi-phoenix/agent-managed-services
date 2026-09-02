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
