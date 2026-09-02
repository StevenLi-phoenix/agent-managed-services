"""Supervision loop tests.

Everything here runs against ``PlainSpawner`` with real ``python -c``
subprocesses, so the loop's logic (pipes, reaping, backoff, lifecycle) is
verified independently of the Linux isolation layer and works on macOS.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import pytest

from ams.decision import Action, Decision, DefaultPolicy, ServiceContext
from ams.events import (
    MAX_LINE_BYTES,
    Event,
    LogLine,
    OrphanReaped,
    ServiceExited,
    ServiceStarted,
    Severity,
)
from ams.schema import ServiceDecl, loads
from ams.spawn import PlainSpawner, SpawnedService, SpawnRequest
from ams.supervisor import ServiceState, Supervisor

# --------------------------------------------------------------------- helpers


def decl(code: str, *, sid: str = "svc", extra: str = "") -> ServiceDecl:
    argv = json.dumps([sys.executable, "-c", code])
    return loads(f'id = "{sid}"\n[start]\nargv = {argv}\n{extra}')


def pump(
    sup: Supervisor,
    *,
    until: Any = None,
    seconds: float = 8.0,
    step: float = 0.02,
) -> list[Event]:
    """Drive run_once until ``until(events)`` is true or ``seconds`` elapse."""
    events: list[Event] = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        events += sup.run_once(step)
        if until is not None and until(events):
            return events
    if until is not None:
        raise AssertionError(f"condition never met after {seconds}s; events={events}")
    return events


def lines(events: list[Event], stream: str | None = None) -> list[str]:
    return [
        e.text for e in events if isinstance(e, LogLine) and (stream is None or e.stream == stream)
    ]


def of_type(events: list[Event], kind: type) -> list[Any]:
    return [e for e in events if isinstance(e, kind)]


def saw_text(events: list[Event], needle: str) -> bool:
    return any(needle in t for t in lines(events))


class RecordingSpawner:
    """PlainSpawner wrapper that records kill_tree / cleanup calls."""

    def __init__(self) -> None:
        self.inner = PlainSpawner()
        self.killed: list[int] = []
        self.cleaned: list[int] = []

    def spawn(self, req: SpawnRequest) -> SpawnedService:
        return self.inner.spawn(req)

    def kill_tree(self, svc: SpawnedService) -> None:
        self.killed.append(svc.pid)
        self.inner.kill_tree(svc)

    def cleanup(self, svc: SpawnedService) -> None:
        self.cleaned.append(svc.pid)
        self.inner.cleanup(svc)


class BrokenSpawner:
    def __init__(self) -> None:
        self.calls = 0

    def spawn(self, req: SpawnRequest) -> SpawnedService:
        self.calls += 1
        raise OSError(2, "No such file or directory: 'nope'")

    def kill_tree(self, svc: SpawnedService) -> None:  # pragma: no cover - never spawned
        pass

    def cleanup(self, svc: SpawnedService) -> None:  # pragma: no cover - never spawned
        pass


@dataclass
class RecordingPolicy:
    inner: Any = field(default_factory=DefaultPolicy)
    seen: list[tuple[str, Action]] = field(default_factory=list)

    def decide(self, event: Event, ctx: ServiceContext) -> Decision:
        d = self.inner.decide(event, ctx)
        self.seen.append((type(event).__name__, d.action))
        return d

    def actions_for(self, kind: str) -> list[Action]:
        return [a for k, a in self.seen if k == kind]


@dataclass
class FixedPolicy:
    """Returns ``action`` for log lines matching ``needle``; DefaultPolicy else."""

    action: Action
    needle: str = ""
    inner: Any = field(default_factory=DefaultPolicy)

    def decide(self, event: Event, ctx: ServiceContext) -> Decision:
        if isinstance(event, LogLine) and (not self.needle or self.needle in event.text):
            return Decision(self.action, "test policy")
        return self.inner.decide(event, ctx)


@dataclass
class RecordingEscalation:
    events: list[tuple[Event, Decision]] = field(default_factory=list)

    def escalate(self, event: Event, decision: Decision, ctx: ServiceContext) -> None:
        self.events.append((event, decision))


def make(tmp_path, d: ServiceDecl, **kw) -> tuple[Supervisor, ServiceState]:
    kw.setdefault("reset_window_min_s", 0.3)
    kw.setdefault("eof_grace_s", 0.3)
    spawner = kw.pop("spawner", None) or RecordingSpawner()
    sup = Supervisor(spawner, **kw)
    st = sup.add(d, tmp_path / d.id, {})
    return sup, st


# ------------------------------------------------------------------- log lines

READY = "print('ready', flush=True)\n"


def test_lines_are_tagged_assembled_and_flushed_at_eof(tmp_path):
    code = (
        "import sys, time\n"
        "sys.stdout.write('par'); sys.stdout.flush()\n"
        "time.sleep(0.15)\n"
        "sys.stdout.write('tial\\nsecond\\n'); sys.stdout.flush()\n"
        "sys.stderr.write('tail-without-newline'); sys.stderr.flush()\n"
    )
    sup, st = make(tmp_path, decl(code))
    sup.start("svc")
    events = pump(sup, until=lambda ev: st.status in ("stopped", "failed"))
    assert lines(events, "stdout") == ["partial", "second"]
    assert lines(events, "stderr") == ["tail-without-newline"]
    assert all(e.service_id == "svc" for e in of_type(events, LogLine))
    sup.shutdown(1.0)


def test_overlong_line_is_truncated(tmp_path):
    code = "import sys\nsys.stdout.write('x' * 102400 + '\\n')\nsys.stdout.flush()\n"
    sup, st = make(tmp_path, decl(code))
    sup.start("svc")
    events = pump(sup, until=lambda ev: st.status in ("stopped", "failed"))
    big = [e for e in of_type(events, LogLine) if e.stream == "stdout"]
    assert len(big) == 1
    assert big[0].truncated is True
    assert len(big[0].text) == MAX_LINE_BYTES
    sup.shutdown(1.0)


# -------------------------------------------------------------- exit / restart


def test_clean_exit_is_not_restarted_with_on_failure(tmp_path):
    d = decl("print('bye')", extra='[restart]\npolicy = "on-failure"\nbackoff_s = 0.05\n')
    sup, st = make(tmp_path, d)
    sup.start("svc")
    pump(sup, until=lambda ev: st.status in ("stopped", "failed"))
    # give a restart, if one were scheduled, time to fire
    pump(sup, seconds=0.4)
    assert st.status == "stopped"
    assert st.attempt == 1
    assert st.consecutive_failures == 0


def test_failure_restarts_with_backoff_then_gives_up(tmp_path):
    d = decl(
        "import sys; print('boom', file=sys.stderr); sys.exit(1)",
        extra=(
            "[health]\nstart_period_s = 0.1\n"
            '[restart]\npolicy = "on-failure"\nmax_retries = 3\n'
            "backoff_s = 0.05\nbackoff_max_s = 0.2\n"
        ),
    )
    esc = RecordingEscalation()
    sup, st = make(tmp_path, d, escalation=esc)
    sup.start("svc")
    events = pump(sup, until=lambda ev: st.status == "failed")
    assert st.attempt == 3
    assert st.consecutive_failures == 3
    assert len(of_type(events, ServiceStarted)) == 2  # the first start is not in `events`
    assert len(of_type(events, ServiceExited)) == 3
    assert esc.events, "giving up must be escalated"
    assert any("giving up on svc" in getattr(e, "text", "") for e, _ in esc.events)


def test_failure_streak_resets_after_a_stable_run(tmp_path):
    d = decl(
        "import sys, time; time.sleep(0.45); sys.exit(1)",
        extra=(
            "[health]\nstart_period_s = 0.05\n"
            '[restart]\npolicy = "on-failure"\nmax_retries = 5\n'
            "backoff_s = 0.05\nbackoff_max_s = 0.05\n"
        ),
    )
    sup, st = make(tmp_path, d, reset_window_min_s=0.3)
    sup.start("svc")
    pump(sup, until=lambda ev: len(of_type(ev, ServiceExited)) >= 2, seconds=6.0)
    # each run lasted longer than the 0.3s reset window, so the streak never grows
    assert st.consecutive_failures == 1
    assert st.attempt >= 2
    sup.shutdown(1.0)


def test_restart_replaces_the_pid(tmp_path):
    d = decl(READY + "import time; time.sleep(30)\n", extra="[stop]\ntimeout_s = 1.0\n")
    sup, st = make(tmp_path, d)
    sup.start("svc")
    pump(sup, until=lambda ev: saw_text(ev, "ready"))
    first = st.pid
    sup.restart("svc")
    pump(sup, until=lambda ev: st.pid is not None and st.pid != first and st.status != "stopping")
    assert st.pid != first
    assert st.attempt == 2
    assert st.desired == "up"
    sup.shutdown(1.0)


# --------------------------------------------------------------- graceful stop


def test_graceful_stop_of_a_well_behaved_child_needs_no_kill(tmp_path):
    code = (
        "import signal, sys, time\n"
        "def handler(signum, frame):\n"
        "    print('caught', flush=True)\n"
        "    sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, handler)\n" + READY + "time.sleep(30)\n"
    )
    d = decl(code, extra="[stop]\ntimeout_s = 2.0\n")
    sup, st = make(tmp_path, d)
    sup.start("svc")
    pump(sup, until=lambda ev: saw_text(ev, "ready"))
    sup.stop("svc")
    events = pump(sup, until=lambda ev: st.status == "stopped")
    assert sup.spawner.killed == []
    assert saw_text(events, "caught")
    exited = of_type(events, ServiceExited)[0]
    assert exited.exit_code == 0
    assert sup.spawner.cleaned == [exited.pid]


def test_stop_timeout_kills_a_child_that_ignores_sigterm(tmp_path):
    code = (
        "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        + READY
        + "time.sleep(30)\n"
    )
    d = decl(code, extra="[stop]\ntimeout_s = 0.3\n")
    sup, st = make(tmp_path, d)
    sup.start("svc")
    pump(sup, until=lambda ev: saw_text(ev, "ready"))
    pid = st.pid
    sup.stop("svc")
    events = pump(sup, until=lambda ev: st.status == "stopped")
    assert sup.spawner.killed == [pid]
    exited = of_type(events, ServiceExited)[0]
    assert exited.signal == signal.SIGKILL
    assert st.status == "stopped"  # operator-requested, so not "failed"


def test_grandchild_holding_the_pipe_does_not_block_cleanup(tmp_path):
    code = (
        "import subprocess, sys\n"
        f"subprocess.Popen([{sys.executable!r}, '-c', 'import time; time.sleep(3)'])\n"
        "print('parent-done', flush=True)\n"
        "sys.exit(0)\n"
    )
    sup, st = make(tmp_path, decl(code), eof_grace_s=0.3)
    sup.start("svc")
    started = time.monotonic()
    pump(sup, until=lambda ev: st.spawned is None, seconds=4.0)
    assert st.status == "stopped"
    assert sup.spawner.cleaned, "cleanup must run even though the pipe stayed open"
    assert time.monotonic() - started < 3.0, "must not wait for the grandchild"


# ------------------------------------------------------------------- reaping


def test_unknown_child_becomes_an_orphan_event(tmp_path):
    sup, st = make(tmp_path, decl("import time; time.sleep(0.4)"))
    sup.start("svc")
    stray = subprocess.Popen([sys.executable, "-c", "pass"])
    events = pump(sup, until=lambda ev: of_type(ev, OrphanReaped), seconds=5.0)
    orphans = of_type(events, OrphanReaped)
    assert stray.pid in [o.pid for o in orphans]
    assert orphans[0].exit_code == 0
    sup.shutdown(1.0)
    stray.poll()


# ------------------------------------------------------------------ decisions


def test_policy_sees_crash_then_success(tmp_path):
    marker = tmp_path / "once"
    code = (
        "import os, sys\n"
        f"m = {str(marker)!r}\n"
        "if not os.path.exists(m):\n"
        "    open(m, 'w').close()\n"
        "    print('crashing', file=sys.stderr)\n"
        "    sys.exit(1)\n"
        "print('ok')\n"
    )
    d = decl(
        code,
        extra=(
            "[health]\nstart_period_s = 0.05\n"
            '[restart]\npolicy = "on-failure"\nbackoff_s = 0.05\nmax_retries = 5\n'
        ),
    )
    policy = RecordingPolicy()
    sup, st = make(tmp_path, d, policy=policy)
    sup.start("svc")
    pump(sup, until=lambda ev: len(of_type(ev, ServiceExited)) >= 2)
    pump(sup, seconds=0.3)
    assert policy.actions_for("ServiceExited") == [Action.RESTART, Action.STOP]
    assert st.status == "stopped"
    assert st.attempt == 2


def test_suppressed_lines_never_reach_the_service_logger(tmp_path, caplog):
    d = decl("print('secret token')")
    sup, st = make(tmp_path, d, policy=FixedPolicy(Action.SUPPRESS, "secret"))
    with caplog.at_level(logging.DEBUG, logger="ams.services"):
        sup.start("svc")
        events = pump(sup, until=lambda ev: st.status in ("stopped", "failed"))
    assert saw_text(events, "secret token"), "the event is still produced"
    assert [r for r in caplog.records if r.name == "ams.services"] == []


def test_logged_lines_do_reach_the_service_logger(tmp_path, caplog):
    d = decl("import sys; print('ERROR nope', file=sys.stderr)")
    sup, st = make(tmp_path, d)
    with caplog.at_level(logging.DEBUG, logger="ams.services"):
        sup.start("svc")
        pump(sup, until=lambda ev: st.status in ("stopped", "failed"))
    msgs = [r.getMessage() for r in caplog.records if r.name == "ams.services"]
    assert "[svc:stderr] ERROR nope" in msgs
    assert any(r.levelno == Severity.ERROR for r in caplog.records if r.name == "ams.services")


def test_stop_action_on_a_log_line_stops_the_service(tmp_path):
    d = decl(
        READY + "print('FATAL corrupt state', flush=True)\nimport time; time.sleep(30)\n",
        extra='[stop]\ntimeout_s = 1.0\n[restart]\npolicy = "always"\nbackoff_s = 0.05\n',
    )
    sup, st = make(tmp_path, d, policy=FixedPolicy(Action.STOP, "FATAL"))
    sup.start("svc")
    pump(sup, until=lambda ev: st.status == "stopped", seconds=6.0)
    pump(sup, seconds=0.4)  # a restart, if scheduled, would have fired
    assert st.desired == "down"
    assert st.status == "stopped"
    assert st.attempt == 1


def test_restart_action_on_a_log_line_restarts_the_service(tmp_path):
    d = decl(
        READY + "print('please-restart', flush=True)\nimport time; time.sleep(30)\n",
        extra="[stop]\ntimeout_s = 1.0\n",
    )
    sup, st = make(tmp_path, d, policy=FixedPolicy(Action.RESTART, "please-restart"))
    sup.start("svc")
    pump(sup, until=lambda ev: st.attempt >= 2, seconds=6.0)
    assert st.attempt >= 2
    sup.shutdown(1.0)


def test_spawn_failure_is_treated_like_a_crash(tmp_path):
    d = decl(
        "pass",
        extra=(
            "[health]\nstart_period_s = 0.05\n"
            '[restart]\npolicy = "on-failure"\nmax_retries = 2\n'
            "backoff_s = 0.05\nbackoff_max_s = 0.05\n"
        ),
    )
    esc = RecordingEscalation()
    broken = BrokenSpawner()
    sup, st = make(tmp_path, d, spawner=broken, escalation=esc)
    sup.start("svc")
    pump(sup, until=lambda ev: st.status == "failed", seconds=4.0)
    assert broken.calls == 2
    assert st.consecutive_failures == 2
    assert st.status == "failed"
    assert esc.events


# ------------------------------------------------------------- table + status


def test_status_summary_is_serialisable(tmp_path):
    d = decl(READY + "import time; time.sleep(30)\n", extra="[stop]\ntimeout_s = 1.0\n")
    sup, st = make(tmp_path, d)
    sup.add(decl("pass", sid="other"), tmp_path / "other", {"main": 1234})
    sup.start("svc")
    pump(sup, until=lambda ev: saw_text(ev, "ready"))
    snapshot = sup.status()
    json.dumps(snapshot)  # must not raise
    assert snapshot["svc"]["pid"] == st.pid
    assert snapshot["svc"]["status"] in ("starting", "running")
    assert snapshot["svc"]["attempt"] == 1
    assert snapshot["svc"]["uptime_s"] >= 0
    assert snapshot["svc"]["cgroup"] is None
    assert snapshot["other"]["ports"] == {"main": 1234}
    assert snapshot["other"]["status"] == "stopped"
    sup.shutdown(1.0)


def test_remove_requires_a_stopped_service(tmp_path):
    d = decl(READY + "import time; time.sleep(30)\n", extra="[stop]\ntimeout_s = 1.0\n")
    sup, st = make(tmp_path, d)
    sup.start("svc")
    pump(sup, until=lambda ev: saw_text(ev, "ready"))
    with pytest.raises(RuntimeError):
        sup.remove("svc")
    sup.stop("svc")
    pump(sup, until=lambda ev: st.status == "stopped")
    sup.remove("svc")
    assert "svc" not in sup.services
    with pytest.raises(KeyError):
        sup.start("svc")


def test_duplicate_registration_is_rejected(tmp_path):
    sup, _ = make(tmp_path, decl("pass"))
    with pytest.raises(KeyError):
        sup.add(decl("pass"), tmp_path / "svc", {})


# ------------------------------------------------------------------- signals


@pytest.mark.skipif(
    threading.current_thread() is not threading.main_thread(),
    reason="signal handlers need the main thread",
)
def test_run_forever_returns_after_sigterm(tmp_path):
    d = decl(READY + "import time; time.sleep(30)\n", extra="[stop]\ntimeout_s = 1.0\n")
    sup, st = make(tmp_path, d)
    sup.start("svc")
    pump(sup, until=lambda ev: saw_text(ev, "ready"))
    before = signal.getsignal(signal.SIGTERM)
    timer = threading.Timer(0.3, lambda: os.kill(os.getpid(), signal.SIGTERM))
    timer.start()
    started = time.monotonic()
    try:
        sup.run_forever()
    finally:
        timer.cancel()
    assert time.monotonic() - started < 8.0
    assert st.spawned is None
    assert st.status == "stopped"
    assert signal.getsignal(signal.SIGTERM) is before, "handlers must be restored"


# ------------------------------------------------ regressions (review-1.md)

TCP_SERVER = (
    "import socket, signal, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "s = socket.socket()\n"
    "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
    "s.bind(('127.0.0.1', int('${PORT_main}')))\n"
    "s.listen(8)\n" + READY + "time.sleep(30)\n"
)

GRANDCHILD_HOLDS_PIPE = (
    "import subprocess, sys\n"
    f"subprocess.Popen([{sys.executable!r}, '-c', 'import time; time.sleep(3)'])\n"
    "print('spawned-grandchild', flush=True)\n"
)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def count_iterations(sup: Supervisor, seconds: float, step: float = 0.05) -> int:
    """How many run_once calls fit in ``seconds``. A spin makes this explode."""
    n = 0
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        sup.run_once(step)
        n += 1
    return n


def test_no_cpu_spin_while_waiting_out_the_stop_timeout(tmp_path):
    """review-1 CRITICAL: an overdue health timer during "stopping" pinned
    poll_timeout at 0 for the whole stop.timeout_s window."""
    port = free_port()
    argv = json.dumps([sys.executable, "-c", TCP_SERVER])
    d = loads(
        f'id = "svc"\n[start]\nargv = {argv}\n[ports]\nmain = {port}\n'
        '[health]\nkind = "tcp"\nport = "main"\ninterval_s = 0.05\n'
        "timeout_s = 0.5\nstart_period_s = 5.0\n"
        "[stop]\ntimeout_s = 2.0\n"
    )
    sup = Supervisor(RecordingSpawner(), reset_window_min_s=0.3, eof_grace_s=0.3)
    st = sup.add(d, tmp_path / "svc", {"main": port})
    sup.start("svc")
    pump(sup, until=lambda ev: st.healthy is True)
    assert st.status == "running"
    sup.stop("svc")
    assert st.status == "stopping"
    iterations = count_iterations(sup, 0.5)
    assert st.status == "stopping", "the child ignores SIGTERM, so we are mid-timeout"
    assert iterations < 200, f"busy loop during stop: {iterations} iterations in 0.5s"
    sup.shutdown(1.0)


def test_no_cpu_spin_while_a_due_restart_waits_for_eof(tmp_path):
    """review-1 HIGH: restart_at could be due while spawned was still set,
    waiting out the EOF grace; nothing advanced the deadline."""
    d = decl(
        GRANDCHILD_HOLDS_PIPE + "import sys; sys.exit(1)\n",
        extra=(
            "[health]\nstart_period_s = 0.05\n"
            '[restart]\npolicy = "on-failure"\nbackoff_s = 0.0\nmax_retries = 50\n'
        ),
    )
    sup, st = make(tmp_path, d, eof_grace_s=1.5)
    sup.start("svc")
    pump(sup, until=lambda ev: of_type(ev, ServiceExited))
    assert st.reaped and st.spawned is not None, "reaped but still draining"
    assert st.restart_at is not None and st.restart_at <= time.monotonic()
    iterations = count_iterations(sup, 0.5)
    assert iterations < 200, f"busy loop during EOF grace: {iterations} iterations in 0.5s"
    sup.shutdown(1.0)


def test_start_on_a_running_service_raises_instead_of_orphaning_it(tmp_path):
    """review-1 HIGH: the old code finalized the live spawn, dropping it from
    _by_pid and leaving it running unsupervised."""
    d = decl(READY + "import time; time.sleep(30)\n", extra="[stop]\ntimeout_s = 1.0\n")
    sup, st = make(tmp_path, d)
    sup.start("svc")
    pump(sup, until=lambda ev: saw_text(ev, "ready"))
    pid = st.pid
    with pytest.raises(RuntimeError, match="still running"):
        sup.start("svc")
    assert st.pid == pid
    assert st.attempt == 1
    assert sup._by_pid.get(pid) == "svc", "the live pid must stay attributed"
    assert sup.spawner.cleaned == []
    os.kill(pid, 0)  # still alive and still ours
    sup.shutdown(1.0)


def test_start_after_a_reaped_but_undrained_run_is_allowed(tmp_path):
    d = decl(GRANDCHILD_HOLDS_PIPE, extra='[restart]\npolicy = "never"\n')
    sup, st = make(tmp_path, d, eof_grace_s=5.0)
    sup.start("svc")
    pump(sup, until=lambda ev: of_type(ev, ServiceExited))
    assert st.reaped and st.spawned is not None
    sup.start("svc")  # must not raise: the process is already gone
    assert st.attempt == 2
    assert st.spawned is not None and not st.reaped
    sup.shutdown(1.0)


def test_stop_and_kill_never_signal_a_reaped_pid(tmp_path, monkeypatch):
    """review-1 MEDIUM: signalling a reaped pid can hit an unrelated process
    of the same user once the pid is reused."""
    d = decl(GRANDCHILD_HOLDS_PIPE, extra='[restart]\npolicy = "never"\n[stop]\ntimeout_s = 1.0\n')
    sup, st = make(tmp_path, d, eof_grace_s=5.0)
    sup.start("svc")
    pump(sup, until=lambda ev: of_type(ev, ServiceExited))
    assert st.reaped and st.spawned is not None

    signalled: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: signalled.append((pid, sig)))
    sup.stop("svc")
    sup.kill("svc")
    assert signalled == []
    assert sup.spawner.killed == [], "kill_tree targets a dead pid's group too"
    monkeypatch.undo()
    sup.shutdown(1.0)


def test_backoff_exponent_cannot_overflow(tmp_path):
    """review-1 LOW: 1.0 * 2**1024 raises OverflowError out of _reap."""
    d = decl("pass", extra="[restart]\nmax_retries = 5000\nbackoff_s = 1.0\nbackoff_max_s = 60.0\n")
    sup, st = make(tmp_path, d)
    st.consecutive_failures = 4000
    sup._schedule_restart(st, 0.0)  # internal: the overflow was unreachable from outside
    assert st.restart_at == 60.0


def test_signal_handlers_stay_installed_during_shutdown(tmp_path):
    """review-1 LOW: leaving the wakeup context before shutdown() meant a second
    SIGTERM in the graceful-stop window killed the harness outright."""
    seen: list[Any] = []

    class WatchingSpawner(RecordingSpawner):
        def cleanup(self, svc: SpawnedService) -> None:
            seen.append(signal.getsignal(signal.SIGTERM))
            super().cleanup(svc)

    d = decl(READY + "import time; time.sleep(30)\n", extra="[stop]\ntimeout_s = 1.0\n")
    sup, st = make(tmp_path, d, spawner=WatchingSpawner())
    sup.start("svc")
    pump(sup, until=lambda ev: saw_text(ev, "ready"))
    stop_event = threading.Event()
    threading.Timer(0.2, stop_event.set).start()
    sup.run_forever(stop_event)
    assert st.status == "stopped"
    assert seen, "cleanup must run during shutdown"
    assert signal.SIG_DFL not in seen, "handlers were restored before shutdown finished"


def test_always_policy_gives_up_on_a_clean_exit_loop(tmp_path):
    """review-1 LOW: restart.policy="always" + immediate exit 0 never grew the
    failure streak, so max_retries was unreachable and nothing escalated."""
    d = decl(
        "print('up and out')",
        extra=(
            "[health]\nstart_period_s = 0.05\n"
            '[restart]\npolicy = "always"\nmax_retries = 3\n'
            "backoff_s = 0.05\nbackoff_max_s = 0.1\n"
        ),
    )
    esc = RecordingEscalation()
    sup, st = make(tmp_path, d, escalation=esc, reset_window_min_s=0.3)
    sup.start("svc")
    pump(sup, until=lambda ev: st.status == "failed", seconds=6.0)
    assert st.consecutive_failures == 3
    assert st.attempt == 3
    assert esc.events, "giving up must reach the agent"


def test_always_policy_forgives_a_long_lived_run(tmp_path):
    """The streak counts short-lived runs only; a service that stayed up past
    the reset window and then exited 0 starts over at one."""
    d = decl(
        "import time; time.sleep(0.45)",
        extra=(
            "[health]\nstart_period_s = 0.05\n"
            '[restart]\npolicy = "always"\nmax_retries = 5\n'
            "backoff_s = 0.05\nbackoff_max_s = 0.05\n"
        ),
    )
    sup, st = make(tmp_path, d, reset_window_min_s=0.3)
    sup.start("svc")
    pump(sup, until=lambda ev: len(of_type(ev, ServiceExited)) >= 2, seconds=6.0)
    assert st.consecutive_failures == 1
    assert st.status != "failed"
    sup.shutdown(1.0)
