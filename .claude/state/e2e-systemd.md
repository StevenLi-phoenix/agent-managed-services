# systemd smoke test on racknerd — 2026-09-02

Harness running as the real `ams-harness.service` unit (not `systemd-run`), one
declared service, verified from a root ssh session. Every command and output
below is real; nothing is reconstructed.

Host: racknerd-b078f4c, Ubuntu 24.04, systemd 255, 1 vCPU / 2 GB, 1 GB swap.
Code at `/home/harness/ams` (rsynced by `scripts/deploy-racknerd.sh`).
State at `/home/harness/store/state` (`AMS_STATE_DIR`).

## Deploy

```
scripts/deploy-racknerd.sh
# rsync -> /home/harness/ams, chown harness:harness,
# install -m 0644 deploy/ams-harness.service /etc/systemd/system/,
# systemctl daemon-reload, systemctl restart ams-harness
```

## The declared service

`/home/harness/store/state/services/hello/service.toml`:

```toml
id = "hello"

[start]
argv = ["/usr/bin/python3", "-m", "http.server", "${PORT_main}", "--bind", "127.0.0.1"]

[ports]
main = 0

[health]
kind = "http"
port = "main"
path = "/"
interval_s = 5.0
timeout_s = 3.0
start_period_s = 10.0

[limits]
memory_max = "64M"
pids_max = 16

[stop]
timeout_s = 5.0

[runtime]
kind = "none"
```

## 1. Unit is active, cgroup root discovered under DelegateSubgroup

`DelegateSubgroup=harness` was never exercised before this run (only
`systemd-run -p Delegate=yes`). `CgroupRoot.discover()` handled it unchanged:
the main pid starts in `<unit cgroup>/harness` and the walk up stops at the unit
cgroup, which is the delegated root. No code change was needed.

```
# systemctl is-active ams-harness
active

# journalctl -u ams-harness -n 50 --no-pager
INFO ams.supervisor: harness is a child subreaper
INFO ams.cli: state dir: /home/harness/store/state
INFO ams.cgroup: delegated cgroup root: /sys/fs/cgroup/system.slice/ams-harness.service
INFO ams.cgroup: controllers enabled at /sys/fs/cgroup/system.slice/ams-harness.service: ['cpu', 'memory', 'pids']
INFO ams.userns: created service root /home/harness/store/state/services/hello/root
INFO ams.supervisor: registered service hello (ports={'main': 20000})
INFO ams.cgroup: limits for hello: {'memory.max': '67108864', 'pids.max': '16'}
INFO ams.isolated: spawned hello pid=1991032 cgroup=/sys/fs/cgroup/system.slice/ams-harness.service/svc-hello block=100000+1024
INFO ams.supervisor: started hello pid=1991032 attempt=1
INFO ams.cli: summary hello status=starting pid=1991032 uptime=0s healthy=None failures=0 ports=main:20000 memory.current=233472
INFO ams.services: [hello:stdout] Serving HTTP on 127.0.0.1 port 20000 (http://127.0.0.1:20000/) ...
INFO ams.supervisor: hello health=ok http 127.0.0.1:20000/ HTTP 200 OK
```

## 2. It serves, as its own subuid, under its declared limits

```
# PORT=$(python3 -c 'import json;print(json.load(open("/home/harness/store/state/state/ports.json"))["ports"]["hello"]["main"])')
20000

# curl -s -o /dev/null -w '%{http_code}\n' 127.0.0.1:20000
200

# ps -o uid,pid,cmd -u 100000
  UID     PID CMD
100000 1991032 /usr/bin/python3 -m http.server 20000 --bind 127.0.0.1

# cat /sys/fs/cgroup/system.slice/ams-harness.service/svc-hello/{memory.max,memory.swap.max,pids.max}
67108864
0
16
```

`memory.swap.max=0` is DECISIONS D12 applied by the spawner: the declaration
asked for a 64 MB ceiling and the box has 1 GB of swap, so `memory.max` alone
would have been a reclaim threshold rather than a ceiling.

