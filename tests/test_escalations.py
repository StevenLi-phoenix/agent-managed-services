"""The escalation journal and `ams escalations`: where the agent reads what the
deterministic decision layer could not settle by itself."""

from __future__ import annotations

import io
import json
import os
import stat
from pathlib import Path

import pytest

from ams.cli import main
from ams.decision import Action, Decision, JsonLinesEscalation, ServiceContext
from ams.escalations import (
    JOURNAL_NAME,
    EscalationJournal,
    format_record,
    journal_path,
    read_records,
)
from ams.events import LogLine, ServiceExited, Severity
from ams.schema import ServiceDecl, StartSpec
from ams.state import StateDir

DECL = ServiceDecl(id="web", start=StartSpec(argv=("true",)))
CTX = ServiceContext(DECL)
ESCALATE = Decision(Action.ESCALATE, "error line")


@pytest.fixture
def state(tmp_path: Path) -> StateDir:
    st = StateDir(tmp_path / "state")
    st.ensure()
    return st


def test_journal_lives_in_the_logs_dir_and_is_private(state: StateDir) -> None:
    journal = EscalationJournal(journal_path(state), source="harness")
    journal.append({"kind": "LogLine", "service_id": "web", "reason": "x"})
    path = state.logs_dir / JOURNAL_NAME
    assert path.is_file()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_each_record_is_one_line_with_a_timestamp_and_its_source(state: StateDir) -> None:
    journal = EscalationJournal(journal_path(state), source="core-sync")
    journal.append({"kind": "CoreSync", "service_id": "core", "reason": "a\nb"})
    lines = journal_path(state).read_text().splitlines()
    assert len(lines) == 1  # an embedded newline never splits a record
    rec = json.loads(lines[0])
    assert rec["source"] == "core-sync"
    assert rec["ts"].endswith("Z") and "T" in rec["ts"]
    assert rec["reason"] == "a\nb"


def test_json_lines_escalation_tees_into_the_journal(state: StateDir) -> None:
    out = io.StringIO()
    sink = JsonLinesEscalation(
        stream=out, journal=EscalationJournal(journal_path(state), source="harness")
    )
    sink.escalate(LogLine("web", "stderr", "boom", Severity.ERROR), ESCALATE, CTX)
    stdout_rec = json.loads(out.getvalue())
    [journal_rec] = read_records(journal_path(state))
    assert stdout_rec["kind"] == journal_rec["kind"] == "LogLine"
    assert journal_rec["service_id"] == "web" and journal_rec["source"] == "harness"


def test_a_broken_journal_never_breaks_escalation(tmp_path: Path) -> None:
    out = io.StringIO()
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    sink = JsonLinesEscalation(
        stream=out, journal=EscalationJournal(blocker / "escalations.jsonl", source="harness")
    )
    sink.escalate(ServiceExited("web", 1, 1, None, 0.1), ESCALATE, CTX)
    assert json.loads(out.getvalue())["kind"] == "ServiceExited"


def test_rotation_keeps_one_previous_file_and_reads_span_both(state: StateDir) -> None:
    path = journal_path(state)
    journal = EscalationJournal(path, source="harness", max_bytes=400)
    for i in range(20):
        journal.append({"kind": "LogLine", "service_id": "web", "reason": f"line {i}"})
    assert path.with_name(JOURNAL_NAME + ".1").is_file()
    assert path.stat().st_size <= 400 + 200
    reasons = [r["reason"] for r in read_records(path)]
    assert reasons[-1] == "line 19"
    assert reasons == sorted(reasons, key=lambda r: int(r.split()[1]))  # oldest first


def test_read_records_skips_garbage_lines(state: StateDir) -> None:
    path = journal_path(state)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"kind": "A", "service_id": "x"}\nnot json\n[1]\n{"kind": "B"}\n')
    assert [r["kind"] for r in read_records(path)] == ["A", "B"]


def test_human_format_strips_terminal_control_sequences() -> None:
    rec = {
        "ts": "2026-10-01T00:00:00Z",
        "source": "harness",
        "kind": "LogLine",
        "service_id": "web",
        "reason": "error line",
        "event": {"text": "\x1b[2J\x1b]0;pwned\x07evil\rtext"},
    }
    line = format_record(rec)
    assert "\x1b" not in line and "\x07" not in line and "\r" not in line
    assert "web" in line and "LogLine" in line and "evil" in line


def _seed(state: StateDir) -> None:
    j = EscalationJournal(journal_path(state), source="harness")
    j.append({"kind": "LogLine", "service_id": "web", "reason": "r1", "event": {"text": "t1"}})
    j.append({"kind": "ServiceExited", "service_id": "db", "reason": "r2", "event": {}})
    j.append({"kind": "LogLine", "service_id": "web", "reason": "r3", "event": {"text": "t3"}})


def test_cli_prints_the_newest_records(state: StateDir, capsys) -> None:
    _seed(state)
    rc = main(["escalations", "--state-dir", str(state.root), "-n", "2"])
    out = capsys.readouterr().out.splitlines()
    assert rc == 0
    assert len(out) == 2 and "r2" in out[0] and "r3" in out[1]


def test_cli_filters_by_service_and_emits_json(state: StateDir, capsys) -> None:
    _seed(state)
    rc = main(["escalations", "--state-dir", str(state.root), "--service", "web", "--json"])
    recs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert rc == 0
    assert [r["reason"] for r in recs] == ["r1", "r3"]


def test_cli_with_no_journal_says_so_and_succeeds(state: StateDir, capsys) -> None:
    rc = main(["escalations", "--state-dir", str(state.root)])
    assert rc == 0
    assert "no escalations" in capsys.readouterr().err
