"""The supervision loop.

One process, one thread, one ``selectors`` loop. Services are direct children
of this process; their stdout/stderr are pipes we hold. Nothing goes through
journald, so every byte a service writes passes through
``DecisionPolicy.decide`` before it is logged, dropped or escalated.

Layering (see DECISIONS.md D5): ``init(systemd) -> harness -> service``. On
Linux the harness declares itself a child subreaper so orphaned grandchildren
reparent here instead of to pid 1; ``waitpid(-1)`` then also returns pids that
are not service main pids, which become ``OrphanReaped`` events. macOS has no
subreaper and the loop must not care.

Policy vs. operator intent: every event goes through the policy, but explicit
lifecycle calls win. ``stop()`` sets ``desired="down"``, and a policy asking to
RESTART a service the operator stopped is ignored (logged). ``restart()`` sets
``restart_pending``, and the resulting expected exit is not counted as a
failure. The policy decides about *unexpected* things.
"""

from __future__ import annotations

import errno
import logging
import math
import os
import selectors
import signal
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ams.decision import (
    Action,
    Decision,
    DecisionPolicy,
    DefaultPolicy,
    Escalation,
    NullEscalation,
    ServiceContext,
)
from ams.events import (
    MAX_LINE_BYTES,
    Event,
    HealthChanged,
    LogLine,
    OrphanReaped,
    ServiceExited,
    ServiceStarted,
    Severity,
    Stream,
    severity_of,
)
from ams.health import HealthMonitor
from ams.schema import ServiceDecl, StartSpec
from ams.spawn import SpawnedService, Spawner, SpawnRequest

log = logging.getLogger("ams.supervisor")
service_log = logging.getLogger("ams.services")

Status = Literal["stopped", "starting", "running", "stopping", "failed", "backoff"]
# Runtime layer hook: decl -> (extra env vars, PATH entries to prepend).
ExtraEnvFn = Callable[[ServiceDecl], tuple[dict[str, str], tuple[str, ...]]]
STREAMS: tuple[Stream, ...] = ("stdout", "stderr")

READ_CHUNK = 65536
# A service must stay up this long (or report healthy) before its failure streak
# is forgiven. Overridable per Supervisor for tests.
RESET_WINDOW_MIN_S = 10.0
# After the main pid is reaped we still drain the pipes; a killed grandchild can
# hold the write end open forever, so give up after this and force-close.
EOF_GRACE_S = 2.0
# Longest a run_forever iteration may block; bounds reaction time to a SIGCHLD
# we did not get a wakeup byte for.
LOOP_TIMEOUT_S = 0.25
# Largest exponent used for exponential backoff; 2**1024 overflows float().
MAX_BACKOFF_SHIFT = 30

_SELF_PIPE = "self-pipe"

# SIGHUP means "re-read the declarations" (D17). It does not exist on Windows,
# which the harness does not target, but the getattr keeps this module importable
# there for anyone reading the code on one.
_SIGHUP: signal.Signals | None = getattr(signal, "SIGHUP", None)
_WATCHED_SIGNALS: tuple[signal.Signals, ...] = tuple(
    s for s in (signal.SIGTERM, signal.SIGINT, signal.SIGCHLD, _SIGHUP) if s is not None
)


@dataclass(frozen=True)
class _FdCallback:
    """Selector payload for an fd owned by something other than the supervisor.

    Wrapped in a dataclass rather than stored bare so :meth:`Supervisor.run_once`
    can tell it apart from the ``(service_id, stream)`` tuples that pipe reads
    use, without control-channel code ever touching ``self._sel``.
    """

    fn: Callable[[int], None]
    name: str = ""


# Orphans belong to no declaration, but the policy contract wants a context.
_ORPHAN_DECL = ServiceDecl(id="orphan", start=StartSpec(argv=("<orphan>",)))
_ORPHAN_CTX = ServiceContext(_ORPHAN_DECL)


@dataclass
class ServiceState:
    """Everything the loop knows about one service. Mutable by design."""

    decl: ServiceDecl
    root: Path
    ports: dict[str, int] = field(default_factory=dict)
    spawned: SpawnedService | None = None
    status: Status = "stopped"
    attempt: int = 0
    consecutive_failures: int = 0
    started_at: float | None = None
    healthy: bool | None = None
    desired: Literal["up", "down"] = "down"
    buffers: dict[str, bytearray] = field(
        default_factory=lambda: {"stdout": bytearray(), "stderr": bytearray()}
    )
    stop_deadline: float | None = None
    restart_at: float | None = None
    eof: set[str] = field(default_factory=set)

    monitor: HealthMonitor | None = None
    exited_at: float | None = None
    reaped: bool = False
    restart_pending: bool = False
    reset_at: float | None = None  # monotonic time at which the failure streak clears
    last_exit_failed: bool = False  # did the most recent exit count as a failed run?

    @property
    def id(self) -> str:
        return self.decl.id

    @property
    def pid(self) -> int | None:
        return self.spawned.pid if self.spawned else None

    def uptime(self, now: float) -> float | None:
        return None if self.started_at is None else now - self.started_at


