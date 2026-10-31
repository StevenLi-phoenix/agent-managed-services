"""The tick of `ams watch`: observe -> gate -> (freeze | rule-chosen fix) -> escalate."""

import json
import time

from ams.ops import freeze
from ams.ops.config import ForwardConfig, parse
from ams.ops.engine import Engine, Env
from ams.ops.forward import Forwarder
from ams.ops.observe import health
from ams.ops.observe.launchd import JobState
from ams.ops.runner import CmdResult
from ams.ops.store import Store

NOON = time.mktime((2026, 10, 31, 12, 0, 0, 0, 0, -1))
UP = JobState(True, "running", 42, 1, 0)
DOWN = JobState(True, "not running", None, 1, -9)
ASR = "gui/501/com.asr.service"


class Rig:
    def __init__(self, tmp_path, services=None):
        self.log = tmp_path / "app.log"
        self.log.write_text("")
        svcs = services or [
            {
                "id": "asr",
                "launchd": ASR,
                "expect": "running",
                "actions": ["kickstart"],
                "logs": [str(self.log)],
                "log_format": "plain",
            },
        ]
        (tmp_path / "tok").write_text("t")
        self.cfg = parse({"service": svcs}, config_dir=tmp_path)
        self.cfg = self.cfg.__class__(
            **{**self.cfg.__dict__, "forward": ForwardConfig(token_file=tmp_path / "tok")}
        )
        self.store = Store(tmp_path / "state.json")
        self.posts = []
        self.jobs = {ASR: UP}
        self.now = NOON
        self.runs = []
        env = Env(
            runner=self._run,
            read_job=lambda spec, r: self.jobs[spec],
            http_probe=lambda u, accept=(): health.Probe(True, "ok"),
            sleep=lambda s: None,
            clock=lambda: self.now,
        )
        self.forwarder = Forwarder(
            self.cfg.forward,
            self.store.data,
            post=lambda *a: self.posts.append(json.loads(a[2])) or (202, b'{"outcome":"accepted"}'),
            clock=lambda: self.now,
        )
        self.engine = Engine(self.cfg, self.store, self.forwarder, env)

    def _run(self, argv, timeout=30):
        self.runs.append(list(argv))
        if argv[:2] == ["/usr/bin/pgrep", "-x"]:
            return CmdResult(1, "")  # no ffmpeg
        if argv[:2] == ["/bin/launchctl", "kickstart"]:
            self.jobs[argv[-1]] = UP  # launchd brought it back
        if argv[:2] == ["/bin/launchctl", "print"]:
            return CmdResult(0, "\tstate = running\n\tpid = 99\n\truns = 2\n")
        return CmdResult(0, "")

    def tick(self, advance=30):
        self.now += advance
        return self.engine.tick()

    def append(self, *lines):
        with open(self.log, "a") as f:
            f.write("".join(ln + "\n" for ln in lines))


def test_first_tick_baselines_history_and_escalates_nothing(tmp_path):
    r = Rig(tmp_path)
    r.append("ERROR old problem 1", "WARNING old problem 2")
    r.tick()
    assert r.posts == [] and r.store.data["baselined"] and len(r.store.data["sigs"]) == 2
    r.append("ERROR old problem 1")  # same cause: still known
    assert r.tick().incidents == 0


def test_a_new_log_cause_is_escalated_once_with_its_severity(tmp_path):
    r = Rig(tmp_path)
    r.tick()
    r.append(
        "2026-10-31 WARNING watch: RSS feed for UCabcdefghijklmnopqrstuv failed after 3 attempts"
    )
    rep = r.tick()
    assert rep.escalated == 1 and rep.actions == []  # a log line has no cure
    [p] = r.posts
    assert p["origin"] == "mini.asr" and p["severity"] == "warn"
    assert p["title"].startswith("asr|log|") and "UC" not in p["title"]  # the stable cause
    assert p["key"].startswith("ops-")
    r.append(
        "2026-10-31 WARNING watch: RSS feed for UCzzzzzzzzzzzzzzzzzzzzzz failed after 4 attempts"
    )
    assert r.tick().incidents == 0  # the same cause is counted, not re-reported


def test_a_job_down_is_fixed_by_rule_and_escalated_with_the_outcome(tmp_path):
    r = Rig(tmp_path)
    r.tick()
    r.jobs[ASR] = DOWN
    r.tick()
    assert r.posts == []  # one tick of grace
    rep = r.tick()
    assert rep.escalated == 1 and rep.actions[0].executed
    assert ["/bin/launchctl", "kickstart", "-k", ASR] in r.runs
    [p] = r.posts
    assert p["title"] == "asr job_down" and p["severity"] == "error"
    assert "已执行 kickstart" in p["text"] and "已自动处理，无需操作" in p["text"]


