"""Control channel + hot reload.

Everything here runs against ``PlainSpawner`` with real ``python -c`` children,
so the protocol, the selector integration and the reload reconciliation are all
verified on any POSIX box, independently of the Linux isolation layer.

Two structural notes:

- The server is served *from* the supervision loop, so a blocking client and a
  pumped loop cannot live on the same thread. Every request here therefore runs
  on a throwaway worker thread while the test pumps ``run_once`` -- which is
  exactly the real topology (client in another process, loop in the harness),
  not a test-only shortcut.
- ``tmp_path`` is not usable for the socket: pytest's temp paths are ~115 bytes
  and ``sun_path`` is 104 on macOS / 108 on Linux, so ``bind`` fails with
  ENAMETOOLONG. The ``state_root`` fixture makes a short ``/tmp`` directory
  instead. Production is fine (``/home/harness/store/state/control.sock``).
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import stat
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from ams import control
from ams.cli import Assembly, build_supervisor
from ams.control import MAX_REQUEST_BYTES, ControlServer, control_socket_path
from ams.events import Event, LogLine
from ams.reload import content_hash, drain_pending_removals, reload
from ams.state import StateDir
from ams.supervisor import Supervisor

pytestmark = pytest.mark.timeout(120)

# The box may be slow (1 vCPU in CI); every wait is bounded but generous.
WAIT_S = 10.0
PUMP_SLICE_S = 0.02


# --------------------------------------------------------------------- fixtures


@pytest.fixture
def state_root() -> Iterator[Path]:
    """A state dir short enough for an AF_UNIX path. See the module docstring."""
    path = Path(tempfile.mkdtemp(prefix="ams-ctl-", dir="/tmp"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


# ---------------------------------------------------------------------- helpers


def service_toml(service_id: str, code: str, *, extra: str = "", top: str = "") -> str:
    """``extra`` goes after ``[start]`` (so it must open its own table); ``top``
    goes in the root table before it (``secrets``, ``env``, ...)."""
    argv = json.dumps([sys.executable, "-c", code])
    return f'id = "{service_id}"\n{top}[start]\nargv = {argv}\n{extra}'


def write_decl(state: StateDir, service_id: str, text: str) -> Path:
    path = state.service_decl_path(service_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def make_asm(state_root: Path, **decls: str) -> Assembly:
    """A real ``build_supervisor`` assembly (PlainSpawner) over written declarations."""
    state = StateDir(state_root)
    state.ensure()
    for service_id, text in decls.items():
        write_decl(state, service_id, text)
    return build_supervisor(state, isolation=False, reset_window_min_s=0.3)


def pump(sup: Supervisor, until: Callable[[], bool], seconds: float = WAIT_S) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        sup.run_once(PUMP_SLICE_S)
        if until():
            return True
    return until()


def pump_events(sup: Supervisor, sink: list[Event], seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        sink.extend(sup.run_once(PUMP_SLICE_S))


def texts(events: list[Event]) -> list[str]:
    return [e.text for e in events if isinstance(e, LogLine)]


def drive(sup: Supervisor, futures: list[Future[Any]], seconds: float = WAIT_S) -> None:
    """Pump the loop until every future is done (or the deadline)."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not all(f.done() for f in futures):
        sup.run_once(PUMP_SLICE_S)