class Supervisor:
    """Owns the service table, the selector and the timers.

    Not thread-safe: drive it from one thread via :meth:`run_once` or
    :meth:`run_forever`.
    """

    def __init__(
        self,
        spawner: Spawner,
        policy: DecisionPolicy | None = None,
        escalation: Escalation | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        extra_env_for: ExtraEnvFn | None = None,
        reset_window_min_s: float = RESET_WINDOW_MIN_S,
        eof_grace_s: float = EOF_GRACE_S,
    ) -> None:
        self.spawner = spawner
        self.policy: DecisionPolicy = policy if policy is not None else DefaultPolicy()
        self.escalation: Escalation = escalation if escalation is not None else NullEscalation()
        self._clock = clock
        self._extra_env_for = extra_env_for or (lambda _decl: ({}, ()))
        self._reset_window_min_s = reset_window_min_s
        self._eof_grace_s = eof_grace_s

        self.services: dict[str, ServiceState] = {}
        self._by_pid: dict[int, str] = {}
        self._sel = selectors.DefaultSelector()
        self._registered: set[int] = set()
        self._pending: list[Event] = []
        self._wakeup_r: int | None = None
        self._stop_requested = False
        self._reload_requested = False

    # ----------------------------------------------------------------- table

    def add(self, decl: ServiceDecl, root: Path, ports: Mapping[str, int]) -> ServiceState:
        if decl.id in self.services:
            raise KeyError(f"service {decl.id!r} already registered")
        st = ServiceState(decl=decl, root=Path(root), ports=dict(ports))
        st.monitor = HealthMonitor(decl.health, st.ports, clock=self._clock)
        self.services[decl.id] = st
        log.info("registered service %s (ports=%s)", decl.id, st.ports)
        return st

    def remove(self, service_id: str) -> None:
        st = self._get(service_id)
        if st.spawned is not None or st.status in ("starting", "running", "stopping"):
            raise RuntimeError(f"service {service_id!r} is not stopped")
        del self.services[service_id]
        log.info("removed service %s", service_id)

    def replace_decl(
        self,
        service_id: str,
        decl: ServiceDecl,
        ports: Mapping[str, int],
        *,
        reset_failures: bool = True,
    ) -> ServiceState:
        """Swap a registered service's declaration and ports in place (hot reload).

        The *running* process keeps the old declaration -- argv, env, limits and
        the uid map are all fixed at spawn time -- so the caller must restart the
        service for the new declaration to take effect. Only the health monitor
        is rebuilt here, because it is pure supervisor-side state and a stale one
        would probe the previous port.

        ``reset_failures`` clears the crash streak by default: a changed
        declaration is the operator's fix, and carrying the old streak forward
        would let a service that already burned ``max_retries`` give up again
        after one attempt on brand-new code.
        """
        st = self._get(service_id)
        if decl.id != service_id:
            raise ValueError(f"declaration id {decl.id!r} does not match {service_id!r}")
        st.decl = decl
        st.ports = dict(ports)
        st.monitor = HealthMonitor(decl.health, st.ports, clock=self._clock)
        if st.spawned is None:
            st.monitor.stop()
        else:
            # Keep probing the *running* process: it still serves the old ports,
            # and _poll_timeout folds in monitor.next_due for a live service.
            st.monitor.start(self._clock())
        if reset_failures:
            st.consecutive_failures = 0
            st.reset_at = None
        log.info("replaced declaration for %s (ports=%s)", service_id, st.ports)
        return st

    def _get(self, service_id: str) -> ServiceState:
        try:
            return self.services[service_id]
        except KeyError:
            raise KeyError(f"unknown service {service_id!r}") from None

    # ------------------------------------------------------------- lifecycle

    def start(self, service_id: str) -> None:
        """Spawn the service.

        Raises ``RuntimeError`` if a process from a previous run is still
        alive: replacing it here would drop it from ``_by_pid``, leave it
        running unsupervised, and report its eventual exit as an orphan. Use
        ``restart()`` (or ``stop()`` then ``start()``) instead.
        """
        st = self._get(service_id)
        if st.spawned is not None and not st.reaped:
            raise RuntimeError(
                f"service {service_id!r} is still running (pid={st.spawned.pid}); "
                "call restart() or stop() first"
            )
        st.desired = "up"
        st.restart_at = None
        if st.spawned is not None:
            # Reaped but not fully drained: finalize now so we never leak the
            # old pipes across a restart. The process itself is already gone.
            self._finalize(st, force=True)
        extra_env, path_prepend = self._extra_env_for(st.decl)
        req = SpawnRequest(
            st.decl, st.root, st.ports, extra_env=extra_env, path_prepend=path_prepend
        )
        st.attempt += 1
        try:
            svc = self.spawner.spawn(req)
        except Exception as e:  # spawner failures must never kill the loop
            self._on_spawn_failure(st, e)
            return
        now = self._clock()
        st.spawned = svc
        st.started_at = now
        st.exited_at = None
        st.reaped = False
        st.restart_pending = False
        st.healthy = None
        st.eof = set()
        st.stop_deadline = None
        st.reset_at = now + max(self._reset_window_min_s, st.decl.health.start_period_s)
        for stream in STREAMS:
            st.buffers[stream].clear()
        self._by_pid[svc.pid] = st.id
        self._register(svc.stdout_fd, (st.id, "stdout"))
        self._register(svc.stderr_fd, (st.id, "stderr"))
        st.status = "starting" if st.decl.health.kind != "none" else "running"
        if st.monitor is not None:
            st.monitor.start(now)
        log.info("started %s pid=%d attempt=%d", st.id, svc.pid, st.attempt)
        self._emit(ServiceStarted(st.id, svc.pid, st.attempt), st)

    def stop(self, service_id: str, *, for_restart: bool = False) -> None:
        """Graceful stop: ``stop.signal`` to the main pid, then kill_tree on timeout."""
        st = self._get(service_id)
        st.desired = "down" if not for_restart else "up"
        st.restart_pending = for_restart
        st.restart_at = None
        # A reaped-but-undrained service is already dead; signalling its pid
        # would hit whatever process inherited that number.
        if st.spawned is None or st.reaped:
            if st.spawned is None:
                st.status = "stopped" if not for_restart else st.status
            if st.monitor is not None:
                st.monitor.stop()
            return
        if st.status == "stopping":
            return
        st.status = "stopping"
        st.stop_deadline = self._clock() + st.decl.stop.timeout_s
        # No health probe applies to a process we are tearing down; leaving the
        # timer due would keep _poll_timeout at 0 for the whole stop window.
        if st.monitor is not None:
            st.monitor.stop()
        signum = st.decl.stop.signum
        try:
            os.kill(st.spawned.pid, signum)
            log.info("sent %s to %s pid=%d", st.decl.stop.signal, st.id, st.spawned.pid)
        except ProcessLookupError:
            log.debug("%s pid=%d already gone", st.id, st.spawned.pid)
        except OSError as e:
            log.warning("could not signal %s pid=%d: %s", st.id, st.spawned.pid, e)

    def restart(self, service_id: str) -> None:
        """Stop-then-start a live service, or start one that is already down.

        ``st.reaped`` counts as down even though ``spawned`` is still set: in
        that window the process is gone and only the pipes are still draining,
        so ``stop()`` correctly declines to signal a dead pid -- and then nothing
        would ever restart it, leaving the service down with ``desired="up"``
        and no timer. ``start()`` handles the window (it force-finalizes first),
        so route there. Reachable from `ams ctl restart` in the couple of
        seconds after a crash.
        """
        st = self._get(service_id)
        if st.spawned is None or st.reaped:
            self.start(service_id)
        else:
            self.stop(service_id, for_restart=True)

    def kill(self, service_id: str) -> None:
        """Hard kill; no grace period. The exit is still reaped by the loop."""
        st = self._get(service_id)
        st.desired = "down"
        st.restart_at = None
        st.restart_pending = False
        if st.monitor is not None:
            st.monitor.stop()
        if st.spawned is None:
            st.status = "stopped"
            return
        if st.reaped:  # already dead, just not drained yet
            st.status = "stopping"
            return
        st.status = "stopping"
        st.stop_deadline = None
        log.warning("killing %s pid=%d", st.id, st.spawned.pid)
        self.spawner.kill_tree(st.spawned)

    def status(self) -> dict[str, dict[str, Any]]:
        now = self._clock()
        out: dict[str, dict[str, Any]] = {}
        for sid, st in self.services.items():
            cgroup = st.spawned.cgroup if st.spawned else None
            out[sid] = {
                "id": sid,
                "status": st.status,
                "desired": st.desired,
                "pid": st.pid,
                "attempt": st.attempt,
                "consecutive_failures": st.consecutive_failures,
                "uptime_s": st.uptime(now) if st.spawned is not None else None,
                "healthy": st.healthy,
                "ports": dict(st.ports),
                "cgroup": str(cgroup) if cgroup is not None else None,
            }
        return out

    # ------------------------------------------------------------------ loop

    def run_once(self, timeout: float = LOOP_TIMEOUT_S) -> list[Event]:
        """One iteration: poll -> read -> reap -> timers -> finalize.

        Returns every event produced (already routed through the policy), which
        is what an embedding agent loop consumes.
        """
        self._pending = []
        now = self._clock()
        poll_timeout = self._poll_timeout(now, timeout)
        try:
            ready = self._sel.select(poll_timeout)
        except OSError as e:  # EINTR is retried by CPython; anything else is odd
            log.debug("selector error: %s", e)
            ready = []
        for key, _mask in ready:
            if key.data == _SELF_PIPE:
                self._drain_wakeup(key.fd)
            elif isinstance(key.data, _FdCallback):
                self._run_fd_callback(key.data, key.fd)
            else:
                sid, stream = key.data
                self._read_stream(sid, stream, key.fd)
        self._reap()
        self._run_timers(self._clock())
        self._finalize_all(self._clock())
        return self._pending

    def run_forever(
        self,
        stop_event: threading.Event | None = None,
        *,
        on_iteration: Callable[[list[Event]], None] | None = None,
        on_reload: Callable[[], Any] | None = None,
        shutdown_timeout_s: float | None = None,
    ) -> None:
        """Loop until SIGTERM/SIGINT or ``stop_event``, then stop everything.

        ``on_iteration`` is called with each iteration's events; it is the hook
        the CLI uses for periodic reporting and must not raise (it is guarded
        anyway, because a broken reporter must not take down supervision).
        ``on_reload`` is called once per SIGHUP, between iterations rather than
        from the signal handler -- the handler only writes to the wakeup pipe, so
        the reload runs on the ordinary loop stack where it may touch the service
        table. ``shutdown_timeout_s`` bounds the final graceful stop, which
        matters under systemd where ``TimeoutStopSec`` will SIGKILL us if we
        overrun.
        """
        self._stop_requested = False
        self._reload_requested = False
        with self._signal_wakeup():
            while not self._stop_requested:
                if stop_event is not None and stop_event.is_set():
                    break
                events = self.run_once(LOOP_TIMEOUT_S)
                if self._reload_requested:
                    self._reload_requested = False
                    self._run_reload(on_reload)
                if on_iteration is not None:
                    try:
                        on_iteration(events)
                    except Exception as e:
                        log.exception("on_iteration hook raised: %s", e)
            # Shut down INSIDE the context: leaving it first restores the
            # default handlers, so a second SIGTERM during the graceful-stop
            # window would kill the harness and orphan every service.
            log.info("shutting down")
            self.shutdown(shutdown_timeout_s)

    def _run_reload(self, on_reload: Callable[[], Any] | None) -> None:
        if on_reload is None:
            log.warning("SIGHUP received but no reload handler is configured; ignoring")
            return
        try:
            summary = on_reload()
        except Exception as e:  # a bad declaration must never stop the loop
            log.exception("reload handler raised: %s", e)
            return
        log.info("reload complete: %s", summary)

    def shutdown(self, timeout_s: float | None = None) -> None:
        """Stop every service gracefully, escalate to kill_tree, then clean up."""
        grace = timeout_s
        if grace is None:
            grace = max((s.decl.stop.timeout_s for s in self.services.values()), default=5.0)
        for sid, st in list(self.services.items()):
            if st.spawned is not None or st.status in ("starting", "running", "backoff"):
                self.stop(sid)
        deadline = self._clock() + grace + 1.0
        while self._clock() < deadline:
            if not any(s.spawned is not None for s in self.services.values()):
                break
            self.run_once(0.05)
        for st in self.services.values():
            if st.spawned is not None:
                log.warning("forcing kill of %s at shutdown", st.id)
                self.spawner.kill_tree(st.spawned)
        # one last reap+finalize pass so no fd or cgroup is left behind
        for _ in range(20):
            self._pending = []
            self._reap()
            self._finalize_all(self._clock(), force_grace=0.0)
            if not any(s.spawned is not None for s in self.services.values()):
                break
            time.sleep(0.02)
        for st in self.services.values():
            if st.spawned is not None:
                self._finalize(st, force=True)
        self._sel.close()

    # ------------------------------------------------------------- selector

    def register_fd(
        self,
        fd: int,
        callback: Callable[[int], None],
        *,
        events: int = selectors.EVENT_READ,
        name: str = "",
    ) -> None:
        """Watch ``fd`` in this loop and call ``callback(fd)`` when it is ready.

        The extension point the control channel (``ams.control``) uses so it can
        live in the single-threaded loop without reaching into ``self._sel``.
        The callback is invoked from inside :meth:`run_once` and must not block:
        anything it raises is logged and swallowed, because a broken control
        client must never take supervision down.

        Registering an fd that is already registered replaces the previous
        interest, which is how a connection flips from reading a request to
        flushing a response.
        """
        self._unregister(fd)
        self._register(fd, _FdCallback(callback, name), events=events)

    def unregister_fd(self, fd: int) -> None:
        """Stop watching an fd registered with :meth:`register_fd`."""
        self._unregister(fd)

    def _run_fd_callback(self, cb: _FdCallback, fd: int) -> None:
        try:
            cb.fn(fd)
        except Exception as e:  # a control-channel bug must not stop supervision
            log.exception("fd callback %s on fd %d raised: %s", cb.name or "?", fd, e)

    def _register(self, fd: int, data: Any, *, events: int = selectors.EVENT_READ) -> None:
        try:
            self._sel.register(fd, events, data)
            self._registered.add(fd)
        except (KeyError, ValueError, OSError) as e:
            log.warning("could not register fd %d: %s", fd, e)

    def _unregister(self, fd: int) -> None:
        if fd in self._registered:
            try:
                self._sel.unregister(fd)
            except (KeyError, ValueError, OSError):
                pass
            self._registered.discard(fd)

    def _poll_timeout(self, now: float, timeout: float) -> float:
        """How long ``select`` may block.

        Invariant: only fold in a deadline that :meth:`_run_timers` (or
        :meth:`_finalize_all`) would actually act on right now. A deadline that
        is due but not actionable clamps the timeout to 0 and never advances,
        which is a 100 %-CPU spin -- so every term below repeats the exact
        guard of the branch that services it.
        """
        deadline = math.inf
        for st in self.services.values():
            live = st.spawned is not None
            if st.stop_deadline is not None and live:
                deadline = min(deadline, st.stop_deadline)
            if st.reset_at is not None and live:
                deadline = min(deadline, st.reset_at)
            if st.restart_at is not None and not live and st.desired == "up":
                deadline = min(deadline, st.restart_at)
            if st.monitor is not None and live and st.status in ("starting", "running"):
                deadline = min(deadline, st.monitor.next_due)
            if st.reaped and live and st.exited_at is not None:
                deadline = min(deadline, st.exited_at + self._eof_grace_s)
        if deadline is math.inf:
            return max(timeout, 0.0)
        return max(0.0, min(timeout, deadline - now))

    # ----------------------------------------------------------------- reads

    def _read_stream(self, service_id: str, stream: Stream, fd: int) -> None:
        st = self.services.get(service_id)
        if st is None:
            self._unregister(fd)
            return
        while True:
            try:
                chunk = os.read(fd, READ_CHUNK)
            except BlockingIOError:
                return
            except InterruptedError:
                continue
            except OSError as e:
                if e.errno != errno.EBADF:
                    log.debug("read error on %s:%s: %s", service_id, stream, e)
                self._mark_eof(st, stream, fd)
                return
            if not chunk:
                self._mark_eof(st, stream, fd)
                return
            self._consume(st, stream, chunk)

    def _consume(self, st: ServiceState, stream: Stream, chunk: bytes) -> None:
        buf = st.buffers[stream]
        data = bytes(buf) + chunk
        parts = data.split(b"\n")
        tail = parts.pop()
        for raw in parts:
            self._emit_line(st, stream, raw)
        # Bound memory for a line that never ends: keep one byte past the cap so
        # LogLine.from_raw still reports truncated=True.
        if len(tail) > MAX_LINE_BYTES:
            tail = tail[: MAX_LINE_BYTES + 1]
        buf.clear()
        buf.extend(tail)

    def _emit_line(self, st: ServiceState, stream: Stream, raw: bytes) -> None:
        line = LogLine.from_raw(st.id, stream, raw)
        if st.monitor is not None and st.monitor.observe_log(line.text):
            self._set_health(st, True, f"log pattern {st.decl.health.pattern!r} matched")
        self._emit(line, st)

    def _mark_eof(self, st: ServiceState, stream: Stream, fd: int) -> None:
        if stream in st.eof:
            return
        buf = st.buffers[stream]
        if buf:
            raw = bytes(buf)
            buf.clear()
            self._emit_line(st, stream, raw)
        self._unregister(fd)
        st.eof.add(stream)
        log.debug("%s:%s eof", st.id, stream)

    # ---------------------------------------------------------------- reaping

    def _reap(self) -> None:
        while True:
            try:
                pid, wstatus = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                return
            except OSError as e:
                log.debug("waitpid failed: %s", e)
                return
            if pid == 0:
                return
            code = os.waitstatus_to_exitcode(wstatus)
            exit_code = code if code >= 0 else None
            signum = -code if code < 0 else None
            sid = self._by_pid.pop(pid, None)
            if sid is None or sid not in self.services:
                self._emit(OrphanReaped(pid, exit_code, signum), None)
                continue
            self._on_exit(self.services[sid], pid, exit_code, signum)

    def _on_exit(
        self, st: ServiceState, pid: int, exit_code: int | None, signum: int | None
    ) -> None:
        now = self._clock()
        uptime = st.uptime(now) or 0.0
        st.reaped = True
        st.exited_at = now
        st.stop_deadline = None
        st.healthy = None
        if st.monitor is not None:
            st.monitor.stop()
        expected = st.desired == "down" or st.restart_pending
        ok = exit_code == 0
        if not expected and uptime >= max(self._reset_window_min_s, st.decl.health.start_period_s):
            st.consecutive_failures = 0
        # Under restart.policy="always" even a clean exit is a failed run: we
        # asked the service to stay up and it did not. Without this, max_retries
        # is unreachable for a service that exits 0 at once and the harness
        # restarts it forever without ever escalating. The reset window above
        # still forgives a genuinely long-lived run.
        counted = not expected and (not ok or st.decl.restart.policy == "always")
        if counted:
            st.consecutive_failures += 1
        st.last_exit_failed = counted
        st.reset_at = None
        log.info(
            "%s exited pid=%d code=%s signal=%s uptime=%.2fs failures=%d",
            st.id,
            pid,
            exit_code,
            signum,
            uptime,
            st.consecutive_failures,
        )
        self._emit(ServiceExited(st.id, pid, exit_code, signum, uptime), st)
        if st.restart_pending:
            # operator-initiated restart wins over whatever the policy decided
            st.restart_pending = False
            st.desired = "up"
            st.restart_at = now
            st.status = "backoff"

    def _on_spawn_failure(self, st: ServiceState, exc: BaseException) -> None:
        """A spawn that never produced a pid is treated exactly like a crash."""
        log.error("spawn of %s failed: %s", st.id, exc)
        st.spawned = None
        st.started_at = None
        st.consecutive_failures += 1
        text = f"spawn failed: {type(exc).__name__}: {exc}"
        self._emit(LogLine(st.id, "stderr", text, Severity.ERROR), st)
        decision = self.policy.decide(
            ServiceExited(st.id, -1, 127, None, 0.0),
            self._ctx(st),
        )
        if decision.action is Action.RESTART and st.desired == "up":
            self._schedule_restart(st, self._clock())
        else:
            st.status = "failed"
            self._escalate_terminal(st, decision, text)

    # ---------------------------------------------------------------- timers

    def _run_timers(self, now: float) -> None:
        for st in list(self.services.values()):
            if st.stop_deadline is not None and now >= st.stop_deadline and st.spawned is not None:
                log.warning(
                    "%s did not exit within %.1fs of %s; killing",
                    st.id,
                    st.decl.stop.timeout_s,
                    st.decl.stop.signal,
                )
                st.stop_deadline = None
                self.spawner.kill_tree(st.spawned)
            if st.reset_at is not None and now >= st.reset_at and st.spawned is not None:
                if st.consecutive_failures:
                    log.info("%s stable; clearing failure streak", st.id)
                st.consecutive_failures = 0
                st.reset_at = None
            if (
                st.spawned is not None
                and st.monitor is not None
                and st.status in ("starting", "running")
                and st.monitor.due(now)
            ):
                healthy, detail = st.monitor.check(now)
                if healthy is not None:
                    self._set_health(st, healthy, detail)
            if (
                st.restart_at is not None
                and now >= st.restart_at
                and st.desired == "up"
                and st.spawned is None
            ):
                st.restart_at = None
                self.start(st.id)

    def _set_health(self, st: ServiceState, healthy: bool, detail: str) -> None:
        if st.healthy == healthy:
            return
        st.healthy = healthy
        if healthy:
            st.status = "running"
            # A healthy report clears the failure streak -- but only a real probe
            # counts. health.kind="none" is healthy the instant we fork, so
            # honouring it would make max_retries unreachable for a crash loop.
            if st.decl.health.kind != "none":
                st.consecutive_failures = 0
                st.reset_at = None
        self._emit(HealthChanged(st.id, healthy, detail), st)

    # ------------------------------------------------------------- finalize

    def _finalize_all(self, now: float, force_grace: float | None = None) -> None:
        grace = self._eof_grace_s if force_grace is None else force_grace
        for st in list(self.services.values()):
            if st.spawned is None or not st.reaped:
                continue
            expired = st.exited_at is not None and (now - st.exited_at) >= grace
            if len(st.eof) == len(STREAMS) or expired:
                self._finalize(st, force=expired and len(st.eof) != len(STREAMS))

    def _finalize(self, st: ServiceState, *, force: bool = False) -> None:
        svc = st.spawned
        if svc is None:
            return
        if force:
            for stream in STREAMS:
                if stream not in st.eof:
                    log.debug("%s:%s still open after exit; forcing close", st.id, stream)
                    buf = st.buffers[stream]
                    if buf:
                        raw = bytes(buf)
                        buf.clear()
                        self._emit_line(st, stream, raw)
        for fd in (svc.stdout_fd, svc.stderr_fd):
            self._unregister(fd)
        self._by_pid.pop(svc.pid, None)
        try:
            self.spawner.cleanup(svc)
        except Exception as e:
            # Detach the spawn regardless of how cleanup failed: leaving it
            # attached means the next _finalize_all pass calls cleanup again,
            # closing fd numbers that have since been reused.
            log.warning("cleanup of %s failed: %s", st.id, e)
        finally:
            st.spawned = None
        st.started_at = None
        st.eof = set(STREAMS)
        if st.status == "stopping":
            st.status = "stopped"
        log.debug("%s finalized", st.id)

    # ------------------------------------------------------------- decisions

    def _ctx(self, st: ServiceState | None) -> ServiceContext:
        if st is None:
            return _ORPHAN_CTX
        return ServiceContext(
            decl=st.decl,
            attempt=st.attempt,
            consecutive_failures=st.consecutive_failures,
            running=st.spawned is not None,
        )

    def _emit(self, event: Event, st: ServiceState | None) -> None:
        """Route one event through the policy, apply the action, record it."""
        self._pending.append(event)
        ctx = self._ctx(st)
        try:
            decision = self.policy.decide(event, ctx)
        except Exception as e:  # a broken policy must not take down the harness
            log.exception("policy raised on %s: %s", type(event).__name__, e)
            decision = Decision(Action.LOG, f"policy error: {e}")
        self._apply(event, decision, ctx, st)

    def _apply(
        self,
        event: Event,
        decision: Decision,
        ctx: ServiceContext,
        st: ServiceState | None,
    ) -> None:
        action = decision.action
        if action is Action.SUPPRESS:
            return
        self._log_event(event)
        if action is Action.ESCALATE:
            self._escalate(event, decision, ctx)
            return
        if st is None:
            return
        if action is Action.RESTART:
            self._act_restart(event, st)
        elif action is Action.STOP:
            self._act_stop(event, decision, st)

    def _act_restart(self, event: Event, st: ServiceState) -> None:
        if st.desired == "down":
            log.info("ignoring policy RESTART for %s: operator stopped it", st.id)
            return
        if isinstance(event, ServiceExited):
            self._schedule_restart(st, self._clock())
        elif st.spawned is not None:
            self.restart(st.id)
        else:
            self._schedule_restart(st, self._clock())

    def _act_stop(self, event: Event, decision: Decision, st: ServiceState) -> None:
        if not isinstance(event, ServiceExited):
            self.stop(st.id)
            return
        # The process is already gone; STOP only decides how we record it.
        # "failed" means the run we are declining to restart was itself a failed
        # one (``last_exit_failed``, set in _on_exit). The streak alone is the
        # wrong test: it can still be non-zero from an earlier crash that a
        # later clean exit did not add to.
        st.desired = "down"
        st.restart_at = None
        if st.last_exit_failed:
            st.status = "failed"
            self._escalate_terminal(st, decision, f"giving up on {st.id}: {decision.reason}")
        else:
            st.status = "stopped"

    def _schedule_restart(self, st: ServiceState, now: float) -> None:
        r = st.decl.restart
        # Clamp the shift: 2**1024 overflows float conversion, and anything past
        # ~2**30 is capped by backoff_max_s anyway.
        exp = min(max(0, st.consecutive_failures - 1), MAX_BACKOFF_SHIFT)
        delay = min(r.backoff_s * (2**exp), r.backoff_max_s)
        st.restart_at = now + delay
        st.status = "backoff"
        log.info("restarting %s in %.2fs (failures=%d)", st.id, delay, st.consecutive_failures)

    def _escalate_terminal(self, st: ServiceState, decision: Decision, detail: str) -> None:
        """A service we will not restart again: the agent must always hear about it."""
        log.error("%s", detail)
        event = LogLine(st.id, "stderr", detail, Severity.CRITICAL)
        self._pending.append(event)
        self._escalate(event, decision, self._ctx(st))

    def _escalate(self, event: Event, decision: Decision, ctx: ServiceContext) -> None:
        try:
            self.escalation.escalate(event, decision, ctx)
        except Exception as e:  # an unreachable agent must not stop supervision
            log.exception("escalation failed: %s", e)

    def _log_event(self, event: Event) -> None:
        if isinstance(event, LogLine):
            service_log.log(
                int(event.severity),
                "[%s:%s] %s%s",
                event.service_id,
                event.stream,
                event.text,
                " (truncated)" if event.truncated else "",
            )
            return
        log.log(int(severity_of(event)), "%s", _describe(event))

    # ---------------------------------------------------------------- signals

    class _SignalWakeup:
        """Context manager: self-pipe + no-op handlers, restored on exit."""

        def __init__(self, sup: Supervisor) -> None:
            self.sup = sup
            self.r = -1
            self.w = -1
            self.old: dict[int, Any] = {}
            self.old_wakeup = -1

        def __enter__(self) -> Supervisor._SignalWakeup:
            self.r, self.w = os.pipe()
            os.set_blocking(self.r, False)
            os.set_blocking(self.w, False)
            self.sup._wakeup_r = self.r
            self.sup._register(self.r, _SELF_PIPE)
            for signum in _WATCHED_SIGNALS:
                try:
                    self.old[signum] = signal.signal(signum, _noop_handler)
                except (ValueError, OSError) as e:  # not the main thread
                    log.warning("could not install handler for signal %d: %s", signum, e)
            try:
                self.old_wakeup = signal.set_wakeup_fd(self.w)
            except ValueError as e:
                log.warning("no signal wakeup fd (not the main thread?): %s", e)
            return self

        def __exit__(self, *exc: Any) -> None:
            try:
                signal.set_wakeup_fd(self.old_wakeup)
            except ValueError:
                pass
            for signum, handler in self.old.items():
                try:
                    signal.signal(signum, handler)
                except (ValueError, OSError):
                    pass
            self.sup._unregister(self.r)
            self.sup._wakeup_r = None
            for fd in (self.r, self.w):
                try:
                    os.close(fd)
                except OSError:
                    pass

    def _signal_wakeup(self) -> Supervisor._SignalWakeup:
        return Supervisor._SignalWakeup(self)

    def _drain_wakeup(self, fd: int) -> None:
        """Read the wakeup pipe and turn signal numbers into loop intentions.

        ``set_wakeup_fd`` writes one byte per delivered signal, so the byte *is*
        the signal number; SIGCHLD needs no action beyond having woken us.
        """
        try:
            data = os.read(fd, 4096)
        except (BlockingIOError, OSError):
            return
        for byte in data:
            if byte in (int(signal.SIGTERM), int(signal.SIGINT)):
                log.info("received signal %d; stopping", byte)
                self._stop_requested = True
            elif _SIGHUP is not None and byte == int(_SIGHUP):
                log.info("received SIGHUP; reload requested")
                self._reload_requested = True