## 3. Escalation JSON reaches the unit's stdout

Provoked with a temporary second declaration `boomer` (a python one-liner that
writes `ERROR: boom` to stderr and exits 3, `restart.max_retries = 2`), then
removed again. Lines below are verbatim from `journalctl -o cat`:

```json
{"kind": "LogLine", "service_id": "boomer", "action": "escalate", "reason": "ERROR on stderr", "event": {"service_id": "boomer", "stream": "stderr", "text": "ERROR: boom", "severity": 40, "ts": 1788351542.9414446, "truncated": false}, "attempt": 1, "consecutive_failures": 0}
{"kind": "LogLine", "service_id": "boomer", "action": "escalate", "reason": "ERROR on stderr", "event": {"service_id": "boomer", "stream": "stderr", "text": "ERROR: boom", "severity": 40, "ts": 1788351543.4320161, "truncated": false}, "attempt": 2, "consecutive_failures": 1}
{"kind": "LogLine", "service_id": "boomer", "action": "stop", "reason": "2 consecutive failures >= max_retries", "event": {"service_id": "boomer", "stream": "stderr", "text": "giving up on boomer: 2 consecutive failures >= max_retries", "severity": 50, "ts": 1788351543.6414328, "truncated": false}, "attempt": 2, "consecutive_failures": 2}
```

Two crash escalations plus the terminal one, restarted once and then given up on
at `max_retries` — the full contract, through systemd, with no agent attached.

Afterwards `boomer` was removed: its service dir deleted and its `ports.json` /
`uidmap.json` entries dropped while the unit was stopped, so the persisted state
holds only `hello`.

## 4. Stop is graceful and leaves nothing behind

```
# /usr/bin/time -f 'stop took %e s' systemctl stop ams-harness
stop took 0.10 s          # TimeoutStopSec=30 never came close

# systemctl is-active ams-harness
inactive

# ps -o uid,pid,cmd -u 100000
  UID     PID CMD          # no rows

# ls /sys/fs/cgroup/system.slice | grep -E 'run-|svc-'
(none)
```

Journal for the same stop:

```
INFO ams.supervisor: received signal 15; stopping
INFO ams.supervisor: shutting down
INFO ams.supervisor: sent SIGTERM to hello pid=1991106
INFO ams.supervisor: hello exited pid=1991106 code=None signal=15 uptime=18.21s failures=0
INFO ams.supervisor: ignoring policy RESTART for hello: operator stopped it
INFO ams.cli: stopped
systemd[1]: ams-harness.service: Deactivated successfully.
```

`Deactivated successfully` = exit 0. The policy's RESTART was correctly ignored
because the operator, not a crash, brought the service down.

## 5. Restart keeps the service's port and uid block

```
# /usr/bin/time -f 'start took %e s' systemctl start ams-harness
start took 0.03 s

# systemctl is-active ams-harness
active

# curl -s -o /dev/null -w '%{http_code}\n' 127.0.0.1:20000
200

# ps -o uid,pid,cmd -u 100000
  UID     PID CMD
100000 1991173 /usr/bin/python3 -m http.server 20000 --bind 127.0.0.1
```

Same port (20000) and same host uid (100000) as before the stop: both come from
the persisted allocator state, not from re-carving.

## 6. Left enabled and running

```
# systemctl enable ams-harness
Created symlink /etc/systemd/system/multi-user.target.wants/ams-harness.service -> /etc/systemd/system/ams-harness.service.

# systemctl is-enabled ams-harness ; systemctl is-active ams-harness
enabled
active

# find /sys/fs/cgroup/system.slice/ams-harness.service -maxdepth 1 -type d
/sys/fs/cgroup/system.slice/ams-harness.service
/sys/fs/cgroup/system.slice/ams-harness.service/svc-hello
/sys/fs/cgroup/system.slice/ams-harness.service/harness

# ps -o uid,pid,ppid,cmd -u 100000
  UID     PID    PPID CMD
100000 1991873 1991865 /usr/bin/python3 -m http.server 20000 --bind 127.0.0.1
```

