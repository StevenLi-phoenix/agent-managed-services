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


def http_decl(path="/health"):
    return loads(
        'id="svc"\n[start]\nargv=["x"]\n[ports]\nmain=0\n'
        f'[health]\nkind="http"\nport="main"\npath="{path}"\n'
    )


def test_an_operator_initiated_exit_is_logged_not_acted_on():
    """A reload/stop used to look exactly like a crash to the policy."""
    pol = DefaultPolicy()
    ctx = ServiceContext(decl("never"))
    crash = ServiceExited("svc", 1, exit_code=None, signal=15, uptime_s=3.0)
    stop = ServiceExited("svc", 1, exit_code=None, signal=15, uptime_s=3.0, expected=True)
    assert pol.decide(crash, ctx).action == Action.STOP
    d = pol.decide(stop, ctx)
    assert d.action == Action.LOG and "expected" in d.reason
    # the restart decision for an unexpected exit is untouched
    assert pol.decide(crash, ServiceContext(decl("on-failure"))).action == Action.RESTART


UVICORN_OK = 'INFO:     127.0.0.1:54312 - "GET /health HTTP/1.1" 200 OK'
STDLIB_OK = '127.0.0.1 - - [02/Sep/2026 10:11:12] "GET /health HTTP/1.1" 200 -'


def test_the_harness_own_health_probe_access_lines_are_suppressed():
    pol = DefaultPolicy()
    ctx = ServiceContext(http_decl())
    for text in (UVICORN_OK, STDLIB_OK):
        line = LogLine("svc", "stdout", text, Severity.INFO)
        d = pol.decide(line, ctx)
        assert d.action == Action.SUPPRESS, text
    # 3xx counts as a working probe too
    redirect = LogLine("svc", "stdout", UVICORN_OK.replace("200 OK", "301 Moved"), Severity.INFO)
    assert pol.decide(redirect, ctx).action == Action.SUPPRESS


def test_only_loopback_2xx_3xx_on_the_declared_health_path_is_suppressed():
    pol = DefaultPolicy()
    ctx = ServiceContext(http_decl())
    cases = {
        "500 on the health path": UVICORN_OK.replace("200 OK", "500 Internal Server Error"),
        "another path": UVICORN_OK.replace("/health", "/admin"),
        "a remote client": UVICORN_OK.replace("127.0.0.1", "10.0.0.9"),
        "a POST": UVICORN_OK.replace("GET", "POST"),
    }
    for why, text in cases.items():
        line = LogLine("svc", "stdout", text, Severity.INFO)
        assert pol.decide(line, ctx).action != Action.SUPPRESS, why


def test_self_probe_suppression_needs_an_http_health_check_and_can_be_turned_off():
    line = LogLine("svc", "stdout", UVICORN_OK, Severity.INFO)
    # no http health check declared: the harness is not the one making the request
    assert DefaultPolicy().decide(line, ServiceContext(decl())).action == Action.LOG
    off = DefaultPolicy(suppress_self_probes=False)
    assert off.decide(line, ServiceContext(http_decl())).action == Action.LOG
