"""Incremental log tailing that survives rotation and truncation, and classifies lines with AMS."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ams.events import LogLine, Severity
from ams.ops.config import ServiceConfig

log = logging.getLogger("ams.ops.logs")

MAX_READ = 2 * 1024 * 1024  # per file per tick; a flood is summarised, not replayed
SEED_BYTES = 256 * 1024  # how much history the first (baseline) pass looks at


def read_new(path: Path, entry: dict[str, Any], *, seed: bool = False) -> tuple[list[str], int]:
    """New complete lines since ``entry['offset']``. Returns ``(lines, skipped_bytes)``.

    ``entry`` is mutated: ``{"inode": int, "offset": int}``. A first sight starts at the end
    (or ``SEED_BYTES`` before it when ``seed``); a new inode or a shorter file restarts at 0.
    """
    try:
        st = path.stat()
    except FileNotFoundError:
        return [], 0
    except OSError as e:
        log.warning("cannot stat %s: %s", path, e)
        return [], 0
    known = "inode" in entry
    if not known:
        entry["inode"] = st.st_ino
        entry["offset"] = max(0, st.st_size - SEED_BYTES) if seed else st.st_size
    elif entry["inode"] != st.st_ino or st.st_size < entry["offset"]:
        entry["inode"], entry["offset"] = st.st_ino, 0
    start = entry["offset"]
    skipped = 0
    if st.st_size - start > MAX_READ:
        skipped = st.st_size - start - MAX_READ
        start = st.st_size - MAX_READ
    if st.st_size <= start:
        entry["offset"] = st.st_size
        return [], skipped
    try:
        with open(path, "rb") as f:
            f.seek(start)
            data = f.read(st.st_size - start)
    except OSError as e:
        log.warning("cannot read %s: %s", path, e)
        return [], 0
    cut = data.rfind(b"\n")
    if cut < 0:  # no complete line yet; wait for the rest
        return [], skipped
    entry["offset"] = start + cut + 1
    if (not known and seed and start > 0) or skipped:
        data = (
            data[data.find(b"\n") + 1 :] if data[:cut].count(b"\n") else data
        )  # drop partial head
    lines = data[: cut + 1].decode("utf-8", "replace").splitlines()
    return [ln for ln in lines if ln.strip()], skipped


# "2026-10-30 23:48:08,615 INFO [watch] ..." / "2026-10-31 03:38 [WARNING] registry: ...":
# python-logging output where the level is stated after the timestamp. A stated level beats
# whatever words the message body happens to contain ("INFO ... failed ... (attempt 1/4)").
_STATED = re.compile(
    r"^\d{4}-\d{2}-\d{2}[ T][\d:,.]+Z?\s+\[?(DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL|FATAL)\]?(?=\s|:)"  # noqa: E501
)
_STATED_SEVERITY = {
    "DEBUG": Severity.DEBUG,
    "INFO": Severity.INFO,
    "WARNING": Severity.WARNING,
    "WARN": Severity.WARNING,
    "ERROR": Severity.ERROR,
    "CRITICAL": Severity.CRITICAL,
    "FATAL": Severity.CRITICAL,
}


def classify(svc: ServiceConfig, line: str) -> Severity:
    """Severity: a stated level, else AMS's heuristics; then the service's ``escalate`` patterns."""
    m = _STATED.match(line)
    if m and svc.log_format in ("auto", "level-prefix"):
        sev = _STATED_SEVERITY[m.group(1)]
        return (
            Severity.ERROR
            if sev < Severity.ERROR and any(p.search(line) for p in svc.escalate)
            else sev
        )
    sev = LogLine.from_raw(
        svc.id, "stderr", line.encode("utf-8", "replace"), svc.log_format
    ).severity
    if sev < Severity.ERROR and any(p.search(line) for p in svc.escalate):
        return Severity.ERROR
    return sev


def noteworthy(svc: ServiceConfig, lines: list[str]) -> Iterator[tuple[str, Severity]]:
    """WARNING-or-worse lines that are not on the service's static ignore list."""
    for ln in lines:
        if any(p.search(ln) for p in svc.ignore):
            continue
        sev = classify(svc, ln)
        if sev >= Severity.WARNING:
            yield ln, sev