The only uid-100000 process on the box is the live `hello` service, and the only
`svc-` cgroup is its own, inside the live unit's tree. The pre-existing docker
container at uid 1000 (`node kd-web/server.mjs`, pid 1197451) was not touched.

## Two bugs this run caught that no unit test would have

1. **`ams check-host` checked the wrong user.** `cmd_check_host` called
   `hostcheck.main()` with no argv, and `hostcheck.main` falls back to
   `sys.argv[1:]`, which under `python -m ams check-host` is `["check-host"]` —
   read as the username whose `/etc/subuid` range to look up. It reported
   `no range for 'check-host' in /etc/subuid` on a perfectly configured host.
   Fixed by passing `[]` explicitly.

2. **A service could not resolve its own workdir by path.** The child inherits
   its cwd, which needs no traversal rights, but `python -m http.server` calls
   `os.getcwd()` and then stats the result — and `StateDir.ensure()` makes
   `services/` 0750, harness-owned, which the service uid can neither own nor
   join by group. Every request returned 404 with the directory right there.
   `ams.cli._ensure_traversable` now adds `o+x` (never `o+r`) to the harness-owned
   ancestors of each service root. Consequence recorded in PROGRESS.md: a service
   can now traverse to a sibling root whose id it guesses, and service roots are
   0755, so a sibling's files are readable. Closing that needs the per-service
   directory group-owned by the service's mapped gid, which is a state-layout
   change rather than a CLI one.

## Reproducing

```
scripts/deploy-racknerd.sh          # rsync + install unit + restart
ssh racknerd 'systemctl is-active ams-harness'
ssh racknerd 'journalctl -u ams-harness -n 50 --no-pager'
scripts/remote-test.sh ams-integ tests/linux    # 43 passed
```

---

# Round 2 — review fixes + live `ams provision` (2026-09-02, later)

Four items from `.claude/state/review-1.md` folded in, redeployed, and the
provisioning path verified live (it was the one thing my first report listed as
unverified).

## What changed

- `isolated.py` — the spawn-failure path closed `out_w`/`err_w` in `except` and
  again in `finally`, with `svc_cg.kill()/wait_empty()/remove()` opening cgroup
  control files in between, so the second close could land on a recycled fd
  number. `_close` swallows the EBADF that would have revealed it. Now
  `except`/`else`, each fd closed exactly once.
- `userns.py ensure_service_root` — ran `chown -R` over the whole root on every
  spawn, which is O(files) once a `.venv` or `node_modules` lives there. Now one
  `stat`: chown only when the root was just created, a subdir was just created,
  or the ownership is actually wrong. The warm restart path forks nothing.
- `cli.py` — `harness_user()` resolves from `pwd.getpwuid(os.getuid())` instead
  of `$USER`/`$LOGNAME`; `PortAllocator` moved inside the guarded block so a
  corrupt `ports.json`/`uidmap.json` (`ams.state.StateCorrupt`, `ValueError`)
  becomes an actionable exit 2 rather than a traceback out of a systemd unit.
- `cli.py cmd_provision` — no longer builds a spawner just to reach the uid
  allocator. Provisioning needs each service's identity and the admin
  namespace, but no cgroup, so `ams provision` now runs from an ordinary shell
  next to an already-running harness. That is how it is actually invoked.

Provisioning was already outside the loop and stays there: `_provision_one` is
called from `_register` (before `start_all`) and from `cmd_provision`, never
from `extra_env_for`, whose `runtime.runtime_env` is documented and verified
pure. `tests/test_cli.py::test_provisioning_is_never_reachable_from_the_supervisor_loop`
now pins that by making `runtime.provision` raise and then driving the loop.

## `ams provision` on the box

A `pyhello` service (`runtime.kind = "uv"`, `packages = ["six"]`) had been added
to the production state dir by another agent and was crash-looping, because
nothing had built its venv — the harness correctly escalated the terminal state:

