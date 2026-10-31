"""``ams intake``: an HTTP receiver that appends external escalations to the journal.

Other hosts (and other users on this one) cannot write the harness's journal; they POST here.
One bearer token, ``POST /escalate`` with the ``warn`` payload (see :mod:`ams.external`),
``GET /healthz`` for the supervisor's health probe. Run it as an ordinary declared service of the
harness, so it shares the journal's owner and is kept alive and probed like any other:

    argv = ["…/python", "-m", "ams", "intake", "serve", "--state-dir", "…",
            "--bind", "0.0.0.0", "--port", "18090"]
    secrets = ["AMS_INTAKE_TOKEN"]

Stdlib only. Bodies are capped at 64 KiB; a key seen in the last hour is a ``duplicate`` (the
sender's retry or spool replay), and is not written twice.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
import time
from collections.abc import Callable, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from ams.escalations import EscalationJournal
from ams.external import ExternalError, parse_payload, record_for

log = logging.getLogger("ams.intake")

MAX_BODY = 64 * 1024
DEDUPE_S = 3600.0
DEDUPE_CAP = 4096
TOKEN_ENV = "AMS_INTAKE_TOKEN"


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


class Intake:
    """The transport-free half: bytes in, ``(status, body)`` out."""

    def __init__(
        self,
        journal: EscalationJournal,
        token: str,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.journal, self.token, self.clock = journal, token, clock
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def _authorized(self, headers: Mapping[str, str]) -> bool:
        auth = headers.get("authorization", "")
        given = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        return bool(given) and hmac.compare_digest(_digest(given), _digest(self.token))

    def _duplicate(self, key: str) -> bool:
        now = self.clock()
        self._seen = {k: t for k, t in self._seen.items() if now - t < DEDUPE_S}
        if key in self._seen:
            return True
        if len(self._seen) >= DEDUPE_CAP:
            self._seen.pop(next(iter(self._seen)))
        self._seen[key] = now
        return False

    def handle(
        self, method: str, path: str, headers: Mapping[str, str], body: bytes
    ) -> tuple[int, dict[str, Any]]:
        path = path.split("?", 1)[0]
        if path == "/healthz":
            return (
                (200, {"ok": True})
                if method in ("GET", "HEAD")
                else (405, {"error": "method not allowed"})
            )
        if path != "/escalate":
            return 404, {"error": "not found"}
        if method != "POST":
            return 405, {"error": "method not allowed"}
        if not self.token:
            return 503, {"error": "no intake token is configured"}
        if not self._authorized(headers):
            log.warning("rejected push: bad or missing token")
            return 401, {"error": "unknown token"}
        if len(body) > MAX_BODY:
            return 413, {"error": "body too large"}
        try:
            event = parse_payload(json.loads(body.decode("utf-8")))
        except (ValueError, UnicodeDecodeError) as e:  # ExternalError is a ValueError
            return 400, {"error": str(e) if isinstance(e, ExternalError) else "body is not JSON"}
        seen_key = f"{event.origin}|{event.key}"
        with self._lock:
            if self._duplicate(seen_key):
                return 202, {"outcome": "duplicate"}
            if not self.journal.append(record_for(event)):
                self._seen.pop(seen_key, None)
                return 503, {"error": "journal unavailable"}
        log.info("%s %s (%s): accepted", event.origin, event.severity, event.key)
        return 202, {"outcome": "accepted"}


def make_server(intake: Intake, host: str, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "ams-intake"

        def _serve(self) -> None:
            length = int(self.headers.get("content-length") or 0)
            # Never read more than the cap (+1 so an oversized body is still seen as oversized).
            body = self.rfile.read(min(length, MAX_BODY + 1)) if length > 0 else b""
            headers = {k.lower(): v for k, v in self.headers.items()}
            status, out = intake.handle(self.command, self.path, headers, body)
            data = json.dumps(out).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.send_header("connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        do_GET = do_POST = do_HEAD = do_PUT = do_DELETE = _serve  # noqa: N815

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            pass  # access lines would be noise next to the accepted/rejected lines above

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def read_token(token_file: Path | None) -> str:
    if token_file is not None:
        return token_file.read_text(encoding="utf-8").strip()
    return os.environ.get(TOKEN_ENV, "").strip()


def serve(journal: EscalationJournal, token: str, host: str, port: int) -> int:
    if not token:
        log.error("no token: set $%s (ams secret set) or pass --token-file", TOKEN_ENV)
        return 3
    server = make_server(Intake(journal, token), host, port)
    log.info("intake listening on %s:%d, journal %s", host, server.server_address[1], journal.path)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
