"""`ams intake`: a small HTTP receiver that appends external escalations to the journal."""

from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path

import pytest

from ams.escalations import EscalationJournal, read_records
from ams.intake import Intake, make_server

TOKEN = "t0ken-for-tests"


@pytest.fixture
def intake(tmp_path: Path) -> Intake:
    return Intake(
        EscalationJournal(tmp_path / "logs" / "escalations.jsonl", source="intake"), TOKEN
    )


def post(intake: Intake, body: object, token: str | None = TOKEN) -> tuple[int, dict]:
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return intake.handle(
        "POST", "/escalate", {"authorization": f"Bearer {token}"} if token else {}, raw
    )


def test_a_valid_event_lands_in_the_journal(intake: Intake) -> None:
    status, out = post(
        intake, {"kind": "warn.n8.error", "key": "k1", "title": "unit failed", "text": "why"}
    )
    assert (status, out) == (202, {"outcome": "accepted"})
    [rec] = read_records(intake.journal.path)
    assert rec["source"] == "intake" and rec["service_id"] == "n8"
    assert json.loads(rec["event"]["text"])["msg"] == "unit failed"


def test_the_same_key_is_a_duplicate_and_writes_once(intake: Intake) -> None:
    post(intake, {"origin": "n8", "key": "k1", "title": "t"})
    assert post(intake, {"origin": "n8", "key": "k1", "title": "t"}) == (
        202,
        {"outcome": "duplicate"},
    )
    assert len(read_records(intake.journal.path)) == 1


def test_the_dedupe_window_expires(tmp_path: Path) -> None:
    now = [1000.0]
    it = Intake(
        EscalationJournal(tmp_path / "e.jsonl", source="intake"), TOKEN, clock=lambda: now[0]
    )
    post(it, {"origin": "n8", "key": "k", "title": "t"})
    now[0] += 3601
    assert post(it, {"origin": "n8", "key": "k", "title": "t"})[1] == {"outcome": "accepted"}


def test_auth_is_required_and_checked_before_the_body(intake: Intake) -> None:
    assert post(intake, {"origin": "n8", "title": "t"}, token=None)[0] == 401
    assert post(intake, {"origin": "n8", "title": "t"}, token="wrong")[0] == 401
    assert post(intake, b"not json at all", token="wrong")[0] == 401
    assert not intake.journal.path.exists()


def test_bad_bodies_are_400_and_big_ones_413(intake: Intake) -> None:
    assert post(intake, b"{not json")[0] == 400
    assert post(intake, {"title": "no origin"})[0] == 400
    assert (
        intake.handle("POST", "/escalate", {"authorization": f"Bearer {TOKEN}"}, b"x" * 70000)[0]
        == 413
    )


def test_routes_and_methods(intake: Intake) -> None:
    assert intake.handle("GET", "/healthz", {}, b"") == (200, {"ok": True})
    assert intake.handle("GET", "/escalate", {}, b"")[0] == 405
    assert intake.handle("POST", "/nope", {"authorization": f"Bearer {TOKEN}"}, b"{}")[0] == 404


def test_no_token_configured_refuses_everything(tmp_path: Path) -> None:
    it = Intake(EscalationJournal(tmp_path / "e.jsonl", source="intake"), "")
    assert (
        it.handle("POST", "/escalate", {"authorization": "Bearer "}, b'{"origin":"a","title":"t"}')[
            0
        ]
        == 503
    )


def test_the_token_never_appears_in_a_response(intake: Intake) -> None:
    for body in ({"origin": "n8", "title": "t"}, b"junk"):
        assert TOKEN not in json.dumps(post(intake, body)[1])


def test_over_a_real_socket(intake: Intake) -> None:
    server = make_server(intake, "127.0.0.1", 0)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        body = json.dumps({"kind": "warn.phm.warn", "key": "s1", "title": "over the wire"})
        conn.request(
            "POST",
            "/escalate",
            body,
            {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        assert resp.status == 202 and json.loads(resp.read()) == {"outcome": "accepted"}
        conn.request("GET", "/healthz")
        assert conn.getresponse().status == 200
    finally:
        server.shutdown()
        server.server_close()
    assert len(read_records(intake.journal.path)) == 1
