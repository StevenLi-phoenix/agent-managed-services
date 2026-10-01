"""``ams platform core ...``: argument wiring and the thin handlers."""

from __future__ import annotations

import json
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from ams.cli import build_parser
from ams.platform import cli as platform_cli
from ams.platform import core, coresync
from ams.state import StateDir

CONFIG = """\
[core]
url = "/srv/upstream/api.git"
node = "24.20.0"
pnpm = "11.19.0"
plugins = ["secrets", "store", "gateway", "health", "timeservice"]
"""


@pytest.fixture
def state() -> Iterator[StateDir]:
    root = Path(tempfile.mkdtemp(prefix="ams-ccli-", dir="/tmp"))
    try:
        st = StateDir(root / "s")
        core.config_path(st).parent.mkdir(parents=True)
        core.config_path(st).write_text(CONFIG, encoding="utf-8")
        yield st
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args)) if hasattr(args, "func") else int(platform_cli.cmd_platform(args))


def _parse(argv: list[str]) -> Any:
    return build_parser().parse_args(["platform", "core", *argv])


def test_wiring_of_every_verb() -> None:
    a = _parse(
        [
            "config",
            "import",
            "p.json",
            "--rebase",
            "/var/lib/core=@data",
            "--rebase",
            "/etc/core=@etc",
            "--jwt-dir",
            "j",
            "--fonts",
            "f",
        ]
    )
    assert a.core_command == "config" and a.config_command == "import"
    assert a.rebase == ["/var/lib/core=@data", "/etc/core=@etc"]
    assert a.jwt_dir == Path("j") and a.fonts == Path("f")
    assert _parse(["sync", "--no-isolation"]).no_isolation is True
    assert _parse(["bootstrap"]).no_isolation is False
    assert _parse(["status", "--json"]).json is True
    assert _parse(["release", "--rollback"]).rollback is True
    s = _parse(["ship", "timeservice", "health", "--force"])
    assert s.ids == ["timeservice", "health"] and s.force is True


def test_the_legacy_platform_verbs_are_gone() -> None:
    for verb in ("sync", "status", "bootstrap", "rollback", "pool"):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["platform", verb])


