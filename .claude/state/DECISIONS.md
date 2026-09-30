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

### D27. Start order: a `waiting` state in the supervisor, gated on health (2026-09-02)

`depends_on` is a top-level list of service ids. A service with dependencies is
not spawned until every one of them is `running` **and** `healthy`; until then it
sits in `status="waiting"` with `waiting_for` naming what it is holding out for.
`health.kind="none"` degrades "healthy" to "running", because there is no probe
to pass — which is precisely why the registry and every Layer-1 service declare a
real http/tcp check.

- **Why health and not merely "spawned".** The incident (racknerd, 2026-09-02
  ~19:00 UTC) was not that the registry had not been forked; it was that it had
  not yet bound its socket. 11 services took `Connection refused` from
  `sdk.registry.start()` inside FastAPI startup — which is fatal, not retried —
  and burned all 5 retries within 90 s. A gate that only waited for a pid would
  have changed nothing.
- **Why the supervisor and not the SDK.** *Rejected:* teach `sdk.registry.start()`
  in the `api` repo to retry a refused connection. It fixes these 11 services and
  nothing else: ordering is a property of the fleet, not of one client library,
  and ams has to host services whose SDK we do not own (Caddy today, anything
  tomorrow). It is also the wrong repo — a supervisor that cannot express "after
  X" is the actual gap.
- **Why not systemd-style `After=`.** *Rejected:* order by "has been started"
  without a readiness condition. That is the semantics that produced the incident
  in the first place; systemd needs `Type=notify` or an explicit
  `ExecStartPre`-style probe to get what we get from `health`, and we already own
  a health monitor per service.
- **Reusing `restart_at` as the re-check timer.** The wait is re-evaluated by
  setting `restart_at = now + 1 s` and letting `_run_timers` call `start()` again,
  which re-runs the gate. No new timer field, and the `_poll_timeout` invariant
  (D13 addendum: fold in only deadlines `_run_timers` acts on) holds unchanged —
  the deadline is always in the future, so it can never clamp the poll timeout to
  0. *Rejected:* a dedicated `dep_check_at` field, which would have meant a fifth
  term in `_poll_timeout` and a fifth chance to get the spin guard wrong.
- **Start gating only: no stop or restart cascade.** If a dependency later goes
  unhealthy or exits, its dependents keep running untouched. A cascade is a much
  larger blast radius (one registry restart would bounce 11 services) and the
  services are written to tolerate a registry that comes and goes *after*
  startup — it is only startup that is fatal. Deliberately a Phase-B question.
  A dependent that is *itself* restarting does re-wait: the gate runs on every
  start, not just the first.
- **Unknown dependency = wait, plus one escalation.** A declaration may honestly
  be registered after its dependents (a reload walks the directory in name
  order), so refusing to start would be wrong. A typo looks identical from here,
  hence the escalation — deduplicated by message so a 1 Hz re-check does not
  flood the agent. *Rejected:* failing the service outright, which would make
  reload order load-bearing.
- **Cycle = `failed`, once, both members.** Detected by DFS over the registered
  declarations at every start. Unlike an unknown id, a cycle cannot resolve
  itself, so waiting would be an unexplained hang; `failed` plus an escalation
  naming the path (`a -> b -> a`) is the honest report. Self-reference is
  rejected earlier, by the schema.
- **Shutdown is the reverse topological order** (Kahn layers, ties in
  registration order, cycles collapsed into one final layer). Tearing a
  dependency down while its dependents still serve is the same outage at the
  other end of the lifecycle. Best effort: the whole sequence shares the existing
  shutdown deadline, so one service ignoring `SIGTERM` cannot hold the fleet
  hostage. With no `depends_on` anywhere there is one layer and the behaviour is
  byte-for-byte the previous one.
- **The translator injects it; Layer 0 and the gateway do not get it.** Every
  translated manifest gets `depends_on = ["registry"]`
  (`translate.DEPENDS_ON`). `registry` is the root. `auth` deliberately does
  **not** depend on it: its only registry use is `email_client_from_env()`, and
  `M2MClient.start()` just constructs an httpx client — no call at startup, so
  auth is fine with the registry down (read in
  `api/components/auth/src/auth/{main,email_client}.py`). Caddy proxies whatever
  happens to be up. Anything else would risk a cycle or would hold the gateway
  down for the whole of Layer 0's start.

### D27 (T4.3). Rollback restores code, never data; and the e2e's process topology is load-bearing (2026-09-03)

- **What a rollback moves: the tree, the venv, the declaration, both sidecars.
  What it does not: `<root>/data`, secrets, the registry, the gateway, and every
  other service.** Each exclusion is a decision, not an omission. *Data* is out
  because a rollback undoes code, not the migration the newer code ran; reversing
  rows is a restore from the T2.4 backup, a different operation with a different
  blast radius, and silently reverting a schema under a service is how you lose
  writes. *Secrets* are out because `SVC_SECRET` belongs to the identity, not to
  a commit — `rollback` never reads a value, never generates one, and fails at
  `declare` if it is missing rather than minting a replacement that would
  invalidate the registry's copy. *The registry* is out because the identity is
  sha-independent and the ACL is re-upserted from the (now rolled-back)
  `registry.json` by the next sync tick; consequently nothing here calls an admin
  endpoint and nothing here needs the admin token, which is why the health gate
  is a plain `urllib` GET (`rollback.wait_healthy`) rather than
  `RegistryClient.wait_healthy`. Borrowing that method would have meant passing a
  placeholder token to a constructor that rejects an empty one — a stub that
  later gets mistaken for a real credential. *Rejected:* re-rendering the
  gateway. `mount.json` **is** rewritten, but the Caddy config is fleet state
  assembled from every mount sidecar (D24) and re-rendering restarts the gateway
  for all twenty routes to fix a case (the mount path changed between two
  commits) that is rare and self-heals on the next tick. The report warns when
  the sidecar actually changed.
- **`prev_sha` is cleared on a successful rollback, kept on a failed one.** It
  means "the last commit that reached healthy", and after a rollback to X that
  commit is X, which is now in `sha`. Leaving the old value would make a second
  rollback target the commit we are already on. On failure nothing has replaced
  the older healthy commit, so it stays. `sync.begin()` refills it on the next
  move, so the record heals itself.
- **The sync timer undoes a rollback, and that is said out loud rather than
  worked around.** `sync` drives every affected service to the head of the
  branch, and the range from the rolled-back sha to the head necessarily touches
  this service — that is why it was rolled back — so the next tick redeploys it.
  Both the report's `warnings` and the escalation say so. *Rejected:* a
  `pinned_sha` on the record. The record is `sync.py`'s (out of this task's file
  scope), and a field nothing reads that *looks* like a pin is worse than no
  field: an operator would believe the service was held. It is one line in
  `ServiceRecord` plus one check in `target_sha` whenever T3.1 wants it.
- **`rolled_back_from` is written by editing the raw JSON document, not through
  `PlatformState`.** That loader keeps only the keys its dataclass declares, so
  round-tripping would drop the field from *every other* service's record too.
  `rollback.load_state_doc` version-checks and otherwise passes the document
  through untouched (pinned by a test). The field still survives only until the
  sync loop next rewrites the file — see the one-line addition in PROGRESS.md.
- **`cfg: SyncConfig` is a required argument, not a default.** Every knob in it
  (memory floor, cpu cap, the loopback registry/auth URLs) feeds the
  `TranslateContext`, and a rollback translated under different caps writes a
  declaration the next sync tick immediately rewrites — an invisible second
  restart of the service that was just brought back. *Rejected:* defaulting
  `repo_url` to `DEFAULT_REPO_URL`; on racknerd the source is a local mirror
  (D24/T3.2) and a silent default would fetch the wrong thing.
