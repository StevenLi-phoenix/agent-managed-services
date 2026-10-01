"""The escalation journal: the durable, queryable side of "escalate".

The decision layer is deterministic (``DecisionPolicy``): it suppresses noise,
restarts per policy and *escalates* what rules cannot settle. Escalations are
written as JSON lines to stdout -- which under systemd lands in journald,
interleaved with every other unit -- and, through this module, appended to
``<state>/logs/escalations.jsonl``. That file is what the agent (an operator
session such as Claude Code, started by a human) reads with
``ams escalations`` before it decides on a fix. See ``docs/agent-loop.md``.

Writers are the harness (``ams run``) and the one-shot timers
(``ams platform core sync``, backup); each record carries ``ts`` (UTC) and
``source``. A record is one ``write(2)`` on an ``O_APPEND`` fd, so concurrent
writers never interleave inside a line. Rotation keeps exactly one previous
file (``.1``); a rename racing another writer's append only puts that line in
``.1``, which :func:`read_records` reads too.

Records are untrusted: ``event.text`` is whatever a supervised process printed.
:func:`format_record` strips control characters before anything reaches a
terminal, and nothing here ever interprets a record's content.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ams.state import StateDir

log = logging.getLogger("ams.escalations")

JOURNAL_NAME = "escalations.jsonl"
#: Rotate past this size. ~8 MiB is tens of thousands of escalations: far more
#: than an operator reads, small enough to `grep` without thinking.
MAX_BYTES = 8 * 1024 * 1024
#: Longest field value :func:`format_record` prints.
MAX_FIELD_CHARS = 300

# C0/C1 controls (incl. ESC, BEL, CR), DEL; tabs and newlines become spaces.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_SPACE_RE = re.compile(r"[\t\n]+")


def journal_path(state: StateDir) -> Path:
    return state.logs_dir / JOURNAL_NAME


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class EscalationJournal:
    """Append-only JSONL file shared by every ams process on one state dir.

    ``append`` never raises: a full disk or a broken logs dir must not take an
    escalation down with it -- stdout still has the record.
    """

    path: Path
    source: str
    max_bytes: int = MAX_BYTES

    def append(self, record: Mapping[str, Any]) -> None:
        line = json.dumps(
            {"ts": utc_now(), "source": self.source, **record}, default=str, sort_keys=False
        )
        data = (line + "\n").encode("utf-8", "replace")
        try:
            self.path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
            self._rotate_if_needed()
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC, 0o600)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
        except OSError as e:
            log.warning("could not append to escalation journal %s: %s", self.path, e)

    def _rotate_if_needed(self) -> None:
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return
        if size < self.max_bytes:
            return
        os.replace(self.path, self.path.with_name(self.path.name + ".1"))
        log.info("rotated escalation journal %s (%d bytes)", self.path, size)


def _lines(path: Path) -> Iterator[str]:
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            yield from f
    except FileNotFoundError:
        return


def read_records(path: Path) -> list[dict[str, Any]]:
    """Every record in ``path.1`` then ``path``, oldest first; junk lines skipped."""
    out: list[dict[str, Any]] = []
    for p in (path.with_name(path.name + ".1"), path):
        for raw in _lines(p):
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
    return out


def _clean(value: Any) -> str:
    text = _SPACE_RE.sub(" ", str(value))
    text = _CONTROL_RE.sub("", text)
    if len(text) > MAX_FIELD_CHARS:
        text = text[: MAX_FIELD_CHARS - 1] + "…"
    return text


def format_record(rec: Mapping[str, Any]) -> str:
    """One terminal-safe line: ``ts source service kind reason | detail``."""
    event = rec.get("event") if isinstance(rec.get("event"), Mapping) else {}
    detail = ""
    for key in ("text", "detail", "cause", "error"):
        if isinstance(event, Mapping) and event.get(key):
            detail = str(event[key])
            break
    parts = [
        _clean(rec.get("ts", "?")),
        _clean(rec.get("source", "?")).ljust(9),
        _clean(rec.get("service_id") or "-").ljust(12),
        _clean(rec.get("kind", "?")).ljust(14),
        _clean(rec.get("reason", "")),
    ]
    line = "  ".join(parts)
    return f"{line} | {_clean(detail)}" if detail else line
