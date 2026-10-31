"""`ams escalate`: the local way in (a program running as the harness user)."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from ams.cli import main
from ams.escalations import read_records


def run(tmp_path: Path, *argv: str) -> int:
    return main(["escalate", "--state-dir", str(tmp_path), *argv])


def test_escalate_appends_a_record(tmp_path: Path) -> None:
    assert run(tmp_path, "--source", "mini", "--severity", "error", "disk is full") == 0
    [rec] = read_records(tmp_path / "logs" / "escalations.jsonl")
    assert rec["source"] == "escalate" and rec["service_id"] == "mini"
    assert json.loads(rec["event"]["text"]) == {
        "level": "error",
        "msg": "disk is full",
        "src": "mini",
    }


def test_text_dash_reads_stdin_and_key_makes_repeats_one_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("line1\nline2\n"))
    assert run(tmp_path, "--source", "phm", "--key", "k", "--text", "-", "boom") == 0
    assert run(tmp_path, "--source", "phm", "--key", "k", "--text", "-", "boom") == 0
    recs = read_records(tmp_path / "logs" / "escalations.jsonl")
    assert len(recs) == 2  # no dedupe locally: the operator's gate (ga-watch) owns that
    assert json.loads(recs[0]["event"]["text"])["error"] == "line1\nline2"


def test_bad_arguments_exit_2_and_write_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(tmp_path, "--source", "n8", "--severity", "panic", "x") == 2
    assert run(tmp_path, "--source", "n8", "   ") == 2
    assert not (tmp_path / "logs" / "escalations.jsonl").exists()


def test_an_unwritable_journal_is_exit_1(tmp_path: Path) -> None:
    blocker = tmp_path / "logs"
    blocker.write_text("a file where the logs dir should be")
    assert run(tmp_path, "--source", "n8", "x") == 1


def test_intake_logs_use_a_stated_level_so_the_supervisor_does_not_misread_them(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import logging

    from ams.events import LogLine, Severity

    # `ams intake serve` without a token logs one ERROR line and exits 3; its format is what the
    # supervisor classifies when intake runs as a service with [logging] format = level-prefix.
    assert main(["intake", "serve", "--state-dir", str(tmp_path)]) == 3
    line = next(ln for ln in capsys.readouterr().err.splitlines() if "no token" in ln)
    assert line.startswith("ERROR ams.intake:")
    info = "INFO ams.intake: n8 error (k1): accepted"
    assert (
        LogLine.from_raw("intake", "stderr", info.encode(), "level-prefix").severity
        == Severity.INFO
    )
    assert logging.getLogger("ams.intake")