def call(
    sup: Supervisor,
    path: Path,
    op: str,
    service_id: str | None = None,
    *,
    seconds: float = WAIT_S,
) -> dict[str, Any]:
    """One `ams ctl` request, issued off-thread while the loop is pumped."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(control.request, path, op, service_id, timeout_s=seconds)
        drive(sup, [future], seconds)
        return future.result(timeout=seconds)


def raw_call(sup: Supervisor, path: Path, payload: bytes, *, seconds: float = WAIT_S) -> str:
    """Send arbitrary bytes and return the first response line as text."""

    def talk() -> str:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(seconds)
        try:
            sock.connect(str(path))
            try:
                sock.sendall(payload)
            except BrokenPipeError:  # server answered and closed mid-write
                pass
            buf = bytearray()
            while b"\n" not in buf:
                chunk = sock.recv(8192)
                if not chunk:
                    break
                buf.extend(chunk)
            return bytes(buf).split(b"\n", 1)[0].decode("utf-8", "replace")
        finally:
            sock.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(talk)
        drive(sup, [future], seconds)
        return future.result(timeout=seconds)


SLEEPER = "import sys, time; print('up', flush=True); time.sleep(30)"


@pytest.fixture
def served(state_root: Path) -> Iterator[tuple[Assembly, ControlServer, Path]]:
    """An assembly with one long-lived service and an open control server."""
    asm = make_asm(
        state_root,
        hello=service_toml("hello", SLEEPER, extra="[stop]\ntimeout_s = 2.0\n"),
    )
    path = control_socket_path(asm.state)
    server = ControlServer(path, asm.supervisor, reload_fn=lambda: reload(asm))
    server.open()
    try:
        yield asm, server, path
    finally:
        server.close()
        asm.supervisor.shutdown(2.0)


# ----------------------------------------------------------------------- server


def test_ping_answers_ok(served: tuple[Assembly, ControlServer, Path]) -> None:
    asm, _server, path = served
    response = call(asm.supervisor, path, "ping")
    assert response["ok"] is True
    assert response["pong"] is True
    assert response["pid"] == os.getpid()


def test_status_reports_every_registered_service(
    served: tuple[Assembly, ControlServer, Path],
) -> None:
    asm, _server, path = served
    asm.start_all()
    pump(asm.supervisor, lambda: asm.supervisor.services["hello"].status == "running")

    response = call(asm.supervisor, path, "status")
    assert response["ok"] is True
    row = response["services"]["hello"]
    assert row["id"] == "hello"
    assert row["status"] == "running"
    assert row["desired"] == "up"
    assert isinstance(row["pid"], int)
    # The whole payload survived a JSON round trip, which is the contract.
    assert set(row) >= {"status", "desired", "pid", "attempt", "healthy", "ports", "cgroup"}


def test_stop_then_start_over_the_socket(served: tuple[Assembly, ControlServer, Path]) -> None:
    asm, _server, path = served
    sup = asm.supervisor
    asm.start_all()
    pump(sup, lambda: sup.services["hello"].status == "running")

    assert call(sup, path, "stop", "hello")["ok"] is True
    assert pump(sup, lambda: sup.services["hello"].status == "stopped")
    assert sup.services["hello"].spawned is None

    assert call(sup, path, "start", "hello")["ok"] is True
    assert pump(sup, lambda: sup.services["hello"].status == "running")
    assert sup.services["hello"].spawned is not None


def test_restart_replaces_the_pid(served: tuple[Assembly, ControlServer, Path]) -> None:
    asm, _server, path = served
    sup = asm.supervisor
    asm.start_all()
    pump(sup, lambda: sup.services["hello"].status == "running")
    before = sup.services["hello"].pid

    assert call(sup, path, "restart", "hello")["ok"] is True
    assert pump(sup, lambda: sup.services["hello"].pid not in (None, before))
    after = sup.services["hello"].pid
    assert after is not None and after != before


def test_kill_takes_the_service_down(served: tuple[Assembly, ControlServer, Path]) -> None:
    asm, _server, path = served
    sup = asm.supervisor
    asm.start_all()
    pump(sup, lambda: sup.services["hello"].status == "running")

    assert call(sup, path, "kill", "hello")["ok"] is True
    assert pump(sup, lambda: sup.services["hello"].status == "stopped")
    assert sup.services["hello"].desired == "down"


def test_unknown_id_is_an_error_not_a_crash(served: tuple[Assembly, ControlServer, Path]) -> None:
    asm, _server, path = served
    response = call(asm.supervisor, path, "restart", "nosuch")
    assert response["ok"] is False
    assert "nosuch" in response["error"]
    # and the loop is still healthy afterwards
    assert call(asm.supervisor, path, "ping")["ok"] is True


def test_a_per_service_op_without_an_id_is_rejected(
    served: tuple[Assembly, ControlServer, Path],
) -> None:
    asm, _server, path = served
    response = call(asm.supervisor, path, "start")
    assert response["ok"] is False
    assert "needs a service 'id'" in response["error"]


def test_unknown_op_is_rejected(served: tuple[Assembly, ControlServer, Path]) -> None:
    asm, _server, path = served
    line = raw_call(asm.supervisor, path, b'{"op": "self-destruct"}\n')
    parsed = json.loads(line)
    assert parsed["ok"] is False
    assert "unknown op" in parsed["error"]


def test_malformed_json_gets_an_error_response(
    served: tuple[Assembly, ControlServer, Path],
) -> None:
    asm, _server, path = served
    line = raw_call(asm.supervisor, path, b"{not json at all\n")
    parsed = json.loads(line)
    assert parsed["ok"] is False
    assert "malformed JSON" in parsed["error"]
    assert call(asm.supervisor, path, "ping")["ok"] is True


def test_a_non_object_request_is_rejected(served: tuple[Assembly, ControlServer, Path]) -> None:
    asm, _server, path = served
    parsed = json.loads(raw_call(asm.supervisor, path, b"[1, 2, 3]\n"))
    assert parsed["ok"] is False
    assert "JSON object" in parsed["error"]


def test_an_oversized_request_line_is_refused_not_buffered(
    served: tuple[Assembly, ControlServer, Path],
) -> None:
    """The cap exists so a client that never sends a newline cannot grow our RSS."""
    asm, _server, path = served
    payload = b"x" * (MAX_REQUEST_BYTES + 16)  # deliberately no newline
    parsed = json.loads(raw_call(asm.supervisor, path, payload))
    assert parsed["ok"] is False
    assert "exceeds" in parsed["error"]
    assert call(asm.supervisor, path, "ping")["ok"] is True


def test_concurrent_clients_are_all_answered(
    served: tuple[Assembly, ControlServer, Path],
) -> None:
    asm, _server, path = served
    sup = asm.supervisor
    asm.start_all()
    pump(sup, lambda: sup.services["hello"].status == "running")

    ops = ["ping", "status", "ping", "status", "ping", "status", "ping", "status"]
    with ThreadPoolExecutor(max_workers=len(ops)) as pool:
        futures = [pool.submit(control.request, path, op, None, timeout_s=WAIT_S) for op in ops]
        drive(sup, futures, WAIT_S)
        results = [f.result(timeout=WAIT_S) for f in futures]
    assert [r["ok"] for r in results] == [True] * len(ops)
    assert all("services" in r for r in results if "pong" not in r)


def test_the_socket_is_private_to_the_harness_user(
    served: tuple[Assembly, ControlServer, Path],
) -> None:
    """0600 is the whole authorisation story (see ams.control's docstring)."""
    _asm, _server, path = served
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600, oct(mode)
    assert stat.S_ISSOCK(path.stat().st_mode)


def test_a_stale_socket_file_is_replaced(state_root: Path) -> None:
    """A SIGKILLed harness leaves the node behind; bind would fail forever."""
    asm = make_asm(state_root, hello=service_toml("hello", "print('x')"))
    path = control_socket_path(asm.state)
    orphan = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    orphan.bind(str(path))
    orphan.close()  # closing does NOT unlink
    assert path.exists()

    server = ControlServer(path, asm.supervisor)
    server.open()
    try:
        assert call(asm.supervisor, path, "ping")["ok"] is True
    finally:
        server.close()
    assert not path.exists(), "close() must unlink the socket"


def test_a_non_socket_at_the_path_is_never_removed(state_root: Path) -> None:
    asm = make_asm(state_root, hello=service_toml("hello", "print('x')"))
    path = control_socket_path(asm.state)
    path.write_text("precious data", encoding="utf-8")
    server = ControlServer(path, asm.supervisor)
    with pytest.raises(OSError, match="not a socket"):
        server.open()
    assert path.read_text(encoding="utf-8") == "precious data"


def test_reload_is_refused_when_no_handler_is_configured(state_root: Path) -> None:
    asm = make_asm(state_root, hello=service_toml("hello", "print('x')"))
    path = control_socket_path(asm.state)
    server = ControlServer(path, asm.supervisor)  # no reload_fn
    server.open()
    try:
        response = call(asm.supervisor, path, "reload")
        assert response["ok"] is False
        assert "not configured" in response["error"]
    finally:
        server.close()


def test_the_client_reports_an_absent_socket_distinctly(state_root: Path) -> None:
    with pytest.raises(control.ControlError, match="no harness listening"):
        control.request(state_root / "control.sock", "ping", timeout_s=1.0)


# ----------------------------------------------------------------------- reload


def test_reload_adds_and_starts_a_new_declaration(state_root: Path) -> None:
    asm = make_asm(state_root, hello=service_toml("hello", SLEEPER))
    sup = asm.supervisor
    asm.start_all()
    pump(sup, lambda: sup.services["hello"].status == "running")
    hello_pid = sup.services["hello"].pid

    write_decl(asm.state, "second", service_toml("second", SLEEPER))
    summary = reload(asm)

    assert summary["added"] == ["second"]
    assert summary["changed"] == [] and summary["removed"] == []
    assert summary["unchanged"] == 1
    assert summary["errors"] == {}
    assert pump(sup, lambda: sup.services["second"].status == "running")
    assert sup.services["hello"].pid == hello_pid, "an untouched service must not restart"
    sup.shutdown(2.0)


def test_reload_stops_and_removes_a_deleted_declaration(state_root: Path) -> None:
    asm = make_asm(
        state_root,
        hello=service_toml("hello", SLEEPER, extra="[stop]\ntimeout_s = 2.0\n"),
        doomed=service_toml(
            "doomed", SLEEPER, extra="[stop]\ntimeout_s = 2.0\n[ports]\nmain = 0\n"
        ),
    )
    sup = asm.supervisor
    asm.start_all()
    pump(sup, lambda: sup.services["doomed"].status == "running")
    assert asm.ports.get("doomed")

    asm.state.service_decl_path("doomed").unlink()
    summary = reload(asm)
    assert summary["removed"] == ["doomed"]
    # Removal is two-phase and this proves the second phase is real: the child
    # has been signalled but not yet reaped, so it cannot be dropped yet.
    assert asm.pending_removals == {"doomed"}
    assert "doomed" in sup.services

    # The drop lands once the child is actually gone.
    def gone() -> bool:
        drain_pending_removals(asm)
        return "doomed" not in sup.services

    assert pump(sup, gone), f"still pending: {asm.pending_removals}"
    assert asm.pending_removals == set()
    assert "doomed" not in asm.declarations
    assert "doomed" not in asm.registered
    assert asm.ports.get("doomed") == {}, "a removed service releases its ports"
    assert sup.services["hello"].status == "running"
    sup.shutdown(2.0)


def test_a_changed_declaration_is_restarted_with_the_new_argv(state_root: Path) -> None:
    v1 = "import time; print('VERSION-1', flush=True); time.sleep(30)"
    v2 = "import time; print('VERSION-2', flush=True); time.sleep(30)"
    asm = make_asm(
        state_root,
        hello=service_toml("hello", SLEEPER),
        app=service_toml("app", v1, extra="[stop]\ntimeout_s = 2.0\n"),
    )
    sup = asm.supervisor
    asm.start_all()
    seen: list[Event] = []
    assert pump(sup, lambda: sup.services["app"].status == "running")
    pump_events(sup, seen, 0.6)
    assert any("VERSION-1" in t for t in texts(seen))
    old_pid = sup.services["app"].pid
    hello_pid = sup.services["hello"].pid

    write_decl(asm.state, "app", service_toml("app", v2, extra="[stop]\ntimeout_s = 2.0\n"))
    summary = reload(asm)
    assert summary["changed"] == ["app"]
    assert summary["added"] == [] and summary["removed"] == []
    assert summary["unchanged"] == 1

    after: list[Event] = []
    deadline = time.monotonic() + WAIT_S
    while time.monotonic() < deadline and not any("VERSION-2" in t for t in texts(after)):
        after.extend(sup.run_once(PUMP_SLICE_S))
    assert any("VERSION-2" in t for t in texts(after)), texts(after)
    assert sup.services["app"].pid not in (None, old_pid)
    assert sup.services["hello"].pid == hello_pid, "only the changed service restarts"
    sup.shutdown(2.0)


def test_a_changed_declaration_keeps_its_port_when_the_request_is_unchanged(
    state_root: Path,
) -> None:
    base = "[ports]\nmain = 0\n[stop]\ntimeout_s = 2.0\n"
    asm = make_asm(state_root, app=service_toml("app", SLEEPER, extra=base))
    sup = asm.supervisor
    asm.start_all()
    pump(sup, lambda: sup.services["app"].status == "running")
    port = sup.services["app"].ports["main"]

    write_decl(asm.state, "app", service_toml("app", SLEEPER + "  # touched", extra=base))
    assert reload(asm)["changed"] == ["app"]
    assert sup.services["app"].ports["main"] == port
    sup.shutdown(2.0)


def test_a_broken_declaration_is_reported_and_the_others_still_reload(
    state_root: Path,
) -> None:
    asm = make_asm(state_root, hello=service_toml("hello", SLEEPER))
    sup = asm.supervisor
    asm.start_all()
    pump(sup, lambda: sup.services["hello"].status == "running")

    write_decl(asm.state, "broken", 'id = "broken"\nthis is not toml\n')
    write_decl(asm.state, "good", service_toml("good", SLEEPER))
    summary = reload(asm)

    assert summary["added"] == ["good"]
    assert "broken" in summary["errors"]
    assert "broken" not in sup.services
    assert pump(sup, lambda: sup.services["good"].status == "running")
    # A broken file is not a removal: `hello` and `good` are untouched.
    assert summary["removed"] == []
    sup.shutdown(2.0)


def test_a_broken_declaration_does_not_remove_an_already_running_service(
    state_root: Path,
) -> None:
    """Editing a live service's toml into garbage must not take it down."""
    asm = make_asm(state_root, app=service_toml("app", SLEEPER, extra="[stop]\ntimeout_s = 2.0\n"))
    sup = asm.supervisor
    asm.start_all()
    pump(sup, lambda: sup.services["app"].status == "running")
    pid = sup.services["app"].pid

    write_decl(asm.state, "app", "this is not toml at all\n")
    summary = reload(asm)
    assert "app" in summary["errors"]
    assert summary["removed"] == []
    assert sup.services["app"].pid == pid
    sup.shutdown(2.0)


def test_reload_is_a_no_op_when_nothing_changed(state_root: Path) -> None:
    asm = make_asm(state_root, hello=service_toml("hello", SLEEPER))
    sup = asm.supervisor
    asm.start_all()
    pump(sup, lambda: sup.services["hello"].status == "running")
    pid = sup.services["hello"].pid

    for _ in range(3):
        summary = reload(asm)
        assert summary == {
            "added": [],
            "removed": [],
            "changed": [],
            "unchanged": 1,
            "errors": {},
            "secrets_missing": [],
        }
    assert sup.services["hello"].pid == pid
    sup.shutdown(2.0)


def test_a_changed_declaration_revives_a_service_the_harness_gave_up_on(
    state_root: Path,
) -> None:
    """Editing the toml is exactly how a crash-looped service gets fixed."""
    broken = "import sys; sys.stderr.write('ERROR: boom\\n'); sys.exit(3)"
    retries = "[restart]\nmax_retries = 1\nbackoff_s = 0.05\n[health]\nstart_period_s = 0.05\n"
    asm = make_asm(state_root, app=service_toml("app", broken, extra=retries))
    sup = asm.supervisor
    asm.start_all()
    assert pump(sup, lambda: sup.services["app"].status == "failed")

    write_decl(asm.state, "app", service_toml("app", SLEEPER, extra=retries))
    assert reload(asm)["changed"] == ["app"]
    assert pump(sup, lambda: sup.services["app"].status == "running")
    assert sup.services["app"].consecutive_failures == 0
    sup.shutdown(2.0)


def test_a_changed_declaration_leaves_an_operator_stopped_service_down(
    state_root: Path,
) -> None:
    asm = make_asm(state_root, app=service_toml("app", SLEEPER, extra="[stop]\ntimeout_s = 2.0\n"))
    sup = asm.supervisor
    asm.start_all()
    pump(sup, lambda: sup.services["app"].status == "running")
    sup.stop("app")
    assert pump(sup, lambda: sup.services["app"].status == "stopped")

    write_decl(asm.state, "app", service_toml("app", SLEEPER + "  # edited"))
    assert reload(asm)["changed"] == ["app"]
    for _ in range(10):
        sup.run_once(0.01)
    assert sup.services["app"].status == "stopped", "operator intent wins over a decl change"
    assert sup.services["app"].spawned is None


def test_reload_over_the_control_socket(served: tuple[Assembly, ControlServer, Path]) -> None:
    asm, _server, path = served
    sup = asm.supervisor
    asm.start_all()
    pump(sup, lambda: sup.services["hello"].status == "running")

    write_decl(asm.state, "extra", service_toml("extra", SLEEPER))
    response = call(sup, path, "reload")
    assert response["ok"] is True
    assert response["reload"]["added"] == ["extra"]
    assert pump(sup, lambda: sup.services["extra"].status == "running")


def test_content_hash_tracks_bytes_not_mtime(state_root: Path) -> None:
    state = StateDir(state_root)
    state.ensure()
    path = write_decl(state, "hello", service_toml("hello", "print(1)"))
    first = content_hash(path)
    os.utime(path, (0, 0))  # mtime moved, bytes did not
    assert content_hash(path) == first
    path.write_text(service_toml("hello", "print(2)"), encoding="utf-8")
    assert content_hash(path) != first


# ------------------------------------------------------------------------ SIGHUP


@pytest.mark.skipif(
    threading.current_thread() is not threading.main_thread(),
    reason="signal handlers need the main thread",
)
def test_sighup_triggers_a_reload_through_the_self_pipe(state_root: Path) -> None:
    """`systemctl reload ams-harness` is `kill -HUP`; it must reach the loop."""
    asm = make_asm(state_root, hello=service_toml("hello", SLEEPER))
    sup = asm.supervisor
    asm.start_all()
    summaries: list[dict[str, Any]] = []

    def on_reload() -> dict[str, Any]:
        summaries.append(reload(asm))
        return summaries[-1]

    write_decl(asm.state, "late", service_toml("late", SLEEPER))
    stop_event = threading.Event()
    hup = threading.Timer(0.4, lambda: os.kill(os.getpid(), signal.SIGHUP))
    done = threading.Timer(2.0, stop_event.set)
    hup.start()
    done.start()
    try:
        sup.run_forever(stop_event, on_reload=on_reload, shutdown_timeout_s=3.0)
    finally:
        hup.cancel()
        done.cancel()

    assert summaries, "SIGHUP did not reach the loop"
    assert summaries[0]["added"] == ["late"]
    assert "late" in asm.declarations


@pytest.mark.skipif(
    threading.current_thread() is not threading.main_thread(),
    reason="signal handlers need the main thread",
)
def test_sighup_without_a_handler_is_logged_not_fatal(state_root: Path, caplog) -> None:
    asm = make_asm(state_root, hello=service_toml("hello", SLEEPER))
    sup = asm.supervisor
    stop_event = threading.Event()
    hup = threading.Timer(0.3, lambda: os.kill(os.getpid(), signal.SIGHUP))
    done = threading.Timer(1.2, stop_event.set)
    hup.start()
    done.start()
    try:
        with caplog.at_level("WARNING", logger="ams.supervisor"):
            sup.run_forever(stop_event, shutdown_timeout_s=2.0)
    finally:
        hup.cancel()
        done.cancel()
    assert "no reload handler" in caplog.text


# ------------------------------------------------------------- secrets (D16)


def test_reload_warns_about_missing_secrets_without_gating_the_others(
    state_root: Path, caplog
) -> None:
    """A declared secret with no stored value is a heads-up, not a reload error.

    The affected service still fails at start (make_extra_env_for raises), which
    is the enforcement; the point here is that the *other* services in the same
    reload go through, and that the reload itself reports rather than refuses.
    """
    asm = make_asm(state_root, hello=service_toml("hello", SLEEPER))
    sup = asm.supervisor
    asm.start_all()
    pump(sup, lambda: sup.services["hello"].status == "running")

    needs = 'secrets = ["SVC_SECRET"]\n'
    write_decl(asm.state, "needy", service_toml("needy", SLEEPER, top=needs))
    write_decl(asm.state, "plain", service_toml("plain", SLEEPER))
    with caplog.at_level("WARNING", logger="ams.secrets"):
        summary = reload(asm)

    assert sorted(summary["added"]) == ["needy", "plain"]
    assert summary["errors"] == {}, "a missing secret is not a reload error"
    assert summary["secrets_missing"] == ["needy"]
    assert "SVC_SECRET" in caplog.text
    assert "ams secret set needy" in caplog.text
    # the service with no secrets is unaffected and comes up
    assert pump(sup, lambda: sup.services["plain"].status == "running")
    sup.shutdown(2.0)


def test_an_untouched_service_does_not_re_warn_on_every_reload(state_root: Path) -> None:
    """Otherwise a known-missing secret logs on every SIGHUP forever."""
    needs = 'secrets = ["SVC_SECRET"]\n'
    asm = make_asm(state_root, needy=service_toml("needy", SLEEPER, top=needs))
    assert reload(asm)["secrets_missing"] == [], "nothing was touched"

    write_decl(asm.state, "needy", service_toml("needy", SLEEPER + "  # edited", top=needs))
    assert reload(asm)["secrets_missing"] == ["needy"], "a changed service is re-checked"
    assert reload(asm)["secrets_missing"] == [], "and then goes quiet again"
    asm.supervisor.shutdown(2.0)


def test_a_broken_secrets_layer_never_fails_a_reload(state_root: Path, monkeypatch) -> None:
    from ams import secrets as secrets_mod

    def explode(*_a, **_kw):
        raise OSError("store unreadable")

    asm = make_asm(state_root, hello=service_toml("hello", SLEEPER))
    # Patched only after construction: build_supervisor calls the same function
    # unguarded, which is the secrets layer's own call site, not reload's.
    monkeypatch.setattr(secrets_mod, "warn_missing_secrets", explode)
    write_decl(asm.state, "second", service_toml("second", SLEEPER))
    summary = reload(asm)
    assert summary["added"] == ["second"]
    assert summary["secrets_missing"] == []
    asm.supervisor.shutdown(2.0)
