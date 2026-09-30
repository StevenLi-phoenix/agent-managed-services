# PROGRESS (append-only)

## 2026-09-02 — session 1: host prerequisites + scaffold

Target host: `racknerd` (Ubuntu 24.04, kernel 6.8, systemd 255, cgroup v2, 1 vCPU / 2 GB).

Done:
- Host prep on racknerd (as root): `apt install uidmap python3.12-venv rsync`;
  `useradd -m harness` (uid 1000, subuid/subgid `100000:65536` auto-assigned);
  `loginctl enable-linger harness`; harness venv `/home/harness/venv` created with
  `python3 -m venv --copies`; uv at `/home/harness/.local/bin/uv`; pytest installed
  into the venv; AppArmor profile `/etc/apparmor.d/ams-harness` loaded.
- Verified end-to-end (n=1 host, scripted probe): userns + newuidmap/newgidmap
  block map, inner uid 1000 -> host 100000, CapEff=0, no_new_privs, harness 0600
  file unreadable from inside, admin map (harness->inner root) can chown/rm
  service-owned files; cgroup delegation via `systemd-run -p Delegate=yes`
  gives cpu/memory/pids/io/cpuset, `cgroup.kill` kills the whole tree, rmdir ok.
- Local scaffold: pyproject (stdlib only, py>=3.12), `src/ams/{schema,events,decision,spawn}.py`,
  tests for those, `scripts/remote-test.sh`, `deploy/ams-harness.service`,
  `deploy/apparmor/ams-harness`, `docs/service-declaration.md`.

Next:
- uidmap allocator + port allocator (pure logic).
- Linux isolated spawner: cgroup.py + userns handshake + IsolatedSpawner.
- supervisor loop + lifecycle + cli `ams run`.
- runtime provisioning (venv/uv/nix) inside service identity; hostcheck; systemd install script; e2e test.

## 2026-09-02 — session 1 (cont.): reflink store, node runtimes, parallel implementation

Done:
- User confirmed design inputs from the design chat (see DECISIONS D7-D10 and
  memory): agent inside harness, Linux+systemd only, reflink venv store, no
  CPython patching, seccomp-notify deferred.
- racknerd: XFS reflink loop store mounted at `/home/harness/store` (fstab),
  `/home/harness` chmod 0711 (services must traverse into store/state),
  `unzip`, pnpm 11 (`store/pnpm-home/bin`), bun 1.4 (`~/.bun/bin`), uv-managed
  python 3.12 in `store/python`. `deploy/install-host.sh` reproduces all of it.
- Verified probes (n=1 host): uv / pnpm / bun provisioning inside the admin ns
  as inner root; second env +0..1 MB via reflink; chown to service; service
  runs as inner 1000. Probe artifacts removed.
- Schema: runtime kinds `pnpm`/`bun`, `runtime.node`; docs updated.
- Dispatched three implementation agents in parallel: `alloc` (uidmap/ports/state),
  `iso` (cgroup/userns/isolated/hostcheck, remote subdir ams-iso), `sup`
  (supervisor/health/cli, remote subdir ams-sup).

Next:
- When `iso` lands: brief the runtime provisioning agent (`src/ams/runtime.py`)
  using `userns.run_admin` — venv/uv/pnpm/bun via the store env vars, nix as
  NotImplemented; integration test end-to-end on racknerd via `ams run`.
- Integrate cli `run` with isolated spawner + runtime layer; install unit; smoke
  test under systemd.

## 2026-09-02 — session 1 (cont.): CLI integration + live systemd e2e

Done:
- `cli.py` rewritten around one reusable assembly, `build_supervisor(state,
  isolation=, escalation=, provision=) -> Assembly`, so `ams run` and
  `tests/linux/test_e2e.py` execute the same wiring instead of two lookalikes.
  It does subreaper, `StateDir.ensure`, `UidAllocator.from_host`, `PortAllocator`,
  `make_isolated_spawner`, `ensure_service_root` per service, and lazy
  `ams.runtime` (absent or unusable -> warn and run `runtime.kind="none"` only).
  Isolation never falls back silently: `Unavailable` -> exit 2 with the fix.
- New: `ams provision [<id>...]` and `ams run --provision` wired to
  `runtime.provision`; `Supervisor(extra_env_for=make_extra_env_for(state, store))`.
- `ams run` logs a one-line-per-service summary at start and every 60 s
  (`PeriodicSummary`: status/pid/uptime/healthy/failures/ports/memory.current),
  via a new `run_forever(on_iteration=, shutdown_timeout_s=)` hook — the only
  supervisor change, both kwargs default to the previous behaviour.
- D12 applied where it belongs: `IsolatedSpawner.spawn` writes
  `memory.swap.max=0` when the declaration sets `memory_max`. The "memory.max
  alone is not a ceiling" fact moved from test_isolated to test_cgroup (it can
  no longer be observed through the spawner, which is the point).
- `tests/linux/test_e2e.py` (11 tests) + `scripts/deploy-racknerd.sh`.
  Local 226 passed / 43 skipped, ruff clean; `remote-test.sh ams-integ
  tests/linux` 43 passed (includes the iso/alloc/runtime agents' tests).
- Live on racknerd: `ams-harness.service` **enabled and active** with a `hello`
  http.server on port 20000, uid 100000, memory.max=64M + swap.max=0.
  Full transcript in `.claude/state/e2e-systemd.md`.

Two bugs the live run caught (neither reachable from a unit test):
- `cmd_check_host` called `hostcheck.main()` with no argv; it falls back to
  `sys.argv[1:]` = `["check-host"]` and read that as the username to look up in
  /etc/subuid. Now passes `[]`.
- A service could not resolve its own workdir by path. The inherited cwd needs
  no traversal rights, but `python -m http.server` calls `os.getcwd()` and stats
  it, and `StateDir.ensure()` makes `services/` 0750 harness-owned. Every
  request 404'd. `cli._ensure_traversable` now adds `o+x` (never `o+r`) to the
  harness-owned ancestors of a service root.

Open / weak signal (n=1, from the fix above): with `services/` at `o+x`, a
service can traverse into a *sibling's* root if it guesses the id, and service
roots are 0755, so the sibling's files are readable. Not a listing leak (`o+r`
is never set) and every service belongs to the same user, so it is low severity.
Closing it properly means group-owning `services/<id>` by the service's mapped
gid at mode 0710 via `run_admin` — a change to the state layout / userns layer,
not to the CLI, so it is left to the owner of those modules. Do not "fix" it by
dropping the `o+x`: that silently breaks every service that resolves its cwd.

Next:
- Decide on the sibling-traversal item above.
- `ams status` / a control socket, if the agent wants more than the 60 s summary.

## 2026-09-02 — session 1 (cont.): conductor verification
- Independent verification (not agent reports): local `pytest -q` 226 passed /
  43 skipped, ruff clean; remote full suite from a clean rsync (`ams-verify`)
  269 passed in 35 s; live unit `ams-harness` active+enabled, `hello` on port
  20000 → HTTP 200, host uid 100000, CapEff 0, NoNewPrivs 1, cgroup
  `svc-hello` memory.max=64M memory.swap.max=0 pids.max=16, no stray cgroups.
- Bug found live: `ams provision` demanded a delegated cgroup (it only needs
  userns). Sent back to integ with the `pyhello` (uv + six) declaration at
  `/home/harness/store/state/services/pyhello/service.toml` for a full
  provision → restart → curl proof.
- Agent scratch dirs `/home/harness/ams-*` and `/home/harness/state/ams-*` removed.

## 2026-09-02 — session 1 (cont.): review-1 fixes + live provisioning

Folded in four review-1 items (details + live transcript in e2e-systemd.md):
- `isolated.py` MEDIUM double close on the spawn-failure path: `except`/`finally`
  closed the write ends twice with cgroup control-file opens in between (a real
  recycled-fd hazard that `_close` hid by swallowing EBADF). Now `except`/`else`.
  Pinned by `test_a_failed_spawn_closes_every_fd_exactly_once`, which asserts the
  fd count is *unchanged* — a leak raises it, a double close lowers it.
- `userns.py` MEDIUM `chown -R` on every spawn: now one `stat`, and the chown
  runs only on creation / new subdirs / wrong ownership. Warm restarts fork
  nothing. Pinned by `test_ensure_service_root_does_no_work_on_an_already_owned_root`
  (counts `run_admin` calls: create=1, warm=0, new subdir=2).
- `cli.py` LOW `$USER` -> `pwd.getpwuid(os.getuid()).pw_name`; the name keys the
  /etc/subuid lookup that every service identity is carved from.
- `cli.py` MEDIUM allocator loads moved inside the guard, catching
  `StateCorrupt`/`ValueError` -> actionable exit 2 instead of a traceback out of
  a systemd unit. Deliberately does NOT re-carve over unreadable state.
- Provisioning was already outside the loop (`_register` before `start_all`, and
  `cmd_provision`); `runtime.runtime_env` is pure, so `extra_env_for` cannot
  provision. Now pinned by a test that makes `runtime.provision` raise and drives
  the loop anyway.

Also: `ams provision` no longer builds a spawner just to reach the uid
allocator, so it needs no delegated cgroup and runs from an ordinary shell next
to a live harness — which is how an agent actually invokes it. Verified live on
racknerd against the `pyhello` (`runtime.kind="uv"`) service another agent had
left crash-looping unprovisioned: `ams provision pyhello` built the venv in
~1.5 s, and after a restart both services are healthy on their own subuid blocks
(hello 100000 / port 20000, pyhello 101024 / port 20001, `six 1.17.0 uid 1000`).

Local 229 passed / 45 skipped, ruff clean. Remote `ams-integ tests/linux`: 45 passed.

