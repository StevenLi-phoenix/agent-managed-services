"""Delivery of incidents to the escalation journal, through ``ams intake`` on the harness.

``ams watch`` runs as the owner (it must reach the owner's launchd domain and logs), the journal
belongs to the harness user, so the hand-over is a local HTTP POST. A durable outbox in the state
file means an intake restart or a harness upgrade loses nothing.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from ams.ops.actions import ActionResult
from ams.ops.config import ForwardConfig
from ams.ops.gate import Incident
from ams.ops.text import clip, sanitize

log = logging.getLogger("ams.ops.forward")

MAX_TRIES = 8
Post = Callable[[str, dict[str, str], bytes, float], tuple[int, bytes]]


def http_post(url: str, headers: dict[str, str], body: bytes, timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - config-controlled
            return resp.status, resp.read(100_000)
    except urllib.error.HTTPError as e:
        return e.code, e.read(100_000)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 0, f"{type(e).__name__}: {e}".encode()


def severity_of(inc: Incident) -> str:
    """Log lines keep their own level; everything else an incident means is an error."""
    if inc.kind == "log":
        return (
            "warn" if inc.facts.get("severity") in (None, "WARNING", "INFO", "DEBUG") else "error"
        )
    return "error"


def render(origin: str, inc: Incident, result: ActionResult | None) -> dict[str, Any]:
    """The intake payload for one incident.

    ``title`` is what the operator's gate signs, so it is the stable cause (service and kind, or
    the normalised log signature); the changing details ride in ``text``.
    """
    title = f"{inc.service} {inc.kind}" if inc.kind != "log" else inc.signature
    lines = [sanitize(inc.headline, 200)]
    lines += [f"- {sanitize(e, 160)}" for e in inc.evidence[:3]]
    if result is not None:
        lines.append(result.line())
    fixed = result is not None and result.executed and result.ok
    lines.append("已自动处理，无需操作" if fixed else "需要人看一眼")
    return {
        "origin": f"{origin}.{inc.service}",
        "severity": severity_of(inc),
        "title": clip(title, 280),
        "text": clip("\n".join(lines), 7900),
    }


class Forwarder:
    def __init__(
        self,
        cfg: ForwardConfig,
        state: dict[str, Any],
        *,
        post: Post = http_post,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.cfg, self.state, self.post, self.clock = cfg, state, post, clock

    def enqueue(self, key: str, payload: dict[str, Any]) -> bool:
        """Queue one escalation; ``False`` when the same key is already waiting."""
        if any(o["key"] == key for o in self.state["outbox"]):
            return False
        self.state["outbox"].append(
            {
                "key": clip(key, 190),
                "body": {**payload, "key": clip(key, 190)},
                "tries": 0,
                "next_try": 0.0,
            }
        )
        return True

    def flush(self) -> tuple[int, int]:
        """Deliver what is due. Returns ``(delivered, failed_attempts)``."""
        now = self.clock()
        try:
            token = self.cfg.token_file.read_text(encoding="utf-8").strip()
        except OSError as e:
            if self.state["outbox"]:
                log.error(
                    "intake token unreadable (%s); %d queued", e.strerror, len(self.state["outbox"])
                )
            return 0, 0
        delivered = failed = 0
        keep = []
        for o in self.state["outbox"]:
            if o["next_try"] > now:
                keep.append(o)
                continue
            status, resp = self.post(
                self.cfg.url,
                {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json.dumps(o["body"], ensure_ascii=False).encode(),
                self.cfg.timeout_s,
            )
            if 200 <= status < 300:
                delivered += 1
                log.info(
                    "escalated %s key=%s: %s",
                    o["body"].get("origin"),
                    o["key"],
                    resp[:60].decode("utf-8", "replace"),
                )
                continue
            failed += 1
            o["tries"] += 1
            if status in (400, 401, 403, 413, 422) or o["tries"] >= MAX_TRIES:
                log.error(
                    "dropping escalation %s after %s tries (HTTP %s): %s",
                    o["key"],
                    o["tries"],
                    status,
                    resp[:120].decode("utf-8", "replace"),
                )
                continue
            o["next_try"] = now + min(300 * 2 ** (o["tries"] - 1), 3600)
            log.warning("intake unreachable (HTTP %s), retry in %ds", status, o["next_try"] - now)
            keep.append(o)
        self.state["outbox"] = keep
        return delivered, failed
