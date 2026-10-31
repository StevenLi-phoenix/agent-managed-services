"""`ams watch journal`: the operator's gate on the escalation journal.

It generalises DSH's ga-watch (a Monitor that reads the journal, signs each cause, and prints a line
only when a cause is new, back after a long silence, or flapping). The global-awareness behaviour is
frozen in `oracle_ga_watch.py`; with the global-awareness rules the generalised watcher must print
exactly what the oracle printed, line for line.
"""

import importlib.util
import json
import random
from pathlib import Path

import pytest

from ams.ops.journal import JournalWatcher, parse_rules

HERE = Path(__file__).parent
GA = "global-awareness"
H = 3600.0

GA_RULES = parse_rules(
    {
        "ignore_raw_prefixes": ["repeated "],
        "rule": [
            {
                "service": GA,
                "level": "warn",
                "msg": [
                    "dropped invalid upstream items",
                    "stream ended by upstream",
                    "upstream http error",
                    "gdelt-geo: newest slots all unavailable, walking back",
                ],
                "msg_prefix": ["gdelt-geo: slot unavailable"],
                "msg_contains": ["ADMIN_OPEN=1"],
            }
        ],
        "known_missing": [
            {
                "service": GA,
                "msg": "source failed",
                "src_prefix": ["cloudflare-radar", "windy-webcams", "openaq"],
                "error_contains": ["unset", "KEY", "TOKEN"],
            }
        ],
    }
)


def record(text, reason="WARNING on stderr", service=GA):
    return json.dumps(
        {"kind": "LogLine", "service_id": service, "reason": reason, "event": {"text": text}}
    )


def jl(**kw):
    return json.dumps(kw)