CAVEAT: the tree deployed to /home/harness/ams predates the sup agent's
supervisor/health busy-loop fixes and the lead's spawn.py fd-leak fix. Neither is
triggered by the two live services (both exit on the first SIGTERM; measured stop
0.10 s), but re-run `scripts/deploy-racknerd.sh` once those land.

## 2026-09-02 — session 1: DONE (core delivered and live)
- Final independent verification: local 229 passed / 45 skipped, ruff clean;
  remote full suite 275 passed (35 s); `scripts/deploy-racknerd.sh` redeployed
  the tree containing every review fix; live: `hello` (uid 100000, :20000) and
  `pyhello` (uv runtime + six, uid 101024, :20001) both HTTP 200 under
  `ams-harness.service`, cgroups `harness`/`svc-hello`/`svc-pyhello`, no strays.
- Handoff task list status: host prerequisites ✔ (`ams check-host`), schema ✔,
  subuid allocator ✔, spawn (userns + cgroup + held fds) ✔, subreaper ✔,
  supervision loop ✔, lifecycle ops ✔, ports + conflict detection ✔ (socket
  activation evaluated + deferred, see ports.py docstring), systemd unit ✔,
  decision interface ✔ (stub policy + JSONL escalation), runtime provisioning
  ✔ (uv/venv/pnpm/bun; nix = explicit NotImplemented).
- Not done / next session: hot reload of declarations (today: restart the unit
  to pick up a new service.toml); sibling-root traversal hardening (services/
  is o+x, roots 0755 — group-own by mapped gid via run_admin); `runtime.node`
  version honouring; health-probe request lines are INFO noise for the policy;
  `max_retries` naming; concurrency (>2 services) and `cpu.max` under
  contention never measured; consider a dedicated uid for harness (shares uid
  1000 with a pre-existing Docker container on racknerd).

## 2026-09-02 — session 1 (cont.): provision without a cgroup, verified

