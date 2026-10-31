"""docker-health-watch turns `docker events` health changes into warnings (stub docker, fake notifier)."""
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import docker_health_watch as watch  # noqa: E402


def event(name, status, **extra):
    attrs = {"name": name, "image": "img:1"}
    attrs.update(extra)
    return json.dumps({"status": status, "id": "abc", "from": "img:1", "Type": "container", "Action": status,
                       "Actor": {"ID": "abc", "Attributes": attrs}, "time": 1})


class ParseTest(unittest.TestCase):
    def test_unhealthy_event(self):
        self.assertEqual(watch.parse_event(event("feiyu-voice", "health_status: unhealthy")), ("feiyu-voice", "unhealthy"))

    def test_healthy_event_is_recovery(self):
        self.assertEqual(watch.parse_event(event("feiyu-voice", "health_status: healthy")), ("feiyu-voice", "healthy"))

    def test_other_and_garbage_lines_ignored(self):
        self.assertIsNone(watch.parse_event(event("x", "start")))
        self.assertIsNone(watch.parse_event("not json"))
        self.assertIsNone(watch.parse_event("[]"))
        self.assertIsNone(watch.parse_event(json.dumps({"status": "health_status: unhealthy"})))  # no name


class RunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bin = Path(self.tmp.name)
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def docker(self, script):
        path = self.bin / "docker"
        path.write_text("#!/bin/sh\n%s\n" % script)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return [str(path)]

    def notify(self, name, severity, title):
        self.calls.append((name, severity, title))

    def test_unhealthy_calls_notify_and_healthy_does_not(self):
        lines = event("feiyu-voice", "health_status: unhealthy") + "\n" + event("feiyu-voice", "health_status: healthy") + "\n"
        data = self.bin / "lines"
        data.write_text(lines)
        cmd = self.docker('if [ "$1" = events ]; then cat "%s"; else :; fi' % data)
        watch.run(cmd, self.notify, sleep=lambda s: None, max_loops=1, log=lambda m: None)
        self.assertEqual(self.calls, [("feiyu-voice", "error", "docker container feiyu-voice is unhealthy")])

    def test_startup_reports_already_unhealthy_containers(self):
        cmd = self.docker('if [ "$1" = ps ]; then echo hotwatch-laya; else :; fi')
        watch.run(cmd, self.notify, sleep=lambda s: None, max_loops=1, log=lambda m: None)
        self.assertEqual(self.calls, [("hotwatch-laya", "error", "docker container hotwatch-laya is unhealthy")])

    def test_restarts_after_docker_events_exits_with_backoff(self):
        sleeps = []
        cmd = self.docker("exit 1")
        loops = watch.run(cmd, self.notify, sleep=sleeps.append, max_loops=3, log=lambda m: None)
        self.assertEqual(loops, 3)
        self.assertEqual(len(sleeps), 3)
        self.assertTrue(all(b >= a for a, b in zip(sleeps, sleeps[1:])))
        self.assertLessEqual(max(sleeps), watch.MAX_BACKOFF)

    def test_missing_docker_binary_does_not_crash(self):
        sleeps = []
        watch.run(["/nonexistent/docker"], self.notify, sleep=sleeps.append, max_loops=2, log=lambda m: None)
        self.assertEqual(len(sleeps), 2)


if __name__ == "__main__":
    unittest.main()
