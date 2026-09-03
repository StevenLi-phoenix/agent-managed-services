"""The two wirings that connect T4.3's rollback to the operator front door.

Both are one line of production code and both fail silently without a test:
an unwired subcommand is only an ``argparse`` error nobody runs, and a field
missing from :class:`~ams.platform.sync.ServiceRecord` is dropped the next time
the sync loop rewrites ``platform/state.json`` -- see DECISIONS D27 (T4.3),
"``rolled_back_from`` is written by editing the raw JSON document".
"""

from __future__ import annotations

import json

import pytest

from ams.cli import main
from ams.platform.sync import STATE_VERSION, PlatformState, ServiceRecord, state_path
from ams.state import StateDir


def test_platform_rollback_is_reachable_from_the_ams_entry_point(capsys):
    """``ams platform rollback --help`` exits 0 and names the verb's own flags."""
    with pytest.raises(SystemExit) as exc:
        main(["platform", "rollback", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "ams platform rollback" in out
    assert "--to SHA" in out


def test_service_record_round_trip_keeps_rolled_back_from(tmp_path):
    """A sync flush must not drop the field a rollback wrote.

    ``PlatformState.load`` keeps only keys the dataclass declares, so this is
    the regression that would silently erase the rollback marker on the next
    60 s tick.
    """
    state = StateDir(tmp_path)
    state.ensure()
    path = state_path(state)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": STATE_VERSION,
                "services": {
                    "kvservice": {
                        "sha": "a" * 40,
                        "stage": "healthy",
                        "rolled_back_from": "b" * 40,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = PlatformState.load(path)
    assert loaded.records["kvservice"].rolled_back_from == "b" * 40

    # Force a write, then re-read from disk: the field survives the rewrite.
    loaded.set_stage("kvservice", "declared", now_s=0.0)
    assert loaded.flush() is True
    reread = json.loads(path.read_text(encoding="utf-8"))
    assert reread["services"]["kvservice"]["rolled_back_from"] == "b" * 40

    assert ServiceRecord().rolled_back_from is None