```
{"kind": "LogLine", "service_id": "pyhello", "action": "stop", "reason": "5 consecutive failures >= max_retries", "event": {... "text": "spawn failed: SpawnError: pyhello: 'python' not found on PATH=/home/harness/store/state/services/pyhello/root/.venv/bin:..."} ...}
```

Provisioned from a plain root ssh, dropping to `harness`, no delegated cgroup:

```
# su -s /bin/bash harness -c 'cd /home/harness/ams && \
    AMS_STATE_DIR=/home/harness/store/state AMS_STORE_DIR=/home/harness/store \
    PYTHONPATH=/home/harness/ams/src HOME=/home/harness \
    /home/harness/venv/bin/python3 -m ams provision pyhello'
INFO ams.runtime: pyhello: uv python install: uv python install -q 3.12 -> rc=0 in 0.3s
INFO ams.runtime: pyhello: uv venv: uv venv -q --python 3.12 .../pyhello/root/.venv -> rc=0 in 0.1s
INFO ams.runtime: pyhello: uv pip install: uv pip install -q --python .../pyhello/root/.venv/bin/python six -> rc=0 in 1.0s
INFO ams.runtime: pyhello: chown to service: chown -R 1000:1000 .../pyhello/root -> rc=0 in 0.0s
INFO ams.cli: provisioned pyhello (uv): RuntimeEnv(extra_env={'VIRTUAL_ENV': '.../pyhello/root/.venv'}, path_prepend=('.../pyhello/root/.venv/bin',)) (log: /home/harness/store/state/logs/pyhello-provision.log)
OK pyhello (uv)
exit=0
```

## Two services, two identities, both healthy

```
# systemctl restart ams-harness && sleep 10 && systemctl is-active ams-harness
active

# cat /home/harness/store/state/state/ports.json
{"ports": {"hello": {"main": 20000}, "pyhello": {"main": 20001}}, "version": 1}

# curl 127.0.0.1:20000 -> 200
# curl 127.0.0.1:20001 -> 200

# ps -eo uid,pid,cmd | awk '$1>=100000'
  UID     PID CMD
100000 1993628 /usr/bin/python3 -m http.server 20000 --bind 127.0.0.1
101024 1993631 python -c import six, http.server, socketserver, os; print("six", ...)

# journalctl -u ams-harness -o cat | grep 'health=\|six'
INFO ams.services: [pyhello:stdout] six 1.17.0 uid 1000
INFO ams.supervisor: hello   health=ok http 127.0.0.1:20000/ HTTP 200 OK
INFO ams.supervisor: pyhello health=ok tcp 127.0.0.1:20001 connected

# find /sys/fs/cgroup/system.slice/ams-harness.service -maxdepth 1 -type d
/sys/fs/cgroup/system.slice/ams-harness.service
/sys/fs/cgroup/system.slice/ams-harness.service/svc-hello
/sys/fs/cgroup/system.slice/ams-harness.service/svc-pyhello
/sys/fs/cgroup/system.slice/ams-harness.service/harness
```

Two services on adjacent subuid blocks (100000 and 101024), each in its own
cgroup, `pyhello` running its own uv-built interpreter and reporting inner uid
1000. Nothing stray outside the unit's tree.

Local: 229 passed / 45 skipped, ruff clean. Remote: `scripts/remote-test.sh
ams-integ tests/linux` → 45 passed.

## Caveat on the deployed tree

`supervisor.py` and `health.py` on the box are from before the "sup" agent's
busy-loop fixes, and `spawn.py` from before the lead's fd-leak fix. The live
`hello`/`pyhello` do not trigger either (both die on the first SIGTERM; stop
measured at 0.10 s), but re-run `scripts/deploy-racknerd.sh` once those land.

---

# Round 3 — `ams provision` without a delegated cgroup (2026-09-02, later still)

