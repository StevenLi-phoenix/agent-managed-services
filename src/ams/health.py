"""Health checks.

Four kinds, all driven from the supervisor's single-threaded loop:

- ``none`` — healthy as soon as the process is spawned; never re-checked.
- ``tcp``  — connect to ``127.0.0.1:<allocated port>``.
- ``http`` — ``GET <path>`` on that port; 2xx/3xx is healthy.
- ``log``  — a regex over log lines; sticky (once healthy, never flips back).

tcp and http never block the loop. A check is a :class:`Probe`: a non-blocking
socket plus a small state machine (connect → send → read the status line)
that the supervisor registers in its own selector, with ``deadline`` folded
into the loop's timers. A hung endpoint costs one fd and one timer, not
``timeout_s`` of every other service's attention. No threads: the supervisor
forks into user namespaces (``ams.userns``), where a thread holding a lock
across the fork deadlocks the child.

:func:`check_tcp` / :func:`check_http` drive the same state machine to
completion on a private selector, for one-shot callers outside the loop.
"""

from __future__ import annotations

import errno
import logging
import math
import re
import selectors
import socket
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal

from ams.schema import HealthSpec

log = logging.getLogger("ams.health")

LOOPBACK = "127.0.0.1"
# Never schedule checks closer together than this, even if interval_s is 0.
MIN_INTERVAL_S = 0.01
# Never let a probe's deadline collapse to "already expired".
MIN_TIMEOUT_S = 0.01
# Stop reading an http answer after this much; the status line is all we need,
# the rest is drained only so the peer is not reset mid-write.
MAX_HTTP_READ = 64 * 1024
_STATUS_RE = re.compile(rb"^HTTP/\d(?:\.\d)?\s+(\d{3})(?:\s+([^\r\n]*))?")
_IN_PROGRESS = (errno.EINPROGRESS, errno.EALREADY, errno.EWOULDBLOCK)

ProbeKind = Literal["tcp", "http"]
ProbeState = Literal["connect", "send", "recv", "done"]


class Probe:
    """One in-flight tcp/http check on a non-blocking socket.

    The owner registers :meth:`fileno` for :attr:`events`, calls
    :meth:`advance` on readiness (re-registering when ``events`` changed) and
    :meth:`expire` once ``deadline`` passes. ``result`` is ``(ok, detail)``
    once the probe concluded. The socket stays open until :meth:`close`, so
    the owner can unregister it from its selector first -- unregistering a
    closed fd (or one the kernel already reused) is the bug this avoids.
    ``started`` is the
    monotonic time the probe opened, which is what the start period is judged
    against.
    """

    def __init__(self, kind: ProbeKind, port: int, path: str, deadline: float, started: float):
        self.kind: ProbeKind = kind
        self.port = port
        self.path = path or "/"
        self.deadline = deadline
        self.started = started
        self.state: ProbeState = "connect"
        self.result: tuple[bool, str] | None = None
        self._out = b""
        self._buf = bytearray()
        self._status: tuple[bool, str] | None = None
        self._sock: socket.socket | None = None

    # ------------------------------------------------------------- lifecycle

    @classmethod
    def open(
        cls,
        kind: ProbeKind,
        port: int,
        path: str,
        timeout_s: float,
        *,
        now: float,
        host: str = LOOPBACK,
    ) -> Probe:
        """Start connecting. Never waits on the peer; may conclude at once
        (refused, or a loopback tcp connect that completed synchronously)."""
        probe = cls(kind, port, path, now + max(timeout_s, MIN_TIMEOUT_S), now)
        if kind == "http":
            probe._out = (
                f"GET {probe.path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
                "User-Agent: ams-health\r\nAccept-Encoding: identity\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii", "replace")
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        except OSError as e:
            probe._conclude(False, f"socket: {e}")
            return probe
        probe._sock = sock
        try:
            sock.setblocking(False)
            err = sock.connect_ex((host, port))
        except OSError as e:
            probe._conclude(False, f"{type(e).__name__}: {e}")
            return probe
        if err == 0:
            probe._connected()
        elif err not in _IN_PROGRESS:
            probe._conclude(False, _refused(err))
        return probe

    def fileno(self) -> int:
        return self._sock.fileno() if self._sock is not None else -1

    @property
    def events(self) -> int:
        """Selector interest for the current state."""
        return selectors.EVENT_READ if self.state == "recv" else selectors.EVENT_WRITE

    def advance(self) -> None:
        """Make progress after the socket became ready. Never blocks."""
        if self._sock is None or self.result is not None:
            return
        try:
            if self.state == "connect":
                err = self._sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                if err in _IN_PROGRESS:
                    return
                if err != 0:
                    self._conclude(False, _refused(err))
                    return
                self._connected()
            elif self.state == "send":
                sent = self._sock.send(self._out)
                self._out = self._out[sent:]
                if not self._out:
                    self.state = "recv"
            elif self.state == "recv":
                self._read()
        except (BlockingIOError, InterruptedError):
            return
        except OSError as e:
            if self._status is not None:  # the answer is in; a late reset changes nothing
                self._conclude(*self._status)
            else:
                self._conclude(False, f"{type(e).__name__}: {e}")

    def expire(self) -> None:
        """The deadline passed. An http status already read still counts."""
        if self.result is not None:
            return
        if self._status is not None:
            self._conclude(*self._status)
        else:
            timeout = self.deadline - self.started
            self._conclude(False, f"timed out after {timeout:.2f}s ({self.state})")

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def run_blocking(self, clock: Callable[[], float] = time.monotonic) -> tuple[bool, str]:
        """Drive the probe to a result on a private selector (one-shot callers).

        The budget is ``deadline - started`` measured on ``clock`` from now, so
        a probe opened with a synthetic ``now`` (tests, :meth:`HealthMonitor.check`)
        still gets its full timeout."""
        end = clock() + (self.deadline - self.started)
        try:
            with selectors.DefaultSelector() as sel:
                while self.result is None:
                    remaining = end - clock()
                    if remaining <= 0:
                        self.expire()
                        break
                    sel.register(self.fileno(), self.events)
                    ready = sel.select(remaining)
                    sel.unregister(self.fileno())
                    if ready:
                        self.advance()
        finally:
            self.close()
        assert self.result is not None
        return self.result

    # ------------------------------------------------------------- internals

    def _connected(self) -> None:
        if self.kind == "tcp":
            self._conclude(True, "connected")
        else:
            self.state = "send"

    def _read(self) -> None:
        assert self._sock is not None
        while True:
            chunk = self._sock.recv(8192)
            if not chunk:  # EOF: the server answered and closed
                self._conclude(*(self._status or (False, "connection closed before a status line")))
                return
            if len(self._buf) < MAX_HTTP_READ:
                self._buf += chunk
            if self._status is None and b"\n" in self._buf:
                line = bytes(self._buf.split(b"\n", 1)[0])
                m = _STATUS_RE.match(line)
                if m is None:
                    self._conclude(False, f"not an HTTP status line: {line[:80]!r}")
                    return
                code = int(m.group(1))
                reason = (m.group(2) or b"").decode("latin-1").strip()
                self._status = (200 <= code < 400, f"HTTP {code} {reason}".strip())
            if len(self._buf) >= MAX_HTTP_READ:
                self._conclude(*(self._status or (False, "no status line in the first 64 KiB")))
                return

    def _conclude(self, ok: bool, detail: str) -> None:
        self.result = (ok, detail)
        self.state = "done"