- **Stop, then stage, then reload, *then* restart.** The stop is D24 (T3.2)'s
  live lesson: `stage()` swaps `<root>/repo` and the venv lives inside it, so a
  process exec'ing during the swap dies with `ModuleNotFoundError`. The reload is
  not redundant with the restart — `restart` alone re-spawns the declaration the
  harness still holds *in memory*, i.e. the bad commit's, against the newly
  staged tree. And `reload` deliberately leaves a service alone when an operator
  stopped it (D17), which the stop just did, so the restart is what brings it
  back. `_wait_stopped` treats `waiting` as down alongside `stopped`/`failed`:
  a `depends_on`-gated service has no process, which is all staging needs.

### Open / weak signals (rollback + platform e2e, 2026-09-03)

- **`tests/linux/test_platform_e2e.py`'s topology is not a style choice.** The
  supervisor is in the pytest process and the sync/rollback are *separate
  processes*, which is production's shape (D17). The first version ran the sync
  in a thread and failed on the box: `ams.userns.run_admin` is a bare
  `os.fork()`, and forking a multi-threaded process gives the child only the
  forking thread, so a lock another thread held is held forever — `rm -rf`, `mv`
  and `uv python install` hung and were SIGKILLed by their own timeouts (`rc=-9`,
  no stderr, intermittent per service). **Nothing may add a thread to a process
  that forks into a user namespace.** That also rules out a threaded fake
  registry, which is why the e2e's registry is a declared ams service.
- **Nothing may be forked before `build_supervisor`.** `CgroupRoot.discover`
  moves *self* into `<delegated>/harness/` and then enables controllers on the
  delegated root; cgroup v2 refuses that (EBUSY, "no internal processes") while
  any process still sits directly in the root. A helper started earlier is
  exactly such a process. Both of these cost a remote round trip each to find and
  are worth knowing before the next Linux test is written.
- **The e2e's containment claim rests on pids, not on our own records.** Every
  injection asserts both the failing service's record *and* that the untouched
  services' pids did not change. n=3 full runs on racknerd, 3 green, ~49 s each.
  The live fleet's pids were captured either side of a full run and were
  identical (16 services, T4.1's in-flight fleet).
- **The memory injection is a real cgroup OOM, confirmed from the logs**
  (`memory.max` 67108864 -> 33554432, `exited signal=9 uptime=0.26s` twice, then
  67108864 again after the rollback), not merely a health-gate timeout. n=1 host,
  n=3 runs.
- **A rollback of a `kind: static` mount raises `RollbackError`.** A static site
  has no process, no declaration and no health probe, so "restart it and gate on
  health" has no meaning; republishing at an older sha is `publish_static`'s job
  and belongs to T3.4. Revisit if a static site ever needs pinning.
- **Untested: a rollback while the harness is down.** `_step_stop` fails with
  `stop: ControlError: ...` and the record is marked `failed` with nothing staged,
  which is the intended shape, but it has n=0 observations — the portable tests
  cover a *refused* stop, not an absent socket.

### D28 (T4.1). Live fleet: a registry burst cap, two Caddy suppressions, and a throttled timer (2026-09-03)

The tier-1 bring-up on racknerd. Every claim below is from a live run; the
transcript with the tables is `.claude/state/platform-fleet.md`.

- **`--ref ams-platform`, not `main`, and `--repo` is the local bare mirror.**
  `StevenLi-phoenix/api` is private and the harness holds no credential for it
  (platform-layer0.md §6), so the mirror at
  `/home/harness/store/upstream/api.git` is the only source `sources.validate_url`
  will accept. The branch is the one carrying the SDK root-logger patch
  (level-tagged logs, PLAN-allin Q5a) and T3.4's `service.ams.toml` overlays;
  upstream `main` stays fetchable in the same mirror, so nothing was lost.
  *Rejected:* running `main` and accepting untagged SDK log levels — T3.2 already
  measured what that costs (every `logger.warning` classified INFO), and the
  overlays are the only place the fleet's third-party secret names exist.
  *Rejected:* a deploy key or a token in the fetch URL — `validate_url` refuses
  it by design, and a private-repo credential on the replica is a Phase-B
  decision, not a bring-up convenience.

- **The registry's memory cap is `700M`; auth stays at `200M`.** The single
  sharpest finding of this task, and it was a **burst, not a leak** — measured,
  not assumed. Registry was OOM-killed twice inside its own cgroup
  (`oom_memcg=/system.slice/ams-harness.service/svc-registry`, dmesg): at a 200M
  cap with `anon-rss:202988kB`, and again at a 320M cap with `anon-rss:325416kB`.
  Memory tracking the cap that closely looks exactly like a leak, so it was
  tested rather than guessed: cap raised to 700M, the fleet restarted at once to
  force the burst, then `memory.current` sampled every 10 s for 5 minutes.
  `memory.peak` jumped to **397 852 672 (379.4 MiB)** at t≈30 s and never moved
  again; `memory.current` fell back to **~64 MB** and crept 6 MB over the
  remaining 4.5 minutes. So the registry's steady state is ~64 MB and its
  *concurrent-registration burst* is ~379 MiB, and both earlier kills were that
  same burst clipped at whatever ceiling existed. Because every Layer-1 service
  registers inside its FastAPI lifespan, the registry's death is the whole
  fleet's: one cap, eleven casualties. 700M is deliberately loose (≈1.85× the
  measured burst) because the burst scales with the number of services
  registering at once and Phase A has no stagger; it costs nothing while unused,
  since `memory.max` is a limit and not a reservation, and the process
  demonstrably sits at 64 MB. *Rejected:* keeping 200M and fixing the burst —
  the burst is upstream registry behaviour (FastAPI + the MCP session manager +
  9 simultaneous registrations), not something this repo can change, and a cap
  that kills the trust root under normal fleet start is not a guard, it is an
  outage generator. *Rejected:* a tight 512M — it is only 1.35× a burst measured
  **once**, at 9 services, for a fleet meant to reach 21. Revisit with a real
  measurement at full fleet size. n=2 kills, n=1 clean burst measurement.

- **Two more Caddy warnings are suppressed by `PlatformPolicy`
  (`_DELIBERATE_MSG_RE`).** platform-layer0.md §7 classified four Caddy start-up
  WARNINGs as noise; the shipped `_TLS_MSG_RE` caught only the two that say
  "requires TLS". Live, `admin endpoint disabled` still escalated on every start
  and `exiting; byeee!!` on every stop — both statements of fact about settings
  the operator chose (`admin off` is D21/Q3; the SIGTERM is an operator stop).
  Matched on the **message**, not the logger, because `admin` is also the logger
  of real admin-API errors, and a test pins that
  `{"level":"error","logger":"admin"}` still escalates. Verified live: zero
  Caddy escalations from the harness pid running the fixed policy, against one
  per start before. *Rejected:* widening `_TLS_MSG_RE` — the suppression reason
  it prints ("TLS/certificate warning") would then be a lie for two of the four.

