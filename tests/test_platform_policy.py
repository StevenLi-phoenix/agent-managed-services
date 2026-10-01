"""T3.3: the platform decision policy.

Everything here drives synthetic event streams through `PlatformPolicy` with an
injected clock, so "after ten minutes" is a variable assignment rather than a
sleep. The last test is the invariant the whole module exists for: over a seeded
500-event stream, no cause escalates twice inside one window.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass

import pytest

from ams.decision import Action, Decision, DefaultPolicy, ServiceContext
from ams.events import HealthChanged, LogLine, ServiceExited, Severity, classify
from ams.platform.policy import (
    EscalationDeduper,
    PlatformPolicy,
    cause_key,
    cause_key_for_event,
    make_policy,
    normalize_cause_text,
)
from ams.schema import loads
from ams.state import StateDir

# --------------------------------------------------------------------- helpers


class FakeClock:
    """A clock the tests move by hand. `policy.clock` is `Callable[[], float]`."""

    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += float(seconds)
        return self.now


def decl(service_id: str = "svc", **kw: object):
    health = kw.pop("health", "")
    restart = kw.pop("restart", 'policy="on-failure"\nmax_retries=5')
    text = f'id="{service_id}"\n[start]\nargv=["x"]\n[restart]\n{restart}\n{health}'
    return loads(text)


def ctx_for(service_id: str = "svc", **kw: object) -> ServiceContext:
    failures = int(kw.pop("consecutive_failures", 0))
    return ServiceContext(decl(service_id, **kw), consecutive_failures=failures)


def line(text: str, service_id: str = "svc", severity: Severity = Severity.ERROR) -> LogLine:
    return LogLine(service_id, "stderr", text, severity)


def caddy_line(payload: dict) -> LogLine:
    """A Caddy log line as the supervisor produces it: JSON, classified by the hint."""
    text = json.dumps(payload)
    return LogLine("caddy", "stderr", text, classify(text, "stderr", "json"))


@dataclass
class Recorder:
    """An `Escalation` sink that keeps what it was handed."""

    records: list[tuple[object, Decision, ServiceContext]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.records = []

    def escalate(self, event, decision, ctx) -> None:  # noqa: ANN001
        self.records.append((event, decision, ctx))


# --------------------------------------------------------------- normalization

NORMALIZATION_CASES = [
    pytest.param(
        "2026-09-02T13:04:07Z worker pid=1234 died on port 20100",
        "2026-09-02T13:09:11.5Z worker pid=5678 died on port 20431",
        True,
        id="timestamp-pid-port",
    ),
    pytest.param(
        "127.0.0.1:54312 - GET /health",
        "127.0.0.1:41999 - GET /health",
        True,
        id="loopback-ephemeral-port",
    ),
    pytest.param(
        "checked out 9f1c0b2e4a6d8f0011223344556677889900aabb",
        "checked out 1122334455667788990011223344556677889900",
        True,
        id="git-sha-all-digits-vs-mixed",
    ),
    pytest.param(
        "checked out 9f1c0b2e4a6d8f0011223344556677889900aabb",
        "checked out ffeeddccbbaa00998877665544332211aabbccdd",
        True,
        id="git-sha",
    ),
    pytest.param(
        "[02/Sep/2026 10:11:12] request done",
        "[02/Sep/2026 11:59:00] request done",
        True,
        id="clf-date",
    ),
    pytest.param(
        "connection refused to registry",
        "connection refused to auth",
        False,
        id="different-cause-stays-different",
    ),
    pytest.param(
        "retried 3 times",
        "retried 5 times",
        False,
        id="small-numbers-are-content",
    ),
]


@pytest.mark.parametrize("first,second,collapse", NORMALIZATION_CASES)
def test_normalization(first: str, second: str, collapse: bool) -> None:
    same = normalize_cause_text(first) == normalize_cause_text(second)
    assert same is collapse, (normalize_cause_text(first), normalize_cause_text(second))


def test_cause_key_separates_service_and_kind() -> None:
    assert cause_key("a", "LogLine", "boom") != cause_key("b", "LogLine", "boom")
    assert cause_key("a", "LogLine", "boom") != cause_key("a", "ServiceExited", "boom")


def test_exit_cause_key_ignores_uptime_and_pid() -> None:
    a = cause_key_for_event(ServiceExited("svc", 111, 1, None, 0.4))
    b = cause_key_for_event(ServiceExited("svc", 222, 1, None, 93.2))
    assert a == b


def test_normalized_text_is_bounded() -> None:
    assert len(normalize_cause_text("x" * 10_000)) <= 240


# -------------------------------------------------------------------- deduper


def test_deduper_window_then_summary() -> None:
    dedup = EscalationDeduper(window_s=100.0)
    verdicts = [dedup.observe("k", 0.0 + i) for i in range(5)]
    assert [v.first for v in verdicts] == [True, False, False, False, False]
    assert [v.count for v in verdicts] == [1, 2, 3, 4, 5]
    assert dedup.expire(50.0) == []  # window still open
    summaries = dedup.expire(200.0)
    assert len(summaries) == 1
    assert summaries[0].count == 5
    assert "repeated 5 times in window" in summaries[0].text
    # The window closed, so the cause is re-armed.
    assert dedup.observe("k", 201.0).first is True


def test_deduper_single_occurrence_makes_no_summary() -> None:
    dedup = EscalationDeduper(window_s=10.0)
    dedup.observe("k", 0.0)
    assert dedup.expire(100.0) == []


def test_deduper_rolls_over_without_an_explicit_expire() -> None:
    dedup = EscalationDeduper(window_s=10.0)
    dedup.observe("k", 0.0)
    dedup.observe("k", 1.0)
    assert dedup.observe("k", 20.0).first is True  # window expired lazily
    summaries = dedup.expire(20.0)
    assert [s.count for s in summaries] == [2]


def test_deduper_rejects_a_non_positive_window() -> None:
    with pytest.raises(ValueError):
        EscalationDeduper(window_s=0)


# --------------------------------------------------------------------- dedupe


def test_same_cause_escalates_once_then_summarizes() -> None:
    clock = FakeClock()
    policy = PlatformPolicy(clock=clock, window_s=600.0)
    ctx = ctx_for()
    actions, reasons = [], []
    for i in range(5):
        d = policy.decide(line(f"ERROR db: pool exhausted after {1000 + i} ms"), ctx)
        actions.append(d.action)
        reasons.append(d.reason)
        clock.advance(2.0)

    assert actions == [Action.ESCALATE] + [Action.LOG] * 4
    assert all(r.startswith("deduped (n=") for r in reasons[1:])
    assert "n=5" in reasons[-1]

    assert policy.flush() == []  # window still open
    clock.advance(700.0)
    summaries = policy.flush()
    assert len(summaries) == 1
    assert "repeated 5 times in window" in summaries[0].event.text
    assert summaries[0].decision.action is Action.ESCALATE


def test_window_expiry_re_escalates() -> None:
    clock = FakeClock()
    policy = PlatformPolicy(clock=clock, window_s=100.0)
    ctx = ctx_for()
    assert policy.decide(line("ERROR boom"), ctx).action is Action.ESCALATE
    clock.advance(50.0)
    assert policy.decide(line("ERROR boom"), ctx).action is Action.LOG
    clock.advance(60.0)
    assert policy.decide(line("ERROR boom"), ctx).action is Action.ESCALATE


def test_two_lines_differing_only_in_pid_collapse_to_one_cause() -> None:
    clock = FakeClock()
    policy = PlatformPolicy(clock=clock)
    ctx = ctx_for()
    a = "2026-09-02T13:04:07Z ERROR w: worker pid=1234 lost 127.0.0.1:20100"
    b = "2026-09-02T13:05:31Z ERROR w: worker pid=5678 lost 127.0.0.1:20431"
    assert policy.decide(line(a), ctx).action is Action.ESCALATE
    assert policy.decide(line(b), ctx).action is Action.LOG


def test_distinct_causes_each_escalate() -> None:
    policy = PlatformPolicy(clock=FakeClock())
    ctx = ctx_for()
    assert policy.decide(line("ERROR disk full"), ctx).action is Action.ESCALATE
    assert policy.decide(line("ERROR dns failure"), ctx).action is Action.ESCALATE


def test_the_same_text_from_two_services_escalates_twice() -> None:
    policy = PlatformPolicy(clock=FakeClock())
    assert policy.decide(line("ERROR boom", "a"), ctx_for("a")).action is Action.ESCALATE
    assert policy.decide(line("ERROR boom", "b"), ctx_for("b")).action is Action.ESCALATE


def test_flush_emits_through_the_sink_when_one_is_set() -> None:
    clock = FakeClock()
    sink = Recorder()
    policy = PlatformPolicy(clock=clock, window_s=10.0, escalation=sink)
    ctx = ctx_for()
    policy.decide(line("ERROR boom"), ctx)
    policy.decide(line("ERROR boom"), ctx)
    clock.advance(20.0)
    emitted = policy.flush()
    assert len(emitted) == 1
    assert len(sink.records) == 1


# ------------------------------------------------------------- delegation intact


def test_expected_exit_still_logs() -> None:
    policy = PlatformPolicy(clock=FakeClock())
    d = policy.decide(ServiceExited("svc", 1, 143, None, 3.0, expected=True), ctx_for())
    assert d.action is Action.LOG
    assert "expected exit" in d.reason


def test_unexpected_exit_still_restarts_and_is_never_deduped_away() -> None:
    policy = PlatformPolicy(clock=FakeClock())
    ctx = ctx_for()
    for _ in range(4):
        d = policy.decide(ServiceExited("svc", 1, 1, None, 3.0), ctx)
        assert d.action is Action.RESTART


def test_exit_past_max_retries_still_stops() -> None:
    policy = PlatformPolicy(clock=FakeClock())
    ctx = ctx_for(restart='policy="always"\nmax_retries=2', consecutive_failures=2)
    assert policy.decide(ServiceExited("svc", 1, 1, None, 3.0), ctx).action is Action.STOP


def test_self_probe_suppression_survives() -> None:
    policy = PlatformPolicy(clock=FakeClock())
    health = '[health]\nkind="http"\nport="main"\npath="/health"\n[ports]\nmain=0\n'
    ctx = ServiceContext(loads(f'id="svc"\n[start]\nargv=["x"]\n{health}'))
    probe = line('127.0.0.1:54312 - "GET /health HTTP/1.1" 200 OK', severity=Severity.INFO)
    d = policy.decide(probe, ctx)
    assert d.action is Action.SUPPRESS
    assert d.reason == DefaultPolicy().decide(probe, ctx).reason


def test_info_lines_still_just_log() -> None:
    policy = PlatformPolicy(clock=FakeClock())
    assert policy.decide(line("all good", severity=Severity.INFO), ctx_for()).action is Action.LOG


# ---------------------------------------------------------------------- caddy

CADDY_CASES = [
    pytest.param(
        {
            "logger": "http.log.access",
            "level": "info",
            "status": 200,
            "request": {"uri": "/files/health"},
        },
        Action.SUPPRESS,
        id="access-200",
    ),
    pytest.param(
        {"logger": "http.log.access", "level": "info", "status": 404, "request": {"uri": "/nope"}},
        Action.SUPPRESS,
        id="access-404",
    ),
    pytest.param(
        {
            "logger": "http.log.access",
            "level": "error",
            "status": 502,
            "request": {"uri": "/kv/get"},
        },
        Action.ESCALATE,
        id="access-502",
    ),
    pytest.param(
        {"level": "warn", "logger": "tls", "msg": "stapling OCSP: no OCSP server specified"},
        Action.SUPPRESS,
        id="tls-warn",
    ),
    pytest.param(
        {"level": "warn", "logger": "tls.issuance", "msg": "could not get certificate from issuer"},
        Action.SUPPRESS,
        id="certificate-warn",
    ),
    # The other two warnings a live Caddy emits every start/stop (T4.1, seen on
    # racknerd): both are statements about a setting the operator chose.
    pytest.param(
        {"level": "warn", "logger": "admin", "msg": "admin endpoint disabled"},
        Action.SUPPRESS,
        id="admin-off-warn",
    ),
    pytest.param(
        {"level": "warn", "msg": "exiting; byeee!! \U0001f44b", "signal": "SIGTERM"},
        Action.SUPPRESS,
        id="sigterm-goodbye-warn",
    ),
    # ...but a real admin-API failure on the same logger still escalates.
    pytest.param(
        {"level": "error", "logger": "admin", "msg": "admin endpoint failed to start"},
        Action.ESCALATE,
        id="admin-error-falls-through",
    ),
    pytest.param(
        {"level": "error", "logger": "http", "msg": "upstream dial failed"},
        Action.ESCALATE,
        id="non-tls-error-falls-through",
    ),
    pytest.param(
        {"level": "info", "logger": "http", "msg": "server running"},
        Action.LOG,
        id="info-falls-through",
    ),
]


@pytest.mark.parametrize("payload,expected", CADDY_CASES)
def test_caddy_rules(payload: dict, expected: Action) -> None:
    policy = PlatformPolicy(clock=FakeClock())
    assert policy.decide(caddy_line(payload), ctx_for("caddy")).action is expected


def test_caddy_5xx_escalates_once_per_path_and_status() -> None:
    clock = FakeClock()
    policy = PlatformPolicy(clock=clock)
    ctx = ctx_for("caddy")

    def access(uri: str, status: int, remote: str) -> LogLine:
        return caddy_line(
            {
                "logger": "http.log.access",
                "level": "error",
                "status": status,
                "ts": clock.now,
                "request": {"uri": uri, "remote_ip": remote},
            }
        )

    # Same path + status, different request ids/ports/timestamps: one cause.
    first = policy.decide(access("/kv/get", 502, "10.0.0.1:5001"), ctx)
    clock.advance(1.0)
    second = policy.decide(access("/kv/get", 502, "10.0.0.9:6123"), ctx)
    assert (first.action, second.action) == (Action.ESCALATE, Action.LOG)
    # A different status on the same path is a different cause.
    assert policy.decide(access("/kv/get", 503, "10.0.0.1:5001"), ctx).action is Action.ESCALATE
    # A different path with the same status is a different cause.
    assert policy.decide(access("/files/x", 502, "10.0.0.1:5001"), ctx).action is Action.ESCALATE


def test_caddy_malformed_json_falls_through_to_the_default() -> None:
    policy = PlatformPolicy(clock=FakeClock())
    ctx = ctx_for("caddy")
    broken = LogLine("caddy", "stderr", '{"logger": "http.log.access", "status": ', Severity.ERROR)
    assert policy.decide(broken, ctx).action is Action.ESCALATE
    banner = LogLine("caddy", "stderr", "ERROR loading config: bad brace", Severity.ERROR)
    assert policy.decide(banner, ctx).action is Action.ESCALATE


def test_caddy_access_without_a_usable_status_falls_through() -> None:
    policy = PlatformPolicy(clock=FakeClock())
    ctx = ctx_for("caddy")
    payload = {"logger": "http.log.access", "level": "error", "status": True}
    assert policy.decide(caddy_line(payload), ctx).action is Action.ESCALATE


def test_caddy_rules_do_not_apply_to_other_services() -> None:
    """A non-caddy service printing an access-shaped JSON object is not special-cased."""
    policy = PlatformPolicy(clock=FakeClock())
    text = json.dumps({"logger": "http.log.access", "level": "error", "status": 200})
    event = LogLine("files", "stderr", text, classify(text, "stderr", "json"))
    assert policy.decide(event, ctx_for("files")).action is Action.ESCALATE


# --------------------------------------------------------------- the invariant


def test_no_cause_escalates_twice_in_a_window() -> None:
    """The property the whole module exists for, over a seeded 500-event stream.

    Every escalation the policy returns or queues is bucketed by its cause key
    and its timestamp; two escalations of one key inside `window_s` is a failure.
    Distinct causes are also checked to still get through, so "escalate nothing"
    cannot pass this test.
    """
    rng = random.Random(20260902)
    clock = FakeClock()
    window = 300.0
    policy = PlatformPolicy(clock=clock, window_s=window)
    services = ("files", "kvservice", "caddy", "registry", "auth")

    escalated_at: dict[str, list[float]] = {}

    def note(key: str, at: float) -> None:
        previous = escalated_at.setdefault(key, [])
        assert not previous or at - previous[-1] >= window, (
            f"{key} escalated twice inside one window: {previous[-1]} then {at}"
        )
        previous.append(at)

    for _ in range(500):
        clock.advance(rng.uniform(0.0, 12.0))
        service = rng.choice(services)
        ctx = ctx_for(service, consecutive_failures=rng.choice((0, 0, 0, 2)))
        roll = rng.random()
        if roll < 0.35:
            event = line(f"ERROR {service}: pool exhausted pid={rng.randrange(9999)}", service)
        elif roll < 0.55:
            event = line(
                f"ERROR {service}: upstream {rng.choice(('a', 'b', 'c'))} unreachable", service
            )
        elif roll < 0.70:
            event = (
                caddy_line(
                    {
                        "logger": "http.log.access",
                        "level": "error",
                        "status": rng.choice((200, 404, 500, 502)),
                        "request": {"uri": rng.choice(("/kv/get", "/files/x"))},
                    }
                )
                if service == "caddy"
                else line(f"WARNING {service}: slow query", service, Severity.WARNING)
            )
        elif roll < 0.85:
            event = ServiceExited(service, rng.randrange(9999), 1, None, rng.uniform(0.1, 9.0))
        else:
            event = HealthChanged(service, healthy=rng.random() < 0.5, detail="probe")

        decision = policy.decide(event, ctx)
        if decision.action is Action.ESCALATE:
            # Re-derive the key the same way the policy does, including the
            # caddy access override.
            key = _key_of(policy, event, decision)
            note(key, clock.now)
        for pending in policy.flush():
            note(cause_key_for_event(pending.event, "synthetic"), clock.now)

    # The stream must not have been silently swallowed.
    assert len(escalated_at) >= 5
    assert sum(len(v) for v in escalated_at.values()) >= 10


def _key_of(policy: PlatformPolicy, event, decision) -> str:  # noqa: ANN001
    """The cause key the policy used, mirrored for the invariant test."""
    if isinstance(event, LogLine) and event.service_id == policy.caddy_service_id:
        obj = json.loads(event.text) if event.text.startswith("{") else {}
        if obj.get("logger") == "http.log.access":
            uri = obj.get("request", {}).get("uri", "-")
            return cause_key(event.service_id, f"caddy-access-{obj.get('status')}", uri)
    return cause_key_for_event(event)


# ------------------------------------------------------------------- cli wiring


def test_build_supervisor_accepts_the_platform_policy(tmp_path) -> None:  # noqa: ANN001
    from ams.cli import build_supervisor

    state = StateDir(tmp_path)
    service_dir = state.service_dir("hello")
    service_dir.mkdir(parents=True)
    (service_dir / "service.toml").write_text(
        'id = "hello"\n[start]\nargv = ["/bin/sleep", "30"]\n', encoding="utf-8"
    )
    policy = make_policy()
    asm = build_supervisor(state, isolation=False, policy=policy)
    assert asm.supervisor.policy is policy
    assert "hello" in asm.registered


def test_ams_run_with_the_platform_policy_starts_a_service(tmp_path) -> None:  # noqa: ANN001
    """DoD #2: `ams run --policy platform --no-isolation` in a subprocess."""
    import os
    import subprocess
    import sys

    state = StateDir(tmp_path / "state")
    service_dir = state.service_dir("hello")
    service_dir.mkdir(parents=True)
    (service_dir / "service.toml").write_text(
        'id = "hello"\n[start]\nargv = ["/bin/sleep", "30"]\n[stop]\ntimeout_s = 1.0\n',
        encoding="utf-8",
    )
    env = dict(os.environ)
    env.pop("AMS_STATE_DIR", None)
    env["PYTHONPATH"] = str(Path_repo() / "src") + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "ams",
            "run",
            "--policy",
            "platform",
            "--no-isolation",
            "--state-dir",
            str(state.root),
            "--log-level",
            "INFO",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
        proc.terminate()
        out, err = proc.communicate(timeout=15)
    finally:
        if proc.poll() is None:  # pragma: no cover - only on a wedged child
            proc.kill()
            proc.communicate()
    assert "started hello" in err, err
    assert "platform policy" in err, err
    assert proc.returncode == 0, (proc.returncode, err)


def Path_repo():
    from pathlib import Path

    return Path(__file__).resolve().parent.parent
