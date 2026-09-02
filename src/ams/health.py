"""Health checks.

Four kinds, all driven from the supervisor's single-threaded loop:

- ``none`` — healthy as soon as the process is spawned; never re-checked.
- ``tcp``  — non-blocking connect to ``127.0.0.1:<allocated port>``.
- ``http`` — ``GET <path>`` on that port; 2xx/3xx is healthy.
- ``log``  — a regex over log lines; sticky (once healthy, never flips back).

The tcp probe is genuinely non-blocking (``connect_ex`` + ``select`` bounded by
``timeout_s``). The http probe uses ``http.client`` and therefore *blocks* the
supervisor loop for at most ``health.timeout_s``; that is a deliberate
trade-off (no threads in the core loop, and ``timeout_s`` is small by
construction). If a declaration ever needs a multi-second http timeout, move
this call out of the loop rather than raising the timeout.
"""

from __future__ import annotations

import errno
import http.client
import logging
import math
import re
import select
import socket
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from ams.schema import HealthSpec

log = logging.getLogger("ams.health")

LOOPBACK = "127.0.0.1"
# Never schedule checks closer together than this, even if interval_s is 0.
MIN_INTERVAL_S = 0.01


def check_tcp(port: int, timeout_s: float, host: str = LOOPBACK) -> bool:
    """True if a TCP connection to ``host:port`` can be established."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setblocking(False)
        err = sock.connect_ex((host, port))
        if err == 0:
            return True
        if err not in (errno.EINPROGRESS, errno.EALREADY, errno.EWOULDBLOCK):
            return False
        _, writable, _ = select.select([], [sock], [], max(timeout_s, 0.0))
        if not writable:
            return False
        return sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR) == 0
    except OSError:
        return False
    finally:
        sock.close()


def check_http(port: int, path: str, timeout_s: float, host: str = LOOPBACK) -> tuple[bool, str]:
    """``GET path``; healthy on 2xx/3xx. Detail is the status line or the error."""
    conn = http.client.HTTPConnection(host, port, timeout=max(timeout_s, 0.01))
    try:
        conn.request("GET", path or "/")
        resp = conn.getresponse()
        resp.read(1024)
        detail = f"HTTP {resp.status} {resp.reason}".strip()
        return 200 <= resp.status < 400, detail
    except (OSError, http.client.HTTPException) as e:
        return False, f"{type(e).__name__}: {e}"
    finally:
        conn.close()


@dataclass
class HealthMonitor:
    """Per-service health state machine. Owns *when* to check, not *what to do*.

    ``check()`` returns ``(None, detail)`` when the result must not be reported
    (start period not elapsed, or the kind is log-driven); the supervisor only
    emits ``HealthChanged`` on real transitions.
    """

    spec: HealthSpec
    ports: Mapping[str, int] = field(default_factory=dict)
    clock: Callable[[], float] = time.monotonic

    started_at: float | None = None
    next_due: float = math.inf
    matched: bool = False  # log kind: pattern already seen for this run

    def __post_init__(self) -> None:
        self._pattern: re.Pattern[str] | None = (
            re.compile(self.spec.pattern) if self.spec.kind == "log" and self.spec.pattern else None
        )

    @property
    def port(self) -> int | None:
        if self.spec.port is None:
            return None
        return self.ports.get(self.spec.port)

    def start(self, now: float) -> None:
        """Reset for a fresh run of the service."""
        self.started_at = now
        self.matched = False
        # log-driven kinds are never polled; everything else gets a first probe now.
        self.next_due = math.inf if self.spec.kind == "log" else now

    def stop(self) -> None:
        self.started_at = None
        self.next_due = math.inf

    def in_start_period(self, now: float) -> bool:
        return self.started_at is not None and (now - self.started_at) < self.spec.start_period_s

    def due(self, now: float) -> bool:
        return now >= self.next_due

    def observe_log(self, text: str) -> bool | None:
        """True the first time the log pattern matches; None otherwise."""
        if self._pattern is None or self.matched:
            return None
        if self._pattern.search(text):
            self.matched = True
            return True
        return None

    def check(self, now: float) -> tuple[bool | None, str]:
        kind = self.spec.kind
        self.next_due = now + max(self.spec.interval_s, MIN_INTERVAL_S)
        if kind == "none":
            self.next_due = math.inf  # nothing to poll; healthy once, forever
            return True, "no health check configured"
        if kind == "log":
            self.next_due = math.inf
            return None, ""
        port = self.port
        if port is None:
            return False, f"health.port {self.spec.port!r} has no allocated port"
        if kind == "tcp":
            ok = check_tcp(port, self.spec.timeout_s)
            detail = f"tcp {LOOPBACK}:{port} " + ("connected" if ok else "refused")
        else:
            ok, detail = check_http(port, self.spec.path, self.spec.timeout_s)
            detail = f"http {LOOPBACK}:{port}{self.spec.path} {detail}"
        if not ok and self.in_start_period(now):
            log.debug("health check suppressed during start period: %s", detail)
            return None, detail
        return ok, detail
