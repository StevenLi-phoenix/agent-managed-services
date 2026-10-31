"""Health probes: an HTTP GET or a unix-socket connect. Nothing is sent that could change state."""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Probe:
    ok: bool
    detail: str


def http(url: str, timeout: float = 5.0, accept: tuple[int, ...] = ()) -> Probe:
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "ams-watch"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - config-controlled
            code = resp.status
    except urllib.error.HTTPError as e:
        return Probe(e.code in accept, f"HTTP {e.code}")
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
        reason = getattr(e, "reason", e)
        return Probe(False, f"{type(reason).__name__}: {reason}"[:160])
    return Probe(200 <= code < 400 or code in accept, f"HTTP {code}")


def unix_socket(path: Path, timeout: float = 3.0) -> Probe:
    """Connect and close. The cookie proxy is probed this way so no cookies are ever requested."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(path))
    except OSError as e:
        return Probe(False, f"{type(e).__name__}: {e}"[:160])
    finally:
        s.close()
    return Probe(True, "connect ok")


def get_json(url: str, timeout: float = 5.0) -> object | None:
    """GET and decode JSON; ``None`` on any failure (callers treat that as "unknown")."""
    req = urllib.request.Request(url, headers={"User-Agent": "ams-watch"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - config-controlled
            return json.loads(resp.read(2_000_000))
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, ValueError):
        return None
