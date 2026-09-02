"""CLI tests.

``validate`` and argument parsing are pure. The ``run`` path needs
``ams.state``/``ams.ports`` (written by another agent) and is skipped when they
are not importable yet.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import signal
import sys
import threading

import pytest

from ams.cli import build_parser, main

GOOD = """
id = "hello"
[start]
argv = ["/bin/echo", "hi"]
"""

BAD_FIELD = """
id = "Hello World"
[start]
argv = ["/bin/echo"]
"""

BAD_TOML = "id = \nnot toml at all"


def write(tmp_path, name: str, text: str):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_validate_accepts_a_good_declaration(tmp_path, capsys):
    path = write(tmp_path, "good.toml", GOOD)
    assert main(["validate", str(path)]) == 0
    assert capsys.readouterr().out.strip() == "OK hello"


def test_validate_reports_each_bad_file_and_exits_1(tmp_path, capsys):
    good = write(tmp_path, "good.toml", GOOD)
    bad = write(tmp_path, "bad.toml", BAD_FIELD)
    broken = write(tmp_path, "broken.toml", BAD_TOML)
    missing = tmp_path / "nope.toml"
    assert main(["validate", str(good), str(bad), str(broken), str(missing)]) == 1
    out = capsys.readouterr().out
    assert "OK hello" in out
    assert f"ERROR {bad}: id:" in out
    assert f"ERROR {broken}: toml:" in out
    assert f"ERROR {missing}:" in out


def test_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])
    assert excinfo.value.code == 0
    assert "validate" in capsys.readouterr().out


def test_missing_subcommand_is_a_usage_error():
    with pytest.raises(SystemExit) as excinfo:
        main([])
    assert excinfo.value.code == 2


def test_parser_defaults():
    args = build_parser().parse_args(["run"])
    assert args.no_isolation is False
    assert args.escalate == "jsonl"
    assert args.log_level == "INFO"
    assert args.state_dir is None


def test_check_host_without_the_module(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "ams.hostcheck", None)
    assert main(["check-host"]) == 2
    assert "hostcheck unavailable" in capsys.readouterr().err


def test_python_m_ams_runs(tmp_path):
    import subprocess

    path = write(tmp_path, "good.toml", GOOD)
    src = pathlib.Path(__file__).resolve().parent.parent / "src"
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(src), env.get("PYTHONPATH", "")])
    proc = subprocess.run(
        [sys.executable, "-m", "ams", "validate", str(path)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "OK hello"


@pytest.mark.skipif(
    threading.current_thread() is not threading.main_thread(),
    reason="signal handlers need the main thread",
)
def test_run_no_isolation_supervises_then_stops(tmp_path, monkeypatch, capsys):
    pytest.importorskip("ams.state", reason="ams.state not written yet")
    pytest.importorskip("ams.ports", reason="ams.ports not written yet")
    monkeypatch.delenv("AMS_STATE_DIR", raising=False)
    state_dir = tmp_path / "state"
    service_dir = state_dir / "services" / "hello"
    service_dir.mkdir(parents=True)
    code = "import time; print('up', flush=True); time.sleep(30)"
    argv = json.dumps([sys.executable, "-c", code])
    (service_dir / "service.toml").write_text(
        f'id = "hello"\n[start]\nargv = {argv}\n[stop]\ntimeout_s = 1.0\n', encoding="utf-8"
    )

    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    timer = threading.Timer(1.0, lambda: os.kill(os.getpid(), signal.SIGTERM))
    timer.start()
    try:
        rc = main(["run", "--no-isolation", "--state-dir", str(state_dir), "--log-level", "DEBUG"])
    finally:
        timer.cancel()
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
    assert rc == 0
    captured = capsys.readouterr()
    # stderr is the harness log; stdout is the escalation stream
    assert "started hello" in captured.err
    assert "[hello:stdout] up" in captured.err
    assert "shutting down" in captured.err


# ------------------------------------------------------------------- assembly


def _state_with(tmp_path, service_id: str, toml_text: str):
    service_dir = tmp_path / "services" / service_id
    service_dir.mkdir(parents=True)
    (service_dir / "service.toml").write_text(toml_text, encoding="utf-8")
    return tmp_path


def test_parser_knows_the_new_flags_and_subcommands():
    args = build_parser().parse_args(["run", "--provision"])
    assert args.provision is True
    prov = build_parser().parse_args(["provision", "hello", "--state-dir", "/tmp/x"])
    assert prov.command == "provision"
    assert prov.ids == ["hello"]
    assert prov.state_dir == pathlib.Path("/tmp/x")
    assert build_parser().parse_args(["provision"]).ids == []


def test_build_supervisor_registers_and_allocates_without_isolation(tmp_path):
    from ams.cli import build_supervisor
    from ams.state import StateDir

    _state_with(
        tmp_path,
        "hello",
        'id = "hello"\n[start]\nargv = ["/bin/echo", "hi"]\n[ports]\nmain = 0\n',
    )
    asm = build_supervisor(StateDir(tmp_path), isolation=False)
    assert asm.registered == ["hello"]
    assert asm.isolated is False
    assert asm.uids is None
    assert set(asm.supervisor.services) == {"hello"}
    port = asm.supervisor.services["hello"].ports["main"]
    assert 20000 <= port <= 29999
    # the allocation is persisted where the next run will find it
    assert json.loads((tmp_path / "state" / "ports.json").read_text())["ports"] == {
        "hello": {"main": port}
    }


def test_build_supervisor_skips_a_service_whose_port_is_taken(tmp_path, caplog):
    """Two declarations claiming the same fixed port: the loser is skipped, not fatal."""
    from ams.cli import build_supervisor
    from ams.state import StateDir

    fixed = "[ports]\nmain = 20999\n"
    _state_with(tmp_path, "one", f'id = "one"\n[start]\nargv = ["/bin/echo"]\n{fixed}')
    _state_with(tmp_path, "two", f'id = "two"\n[start]\nargv = ["/bin/echo"]\n{fixed}')
    with caplog.at_level(logging.ERROR):
        asm = build_supervisor(StateDir(tmp_path), isolation=False)
    assert asm.registered == ["one"]  # a conflicting declaration must not stop the others
    assert "skipping two: port allocation failed" in caplog.text


def test_shutdown_timeout_is_clamped_to_the_systemd_budget(caplog):
    from ams.cli import SHUTDOWN_BUDGET_S, shutdown_timeout_for
    from ams.schema import from_dict

    def decl(service_id: str, timeout: float):
        return from_dict(
            {"id": service_id, "start": {"argv": ["/bin/true"]}, "stop": {"timeout_s": timeout}}
        )

    assert shutdown_timeout_for({}) == 5.0
    assert shutdown_timeout_for({"a": decl("a", 2.0), "b": decl("b", 7.5)}) == 7.5
    with caplog.at_level(logging.WARNING):
        assert shutdown_timeout_for({"a": decl("a", 120.0)}) == SHUTDOWN_BUDGET_S
    assert "shutdown budget" in caplog.text


def test_periodic_summary_logs_once_per_interval(tmp_path, caplog):
    from ams.cli import PeriodicSummary, build_supervisor
    from ams.state import StateDir

    _state_with(
        tmp_path,
        "hello",
        'id = "hello"\n[start]\nargv = ["/bin/echo", "hi"]\n[ports]\nmain = 0\n',
    )
    asm = build_supervisor(StateDir(tmp_path), isolation=False)
    now = [100.0]
    summary = PeriodicSummary(asm.supervisor, asm.spawner, interval_s=60.0, clock=lambda: now[0])
    with caplog.at_level(logging.INFO, logger="ams.cli"):
        summary([])  # too early: constructed at 100, next due at 160
        assert "summary hello" not in caplog.text
        now[0] = 160.0
        summary([])
    assert "summary hello status=stopped pid=None uptime=- healthy=None" in caplog.text
    assert "ports=main:" in caplog.text


def test_provision_without_the_runtime_module(monkeypatch, capsys):
    import ams

    # Both are needed: `from ams import runtime` resolves the package attribute
    # first if another test in this session already imported it.
    monkeypatch.delattr(ams, "runtime", raising=False)
    monkeypatch.setitem(sys.modules, "ams.runtime", None)
    assert main(["provision"]) == 2
    assert "ams provision needs ams.runtime" in capsys.readouterr().err


def test_harness_user_comes_from_the_real_uid_not_the_environment(monkeypatch):
    """$USER is unset under some systemd-run invocations and is not authoritative.

    The name keys the /etc/subuid lookup that every service identity is carved
    from, so it must follow the uid.
    """
    import pwd

    from ams.cli import harness_user

    expected = pwd.getpwuid(os.getuid()).pw_name
    monkeypatch.setenv("USER", "definitely-not-the-real-user")
    monkeypatch.setenv("LOGNAME", "definitely-not-the-real-user")
    assert harness_user() == expected


def test_corrupt_allocator_state_exits_2_with_an_actionable_message(tmp_path, capsys):
    """A corrupt ports.json must not be a traceback, and must not be re-carved."""
    from ams.cli import Unavailable, build_supervisor
    from ams.state import StateDir

    _state_with(tmp_path, "hello", 'id = "hello"\n[start]\nargv = ["/bin/echo"]\n')
    state = StateDir(tmp_path)
    state.ensure()
    state.ports_state.write_text('{"version": 1, "ports": {trunca', encoding="utf-8")

    with pytest.raises(Unavailable) as excinfo:
        build_supervisor(state, isolation=False)
    message = str(excinfo.value)
    assert "ports.json" in message
    assert str(state.runtime_state_dir) in message

    # and the CLI turns that into exit 2 on stderr, not a traceback
    rc = main(["run", "--no-isolation", "--state-dir", str(tmp_path), "--escalate", "null"])
    assert rc == 2
    assert "ports.json" in capsys.readouterr().err


def test_provisioning_is_never_reachable_from_the_supervisor_loop(tmp_path, monkeypatch):
    """provision() blocks for seconds to minutes; the loop is single-threaded.

    Only the explicit pre-start step may call it, so starting a service must not
    reach it even when the runtime layer is fully wired.
    """
    runtime = pytest.importorskip("ams.runtime", reason="runtime layer not written yet")
    from ams.cli import build_supervisor
    from ams.state import StateDir

    def explode(*args, **kwargs):
        raise AssertionError("provision() called from the supervision loop")

    monkeypatch.setattr(runtime, "provision", explode)
    _state_with(
        tmp_path,
        "hello",
        'id = "hello"\n[start]\nargv = ["/bin/echo", "hi"]\n[runtime]\nkind = "none"\n',
    )
    asm = build_supervisor(StateDir(tmp_path), isolation=False)
    asm.start_all()  # would raise if start() provisioned
    for _ in range(5):
        asm.supervisor.run_once(0.01)
    asm.supervisor.shutdown(0.5)


def test_provision_never_needs_a_delegated_cgroup(tmp_path, monkeypatch, capsys):
    """`ams provision` runs from an ordinary shell, next to a live harness.

    It needs each service's uid block and the admin namespace; it never runs a
    service, so it never needs a cgroup. Building the isolated spawner just to
    reach the allocator made it exit 2 with "cgroup ... is not delegated" from
    any normal login session -- which is exactly where an operator invokes it.
    Poisoning CgroupRoot.discover proves the provisioning chain never goes there.
    """
    runtime = pytest.importorskip("ams.runtime", reason="runtime layer not written yet")
    from ams import cgroup, cli, userns
    from ams.uidmap import UidBlock

    def no_cgroups(*_a, **_kw):
        raise AssertionError("ams provision must not touch ams.cgroup")

    monkeypatch.setattr(cgroup.CgroupRoot, "discover", no_cgroups)

    block = UidBlock(100_000, 100_000, 1024)
    monkeypatch.setattr(cli, "uid_allocator", lambda _state: _FakeAllocator(block))
    # Needs a real user namespace; the point here is the cgroup, not the chown.
    monkeypatch.setattr(userns, "ensure_service_root", lambda *_a, **_kw: None)

    seen: list[tuple[str, object]] = []

    def fake_provision(decl, root, store, blk, **_kw):
        seen.append((decl.id, blk))
        return runtime.RuntimeEnv()

    monkeypatch.setattr(runtime, "provision", fake_provision)
    monkeypatch.setenv("AMS_STORE_DIR", str(tmp_path / "store"))
    _state_with(
        tmp_path,
        "hello",
        'id = "hello"\n[start]\nargv = ["/bin/echo", "hi"]\n[runtime]\nkind = "none"\n',
    )

    assert main(["provision", "hello", "--state-dir", str(tmp_path)]) == 0
    assert seen == [("hello", block)], seen
    assert capsys.readouterr().out.strip() == "OK hello (none)"


class _FakeAllocator:
    def __init__(self, block):
        self.block = block

    def allocate(self, _service_id):
        return self.block


# ------------------------------------------------------------------------- ctl


def test_ctl_parser_defaults():
    args = build_parser().parse_args(["ctl", "status"])
    assert args.command == "ctl"
    assert args.op == "status"
    assert args.id is None
    assert args.timeout == 10.0
    with_id = build_parser().parse_args(["ctl", "restart", "kvservice"])
    assert with_id.id == "kvservice"


def test_ctl_rejects_an_unknown_op():
    with pytest.raises(SystemExit) as excinfo:
        build_parser().parse_args(["ctl", "self-destruct"])
    assert excinfo.value.code == 2


def test_ctl_without_a_running_harness_exits_2(monkeypatch, capsys):
    """Exit 2 = "nothing to talk to", distinct from 1 = "the harness said no"."""
    import shutil
    import tempfile

    monkeypatch.delenv("AMS_STATE_DIR", raising=False)
    # Not tmp_path: pytest's paths overflow a unix socket's 104-byte sun_path on
    # macOS, which is a different (also handled) error. See test below.
    state_dir = tempfile.mkdtemp(prefix="ams-cli-", dir="/tmp")
    try:
        assert main(["ctl", "ping", "--state-dir", state_dir, "--timeout", "1"]) == 2
    finally:
        shutil.rmtree(state_dir, ignore_errors=True)
    err = capsys.readouterr().err
    assert "no harness listening" in err
    assert "control.sock" in err
    assert "ams-harness" in err, "the message must name the fix"


def test_ctl_names_a_state_dir_too_deep_for_a_unix_socket(tmp_path, monkeypatch, capsys):
    """sun_path is ~104 bytes; "cannot connect" would send the reader hunting
    for a stopped harness that is in fact running."""
    monkeypatch.delenv("AMS_STATE_DIR", raising=False)
    deep = tmp_path / ("d" * 60) / ("e" * 60)
    deep.mkdir(parents=True)
    assert main(["ctl", "ping", "--state-dir", str(deep), "--timeout", "1"]) == 2
    err = capsys.readouterr().err
    assert "too long for a unix socket" in err
    assert "shorter path" in err


def test_ctl_exit_code_follows_the_response_ok_flag(tmp_path, monkeypatch, capsys):
    """The harness answered; 'ok' decides 0 vs 1, and the JSON reaches stdout."""
    from ams import control

    responses = iter([{"ok": True, "pong": True}, {"ok": False, "error": "unknown service 'x'"}])
    monkeypatch.setattr(control, "request", lambda *a, **kw: next(responses))

    assert main(["ctl", "ping", "--state-dir", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "pong": True}

    assert main(["ctl", "restart", "x", "--state-dir", str(tmp_path)]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "unknown service 'x'"


def test_the_cli_and_the_control_module_agree_on_the_op_list():
    from ams.cli import CTL_OPS
    from ams.control import OPS

    assert set(CTL_OPS) == set(OPS)
