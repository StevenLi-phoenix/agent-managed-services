"""Tests for the ``pool`` key in ``service.ams.toml`` (PLAN-pool.md §3.1-3.2, §5.2).

Scope is deliberately narrow: this module tests only what
``ams.platform.static.load_ams_overlay`` / ``overlay_pool`` can decide from a
single overlay file. The `kind: static` cross-check ("pool is not valid for
kind: static") and cross-manifest checks (member id collisions, reserved
admin port name clash) belong to sync/translate, which see more than one
file -- they are out of scope here per PLAN-pool.md §3.2's own split.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ams.platform.static import (
    Overlay,
    StaticError,
    load_ams_overlay,
    overlay_pool,
    overlay_secret_names,
)

# --------------------------------------------------------------------------- pool


def test_overlay_pool_parses(tmp_path: Path) -> None:
    (tmp_path / "service.ams.toml").write_text('pool = "core"\n', encoding="utf-8")
    ov = load_ams_overlay(tmp_path)
    assert ov.pool == "core"
    assert overlay_pool(tmp_path) == "core"


def test_overlay_pool_non_string_rejected(tmp_path: Path) -> None:
    (tmp_path / "service.ams.toml").write_text("pool = 3\n", encoding="utf-8")
    with pytest.raises(StaticError, match=r"pool must be a string, got 3") as exc:
        load_ams_overlay(tmp_path)
    assert str(tmp_path / "service.ams.toml") in str(exc.value)


def test_overlay_pool_bad_charset_rejected(tmp_path: Path) -> None:
    (tmp_path / "service.ams.toml").write_text('pool = "Core"\n', encoding="utf-8")
    with pytest.raises(StaticError, match=r"pool 'Core' does not match") as exc:
        load_ams_overlay(tmp_path)
    assert str(tmp_path / "service.ams.toml") in str(exc.value)


@pytest.mark.parametrize("reserved", ["registry", "auth", "caddy", "pool"])
def test_overlay_pool_reserved_value_rejected(tmp_path: Path, reserved: str) -> None:
    (tmp_path / "service.ams.toml").write_text(f'pool = "{reserved}"\n', encoding="utf-8")
    with pytest.raises(StaticError, match=rf"pool '{reserved}' is reserved") as exc:
        load_ams_overlay(tmp_path)
    assert str(tmp_path / "service.ams.toml") in str(exc.value)


def test_overlay_pool_with_secrets_and_env(tmp_path: Path) -> None:
    (tmp_path / "service.ams.toml").write_text(
        'pool = "core"\nsecrets = ["DEEPSEEK_API_KEY"]\n[env]\nFEATURE_X = "on"\n',
        encoding="utf-8",
    )
    ov = load_ams_overlay(tmp_path)
    assert ov.pool == "core"
    assert ov.secrets == ("DEEPSEEK_API_KEY",)
    assert dict(ov.env) == {"FEATURE_X": "on"}
    assert overlay_secret_names(tmp_path) == ["DEEPSEEK_API_KEY"]


def test_overlay_unknown_top_key_still_rejected(tmp_path: Path) -> None:
    (tmp_path / "service.ams.toml").write_text('typo = "x"\n', encoding="utf-8")
    with pytest.raises(StaticError, match="unknown key"):
        load_ams_overlay(tmp_path)


def test_overlay_without_pool_key_has_none(tmp_path: Path) -> None:
    (tmp_path / "service.ams.toml").write_text('secrets = ["DEEPSEEK_API_KEY"]\n', encoding="utf-8")
    ov = load_ams_overlay(tmp_path)
    assert ov.pool is None
    assert overlay_pool(tmp_path) is None


def test_overlay_pool_missing_file_returns_none(tmp_path: Path) -> None:
    assert overlay_pool(tmp_path) is None
    assert load_ams_overlay(tmp_path) == Overlay()


def test_overlay_fields_unchanged_besides_pool(tmp_path: Path) -> None:
    ov = Overlay()
    assert ov.secrets == ()
    assert dict(ov.env) == {}
    assert ov.pool is None
