"""core.node / core.pnpm must match what the staged tree asks for (review 2026-09-29,
upstream-fit-8).

pnpm only *warns* on an ``engines.node`` mismatch, so a release would build and
core would run on an unsupported Node; and a ``packageManager`` pin that differs
from ``core.pnpm`` makes pnpm switch to (and download) another version inside the
provisioning step. Both are refused at stage time, naming the fields.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_platform_coresync import (  # noqa: F401 - `world` is a fixture, used by name
    SHA_A,
    SHA_B,
    World,
    first_release,
    world,
)

from ams.platform import core


def _tree(tmp_path: Path, pkg: dict) -> Path:
    (tmp_path / "package.json").write_text(json.dumps(pkg), encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize(
    ("node_range", "version", "ok"),
    [
        (">=24.19.0 <25", "24.20.0", True),
        (">=24.19.0 <25", "24.18.0", False),
        (">=24.19.0 <25", "25.1.0", False),
        ("^24.19.0", "24.30.1", True),
        ("^24.19.0", "25.0.0", False),
        ("~24.19.0", "24.19.9", True),
        ("~24.19.0", "24.20.0", False),
        ("24.x", "24.0.1", True),
        ("24", "23.9.9", False),
        (">=22 <23 || >=24.19", "24.20.0", True),
        ("=24.20.0", "24.20.0", True),
        ("24.20.0", "24.20.1", False),
    ],
)
def test_node_satisfies(node_range: str, version: str, ok: bool) -> None:
    assert core.node_satisfies(version, node_range) is ok


def test_unparseable_range_is_not_a_verdict() -> None:
    assert core.node_satisfies("24.20.0", "latest") is None


def test_check_tree_pins_matches_the_api_package_json(tmp_path: Path) -> None:
    tree = _tree(
        tmp_path,
        {"engines": {"node": ">=24.19.0 <25"}, "packageManager": "pnpm@11.19.0+sha512.abc"},
    )
    assert core.check_tree_pins(tree, "24.20.0", "11.19.0") == []
    problems = core.check_tree_pins(tree, "25.1.0", "11.18.0")
    assert any("engines.node" in p and "25.1.0" in p for p in problems)
    assert any("packageManager" in p and "11.18.0" in p for p in problems)


def test_check_tree_pins_refuses_another_package_manager(tmp_path: Path) -> None:
    tree = _tree(tmp_path, {"packageManager": "yarn@4.1.0"})
    assert core.check_tree_pins(tree, "24.20.0", "11.19.0")


def test_check_tree_pins_without_pins_or_package_json(tmp_path: Path) -> None:
    assert core.check_tree_pins(tmp_path, "24.20.0", "11.19.0") == []
    assert core.check_tree_pins(_tree(tmp_path, {"name": "x"}), "24.20.0", "11.19.0") == []


def test_a_tree_pinning_another_node_is_not_staged(world: World) -> None:  # noqa: F811
    w = world
    first_release(w)
    real = w.mirror.stage_plain

    def stage_plain(sha: str, dest: Path, **kw: object) -> Path:
        tree = real(sha, dest)
        (tree / "package.json").write_text(
            json.dumps({"engines": {"node": ">=26"}, "packageManager": "pnpm@11.19.0"})
        )
        return tree

    w.mirror.stage_plain = stage_plain  # type: ignore[method-assign]
    w.mirror.head = SHA_B
    w.mirror.diffs[(SHA_A, SHA_B)] = ["package.json"]
    installs = len(w.provisioned)
    rep = w.tick()
    assert rep.exit_code == 1
    assert w.kinds() == ["core_stage_failed"]
    cause = w.escalations()[0]["event"]["cause"]
    assert "engines.node" in cause and "24.20.0" in cause
    assert len(w.provisioned) == installs  # nothing installed with the wrong toolchain
    assert w.record()["release_sha"] == SHA_A
