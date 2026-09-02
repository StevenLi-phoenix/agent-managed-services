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
