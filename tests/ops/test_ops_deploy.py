"""`[[deploy]]`: follow a branch, fast-forward a clean working copy, run its deploy command once
per commit, and escalate exactly one result per commit. Real git repositories, real processes."""

import json
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ams.ops import deploy as deploy_mod
from ams.ops import freeze
from ams.ops.config import ConfigError, ForwardConfig, parse
from ams.ops.engine import Engine, Env
from ams.ops.forward import Forwarder
from ams.ops.store import Store

SRC = Path(__file__).resolve().parents[2] / "src"
NOON = time.mktime((2026, 11, 16, 12, 0, 0, 0, 0, -1))

# The deploy command: what it does is read from mode.txt next to the repos (ok | fail | sleep).
SCRIPT = """
import os, pathlib, sys, time
root = pathlib.Path(os.environ["DEPLOY_TEST_ROOT"])
mode = (root / "mode.txt").read_text().strip()
print("deploying", os.environ["AMS_DEPLOY_SHA"], "mode", mode, flush=True)
if mode == "sleep":
    time.sleep(60)
if mode == "fail":
    print("error: the build broke", flush=True)
    sys.exit(3)
with open(root / "deployed.txt", "a") as f:
    f.write(os.environ["AMS_DEPLOY_SHA"] + "\\n")
"""


def git(cwd, *args):
    cp = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "-c", "commit.gpgsign=false", *args],
        cwd=cwd, capture_output=True, text=True, check=True,
    )  # fmt: skip
    return cp.stdout.strip()