@pytest.fixture
def oracle(tmp_path):
    spec = importlib.util.spec_from_file_location("oracle_ga_watch", HERE / "oracle_ga_watch.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.STATE = str(tmp_path / "oracle-state.json")
    mod.LOG = str(tmp_path / "oracle.jsonl")
    Path(mod.LOG).write_text("")
    mod.emitted = []
    mod.emit = lambda text: None if mod._QUIET else mod.emitted.append(text)  # the real one prints
    mod.clock = [0.0]
    mod.now = lambda: mod.clock[0]
    return mod


def replay(oracle, tmp_path, events, journal_lines_before=()):
    """Feed the same (time, record) sequence to the oracle and to the new watcher."""
    old = Path(oracle.LOG)
    old.write_text("".join(ln + "\n" for ln in journal_lines_before))
    new_journal = tmp_path / "new.jsonl"
    new_journal.write_text("".join(ln + "\n" for ln in journal_lines_before))
    clock = [0.0]
    out = []
    watcher = JournalWatcher(GA_RULES, tmp_path / "new-state.json", out.append, lambda: clock[0])
    oracle.bootstrap()
    watcher.bootstrap(new_journal)
    for t, line in events:
        oracle.clock[0] = clock[0] = t
        oracle.handle(line)
        watcher.handle(line)
    return oracle.emitted, out


def test_the_ga_rules_print_exactly_what_ga_watch_printed(oracle, tmp_path):
    warn = lambda msg, **kw: jl(level="warn", msg=msg, **kw)  # noqa: E731
    events = [
        (1000, record(warn("dropped invalid upstream items", src="opensky", kept=4000))),
        (1010, record(warn("stream ended by upstream", src="wikipedia"))),
        (1020, record(warn("upstream http error", src="gdelt"))),
        (1030, record(warn("gdelt-geo: slot unavailable for 20261031"))),
        (1040, record(jl(level="warn", msg="ADMIN_OPEN=1 is set"))),
        (1050, record("repeated 12 times in window: dropped invalid upstream items", "dedupe")),
        (
            1060,
            record(jl(level="error", msg="source failed", src="openaq-latest", error="KEY unset")),
        ),
        (1070, record(jl(level="error", msg="source failed", src="gdelt-events", error="timeout"))),
        (1080, record(jl(level="error", msg="source failed", src="gdelt-events", error="timeout"))),
        (1090, record("Traceback (most recent call last):")),
        (1100, record("plain text line that is not json")),
        (
            1110,
            record(jl(level="error", msg="boom", error={"message": "x" * 400}, cron="*/5 * * * *")),
        ),
    ]
    # the same cause comes back after silence, then flaps
    events += [
        (
            1000 + 13 * H,
            record(jl(level="error", msg="source failed", src="gdelt-events", error="t")),
        )
    ]
    base = 30 * H
    events += [
        (base + 60 * i, record(jl(level="error", msg="flappy", src="x"))) for i in range(0, 14)
    ]
    events += [
        (base + 7 * H + 60 * i, record(jl(level="error", msg="flappy", src="x")))
        for i in range(0, 8)
    ]
    events += [(base + 20 * H, record(jl(level="error", msg="flappy", src="x")))]
    old, new = replay(oracle, tmp_path, events)
    assert new == old
    assert len(old) >= 6  # the sequence really exercises NEW, BACK and FLAP


def test_a_journal_that_already_holds_history_starts_quiet_in_both(oracle, tmp_path):
    history = [record(jl(level="error", msg=f"old {i}", src="s")) for i in range(5)]
    events = [
        (500, record(jl(level="error", msg="old 1", src="s"))),
        (510, record(jl(level="error", msg="fresh", src="s"))),
    ]
    old, new = replay(oracle, tmp_path, events, history)
    assert new == old and len(old) == 1 and "fresh" in old[0]


def test_randomised_traffic_matches_the_oracle(oracle, tmp_path):
    rng = random.Random(7)
    msgs = [
        record(jl(level="warn", msg="dropped invalid upstream items", src="a")),
        record(
            jl(level="error", msg="source failed", src=rng.choice(["openaq-x", "gdelt"]), error="e")
        ),
        record(jl(level="warn", msg="stream ended by upstream")),
        record(jl(level="error", msg="a", src="s1")),
        record(jl(level="error", msg="b", src="s2")),
        record(jl(level="warn", msg="c", src="s3")),
        record("raw line one"),
        record("repeated 3 times in window: x"),
    ]
    t = 0.0
    events = []
    for _ in range(600):
        t += rng.choice([1, 1, 5, 5, 30, 30, 60, 600, 3 * H, 14 * H])
        events.append((t, rng.choice(msgs)))
    old, new = replay(oracle, tmp_path, events)
    assert new == old
    assert len(old) > 10 and any("反复出现" in ln for ln in old) and any("重现" in ln for ln in old)


# ---- what is new: other services are not subject to the GA rules, and there is a health poll ----


def test_ga_rules_do_not_hide_the_same_words_from_another_service(tmp_path):
    out = []
    w = JournalWatcher(GA_RULES, tmp_path / "s.json", out.append, lambda: 1.0)
    w.handle(record(jl(level="warn", msg="upstream http error", src="x"), service=GA))
    w.handle(record(jl(level="warn", msg="upstream http error", src="x"), service="intake"))
    assert len(out) == 1 and "upstream http error" in out[0]


def test_external_records_are_gated_like_any_other_cause(tmp_path):
    out = []
    now = [100.0]
    w = JournalWatcher(parse_rules({}), tmp_path / "s.json", out.append, lambda: now[0])
    line = json.dumps(
        {
            "kind": "External",
            "service_id": "n8",
            "reason": "external error",
            "event": {
                "text": json.dumps(
                    {"level": "error", "msg": "unit foo failed", "src": "n8", "error": "x"}
                )
            },
        }
    )
    w.handle(line)
    now[0] += 60
    w.handle(line)
    assert len(out) == 1 and out[0].startswith("[新问题] level=error unit foo failed src=n8 err=x")


def test_state_is_the_same_file_format_so_the_swap_keeps_what_ga_watch_knew(oracle, tmp_path):
    oracle.clock[0] = 10.0
    oracle.bootstrap()
    oracle.handle(record(jl(level="error", msg="known", src="s")))
    state = json.loads(Path(oracle.STATE).read_text())
    out = []
    w = JournalWatcher(GA_RULES, Path(oracle.STATE), out.append, lambda: 20.0)
    w.handle(record(jl(level="error", msg="known", src="s")))
    assert out == [] and state["sig"]
    assert set(json.loads(Path(oracle.STATE).read_text())) >= {"sig"}


def test_health_poll_reports_down_and_restart_and_is_quiet_when_steady(tmp_path):
    out = []
    now = [10_000.0]
    w = JournalWatcher(parse_rules({}), tmp_path / "s.json", out.append, lambda: now[0])
    ok = {"status": "running", "healthy": True, "uptime_s": 9999, "consecutive_failures": 0}
    w.check_service("intake", ok)
    now[0] += 300
    w.check_service("intake", ok)
    assert out == []
    now[0] += 300
    w.check_service("intake", {**ok, "uptime_s": 20})
    assert out[-1].startswith("[服务刚重启] intake")
    now[0] += 1900  # restart and down lines share one 30-minute allowance, as in ga-watch
    w.check_service("intake", {"status": "failed", "healthy": False, "consecutive_failures": 5})
    assert out[-1].startswith("[服务异常] intake status=failed")
    n = len(out)
    now[0] += 300
    w.check_service("intake", {"status": "failed", "healthy": False, "consecutive_failures": 5})
    assert len(out) == n  # once per 30 minutes while it stays down
    now[0] += 1900
    w.check_service("intake", {"status": "failed", "healthy": False, "consecutive_failures": 5})
    assert len(out) == n + 1


def test_a_failing_ams_ctl_is_reported_at_most_every_30_minutes(tmp_path):
    out = []
    now = [10_000.0]
    w = JournalWatcher(parse_rules({}), tmp_path / "s.json", out.append, lambda: now[0])
    w.check_service("intake", None, error="TimeoutExpired: 120s")
    now[0] += 300
    w.check_service("intake", None, error="TimeoutExpired: 120s")
    assert len(out) == 1 and out[0].startswith("[服务查询失败]")


def test_rules_reject_typos():
    with pytest.raises(ValueError, match="unknown"):
        parse_rules({"rules": []})
    with pytest.raises(ValueError, match="unknown"):
        parse_rules({"rule": [{"mesg": ["x"]}]})


def test_the_config_file_loads_rules_state_and_health_list(tmp_path):
    from ams.ops.journal import load_config

    p = tmp_path / "journal.toml"
    p.write_text(
        'ignore_raw_prefixes = ["repeated "]\n'
        '[journal]\nstate = "~/s.json"\n'
        '[health]\ninterval_s = 60\nservices = ["a", "b"]\n'
        '[[rule]]\nservice = "a"\nlevel = "warn"\nmsg = ["x"]\n'
        '[[known_missing]]\nmsg = "source failed"\nsrc_prefix = ["p"]\nerror_contains = ["KEY"]\n'
    )
    cfg = load_config(p)
    assert cfg.health_services == ("a", "b") and cfg.health_interval_s == 60
    assert cfg.rules.ignore_raw_prefixes == ("repeated ",) and cfg.rules.ignore[0].service == "a"
    assert str(cfg.state).startswith("/")
    p.write_text('[journal]\npath = "x"\n')
    with pytest.raises(ValueError, match="state"):
        load_config(p)


def test_the_cli_reports_a_bad_config(tmp_path, capsys):
    from ams.cli import main

    bad = tmp_path / "j.toml"
    bad.write_text("[journal]\nstate = 'x'\n[health]\nbogus = 1\n")
    assert main(["watch", "journal", "--journal-config", str(bad)]) == 2
    assert "bogus" in capsys.readouterr().err
