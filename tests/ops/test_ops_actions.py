import json
import os
import time

import pytest

from ams.ops import actions, freeze
from ams.ops.actions import ActionRequest, Executor
from ams.ops.config import parse
from ams.ops.observe import health
from ams.ops.runner import CmdResult

PRINT_UP = "x = {\n\tstate = running\n\tpid = 99\n\truns = 2\n}"


def make(tmp_path, **over):
    raw = {
        "ops": {"mode": over.pop("mode", "live")},
        "actions": {
            "cooldown_s": 1800,
            "max_per_target_per_day": 2,
            "max_per_hour": 3,
            "clean_tmp": {"dirs": [str(tmp_path / "a" / "b" / "scratch")], "min_age_hours": 1},
            "advertise_tailscale": {"services": ["svc:dsh"]},
        },
        "kaorou": {"env_script": "/opt/kaorou-env.sh"},
        "service": [
            {
                "id": "asr",
                "launchd": "gui/501/com.asr.service",
                "expect": "running",
                "actions": ["kickstart"],
            },
            {
                "id": "kaorou-daemon",
                "launchd": "gui/501/app.example.kaorou.daemon",
                "expect": "running",
                "actions": ["kickstart", "soft_restart_kaorou"],
            },
            {
                "id": "dsh",
                "launchd": "system/app.example.dsh-events",
                "expect": "running",
                "actions": ["advertise_tailscale", "clean_tmp"],
            },
            {"id": "plain", "actions": []},
        ],
    }
    return parse(raw)


class Fake:
    def __init__(self, ffmpeg=False, prefs=None):
        self.calls, self.ffmpeg, self.prefs = [], ffmpeg, prefs or {"AdvertiseServices": []}

    def __call__(self, argv, timeout=30):
        self.calls.append(list(argv))
        if argv[0] == "/usr/bin/pgrep":
            return CmdResult(0 if self.ffmpeg else 1)
        if argv[:2] == ["/bin/launchctl", "print"]:
            return CmdResult(0, PRINT_UP)
        if argv[-2:] == ["debug", "prefs"]:
            return CmdResult(0, json.dumps(self.prefs))
        return CmdResult(0, "ok")


def ex(cfg, fake, hist=None):
    return Executor(cfg, hist if hist is not None else [], runner=fake, sleep=lambda s: None)


def req(name, svc):
    return ActionRequest(name, svc, {})


def test_kickstart_runs_with_exact_argv_and_verifies(tmp_path):
    cfg, fake = make(tmp_path), Fake()
    r = ex(cfg, fake).execute(req("kickstart", "asr"), {}, now=1000)
    assert r.executed and r.ok and "pid=99" in r.detail
    assert ["/bin/launchctl", "kickstart", "-k", "gui/501/com.asr.service"] in fake.calls


def test_action_must_be_listed_for_the_service(tmp_path):
    r = ex(make(tmp_path), Fake()).execute(req("soft_restart_kaorou", "asr"), {}, now=1)
    assert not r.executed and "允许动作" in r.refused
    assert (
        "未知服务" in ex(make(tmp_path), Fake()).execute(req("kickstart", "ghost"), {}, 1).refused
    )
    assert not ex(make(tmp_path), Fake()).execute(req("kickstart", "plain"), {}, 1).executed


def test_freeze_blocks_actions(tmp_path):
    fz = {"asr": freeze.Freeze("asr", 9e9, "tcc-host migration")}
    r = ex(make(tmp_path), Fake()).execute(req("kickstart", "asr"), fz, now=1)
    assert not r.executed and "冻结" in r.refused and "tcc-host" in r.refused
    star = {"*": freeze.Freeze("*", 9e9, "all")}
    assert not ex(make(tmp_path), Fake()).execute(req("kickstart", "asr"), star, 1).executed


def test_kaorou_daemon_is_never_hard_restarted(tmp_path):
    fake = Fake()
    r = ex(make(tmp_path), fake).execute(req("kickstart", "kaorou-daemon"), {}, now=1)
    assert not r.executed and "soft_restart_kaorou" in r.refused
    assert not any(c[:2] == ["/bin/launchctl", "kickstart"] for c in fake.calls)


def test_cooldown_daily_cap_and_hourly_cap(tmp_path):
    cfg, fake, hist = make(tmp_path), Fake(), []
    e = ex(cfg, fake, hist)
    assert e.execute(req("kickstart", "asr"), {}, now=1000).executed
    assert "冷却" in e.execute(req("kickstart", "asr"), {}, now=1100).refused
    assert e.execute(req("kickstart", "asr"), {}, now=1000 + 1900).executed
    assert "上限" in e.execute(req("kickstart", "asr"), {}, now=1000 + 4000).refused  # 2/day
    # refusals are recorded but do not count against the caps
    assert [h["outcome"] for h in hist].count("refused") == 2