class Rig:
    def __init__(self, tmp_path, monkeypatch, mode="live", **extra):
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")  # no hooks or signing from $HOME
        monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
        monkeypatch.setenv("PYTHONPATH", str(SRC))  # the wrapper runs as `python -m ams.ops.deploy`
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.root = tmp_path
        (tmp_path / "mode.txt").write_text("ok")
        self.origin = tmp_path / "origin.git"
        git(tmp_path, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.dev = tmp_path / "dev"
        git(tmp_path, "clone", "-q", str(self.origin), str(self.dev))
        git(self.dev, "checkout", "-q", "-b", "main")
        self.push("first")
        self.repo = tmp_path / "repo"
        git(tmp_path, "clone", "-q", str(self.origin), str(self.repo))
        (tmp_path / "deploy.py").write_text(SCRIPT)
        entry = {
            "id": "app",
            "repo": str(self.repo),
            "command": [sys.executable, str(tmp_path / "deploy.py")],
            "env": {"DEPLOY_TEST_ROOT": str(tmp_path)},
            **extra,
        }
        (tmp_path / "tok").write_text("t")
        cfg = parse(
            {"ops": {"state_dir": str(tmp_path / "state"), "mode": mode}, "deploy": [entry]},
            config_dir=tmp_path,
        )
        self.cfg = cfg.__class__(
            **{**cfg.__dict__, "forward": ForwardConfig(token_file=tmp_path / "tok")}
        )
        self.state = {}
        self.now = NOON
        self.kills = []
        self.dep = self.deployer()

    def deployer(self):
        def kill(pid, sig):
            self.kills.append(sig)
            deploy_mod.os.killpg(pid, sig)

        return deploy_mod.Deployer(
            self.cfg, self.state, sleep=lambda s: time.sleep(0.05), kill=kill
        )

    def push(self, msg):
        (self.dev / "f.txt").write_text(msg)
        git(self.dev, "add", "f.txt")
        git(self.dev, "commit", "-q", "-m", msg)
        git(self.dev, "push", "-q", "origin", "main")
        return git(self.dev, "rev-parse", "HEAD")

    def tick(self, advance=301, freezes=None):
        self.now += advance
        return self.dep.tick(self.now, freezes or {})

    def finish(self):
        """Wait for the running deploy's process, then tick so its result is read."""
        h = self.dep._handles["app"]
        for _ in range(200):
            if h.poll() is not None:
                break
            time.sleep(0.05)
        return self.tick(advance=30)

    def deployed(self):
        p = self.root / "deployed.txt"
        return p.read_text().split() if p.exists() else []

    @property
    def head(self):
        return git(self.repo, "rev-parse", "HEAD")


def test_new_commit_is_fast_forwarded_deployed_and_told_once(tmp_path, monkeypatch):
    r = Rig(tmp_path, monkeypatch)
    first = r.head
    assert r.tick() == [] and r.state["app"]["run"]["sha"] == first  # never deployed: deploy HEAD
    [ev] = r.finish()
    assert (ev.decision, ev.severity, ev.title) == ("deployed", "info", "app deployed")
    assert "first" in ev.text and r.deployed() == [first]
    second = r.push("second")
    assert r.tick() == [] and r.head == second  # fast-forwarded before the command ran
    [ev] = r.finish()
    assert ev.sha == second and r.deployed() == [first, second]
    assert r.tick() == [] and r.tick() == []  # up to date: nothing runs, nothing is said
    assert r.deployed() == [first, second]
    log = (r.cfg.state_dir / "deploy" / "app.log").read_text()
    assert log.startswith("# ") and f"deploying {second}" in log


def test_checks_wait_for_every_s(tmp_path, monkeypatch):
    r = Rig(tmp_path, monkeypatch, every_s=600)
    r.tick(advance=601)
    r.finish()
    r.push("second")
    assert r.tick(advance=100) == [] and r.state["app"]["run"] is None  # not due yet
    r.tick(advance=600)
    assert r.state["app"]["run"] is not None


def test_failed_commit_is_told_once_and_not_retried_until_the_next_push(tmp_path, monkeypatch):
    r = Rig(tmp_path, monkeypatch)
    (tmp_path / "mode.txt").write_text("fail")
    r.tick()
    [ev] = r.finish()
    assert (ev.decision, ev.severity, ev.title) == ("deploy_failed", "error", "app deploy failed")
    assert "退出码 3" in ev.text and "| error: the build broke" in ev.text
    assert "app.log" in ev.text
    assert r.tick() == [] and r.tick() == [] and r.state["app"]["run"] is None  # not retried
    (tmp_path / "mode.txt").write_text("ok")
    fixed = r.push("fix")
    r.tick()
    [ev] = r.finish()
    assert ev.decision == "deployed" and r.deployed() == [fixed]
    assert r.state["app"]["failed"] is None


def test_dirty_tree_is_left_alone_and_told_once_per_commit(tmp_path, monkeypatch):
    r = Rig(tmp_path, monkeypatch)
    r.tick()
    r.finish()
    before = r.head
    (r.repo / "f.txt").write_text("someone is editing")
    second = r.push("second")
    [ev] = r.tick()
    assert (ev.decision, ev.severity) == ("deploy_blocked", "warn") and "f.txt" in ev.text
    assert r.head == before and (r.repo / "f.txt").read_text() == "someone is editing"
    assert r.tick() == []  # same commit, same cause: once
    third = r.push("third")
    [ev] = r.tick()
    assert ev.sha == third and third != second  # a new commit is worth saying again
    git(r.repo, "checkout", "--", "f.txt")
    r.tick()
    [ev] = r.finish()
    assert ev.decision == "deployed" and r.head == third


def test_untracked_files_do_not_block(tmp_path, monkeypatch):
    r = Rig(tmp_path, monkeypatch)
    (r.repo / "build").mkdir()
    (r.repo / "build" / "x.log").write_text("x")
    r.tick()
    [ev] = r.finish()
    assert ev.decision == "deployed"


def test_local_commits_the_remote_lacks_block(tmp_path, monkeypatch):
    r = Rig(tmp_path, monkeypatch)
    r.tick()
    r.finish()
    (r.repo / "local.txt").write_text("x")
    git(r.repo, "add", "local.txt")
    git(r.repo, "commit", "-q", "-m", "local only")
    local = r.head
    r.push("remote")
    [ev] = r.tick()
    assert ev.decision == "deploy_blocked" and "diverged" in ev.title
    assert r.head == local  # never reset


def test_another_branch_checked_out_blocks(tmp_path, monkeypatch):
    r = Rig(tmp_path, monkeypatch)
    git(r.repo, "checkout", "-q", "-b", "experiment")
    [ev] = r.tick()
    assert ev.decision == "deploy_blocked" and "experiment" in ev.text
    assert r.state["app"]["run"] is None and r.tick() == []


def test_fetch_failures_are_told_after_six_in_a_row(tmp_path, monkeypatch):
    r = Rig(tmp_path, monkeypatch, url=str(tmp_path / "gone.git"))
    events = [e for _ in range(8) for e in r.tick()]
    assert [e.decision for e in events] == ["fetch_failing"]
    assert events[0].severity == "warn" and r.state["app"]["fetch_fails"] == 8


def test_url_fetches_into_the_remote_tracking_ref(tmp_path, monkeypatch):
    r = Rig(tmp_path, monkeypatch, url=str(tmp_path / "origin.git"), remote="upstream")
    r.tick()
    [ev] = r.finish()
    assert ev.decision == "deployed"
    assert git(r.repo, "rev-parse", "refs/remotes/upstream/main") == r.head


def test_timeout_kills_the_process_group_and_fails(tmp_path, monkeypatch):
    r = Rig(tmp_path, monkeypatch, timeout_s=60)
    (tmp_path / "mode.txt").write_text("sleep")
    r.tick()
    h = r.dep._handles["app"]
    assert r.tick(advance=30) == []  # still inside its time
    [ev] = r.tick(advance=31)
    assert ev.decision == "deploy_failed" and "已终止" in ev.text
    assert r.kills[0] == signal.SIGTERM and h.poll() is not None
    assert r.state["app"]["failed"] == ev.sha and r.state["app"]["run"] is None


def test_a_restarted_watch_reads_the_result_from_the_rc_file(tmp_path, monkeypatch):
    r = Rig(tmp_path, monkeypatch)
    r.tick()
    r.dep._handles["app"].wait()  # the run ended while ams watch was down
    r.dep = r.deployer()  # a new process: the handle is gone, the state file remains
    [ev] = r.tick(advance=30)
    assert ev.decision == "deployed" and r.deployed() == [r.head]


def test_a_restarted_watch_waits_for_a_run_still_going(tmp_path, monkeypatch):
    r = Rig(tmp_path, monkeypatch)
    (tmp_path / "mode.txt").write_text("sleep")
    r.tick()
    old = r.dep._handles["app"]
    r.dep = r.deployer()
    assert r.tick(advance=30) == [] and r.state["app"]["run"] is not None  # found it by rc path
    old.kill()
    old.wait()


def test_a_run_that_dies_without_an_exit_code_is_retried_then_failed(tmp_path, monkeypatch):
    r = Rig(tmp_path, monkeypatch)
    (tmp_path / "mode.txt").write_text("sleep")
    for attempt in range(1, deploy_mod.MAX_INTERRUPTED + 1):
        r.tick()
        assert r.state["app"]["run"]["attempt"] == attempt
        h = r.dep._handles["app"]
        deploy_mod.os.killpg(h.pid, signal.SIGKILL)  # a reboot, say
        h.wait()
        events = r.tick(advance=30)
    [ev] = events
    assert ev.decision == "deploy_failed" and "没有退出码" in ev.text


def test_freeze_and_dry_run_start_nothing(tmp_path, monkeypatch):
    r = Rig(tmp_path, monkeypatch)
    fz = {"app": freeze.Freeze("app", NOON + 9999, "maintenance")}
    assert r.tick(freezes=fz) == [] and r.state["app"]["run"] is None
    d = Rig(tmp_path / "dry", monkeypatch, mode="dry-run")
    before = d.head
    d.push("second")
    assert d.tick() == [] and d.state["app"]["run"] is None and d.head == before


def test_wrapper_writes_the_exit_code(tmp_path):
    rc = tmp_path / "x.rc"
    assert (
        deploy_mod.main(["--rc-file", str(rc), "--", sys.executable, "-c", "raise SystemExit(4)"])
        == 4
    )
    assert rc.read_text() == "4\n"
    assert deploy_mod.main(["--rc-file", str(rc), "--", str(tmp_path / "missing")]) == 127
    assert rc.read_text() == "127\n"


def test_engine_escalates_the_result_and_reports_status(tmp_path, monkeypatch):
    r = Rig(tmp_path, monkeypatch)
    store = Store(tmp_path / "state.json")
    posts = []
    fw = Forwarder(
        r.cfg.forward,
        store.data,
        post=lambda *a: posts.append(json.loads(a[2])) or (202, b'{"outcome":"accepted"}'),
        clock=lambda: r.now,
    )
    eng = Engine(r.cfg, store, fw, Env(clock=lambda: r.now))
    eng.tick()  # baseline tick also checks deploys
    r.now += 1
    h = eng.deployer._handles["app"]
    h.wait()
    r.now += 30
    rep = eng.tick()
    assert [e.decision for e in rep.deploys] == ["deployed"]
    [post] = posts
    assert post["origin"] == "mini.app" and post["severity"] == "info"
    assert post["title"] == "app deployed"
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved["status"]["deploy"]["app"]["deployed"] == r.head
    assert saved["deploy"]["app"]["deployed"] == r.head


def test_old_state_files_get_the_deploy_section(tmp_path):
    p = tmp_path / "state.json"
    p.write_text(json.dumps({"version": 2, "baselined": True}))
    assert Store(p).data["deploy"] == {}


BASE = {"id": "app", "repo": "/srv/app", "command": ["scripts/install.sh"]}


@pytest.mark.parametrize(
    "change,needle",
    [
        ({"id": "Bad"}, "deploy.id"),
        ({"repo": "relative/path"}, "repo"),
        ({"command": []}, "command"),
        ({"command": "scripts/install.sh"}, "command"),
        ({"branch": "-x"}, "branch"),
        ({"remote": "a..b"}, "remote"),
        ({"url": "--upload-pack=x"}, "url"),
        ({"env": {"lower": "x"}}, "env"),
        ({"every_s": 10}, "every_s"),
        ({"timeout_s": 5}, "timeout_s"),
        ({"shell": True}, "unknown"),
    ],
)
def test_bad_deploy_entries_fail_at_load(change, needle):
    with pytest.raises(ConfigError, match=needle):
        parse({"deploy": [{**BASE, **change}]})


def test_deploy_ids_are_unique_and_apart_from_service_ids():
    with pytest.raises(ConfigError, match="duplicate"):
        parse({"deploy": [BASE, BASE]})
    with pytest.raises(ConfigError, match="also a service id"):
        parse({"deploy": [BASE], "service": [{"id": "app"}]})
    with pytest.raises(ConfigError, match=r"\[\[deploy\]\]"):
        parse({"deploy": {"id": "app"}})


def test_deploy_defaults_and_expansion():
    [d] = parse(
        {"deploy": [{**BASE, "repo": "~/app", "ssh_key": "~/k", "env": {"A_B": "1"}}]}
    ).deploys
    assert d.repo.is_absolute() and d.ssh_key and d.ssh_key.is_absolute()
    assert (d.branch, d.remote, d.url, d.every_s, d.timeout_s) == (
        "main",
        "origin",
        None,
        300,
        3600,
    )
    assert d.env == (("A_B", "1"),) and d.command == ("scripts/install.sh",)


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin's background band")
def test_the_command_runs_outside_the_background_band(tmp_path, monkeypatch):
    """ams watch is a Background LaunchAgent; a build it starts must not inherit the throttling."""
    monkeypatch.setenv("PYTHONPATH", str(SRC))
    probe = "import os; print('prio', os.getpriority(os.PRIO_DARWIN_PROCESS, 0))"
    out = subprocess.run(
        ["/usr/sbin/taskpolicy", "-b", sys.executable, "-m", "ams.ops.deploy",
         "--rc-file", str(tmp_path / "x.rc"), "--", sys.executable, "-c", probe],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    assert "left the background band" in out and "prio 0" in out
