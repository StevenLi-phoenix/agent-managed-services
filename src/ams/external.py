"""Escalations from outside the supervisor.

``ams escalate`` (a program on the harness host) and ``ams intake`` (an HTTP receiver for other
hosts) both turn "something is wrong on <origin>" into a record of the escalation journal -- the
same file the supervisor's own escalations go to, so one operator watcher (``ams escalations``,
DSH's ga-watch) sees every source and applies one gate.

The payload is the one the ``warn`` CLI sends (``kind`` = ``warn.<origin>.<severity>``, ``key``,
``subject``, ``title``, ``text``, ``url``, ``occurredAt``); ``origin`` and ``severity`` may also
be given as fields. Every field is untrusted: it is clipped, never interpreted, and ends up
inside one JSON string (``event.text``), the shape a JSON log line of a supervised service has.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

SEVERITIES = ("info", "warn", "error", "crit")
LIMITS = {"key": 200, "subject": 200, "title": 300, "text": 8000, "url": 2000}
_ORIGIN_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*(?:\.[a-z0-9][a-z0-9_-]*)*$")
_URL_RE = re.compile(r"^https?://\S+$")
_SPACE_RE = re.compile(r"\s+")
MAX_ORIGIN = 80


class ExternalError(ValueError):
    """The payload is wrong; the message is safe to send back."""


@dataclass(frozen=True)
class ExternalEvent:
    origin: str
    severity: str
    title: str
    key: str
    subject: str
    text: str = ""
    url: str | None = None
    occurred_at: int | None = None


def _line(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ExternalError(f"{field} must be a string")
    return _SPACE_RE.sub(" ", value).strip()[: LIMITS[field]]


def _origin_severity(body: dict[str, Any]) -> tuple[str, str]:
    origin, severity = body.get("origin"), body.get("severity")
    kind = body.get("kind")
    if kind is not None:
        if not isinstance(kind, str):
            raise ExternalError("kind must be a string")
        parts = kind.split(".")
        if len(parts) < 3 or parts[0] != "warn":
            raise ExternalError("kind must be warn.<origin>.<severity>")
        origin = origin if origin is not None else ".".join(parts[1:-1])
        severity = severity if severity is not None else parts[-1]
    if not isinstance(origin, str) or not origin:
        raise ExternalError("origin is required (or kind warn.<origin>.<severity>)")
    if len(origin) > MAX_ORIGIN or not _ORIGIN_RE.match(origin):
        raise ExternalError("origin must be dotted lowercase words of [a-z0-9_-]")
    severity = "warn" if severity is None else severity
    if severity not in SEVERITIES:
        raise ExternalError(f"severity must be one of {'|'.join(SEVERITIES)}")
    return origin, severity


def parse_payload(body: object) -> ExternalEvent:
    """Validate one decoded JSON body.

    Raises:
        ExternalError: naming the first field that fails.
    """
    if not isinstance(body, dict):
        raise ExternalError("body must be a JSON object")
    origin, severity = _origin_severity(body)
    if "title" not in body:
        raise ExternalError("title is required")
    title = _line(body["title"], "title")
    if not title:
        raise ExternalError("title is empty")
    text = body.get("text", "")
    if text is None:
        text = ""
    if not isinstance(text, str):
        raise ExternalError("text must be a string")
    url = body.get("url")
    if url is not None and (
        not isinstance(url, str) or len(url) > LIMITS["url"] or not _URL_RE.match(url)
    ):
        raise ExternalError("url must be an http(s) URL")
    occurred = body.get("occurredAt")
    if occurred is not None and (
        isinstance(occurred, bool) or not isinstance(occurred, int) or occurred <= 0
    ):
        raise ExternalError("occurredAt must be epoch milliseconds")
    key = body.get("key")
    if key is None:
        digest = hashlib.sha1(f"{origin}|{title}".encode(), usedforsecurity=False).hexdigest()[:10]
        key = f"{origin}-{digest}"
    subject = body.get("subject", origin)
    return ExternalEvent(
        origin=origin,
        severity=severity,
        title=title,
        key=_line(key, "key"),
        subject=_line(subject, "subject") or origin,
        text=text.strip()[: LIMITS["text"]],
        url=url,
        occurred_at=occurred,
    )


def record_for(event: ExternalEvent) -> dict[str, Any]:
    """The journal record for ``event`` (the supervisor's own escalation shape).

    ``event.text`` is one JSON log line, ``{"level", "msg", "src", "error"?}``: what ga-watch
    signs (level, msg, src) and prints, so a repeat of the same title from the same origin is one
    cause, and the free text rides in ``error`` where it never changes the signature.
    """
    import json

    line: dict[str, Any] = {"level": event.severity, "msg": event.title, "src": event.origin}
    if event.text:
        line["error"] = event.text
    detail: dict[str, Any] = {
        "text": json.dumps(line, ensure_ascii=False),
        "stream": "external",
        "key": event.key,
        "subject": event.subject,
    }
    if event.url:
        detail["url"] = event.url
    if event.occurred_at:
        detail["occurred_at_ms"] = event.occurred_at
    return {
        "kind": "External",
        "service_id": event.origin,
        "action": "escalate",
        "reason": f"external {event.severity}",
        "event": detail,
        "attempt": 0,
        "consecutive_failures": 0,
    }
