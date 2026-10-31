import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from ams.events import Severity
from ams.ops.config import parse
from ams.ops.observe import health, launchd, logs
from ams.ops.runner import CmdResult

PRINT = """gui/501/app.example.chrome-cookie-proxy = {
\tactive count = 1
\tstate = running

\tprogram = /x
\tenvironment = {
\t\tstate = bogus
\t}
\truns = 3
\tpid = 72006
\tlast exit code = 0
\tlast terminating signal = Killed: 9
}"""


def test_parse_print_top_level_only():
    j = launchd.parse_print(PRINT)
    assert j.running and j.pid == 72006 and j.runs == 3 and j.last_exit == 0
    assert j.state == "running" and j.last_signal == "Killed: 9"


def test_parse_print_not_running_never_exited():
    j = launchd.parse_print("x = {\n\tstate = not running\n\tlast exit code = (never exited)\n}")
    assert j.loaded and not j.running and j.pid is None and j.last_exit is None


def test_read_job_unloaded():
    runner = lambda argv, timeout=30: CmdResult(113, "", 'Could not find service "x" in domain\n')  # noqa: E731
    j = launchd.read_job("gui/501/x", runner)
    assert not j.loaded and "Could not find" in j.detail


def test_http_probe_and_socket():
    import pathlib
    import tempfile

    tmp_path = pathlib.Path(tempfile.mkdtemp(dir="/tmp"))

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200 if self.path == "/ok" else 503)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    assert health.http(base + "/ok").ok
    assert health.http(base + "/bad").detail == "HTTP 503"
    assert health.http(base + "/bad", accept=(503,)).ok  # e.g. 401 = "alive, wants a login"
    srv.shutdown()
    assert not health.http(base + "/ok", timeout=0.5).ok
    sp = tmp_path / "p.sock"
    s = socket.socket(socket.AF_UNIX)
    s.bind(str(sp))
    s.listen(1)
    assert health.unix_socket(sp).ok
    s.close()
    assert not health.unix_socket(tmp_path / "missing.sock").ok


def test_tail_first_sight_starts_at_end_then_reads_new(tmp_path):
    f = tmp_path / "a.log"
    f.write_text("old1\nold2\n")
    e: dict = {}
    assert logs.read_new(f, e)[0] == []
    with open(f, "a") as fh:
        fh.write("new1\npart")
    assert logs.read_new(f, e)[0] == ["new1"]  # partial line waits
    with open(f, "a") as fh:
        fh.write("ial\n")
    assert logs.read_new(f, e)[0] == ["partial"]


def test_tail_seed_reads_history_and_survives_rotation(tmp_path):
    f = tmp_path / "a.log"
    f.write_text("h1\nh2\n")
    e: dict = {}
    assert logs.read_new(f, e, seed=True)[0] == ["h1", "h2"]
    f.rename(tmp_path / "a.log.1")
    f.write_text("fresh\n")
    assert logs.read_new(f, e)[0] == ["fresh"]  # new inode -> from 0
    f.write_text("x\n")  # truncated (shorter)
    assert logs.read_new(f, e)[0] == ["x"]


def test_tail_missing_file_is_quiet(tmp_path):
    assert logs.read_new(tmp_path / "none.log", {}) == ([], 0)


def test_tail_flood_is_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(logs, "MAX_READ", 100)
    f = tmp_path / "a.log"
    f.write_text("")
    e: dict = {}
    logs.read_new(f, e)
    f.write_text("".join(f"line-{i:03d}\n" for i in range(1, 60)))
    lines, skipped = logs.read_new(f, e)
    assert skipped > 0 and len(lines) <= 11 and lines[-1] == "line-059"


def test_classify_and_noteworthy():
    cfg = parse(
        {
            "service": [
                {
                    "id": "tcc-host",
                    "log_format": "plain",
                    "ignore": ["^IGNORE"],
                    "escalate": [r" (deny|fail) "],
                }
            ]
        }
    )
    svc = cfg.services[0]
    assert logs.classify(svc, "2026-10-31T03:38:49Z start service=asr pid=1") == Severity.INFO
    assert logs.classify(svc, "2026-10-31T03:38:49Z deny service=x reason=y") == Severity.ERROR
    got = list(logs.noteworthy(svc, ["ok", "IGNORE ERROR boom", "ERROR boom", "WARNING hmm"]))
    assert [g[0] for g in got] == ["ERROR boom", "WARNING hmm"]


def test_stated_level_beats_message_body():
    svc = parse({"service": [{"id": "d"}]}).services[0]
    info = "2026-10-30 23:48:08,615 INFO [watch] yt2bili.watcher: feed fetch HTTP Error 404: Not Found failed (attempt 1/4)"
    warn = (
        "2026-10-30 23:48:08,615 WARNING [watch] yt2bili.watcher: RSS feed failed after 4 attempts"
    )
    br = "2026-10-31 03:38:49,001 [WARNING] registry: heartbeat failed for asr: timed out"
    assert logs.classify(svc, info) == Severity.INFO
    assert logs.classify(svc, warn) == Severity.WARNING
    assert logs.classify(svc, br) == Severity.WARNING
    assert logs.classify(svc, "ERROR: [youtube] abc: This live event will begin") >= Severity.ERROR
    plain = parse({"service": [{"id": "d", "log_format": "plain"}]}).services[0]
    assert logs.classify(plain, warn) >= Severity.WARNING  # plain = words only