def test_release_requires_rollback_flag(
    state: StateDir, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = platform_cli.cmd_platform(_parse(["release", "--state-dir", str(state.root)]))
    assert rc == 1
    assert "--rollback" in capsys.readouterr().err


def test_config_import_prints_names_only(
    state: StateDir, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = tmp_path / "plugins.json"
    bundle.write_text(
        json.dumps(
            {
                "plugins": {
                    "gateway": {"config": {"port": 18080}},
                    "auth": {"config": {"secret": "S3CR3T-VALUE", "key": "/etc/core/jwt.pem"}},
                }
            }
        ),
        encoding="utf-8",
    )
    jwt = tmp_path / "jwt"
    jwt.mkdir()
    (jwt / "jwt.pem").write_text("PEM-BYTES", encoding="utf-8")
    (jwt / "jwt.pub").write_text("PUB-BYTES", encoding="utf-8")
    rc = platform_cli.cmd_platform(
        _parse(
            [
                "config",
                "import",
                str(bundle),
                "--rebase",
                "/etc/core=@etc",
                "--jwt-dir",
                str(jwt),
                "--state-dir",
                str(state.root),
            ]
        )
    )
    out, err = capsys.readouterr()
    assert rc == 0, err
    assert out.split() == ["jwt.pem", "jwt.pub", "plugins.json"]
    assert "S3CR3T" not in out + err and "PEM-BYTES" not in out + err
    written = json.loads(
        (core.bundle_master_dir(state) / "plugins.json").read_text(encoding="utf-8")
    )
    assert written["plugins"]["auth"]["config"]["key"] == str(
        core.CoreLayout.from_state(state).etc / "jwt.pem"
    )


def test_config_import_bad_rebase_syntax(
    state: StateDir, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = platform_cli.cmd_platform(
        _parse(
            [
                "config",
                "import",
                str(tmp_path / "x.json"),
                "--rebase",
                "nope",
                "--state-dir",
                str(state.root),
            ]
        )
    )
    assert rc == 1
    assert "OLD=NEW" in capsys.readouterr().err


def test_missing_config_is_exit_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = platform_cli.cmd_platform(_parse(["sync", "--state-dir", str(tmp_path / "none")]))
    assert rc == 1
    assert "no core config" in capsys.readouterr().err


def test_sync_passes_isolation_and_returns_the_tick_exit_code(
    state: StateDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def fake_tick(st, store, cfg, *, isolation=True, **kw):  # noqa: ANN001, ANN202
        seen.update(state=st, isolation=isolation, roster=cfg.plugins)
        return coresync.CoreReport(sha="a" * 40, failed=("timeservice",))

    monkeypatch.setattr(coresync, "tick", fake_tick)
    rc = platform_cli.cmd_platform(
        _parse(["sync", "--no-isolation", "--state-dir", str(state.root)])
    )
    assert rc == 1
    assert seen["isolation"] is False
    assert seen["state"].root == state.root


def test_ship_and_rollback_delegate(state: StateDir, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, Any]] = []

    def fake_ship(st, store, cfg, ids, *, force=False, isolation=True, **kw):  # noqa: ANN001, ANN202
        calls.append(("ship", (list(ids), force, isolation)))
        return coresync.CoreReport(shipped=tuple(ids), live=tuple(ids))

    def fake_rollback(st, store, cfg, *, isolation=True, **kw):  # noqa: ANN001, ANN202
        calls.append(("rollback", isolation))
        return coresync.CoreReport(released=True, release_ok=True, rolled_back=True)

    monkeypatch.setattr(coresync, "ship", fake_ship)
    monkeypatch.setattr(coresync, "rollback_release", fake_rollback)
    assert (
        platform_cli.cmd_platform(
            _parse(["ship", "timeservice", "--force", "--state-dir", str(state.root)])
        )
        == 0
    )
    assert (
        platform_cli.cmd_platform(
            _parse(["release", "--rollback", "--no-isolation", "--state-dir", str(state.root)])
        )
        == 0
    )
    assert calls == [("ship", (["timeservice"], True, True)), ("rollback", False)]


def _write_record(state: StateDir) -> None:
    rec = {
        "version": 1,
        "staged_sha": "b" * 40,
        "release_sha": "a" * 40,
        "previous_release_sha": None,
        "release_failed_sha": None,
        "planned_sha": "b" * 40,
        "planned_roster": ["secrets"],
        "plugins": {
            "timeservice": {
                "content_key": "c" * 64,
                "artifact_id": "d" * 64,
                "sha": "b" * 40,
                "outcome": "failed",
                "reason": "deploy rejected: x",
                "at": "2026-09-29T00:00:00Z",
            },
            "health": {
                "content_key": "e" * 64,
                "artifact_id": "f" * 64,
                "sha": "a" * 40,
                "outcome": "live",
                "reason": None,
                "at": "2026-09-29T00:00:00Z",
            },
        },
        "build_failures": {},
        "escalated": {},
    }
    core.record_path(state).write_text(json.dumps(rec), encoding="utf-8")


def test_status_json_offline_from_the_record(
    state: StateDir, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_record(state)
    rc = platform_cli.cmd_platform(
        _parse(["status", "--json", "--offline", "--state-dir", str(state.root)])
    )
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["release_sha"] == "a" * 40
    assert out["core"] == "not queried"
    plugins = {p["plugin"]: p for p in out["plugins"]}
    assert list(plugins) == ["secrets", "store", "gateway", "health", "timeservice"]
    assert plugins["timeservice"]["ams"]["outcome"] == "failed"
    assert plugins["health"]["ams"]["artifact"] == "f" * 12
    assert plugins["secrets"]["ams"] is None


def test_status_view_with_live_core_marks_drift() -> None:
    record = {
        "release_sha": "a" * 40,
        "plugins": {
            "health": {
                "content_key": "e" * 64,
                "artifact_id": "f" * 64,
                "sha": "a" * 40,
                "outcome": "live",
                "reason": None,
            }
        },
    }
    live = {
        "plugins": [
            {
                "pluginId": "health",
                "desired": {"artifactId": "9" * 64, "enabled": True, "privileges": ["ops.read"]},
                "observed": {"phase": "active", "artifactId": "9" * 64},
                "live": {"phase": "active", "artifactId": "9" * 64, "commit": "1" * 40},
            },
            {"pluginId": "hello", "desired": None, "observed": None, "live": None},
        ]
    }
    view = coresync.status_view(record, live, ("health",))
    rows = {p["plugin"]: p for p in view["plugins"]}
    assert view["core"] == "reachable"
    assert rows["health"]["drift"] is True
    assert rows["health"]["phase"] == "active"
    assert rows["health"]["artifact"] == "9" * 12
    assert rows["health"]["commit"] == "1" * 12
    assert rows["health"]["privileges"] == ["ops.read"]
    assert "hello" in rows  # installed by hand: still shown
    lines = platform_cli.format_core_status(view)
    assert any("drift" in line for line in lines)
    assert any(line.startswith("release") for line in lines)


def test_status_text_without_record(state: StateDir, capsys: pytest.CaptureFixture[str]) -> None:
    rc = platform_cli.cmd_platform(_parse(["status", "--offline", "--state-dir", str(state.root)]))
    assert rc == 2
    assert "no core record" in capsys.readouterr().err
