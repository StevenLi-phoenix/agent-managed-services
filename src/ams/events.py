"""Supervision events.

Everything the supervision loop observes is turned into one of these frozen
event records before any decision is made. Log lines are attacker-influenced
input: this module only classifies text, it never interprets it as commands.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Literal

Stream = Literal["stdout", "stderr"]


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


def classify(text: str, stream: Stream) -> Severity:
    """Heuristic severity of one log line.

    Deliberately conservative: an unknown line on stderr is INFO, not WARNING,
    because most servers log everything to stderr. Only explicit markers raise
    the level. The decision policy can always re-classify.
    """
    for pattern, sev in _SEVERITY_PATTERNS:
        if pattern.search(text):
            return sev
    return Severity.INFO


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
    def from_raw(cls, service_id: str, stream: Stream, raw: bytes) -> LogLine:
        truncated = len(raw) > MAX_LINE_BYTES
        text = raw[:MAX_LINE_BYTES].decode("utf-8", errors="replace").rstrip("\r\n")
        return cls(service_id, stream, text, classify(text, stream), truncated=truncated)


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
    ts: float = field(default_factory=time.time)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    @property
    def severity(self) -> Severity:
        return Severity.INFO if self.ok else Severity.ERROR


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
