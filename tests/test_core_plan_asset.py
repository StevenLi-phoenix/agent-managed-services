"""assets/core_plan.mjs: package data that builds plugin artifacts with content keys.

The live half runs the asset with a real Node 24 against the real ``../api``
checkout (read only: artifacts go to tmp_path) and skips when either is missing.
It pins the one property ams's change detection rests on: ``contentKey`` is
``computeArtifactId`` minus ``buildInfo``, so it is stable across commits and
tree locations while ``artifactId`` is not.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from ams.platform import core, coresync
from ams.platform.corectl import plain_runner

ASSET = Path(coresync.__file__).resolve().parent / "assets" / "core_plan.mjs"
API = Path(__file__).resolve().parents[2] / "api"


def _node24() -> Path | None:
    candidates = [os.environ.get("AMS_TEST_NODE24", "")]
    nvm = Path.home() / ".nvm" / "versions" / "node"
    if nvm.is_dir():
        candidates += [str(p / "bin" / "node") for p in sorted(nvm.glob("v24.*"), reverse=True)]
    candidates.append(shutil.which("node") or "")
    for c in candidates:
        if not c or not Path(c).is_file():
            continue
        try:
            out = subprocess.run([c, "--version"], capture_output=True, text=True, timeout=10)
        except OSError:
            continue
        major, _, rest = out.stdout.strip().lstrip("v").partition(".")
        minor = rest.partition(".")[0]
        if major == "24" and minor.isdigit() and int(minor) >= 19:
            return Path(c)
    return None


NODE = _node24()
LIVE = pytest.mark.skipif(
    NODE is None
    or not (API / "scripts" / "build-artifact.mjs").is_file()
    or not (API / "node_modules" / "esbuild").exists(),
    reason="needs Node >=24.19 <25 and an installed ../api checkout",
)


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# ------------------------------------------------------------------ static


def test_asset_ships_as_package_data() -> None:
    assert ASSET.is_file()
    assert coresync.core_plan_source() == ASSET


def test_asset_formula_matches_compute_artifact_id_minus_build_info() -> None:
    text = ASSET.read_text(encoding="utf-8")
    assert "[file.bundle, canonical(file.manifest), file.docs, file.sources]" in text
    assert "hash.update('\\0')" in text


def test_no_ams_module_references_the_asset_as_code() -> None:
    src = Path(coresync.__file__).resolve().parents[1]
    for py in src.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        assert "import core_plan" not in text, py


# -------------------------------------------------------------------- live


def _run(tree: Path, out: Path, *ids: str, commit: str | None = None) -> list[dict]:
    assert NODE is not None
    env = {"PATH": f"{NODE.parent}:/usr/bin:/bin", "LANG": "C.UTF-8"}
    if commit:
        env["CORE_SOURCE_COMMIT"] = commit
    proc = subprocess.run(
        [str(NODE), str(ASSET), str(tree), str(out), *ids],
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


@LIVE
def test_live_plan_against_the_api_checkout(tmp_path: Path) -> None:
    lines = _run(API, tmp_path, "timeservice", "health", "no-such-plugin", "Bad_Id")
    by_id = {line["pluginId"]: line for line in lines}
    assert list(by_id) == ["timeservice", "health", "no-such-plugin", "Bad_Id"]
    assert "error" in by_id["no-such-plugin"] and "error" in by_id["Bad_Id"]
    for pid in ("timeservice", "health"):
        line = by_id[pid]
        assert set(line) == {
            "pluginId",
            "dir",
            "artifactId",
            "contentKey",
            "path",
            "commit",
            "dirty",
        }
        assert line["artifactId"] != line["contentKey"]
        file = json.loads(Path(line["path"]).read_text(encoding="utf-8"))
        parts = [file["bundle"], _canonical(file["manifest"]), file["docs"], file["sources"]]
        assert hashlib.sha256("\0".join(parts).encode()).hexdigest() == line["contentKey"]
        full = [*parts, _canonical(file["buildInfo"])]
        assert hashlib.sha256("\0".join(full).encode()).hexdigest() == line["artifactId"]


@LIVE
def test_live_content_key_ignores_the_commit_artifact_id_does_not(tmp_path: Path) -> None:
    # The api tree itself is a git checkout, so CORE_SOURCE_COMMIT is ignored
    # there; a git-archive copy is what staging produces. Copy only what the
    # timeservice build reads, then point node_modules at the real install.
    tree = tmp_path / "tree"
    subprocess.run(
        [
            "git",
            "-C",
            str(API),
            "archive",
            "--format=tar",
            "-o",
            str(tmp_path / "t.tar"),
            "HEAD",
            "scripts",
            "plugins/timeservice",
            "packages",
            "package.json",
        ],
        check=True,
        timeout=120,
    )
    tree.mkdir()
    subprocess.run(["tar", "-xf", str(tmp_path / "t.tar"), "-C", str(tree)], check=True)
    (tree / "node_modules").symlink_to(API / "node_modules")
    one = _run(tree, tmp_path / "o1", "timeservice", commit="1" * 40)[0]
    two = _run(tree, tmp_path / "o2", "timeservice", commit="2" * 40)[0]
    assert "error" not in one, one
    assert one["commit"] == "1" * 40 and one["dirty"] is False
    assert one["contentKey"] == two["contentKey"]
    assert one["artifactId"] != two["artifactId"]


@LIVE
def test_live_make_planner_plain_mode(tmp_path: Path) -> None:
    """The coresync planner end to end: asset placement, plain runner, parsing."""
    from ams.state import StateDir

    state = StateDir(tmp_path / "s")
    layout = core.CoreLayout.from_state(state)
    core.ensure_layout(layout, None)
    planner = coresync.make_planner(state, layout, None)
    assert NODE is not None
    plans = planner(
        API,
        layout.artifacts_dir("a" * 40),
        ["timeservice"],
        runner=plain_runner(),
        env={"PATH": f"{NODE.parent}:/usr/bin:/bin", "LANG": "C.UTF-8"},
    )
    assert [p.plugin_id for p in plans] == ["timeservice"]
    assert plans[0].error is None
    assert Path(plans[0].path).is_file()
    assert (layout.build / "core_plan.mjs").read_bytes() == ASSET.read_bytes()