def _refused(err: int) -> str:
    if err == errno.ECONNREFUSED:
        return "refused"
    return f"connect failed: {errno.errorcode.get(err, err)}"


def check_tcp(port: int, timeout_s: float, host: str = LOOPBACK) -> bool:
    """True if a TCP connection to ``host:port`` can be established. Blocks up
    to ``timeout_s``; for one-shot callers, never the supervisor loop."""
    probe = Probe.open("tcp", port, "/", timeout_s, now=time.monotonic(), host=host)
    return probe.run_blocking()[0]


def check_http(port: int, path: str, timeout_s: float, host: str = LOOPBACK) -> tuple[bool, str]:
    """``GET path``; healthy on 2xx/3xx. Detail is the status line or the error.
    Blocks up to ``timeout_s``; for one-shot callers, never the supervisor loop."""
    probe = Probe.open("http", port, path, timeout_s, now=time.monotonic(), host=host)
    return probe.run_blocking()


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

    def begin(self, now: float) -> tuple[bool | None, str] | Probe:
        """Start a check. Returns the verdict at once for kinds with nothing to
        wait on, otherwise an open :class:`Probe` the caller drives and then
        hands to :meth:`finish`."""
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
        probe_kind: ProbeKind = "tcp" if kind == "tcp" else "http"
        return Probe.open(probe_kind, port, self.spec.path, self.spec.timeout_s, now=now)

    def finish(self, probe: Probe) -> tuple[bool | None, str]:
        """Turn a concluded probe into a report; ``None`` while in the start period."""
        ok, detail = probe.result if probe.result is not None else (False, "probe never concluded")
        if probe.kind == "tcp":
            detail = f"tcp {LOOPBACK}:{probe.port} {detail}"
        else:
            detail = f"http {LOOPBACK}:{probe.port}{self.spec.path} {detail}"
        if not ok and self.in_start_period(probe.started):
            log.debug("health check suppressed during start period: %s", detail)
            return None, detail
        return ok, detail

    def check(self, now: float) -> tuple[bool | None, str]:
        """Blocking convenience: :meth:`begin`, drive the probe, :meth:`finish`.
        The supervisor never calls this; it drives probes on its selector."""
        started = self.begin(now)
        if not isinstance(started, Probe):
            return started
        started.run_blocking(self.clock)  # returns at once if already concluded; closes
        return self.finish(started)
