import io
import json

from ams.decision import (
    Action,
    DecisionPolicy,
    DefaultPolicy,
    Escalation,
    JsonLinesEscalation,
    NullEscalation,
    ServiceContext,
)
from ams.events import HealthChanged, LogLine, ServiceExited, Severity
from ams.schema import loads


def decl(restart_policy="on-failure", max_retries=2):
    return loads(
        f'id="svc"\n[start]\nargv=["x"]\n[restart]\npolicy="{restart_policy}"\nmax_retries={max_retries}'
    )


def test_protocols_are_satisfied_by_stubs():
    assert isinstance(DefaultPolicy(), DecisionPolicy)
    assert isinstance(JsonLinesEscalation(io.StringIO()), Escalation)
    assert isinstance(NullEscalation(), Escalation)


def test_log_lines_escalate_at_threshold():
    pol = DefaultPolicy()
    ctx = ServiceContext(decl())
    info = LogLine("svc", "stdout", "ok", Severity.INFO)
    warn = LogLine("svc", "stderr", "careful", Severity.WARNING)
    err = LogLine("svc", "stderr", "boom", Severity.ERROR)
    assert pol.decide(info, ctx).action == Action.LOG
    assert pol.decide(warn, ctx).action == Action.ESCALATE
    assert pol.decide(err, ctx).action == Action.ESCALATE
    assert DefaultPolicy(escalate_at=Severity.ERROR).decide(warn, ctx).action == Action.LOG


def test_exit_follows_restart_policy():
    pol = DefaultPolicy()
    crash = ServiceExited("svc", 1, 1, None, 5.0)
    clean = ServiceExited("svc", 1, 0, None, 5.0)

    assert pol.decide(crash, ServiceContext(decl("on-failure"))).action == Action.RESTART
    assert pol.decide(clean, ServiceContext(decl("on-failure"))).action == Action.STOP
    assert pol.decide(clean, ServiceContext(decl("always"))).action == Action.RESTART
    assert pol.decide(crash, ServiceContext(decl("never"))).action == Action.STOP
    # give up after max_retries consecutive failures
    ctx = ServiceContext(decl("always", max_retries=2), consecutive_failures=2)
    d = pol.decide(crash, ctx)
    assert d.action == Action.STOP and "max_retries" in d.reason


def test_health_and_other_events():
    pol = DefaultPolicy()
    ctx = ServiceContext(decl())
    assert pol.decide(HealthChanged("svc", healthy=False), ctx).action == Action.ESCALATE
    assert pol.decide(HealthChanged("svc", healthy=True), ctx).action == Action.LOG


def test_jsonlines_escalation_writes_one_record_per_event():
    buf = io.StringIO()
    sink = JsonLinesEscalation(buf)
    ctx = ServiceContext(decl(), attempt=3, consecutive_failures=1)
    ev = LogLine("svc", "stderr", "boom", Severity.ERROR, ts=1.0)
    sink.escalate(ev, DefaultPolicy().decide(ev, ctx), ctx)
    lines = buf.getvalue().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["kind"] == "LogLine" and rec["service_id"] == "svc"
    assert rec["action"] == "escalate" and rec["attempt"] == 3
    assert rec["event"]["text"] == "boom" and rec["event"]["severity"] == 40