- **The sync timer's `ExecStart` carries an explicit `--only` list, and `oss` and
  `secretsservice` are deliberately not in it.** Neither can start in the
  replica for reasons a retry cannot fix: `oss` calls `ensure_bucket()` against
  R2 inside its lifespan and dies on the placeholder credentials with
  `SSLError ... replica-placeholder.r2.cloudflarestorage.com`; `secretsservice`
  requires `SECRETS_MASTER_KEY`, which no manifest and no `service.ams.toml`
  declares. Leaving them in the list spends 2 × 90 s of health gate on every 60 s
  tick — back-to-back ticks on one vCPU — which is the same reasoning that
  already keeps `Restart=` off the unit. Both keep their `failed` + `escalated`
  records, which is the honest state. *Rejected:* syncing the whole fleet
  unfiltered — 2 GB cannot hold 21 services (§2 of the fleet report) and an
  unattended tick that picks up a new upstream manifest would start it into a
  fleet that has no admission control.

- **`service_policies` needs no manual step for the fleet, and T3.2's hand-inserted
  row was the exception.** Checked rather than assumed: the registry has no write
  API for M2M policy by design, but migrations `003`–`009` seed it, and the
  replica's `registry.db` already carries all eight upstream rows (`* →
  {emailservice,logservice,messageservice,notificationservice}`, `mailbox →
  {kvservice,messageservice,oss}`, `pages → oss`) with `schema_version` at 10 —
  because `ams platform bootstrap` points `REGISTRY_MIGRATIONS_DIR` at the staged
  tree and the registry runs them at startup. The only hand-written row is
  `('timeservice','kvservice')`, a pair no migration declares, invented for
  T3.2's M2M proof. So the Phase-B item is not "ams must script this" but "a new
  M2M pair still needs an upstream migration", which is upstream's design.
  *Rejected:* scripting a `service_policies` upsert through `run_admin` + sqlite
  for every service — it would write rows the migrations already own, and a
  second writer to a migration-managed table is how the two drift.

- **Placeholder secrets are `replica-placeholder`, set for the eight overlay
  names, and the report says which services are therefore fake.**
  `commentservice/DEEPSEEK_API_KEY`, `wechatservice/WECHAT_MP_APP{ID,SECRET}`,
  `notificationservice/BARK_DEVICE_KEY`, `emailservice/RESEND_API_KEY`,
  `oss/R2_{ACCOUNT_ID,ACCESS_KEY_ID,SECRET_ACCESS_KEY}`. A declared secret with
  no stored value fails the service at spawn (D16), so the choice was a
  placeholder or a dead service. Four of the five services start fine and will
  fail only when they call their provider; `oss` is the exception and dies at
  startup, which is a property of `oss`, not of the placeholder policy.
  *Rejected:* leaving them unset — five services down for a reason that says
  nothing about the platform. *Rejected:* real credentials — they are not the
  replica's to hold (PLAN-allin "what stays manual").

### D28 addendum. Health-gate fix for static mounts stuck at `declared` (2026-09-02)
- **Resolved the first D28 open item.** `PlatformPolicy._health_gate`
  (`src/ams/platform/policy.py`) now special-cases `stage == "declared"`: it
  reads `<state>/platform/mounts/<id>.json` and, when the sidecar parses as a
  dict with `kind == "static"`, discards the gate marker and skips straight to
  the next record -- the same treatment `stage == "healthy"` already gets.
  `declared` is a static mount's real terminal stage (`docs/platform-sidecars.md`,
  `sync._phase_declare`: "a static site has no process and no registry record,
  so `declared` is where its state machine ends"), so reaching it is success,
  not a sync stuck partway.
- **Fail-safe on a missing or malformed sidecar: falls back to gating.** The
  `json.loads`/read is wrapped in a bare `try/except (OSError,
  UnicodeDecodeError, ValueError)` returning `False` ("not static"), so an
  absent file, a non-dict, or a sidecar with no `kind` key all leave the
  ordinary grace-period/escalate path untouched -- a broken sidecar must never
  *hide* a real stuck sync, only fail to suppress one. Verified by
  `tests/test_platform_policy.py::test_health_gate_treats_a_broken_mount_sidecar_as_non_static`
  (absent, corrupt JSON, a JSON array, and a dict with no `kind` key -- all four
  still escalate).
- **Read the sidecar directly (`state.root / "platform" / "mounts" / f"{id}.json"`),
  not via `ams.platform.sync.mounts_dir`/`load_mounts`.** The path is a stable,
  documented contract (`docs/platform-sidecars.md`) and `policy.py` already
  reads `platform/state.json` the same way (`platform_state_path`) rather than
  importing a sync-module accessor; importing `sync` into `policy` would also be
  the first `platform.policy -> platform.sync` edge in a graph where `sync`
  currently imports nothing from `policy` (no cycle today, but no reason to
  create the coupling for one path join). *Rejected:* caching the sidecar read
  like `_state_document`'s mtime/size cache -- the live fleet has exactly two
  static mounts (D28: `files-web`, `llm-web`) and, once a static record sits at
  `declared` forever, this check runs once per `flush()` tick per static id
  (cheap: one `stat`+read of a small file), never escalates, and therefore never
  needs the `_gated` de-dup a healthy/failed record relies on. Revisit only if
  the fleet's static-mount count grows enough for that read to show up in a
  profile -- n=0 evidence it ever will.
- Tests added: a static mount at `declared` beyond grace does not escalate; a
  `kind: service` mount at `declared` beyond grace still escalates (matching the
  pre-existing `test_health_gate_falls_back_to_updated_at`, which covers the
  no-sidecar-at-all case); the four malformed-sidecar cases above.
  Full local suite: **999 passed** (was 993; +6 = 2 scenario tests + a
  4-way `pytest.mark.parametrize` on the malformed-sidecar test), 110 skipped,
  ruff clean.

### Open / weak signals (live fleet, 2026-09-03)
- **`resume` and `displayservice` cannot start, and it is a translation gap, not
  a resource one.** Both read their DB path from a **code** default of
  `/var/lib/<name>/`, which the manifest never sets, so `translate`'s
  `/var/lib/<n>/ → <root>/data/` rewrite (the pilot's finding #5) has no env value
  to rewrite and the mapped uid gets `PermissionError: '/var/lib/resume'`. The
  five tier-2 services whose manifest *does* set the path (`files`,
  `locationservice`, `mailbox`, `pages`, `turingtest`) are unaffected. The fix is
  either an upstream manifest line or a translator that injects `<SVC>_DB_PATH`
  when the manifest omits it — `translate.py` was out of this task's scope, and
  the second option is a guess about a variable name, so neither was taken. n=2
  services observed.
- **The registry burst number is n=1 at 9 services.** 379 MiB was measured once,
  for nine simultaneous registrations. Nothing has measured it at 21, and the
  relationship between service count and burst size is **unknown** — do not
  linearly extrapolate it into a cap for the full fleet, and do not delete the
  700M headroom on the strength of the 64 MB steady state.
- **`depends_on` closed the start-ordering gap during this task, and the fix is
  T4.5's, not this one's.** Before it, a harness restart started all Layer-1
  services alongside the registry; each SDK client registers inside its lifespan,
  so `Connection refused` → `Application startup failed` → 5 attempts → the whole
  tier-1 fleet parked in `failed`. Observed twice. After the harness picked up
  `depends_on = ["registry"]` the fleet restarts cleanly: three harness restarts
  (9, 17 and 19 dependents), Layer 0 first, registry healthy, then every
  dependent on **attempt 1**, no crash loop [verified, n=3]. 19 of 24
  declarations carry the line; the five without it are `registry`, `auth`,
  `caddy`, `hello`, `pyhello`, which is correct.
- **The registry burst is flat in fleet size, which the first measurement could
  not show.** Re-measured at the 700M cap across three bring-ups: 379.4 MiB at 9
  simultaneous registrations, 384.5 MiB at 20, 385.3 MiB at 20 again — **1.5%**
  for going from 9 to 20. So the cost is dominated by fixed start-up (FastAPI
  init + the MCP session manager), not by per-registration allocation, and 512M
  would have sufficed. 700M is kept anyway: the margin is free (the process sits
  at 64–78 MB) and the two OOM kills that motivated it cost the fleet twice.
  This supersedes D28's earlier "do not extrapolate" caveat, which was written
  when n=1 [verified, n=3].
- **The registry's health probe flaps under a full-fleet start.**
  `registry health=FAIL ... TimeoutError: timed out` twice during the 20-service
  restart, recovering in 35 s and 14 s. On one vCPU, 20 uvicorn imports plus 20
  registrations is enough contention that the registry cannot answer a probe in
  time. Harmless today — a failed probe does not restart anything — but it is the
  signal to watch before anything is made to act on a failed health check, and
  before `start_period_s` or the probe timeout is tightened. n=2 flaps, one
  restart.
- **A code deploy between two sync ticks can make every declaration unloadable.**
  Seen live: the translator started emitting `depends_on` while the *running*
  harness process still had the old `schema.py` imported, so its reload rejected
  all nine freshly written declarations (`unknown top-level keys ['depends_on']`,
  `errors=9`) and restarted nothing. The services kept running on their in-memory
  declarations, so nothing broke — but a harness restart in that window would
  have failed to start nine services. Nothing detects this today. n=1.

### D29. Pools: N manifests, one process, distinguished by tag (2026-09-03)

The fleet's memory is dominated by a fixed per-process cost, not by the
services. On racknerd, 16 Python uvicorn processes hold 44–74 MiB each while
`import fastapi` alone is 44 MiB, and 14 member packages imported into one
interpreter cost 59 MiB in total. The services are ~1 MiB each; the interpreter
is everything. **So the unit of deployment, not the unit of code, is what has
to change.**

A **pool** is one ams declaration, one root, one venv, one process, hosting N
`api` services as N `uvicorn.Server` instances on N ports in one asyncio event
loop (`src/ams/platform/assets/pool_runner.py`). Each member keeps its own
allocated port, its own registry identity and ACL, its own Caddy route, its own
`/health` on its own port, its own `data/<member>/` directory, its own
secrets, its own `ServiceRecord` and its own change detection. The **tag that
distinguishes members is the service id it already has** — `SVC_NAME`, the
registry id, the mount id. Nothing new was invented to tell members apart;
the only new key says which process a manifest runs in. Full contract:
`docs/platform-pools.md`. Full design and rejected alternatives:
`.claude/state/PLAN-pool.md`.

- **The grouping key is `pool = "<name>"` in `service.ams.toml`
  (`static.Overlay.pool`, `static.overlay_pool`), not in `service.yaml`.** The
  legacy deployer validates manifests against a schema with
  `"additionalProperties": false` at the root, so a new manifest key would
  hard-fail the deployer that still owns production. The overlay is already
  the ams-only channel, already read before `translate()`, already
  reject-rather-than-guess. The pool's ams id is `pool-<name>`
  (`translate.POOL_ID_PREFIX`) so it can never collide with a member id.
- **N ports, not one.** *Rejected:* one uvicorn with N `build_app()` results
  mounted under N path prefixes. A live probe (2026-09-03, n=1) showed that
  shape works — four apps under four Starlette `Mount()`s on one port, with the
  right `root_path`, `/docs` and `openapi.json` `servers` whether or not a
  member sets `root_path` itself. It was still rejected on what the probe did
  not remove: it inverts the gateway's prefix-stripping rule for every pooled
  member, forces `health_path` to carry the mount prefix, has no prefix at all
  for the five subdomain-mounted members, and leaves two websocket routes
  behind an untested `Mount` scope rewrite. **The decisive reason is
  reversibility.** This design's own safety valve — for a dependency conflict,
  a memory spike, or a service that must not be interrupted — is "that member
  leaves the pool". With one port per member, leaving is a pure deployment
  edit: the member's Caddy block is byte-identical in or out of the pool, and
  only the port number in `reverse_proxy` differs (verified — `gateway.py`'s
  `resolve_ports` is the *entire* gateway-side change; `_render_site`,
  `_port_for` and every existing Caddyfile golden are untouched). With one
  shared port, leaving is a routing migration every time. Keeping one port per
  member also leaves `registryclient.py` and every `registry/<id>.json`
  sidecar byte-identical — the registry never learns pooling exists. The
  one-port shape stays documented as the fallback if N `uvicorn.Server`
  instances in one event loop turn out not to work.
- **The runner is an ams asset placed into the pool root, not SDK code.**
  `src/ams/platform/assets/pool_runner.py` (no `__init__.py` in `assets/`),
  copied to `<root>/pool_runner.py` by `_phase_declare` and run by the pool's
  own venv python. ams only ever reads its bytes, so "nothing in `src/ams`
  imports fastapi/uvicorn" holds and is asserted by a portable test (imports
  every `ams.*` module, asserts both absent from `sys.modules`). *Rejected:* a
  `sdk.pool` module in the api repo — which manifests share a process is a
  deployment decision owned by the runtime, and putting it in a library Layer 0
  also depends on means every runner fix is an api commit plus a fleet
  redeploy, for a private repo with a second consumer (the legacy deployer)
  that needed zero changes of its own.
- **The env splits into ~12 identity keys, swapped per member per phase, and
  everything else, unioned into the process env.** Fifteen members' `SVC_NAME`s
  cannot coexist in one env; fifteen sequential reads can. `load_from_env()` is
  **not** one-shot at `build_app()` time as both fact reports had assumed: an
  AST survey of all 19 services plus the SDK (n=19, static, `envsurvey.py`)
  found `timeservice`, `llmpricing`, `resume`, `displayservice` and
  `test-service` calling it again **inside their lifespan**. So the swap wraps
  build, lifespan startup and lifespan shutdown (`pool_runner._identity_env`,
  `_build_member`, `_startup_member`, `_shutdown_member`), using the uvicorn
  split verified in the spike (n=3 runs, 6 members, every `/health` 200, RSS
  66 MiB, SIGTERM → all stopped in 0.63–0.73 s) — `config.load()`,
  `lifespan = config.lifespan_class(config)`, sequential `await startup()` per
  member under its identity env, concurrent `main_loop()`, sequential
  `shutdown()` back under the identity env. Every request-time env read outside
  the identity set uses a service-specific name (`MESSAGE_INGEST_TOKEN`,
  `LOCATION_*`, `DEEPSEEK_API_KEY`, …), which is what makes the union safe;
  `translate.build_pool`/`_pool_env` reject a pool whose members bind one
  non-identity key to different values, naming both members and the conflicting
  value. Two keys turned out to be mandatory rather than metadata:
  `SVC_AUDIENCE` (`KeyError` without it) and `REGISTRY_URL`/`AUTH_URL` (the SDK
  refreshes ACLs against the *default* registry URL when they are unset). The
  one identity key read at **request** time is `SVC_ENDPOINT`
  (`sdk/ui.py:266`, `mailbox/main.py:249`); it resolves to `""` in a pool, so
  the login return-to is derived from the request instead — the single
  api-side change in this design (T11, landed on the `api` branch
  `ams-platform` at `f88ebe60` per the fleet dry check below), and a
  correctness fix in its own right. Secrets are the exception to `pool.json`:
  they arrive through the process env as `<NAME>__<MANGLED-MEMBER>` from
  `<state>/secrets/<pool-id>/` (`translate.mangle_member`, checked for
  injectivity across the pool), because the spawn-time injection point is per
  declaration and there is exactly one declaration.
- **The runner catches `SystemExit` per member, not just `Exception`.** A
  uvicorn startup failure calls `sys.exit(3)` inside the task, and in one event
  loop that `SystemExit` — a `BaseException`, invisible to `except Exception` —
  propagates out of `asyncio.run` and takes down every member. Verified in the
  spike. `_build_member`/`_startup_member`/`_shutdown_member` each catch
  `(Exception, SystemExit)` and log one `ERROR [<member>] pool: <phase> failed`
  line; the process itself exits non-zero only when zero members started
  (`_serve`).
- **The pool venv pins `uvicorn[standard]==0.52.4`** (`translate.POOL_UVICORN_PIN`,
  emitted as `packages[0]` ahead of every member's editable install),
  overriding every member's own `>=0.27`. The startup split uses six uvicorn
  internals (`Config.load`, `Config.lifespan_class`, `Server.lifespan`,
  `startup`, `main_loop`, `shutdown`), none public API. The runner
  `hasattr`-checks all six (`_check_uvicorn_api`) before building anything and
  exits with one line naming the missing attribute and the version found, so an
  incompatible bump is a clean `failed` record at second zero rather than a
  half-started pool. Any uvicorn bump revisits this file deliberately.
- **One pool is one trust domain, stated rather than engineered around.**
  Members share an address space; any member can read another's environment
  and `app.state`. Isolation between members is not attempted. The runner's
  environment restore is hygiene, not a boundary. The pool boundary is drawn on
  blast radius: `displayservice`, `llmgateway`, `files` and `oss` stay
  standalone because their memory is bounded by workload rather than by code.
- **A member that fails to build or start is skipped; the pool serves the
  rest.** *Rejected:* failing the whole pool, which converts one bad commit
  into a 12-service outage — strictly worse than today. Two distinct failure
  points, and they are handled differently on purpose: a member whose
  **manifest itself does not translate** (a `TranslateError`) fails the whole
  pool at build time — `translate._check_pool_members`/`build_pool` raise, the
  sync loop's `_build_pool_group` catches it as a `PoolError`, and every member
  of that pool is marked `failed` with one shared message (`_pool_blocked`),
  not N separate escalations, because one broken manifest is one cause. A
  member whose **process** fails to build or start inside a pool that *did*
  translate is the case §4.5 covers: its port never binds, its own health
  probe fails, its own `ServiceRecord` goes `failed`, and the rest of the pool
  keeps running. `/_pool/health` is 200 while **any** member serves, because it
  drives the supervisor's restart policy and the D27 dependency gate.
- **The pool is the rollback unit; a member id is refused** (`rollback.py`,
  checked against `record.get("pool")`). Message names the pool and states the
  reason: "its members share one process, one tree and one venv". Rolling back
  a pool id re-derives it from its members' manifests **as they were at the
  target sha** (`_find_pool_translation`: per-member `translate()` with
  `TranslateContext.pool` set, then one `build_pool` over the union) — there is
  no manifest for a pool itself. All or nothing: a member that does not
  translate at that sha means the commit is not one the pool can reach. The
  health gate afterwards runs **per member** (`_step_pool_health`): the pool's
  own admin probe answering proves the runner is up, not that every member's
  app was built and mounted. D26's change detection survives at pool
  granularity — a commit to any member restarts all of them; that is the
  accepted price.
- **Provisioning is one `uv pip install -e <member>…` into one venv at
  `<root>/.venv`** (`runtime=RuntimeSpec(kind="uv", sync=False,
  packages=[pin, "-e", rel, …])`), not `uv sync --frozen`. Twelve
  `cp --reflink` and twelve `uv sync` become one of each
  (`sync._phase_materialize`: `if item.pool and not item.is_pool: … nothing to
  stage or provision`).
- **Adoption is an explicit operator command, never a sync side effect.**
  `ams platform pool adopt <pool>` (`src/ams/platform/pool.py`) stops each
  member over the control socket, moves its `data/` under the pool root and
  its secrets under `<NAME>__<MANGLED>`, then unlinks its stale
  `service.toml`. Sync's `_declare_pool` refuses to declare a pool whose member
  still has a non-empty legacy `data/` and escalates once with the exact
  command. **The move is a two-hop staged move, not a direct `mv`**: a member's
  `data/` is owned by the member's uid block and the pool's by the pool's, and
  `run_admin` maps exactly one block into the namespace it forks, so no single
  admin fork holds `CAP_DAC_OVERRIDE` over both directories at once. The move
  is therefore `<member-root>/data/*` → a harness-owned staging directory
  (`<state>/platform/adopt/<member>/`, inner 0 in *both* namespaces) →
  `<pool-root>/data/<member>/`, two renames on one filesystem, so a
  multi-gigabyte database moves in constant time. *Rejected:* teaching
  `run_admin` to map two blocks at once — it widens the privilege of every
  admin fork in the harness to fix one operation that runs once per pool in the
  lifetime of the fleet. A run that dies between hops is recoverable: the next
  run drains the staging directory before touching the member's own `data/`.
- **`ams platform status`'s rendering has no header row and no `AGE`
  column** (`cli.format_status`) — a correction to the plan's mock, which
  sketched both. Pools sort first with members indented underneath
  (`_ordered`); a `POOL` column is added only when at least one record carries
  a pool key, so an unpooled fleet's output is byte-identical to before pools
  existed; every row still prints `since=<stage_since>`, the pre-existing
  per-row format, rather than a separate age field.

**Assumptions.** (1) The member projects resolve into one venv — read from 21
`pyproject.toml` files with zero conflicting specifiers, installed together
once in a scratch venv, and six of them then built, served and stopped from it
(n=3). The escape hatch when this breaks is deleting one overlay line. (2) No
member's memory is unbounded — mitigated by the exclusion list, not by a
mechanism. (3) Members tolerate a shared process: no `signal.signal`, no
`sys.exit`, no module-level mutable SDK state, `ContextVar` rather than
globals, per-instance heartbeat threads, distinct package names, distinct DB
paths — all read exhaustively. `auth` is the known counter-example (a
module-level session secret minted at import) and stays out of every pool.

**Facts** (verified, this task)
- `load_from_env()` is not one-shot at `build_app()` time: an AST survey of all
  19 `api` services plus the SDK (n=19, static, `envsurvey.py`) found
  `timeservice`, `llmpricing`, `resume`, `displayservice` and `test-service`
  calling it again inside their lifespan. This is why the identity-env swap
  wraps three phases, not one.
- Every request-time env read outside the ~12 identity keys uses a
  service-specific name, not a shared one (same n=19 static survey) — the
  finding that makes the non-identity union safe, with `SVC_ENDPOINT` the one
  exception (T11 fixed it).
- A uvicorn startup failure calls `sys.exit(3)` inside the task, and in one
  event loop that `SystemExit` propagates out of `asyncio.run` and kills every
  member unless caught explicitly — verified live in the spike (n=3 runs, 6
  members: `kvservice`, `timeservice`, `llmpricing`, `logservice`,
  `messageservice`, `commentservice`), which also measured RSS 66 MiB, 3–4
  threads, and SIGTERM → all stopped in 0.63–0.73 s under `SVC_DEV=1`.
- The fleet dry check (`build_pool("core")` over all 15 real manifests on api
  branch `ams-platform` @ `c8b1fff3`, T11 fix at `f88ebe60`, 2026-09-03, n=1
  local run — not yet on racknerd): 15 members, 16 ports, 20 mangled secrets,
  `limits = 600M / 100% / pids 288`, 34 unioned non-identity env keys with zero
  conflicts, `resume` correctly detected as a module-level app
  (`factory=false`), and `schema.loads(emit_toml(decl)) == decl` held.
- Local suite: **1193 passed / 122 skipped** (`.venv/bin/python -m pytest -q`,
  2026-09-03) with T1–T8 landed; up from 993/110 at the end of wave 4.

**Open / weak signals**
- **Memory saving is a projection, not a result.** No measurement has
  exercised request traffic, database connections or per-app caches, and every
  measurement ran with `SVC_DEV=1` (registry client and its per-member threads
  disabled). What exists: 16 standalone processes at 44–74 MiB (n=1 each);
  `import fastapi` 44 MiB; 14 apps imported into one interpreter 59 MiB,
  marginal ≈1 MiB/app (n=1); T0's 6 apps serving in one process at 66 MiB
  (n=3). The projected saving is ≈500 MiB for a 12-member pool. **Do not
  delete the standalone path or widen the pool roster on the strength of this**
  until §7.3 of `docs/platform-pools.md` is filled in with n=3 idle samples on
  the real fleet (T10, not yet run).
- **`pids_max = 48 + 16 × N` and `memory_max = 150M + 30M × N` are still
  unmeasured formulas.** T0's 3–4 threads at N=6 ran with `SVC_DEV=1`, which
  suppresses exactly the per-member heartbeat, M2M-refresh and CLS-forwarder
  threads the pids formula is sized for — that number must not be reused. T10
  re-derives both against a fleet with `SVC_DEV` unset.
- **The one-loop blocking-I/O hazard is a structural risk with a textual
  audit, not a live measurement.** A static heuristic (n=19, `asyncaudit.py`)
  grepped pooled members for blocking calls inside `async def` handlers and
  found none among the recommended roster (sync `def` handlers run on
  Starlette's shared anyio threadpool and do not block the loop); what
  couples is one 40-thread pool shared by 15 members instead of 15 separate
  pools. Decision: keep the one-loop runner; the documented fallback if T10
  shows cross-member latency coupling is one thread + one loop per member
  (same memory, sequential startup still needed for the env swap). A textual
  grep is not a load test — treat "no blocking calls found" as a lead, not a
  clearance.
- **The pool venv has no lockfile.** Provisioning resolves at declare time
  with no `uv.lock` equivalent for the pool as a whole (each member still has
  its own, unused by the pool's install). A `uv pip compile` lockfile for the
  pool is a Phase-B option that changes nothing about the shape described
  here.
- **`SVC_ENDPOINT`-style request-time reads of an identity key were found by
  reading the SDK and one member (`mailbox`), not by an exhaustive audit of
  all 19 services' request handlers.** T11 fixed both known call sites; a
  third has not been ruled out. `CLAUDE.md`'s new rule ("a request-time read of
  `SVC_*` is a bug") is the standing instruction for anyone who finds a third.
- **User-decidable defaults were taken in the user's absence** (PLAN-pool §10
  risk 1–3), each with its default stated and reversible: the 15-member `core`
  roster with `displayservice`/`llmgateway`/`files`/`oss` standalone (default:
  accept the 15-member roster); the accepted losses in `docs/platform-pools.md`
  § "What the user loses" (default: accept — the request explicitly asked for
  merged processes); backup keys staying per logical service via `Target.label`
  rather than becoming `pool-core/<member>/…` (default: keep them per-service,
  since changing it after T7 orphans 14 days of R2 objects). None of the three
  has been confirmed by the user; flag before the racknerd cutover (T10).
- **The migration is not one-command reversible.** Un-pooling means restoring
  each member's `data/` from a backup, dropping the overlay lines and syncing.
  `ams platform pool adopt` has no inverse (`pool evict` is not in scope).

#### D29 addendum — live result and two post-cutover fixes (2026-09-03)

**Facts (measured, racknerd, n=3 per side, `evidence/ams-measure.sh`, fleet idle):**
Layer-1 cgroup sum 712 → 275 MiB; all services 866 → 464 MiB; Python
processes 18 → 5; `pool-core` `memory.current` 133–134 MiB with 15 members
(27 threads of a 288 `pids_max`); cold start to all members healthy 21 s
(standalone fleet: 120–150 s); 13 members healthy, `resume` and
`secretsservice` failed exactly as before pooling; M2M across the pool
boundary verified; no route regressed. The memory projection in the Open
section above is therefore confirmed; the sizing formulas are 4–10× generous
and are left as ceilings.

**Two adoption bugs found only live, both fixed with red-then-green tests on
the box:** (1) `Path.is_dir()` on the pool-owned 0750 `data/` raised
`PermissionError` (harness cannot stat into a member-uid dir) — every `Path`
probe in `pool.py` now falls back to an admin-ns `find`, and "absent" is
distinguished from "unlistable"; (2) the move acted on a pre-stop listing and
WAL-mode SQLite drops `-wal`/`-shm` on clean stop, and a multi-argument `mv`
is not atomic — each hop is now one `rename(2)` of the whole `data/` directory
after the stop, with a resumable per-entry merge only when the target is not
empty. *Rejected:* per-file moves with a post-stop re-list (still not atomic);
stopping the whole fleet first (hides the defect behind procedure).

**Two post-cutover fixes:** the `level-prefix` parser accepts `LEVEL [tag]
name:` (uvicorn banners were escalated as ERROR on each pool start); a service
that failed its health gate at an unchanged sha is held for 900 s instead of
re-gated every tick (a no-op tick took ~3 min with two dead members).

**Open:** the 900 s hold and the sizing formulas are starting values (n=1
live observation each); latency coupling under load is unmeasured (one idle
run: pool members 5.4 ms vs standalone control 5.8 ms via Caddy, n=20).

### D30. Mock deploy on a fresh 1 GB DigitalOcean droplet: three fresh-host fixes (2026-09-03)

**Context.** The user asked for a mock deploy of the api platform "to phm with
a new DO machine", 1 GB RAM. Everything Phase A knew was learned on racknerd,
a box hand-configured over two days; the point of the rehearsal is to find
what only the scripts know. Full record: `.claude/state/mock-deploy-do.md`.

**Decisions.**

1. **`layer0.py --no-layer1` rather than making `stop-layer1` tolerate an
   unknown service.** Rejected alternative: treat `ctl stop <unknown>` as
   already-stopped and carry on. That would have let the bring-up declare a
   *standalone* kvservice and timeservice on a fresh host, which since D29
   are members of `pool-core`; the first sync tick would then hit the adoption
   guard (or worse, run two copies). The pilot re-pointing was Phase A's proof
   step and has no meaning on a host that never ran the pilot. Layer 0 alone
   is the fresh-host shape; the sync timer owns the fleet.
2. **`_phase_finish` registers every identity before it opens any health
   gate (two passes), rather than moving `registered` ahead of `reloaded` in
   the stage order.** Observed: the pool item precedes its members in `work`,
   so the pool's 90 s `/_pool/health` gate ran while every member's startup
   404ed on `POST /api/services/register`; the pool was recorded `failed`
   (and, with the D29-addendum hold, would not have been re-gated for 900 s)
   although its next restart succeeded once the identities existed.
   Rejected alternative: register before reload. Correct in principle (it is
   the CLAUDE.md rule) but it re-orders the monotonic stage list every record
   and golden test pins, for a gain of one crash + one 20 s backoff per
   service on a fresh host only. Recorded as an open item, not done.
3. **`install-host.sh` gains `libatomic1` and an `apt-get update`.** The
   standalone pnpm binary needs `libatomic.so.1`; racknerd had it by accident
   (a dependency of something else), a minimal 24.04 cloud image does not, and
   `set -e` then silently skipped Caddy, the state dir and the unit.
4. **A 1 GiB swapfile on the 1 GB box, and swap *use* is reported as a
   number, not hidden.** Without it, `uv sync` of the 15-member pool venv on
   961 MiB would be one OOM kill away from a failed provision with no
   diagnostic. DO images ship no swap; racknerd has a swap partition.

**Assumptions / would break if.** The two-pass register assumes creating an
identity never depends on the service being up (true: it is a registry
write with a stored secret). `--no-layer1` assumes the sync unit's `--only`
list names at least one member of every pool wanted (it does: "8 of 15
members named; a pool is one process, so all of it is selected").

### D31. Core mode: host the Cordis-based api core as one ams service (2026-09-29)

**Context.** api `main` (v3.1.0, 2026-09-26) no longer has `service.yaml`
manifests. It is one Node 24 process, `core` (`cordis@4`), that hot-installs
~28 TypeScript plugins through a unix control socket and owns per-plugin
safety itself: isolated apply, health check, atomic facade swap, drain,
60 s probation, auto-revert. Registry, auth, gateway, store, secrets and
health are plugins inside it. Production runs on phm under systemd
(`core.service`, `core-ship`, `core-release`, `core-daily-backup`),
which is out of scope. Plan and interfaces: `.claude/state/PLAN-core.md`.
End-to-end story: `docs/platform-core.md`.

**Decision.** A second platform mode, **core mode** (ams 1.1.0):
`core` is one ordinary ams service, and a one-shot tick on a 60 s timer
(`ams platform core sync`) does what phm's units do. It stages and releases
core when `core_paths` change, ships plugins whose *content* changed through
core's own client, fronts core with Caddy, backs it up, and escalates. ams
does **not** re-implement anything core owns: probation, plugin rollback,
dependency order, artifact validation. The legacy manifest mode stays,
deprecated and untouched.

1. **The control plane goes through upstream `scripts/corectl.mjs`, run as
   the service.** The socket is 0660 and owned by the service uid, so the
   harness cannot connect in isolated mode. `run_as_service` gives a one-shot
   process the service's own identity. corectl already does the CAS dance
   (`expectedGeneration` from a fresh `status`) and the base64 upload.
   A transition with `outcome != ok` is a result; only a failure to get one
   raises.
2. **Change detection is a content key**: upstream `computeArtifactId` minus
   buildInfo, computed by `assets/core_plan.mjs` with the tree's own
   `build-artifact.mjs`. `buildInfo.commit` changes on every commit, so the
   artifactId cannot tell ams whether a plugin changed. A `git archive` tree
   has no `.git`, so `CORE_SOURCE_COMMIT` carries the sha. The planner is
   package data, copied into `R/build/` because the service cannot read the
   ams checkout (same reason as the pool runner).
3. **A failed content key is never retried** under unchanged conditions (no
   retry storm), and a new commit with new content retries. D32 refines
   "conditions". Likewise, a sha whose release failed is held
   (`release_failed_sha`); a failed *first* release is not held, because
   nothing runs yet and a fresh host with an environment problem should heal.
4. **Release = stop → flip `current` → start → gate → flip back on failure.**
   Core imports from the tree `current` resolves to, so it is stopped first.
   The gate is **"no regression"**, not "HTTP 200": `/health` belongs to the
   `health` plugin behind the `gateway` plugin, so a fresh core cannot pass an
   HTTP gate. `/health` must be 200 again if it was before; otherwise every
   plugin serving before must serve again (D32); otherwise the gateway port
   must answer; otherwise the control socket answering is enough. The gate
   fails at once when the harness reports core `failed` (added after the
   local e2e: a crashing release was down ≈92 s, now ≈17 s). No plugins
   ship in the tick of a failed release.
5. **Privileges are granted right after a new plugin's deploy, then the
   plugin is restarted** (`CoreControl.restart`), so the generation judged
   by probation holds them. Seen in the first local run: `health` without
   `ops.read` answers every probe 503, fails probation, and has no revert
   target. A steady-state pass (`apply_privileges`) keeps them in line with
   `core.toml` afterwards.
6. **Fixed ports, like Layer 0 (D22)**: gateway 18080 (+ extras). The number
   is written inside `plugins.json` and every Caddy site names it; `import`
   checks that `plugins.gateway.config.port == core.gateway_port`. The
   Caddy front's listen port is the new config key `caddy_port` (default
   20180). The gate's patience is the new `health_timeout_s` (default 90).
   Neither key is in the plan's example.
7. **The config bundle is a write-only secret** (D16): a harness master copy
   under `<state>/secrets/core/bundle/` (0700/0600), names only on output,
   placed into `R/etc` on every tick whose digest differs from
   `bundle.placed` (the harness cannot read the 0700 service-owned `etc/`
   back). A changed bundle outside a release logs a warning to restart
   the affected plugins instead of restarting them: a restart is a
   probation, and which plugin reads which section is core's business.
8. **Plumbing choices.** `core.lock` is taken by the process (non-blocking
   flock) instead of `flock(1)` in the unit, which would deadlock against
   it and would not cover a hand-run `ship`/`release --rollback`.
   `Conflicts=` keeps legacy sync off the same state dir. `releases/` is
   0755 (the harness reads `.ams-sha` and `package.json` there, and the
   spawner walks `current` before dropping to the service). The harness
   shutdown budget is 45 s with `TimeoutStopSec=50` (core drains 40 s,
   upstream `TimeoutStopSec=40`). `--no-isolation` loads the runtime layer so
   a managed-Node service finds `node`. Site hosts are validated by
   `gateway.CoreSite` (strict lowercase RFC 1123; the host is also a file
   name and an `import` argument). The managed pnpm is installed with
   `ignore-scripts`. Node archives are `.tar.gz` on every platform (zlib is
   always there; `lzma` is optional in a source-built CPython).
   `provision_tree` refuses a tree outside a service root.
9. **Backup** adds immutable byte stores (`blobs`, `oss-bytes`,
   `pages-content`), copied with `rclone copy --immutable` to a separate,
   never-pruned `bytes/<id>/<path under data>` prefix after the sqlite
   snapshots, with new `ByteStoreSynced`/`ByteStoreFailed` records. The first
   cut left `R/data/artifacts` out as "rebuildable". The review showed that
   a restored `core.sqlite` without it boots every plugin
   `artifact_unreadable`, so it is now backed up too (core only, top level;
   part of the D32 review fixes), and the tick reinstalls unreadable plugins.
10. **Untrusted build code.** The first cut ran `pnpm install` and the build
    as inner root under `provisioning_mask` (the merge of issue #1). D32
    replaced that with `run_as_service`.

**Rejected alternatives.**

- *Re-implement core's control protocol (JSON lines over the socket) in
  Python.* It would avoid a Node process per call and let the harness talk
  to the socket directly. Rejected: a second implementation of the CAS
  dance and the upload framing drifts from the one production uses, and
  upstream changes it freely. The cost is a Node process per call (not
  measured separately; a whole no-change tick, one `corectl status`
  included, took 0.17 s locally) and `corectl deploy` hashing the whole
  artifact store (mitigated with `gc`).
- *Use artifactId for change detection.* It changes on every commit
  (buildInfo), so every tick would ship every plugin, each into a 60 s
  probation.
- *Treat each plugin as an ams service, or translate plugins into
  declarations.* That fights core's model: plugins are generations inside
  one process, and core already gates, swaps and reverts them.
- *Delete legacy mode in 1.1.0.* api v2.0.0 still needs it, racknerd may
  be rebuilt from it, and deleting it is irreversible for anyone still on
  v2.0.0. Deprecate now, decide in 2.0.0.
- *Run installs and builds as the harness, or as inner root without a
  mask.* The build runs repository code and dependency lifecycle scripts:
  untrusted code with the harness uid's access to the SecretStore, the
  platform RS256 key and the ams checkout. See D32 for why even the masked
  form was replaced.
- *Back up by stopping the writer, like phm's `core-daily-backup`.* That is
  a daily outage of every plugin at once. sqlite's online backup API gives a
  consistent snapshot while core runs, and the byte stores are immutable,
  content-named files.
- *A webhook instead of the 60 s timer.* Same reasoning as the legacy sync
  (PLAN-allin Q1): an inbound port, a route and an HMAC secret for the same
  latency.
- *Caddy doing routing and auth per plugin*, as in the legacy per-mount
  sites. Core's `gateway` plugin already routes, authenticates and sets
  headers. Caddy shrinks to a host → port map.

**Assumptions / would break if.** Upstream keeps `scripts/corectl.mjs`'s
CLI and its `corectl: <code>:` stderr shape, `build-artifact.mjs`'s
`buildArtifact`/`canonical` exports and the `computeArtifactId` part order
(a drift makes every content key change once, which is safe but noisy).
Core's status shape (`desired/observed/live`, `phase`, `reason`) is as
described in `corectl.py`'s docstring. The socket path stays under the
`sun_path` limit.

### Open / weak signals (core mode, 2026-09-30)

- **The Linux isolation path has never run live** (n=0): `run_as_service`
  provisioning with a per-service pnpm store, as-service `mkdir` in
  `ensure_layout`, planner placement as the service, and the staged
  `.etc.stage` / `.current.new` renames. The evidence is portable tests of
  the argv/paths and the plain-mode e2e (n=1, macOS). Next test:
  re-provision racknerd, run `scripts/remote-test.sh` (including
  `tests/linux/test_run_as_service_live.py` and
  `test_run_admin_mask_live.py`), then a live core e2e. This is a user
  decision.
- **Two escalation streams for one failure.** Core-sync's `CoreSync` line,
  plus the harness escalating core's own `level:"warn"` lines
  (`plugin.failure`, `core.transition … failed`) and "giving up on core".
  Candidate: a platform-policy rule that suppresses core's transition and
  failure lines in core mode. Not done: it hides core's own view if
  core-sync is down.
- **Core probation can pass a plugin whose health always fails** under
  enough successful traffic (12 of 71 = 17% < 20%, n=1). Upstream
  behaviour (`manager.ts` `startProbation`); ams records core's verdict.
- **Re-shipping reverted content** deploys a new artifactId with a fresh
  probation even when core already serves identical bytes. Harmless; it
  matches "new commit with new content retries".
- **`SourceMirror.stage` and the release gc** still run as inner root
  inside the service root. `ensure_layout`'s symlink refusal covers the
  non-racing case; a service that wins a race could redirect them.
  Candidate: stage outside the root and rename in, or run them as the
  service.
- **First isolated install downloads everything**: the per-service pnpm
  store (D32) costs one full network fetch per host. Not measured.
- **Upstream docs say core logs to stdout; it logs JSON lines to stderr**
  (observed in the e2e). Harmless: `[logging] format = "json"` classifies
  either stream.
- **Legacy mode: the issue #1 mask is bypassable** (review finding
  security-4, 2026-09-29). Legacy provisioning and static builds still run
  untrusted code as inner root under the *admin* map; the overmounts are not
  `MNT_LOCKED`, so the code can `umount` them (it holds CAP_SYS_ADMIN over
  its own mount ns), and with no pid ns `/proc/<harness pid>/root` reaches
  the real tree. Core mode is not affected (D32.3: untrusted steps run as
  the service). Fix = the same redesign for legacy (run as the service), or
  remove legacy mode in 2.0.0. Unverified on Linux either way.
- **Legacy Layer 0: registry/auth port hijack** (was GitHub issue #3,
  medium; the issue was deleted 2026-09-29 before the repo went public, so
  this is the only record). While registry (20100) or auth (20101) is down
  -- restart, crash, deploy -- any compromised ordinary service can bind
  127.0.0.1:20100/20101 and harvest the `X-Admin-Token` /
  `X-Service-Secret` headers other services send, escalating to platform
  admin. Core mode is not affected (registry/auth are plugins inside core;
  no fixed loopback ports between processes). Not fixed; candidates were
  per-service network namespaces, `SO_PEERCRED`-checked unix sockets, or
  holding the ports in the harness and passing fds. Retires with legacy mode.
- **No state migration from phm.** Bootstrap starts from a fresh
  `core.sqlite` and ships the roster; moving an existing core's state is an
  un-rehearsed manual step.

### D32. Core-mode review fixes: a verdict holds under its conditions; untrusted steps run as the service (2026-09-29)

(D31 above is the core-mode decision itself.)

1. **A content key that did not go live is a verdict *under the conditions it
   was reached in*.** Each plugin record carries the core `release` and the
   config-`bundle` digest; a rejection whose reason is about core's *state*
   (`dependency_unavailable`, `dependency_failed`, `dependency_major_mismatch`,
   `generation_conflict`, `service_in_use`, `major_in_use`, `unknown_plugin`)
   is recorded `blocked` with a fingerprint of what core runs (every plugin's
   live artifact + observed phase). A changed release, bundle or (for
   `blocked`) fingerprint makes the same bytes a new experiment, tried once;
   nothing else retries. Core refusing the bytes at upload
   (`invalid_manifest`, `invalid_artifact`, `artifact_too_large`,
   `manifest_mismatch`, `plugin_mismatch`, read from corectl's
   `corectl: <code>:` stderr) is a recorded `failed`. Only transport failures
   stay unrecorded, in `ship_retry`, and the next tick re-plans *only* those
   plugins; `planned_sha` advances whenever the plan itself ran.
   Rejected: never retry any failure (the original rule) -- it pinned good
   plugins forever behind a missing provider or an old core; retry every tick
   -- the storm the rule exists to prevent; key blocked retries on the
   provider's artifact only -- ams does not parse manifests' dependencies, and
   the whole-core fingerprint is bounded (one retry per core change).
2. **Held shas.** A stage that failed (`stage_failed_sha`) or a core release
   that failed (`release_failed_sha == head`) holds the sha: no re-install,
   and no plugin built from that tree ships onto the older core. The rest of
   the tick (probation verdicts, drift, privileges, gateway) still runs.
3. **Untrusted code never runs under the admin map in core mode.**
   `provision_tree` runs `pnpm install` and the build with `run_as_service`
   (no harness uid mapped, so neither harness DAC access nor a mount mask to
   `umount`), with a service-owned pnpm store/cache/HOME in `<root>/.cache`.
   Every other write into the service root is either done as the service
   (`ensure_layout`, the planner asset) or prepared in the harness-owned
   `<state>/services/core/` and renamed in (`place_bundle`, `flip_current`).
   Rejected: lock the mask (nested userns + CLONE_NEWPID + fresh /proc + cap
   drop) -- still leaves harness-uid write access, and cannot be verified
   without a Linux host; keep the shared harness pnpm store -- the service
   cannot write it, and write access to it is exactly the escalation.
   Cost: the first core install fills a per-service store (network once).

**Open.** Legacy provisioning (uv/pnpm/bun installs) and static builds still
run as inner root under the admin map with the (now wider, security-5) mask;
issue-#1-style bypasses via `umount` or `/proc/<harness>/root` remain there.
Fixing them needs per-service caches for uv/python/bun -- a legacy-mode
redesign, deferred with the rest of legacy mode to 2.0.0.
