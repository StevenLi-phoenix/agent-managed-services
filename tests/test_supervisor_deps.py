"""Start-order dependencies (``depends_on``).

Written against ``PlainSpawner`` with real ``python -c`` children, like
``tests/test_supervisor.py``, so the gate is exercised through the actual loop
(timers, health, reaping) rather than by poking ``ServiceState``.

The incident these tests encode: on 2026-09-02 a harness restart started 18
services at once, the 11 that register with the registry during FastAPI startup
got ``Connection refused``, and every one of them burned its retry budget
before the registry had bound its socket.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import pytest

from ams.decision import Decision, ServiceContext
from ams.events import Event, LogLine, ServiceExited, Severity
from ams.schema import ServiceDecl, loads
from ams.spawn import PlainSpawner
from ams.supervisor import ServiceState, Supervisor

# --------------------------------------------------------------------- helpers

#: Sleeps ``delay`` seconds, prints "ready", then idles. The delay is what makes
#: "spawned" and "usable" two different moments, which is the whole point.
SLOW_READY = "import sys, time\ntime.sleep({delay})\nprint('ready', flush=True)\ntime.sleep(30)\n"
IDLE = "import sys, time\nprint('up', flush=True)\ntime.sleep(30)\n"

#: Appends its own id to a shared file on SIGTERM, so a test can assert the
#: order in which services were actually torn down.
NOTE_ON_TERM = (
    "import signal, sys, time\n"
    "def bye(*a):\n"
    "    with open({path!r}, 'a') as f:\n"
    "        f.write({sid!r} + chr(10))\n"
    "    sys.exit(0)\n"
    "signal.signal(signal.SIGTERM, bye)\n"
    "print('ready', flush=True)\n"
    "time.sleep(30)\n"
)


def decl(code: str, *, sid: str, depends_on: tuple[str, ...] = (), extra: str = "") -> ServiceDecl:
    argv = json.dumps([sys.executable, "-c", code])
    deps = f"depends_on = {json.dumps(list(depends_on))}\n" if depends_on else ""
    return loads(f'id = "{sid}"\n{deps}[start]\nargv = {argv}\n{extra}')


#: A ``log``-kind health check is the only one that needs no port, which keeps
#: these tests free of port allocation.
LOG_HEALTH = '[health]\nkind = "log"\npattern = "ready"\nstart_period_s = 0.05\n'
RETRY = '[restart]\npolicy = "on-failure"\nbackoff_s = 0.0\nmax_retries = 50\n'


@dataclass
class RecordingEscalation:
    events: list[tuple[Event, Decision]] = field(default_factory=list)

    def escalate(self, event: Event, decision: Decision, ctx: ServiceContext) -> None:
        self.events.append((event, decision))

    def texts(self) -> list[str]:
        return [e.text for e, _d in self.events if isinstance(e, LogLine)]


def make(tmp_path, *decls: ServiceDecl, **kw) -> tuple[Supervisor, RecordingEscalation]:
    kw.setdefault("reset_window_min_s", 0.3)
    kw.setdefault("eof_grace_s", 0.3)
    kw.setdefault("dep_poll_s", 0.05)
    esc = RecordingEscalation()
    sup = Supervisor(PlainSpawner(), escalation=esc, **kw)
    for d in decls:
        sup.add(d, tmp_path / d.id, {})
    return sup, esc


def pump(sup: Supervisor, *, until: Any = None, seconds: float = 8.0, step: float = 0.02) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        sup.run_once(step)
        if until is not None and until():
            return
    if until is not None:
        raise AssertionError(f"condition never met after {seconds}s")


def count_iterations(sup: Supervisor, seconds: float, step: float = 0.05) -> int:
    n = 0
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        sup.run_once(step)
        n += 1
    return n


def st_of(sup: Supervisor, sid: str) -> ServiceState:
    return sup.services[sid]


# ------------------------------------------------------------------- the gate


def test_a_dependent_waits_until_its_dependency_is_running_and_healthy(tmp_path):
    a = decl(SLOW_READY.format(delay=0.5), sid="a", extra=LOG_HEALTH)
    b = decl(IDLE, sid="b", depends_on=("a",))
    sup, esc = make(tmp_path, a, b)
    try:
        sup.start("a")
        sup.start("b")

        # b was parked, not spawned: no pid, no attempt consumed, no failure.
        sb = st_of(sup, "b")
        assert sb.status == "waiting"
        assert sb.waiting_for == ("a",)
        assert sb.spawned is None and sb.attempt == 0 and sb.consecutive_failures == 0
        assert sb.desired == "up", "waiting is an intent to run, not a stop"

        # a is up but NOT yet healthy -- and that is not good enough.
        pump(sup, until=lambda: st_of(sup, "a").spawned is not None)
        assert st_of(sup, "a").healthy is not True
        assert st_of(sup, "b").status == "waiting"

        pump(sup, until=lambda: st_of(sup, "b").spawned is not None)
        assert st_of(sup, "a").healthy is True, "b started before a was healthy"
        assert st_of(sup, "b").status == "running"
        assert st_of(sup, "b").waiting_for == ()
        assert st_of(sup, "b").attempt == 1
        assert esc.events == [], "a plain wait is not an escalation"
    finally:
        sup.shutdown(1.0)


def test_a_dependency_with_no_health_check_counts_as_ready_once_running(tmp_path):
    """``kind="none"`` has no probe to pass, so "up" is all it can ever report."""
    a = decl(IDLE, sid="a")  # health.kind defaults to "none"
    b = decl(IDLE, sid="b", depends_on=("a",))
    sup, _esc = make(tmp_path, a, b)
    try:
        sup.start("b")
        assert st_of(sup, "b").status == "waiting"
        sup.start("a")
        pump(sup, until=lambda: st_of(sup, "b").spawned is not None)
        assert st_of(sup, "a").healthy is True
        assert st_of(sup, "b").status == "running"
    finally:
        sup.shutdown(1.0)


def test_a_service_without_depends_on_is_not_gated(tmp_path):
    sup, _esc = make(tmp_path, decl(IDLE, sid="solo"))
    try:
        sup.start("solo")
        assert st_of(sup, "solo").status == "running"
        assert st_of(sup, "solo").waiting_for == ()
        assert st_of(sup, "solo").spawned is not None
    finally:
        sup.shutdown(1.0)


def test_a_restarting_dependent_re_waits_for_a_dependency_that_went_away(tmp_path):
    """The gate applies to every start, not just the first (crash + backoff)."""
    a = decl(SLOW_READY.format(delay=0.0), sid="a", extra=LOG_HEALTH)
    b = decl(IDLE, sid="b", depends_on=("a",), extra=RETRY)
    sup, _esc = make(tmp_path, a, b)
    try:
        sup.start("a")
        sup.start("b")
        pump(sup, until=lambda: st_of(sup, "b").spawned is not None)
        assert st_of(sup, "b").attempt == 1

        # Take the dependency down, then crash the dependent.
        sup.stop("a")
        pump(sup, until=lambda: st_of(sup, "a").spawned is None)
        pid = st_of(sup, "b").pid
        assert pid is not None
        os.kill(pid, signal.SIGKILL)
        pump(sup, until=lambda: st_of(sup, "b").status == "waiting")

        sb = st_of(sup, "b")
        assert sb.waiting_for == ("a",)
        assert sb.spawned is None
        assert sb.attempt == 1, "the gated start must not consume an attempt"

        # And it recovers on its own once the dependency is back.
        sup.start("a")
        pump(sup, until=lambda: st_of(sup, "b").spawned is not None, seconds=10.0)
        assert st_of(sup, "b").attempt == 2
    finally:
        sup.shutdown(1.0)


def test_a_dependency_that_dies_does_not_stop_its_dependents(tmp_path):
    """Start gating only: no stop cascade, no restart cascade (documented)."""
    a = decl(SLOW_READY.format(delay=0.0), sid="a", extra=LOG_HEALTH)
    b = decl(IDLE, sid="b", depends_on=("a",))
    sup, _esc = make(tmp_path, a, b)
    try:
        sup.start("a")
        sup.start("b")
        pump(sup, until=lambda: st_of(sup, "b").spawned is not None)
        b_pid = st_of(sup, "b").pid

        sup.kill("a")
        pump(sup, until=lambda: st_of(sup, "a").spawned is None)
        sup.run_once(0.05)
        assert st_of(sup, "b").status == "running"
        assert st_of(sup, "b").pid == b_pid, "b was restarted by its dependency dying"
    finally:
        sup.shutdown(1.0)


# ---------------------------------------------------------------- bad graphs


def test_an_unknown_dependency_waits_and_escalates_exactly_once(tmp_path):
    b = decl(IDLE, sid="b", depends_on=("nosuch",))
    sup, esc = make(tmp_path, b)
    try:
        sup.start("b")
        assert st_of(sup, "b").status == "waiting"
        assert st_of(sup, "b").waiting_for == ("nosuch",)
        # Many re-checks (dep_poll_s = 0.05), still one escalation.
        count_iterations(sup, 0.5)
        assert st_of(sup, "b").status == "waiting"
        assert len(esc.events) == 1, f"escalation storm: {esc.texts()}"
        text = esc.texts()[0]
        assert "nosuch" in text and "b" in text
        assert esc.events[0][0].severity is Severity.ERROR
    finally:
        sup.shutdown(1.0)


def test_a_late_registration_satisfies_a_dependency_that_was_unknown(tmp_path):
    """Waiting on an unregistered id is not fatal: a reload may add it later."""
    b = decl(IDLE, sid="b", depends_on=("a",))
    sup, esc = make(tmp_path, b)
    try:
        sup.start("b")
        assert st_of(sup, "b").status == "waiting"
        assert len(esc.events) == 1

        sup.add(decl(IDLE, sid="a"), tmp_path / "a", {})
        sup.start("a")
        pump(sup, until=lambda: st_of(sup, "b").spawned is not None)
        assert st_of(sup, "b").status == "running"
    finally:
        sup.shutdown(1.0)


def test_a_dependency_cycle_fails_both_members_and_escalates_once_each(tmp_path):
    a = decl(IDLE, sid="a", depends_on=("b",))
    b = decl(IDLE, sid="b", depends_on=("a",))
    sup, esc = make(tmp_path, a, b)
    try:
        sup.start("a")
        sup.start("b")
        assert st_of(sup, "a").status == "failed"
        assert st_of(sup, "b").status == "failed"
        assert st_of(sup, "a").spawned is None and st_of(sup, "b").spawned is None
        # No retry timer: a cycle cannot resolve itself, so waiting would hang.
        assert st_of(sup, "a").restart_at is None
        count_iterations(sup, 0.3)
        assert st_of(sup, "a").status == "failed"

        assert len(esc.events) == 2, f"expected one per member, got {esc.texts()}"
        assert all(e.severity is Severity.CRITICAL for e, _d in esc.events)
        assert all("cycle" in t for t in esc.texts())
    finally:
        sup.shutdown(1.0)


def test_a_three_service_cycle_is_detected(tmp_path):
    sup, esc = make(
        tmp_path,
        decl(IDLE, sid="a", depends_on=("b",)),
        decl(IDLE, sid="b", depends_on=("c",)),
        decl(IDLE, sid="c", depends_on=("a",)),
    )
    try:
        sup.start("a")
        assert st_of(sup, "a").status == "failed"
        assert "a -> b -> c -> a" in esc.texts()[0]
    finally:
        sup.shutdown(1.0)


def test_a_diamond_is_not_a_cycle(tmp_path):
    """b and c both depend on a, d depends on both: no cycle, everything starts."""
    ready = SLOW_READY.format(delay=0.0)
    sup, esc = make(
        tmp_path,
        decl(ready, sid="a", extra=LOG_HEALTH),
        decl(ready, sid="b", depends_on=("a",), extra=LOG_HEALTH),
        decl(ready, sid="c", depends_on=("a",), extra=LOG_HEALTH),
        decl(IDLE, sid="d", depends_on=("b", "c")),
    )
    try:
        for sid in ("d", "c", "b", "a"):  # deliberately the wrong order
            sup.start(sid)
        pump(sup, until=lambda: st_of(sup, "d").spawned is not None, seconds=10.0)
        assert all(st_of(sup, s).status == "running" for s in ("a", "b", "c", "d"))
        assert esc.events == []
    finally:
        sup.shutdown(1.0)


# ------------------------------------------------------------------ shutdown


def test_shutdown_stops_dependents_before_their_dependencies(tmp_path):
    order = tmp_path / "order.txt"
    a = decl(
        NOTE_ON_TERM.format(path=str(order), sid="a"),
        sid="a",
        extra=LOG_HEALTH + "[stop]\ntimeout_s = 2.0\n",
    )
    b = decl(
        NOTE_ON_TERM.format(path=str(order), sid="b"),
        sid="b",
        depends_on=("a",),
        extra=LOG_HEALTH + "[stop]\ntimeout_s = 2.0\n",
    )
    sup, _esc = make(tmp_path, a, b)
    sup.start("a")
    sup.start("b")
    pump(sup, until=lambda: st_of(sup, "b").healthy is True)

    sup.shutdown(3.0)
    assert order.read_text().split() == ["b", "a"], "a dependency was torn down first"


def test_shutdown_of_an_unrelated_fleet_is_unchanged(tmp_path):
    """No depends_on anywhere == one layer == the previous behaviour."""
    order = tmp_path / "order.txt"
    sup, _esc = make(
        tmp_path,
        decl(NOTE_ON_TERM.format(path=str(order), sid="x"), sid="x", extra=LOG_HEALTH),
        decl(NOTE_ON_TERM.format(path=str(order), sid="y"), sid="y", extra=LOG_HEALTH),
    )
    sup.start("x")
    sup.start("y")
    # Healthy, not merely spawned: "ready" is printed after the SIGTERM handler
    # is installed, so waiting for it is what makes the teardown observable.
    pump(sup, until=lambda: all(st_of(sup, s).healthy is True for s in ("x", "y")))
    sup.shutdown(3.0)
    assert sorted(order.read_text().split()) == ["x", "y"]


def test_shutdown_stops_a_service_parked_in_waiting(tmp_path):
    b = decl(IDLE, sid="b", depends_on=("nosuch",))
    sup, _esc = make(tmp_path, b)
    sup.start("b")
    assert st_of(sup, "b").status == "waiting"
    sup.shutdown(1.0)
    assert st_of(sup, "b").status == "stopped"
    assert st_of(sup, "b").desired == "down"
    assert st_of(sup, "b").waiting_for == ()


def test_a_cycle_does_not_hang_shutdown(tmp_path):
    sup, _esc = make(
        tmp_path,
        decl(IDLE, sid="a", depends_on=("b",)),
        decl(IDLE, sid="b", depends_on=("a",)),
    )
    sup.start("a")
    sup.start("b")
    began = time.monotonic()
    sup.shutdown(1.0)
    assert time.monotonic() - began < 5.0


# --------------------------------------------------------- loop hygiene, status


def test_waiting_never_spins_the_loop(tmp_path):
    """``_poll_timeout`` may only fold in deadlines ``_run_timers`` acts on.

    The wait deadline is always one poll interval in the future, so it can never
    clamp the timeout to zero -- the failure mode that produced two 100 %-CPU
    spins in review-1.
    """
    b = decl(IDLE, sid="b", depends_on=("nosuch",))
    sup, _esc = make(tmp_path, b, dep_poll_s=1.0)
    try:
        sup.start("b")
        assert st_of(sup, "b").status == "waiting"
        iterations = count_iterations(sup, 0.5)
        assert st_of(sup, "b").status == "waiting"
        assert iterations < 200, f"busy loop while waiting: {iterations} iterations in 0.5s"
    finally:
        sup.shutdown(1.0)


def test_a_failed_cycle_never_spins_the_loop(tmp_path):
    sup, _esc = make(
        tmp_path,
        decl(IDLE, sid="a", depends_on=("b",)),
        decl(IDLE, sid="b", depends_on=("a",)),
    )
    try:
        sup.start("a")
        iterations = count_iterations(sup, 0.5)
        assert iterations < 200, f"busy loop on a failed cycle: {iterations} iterations in 0.5s"
    finally:
        sup.shutdown(1.0)


def test_status_reports_waiting_and_waiting_for(tmp_path):
    a = decl(SLOW_READY.format(delay=0.5), sid="a", extra=LOG_HEALTH)
    b = decl(IDLE, sid="b", depends_on=("a", "nosuch"))
    sup, _esc = make(tmp_path, a, b)
    try:
        sup.start("b")
        row = sup.status()["b"]
        assert row["status"] == "waiting"
        assert row["waiting_for"] == ["a", "nosuch"]
        assert row["pid"] is None and row["uptime_s"] is None
        assert sup.status()["a"]["waiting_for"] == []
    finally:
        sup.shutdown(1.0)


def test_stopping_a_waiting_service_clears_the_wait(tmp_path):
    b = decl(IDLE, sid="b", depends_on=("nosuch",))
    sup, _esc = make(tmp_path, b)
    try:
        sup.start("b")
        assert st_of(sup, "b").status == "waiting"
        sup.stop("b")
        assert st_of(sup, "b").status == "stopped"
        assert st_of(sup, "b").desired == "down"
        assert st_of(sup, "b").waiting_for == ()
        assert st_of(sup, "b").restart_at is None
        count_iterations(sup, 0.3)
        assert st_of(sup, "b").status == "stopped", "a stopped service must stay stopped"
    finally:
        sup.shutdown(1.0)


@pytest.mark.parametrize("op", ["stop", "kill"])
def test_operator_intent_beats_a_pending_wait(tmp_path, op: str):
    b = decl(IDLE, sid="b", depends_on=("nosuch",))
    sup, _esc = make(tmp_path, b)
    try:
        sup.start("b")
        getattr(sup, op)("b")
        assert st_of(sup, "b").status == "stopped"
        assert st_of(sup, "b").waiting_for == ()
    finally:
        sup.shutdown(1.0)


def test_no_service_exits_while_waiting(tmp_path):
    """A parked service produces no events at all -- it was never spawned."""
    b = decl(IDLE, sid="b", depends_on=("nosuch",))
    sup, _esc = make(tmp_path, b)
    try:
        sup.start("b")
        events: list[Event] = []
        for _ in range(10):
            events += sup.run_once(0.02)
        assert [e for e in events if isinstance(e, ServiceExited)] == []
    finally:
        sup.shutdown(1.0)