The lead hit `ams provision` exiting 2 with `CgroupUnavailable: cgroup
/sys/fs/cgroup/user.slice/user-0.slice/session-*.scope is not delegated` from
`su -l harness`. Root cause: `cmd_provision` built the isolated spawner purely
to reach the uid allocator, and `make_isolated_spawner` calls
`CgroupRoot.discover()`. Provisioning runs no service, so it needs no cgroup —
only the uid block and the admin namespace.

Fixed in round 2 (`cmd_provision` now calls `cli.uid_allocator(state)` directly);
this round adds the regression test and re-runs the lead's exact chain against
the deployed build.

## The test

`tests/test_cli.py::test_provision_never_needs_a_delegated_cgroup` poisons
`ams.cgroup.CgroupRoot.discover` with an `AssertionError` and asserts the whole
provisioning chain still reaches `runtime.provision` and exits 0. Confirmed to
be a real guard, not a vacuous pass — reintroducing the bug fails it:

```
# with `uids = uid_allocator(state)` replaced by `_make_spawner(state, isolation=True)`
E       AssertionError: ams provision must not touch ams.cgroup
1 failed in 0.06s
# restored
1 passed in 0.04s
```

`AssertionError` is deliberately outside `cmd_provision`'s
`except (OSError, RuntimeError, ValueError)`, so it cannot be swallowed.

## The lead's chain, verbatim, on racknerd

```
# su -l harness -c 'cd /home/harness/ams && PYTHONPATH=src \
    AMS_STATE_DIR=/home/harness/store/state AMS_STORE_DIR=/home/harness/store \
    /home/harness/venv/bin/python3 -m ams provision pyhello'
INFO ams.runtime: pyhello: uv python install: uv python install -q 3.12 -> rc=0 in 0.2s
INFO ams.runtime: pyhello: reusing existing venv .../pyhello/root/.venv
INFO ams.runtime: pyhello: uv pip install: ... six -> rc=0 in 0.0s
INFO ams.runtime: pyhello: chown to service: chown -R 1000:1000 .../pyhello/root -> rc=0 in 0.0s
OK pyhello (uv)
provision exit=0
```

Idempotent on the second run: the venv is reused rather than rebuilt.

```
# ls -ldn .../pyhello/root .../pyhello/root/.venv .../pyhello/root/.venv/bin/python
drwxr-xr-x 3 101024 101024  19 .../pyhello/root
drwxrwxr-x 4 101024 101024 110 .../pyhello/root/.venv
lrwxrwxrwx 1 101024 101024  71 .../pyhello/root/.venv/bin/python -> /home/harness/store/python/cpython-3.12-linux-x86_64-gnu/bin/python3.12
```

uid 101024 = the second 1024-wide block, as predicted. The interpreter is a
symlink into the shared uv python store (D8), so the venv costs almost nothing.

```
# systemctl restart ams-harness && sleep 12 && systemctl is-active ams-harness
active

# cat /home/harness/store/state/state/ports.json
{"ports": {"hello": {"main": 20000}, "pyhello": {"main": 20001}}, "version": 1}

# curl -s -o /dev/null -w '%{http_code}\n' 127.0.0.1:20001
200

# journalctl -u ams-harness -o cat | grep 'six \|health='
INFO ams.services: [pyhello:stdout] six 1.17.0 uid 1000
INFO ams.supervisor: hello   health=ok http 127.0.0.1:20000/ HTTP 200 OK
INFO ams.supervisor: pyhello health=ok tcp 127.0.0.1:20001 connected

# ps -eo uid,pid,cmd | awk '$1>=100000'
  UID     PID CMD
100000 1995080 /usr/bin/python3 -m http.server 20000 --bind 127.0.0.1
101024 1995083 python -c import six, http.server, socketserver, os; print("six", ...)
```

`six 1.17.0 uid 1000` is the whole stack in one line: the service found the
package from its own provisioned venv, and sees itself as inner uid 1000 while
the host sees 101024. Both services left running.

Local: 230 passed / 45 skipped, ruff clean.

## Noted, not a bug: health-probe log noise

