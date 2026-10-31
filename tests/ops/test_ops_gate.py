from ams.ops.config import GateConfig, parse
from ams.ops.gate import HealthGate, JobGate, LogGate
from ams.ops.observe.launchd import JobState

CFG = GateConfig()
H = 3600.0


def test_log_new_then_silent_then_back():
    g = LogGate({}, CFG)
    assert g.observe("s", "sig", "line", 0)[0] == "NEW"
    assert g.observe("s", "sig", "line", 10) is None
    assert g.observe("s", "sig", "line", 20) is None
    assert g.observe("s", "sig", "line", 13 * H)[0] == "BACK"  # silent > 12h


def test_log_flap_reports_once_per_cooldown():
    g = LogGate({}, CFG)
    assert g.observe("s", "sig", "x", 0)[0] == "NEW"
    # flapping right after NEW is not re-reported: NEW already told the story
    assert [g.observe("s", "sig", "x", 60 * i) for i in range(1, 8)] == [None] * 7
    # hours later the same cause flaps again: one FLAP report, then silence until the cooldown
    base = 7 * H
    got = [g.observe("s", "sig", "x", base + 60 * i) for i in range(0, 12)]
    assert [r for r in got if r] == [("FLAP", 5)]


def test_seeded_signature_is_not_new():
    g = LogGate({}, CFG)
    g.seed("s", "sig", "old", 0)
    assert g.observe("s", "sig", "old", 100) is None
    assert g.sigs["sig"]["base"] is True


def test_prune_drops_old_signatures():
    g = LogGate({}, CFG)
    g.observe("s", "a", "x", 0)
    g.observe("s", "b", "x", 20 * 86400)
    assert g.prune(20 * 86400) == 1 and "a" not in g.sigs


def svc(expect="running"):
    return parse(
        {"service": [{"id": "asr", "launchd": "gui/501/com.asr.service", "expect": expect}]}
    ).services[0]


def up(runs=1, pid=10):
    return JobState(True, "running", pid, runs, 0)


def test_job_down_has_one_tick_grace_and_repeat_interval():
    g = JobGate({}, CFG)
    s = svc()
    down = JobState(True, "not running", None, 1, -9)
    assert g.check(s, down, 0) == []  # first tick: maybe mid-restart
    inc = g.check(s, down, 30)
    assert [i.kind for i in inc] == ["job_down"] and inc[0].facts["last_exit"] == -9
    assert g.check(s, down, 60) == []  # not repeated within job_repeat_s
    assert [i.kind for i in g.check(s, down, 30 + CFG.job_repeat_s)] == ["job_down"]


def test_job_recovery_clears_state():
    g = JobGate({}, CFG)
    s = svc()
    down = JobState(False, detail="Could not find service")
    g.check(s, down, 0)
    g.check(s, down, 30)
    assert g.check(s, up(), 60) == []
    assert "down_since" not in g.jobs["asr"]
    g.check(s, down, 90)
    assert g.check(s, down, 120)[0].kind == "job_down"  # fresh episode reports again


def test_restart_storm():
    g = JobGate({}, CFG)
    s = svc()
    assert g.check(s, up(runs=1), 0) == []
    assert g.check(s, up(runs=2), 100) == []
    inc = g.check(s, up(runs=4, pid=11), 200)
    assert [i.kind for i in inc] == ["job_flap"] and inc[0].facts["restarts"] == 3
    assert g.check(s, up(runs=6), 300) == []  # cooldown


def test_slow_restarts_are_not_a_storm():
    g = JobGate({}, CFG)
    s = svc()
    for i, t in enumerate(range(0, 5 * 3600, 3600)):
        assert g.check(s, up(runs=i + 1), t) == []


def test_interval_job_reports_each_new_failed_run_once():
    g = JobGate({}, CFG)
    s = svc("interval")
    bad = JobState(True, "not running", None, 5, 1)
    assert [i.kind for i in g.check(s, bad, 0)] == ["job_exit"]
    assert g.check(s, bad, 30) == []
    assert g.check(s, JobState(True, "not running", None, 6, 0), 60) == []
    assert [i.kind for i in g.check(s, JobState(True, "not running", None, 7, 1), 90)] == [
        "job_exit"
    ]


def test_observe_only_never_opens_job_incidents():
    s = parse({"service": [{"id": "x"}]}).services[0]
    assert JobGate({}, CFG).check(s, JobState(False), 0) == []


def test_health_needs_consecutive_failures():
    h = HealthGate({}, CFG)
    s = svc()
    assert h.check(s, False, "HTTP 503", 0) is None
    assert h.check(s, True, "", 30) is None  # reset
    assert h.check(s, False, "x", 60) is None and h.check(s, False, "x", 90) is None
    inc = h.check(s, False, "ConnectionRefused", 120)
    assert inc and inc.kind == "health" and inc.facts["consecutive_failures"] == 3
    assert h.check(s, False, "x", 150) is None
