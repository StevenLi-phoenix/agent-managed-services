# The event loop, and what is never allowed in it

The harness is one process with one thread running one `selectors` loop
(`ams.supervisor.Supervisor.run_once`). That is a choice, not an accident, and
it has one obvious cost: anything slow inside the loop delays everything else.
This page lists what runs in the loop, what is kept out of it, and how.

## Why one thread

- **Forking into user namespaces.** `run_admin`, `run_as_service` and the
  isolated spawner are a bare `os.fork()` followed by `unshare`. A forked
  child keeps only the forking thread; a lock another thread held at that
  moment is held forever in the child. ams hit this for real: `rm`, `mv` and
  `uv python install` hung until their SIGKILL timeouts. So **nothing may add
  a thread to a process that forks into a user namespace** -- not a thread
  pool for health checks, not a background logger.
- **Ordering for free.** Pipe reads, `waitpid`, timers and the control socket
  are serviced in one order per iteration. There is no lock in the supervisor
  because there is nothing to lock against.

## What the loop does per iteration

```
select(timeout = earliest actionable deadline, ≤ 0.25 s)
  ├─ readable service pipes  → split lines → DecisionPolicy → log/escalate
  ├─ control socket           → non-blocking accept/read/write (ams ctl)
  ├─ health probe sockets     → advance the probe's state machine
  └─ self-pipe (SIGCHLD/TERM/HUP)
reap (waitpid -1, WNOHANG)    → exits, restarts per policy
timers                         → stop deadlines, backoff restarts, probe
                                 deadlines, new probes, failure-streak resets
finalize                       → close drained pipes, remove cgroups
```

## Health checks are non-blocking

A `tcp` or `http` check is an `ams.health.Probe`: a non-blocking socket and a
small state machine (connect → send `GET` → read the status line) registered
in the loop's own selector, with its deadline in the timers. A `/health`
endpoint that accepts and never answers costs one fd and one timer; every
other service's pipes, restarts and probes carry on. At most one probe per
service is in flight; it is cancelled (unregistered, then closed) whenever the
service stops, exits, restarts or gets a new declaration.

Until 2.0.0 the http probe called `http.client` inline and blocked the loop for
up to `health.timeout_s` per check (`tests/test_health.py` has the regression
test: a silent endpoint with a 2 s timeout used to stall `run_once` for 2.00 s).

The `_poll_timeout` invariant: `select` may only be woken early by a deadline
that the timers will actually act on. A due-but-unactionable deadline (say, the
next probe time while a probe is still in flight) clamps the timeout to 0 and
spins a core at 100 %. Every term in `_poll_timeout` repeats the exact guard of
the timer branch that services it, and the no-spin tests count iterations.

## What is kept out of the loop

| Slow thing | Where it runs instead |
|---|---|
| `git fetch`, staging a tree | `ams platform core sync` -- a separate one-shot process on a 60 s timer (`deploy/ams-core-sync.timer`) |
| `pnpm install`, `uv sync`, builds | the same one-shot, or `ams provision` before `ams run` |
| plugin shipping, release health gates, probation waits | the core-sync one-shot, talking to the harness over the control socket |
| backups (sqlite snapshot, gzip, rclone) | `ams.platform.backup`, its own daily timer |
| anything an agent does | outside the process entirely; it reads `ams escalations` and calls `ams ctl` |

The control socket only ever asks the loop for cheap things: status, start,
stop, restart, kill, reload. A reload re-reads the declarations (a few small
TOML files) and starts or restarts what changed.

## What can still block, and for how long

Bounded, and listed so nobody has to rediscover them:

- **Spawning a service** forks into a user namespace and execs. On the cold
  path (a service root that does not exist yet, or ownership that is wrong)
  `ensure_service_root` runs one `run_admin chown -R` over the root first; the
  warm path costs one `stat` and no fork.
- **Cleaning up after an exit** waits up to 2 s (`CLEANUP_DRAIN_S`) for the
  service's cgroup to empty before removing it; usually it is already empty.
- **Shutdown** stops services in reverse dependency order within one budget
  (45 s, `SHUTDOWN_BUDGET_S`), servicing the loop while it waits.
- **The decision policy** runs per log line; it is regexes and a dict (cause
  dedupe), with lines capped at 64 KiB.

If a new feature needs something slower than these, it belongs in a one-shot
process on a timer, not in the loop -- and never in a thread.