def _noop_handler(signum: int, frame: Any) -> None:
    """Signals are handled by reading the wakeup fd in the main loop."""


def _describe(event: Event) -> str:
    if isinstance(event, ServiceStarted):
        return f"{event.service_id} started pid={event.pid} attempt={event.attempt}"
    if isinstance(event, ServiceExited):
        return (
            f"{event.service_id} exited code={event.exit_code} signal={event.signal} "
            f"uptime={event.uptime_s:.2f}s"
        )
    if isinstance(event, HealthChanged):
        return f"{event.service_id} health={'ok' if event.healthy else 'FAIL'} {event.detail}"
    if isinstance(event, OrphanReaped):
        return f"orphan pid={event.pid} code={event.exit_code} signal={event.signal}"
    return str(event)


def set_child_subreaper_if_possible() -> bool:
    """Linux: reparent orphaned grandchildren to us. No-op elsewhere."""
    try:
        from ams.userns import set_child_subreaper  # noqa: PLC0415 - optional, Linux-only
    except ImportError:
        log.debug("ams.userns unavailable; not a subreaper")
        return False
    try:
        set_child_subreaper()
    except (OSError, AttributeError, NotImplementedError) as e:
        log.debug("PR_SET_CHILD_SUBREAPER unavailable: %s", e)
        return False
    log.info("harness is a child subreaper")
    return True
