import json

from ams.ops.config import ForwardConfig
from ams.ops.forward import MAX_TRIES, Forwarder, render, severity_of
from ams.ops.gate import Incident

NOW = 1_000_000.0


def make(tmp_path, post, tokens="tok"):
    tf = tmp_path / "tok"
    if tokens is not None:
        tf.write_text(tokens + "\n")
    state = {"outbox": []}
    return Forwarder(ForwardConfig(token_file=tf), state, post=post, clock=lambda: NOW), state


def test_delivers_with_the_bearer_token_and_clears_the_outbox(tmp_path):
    seen = []

    def post(url, headers, body, timeout):
        seen.append((url, headers["Authorization"], json.loads(body)))
        return 202, b'{"outcome":"accepted"}'

    fw, state = make(tmp_path, post)
    assert fw.enqueue("k1", {"origin": "mini.x", "title": "t"}) is True
    assert fw.enqueue("k1", {"origin": "mini.x", "title": "t"}) is False  # already queued
    assert fw.flush() == (1, 0) and state["outbox"] == []
    assert seen == [
        (
            "http://127.0.0.1:18090/escalate",
            "Bearer tok",
            {"origin": "mini.x", "title": "t", "key": "k1"},
        )
    ]


def test_an_unreachable_intake_keeps_the_entry_and_backs_off(tmp_path):
    fw, state = make(tmp_path, lambda *a: (0, b"ConnectionRefusedError"))
    fw.enqueue("k", {"origin": "mini.x", "title": "t"})
    assert fw.flush() == (0, 1)
    assert state["outbox"][0]["tries"] == 1 and state["outbox"][0]["next_try"] > NOW
    assert fw.flush() == (0, 0)  # not due yet


def test_a_rejected_entry_is_dropped_and_so_is_one_that_never_gets_through(tmp_path):
    fw, state = make(tmp_path, lambda *a: (401, b'{"error":"unknown token"}'))
    fw.enqueue("k", {"origin": "mini.x", "title": "t"})
    fw.flush()
    assert state["outbox"] == []
    fw2, state2 = make(tmp_path, lambda *a: (503, b""))
    fw2.enqueue("k", {"origin": "mini.x", "title": "t"})
    for _ in range(MAX_TRIES):
        state2["outbox"][0]["next_try"] = 0
        fw2.flush()
    assert state2["outbox"] == []


def test_without_a_token_nothing_is_sent_and_nothing_is_lost(tmp_path):
    fw, state = make(tmp_path, lambda *a: (202, b"{}"), tokens=None)
    fw.enqueue("k", {"origin": "mini.x", "title": "t"})
    assert fw.flush() == (0, 0) and len(state["outbox"]) == 1


def test_render_uses_the_stable_cause_as_title_and_puts_details_in_text():
    inc = Incident(
        "health",
        "asr",
        "asr|health",
        "asr 健康检查连续失败（HTTP 500）",
        "HEALTH",
        evidence=("line one",),
    )
    p = render("mini", inc, None)
    assert p["origin"] == "mini.asr" and p["title"] == "asr health" and p["severity"] == "error"
    assert "HTTP 500" in p["text"] and "line one" in p["text"] and "需要人看一眼" in p["text"]


def test_log_incident_keeps_its_signature_as_title_and_level_as_severity():
    warn = Incident("log", "asr", "asr|log|retry N", "x", "NEW", facts={"severity": "WARNING"})
    err = Incident("log", "asr", "asr|log|boom", "x", "NEW", facts={"severity": "ERROR"})
    assert render("mini", warn, None)["title"] == "asr|log|retry N"
    assert (severity_of(warn), severity_of(err)) == ("warn", "error")


def test_secrets_in_evidence_are_redacted_before_they_leave():
    inc = Incident(
        "log", "x", "x|log|a", "h", "NEW", evidence=("auth Bearer abcdefghijklmnop12345",)
    )
    assert "abcdefghijklmnop12345" not in render("mini", inc, None)["text"]
