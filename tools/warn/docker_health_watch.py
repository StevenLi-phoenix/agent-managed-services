#!/usr/bin/env python3
"""docker_health_watch: hand a Docker container that turned unhealthy to the operator through `warn`.

Long-running, standard library only. It lists containers that are already unhealthy, then follows
`docker events` for health_status changes and calls warn once per `unhealthy` event (recoveries are
only logged: nobody needs news that a thing is fine). If `docker events` ends (Docker Desktop
restarting) it reconnects with a growing pause. Log: ~/.local/state/warn/docker-health-watch.log.

    python docker_health_watch.py            # runs until killed
"""
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

MAX_BACKOFF = 300
EVENT_CMD = ["events", "--filter", "type=container", "--filter", "event=health_status", "--format", "{{json .}}"]
NO_WINDOW = {"creationflags": 0x08000000} if os.name == "nt" else {}  # pythonw: no console flashing
PS_CMD = ["ps", "--filter", "health=unhealthy", "--format", "{{.Names}}"]


def parse_event(line):
    """(container name, 'unhealthy' | 'healthy') for a health_status event line, else None."""
    try:
        data = json.loads(line)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    status = str(data.get("status", ""))
    if not status.startswith("health_status: "):
        return None
    attrs = (data.get("Actor") or {}).get("Attributes") or {}
    name = attrs.get("name")
    if not name:
        return None
    return name, status[len("health_status: "):].strip()


def default_log(message):
    path = Path.home() / ".local" / "state" / "warn" / "docker-health-watch.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > 256 * 1024:
            path.replace(path.with_suffix(".log.1"))
        with open(str(path), "a", encoding="utf-8") as fh:
            fh.write("%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S%z"), message))
    except OSError:
        pass


def run(docker, notify, sleep=time.sleep, max_loops=None, log=default_log):
    """Follow health events until max_loops (None = forever); returns the number of loops run."""
    loops = 0
    backoff = 5
    while max_loops is None or loops < max_loops:
        loops += 1
        started = time.time()
        try:
            listed = subprocess.run(docker + PS_CMD, capture_output=True, text=True, timeout=30, **NO_WINDOW)
            for name in listed.stdout.split():
                log("already unhealthy: %s" % name)
                notify(name, "error", "docker container %s is unhealthy" % name)
            proc = subprocess.Popen(docker + EVENT_CMD, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, **NO_WINDOW)
            log("following docker events (pid %d)" % proc.pid)
            for line in proc.stdout:
                parsed = parse_event(line)
                if parsed is None:
                    continue
                name, state = parsed
                log("%s: %s" % (name, state))
                if state == "unhealthy":
                    notify(name, "error", "docker container %s is unhealthy" % name)
            proc.wait()
            log("docker events ended (exit %s)" % proc.returncode)
        except (OSError, subprocess.SubprocessError) as err:
            log("docker unavailable: %s" % err)
        if time.time() - started > 60:
            backoff = 5
        log("reconnecting in %ds" % backoff)
        sleep(backoff)
        backoff = min(backoff * 2, MAX_BACKOFF)
    return loops


def make_notifier():
    here = Path(__file__).resolve().parent
    warn_path = next((p for p in (here / "warn.py", here / "warn") if p.exists()), None)
    host = os.environ.get("WARN_HOST") or socket.gethostname().split(".")[0]

    def notify(name, severity, title):
        cmd = [sys.executable, str(warn_path)] if warn_path else ["warn"]
        cmd += ["--source", "%s.docker" % host.lower(), "--severity", severity, "--subject", "%s %s" % (host, name), title]
        try:
            subprocess.run(cmd, timeout=60, **NO_WINDOW)
        except (OSError, subprocess.SubprocessError) as err:
            default_log("warn failed: %s" % err)

    return notify


if __name__ == "__main__":
    run([os.environ.get("DOCKER_BIN", "docker")], make_notifier())
