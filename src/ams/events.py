"""Supervision events.

Everything the supervision loop observes is turned into one of these frozen
event records before any decision is made. Log lines are attacker-influenced
input: this module only classifies text, it never interprets it as commands.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Literal

Stream = Literal["stdout", "stderr"]
# Per-declaration hint (``[logging] format``) for how a service marks severity.
LogFormat = Literal["auto", "level-prefix", "json", "plain"]


class Severity(IntEnum):
    DEBUG = 10
    INFO = 20
    WARNING = 30
    ERROR = 40
    CRITICAL = 50


# Ordered from most to least severe so the first hit wins.
_SEVERITY_PATTERNS: tuple[tuple[re.Pattern[str], Severity], ...] = (
    (re.compile(r"\b(CRITICAL|FATAL|PANIC|EMERG)\b", re.IGNORECASE), Severity.CRITICAL),
    (re.compile(r"^Traceback \(most recent call last\)"), Severity.ERROR),
    (
        re.compile(r"\b(ERROR|ERR|EXCEPTION|SEGFAULT|SEGMENTATION FAULT)\b", re.IGNORECASE),
        Severity.ERROR,
    ),
    (re.compile(r"^\s*\w*(Error|Exception)\b:"), Severity.ERROR),  # "ValueError: ..."
    (re.compile(r"\b(WARNING|WARN|DEPRECAT\w*)\b", re.IGNORECASE), Severity.WARNING),
    (re.compile(r"\b(DEBUG|TRACE)\b", re.IGNORECASE), Severity.DEBUG),
)


# Level names a service may use, mapped to our five. Everything is matched
# case-insensitively; the keys below are the union of the Python logging names
# and Caddy's (debug|info|warn|error|panic|fatal).
_LEVEL_NAMES: dict[str, Severity] = {
    "DEBUG": Severity.DEBUG,
    "TRACE": Severity.DEBUG,
    "INFO": Severity.INFO,
    "NOTICE": Severity.INFO,
    "WARN": Severity.WARNING,
    "WARNING": Severity.WARNING,
    "ERROR": Severity.ERROR,
    "ERR": Severity.ERROR,
    "CRITICAL": Severity.CRITICAL,
    "FATAL": Severity.CRITICAL,
    "PANIC": Severity.CRITICAL,
}

# `%(levelname)s %(name)s: %(message)s` -- what the api SDK's basicConfig emits.
# The logger name must be there: without it "ERROR: connection refused" would be
# read as a level token, which is the heuristics' job, not this one's. An
# optional `[<member>]` tag between the level and the logger name is the pool
# runner's shape (`LEVEL [<member>] <logger>: <msg>`, e.g.
# "INFO [kvservice] kvservice.main: started") -- several pooled services'
# lines multiplexed onto one stream, tagged with which member emitted them.
_LEVEL_PREFIX_RE = re.compile(
    r"^(?P<level>[A-Za-z]+)[ \t]+(?:\[(?P<tag>[a-z0-9][a-z0-9_-]*)\][ \t]+)?(?P<name>[^\s:]+):",
)
# JSON is only *considered* for a line that already looks like an object.
_JSON_START_RE = re.compile(r"^\s*\{")
# The severity keys we are willing to read out of a JSON log line. Nothing else
# in the object is ever interpreted (D: the object is attacker-influenced data).
_JSON_LEVEL_KEYS = ("level", "severity")


# Lines that trip a marker word but carry no signal of their own. Node prints the
# first as a follow-up to its first process warning ("... to show where the warning
# was created"); the warning line it follows is classified on its own. Anchored
# and exact, so nothing else can ride on it.
_NOISE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\(Use `node --trace-[a-z-]+ \.\.\.` to show where the \w+ was created\)$"),
)


def _heuristic(text: str) -> Severity:
    if any(p.match(text) for p in _NOISE_PATTERNS):
        return Severity.INFO
    for pattern, sev in _SEVERITY_PATTERNS:
        if pattern.search(text):
            return sev
    return Severity.INFO


def _from_level_prefix(text: str) -> Severity | None:
    m = _LEVEL_PREFIX_RE.match(text)
    if m is None:
        return None
    return _LEVEL_NAMES.get(m.group("level").upper())


def member_tag(text: str) -> str | None:
    """The pool member tag of a ``LEVEL [<member>] <logger>: ...`` line, if any.

    ``None`` when the line does not have this shape at all: no leading level
    token, no bracketed tag, or no logger name before the colon. The level
    token itself is not checked against the known severity names here --
    that is ``classify``'s job, not this one's.
    """
    m = _LEVEL_PREFIX_RE.match(text)
    if m is None:
        return None
    return m.group("tag")


def _from_json(text: str) -> Severity | None:
    """Severity of a structured log line, or None if there is nothing to trust.

    The line is data: it is parsed with ``json.loads`` (which executes nothing)
    and only ``level``/``severity`` are read. A malformed line, a non-object, a
    missing key or an unknown level name all mean "no answer", never a guess.
    """
    if not _JSON_START_RE.match(text):
        return None
    try:
        obj: Any = json.loads(text)
    except (ValueError, RecursionError):
        return None
    if not isinstance(obj, dict):
        return None
    for key in _JSON_LEVEL_KEYS:
        value = obj.get(key)
        if isinstance(value, str):
            sev = _LEVEL_NAMES.get(value.strip().upper())
            if sev is not None:
                return sev
    return None


def classify(text: str, stream: Stream, fmt: LogFormat = "auto") -> Severity:
    """Severity of one log line, given the declaration's ``[logging] format``.

    - ``level-prefix``/``json``: read the level the service itself printed, and
      fall back to the heuristics only when the line does not carry one (a
      traceback body, a framework banner printed before logging was configured).
      A level the service stated wins over anything in the message text, which
      is the whole point: an INFO line quoting the word "ERROR" is INFO.
    - ``plain``: heuristics only.
    - ``auto``: level prefix, then JSON, then heuristics.

    The heuristics are deliberately conservative: an unknown line on stderr is
    INFO, not WARNING, because most servers log everything to stderr. The
    decision policy can always re-classify.
    """
    if fmt in ("auto", "level-prefix"):
        sev = _from_level_prefix(text)
        if sev is not None:
            return sev
    if fmt in ("auto", "json"):
        sev = _from_json(text)
        if sev is not None:
            return sev
    return _heuristic(text)


MAX_LINE_BYTES = 64 * 1024


@dataclass(frozen=True)
class LogLine:
    service_id: str
    stream: Stream
    text: str  # decoded, trailing newline stripped, truncated to MAX_LINE_BYTES
    severity: Severity
    ts: float = field(default_factory=time.time)
    truncated: bool = False

    @classmethod
    def from_raw(
        cls, service_id: str, stream: Stream, raw: bytes, fmt: LogFormat = "auto"
    ) -> LogLine:
        truncated = len(raw) > MAX_LINE_BYTES
        text = raw[:MAX_LINE_BYTES].decode("utf-8", errors="replace").rstrip("\r\n")
        return cls(service_id, stream, text, classify(text, stream, fmt), truncated=truncated)


@dataclass(frozen=True)
class ServiceStarted:
    service_id: str
    pid: int
    attempt: int  # 1 for the first start, increments on every restart
    ts: float = field(default_factory=time.time)


@dataclass(frozen=True)
class ServiceExited:
    service_id: str
    pid: int
    exit_code: int | None  # None when killed by a signal
    signal: int | None  # None on normal exit
    uptime_s: float
    # True when this exit follows something the harness asked for: stop(),
    # kill(), shutdown, or the stop half of a restart/reload. Set by the
    # supervisor, which is the only component that knows operator intent -- an
    # exit code of 143 looks identical to a crash from the outside.
    expected: bool = False
    ts: float = field(default_factory=time.time)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    @property
    def severity(self) -> Severity:
        return Severity.INFO if (self.ok or self.expected) else Severity.ERROR


@dataclass(frozen=True)
class HealthChanged:
    service_id: str
    healthy: bool
    detail: str = ""
    ts: float = field(default_factory=time.time)


@dataclass(frozen=True)
class OrphanReaped:
    """A subreaped grandchild (not a direct service pid) exited."""

    pid: int
    exit_code: int | None
    signal: int | None
    ts: float = field(default_factory=time.time)


Event = LogLine | ServiceStarted | ServiceExited | HealthChanged | OrphanReaped


def severity_of(event: Event) -> Severity:
    if isinstance(event, LogLine):
        return event.severity
    if isinstance(event, ServiceExited):
        return event.severity
    if isinstance(event, HealthChanged):
        return Severity.INFO if event.healthy else Severity.WARNING
    return Severity.INFO