def test_global_hourly_cap(tmp_path):
    cfg = make(tmp_path)
    hist = [
        {"ts": 900 + i, "action": "x", "target": f"t{i}", "outcome": "ok", "detail": ""}
        for i in range(3)
    ]
    r = ex(cfg, Fake(), hist).execute(req("kickstart", "asr"), {}, now=1000)
    assert "全局" in r.refused


def test_dry_run_never_executes(tmp_path):
    fake = Fake()
    r = ex(make(tmp_path, mode="dry-run"), fake).execute(req("kickstart", "asr"), {}, now=1)
    assert not r.executed and "dry-run" in r.refused
    assert not any(c[:2] == ["/bin/launchctl", "kickstart"] for c in fake.calls)


@pytest.fixture
def api(monkeypatch):
    state = {"/jobs": {"jobs": []}, "/system": {"daemon": {"state": "running"}}}
    monkeypatch.setattr(
        health, "get_json", lambda url, timeout=5.0: state.get("/" + url.rsplit("/", 1)[1])
    )
    return state


def test_soft_restart_runs_only_when_provably_idle(tmp_path, api):
    cfg, fake = make(tmp_path), Fake()
    r = ex(cfg, fake).execute(req("soft_restart_kaorou", "kaorou-daemon"), {}, now=1)
    assert r.executed and ["/opt/kaorou-env.sh", "soft-restart"] in fake.calls


def test_soft_restart_refused_during_a_burn(tmp_path, api):
    cfg = make(tmp_path)
    r = ex(cfg, Fake(ffmpeg=True)).execute(req("soft_restart_kaorou", "kaorou-daemon"), {}, 1)
    assert not r.executed and "ffmpeg" in r.refused
    api["/jobs"] = {"jobs": [{"stage": "burn", "status": "running"}]}
    r = ex(cfg, Fake()).execute(req("soft_restart_kaorou", "kaorou-daemon"), {}, 1)
    assert "burn" in r.refused
    api["/jobs"] = {"jobs": []}
    api["/system"] = {"daemon": {"state": "draining"}}
    assert (
        "draining"
        in ex(cfg, Fake()).execute(req("soft_restart_kaorou", "kaorou-daemon"), {}, 1).refused
    )


def test_soft_restart_refused_when_api_unknown(tmp_path, api):
    api["/jobs"] = None
    r = ex(make(tmp_path), Fake()).execute(req("soft_restart_kaorou", "kaorou-daemon"), {}, 1)
    assert not r.executed and "无法" in r.refused


def test_clean_tmp_removes_only_old_entries_under_the_configured_dir(tmp_path):
    cfg = make(tmp_path)
    d = tmp_path / "a" / "b" / "scratch"
    (d / "olddir").mkdir(parents=True)
    (d / "olddir" / "f").write_text("x" * 2048)
    (d / "old.txt").write_text("old")
    (d / "new.txt").write_text("new")
    outside = tmp_path / "outside.txt"
    outside.write_text("keep")
    (d / "link").symlink_to(outside)
    old = time.time() - 7200
    for p in (d / "olddir", d / "old.txt", d / "link"):
        os.utime(p, (old, old), follow_symlinks=False)
    r = ex(cfg, Fake()).execute(req("clean_tmp", "dsh"), {}, now=time.time())
    assert r.ok and "删除 3 项" in r.detail
    assert (d / "new.txt").exists() and outside.exists() and not (d / "olddir").exists()


def test_clean_tmp_refuses_shallow_or_symlinked_roots(tmp_path):
    raw = {
        "actions": {"clean_tmp": {"dirs": ["/tmp", "/"]}},
        "service": [{"id": "x", "actions": ["clean_tmp"]}],
    }
    cfg = parse(raw)
    r = Executor(cfg, [], runner=Fake()).execute(req("clean_tmp", "x"), {}, now=1)
    assert r.ok and "删除 0 项" in r.detail


def test_advertise_only_what_is_missing(tmp_path):
    cfg = make(tmp_path)
    fake = Fake(prefs={"AdvertiseServices": []})
    r = ex(cfg, fake).execute(req("advertise_tailscale", "dsh"), {}, now=1)
    assert r.ok and [c for c in fake.calls if "advertise" in c][0][-1] == "svc:dsh"
    fake2 = Fake(prefs={"AdvertiseServices": ["svc:dsh"]})
    r2 = ex(cfg, fake2).execute(req("advertise_tailscale", "dsh"), {}, now=1)
    assert r2.ok and not any("advertise" in c for c in fake2.calls)
    assert actions.missing_services(cfg, Fake(prefs={"AdvertiseServices": None})) == ["svc:dsh"]