def test_a_failed_fix_says_a_human_is_needed(tmp_path):
    r = Rig(tmp_path)
    r.tick()
    r.engine.executor.runner = lambda argv, timeout=30: CmdResult(1, "", "boom")
    r.jobs[ASR] = DOWN
    r.tick()
    r.tick()
    assert "需要人看一眼" in r.posts[0]["text"]


def test_frozen_service_gets_no_action_and_a_muted_incident_is_not_escalated(tmp_path):
    r = Rig(tmp_path)
    r.tick()
    freeze.add(tmp_path / "freeze.json", "asr", 3600, "tcc-host migration", now=r.now)
    r.jobs[ASR] = DOWN
    r.tick()
    rep = r.tick()
    assert r.posts == [] and r.runs == [] and rep.muted == 1


def test_frozen_without_mute_still_escalates_but_never_acts(tmp_path):
    r = Rig(tmp_path)
    r.tick()
    freeze.add(tmp_path / "freeze.json", "asr", 3600, "maintenance", mute=False, now=r.now)
    r.jobs[ASR] = DOWN
    r.tick()
    r.tick()
    assert r.runs == [] and "冻结中" in r.posts[0]["text"]


def test_kaorou_burn_blocks_the_soft_restart_and_the_daemon_is_never_kickstarted(
    tmp_path, monkeypatch
):
    label = "gui/501/app.example.kaorou.daemon"
    svcs = [
        {
            "id": "kaorou-daemon",
            "launchd": label,
            "expect": "running",
            "actions": ["soft_restart_kaorou", "kickstart"],
        }
    ]
    r = Rig(tmp_path, services=svcs)
    r.jobs = {label: UP}
    monkeypatch.setattr(
        health,
        "get_json",
        lambda url, timeout=5.0: {"jobs": [{"stage": "burn", "status": "running"}]},
    )
    r.tick()
    r.jobs[label] = DOWN
    r.tick()
    r.tick()
    # not the soft restart (a burn runs), and kickstart is never tried
    assert not any("soft-restart" in c or c == "kickstart" for run in r.runs for c in run)
    assert "未执行 soft_restart_kaorou：保护规则" in r.posts[0]["text"]


def test_kaorou_idle_gets_the_soft_restart_not_a_kickstart(tmp_path, monkeypatch):
    label = "gui/501/app.example.kaorou.daemon"
    svcs = [
        {
            "id": "kaorou-daemon",
            "launchd": label,
            "expect": "running",
            "actions": ["soft_restart_kaorou", "kickstart"],
        }
    ]
    r = Rig(tmp_path, services=svcs)
    r.jobs = {label: UP}
    monkeypatch.setattr(
        health,
        "get_json",
        lambda url, timeout=5.0: (
            {"jobs": []} if url.endswith("/jobs") else {"daemon": {"state": "running"}}
        ),
    )
    r.tick()
    r.jobs[label] = DOWN
    r.tick()
    r.tick()
    assert any("soft-restart" in c for run in r.runs for c in run)
    assert not any("kickstart" in run for run in r.runs)


def test_restart_storm_is_escalated_without_a_fix(tmp_path):
    r = Rig(tmp_path)
    r.tick()
    r.tick()  # first real sample of the restart counter
    r.jobs[ASR] = JobState(True, "running", 50, 5, -9)
    rep = r.tick(60)
    assert rep.escalated == 1 and rep.actions == []
    assert r.posts[0]["title"] == "asr job_flap"


def test_the_same_incident_in_one_minute_is_one_outbox_entry(tmp_path):
    r = Rig(tmp_path)
    r.tick()
    r.forwarder.cfg = r.forwarder.cfg.__class__(
        token_file=tmp_path / "missing"
    )  # intake unreadable
    r.append("ERROR something new")
    r.tick()
    assert len(r.store.data["outbox"]) == 1 and r.posts == []


def test_status_snapshot_is_written(tmp_path):
    r = Rig(tmp_path)
    r.tick()
    st = Store(tmp_path / "state.json").data["status"]
    assert st["services"]["asr"]["job"]["pid"] == 42 and st["outbox"] == 0


def test_dry_run_decides_but_never_acts(tmp_path):
    r = Rig(tmp_path)
    r.cfg = r.engine.cfg = r.cfg.__class__(**{**r.cfg.__dict__, "mode": "dry-run"})
    r.engine.executor.cfg = r.cfg
    r.tick()
    r.jobs[ASR] = DOWN
    r.tick()
    r.tick()
    assert r.runs == [] and "dry-run" in r.posts[0]["text"]
