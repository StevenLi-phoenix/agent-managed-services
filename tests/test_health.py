"""Health check tests: probes in isolation, then driven by the supervisor."""

from __future__ import annotations

import json
import math
import socket
import sys
import threading
import time

from ams.events import Event, HealthChanged, LogLine
from ams.health import HealthMonitor, check_http, check_tcp
from ams.schema import HealthSpec, loads
from ams.spawn import PlainSpawner
from ams.supervisor import Supervisor


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def pump(sup: Supervisor, until, seconds: float = 8.0) -> list[Event]:
    events: list[Event] = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        events += sup.run_once(0.02)
        if until(events):
            return events
    raise AssertionError(f"condition never met after {seconds}s; events={events}")


def service(code: str, health: str, port: int, sid: str = "svc"):
    argv = json.dumps([sys.executable, "-c", code])
    return loads(
        f'id = "{sid}"\n[start]\nargv = {argv}\n[ports]\nmain = {port}\n'
        f"[health]\n{health}\n[stop]\ntimeout_s = 1.0\n"
    )


# ------------------------------------------------------------------- probes


def test_check_tcp_true_when_listening_false_when_not():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    port = listener.getsockname()[1]
    try:
        assert check_tcp(port, 1.0) is True
    finally:
        listener.close()
    assert check_tcp(port, 0.5) is False


def test_check_http_status_classes(tmp_path):
    from http.server import HTTPServer, SimpleHTTPRequestHandler

    (tmp_path / "index.html").write_text("hi", encoding="utf-8")

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(tmp_path), **kw)

        def log_message(self, *a):  # keep the test output clean
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        ok, detail = check_http(port, "/", 2.0)
        assert ok is True and "200" in detail
        bad, detail404 = check_http(port, "/missing", 2.0)
        assert bad is False and "404" in detail404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    dead, detail = check_http(port, "/", 0.5)
    assert dead is False and detail


# ------------------------------------------------------------------ monitor


def test_monitor_none_kind_is_healthy_once_then_never_due():
    m = HealthMonitor(HealthSpec(kind="none"))
    m.start(0.0)
    assert m.due(0.0)
    healthy, detail = m.check(0.0)
    assert healthy is True and detail
    assert m.next_due == math.inf


def test_monitor_log_pattern_is_sticky():
    m = HealthMonitor(HealthSpec(kind="log", pattern=r"Listening on \d+"))
    m.start(0.0)
    assert m.next_due == math.inf  # log kind is never polled
    assert m.observe_log("starting up") is None
    assert m.observe_log("Listening on 8080") is True
    assert m.observe_log("Listening on 8080") is None  # only the transition
    assert m.check(1.0) == (None, "")


def test_monitor_suppresses_failures_during_the_start_period():
    port = free_port()  # nothing is listening there
    spec = HealthSpec(kind="tcp", port="main", interval_s=0.01, timeout_s=0.2, start_period_s=1.0)
    m = HealthMonitor(spec, {"main": port})
    m.start(0.0)
    assert m.in_start_period(0.5)
    healthy, detail = m.check(0.5)
    assert healthy is None and detail  # suppressed, but the reason is kept
    healthy, detail = m.check(1.5)
    assert healthy is False and detail


def test_monitor_reports_missing_port_allocation():
    m = HealthMonitor(HealthSpec(kind="tcp", port="main", start_period_s=0.0), {})
    m.start(0.0)
    healthy, detail = m.check(0.0)
    assert healthy is False and "no allocated port" in detail


# ------------------------------------------------- supervisor-driven checks


def test_tcp_health_transitions_to_healthy_exactly_once(tmp_path):
    port = free_port()
    code = (
        "import socket, time\n"
        "s = socket.socket()\n"
        "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        "s.bind(('127.0.0.1', int('${PORT_main}')))\n"
        "s.listen(8)\n"
        "time.sleep(30)\n"
    )
    d = service(
        code,
        'kind = "tcp"\nport = "main"\ninterval_s = 0.05\ntimeout_s = 0.5\nstart_period_s = 5.0',
        port,
    )
    sup = Supervisor(PlainSpawner(), reset_window_min_s=0.3, eof_grace_s=0.3)
    st = sup.add(d, tmp_path / "svc", {"main": port})
    sup.start("svc")
    events = pump(sup, lambda ev: any(isinstance(e, HealthChanged) and e.healthy for e in ev))
    changes = [e for e in events if isinstance(e, HealthChanged)]
    assert len(changes) == 1, changes  # early failures suppressed by start_period_s
    assert changes[0].healthy is True and "connected" in changes[0].detail
    assert st.healthy is True and st.status == "running"
    # steady state produces no further transitions
    more = []
    for _ in range(10):
        more += sup.run_once(0.02)
    assert [e for e in more if isinstance(e, HealthChanged)] == []
    sup.shutdown(1.0)


def test_http_health_reports_healthy(tmp_path):
    port = free_port()
    workdir = tmp_path / "svc"
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "index.html").write_text("ok", encoding="utf-8")
    code = (
        "import http.server, socketserver, os\n"
        "socketserver.TCPServer.allow_reuse_address = True\n"
        "h = http.server.SimpleHTTPRequestHandler\n"
        "with socketserver.TCPServer(('127.0.0.1', int(os.environ['PORT_main'])), h) as srv:\n"
        "    srv.serve_forever()\n"
    )
    d = service(
        code,
        'kind = "http"\nport = "main"\npath = "/"\ninterval_s = 0.05\n'
        "timeout_s = 1.0\nstart_period_s = 5.0",
        port,
    )
    sup = Supervisor(PlainSpawner(), reset_window_min_s=0.3, eof_grace_s=0.3)
    st = sup.add(d, workdir, {"main": port})
    sup.start("svc")
    events = pump(sup, lambda ev: any(isinstance(e, HealthChanged) and e.healthy for e in ev))
    change = next(e for e in events if isinstance(e, HealthChanged))
    assert change.healthy is True and "200" in change.detail
    assert st.healthy is True
    sup.shutdown(1.0)


def test_log_health_marks_healthy_on_pattern(tmp_path):
    code = "import time\nprint('Listening on 9999', flush=True)\ntime.sleep(30)\n"
    d = service(
        code,
        'kind = "log"\npattern = "Listening on \\\\d+"\nstart_period_s = 5.0',
        free_port(),
    )
    sup = Supervisor(PlainSpawner(), reset_window_min_s=0.3, eof_grace_s=0.3)
    st = sup.add(d, tmp_path / "svc", {"main": 1})
    sup.start("svc")
    events = pump(sup, lambda ev: any(isinstance(e, HealthChanged) for e in ev))
    change = next(e for e in events if isinstance(e, HealthChanged))
    assert change.healthy is True and "matched" in change.detail
    assert st.status == "running"
    # the log line itself is still delivered as an event
    assert any(isinstance(e, LogLine) and "Listening on" in e.text for e in events)
    sup.shutdown(1.0)