`hello`'s http health probe makes `http.server` log one request line per
interval, which arrives as an INFO `ams.services` line:

```
INFO ams.services: [hello:stderr] 127.0.0.1 - - [02/Sep/2026 12:19:12] "GET / HTTP/1.1" 200 -
```

Correct behaviour — every byte a service writes goes through the policy — but at
`interval_s = 5` that is 720 lines an hour per probed service, and the harness's
own probe is the only client. A real `DecisionPolicy` should suppress log lines
a service emits in response to the harness's own health probe. Recorded in
PROGRESS.md rather than special-cased in `DefaultPolicy`, because the general
shape (suppress self-inflicted traffic) is a policy decision, not a parser one.

## 2026-09-02 — session 2: control socket + hot reload live on racknerd (D17)

Deployed with `scripts/deploy-racknerd.sh`. Unit now carries
`ExecReload=/bin/kill -HUP $MAINPID`, so `systemctl reload ams-harness` rescans
the declarations instead of restarting (which `KillMode=control-group` turns
into an outage for every service).

### The socket exists, is private, and answers as `harness`

```
# ls -l /home/harness/store/state/control.sock
srw------- 1 harness harness 0 Sep  2 16:39 /home/harness/store/state/control.sock

$ ams ctl ping
{ "ok": true, "pid": 2015503, "pong": true }        # rc=0
```

`ams ctl status` returned all four services `running`/`healthy: true` with their
cgroup paths and ports (hello 20000, pyhello 20001, kvservice 20002,
timeservice 20003).

### Adding a declaration does not touch the other four

A throwaway `reloadtest` (`python -m http.server ${PORT_main}`, `runtime.kind =
"none"`, http health) was written into `services/reloadtest/service.toml`, then
`systemctl reload ams-harness`:

```
BEFORE: 100000 2015505 | 102048 2015508 | 101024 2015511 | 103072 2015514
AFTER:  100000 2015505 | 102048 2015508 | 101024 2015511 | 103072 2015514 | 104096 2015596
```

Every pre-existing pid is byte-identical across the reload; only the new service
appeared. Journal:

```
INFO ams.supervisor: received SIGHUP; reload requested
INFO ams.userns: created service root .../services/reloadtest/root
INFO ams.supervisor: registered service reloadtest (ports={'main': 20004})
INFO ams.isolated: spawned reloadtest pid=2015596 cgroup=.../svc-reloadtest block=104096+1024
INFO ams.reload: reload: +1 ~0 -0 =4 errors=0
INFO ams.supervisor: reload complete: {'added': ['reloadtest'], 'removed': [], 'changed': [], 'unchanged': 4, 'errors': {}}
INFO ams.supervisor: reloadtest health=ok http 127.0.0.1:20004/ HTTP 200 OK
```

A new service therefore gets its own uid block (104096), its own cgroup, its own
auto-allocated port, and a real health transition — through the same
`ams.cli._register` path startup uses — with the harness never restarting.

### Per-service restart from outside

```
$ ams ctl restart timeservice        # rc=0
BEFORE: 100000 2015505 | 102048 2015508 | 101024 2015511 | 103072 2015514 | 104096 2015596
AFTER:  100000 2015505 | 102048 2015508 | 101024 2015511 | 103072 2015646 | 104096 2015596
```

Only timeservice's pid moved (2015514 -> 2015646), same uid block 103072, same
port 20003. This is the D15 gap-2 blast radius fixed: the api deployer's
per-service restart is now matched.

### Removing a declaration releases the port and the cgroup

`rm services/reloadtest/service.toml` then `ams ctl reload`:

```
{ "ok": true, "reload": { "added": [], "changed": [], "errors": {},
                          "removed": ["reloadtest"], "unchanged": 4 } }

INFO ams.reload: reload: reloadtest is gone from disk; stopping it
INFO ams.supervisor: sent SIGTERM to reloadtest pid=2015596
INFO ams.supervisor: reloadtest exited pid=2015596 code=None signal=15 uptime=38.00s
INFO ams.supervisor: removed service reloadtest
INFO ams.reload: reload: removed reloadtest (ports released, uid block kept)
```