- `ams provision` needed a delegated cgroup it never uses: `cmd_provision` built
  the isolated spawner just to reach the uid allocator, and `make_isolated_spawner`
  calls `CgroupRoot.discover()`, so it exited 2 from any ordinary login shell —
  exactly where an operator or agent runs it. Now calls `cli.uid_allocator(state)`
  directly (`ams run` is unaffected; it needs the spawner anyway).
  Guarded by `test_provision_never_needs_a_delegated_cgroup`, which poisons
  `CgroupRoot.discover` with an AssertionError (deliberately outside
  cmd_provision's except clause) and asserts the chain still reaches
  `runtime.provision`. Confirmed to fail when the bug is reintroduced, so it is
  a real guard, not a vacuous pass.
- Verified live: `su -l harness` + `python -m ams provision pyhello` -> exit 0,
  idempotent (reuses the venv on a second run); `.venv` owned by 101024 (the
  second block) with `bin/python` a symlink into the shared uv python store;
  after restart both services healthy — hello :20000 uid 100000 (http probe),
  pyhello :20001 uid 101024 (tcp probe), `six 1.17.0 uid 1000` on its stdout.
  Local 230 passed / 45 skipped, ruff clean.

Future noise for the policy (not a bug): a service's response to the harness's
OWN health probe comes back as an INFO log line, e.g.
`[hello:stderr] 127.0.0.1 - - [...] "GET / HTTP/1.1" 200 -`, once per
`health.interval_s` (720/hour/service at the default 5 s). Correct by design —
every byte a service writes goes through the policy — but a real `DecisionPolicy`
should SUPPRESS log lines caused by the harness probing itself. Left to the
policy layer rather than special-cased in `DefaultPolicy`: the general rule is
"suppress self-inflicted traffic", which needs the probe's timing/port context.

## 2026-09-02 — session 1 (cont.): `runtime.sync` + api pilot (two real services live)

- `src/ams/runtime.py` grew uv *project* mode. `runtime.sync = true` (kind=uv,
  schema-checked) means the workdir is a uv project: provisioning runs
  `uv sync --frozen` there and the venv is `<workdir>/.venv`, not `<root>/.venv`
  (new `python_venv_dir(decl, root)`; `runtime_env` and `make_extra_env_for`
  follow it, so the supervisor's lookup and the provisioner cannot disagree).
  `UV_PROJECT_ENVIRONMENT` is deliberately left unset — a project's
  `[tool.uv.sources]` path dependencies resolve relative to the project, and
  moving the environment out of the tree is the configuration uv warns about.
  Missing `uv.lock` is detected by a file check (not by letting `--frozen` fail:
  uv exits non-zero for a missing lock *and* for a stale one, and only the first
  is safe to retry), logs a WARNING and falls back to a resolving `uv sync`.
  `runtime.packages` must be empty in sync mode — `uv add` would rewrite the
  copied repository's own pyproject/uv.lock, so the running service would stop
  matching its source. Rejected in `provision()` before any disk is touched.
- Live on racknerd next to hello/pyhello: `kvservice` :20002 uid 102048 and
  `timeservice` :20003 uid 103072, both from the `api` monorepo, both healthy,
  ports and uids stable across `systemctl restart ams-harness`. Declarations in
  `examples/api-pilot/`, installer `scripts/pilot-api.sh` (idempotent, 24.7 s
  cold / 14.2 s warm), full transcript and numbers in `.claude/state/pilot-api.md`.
- Reflink holds for uv projects too: a third replica's venv cost 2.6 MiB of new
  blocks for a 47.3 MiB environment (94.6 % shared, n=1). The *source* copy does
  not share — 31.4 MiB of blocks per service for a 17.4 MiB tree — and is now the
  dominant per-service cost. Copying the shared tree once and reflinking it per
  service is the obvious next optimisation.

Finding for the policy layer (real gap, not noise): **a service's own
`logger.warning` reached the harness as severity INFO.** Neither the api SDK nor
uvicorn configures the root logger, so `logging.lastResort` writes the bare
message with no level prefix and `ams.events.classify` finds no token to key on.
Verbatim: `[kvservice:stderr] acl refresh failed: [Errno 111] Connection refused`
→ INFO. Zero escalations were emitted in the whole pilot, which looked clean and
partly was not. Fix belongs at the source (services should log a level prefix)
or in a per-service severity parser; the heuristic cannot recover a level that
was never printed. Separately, an operator-initiated stop logs
`ERROR ams.supervisor: <id> exited code=None signal=15` before correctly
declining to restart — that should be INFO when the operator asked for it.

## Secrets: write-only store + injection (2026-09-02, D16)

`src/ams/secrets.py` is the whole feature: `SecretStore` (0700 dirs, 0600 files
under `<state>/secrets/<id>/<NAME>`, atomic temp+rename writes, NUL rejected,
exactly one trailing newline stripped, empty values rejected), `store_for`,
`make_extra_env_for` (composes with the runtime lookup; secrets win, reserved
names still beat both because `SpawnRequest.env` applies them last),
`warn_missing_secrets`, and the `ams secret set|rm|list|check` CLI. A declared
secret with no value raises `MissingSecret` at start time -> spawn failure ->
escalation; startup only warns, so one unset secret cannot keep the other
services down.

`cli.py` changed in three spots only (a concurrent agent owned the rest of the
file): the `secret` subparser at the end of `build_parser`, the `secret` branch
in `main`, and the `extra_env_for` composition in `build_supervisor`. The
argument wiring and every handler live in `secrets.py` so that all code that can
touch a value sits in one auditable module.

Tests: `tests/test_secrets.py` (38 portable) plus one Linux test in
`tests/linux/test_isolated.py` that spawns a service with the value injected via
`extra_env` while the 0600 store file itself is opened from inside and fails
with `PermissionError`/EACCES — the D4 argument, executed. Suite 280 passed /
48 skipped locally; `scripts/remote-test.sh ams-sec ...` 57 passed on racknerd.

Live: pilot rerun with both api replicas taking `SVC_SECRET` from the store,
values generated on the box and never printed. Details and the leak audit in
`.claude/state/pilot-api.md`.

Open / not done: `ams secret` has no `--state-dir`-less discovery of *which*
services declare secrets (there is no `ams secret check --all`); rotation is
"rm + set + restart the service" with no command of its own; nothing prunes the
store when a declaration is removed (`SecretStore.remove_all` exists and is
tested, but no caller invokes it — wire it into the reload path's service
removal when that is settled).

## 2026-09-02 — session 2: control socket + hot reload (D17 implemented)

New: `src/ams/control.py` (unix-socket control channel) and `src/ams/reload.py`
(declaration reconciliation). `ams ctl <op> [id]` client and the `reload` wiring
live in `cli.py`; `deploy/ams-harness.service` gained `ExecReload=/bin/kill -HUP
$MAINPID`. Live verification with real pids, cgroups and ports is in
`.claude/state/e2e-systemd.md` (session 2).

Supervisor extension points added rather than letting control.py reach into
private state: `register_fd(fd, cb, events=, name=)` / `unregister_fd(fd)`
(selector payloads are wrapped in `_FdCallback`, so `run_once` can tell them from
the `(id, stream)` pipe tuples), `replace_decl(id, decl, ports)` (swaps the
declaration and rebuilds the health monitor; the running process keeps the old
one until it is restarted), and `run_forever(on_reload=)` fed by SIGHUP through
the existing self-pipe.

**Bug found and fixed while testing** (regression test:
`test_restart_in_the_reaped_but_undrained_window_actually_restarts`, verified to
fail without the fix): `Supervisor.restart()` branched on `st.spawned is None`,
but in the reaped-but-undrained window `spawned` is still set while the process
is already gone. `stop()` then correctly declines to signal a dead pid and
returns, leaving the service down with `desired="up"`, `restart_pending=True`
and no timer to ever start it again — a permanent hang. `restart()` now treats
`st.reaped` as down and routes to `start()`, which already force-finalizes the
old pipes. This was previously unreachable in practice; `ams ctl restart` in the
couple of seconds after a crash makes it reachable.

**Also found:** a unix socket path is capped at ~104 bytes (`sun_path`), and
pytest's `tmp_path` is ~115, so tests bind under a short `/tmp` dir instead. Both
`ControlServer.open()` and the client name this case explicitly, because
"cannot connect" would otherwise send the reader hunting for a stopped harness
that is in fact running.

Open, deliberately not done:
- A removed service keeps its uid block (`uidmap.json`) and its service root on
  disk; only the port is released. Releasing the block while the root is still
  owned by it would hand a later service ownership of that tree. The pair
  belongs to a future `ams rm <id>`. Cost today: a removed id permanently
  consumes 1 of 64 blocks.
- Reload never provisions (D17): a new declaration with a real runtime needs
  `ams provision <id>` first, because `runtime.provision` blocks for seconds to
  minutes and the loop is single-threaded.
- `ERROR ams.supervisor: <id> exited ... signal=15` on every operator stop is
  now routine (each removal and each changed declaration produces one).
  `severity_of(ServiceExited)` should account for `desired`/`restart_pending`.
- The control channel has no in-band auth; the socket is 0600 in a 0750
  harness-owned dir. A peer that can open it already runs as the harness user.

### Addendum (same session): secrets on the reload path, and a start() guard bug

`reload()` now calls `ams.secrets.warn_missing_secrets` over **only the ids the
reload touched** (added + changed) and returns them as `secrets_missing` in the
summary. Scoped deliberately: warning over every declaration would re-log the
same known-missing secret on every SIGHUP, which an operator learns to skip. It
never gates — a missing value is a per-service start failure by design, and the
other services in that reload go through regardless, so an absent secrets module
or a stat error degrades to "no warning".

**Second bug found and fixed** (regression tests:
`test_a_raising_extra_env_hook_fails_only_that_service` in test_supervisor.py and
`test_reload_warns_about_missing_secrets_without_gating_the_others` in
test_control.py; both verified to fail against the old code):
`Supervisor.start()` called `self._extra_env_for(st.decl)` *outside* its
try/except. That hook is the runtime + secrets composition and it genuinely
raises — `MissingSecret` for a declared secret with no stored value. So the
exception escaped `start()` entirely instead of becoming a spawn failure:

- at boot, `Assembly.start_all()` iterates services in order, so ONE service with
  an unset secret aborted the loop and took down every service after it in the
  list, before the harness had spawned them;
- on reload, it aborted the reload part way through its service list.

`ams.secrets.make_extra_env_for`'s own docstring already claimed this became a
"spawn failure -> escalation, by design"; it now actually does. The env hook call
and the `SpawnRequest` construction moved inside the existing guard, so a failing
env hook is a failed start of that service and nothing more.

Not changed, reported to the secrets agent instead (their call site, their
ownership): `build_supervisor` calls `warn_missing_secrets(state, declarations)`
unguarded at cli.py, so an OSError from the store (unreadable/permissions) would
raise out of `ams run` rather than warn. Same shape as the bug above — a
heads-up function able to take down startup.

### Secrets follow-up (2026-09-02, after the ctl agent's review)

Two bugs of the same shape, both found by wiring the secrets hook into paths
that did not expect it to raise. Worth stating as one lesson: **a hook that can
raise must be inside the caller's failure guard, and an advisory diagnostic must
not be able to raise at all.**

1. `Supervisor.start()` called `extra_env_for` outside its try/except (fixed by
   the ctl agent in `supervisor.py`). `MissingSecret` escaped `start()`, so one
   service with an unset secret aborted `Assembly.start_all()` and every service
   after it in the list never spawned. Regression test
   `test_a_raising_extra_env_hook_fails_only_that_service`.
2. `warn_missing_secrets` could itself raise `OSError` on an unreadable store and
   take down `ams run` at boot. Now guarded **inside the function**, per service,
   rather than at each call site: the "advisory, never fatal" contract then holds
   for every present and future caller instead of depending on each one
   remembering to wrap it. An unreadable directory is logged at ERROR and
   skipped, and does not suppress the warnings for the services behind it. Tests
   `test_an_unreadable_store_is_reported_not_fatal` and
   `test_build_supervisor_survives_an_unreadable_store` (both skipped as root,
   which bypasses directory permissions). Verified the guard is load-bearing and
   not decorative: `store.missing` on a 0000 directory raises `PermissionError`
   (errno 13) when called unguarded.

Live after the redeploy at 16:51:27 UTC: four services healthy, `SVC_SECRET`
present in both api replicas' environ, store files 0600 harness, no 64-hex token
in the journal, and `ams ctl reload` returns `secrets_missing: []` — the ctl
agent's reload wiring calling this function end to end. Deployed `secrets.py`
sha256 matches local.

## T1.4 — SDK log level patch (2026-09-02)
`setup_sdk` (api/components/sdk/src/sdk/fastapi.py) now calls
`logging.basicConfig(level=INFO, format="%(levelname)s %(name)s: %(message)s",
stream=sys.stderr)` when `logging.getLogger().handlers` is empty — additive,
composes with `sdk/cls.py` (skipped once any handler, incl. CLSHandler, exists).
Fixes D15 gap 1 (bare `logging.lastResort` messages with no level token).
Module diff: 8 lines. New test `tests/test_fastapi_logging.py` (subprocess-isolated
root logger) covers both the configured and already-configured cases; full SDK
suite 295/295 passed, ruff clean. Committed `3ce73d51` on branch `ams-platform`
in `api/`, not pushed.

### T1.1 source delivery (2026-09-02)

`src/ams/platform/{__init__,sources}.py` implement `SourceMirror`: bare mirror,
`fetch(ref) -> sha`, `materialize(sha)` via `git archive | tar` (two Popen, no
shell, atomic rename), `stage(sha, root, block)` as a `cp -a --reflink=auto`
inside the admin ns followed by `chown -R 1000:1000` and a two-`mv` swap, and
`gc(keep)` over a last-use index. Rationale and rejected alternatives in D19.
Verified on racknerd (n=1): a 17.0 MiB tree extracts for 17.01 MiB of free
space, and each per-service `stage` costs 0.01 MiB -- reflink held; staged files
are owned by host uid 100000, re-staging the same sha does not touch `.ams-sha`.
28 portable + 5 Linux tests, `scripts/remote-test.sh ams-src ...` 33 passed with
zero skips. Remote scratch (`/home/harness/ams-src`, `store/state/plat-src-test`)
removed; nothing else on the box touched.

### T2.3 registry client (2026-09-02)

`src/ams/platform/registryclient.py` implements `RegistryClient` (stdlib
`urllib.request` only): `create_identity` (`POST /api/services`, 409 = success
`created=False`, other 4xx immediate `RegistryError`, 502/503/504 and network
errors retried on the deployer's `(1,2,4,8,15,30,60,60,60)` schedule with an
injectable `sleep_fn`), `upsert_acl` (`POST /api/acl` once per rule, same retry
policy), `wait_healthy` (`GET` until 200 or a deadline, injectable clock), and
module-level `from_sidecar(reg)` mapping a `registry.json` sidecar to
`(service_id, create_identity_kwargs, acl_rules)`. `base_url` is rejected
outright unless `http://127.0.0.1:<port>` or `http://localhost:<port>` (Phase-A
guard, PLAN-allin.md risk 5). Secret and admin token never reach a log record,
an exception message, or `repr()`/`str()` of the client (`_Redacted` wrapper).
22 tests in `tests/test_platform_registryclient.py` against a threaded stdlib
`http.server` fake that records every request and replays a scripted response
list — covers 201/409/502-then-200/403/exhausted-backoff/network-error for
both `create_identity` and `upsert_acl`, `wait_healthy` true/false/transport-
error, base_url rejection, secret non-leakage (`caplog` + exception text +
`repr`), and `from_sidecar` round-tripping into both calls. `pytest -q
tests/test_platform_registryclient.py` 22/22 green, ruff clean. Full suite:
396 passed / 1 failed (`test_supervisor.py::test_an_operator_stop_is_not_
reported_as_an_error`) / 57 skipped — the one failure is in a file another
agent (T1.3) is mid-editing, unrelated to this module.

### T1.2 manifest translator (2026-09-02)

`docs/platform-sidecars.md` fixes the JSON shapes for `mount.json`,
`registry.json` and `platform-state.json` (all `"version": 1`; state machine
`fetched → translated → provisioned → declared → reloaded → registered →
healthy` plus `failed`, with `prev_sha` and an `escalated` flag so a repeated
failure escalates once). `src/ams/platform/yamlsubset.py` is a stdlib YAML
subset parser (block maps/seqs, flow maps/seqs, quoted + plain scalars,
multi-line only via quoted strings) that rejects anchors, aliases, tags, merge
keys, multi-doc, block scalars, tabs, duplicate keys, ambiguous numerics and
the YAML 1.1 `yes/no/on/off` booleans, naming line + construct.
`src/ams/platform/translate.py` implements `TranslateContext` (loopback-only
registry/auth URLs — PLAN-allin risk 5 — enforced in `__post_init__`),
`translate()` → `Translation(id, kind, decl, mount, registry, flags)`, and
`emit_toml()`. `docs/manifest-translation.md` is the mapping table + the
unsupported list. All 21 manifests translate: **19 services, 2 static
(files-web, llm-web), 0 rejected**; the 2 `.disabled` files are skipped by
name. Goldens for all 21 (`tests/golden/platform/<id>.{toml,mount.json,
registry.json}`) plus committed copies of the manifests themselves under
`tests/golden/platform/manifests/` — `api/` is gitignored, so without the
copies the suite could not run on the Linux host; a skip-if-absent test asserts
the copies stay byte-identical to the clone. 179 tests in
`tests/test_platform_translate.py` (PyYAML oracle over all 21 + 10 synthetic
cases, 15 YAML rejections, 27 manifest rejections, loopback gate, round trip,
`ams validate` over all 19 emitted declarations). Full suite 576 passed /
57 skipped, ruff clean.

## T1.3 core gaps in ams (2026-09-02)
Four gaps closed against `PLAN-allin.md` T1.3, one unit test per item, each
verified failing against the pre-change tree first (18 new portable tests, 4 new
Linux tests). (a) `ensure_service_root` always creates `<root>/data` (0750,
service-owned) and `SpawnRequest.env()` exports `AMS_DATA_DIR`; the harness
creates it itself while the root is still harness-owned, so the "warm path forks
nothing" invariant survives, and only the upgrade path (a root created before
this) pays an admin-ns `mkdir`. (b) `[logging] format = auto|level-prefix|json|
plain` (`LoggingSpec`) is threaded `ServiceDecl → LogLine.from_raw →
events.classify`; a stated level beats the message body, a line without one falls
back to the heuristics. (c) `ServiceExited.expected` is set by the supervisor for
stop/kill/shutdown/reload-restart; those exits are INFO and `DefaultPolicy`
returns LOG for them, so a reload no longer emits one ERROR per service.
(d) `DefaultPolicy.suppress_self_probes` drops loopback 2xx/3xx GET/HEAD access
lines for the declared `health.path` (uvicorn + http.server shapes); a 500 there
still escalates. Docs: `docs/service-declaration.md` gained `[logging]` and the
`AMS_DATA_DIR` contract. Local 576 passed / 61 skipped, ruff clean on the files
this task owns; remote `ams-core` (`tests/linux/test_userns.py`,
`test_isolated.py`, `test_e2e.py`) 38 passed. The data dir makes one T1.1
assertion stale (`test_a_new_sha_replaces_the_repo_and_leaves_no_scratch_dirs`
expects `["repo"]`, now `["data", "repo"]`); handed to that task's owner, file
not touched. Remote `ams-core` dirs removed.

Follow-up after T1.3 landed `<root>/data` in `ensure_service_root`: the "no
scratch dirs" assertion in `tests/linux/test_platform_sources_linux.py` now
checks that `data` *survives* the `mv`/`mv`/`rm` repo swap (a re-sync that wiped
it would destroy a service's sqlite state) and that nothing but `repo`/`data`
is left. Remote re-run: 33 passed, reflink numbers unchanged (17.01 / 0.01 / 0.01 MiB).

## T2.2 — gateway generator + pinned Caddy binary (2026-09-02)
`src/ams/platform/gateway.py`: `render(mounts, ports, GatewayConfig) -> {relpath: text}`
(pure) + `write(state, files) -> [changed paths]` (atomic, 0755 dirs / 0644 files
so the mapped Caddy uid can read them, stale `sites/*.caddy` removed, unchanged
content not rewritten so the caller can skip the restart) + `resolve_ports()` +
`caddy_declaration()`. The deployer's security-header block, `default_csp`,
`csp_map` (locationservice, pages) and `admin_cors_map`, plus the static
template's SPA fallback and the bootstrap Caddyfile's SPA CORS + `/pages/api/*`
rules, are module-level data with source citations; five golden scenarios
(path / subdomain / static / logdir / tls) pin every byte and per-header tests say
why each line exists. See DECISIONS D21 for the version pin, the plain-HTTP
Phase-A switch and what was rejected.

Caddy **2.11.4** installed on racknerd at `/home/harness/store/bin/caddy`
(0755 harness) by the new `deploy/install-host.sh` §4c; tarball sha256
`527fbf91…`, binary sha256 `b7105518…`, both pinned in the script. Verified live
as `harness`: `caddy validate` exit 0 on all five renders, output already
`caddy fmt`-canonical, and a briefly-run gateway answered `/ams-health` 200,
`/nope` 404, and served the `pages` sandbox CSP + Referrer-Policy + X-Frame-Options
on a real response. Local 633 passed / 68 skipped; remote `ams-gw` 69 passed
(57 portable + 12 Linux), remote dirs cleaned. The Linux file is
`tests/linux/test_platform_gateway_live.py`, not `…_gateway.py`: pytest's
default import mode rejects two test modules with the same basename.

## T2.1 — Layer-0 bootstrap (2026-09-02)
`src/ams/platform/bootstrap.py`: `bootstrap(state, store, *, registry_port_name,
examples_dir) -> BootstrapResult` (created/updated/existing labels, never values)
plus the pieces T3.1/T3.2 call separately — `ensure_keypair` (one `openssl
genpkey` + `openssl rsa -pubout`, argv lists, temp dir then `os.replace`, 0600/0644
in a 0700 `<store>/platform/`), `ensure_secrets` (`token_hex(32)` straight into the
SecretStore, never overwriting), `ensure_service_dirs` (`data` 0750 + `etc` 0755,
chowned to the block, warm path forks nothing) and `place_jwt_key(state, id, block,
*, store, private=False)` (mkdir/cp/chown/chmod through `run_admin`, 0444 public /
0400 private, warm path is one `stat`). No `ams.cli` wiring — T3.1 owns the
`platform` subparser; `python -m ams.platform.bootstrap` is the interim door.

Declarations written to `<state>/services/{registry,auth}/service.toml` and
`examples/platform/layer0/`. Read out of `api/`, not guessed: both components run
`uvicorn <pkg>.main:app` with a **module-level lifespan app, not a `build_app`
factory** (no `--factory`); registry has `/health`, auth has no health route at
all (tcp probe); auth's GitHub creds are **not** required — `_build_oauth_providers`
skips the provider unless both are set, so placeholders were dropped. Ports fixed
at registry 20100 / auth 20101 so every cross reference is a literal. See
DECISIONS D22.

Local 30 passed (`tests/test_platform_bootstrap.py`); whole suite 699 passed,
2 pre-existing failures in `tests/test_platform_backup.py` (T2.4, mid-edit, not
touched here). Remote `ams-boot` 36 passed (30 portable + 6 Linux); remote dirs
and the scratch `plat-boot-test-*` tree removed, live state dir untouched.
Unverified and owned by T3.2 (n=0 live starts): whether registry and auth actually
come up under these settings.

## T2.4 — backup + restore drill (2026-09-02)
`src/ams/platform/backup.py` (stdlib only): `discover(state, allocator)` lists
`<root>/data/*.{db,sqlite,sqlite3}` through `run_admin(["find", …])` because the
harness cannot list a 0750 service-owned dir; `snapshot(target, workdir)` runs the
stdlib `Connection.backup` as inner root (the `sqlite3` CLI is **absent** on
racknerd — checked) then gzips harness-side to `<id>-<stem>-YYYYMMDD.db.gz`;
`upload`/`prune` shell out to the pinned rclone with `RCLONE_CONFIG_R2_*` from the
SecretStore under pseudo-id `platform-backup`; `restore(gz, dest)` gunzips and
raises unless `PRAGMA integrity_check` says `ok`. `run(...)` reports one JSON line
per target in `JsonLinesEscalation`'s shape, continues past a failing target and
exits 1. `--dry-run` snapshots for real and prints the exact rclone argv with the
credential env reported **by name only**. See DECISIONS D23.

Also added: `scripts/install-rclone.sh` (pinned v1.75.0, zip sha256 verified
against upstream `SHA256SUMS` plus a separately pinned binary hash, idempotent,
installs to `<store>/bin/rclone`) and `deploy/ams-platform-backup.{service,timer}`
(04:10 UTC, `RandomizedDelaySec=15min`, `Persistent=true`, `User=harness`, the same
Environment block as `ams-harness.service`, `NoNewPrivileges=no` because the
snapshot needs `run_admin`'s setuid map helpers). Units are **not installed or
enabled** on racknerd — T4.2 does that.

Verified: local `tests/test_platform_backup.py` 37 passed, whole suite 701 passed /
83 skipped, ruff clean on all five files. Remote `scripts/remote-test.sh ams-bk`
**41 passed** — the restore drill runs on real service-owned data: a db written as
inner uid 1000, the harness proven unable to `open`/`stat`/`iterdir` it, snapshotted
through the admin ns, the original deleted, restored, the row back, and the service
still able to commit afterwards. rclone installed at `/home/harness/store/bin/rclone`
(`rclone v1.75.0`); env-var-only remote confirmed offline (`rclone listremotes` →
`r2:` with `RCLONE_CONFIG=/dev/null`). `systemd-analyze verify` clean (rc 0) for both
units. `python3 -m ams.platform.backup run --dry-run` exits 0 under the unit's exact
interpreter and environment. Remote dirs `/home/harness/{ams-bk,state/ams-bk}` and
the `plat-bk-test` tree removed; live state dir untouched.

Unverified (n=0): **the R2 upload and the retention sweep have never run.** No
credentials were available; `upload`/`prune` are pinned by argv equality only. The
real round trip is T4.2.

## T3.3 platform policy + escalation — done (2026-09-02)

`src/ams/platform/policy.py` (~640 lines) + `tests/test_platform_policy.py`
(66 tests) + one minimal `src/ams/cli.py` edit (`ams run --policy default|platform`).

`PlatformPolicy(DecisionPolicy)` wraps `DefaultPolicy` — always delegates first,
then refines, and never changes a RESTART or a STOP. Four rules:
1. **Cause dedupe.** Key = `(service_id, kind, normalize_cause_text(text))`; the
   normalizer strips ISO/CLF/clock timestamps, ipv4(:port), `pid=`, 32–64-char hex
   ids, shorter hex ids with a letter, and numbers ≥3 digits. One escalation per
   cause per `window_s` (600 s); repeats become `LOG` with
   `reason="deduped (n=k)"`; a closed window emits one
   `"repeated k times in window"` summary.
2. **Post-sync health gate.** Reads `<state>/platform/state.json` (T3.1's file,
   absence tolerated, mtime-cached). A service not at `healthy` for more than
   `health_grace_s` (300 s) past `stage_since` escalates **once** per
   `(id, sha, stage)` with both `sha` and `prev_sha` and
   `"suggested action: rollback to prev_sha"`. Crash loops
   (`consecutive_failures ≥ 2`) within the grace of a sync's `updated_at` escalate
   with the sha pair too. Recommendation only — the rollback is T4.3.
3. **Caddy.** Guarded `json.loads` on `event.text` for `service_id == "caddy"`
   only: access lines <500 SUPPRESS, ≥500 ESCALATE once per (path, status),
   warn-level TLS/certificate lines SUPPRESS (Phase A is plain HTTP, D21),
   malformed JSON falls through to `DefaultPolicy`.
4. **Registry heartbeat noise.** Until a `HealthChanged(registry, healthy=True)`
   passes through the policy, other services' heartbeat/acl-refresh
   connection-refused lines are downgraded to LOG.

`EscalationDeduper` and `DedupingEscalation` are standalone so T3.1's sync loop
can wrap them around its own JSONL sink (its `PlatformSync` records never reach a
`DecisionPolicy`). Provided, not wired — T3.1 owns that call site.

`flush(now)` is the tick: health gate + expired windows + queued records. It emits
through the policy's `escalation` sink when one is set and returns what it emitted.
`cmd_run` calls it from `run_forever(on_iteration=...)`.

Verified: `tests/test_platform_policy.py` 66 passed; whole suite **813 passed /
83 skipped**; ruff check + ruff format clean on both new files and on `cli.py`.
Live wiring proven by hand — `ams run --policy platform --no-isolation` against a
tmp state dir with a `sleep` service and a synthetic `state.json` at `stage=failed`
produced **exactly one** stdout JSONL escalation carrying both shas and the
rollback recommendation, over ~3 s of loop iterations, then shut down clean (rc 0).
The same run is pinned by two tests (`build_supervisor(policy=...)` and a
subprocess `ams run --policy platform`).

Unverified: the Caddy payloads and the SDK heartbeat wording are hand-written from
documented shapes, **n=0 real log lines**; `window_s`/`health_grace_s` are derived
from PLAN-allin Q8, **n=0 against a running fleet**. See DECISIONS D24 open section.

## T3.4 static sites + third-party secret names (2026-09-02)

`src/ams/platform/static.py`: `publish_static(mount, checkout, state, store,
block)` stages a `kind: static` mount's built output at
`<state>/platform/static/<id>/`, atomically and idempotently by sha (`.ams-sha`
marker), harness-owned 0755/0644. Source is copied from `checkout/apps/<id>`
(reflink, direct as the harness); `mount["build"]` (deploy.install verbatim)
runs, argv-only, inside the admin ns via `ams.userns.run_admin` +
`ams.runtime.provisioning_env` (never as the bare harness process — D25); an
argv allowlist (`bun`/`pnpm`/`find`/`cp`/`rm`/`mv`/`mkdir`, no shell tokens)
covers exactly what `files-web`/`llm-web` need and rejects anything else.
`load_ams_overlay(manifest_dir)` / `overlay_secret_names(manifest_dir)` read an
optional `service.ams.toml` beside `service.yaml` for third-party secret NAMES
`TranslateContext.extra_secret_names` has nowhere else to come from
(`docs/manifest-translation.md` new section). See D25 for the full reasoning
and the rejected alternatives.

Overlays committed on `api/`'s `ams-platform` branch (not pushed):
commentservice (`DEEPSEEK_API_KEY`), wechatservice (`WECHAT_MP_APPID`,
`WECHAT_MP_APPSECRET`), notificationservice (`BARK_DEVICE_KEY`), emailservice
(`RESEND_API_KEY`), oss (`R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`,
`R2_SECRET_ACCESS_KEY`) — each cites its `os.environ[...]` source line.
llmgateway investigated, needs none (n=1 grep, no third-party key).

Verified: `tests/test_platform_static.py` 23 passed (portable; `run_admin`
faked for the bun/pnpm-form test, real `cp -a --reflink=auto` for the no-build
path); whole local suite 836 passed / 83 skipped, ruff clean. Live:
`scripts/remote-test.sh ams-static tests/linux/test_platform_static_live.py
tests/test_platform_static.py` → 26 passed on racknerd, including a real
`bun install && bun build` inside the admin ns (bun is installed there) with
the published `index.js` read back as a genuinely different mapped uid
(`setpriv --reuid 1000`, proving world-readability, not same-uid access).
Remote scratch (`/home/harness/ams-static`, `/home/harness/state/ams-static`,
`/home/harness/store/state/plat-static-test`) cleaned after the run.

Hooks for T3.1 (sync loop), not wired by this task:
1. Before calling `translate()` for a manifest at `manifest_dir`: `ctx =
   TranslateContext(..., extra_secret_names=tuple(ams.platform.static.
   overlay_secret_names(manifest_dir)))`.
2. For a `kind: static` `Translation` (`t.decl is None`): `ams.platform.
   static.publish_static(t.mount, checkout, state, store, block)` — `block`
   may be `None` unless `t.mount["build"]` is non-empty, and when it is
   required any allocated `UidBlock` works (never chowned to, see D25).

Confidence: high on the static-publish mechanics (real Linux run, including
the bun path). Medium on the overlay secret-name lists — each is a single
2026-09-02 grep of one file per service; a future provider change in any of
these five services needs its own re-check (flagged in DECISIONS.md).

## T3.1 `ams platform sync` (2026-09-02)
`src/ams/platform/sync.py` — one-shot tick: fetch → materialize → discover manifests
(skipping `*.disabled`) → translate (with `service.ams.toml` extra secret names) →
stage/`place_jwt_key`/provision → generate `SVC_SECRET` + write `service.toml` and both
sidecars → **one** `ams ctl reload` → render the gateway from the mount sidecars on
disk and `restart caddy` if it changed → registry identity + ACL → health gate. Progress
is `<state>/platform/state.json` in the sidecar doc's shape; every failure is one
`JsonLinesEscalation`-shaped record on stdout with `kind="PlatformSync"` and leaves the
other services running. Plus `src/ams/platform/cli.py` (`ams platform sync|status|
bootstrap`, wired into `ams.cli` with two edits) and `deploy/ams-platform-sync.{service,
timer}` (oneshot, `OnBootSec=2min`/`OnUnitActiveSec=60s`, `Persistent=false`) — **not**
installed on racknerd; T4.1 does that. See DECISIONS D24.

Verified locally: `tests/test_platform_sync.py` **61 passed** (fake git repo with three
commits, threaded fake registry that also answers the health probe, `stage`/`provision`/
`place_jwt_key`/`ctl reload`/`ctl restart` recorded); whole suite **904 passed / 86
skipped**; ruff clean on all four files. Covered transitions: fetched→…→healthy for two
services, `declared` as the terminus for a static site, and a failure injected at fetch,
translate, provision, register (403) and health — each leaving `stage="failed"` with an
`error` naming the transition and exactly one escalation.

T3.4 hooks wired (2026-09-02): `ams.platform.static.load_ams_overlay` is now the **only**
`service.ams.toml` reader — sync's own copy was deleted — and both halves of it are used:
`secrets` feeds `TranslateContext.extra_secret_names`, and `[env]` is folded into the
declaration with `dataclasses.replace` (which re-runs `ServiceDecl.__post_init__`, so a
name colliding with a declared secret raises `DeclError` at translate time, not at spawn).
`publish_static` is a `kind: static` mount's `provisioned` step, called before the gateway
render, allocating a uid block only when `mount["build"]` is non-empty. `DedupingEscalation`
was **not** adopted — see DECISIONS D24.

Unverified (n=0): **nothing here has run against a real harness, registry or repo.** The
control socket, `SourceMirror.stage`, `place_jwt_key` and `runtime.provision` are all
recorders in these tests; the live path is T3.2/T4.1. The 60 s timer's steady-state cost
(one `git fetch`, no writes) is asserted in a test, not measured on racknerd. `publish_static`
runs for real in these tests but only over a directory holding one file, on macOS, where
`cp -a --reflink=auto` resolved to GNU coreutils — **no build step has ever executed**
(the fixture's static manifest declares none).

## T3.2 (live) — Layer-0 bring-up on racknerd (2026-09-02)
Registry, auth and Caddy run as ams services under the live `ams-harness.service`
beside hello/pyhello/kvservice/timeservice — **7 services, all `running`/healthy**.
The two pilot services were re-pointed at the replica: translated from their
manifests (no `SVC_DEV`), staged at upstream `main` `5ea3572`, registered in the
replica registry, heartbeating (`last_seen` 11 s), gateway-routed at `/kv` and
`/time`. Full transcript with every number: `.claude/state/platform-layer0.md`.

`src/ams/platform/layer0.py` — `bring_up(state, store, repo_url=..., ref="main")`
orchestrates 17 named stages (fetch → materialize → bootstrap → allocate →
stage/keys/provision Layer 0 → stop/stage/translate/provision Layer 1 → gateway →
reload → health → identities → start → health) and raises `Layer0Error(stage, …)`
carrying the partial `Layer0Report`. Four ordering constraints are the deliverable
and each is a test: identities before a Layer-1 start (no `SVC_DEV` ⇒ the SDK
registers in the FastAPI lifespan and a 404 there is a uvicorn startup failure);
uid block before the reload; fixed Caddy port 20180 because the entry site's own
listen address is inside the Caddyfile; stop a service before re-staging the tree
its venv lives in. `scripts/platform-bootstrap.sh` drives it.

**Source: the local mirror path, not GitHub.** `StevenLi-phoenix/api` is private
(`curl` → 404, `git ls-remote` → "could not read Username"), the harness holds no
credential and none was made. The script pushes a bare mirror to
`<store>/upstream/api.git` and points `SourceMirror` at that path — a shape
`validate_url` already accepts. Phase A therefore runs upstream `main`, **without**
the T1.4 SDK root-logger patch, so SDK `logger.warning` is still classified INFO.

**The live gate found two real defects, both outside T3.2's nominal scope, both
fixed** (diagnosis: `.claude/state/diagnosis-layer0.md`, DECISIONS D24):
`UidAllocator.allocate` was not idempotent across processes (the harness never
re-read `uidmap.json`, so a reload re-carved blocks another process had already
staged files under — this would have broken T3.1's `ams provision` + `ams ctl
reload` loop too); and `StateDir.ensure()` re-narrowed `services/` to 0750,
removing the `o+x` `ams.cli._ensure_traversable` adds, which broke the next spawn
of every running service with `PermissionError` on its own interpreter.
`ams/uidmap.py` and `ams/state.py` were changed for these; `cli.py` and `schema.py`
were not touched.

**It is NOT PLAN-allin risk 4.** Registry and auth start rootless with data outside
`/var/lib` on the first attempt; they were never executed until the harness would
register them. Q4's env contract needed no correction — `bootstrap.py` is unchanged.

Verified: local `tests/test_platform_layer0.py` 46 passed (call-sequence equality,
failure injection at all 17 stages, the two ordering invariants, no secret in the
report); `tests/test_uidmap.py` +3 and `tests/test_state.py` +1 regression tests;
whole suite **897 passed / 86 skipped**; ruff clean on all changed files. Live:
`ams ctl status` 7/7 running, `/kv/health` and `/time/now` 200 through Caddy on
20180, registry `discover/{keyvalue,time}` 200, and a real RS256 M2M token minted
by registry for `timeservice`→`kvservice` accepted by kvservice (204/200) where
anonymous gets 401 and a tampered signature gets 401. 47 escalations in the window,
all classified (§7 of the transcript): 7 Caddy start-up WARNINGs are noise for T3.3
to suppress, the rest are real and caused by the manual repair restart.

Memory: 553 → 710 MiB used (`free -m`), 157 MiB for the three new services; harness
cgroup total 378.9 MiB for all seven. Registry 92.8 MiB and auth 94.8 MiB are
**cold** numbers, n=1 — not planning constants.

Open (n=0): auth's user-JWT path is untested (no health route, no OAuth, no account
can exist); the gateway is loopback-only with no subdomain or static mount live;
the registry's `endpoint` column still holds the production URL; the M2M policy row
was inserted by hand because nothing seeds `service_policies` outside migrations.

Per-service change detection added (2026-09-02, D26): `SourceMirror.changed_paths`
(`git diff --name-only --no-renames`, 3 unit tests in `tests/test_platform_sources.py`)
plus a `deployed_sha` on each record. A service is translated at the sha it is already
deployed at unless the commit range touched its manifest directory or a shared prefix
(`SyncConfig.shared_prefixes`, default `("shared/", "components/sdk/")`), so its
declaration is byte-identical and nothing restarts it. A docs-only commit is now a
whole-fleet no-op; a `components/sdk/` or `shared/` change fans out to everything;
`components/registry` does not. This closes the "`GIT_COMMIT` restarts the fleet"
finding recorded above.

## Start-order dependencies (`depends_on`) — 2026-09-02

Cause of the work: at ~19:00 UTC the racknerd harness restarted
(`KillMode=control-group`), all 18 services were started at once, and the 11
platform services that call `sdk.registry.start()` during FastAPI startup got
`RegistryError: register failed: Connection refused` → "Application startup
failed". Each crashed 5× inside 90 s and the supervisor gave up on all of them.
ams had no notion of start order.

Added: top-level `depends_on = ["registry", ...]` in `service.toml`
(`ams.schema`, validated locally — id pattern, no self-reference, no
duplicates), a `waiting` status in the supervisor, and `depends_on = ["registry"]`
injected into every translated Layer-1 declaration
(`ams.platform.translate.DEPENDS_ON`). The 19 service goldens each grew exactly
that one line; the diff was inspected line by line. Layer 0 and the gateway
declare nothing — verified in `api/components/auth`: auth's only registry use is
`email_client_from_env()`, whose `M2MClient.start()` builds an httpx client and
makes no call, so auth starts fine with the registry down.

Mechanics: `Supervisor.start()` gates on `_check_start_gate`. A service whose
dependencies are not all running+healthy is parked (`status="waiting"`,
`waiting_for=(...)`, `attempt` untouched) with `restart_at = now + dep_poll_s`
(1 s, overridable per Supervisor for tests) — reusing the existing restart timer
is what keeps the `_poll_timeout` invariant true, since the deadline is always
in the future and `_run_timers` is exactly what acts on it. `_dependency_layers`
(Kahn) drives a reverse-topological `shutdown()`: dependents are stopped and
given a chance to exit before their dependencies, best effort inside the one
existing shutdown deadline. `status()` gained `waiting_for`; the CLI summary
line prints it when non-empty.

Verified: full local suite **990 passed, 0 failed** (the 2 concurrent agents'
tests included; the skip count moved 86 -> 109 during the task as they added
Linux-marked tests), ruff clean on every file touched. Remote
`scripts/remote-test.sh ams-deps tests/linux/test_e2e.py tests/linux/test_isolated.py`
**34 passed**; the portable supervisor/schema/translate files also run green
there (290 passed, 3 runs). New tests: `tests/test_supervisor_deps.py` (21) plus
schema and translate cases.

Open (n=1 in 3 runs, NOT a regression from this change): on racknerd under the
load of a 4-file run, `test_supervisor.py::test_failure_restarts_with_backoff_then_gives_up`
failed with `attempt == 5` while `consecutive_failures == 3`. Hypothesis, not
confirmed: a slow loop iteration lets the `reset_at` timer fire while the
service is reaped-but-still-draining (`spawned is not None`), clearing the
streak — the same load-dependent family as the two flakes already recorded
above. It passes alone and passed both reruns of the identical command. Do not
"fix" it by widening the assertion without reproducing the mechanism first.

Not done deliberately: no stop/restart cascade when a dependency dies later
(Phase-B question), and `platform/rollback.py` still treats only
`("stopped", "failed")` as down — a service parked in `waiting` reads as neither
there. Out of this task's scope; worth a look when rollback is next touched.

## T4.3 failure injection + rollback (2026-09-03)

Built `src/ams/platform/rollback.py`: `rollback(state, store, id, *, cfg, to_sha=None)`
re-stages the target commit, re-provisions, re-reads and re-translates the manifest
**as it was at that commit**, rewrites the declaration and both sidecars, then
stop -> reload -> restart -> health gate, and flips the record
(`sha`/`deployed_sha` = target, `stage`, `rolled_back_from`, `escalated=False`,
`prev_sha=None` on success). One `PlatformRollback` JSONL record on stdout either
way. Runnable as `python -m ams.platform.rollback <id> [--to SHA]`; exit 0 healthy,
1 rolled-back-but-unhealthy, 2 precondition (nothing touched).

Verified: local suite **990 passed / 110 skipped**, ruff clean on the three files.
Remote `scripts/remote-test.sh ams-e2e tests/linux/test_platform_e2e.py` **24 passed
in ~49 s, 3 runs, 3 green**; `tests/test_platform_rollback.py` (32) also green there.
Live evidence from the run logs, not just our bookkeeping: beta `exited signal=9
uptime=0.40s` then `giving up on beta: restart.policy=never`; hog's cgroup moved
`memory.max` 67108864 -> 33554432, OOM-killed twice (`signal=9 uptime=0.26s`), and
the rollback put it back at 67108864 and listening. Live fleet pids captured either
side of a full e2e run: **identical**, 16 services (T4.1's in-flight fleet), harness
active. `/home/harness/{ams-e2e,state/ams-e2e}` and the store's `repos/e2e*.git` +
`src/e2e*` removed.

Two host findings the e2e forced, both recorded in DECISIONS: `run_admin`'s bare
`os.fork()` must not run in a multi-threaded process (a threaded sync deadlocked
`rm`/`mv`/`uv python install` into their own SIGKILL timeouts), and nothing may be
forked before `build_supervisor` claims the delegated cgroup (cgroup v2 EBUSY).

Wiring left for T3.1/T4.4 — two lines in `src/ams/platform/cli.py`:
`from ams.platform.rollback import add_subparser as add_rollback, cmd_rollback`, then
`add_rollback(ops)` in `add_subparser` and `"rollback": cmd_rollback` in
`cmd_platform`'s handler dict. Also one line in `sync.ServiceRecord`:
`rolled_back_from: str | None = None`, without which the field a rollback writes is
dropped the next time the sync loop rewrites the state file.

Follow-up (T4.5's `waiting` status, committed f382790): `rollback.DOWN_STATUSES`
is `{"stopped", "failed", "waiting"}` — a service parked on an unhealthy
`depends_on` has no process, which is all the staging step needs to know, and
treating it as neither up nor down made `_wait_stopped` spin to its deadline and
then warn about a service that was never running. One unit case pins it
(`test_a_service_parked_waiting_on_a_dependency_counts_as_down`: one `status`
call, no warning). Local suite **991 passed / 110 skipped**; remote
`scripts/remote-test.sh ams-e2e tests/linux/test_platform_e2e.py tests/test_platform_rollback.py`
**57 passed**; remote dirs removed again.

## T4.4 docs + Phase-B audit (2026-09-03)

Wired T4.3's leftovers: `ams platform rollback` is now a real subcommand
(`src/ams/platform/cli.py` imports `add_subparser`/`cmd_rollback` from
`rollback.py`, calls `add_rollback(ops)` and routes `"rollback"` in
`cmd_platform`), and `sync.ServiceRecord` gained `rolled_back_from: str | None`
so the field survives the next tick's rewrite. Two tests in a new
`tests/test_platform_cli.py` pin both (`--help` through `ams.cli.main`, and a
state-file round trip). `docs/platform-sidecars.md`'s record table gained
`rolled_back_from` **and** `deployed_sha` (D26's field was never documented) and
`test_platform_sync.py`'s shape assertion was updated to match.

Docs written: `docs/platform.md` (the end-to-end story — layers, on-disk layout,
the nine steps from push to running service, the state machine, secrets, the
gateway, Layer 0, `depends_on`, backup, rollback, the control socket, operator
commands, known gaps), plus rewritten `README.md` and `CLAUDE.md`. Every path and
command named in them was spot-checked with `ls` or `--help`; the only correction
that surfaced was `make_extra_env_for` living in `secrets.py`, not `runtime.py`.

`.claude/state/phase-b-prereqs.md`: 21 checklist items across edge/TLS, data
migration, trust root, secrets and source, lifecycle gaps, capacity, and the
three decisions the user must sign off (D18 scope, D22 fixed ports, D26 shared
prefixes), each with the Phase-A evidence cited by state doc, what is still
required, the risk and the rollback.

Verified: local suite **993 passed / 110 skipped** (one more landed from a
concurrent task while this ran), ruff check + format clean on
`platform/cli.py`, `platform/sync.py` and the new test file. Fleet numbers were
**not** available — `.claude/state/platform-fleet.md` existed as a skeleton with
`<!-- TABLE:FLEET -->` placeholders when this task finished, so README carries
the Layer-0 numbers (n=1, `platform-layer0.md`) and points at the fleet doc for
the rest.

## T4.1 (live) — fleet bring-up + resource report (2026-09-02/03)

Full transcript with every table: `.claude/state/platform-fleet.md`. Decisions
and rejected alternatives: `DECISIONS.md` **D28**.

**20 ams services and 2 static sites run on racknerd** under the live harness,
driven by `ams-platform-sync.timer` (enabled, 60 s) from the local bare mirror at
ref `ams-platform`. 15 services heartbeat into the replica registry with
`last_seen` 9–24 s old, and every one of them answers `GET /health` 200 through
Caddy — including **five subdomain mounts and two static `file_server` mounts,
which had never been exercised outside golden files** (platform-layer0.md §9).

Counts: tier 1 **9 of 11** (`oss`, `secretsservice` down), tier 2 **6 of 8**
(`resume`, `displayservice` down), statics **2 of 2**. All four failures are
credential or manifest gaps, not resources — see the fleet report §3.

**Resource verdict:** the harness cgroup holds all 20 services in
**1 097 969 664 B (1047 MiB)** with **650 MiB still available** and 148 MiB of
swap in use. The 300 MB stop-floor was never reached. Tier-1 `services/*` sit at
45.6–47.8 MiB each (n=9) and tier-2 `apps/*` at 56.5–66.4 MiB (n=6), so Q8's
"~60 MB per uvicorn" held; what Q8 missed was the registry.

**The bring-up's sharpest finding:** the registry OOM-killed itself twice inside
its own cgroup — at the 200M cap (`anon-rss:202988kB`) and again at 320M
(`anon-rss:325416kB`) — while the fleet registered simultaneously, and because
every Layer-1 service registers inside its FastAPI lifespan, each kill took the
whole tier down. Memory tracking the cap looks exactly like a leak, so it was
measured rather than assumed: at a 700M cap the burst peaked at **397 852 672 B
(379.4 MiB)** at t≈30 s and `memory.current` then sat at ~64 MB for five minutes.
Burst, not leak. `_REGISTRY_MEMORY_MAX = "700M"` in `bootstrap.py`; auth stays at
200M.

Also fixed here: `PlatformPolicy` now suppresses the two remaining Caddy start-up
warnings (`admin endpoint disabled`, `exiting; byeee!!`) that platform-layer0.md
§7 classified as noise and `_TLS_MSG_RE` did not match — verified as zero Caddy
escalations from the fixed harness pid, against one per start before.

Both timer properties are proven live: a second consecutive tick is
`unchanged=9 reloaded=False` with a **byte-identical pid list**, and a one-line
commit to `api/apps/timeservice/README.md` produced `unchanged=8`,
`reload: timeservice changed; restarting`, `+0 ~1 -0 =15 errors=0`, and a pid
diff of exactly one line. D26's per-service change detection works end to end.

Two open items this task found and did **not** fix, both recorded in D28:
`_health_gate` escalates every `kind: static` mount once per sha because
`declared` is a static's terminal stage (the fix needs `policy.py` to read the
mount sidecar's `kind`, which is more than the constants this task's scope
allowed); and `resume`/`displayservice` cannot start because they read their DB
path from a code default of `/var/lib/<name>/` that no manifest sets, so the
translator's `/var/lib → <root>/data` rewrite has nothing to rewrite.

`depends_on` (T4.5) landed mid-task and closed the start-ordering gap this
bring-up had already hit twice. Verified over **three** harness restarts (9, 17
and 19 dependents): Layer 0 starts, registry goes healthy, then every dependent
starts on attempt 1 with no crash loop. 19 of 24 declarations carry the line;
the five without are registry/auth/caddy/hello/pyhello, which is correct. Those
restarts also re-measured the registry burst at the 700M cap — 379.4 MiB at 9
registrations, 384.5 and 385.3 MiB at 20 — so the burst is **flat in fleet
size** (n=3) and dominated by fixed start-up cost, not per-registration
allocation. One new contention signal: the registry's health probe timed out
twice during the 20-service start and recovered on its own.

Local suite **999 passed / 110 skipped**, ruff clean. `api/` carries two new
commits on `ams-platform` (the README fixture and its follow-up); nothing pushed
to GitHub.

## 2026-09-02 — health-gate fix: static mounts no longer escalate at `declared`

D28's first open item closed: `PlatformPolicy._health_gate` now reads
`<state>/platform/mounts/<id>.json` and skips the gate when `stage == "declared"`
and the sidecar's `kind == "static"` (absent/malformed sidecar fails safe = still
gates). Local suite **999 passed / 110 skipped** (+6 tests), ruff clean; deploying
to racknerd via `scripts/deploy-racknerd.sh` (see DECISIONS D28 for the live
verification).

## 2026-09-03 — Pools (memory optimisation, user request 「一大堆服务可以合并，只通过 tag 区分」)

Phase: implementation dispatched. Facts in `pool-facts-{api,ams}.md`, plan in
`PLAN-pool.md` (alternative B chosen: one process, one venv, N `uvicorn.Server`s
on N ports; grouping key `pool = "<name>"` in `service.ams.toml`; the tag is the
member's own service id). T0 spike PASSED (n=3) — `spike-pool.md`,
`evidence/pool_spike2.py`. Baseline to beat: fleet cgroup 1105 MiB, 16 Python
processes at 44–74 MiB each; pooled 6 members measured at 66 MiB.

Done: facts, plan, T0. In flight: T1 (schema PORT_NAME_RE), T4 (overlay `pool`),
T5 (gateway `port_owner`), T7 (backup label + policy), T11 (api SDK
`login_redirect` request-derived return_to). Next: planner revision of §4.2/§4.3
→ T2 (runner asset), T3 (translate `build_pool`), T6 (sync grouping) → T8
(`pool adopt`, rollback refusal) → T9 docs/D29 → T10 live cutover on racknerd
with the §7.3 before/after table.

User-decidable defaults taken (user not present): 15-member `core` roster with
displayservice/llmgateway/files/oss standalone; accepted losses §5.9; backup keys
stay per logical service.

## 2026-09-03 — Pools: T1–T9 (T2 confirmed landed; T10 next)

All of T1–T8 are landed and verified by reading the tree, not by report:

- **T0** spike PASSED — `.claude/state/spike-pool.md` (6 members, one loop, RSS
  66 MiB n=3).
- **T1** `schema.PORT_NAME_RE` widened to 32 chars — `tests/test_schema_portnames.py`.
- **T2** the pool runner asset — `src/ams/platform/assets/pool_runner.py` (no
  `__init__.py`), `tests/test_pool_runner_static.py`,
  `tests/linux/test_pool_runner_live.py`, fixture `tests/fixtures/pool-members`.
  Confirmed present on disk this task (was still "not started" as of the last
  PLAN-pool.md status table read).
- **T3** `translate.build_pool`/`pool_member`/`mangle_member`/`PoolMember`/
  `PoolTranslation` — `tests/test_platform_pool_translate.py`, goldens
  `tests/golden/platform/pool/{pool-core.toml,pool-core.pool.json,
  kvservice.mount.json,timeservice.mount.json,kvservice.yaml,timeservice.yaml}`.
- **T4** `static.Overlay.pool`/`overlay_pool` — `tests/test_platform_overlay_pool.py`.
- **T5** `gateway.resolve_ports` `port_owner` — `tests/test_platform_gateway_pool.py`.
- **T6** `sync.py` pool grouping, `ServiceRecord.pool`/`pool_members`, adoption
  guard, two-hop-aware `_phase_materialize`/`_phase_declare`/`_phase_finish` —
  `tests/test_platform_sync_pool.py`.
- **T7** `backup.Target.label`/`discover` pool-aware labeling,
  `policy._pool_suffix` — `tests/test_platform_backup_pool.py`,
  `tests/test_platform_policy_pool.py`.
- **T8** `src/ams/platform/pool.py` (`plan`/`adopt`, two-hop staged move),
  `rollback.py` member refusal + pool re-derivation, `cli.py` `platform pool
  {plan,adopt}` — `tests/test_platform_pool_adopt.py`,
  `tests/linux/test_pool_adopt_live.py`.
- **T11** (api-repo side, separate repo): landed on branch `ams-platform` at
  `f88ebe60` per the fleet dry check in `spike-pool.md` — `SVC_ENDPOINT`
  derived from the request instead of falling back to `""`.
- **T9** (this entry): `docs/platform-pools.md` (new), edits to
  `docs/platform.md`, `docs/manifest-translation.md`,
  `docs/platform-sidecars.md`, `CLAUDE.md`; `.claude/state/DECISIONS.md` D29
  appended.

Local suite **1193 passed / 122 skipped** (`.venv/bin/python -m pytest -q`,
2026-09-03), up from 999/110 before pools. ruff not re-run this task.

Where code and plan disagreed (documented in D29 rather than silently
matching the plan's sketch):
- `cli.format_status` has no header row and no `AGE` column — the plan's §3.4
  mock showed both; the real renderer uses the pre-existing `since=<ts>` field
  per row and adds a `POOL` column only when at least one record carries a
  pool key.
- `PlatformState`'s pool fields are omitted by `as_json` when unset (not
  written as `null`), matching the `deployed_sha` precedent (D26).
- `gateway._phase_gateway` needed **no** code change of its own — `port_owner`
  is entirely `resolve_ports`'s concern, so the phase function is unchanged
  from its pre-pool form.
- A member whose *manifest* fails to translate fails the **whole pool** at
  build time (one `PoolError`, one shared `failed` message for every member —
  `sync._pool_blocked`); a member whose *process* fails at runtime is the
  skip-and-continue case §4.5 describes. The plan's prose did not always keep
  these two failure points distinct.
- Adoption's data move is implemented as a two-hop staged move through a
  harness-owned directory (member uid block → staging → pool uid block), not
  a direct `mv` — `run_admin` maps only one uid block per fork, so a direct
  move across two different service uids cannot work in one admin namespace.

Next: **T10** — live verification on racknerd: the blocking-I/O grep audit,
confirm `secretsservice` starts standalone, `scripts/remote-test.sh` with the
pool Linux tests, deploy, the §7.2 cutover (respecting the ordering
constraint — deploy ams before pushing `pool` overlays to the mirror), and
the §7.3 before/after measurement table in `docs/platform-pools.md`. Three
user-decidable defaults (15-member roster, accepted losses, per-service
backup keys) were taken in the user's absence per PLAN-pool §10 and should be
confirmed before or during the cutover.

## 2026-09-03 — Pools: LIVE on racknerd (T10 done)

`pool-core` runs 15 members in one process. Layer-1 memory 712 → 275 MiB,
all services 866 → 464 MiB, 18 → 5 Python processes, pool 134 MiB, cold start
21 s (n=3; `evidence/pool-{before,after}-2026-09-03.txt`, `pool-migration.md`).
Two adoption bugs fixed live (e873270, 436d86b), two follow-ups (315b9a1:
log-tag parsing, failed-gate hold). Tests 1224 passed / 125 skipped; remote
Linux suite green except the known load-sensitive `test_pids_max_caps_a_fork_storm`.
Legacy member roots + snapshot `/var/lib/ams/pre-pool-data-20260903080517.tgz`
kept on the box for one backup cycle; delete by hand after.
Next: user decisions (roster, R2 creds, api branch push), Phase B unchanged.

## 2026-09-03 — racknerd cleanup + mock deploy on a fresh 1 GB DO droplet

**racknerd residue removed** (user: "顺手把 racknerd 上的残留清掉"): the 15
legacy pooled-member roots and their secret copies, the e2e `hello`/`pyhello`
declarations, uid blocks + ports of those 18 ids (`UidAllocator.release` /
`PortAllocator.release`), `uidmap.json.bak.*`, the test rsync dirs
(`t10-live t2-pool-runner t41 t8-pool-adopt`, `t41*.log`, the stale
`/home/harness/state`), `store/scratch-pool`, and
`/var/lib/ams/pre-pool-data-20260903080517.tgz`. `services/` 1.7 G → 609 M.
One sync tick after: `unchanged=17 failed=2`, nothing recreated, 8 cgroups.

**Mock deploy** (user: "mock deploy to phm with new DO machine … 1GB"):
droplet `platform-mock` (203.0.113.10, `s-1vcpu-1gb`, ssh `phm-mock`,
$6/mo — delete when done). Scripts-only fresh-host bring-up surfaced four
gaps, all fixed and tested (D30): `install-host.sh` needs `libatomic1` (+
`apt-get update`); `layer0.py --no-layer1` (no pilot pair on a fresh host,
and it would collide with `pool-core`); `platform-bootstrap.sh --no-layer1`
passthrough; `sync._phase_finish` registers every identity before any health
gate. Fresh re-run on wiped state: Layer 0 up in 19 s, `pool-core` healthy in
23 s on one attempt, 13/15 members + llmgateway healthy (resume/secretsservice
dead as on racknerd), 15/15 routes 200 via Caddy, second tick 1 s. Record:
`.claude/state/mock-deploy-do.md`, evidence `evidence/mock-do-1gb-2026-09-03.txt`.
Open (D30): standalone services still start before they are registered (one
crash + 20 s backoff each on a fresh host); node 22 and the timer units are
hand steps.

## 2026-09-03 — releases

ams: `v1.0.0` (248d7f3) pushed to the new private repo
`github.com/StevenLi-phoenix/agent-managed-services` (origin, main). api: branch
`ams-platform` pushed to GitHub for the first time and tagged `v2.0.0` at
c8b1fff3 — **not merged to main**; the production deployer ignores tag pushes
and non-main branches, so nothing deployed. Both earlier open items ("git
remote for ams", "api branch unpushed") are closed.

## 2026-09-29/30 — ams 1.1.0: core mode (host the Cordis-based api core)

api `main` (v3.1.0) replaced the Python service fleet with one Node 24
process, `core`, that hot-installs its own plugins. ams 1.1.0 hosts it as
**core mode** (`PLAN-core.md`, DECISIONS D31/D32, `docs/platform-core.md`).
The legacy manifest mode stays, deprecated and untouched.

Landed:
- a9fca88: `platform/{core,corectl,coresync}.py`, `assets/core_plan.mjs`,
  `ams platform core {config import,bootstrap,sync,status,release --rollback,ship}`,
  the managed Node/pnpm toolchain and `provision_tree` (`runtime.py`),
  `run_as_service` + `run_admin(env, cwd)` (`userns.py`), `sources.stage(dest=)`
  / `stage_plain`, `gateway.render_core`, backup byte stores under `bytes/`,
  `deploy/ams-core-sync.{service,timer}`.
- 1c0460e: merge of `security-audit-fixes` (issues #1 #2 #4 #5, docs #6–#12:
  `run_admin` mask / `provisioning_mask`, static symlink refusal, overlay
  env-name gate, yamlsubset hardening).
- Adversarial review of the full diff: 25 findings (correctness, security,
  upstream fit). 24 applied, each test-first; see D32 and CHANGELOG
  [1.1.0]. Main outcomes: verdicts keyed by content key + core release +
  bundle digest (+ fingerprint for `blocked`), transport failures in
  `ship_retry`, held stage/release shas, untrusted steps run as the
  service with a per-service pnpm store, harness-side staging for
  bundle/`current`, `data/artifacts` backed up, a pin check against
  `engines`/`packageManager`, a shutdown budget computed at shutdown.
- Local e2e (macOS, `--no-isolation`, scratch clone of api at f99fb863, node
  24.20.0 / pnpm 11.19.0 managed): all six PLAN-core §6.2 scenarios pass.
  Bootstrap 74 s; no-change tick 0.17 s with nothing written; a one-plugin
  content change ships only that plugin (65 s); a broken plugin is rejected
  and escalated once, not retried, while the old artifact keeps serving;
  a core release costs ≈2 s of `/health`; rollback 3 s and the sha is then
  held. Extras: a crashing release flips back (outage ≈92 s → ≈17 s after the
  fix), and a harness restart reboots all plugins from `core.sqlite`. Two
  fixes came out of it (gate fails fast on a harness-`failed` core; Node's
  trace-warnings hint is INFO). Evidence:
  `evidence/core-e2e-local-2026-09-29.txt`.
- Docs: `docs/platform-core.md` (new), legacy banners on `platform.md`,
  `platform-pools.md`, `manifest-translation.md`, `platform-sidecars.md`;
  `service-declaration.md` runtime pins; README (zh), CLAUDE.md, AGENTS.md,
  CHANGELOG `[1.1.0]` + `[1.0.0]`, DECISIONS D31. `pyproject.toml` 1.1.0.

Tests: **1608 passed / 167 skipped** (`.venv/bin/python -m pytest -q`,
2026-09-30), up from 1231/125 at 1.0.0. No new skips besides Linux-only ones.

Next:
- Commit on main, then the publish steps in PLAN-core §7 (history
  scrub of the pilot secrets and IPs, gitleaks re-scan, force-push, tags
  `v1.0.0` (rewritten) and `v1.1.0`, repo public).
- **Linux live verification of core mode**: no host exists. It needs racknerd
  (or another box) re-provisioned (user decision), then
  `scripts/remote-test.sh` + a live core e2e. None of the isolation code
  (`run_as_service` provisioning, as-service layout, staged renames) has run
  on Linux yet.
- Open follow-ups (D31/D32 Open): the two escalation streams, `SourceMirror.stage`
  and gc as inner root inside the root, legacy provisioning's mask bypass.
