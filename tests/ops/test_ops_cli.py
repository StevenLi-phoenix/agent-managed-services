import fcntl
import json

import pytest

from ams.cli import main
from ams.ops.cli import parse_duration

CONF = """
[ops]
state_dir = "{state}"
tick_s = 1
[forward]
token_file = "{tmp}/tok"
url = "http://127.0.0.1:9/escalate"
[[service]]
id = "asr"
launchd = "gui/501/com.asr.service"
expect = "running"
actions = ["kickstart"]
"""


@pytest.fixture
def conf(tmp_path):
    p = tmp_path / "watch.toml"
    p.write_text(CONF.format(state=tmp_path / "state", tmp=tmp_path))
    return str(p)


def watch(conf, *argv):
    return main(["watch", "-c", conf, *argv])


def test_parse_duration():
    assert parse_duration("90s") == 90 and parse_duration("2h") == 7200
    with pytest.raises(Exception):
        parse_duration("soon")


def test_check_reports_a_missing_token(conf, capsys):
    assert watch(conf, "check") == 0
    out = capsys.readouterr().out
    assert "asr" in out and "intake token: MISSING" in out


def test_bad_config_exits_2(tmp_path, capsys):
    bad = tmp_path / "bad.toml"
    bad.write_text("[[service]]\nid = 'Bad Id'\n")
    assert watch(str(bad), "check") == 2
    assert "service.id" in capsys.readouterr().err


def test_freeze_roundtrip(conf, capsys):
    assert watch(conf, "freeze", "add", "asr", "30m", "migration") == 0
    assert "frozen asr" in capsys.readouterr().out
    watch(conf, "freeze", "list")
    assert "asr" in capsys.readouterr().out
    watch(conf, "freeze", "rm", "asr")
    assert "removed" in capsys.readouterr().out


def test_status_before_first_tick(conf, capsys):
    assert watch(conf, "status") == 0
    assert "outbox=0" in capsys.readouterr().out


def test_test_without_a_token_stays_queued(conf, capsys):
    assert watch(conf, "test") == 1
    assert "queued=1" in capsys.readouterr().out


def test_test_refuses_while_the_loop_owns_the_state(conf, tmp_path, capsys):
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    holder = open(state / "lock", "w")
    fcntl.flock(holder, fcntl.LOCK_EX)  # the loop is "running"
    try:
        assert watch(conf, "test") == 1
    finally:
        holder.close()
    assert "owns the state file" in capsys.readouterr().out
    assert not (state / "state.json").exists()


def test_run_once_survives_everything_unavailable(conf, tmp_path):
    # launchctl says the job is not loaded; no token; no network. A tick must not raise.
    assert watch(conf, "run", "--once") == 0
    st = json.loads((tmp_path / "state" / "state.json").read_text())
    assert st["baselined"] is True and "asr" in st["status"]["services"]
