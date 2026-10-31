"""External escalations: any program hands the operator a warning through the same journal
the supervisor writes (`ams escalate`, `ams intake`)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ams.escalations import EscalationJournal, format_record, read_records
from ams.external import ExternalError, ExternalEvent, parse_payload, record_for


def test_a_warn_cli_payload_is_understood() -> None:
    ev = parse_payload(
        {
            "kind": "warn.n8.error",
            "key": "n8-abc-2026103105",
            "subject": "n8 foo.service",
            "title": "systemd unit foo.service failed on n8",
            "text": "Active: failed",
            "url": "https://example.com/x",
            "occurredAt": 1793425004325,
        }
    )
    assert ev == ExternalEvent(
        origin="n8",
        severity="error",
        title="systemd unit foo.service failed on n8",
        key="n8-abc-2026103105",
        subject="n8 foo.service",
        text="Active: failed",
        url="https://example.com/x",
        occurred_at=1793425004325,
    )


def test_origin_and_severity_fields_beat_the_kind_and_dotted_origins_survive() -> None:
    assert parse_payload({"kind": "warn.phm.docker.crit", "title": "t"}).origin == "phm.docker"
    assert parse_payload({"kind": "warn.phm.docker.crit", "title": "t"}).severity == "crit"
    ev = parse_payload(
        {"origin": "ams-watch", "severity": "warn", "title": "t", "kind": "warn.x.info"}
    )
    assert (ev.origin, ev.severity) == ("ams-watch", "warn")


def test_default_key_subject_and_severity() -> None:
    ev = parse_payload({"origin": "mini", "title": "disk 96% full"})
    assert ev.severity == "warn" and ev.subject == "mini"
    assert ev.key.startswith("mini-") and len(ev.key) <= 200


@pytest.mark.parametrize(
    "bad",
    [
        [],
        {},
        {"origin": "n8"},  # no title
        {"origin": "n8", "title": "   "},
        {"title": "t"},  # no origin, no kind
        {"origin": "n8", "title": "t", "severity": "panic"},
        {"origin": "N8 host!", "title": "t"},
        {"origin": "n8", "title": "t", "url": "javascript:alert(1)"},
        {"origin": "n8", "title": "t", "text": 5},
        {"origin": "n8", "title": "t", "occurredAt": "yesterday"},
        {"kind": "host.disk.full", "title": "t"},  # a kind that is not warn.<origin>.<severity>
    ],
)
def test_bad_payloads_are_refused(bad: object) -> None:
    with pytest.raises(ExternalError):
        parse_payload(bad)


def test_fields_are_clipped_not_refused() -> None:
    ev = parse_payload({"origin": "n8", "title": "t" * 900, "text": "x" * 20000, "key": "k" * 500})
    assert len(ev.title) <= 300 and len(ev.text) <= 8000 and len(ev.key) <= 200


def test_the_record_has_the_shape_ga_watch_reads(tmp_path: Path) -> None:
    ev = ExternalEvent("n8", "error", "unit foo failed", "k1", "n8", "Active: failed", None, 5)
    rec = record_for(ev)
    # ga-watch (the DSH Monitor on the journal) reads event.text (a JSON log line) and reason.
    text = json.loads(rec["event"]["text"])
    assert text == {
        "level": "error",
        "msg": "unit foo failed",
        "src": "n8",
        "error": "Active: failed",
    }
    assert rec["reason"] == "external error"
    assert rec["kind"] == "External" and rec["service_id"] == "n8" and rec["action"] == "escalate"
    # ... and the journal's own reader prints it.
    journal = EscalationJournal(tmp_path / "e.jsonl", source="intake")
    journal.append(rec)
    [back] = read_records(tmp_path / "e.jsonl")
    assert back["source"] == "intake" and "unit foo failed" in format_record(back)


def test_untrusted_text_stays_inside_the_json_string() -> None:
    ev = parse_payload({"origin": "n8", "title": 'x"}\n{"level":"info"', "text": "\x1b[31mred"})
    rec = record_for(ev)
    assert "\n" not in rec["event"]["text"]
    assert json.loads(rec["event"]["text"])["msg"].startswith("x")