Elapsed from `reload` to `removed service`: 13 ms (2015596 was a well-behaved
SIGTERM handler). Verified afterwards:

- `svc-reloadtest` is gone from `/sys/fs/cgroup/system.slice/ams-harness.service/`
  (the other four dirs remain).
- `state/ports.json` no longer lists `reloadtest`; 20004 is back in the pool.
- `ams ctl status` lists exactly hello, kvservice, pyhello, timeservice, all
  `running` and `healthy: true`.

**Port release on removal: yes. Uid block release: no** (`uidmap.json` still
carries `reloadtest -> 104096`). The block is not released because the service
root on disk is still owned by those uids; handing 104096 to a different service
later would give it ownership of the leftover tree. Removing the root *and* the
block belongs to a future `ams rm <id>`, not to a reload triggered by a file
disappearing. Cost of the current behaviour: a removed service permanently
consumes 1 of the 64 blocks in the subuid range.

### Final state

```
# systemctl is-active ams-harness  ->  active
port 20000 -> 200   port 20001 -> 200   port 20002 -> 401 (auth-gated, expected)
port 20003 -> 200   ( /health -> {"ok":true} )
escalations in the last 5 minutes: 0
```

All four services left running. `/home/harness/ams-ctl` (the remote test tree)
and the throwaway `services/reloadtest/` directory were removed.

### Noted, not fixed: an operator stop still logs at ERROR

```
ERROR ams.supervisor: reloadtest exited code=None signal=15 uptime=38.00s
INFO  ams.supervisor: ignoring policy RESTART for reloadtest: operator stopped it
```

Already recorded in PROGRESS.md before this session; hot reload makes it routine
rather than rare, because every removal and every changed declaration produces
one. `severity_of(ServiceExited)` should consider `desired`/`restart_pending`.

### Redeploy after the `start()` guard fix (16:48:50 UTC)

`supervisor.py` (env hook moved inside the spawn guard) and `reload.py`
(`secrets_missing` in the summary) changed, so the box was redeployed and
re-verified. `ams ctl reload` is idempotent on the live declarations and now
carries the new field:

```
{ "ok": true, "reload": { "added": [], "changed": [], "errors": {},
                          "removed": [], "secrets_missing": [], "unchanged": 4 } }
```

All four services `running`/`healthy: true`; ports 20000 -> 200, 20001 -> 200,
20002 -> 401 (auth-gated, expected), 20003 -> 200; unit active. Left running.

### Final boot re-verification (16:51:27 UTC, after the secrets agent's redeploy)

The secrets agent redeployed for its own `warn_missing_secrets` guard, so the
control channel was re-exercised on that boot. All of `control.py`, `reload.py`,
`supervisor.py`, `cli.py` and the unit sha256-match the local tree.

```
ams ctl ping    -> {"ok": true, "pid": 2019002, "pong": true}

ams ctl restart timeservice
BEFORE: 100000 2019004 | 102048 2019007 | 101024 2019010 | 103072 2019013
AFTER:  100000 2019004 | 102048 2019007 | 101024 2019010 | 103072 2019210

ams ctl reload  -> {"added": [], "changed": [], "errors": {}, "removed": [],
                    "secrets_missing": [], "unchanged": 4}
```

Only timeservice's pid moved; the other three are byte-identical across a
restart issued over the socket, on the boot that carries the `start()` guard
fix. After settling, all four are `running`/`healthy: true`; 20000 -> 200,
20001 -> 200, 20002 -> 401 (auth-gated, expected), 20003 -> 200. Unit active,
`NRestarts=0`, zero escalations in the window.

`/home/harness/ams-ctl` and `/home/harness/state/ams-ctl` removed (the remote
test run after the `start()` fix had recreated them). `ams-integ` belongs to
another agent and was left alone. All four services left running.
