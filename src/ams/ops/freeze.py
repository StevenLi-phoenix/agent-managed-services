"""Planned-maintenance freezes: ``freeze.json`` in the config dir, written by ``ams-watch freeze``.

A frozen service is still observed, but no action ever runs on it; with ``mute`` its incidents go
to the digest instead of being triaged or notified (a planned restart is not an outage).
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class Freeze:
    service: str  # service id, or "*"
    until: float
    reason: str
    mute: bool = True


def load(path: Path, now: float | None = None) -> dict[str, Freeze]:
    now = time.time() if now is None else now
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return {}
    out: dict[str, Freeze] = {}
    for sid, v in raw.items() if isinstance(raw, dict) else ():
        try:
            f = Freeze(sid, float(v["until"]), str(v.get("reason", "")), bool(v.get("mute", True)))
        except (KeyError, TypeError, ValueError):
            continue
        if f.until > now:
            out[sid] = f
    return out


def _write(path: Path, freezes: dict[str, Freeze]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".freeze-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(
            {
                k: {kk: vv for kk, vv in asdict(v).items() if kk != "service"}
                for k, v in freezes.items()
            },
            f,
            indent=1,
        )
    os.replace(tmp, path)


def add(
    path: Path,
    service: str,
    seconds: float,
    reason: str,
    mute: bool = True,
    now: float | None = None,
) -> Freeze:
    now = time.time() if now is None else now
    cur = load(path, now)
    cur[service] = Freeze(service, now + seconds, reason, mute)
    _write(path, cur)
    return cur[service]


def remove(path: Path, service: str, now: float | None = None) -> bool:
    cur = load(path, now)
    had = cur.pop(service, None) is not None
    _write(path, cur)
    return had


def lookup(freezes: dict[str, Freeze], service: str) -> Freeze | None:
    return freezes.get(service) or freezes.get("*")
