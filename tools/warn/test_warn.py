"""Tests for warn.py: run with `python3 -m unittest discover -s lsy/tools/warn` (stdlib only)."""
import io
import json
import os
import socket
import stat
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import warn  # noqa: E402

TOKEN = "t0k3n-for-tests"


class Fake:
    """A fake ingest endpoint recording requests; `status`/`body` can be changed between calls."""

    def __init__(self):
        self.requests = []
        self.status = 202
        self.body = {"outcome": "accepted"}
        self.delay = 0.0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("content-length", "0"))
                raw = self.rfile.read(length)
                outer.requests.append({
                    "auth": self.headers.get("authorization"),
                    "ctype": self.headers.get("content-type"),
                    "path": self.path,
                    "json": json.loads(raw.decode("utf-8")),
                })
                if outer.delay:
                    time.sleep(outer.delay)
                data = json.dumps(outer.body).encode()
                try:
                    self.send_response(outer.status)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    pass

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d/escalate" % self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class WarnTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        cfg = self.home / ".config" / "warn"
        cfg.mkdir(parents=True)
        self.token_file = cfg / "token"
        self.token_file.write_text(TOKEN + "\n")
        os.chmod(self.token_file, 0o600)
        self.fake = Fake()
        self.env = {"HOME": str(self.home), "USERPROFILE": str(self.home), "WARN_URL": self.fake.url}
        self.spool = self.home / ".local" / "state" / "warn" / "spool"

    def tearDown(self):
        self.fake.close()
        self.tmp.cleanup()

    def run_warn(self, *argv, env=None, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        e = dict(self.env)
        e.update(env or {})
        code = warn.main(list(argv), env=e, stdin=io.StringIO(stdin), stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue()

    def spooled(self):
        return sorted(self.spool.glob("*.json")) if self.spool.exists() else []

    # --- event shape -------------------------------------------------------------------------

    def test_sends_event_with_bearer_and_kind(self):
        code, out, _ = self.run_warn("--source", "n8", "--severity", "error", "disk is full")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.fake.requests), 1)
        req = self.fake.requests[0]
        self.assertEqual(req["auth"], "Bearer " + TOKEN)
        self.assertEqual(req["ctype"], "application/json")
        self.assertEqual(req["path"], "/escalate")
        body = req["json"]
        self.assertEqual(body["kind"], "warn.n8.error")
        self.assertEqual(body["title"], "disk is full")
        self.assertEqual(body["subject"], "n8")
        self.assertIn("accepted", out)

    def test_default_key_is_origin_hash_hour_and_stable(self):
        now = 1_788_000_000  # fixed epoch
        k1 = warn.default_key("n8", "disk is full", now)
        k2 = warn.default_key("n8", "disk is full", now + 60)
        k3 = warn.default_key("n8", "disk is full", now + 3600)
        k4 = warn.default_key("n8", "other", now)
        self.assertEqual(k1, k2)
        self.assertNotEqual(k1, k3)
        self.assertNotEqual(k1, k4)
        self.assertRegex(k1, r"^n8-[0-9a-f]{10}-\d{10}$")

    def test_explicit_key_url_text_and_occurred_at(self):
        code, _, _ = self.run_warn("--source", "phm", "--severity", "warn", "--key", "k-1",
                                   "--url", "https://example.com/x", "--text", "details here", "svc down")
        self.assertEqual(code, 0)
        body = self.fake.requests[0]["json"]
        self.assertEqual(body["key"], "k-1")
        self.assertEqual(body["url"], "https://example.com/x")
        self.assertEqual(body["text"], "details here")
        self.assertIsInstance(body["occurredAt"], int)

    def test_text_dash_reads_stdin(self):
        self.run_warn("--source", "n8", "--text", "-", "boom", stdin="line1\nline2\n")
        self.assertEqual(self.fake.requests[0]["json"]["text"], "line1\nline2")

    def test_limits_clip_title_text_key(self):
        self.run_warn("--source", "n8", "--key", "k" * 500, "--text", "x" * 20000, "t" * 900)
        body = self.fake.requests[0]["json"]
        self.assertLessEqual(len(body["title"]), 300)
        self.assertLessEqual(len(body["text"]), 8000)
        self.assertLessEqual(len(body["key"]), 200)

    def test_whitespace_title_collapsed_and_origin_sanitised(self):
        self.run_warn("--source", "My Host_1!", "--severity", "info", "a\n  b")
        body = self.fake.requests[0]["json"]
        self.assertEqual(body["title"], "a b")
        self.assertRegex(body["kind"], r"^warn\.[a-z0-9_.-]+\.info$")
        self.assertEqual(warn.clean_origin("N8.Docker"), "n8.docker")
        self.assertEqual(warn.clean_origin("  "), "unknown")

    def test_bad_severity_is_usage_error(self):
        code, _, err = self.run_warn("--source", "n8", "--severity", "panic", "x")
        self.assertEqual(code, 2)
        self.assertEqual(self.fake.requests, [])
        self.assertIn("severity", err)

    def test_empty_title_is_usage_error(self):
        code, _, _ = self.run_warn("--source", "n8", "   ")
        self.assertEqual(code, 2)

    def test_print_does_not_send(self):
        code, out, _ = self.run_warn("--source", "n8", "--print", "hello")
        self.assertEqual(code, 0)
        self.assertEqual(self.fake.requests, [])
        self.assertEqual(json.loads(out)["kind"], "warn.n8.warn")

    # --- token --------------------------------------------------------------------------------

    def test_missing_token_default_exit0_strict_exit2(self):
        self.token_file.unlink()
        code, _, err = self.run_warn("--source", "n8", "x")
        self.assertEqual(code, 0)
        self.assertIn("token", err)
        code, _, _ = self.run_warn("--source", "n8", "--strict", "x")
        self.assertEqual(code, 2)
        self.assertEqual(self.spooled(), [])

    @unittest.skipIf(sys.platform == "win32", "POSIX permissions")
    def test_loose_token_permissions_warn_but_send(self):
        os.chmod(self.token_file, 0o644)
        code, _, err = self.run_warn("--source", "n8", "x")
        self.assertEqual(code, 0)
        self.assertIn("permissions", err)
        self.assertEqual(len(self.fake.requests), 1)

    def test_token_value_never_in_output(self):
        _, out, err = self.run_warn("--source", "n8", "x")
        self.assertNotIn(TOKEN, out + err)
        self.fake.status = 401
        self.fake.body = {"error": "unknown token"}
        _, out, err = self.run_warn("--source", "n8", "y")
        self.assertNotIn(TOKEN, out + err)

    # --- endpoint -------------------------------------------------------------------------------

    def test_url_comes_from_the_env_or_a_url_file_and_has_no_default(self):
        self.assertIsNone(warn.Env({"HOME": str(self.home)}).url)
        (self.home / ".config" / "warn" / "url").write_text("http://example.invalid/escalate\n")
        self.assertEqual(warn.Env({"HOME": str(self.home)}).url, "http://example.invalid/escalate")
        self.assertEqual(
            warn.Env({"HOME": str(self.home), "WARN_URL": "http://x/y"}).url, "http://x/y"
        )

    def test_no_endpoint_is_reported_and_nothing_is_spooled(self):
        code, _, err = self.run_warn("--source", "n8", "x", env={"WARN_URL": ""})
        self.assertEqual(code, 0)
        self.assertIn("endpoint", err)
        code, _, _ = self.run_warn("--source", "n8", "--strict", "x", env={"WARN_URL": ""})
        self.assertEqual(code, 2)
        self.assertEqual(self.spooled(), [])

    # --- failure handling and spool -----------------------------------------------------------

    def test_server_error_spools_and_next_run_flushes(self):
        self.fake.status = 503
        self.fake.body = {"error": "ingest unavailable"}
        code, _, _ = self.run_warn("--source", "n8", "--severity", "error", "first")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.spooled()), 1)
        saved = json.loads(self.spooled()[0].read_text())
        self.assertEqual(saved["title"], "first")
        self.assertNotIn(TOKEN, self.spooled()[0].read_text())

        self.fake.status = 202
        self.fake.body = {"outcome": "accepted"}
        n_before = len(self.fake.requests)
        code, _, _ = self.run_warn("--source", "n8", "--severity", "info", "second")
        self.assertEqual(code, 0)
        self.assertEqual(self.spooled(), [])
        titles = [r["json"]["title"] for r in self.fake.requests[n_before:]]
        self.assertEqual(sorted(titles), ["first", "second"])
        # the replayed event keeps its original occurredAt and key
        first = [r["json"] for r in self.fake.requests[n_before:] if r["json"]["title"] == "first"][0]
        self.assertEqual(first["occurredAt"], saved["occurredAt"])
        self.assertEqual(first["key"], saved["key"])

    def test_unreachable_spools_strict_exits_1(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        env = {"WARN_URL": "http://127.0.0.1:%d/escalate" % port}
        code, _, _ = self.run_warn("--source", "n8", "x", env=env)
        self.assertEqual(code, 0)
        self.assertEqual(len(self.spooled()), 1)
        code, _, _ = self.run_warn("--source", "n8", "--strict", "y", env=env)
        self.assertEqual(code, 1)
        self.assertEqual(len(self.spooled()), 2)

    def test_timeout_is_enforced_and_spools(self):
        self.fake.delay = 2.0
        t0 = time.time()
        code, _, _ = self.run_warn("--source", "n8", "--strict", "slow", env={"WARN_TIMEOUT": "0.3"})
        self.assertLess(time.time() - t0, 1.8)
        self.assertEqual(code, 1)
        self.assertEqual(len(self.spooled()), 1)

    def test_default_timeout_is_five_seconds(self):
        self.assertEqual(warn.DEFAULT_TIMEOUT, 5.0)

    def test_401_is_not_spooled(self):
        self.fake.status = 401
        self.fake.body = {"error": "unknown token"}
        code, _, err = self.run_warn("--source", "n8", "x")
        self.assertEqual(code, 0)
        self.assertEqual(self.spooled(), [])
        self.assertIn("401", err)
        code, _, _ = self.run_warn("--source", "n8", "--strict", "x")
        self.assertEqual(code, 1)

    def test_400_is_not_spooled(self):
        self.fake.status = 400
        self.fake.body = {"error": "bad kind"}
        self.run_warn("--source", "n8", "x")
        self.assertEqual(self.spooled(), [])

    def test_spool_cap_and_expiry(self):
        self.spool.mkdir(parents=True)
        old = self.spool / "0000000001-old.json"
        old.write_text(json.dumps({"kind": "warn.n8.info", "key": "old", "subject": "n8", "title": "old"}))
        past = time.time() - 8 * 86400
        os.utime(old, (past, past))
        self.run_warn("--source", "n8", "fresh")
        self.assertEqual(self.spooled(), [])
        titles = [r["json"]["title"] for r in self.fake.requests]
        self.assertEqual(titles, ["fresh"])  # expired entry is dropped, not replayed

        self.fake.status = 503
        for i in range(warn.SPOOL_MAX + 5):
            self.run_warn("--source", "n8", "--key", "k%d" % i, "e%d" % i)
        self.assertLessEqual(len(self.spooled()), warn.SPOOL_MAX)

    def test_corrupt_spool_file_is_removed(self):
        self.spool.mkdir(parents=True)
        bad = self.spool / "0000000002-bad.json"
        bad.write_text("{not json")
        self.run_warn("--source", "n8", "ok")
        self.assertFalse(bad.exists())

    def test_log_file_written(self):
        self.run_warn("--source", "n8", "logged")
        log = self.home / ".local" / "state" / "warn" / "warn.log"
        self.assertTrue(log.exists())
        text = log.read_text()
        self.assertIn("sent", text)
        self.assertNotIn(TOKEN, text)

    def test_curl_line_helper(self):
        line = warn.curl_line("https://x/escalate")
        self.assertIn("Authorization: Bearer $(cat ~/.config/warn/token)", line)
        self.assertIn("https://x/escalate", line)
        self.assertIn("-m 5", line)


if __name__ == "__main__":
    unittest.main()
