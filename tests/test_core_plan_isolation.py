"""core_plan.mjs and its planner: one plugin's stray error must not cost the others.

Review 2026-09-29, correctness-5. ``buildArtifact`` evaluates every plugin bundle
in the planner's own process (``exportedManifest``), so a bundle whose module
scope rejects a promise or throws from a timer used to kill ``core_plan.mjs``
(Node's default ``--unhandled-rejections=throw``), and the planner threw away the
lines the other plugins had already printed. A module-scope ``setInterval`` kept
it alive until ``PLAN_TIMEOUT_S``.

The node half runs the real asset against a *fake* ``scripts/build-artifact.mjs``
(no api checkout needed, any Node >= 18) and skips without a node binary.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from ams.platform import core, coresync
from ams.state import StateDir
from ams.userns import AdminResult

ASSET = Path(coresync.__file__).resolve().parent / "assets" / "core_plan.mjs"


def _any_node() -> str | None:
    nvm = Path.home() / ".nvm" / "versions" / "node"
    found = sorted(nvm.glob("v*/bin/node"), reverse=True) if nvm.is_dir() else []
    for c in [*map(str, found), shutil.which("node") or ""]:
        if c and Path(c).is_file():
            return c
    return None


NODE = _any_node()

FAKE_BUILDER = r"""
import { writeFile, mkdir } from 'node:fs/promises';
import { join } from 'node:path';
export const canonical = value => JSON.stringify(value);
export async function buildArtifact(dir, { outDir }) {
  const pluginId = dir.split('/').pop();
  // What exportedManifest does: evaluate the bundle's module scope in-process.
  if (pluginId === 'rejects') Promise.reject(new Error('module-scope readFile failed'));
  if (pluginId === 'throws') setTimeout(() => { throw new Error('timer threw'); }, 0);
  if (pluginId === 'ticks') setInterval(() => {}, 1000);
  await new Promise(ok => setTimeout(ok, 20));
  await mkdir(outDir, { recursive: true });
  const file = { bundle: pluginId, manifest: { pluginId }, docs: '', sources: '', buildInfo: {} };
  const path = join(outDir, `${pluginId}.artifact.json`);
  await writeFile(path, JSON.stringify(file));
  return { artifactId: 'a'.repeat(64), pluginId, path, commit: null, dirty: false, file };
}
"""


def _tree(tmp_path: Path, ids: Sequence[str]) -> Path:
    tree = tmp_path / "tree"
    (tree / "scripts").mkdir(parents=True)
    (tree / "scripts" / "build-artifact.mjs").write_text(FAKE_BUILDER, encoding="utf-8")
    for pid in ids:
        (tree / "plugins" / pid).mkdir(parents=True)
    return tree


@pytest.mark.skipif(NODE is None, reason="needs a node binary")
def test_a_stray_async_error_is_pinned_on_its_plugin_and_the_run_finishes(
    tmp_path: Path,
) -> None:
    ids = ["good", "rejects", "throws", "ticks", "after"]
    tree = _tree(tmp_path, ids)
    assert NODE is not None
    proc = subprocess.run(
        [NODE, str(ASSET), str(tree), str(tmp_path / "out"), *ids],
        capture_output=True,
        text=True,
        timeout=30,  # a module-scope setInterval must not keep it alive
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    plans = {p.plugin_id: p for p in coresync.parse_plan_output(proc.stdout)}
    assert set(plans) == set(ids), proc.stdout
    assert plans["good"].error is None and plans["after"].error is None
    assert plans["ticks"].error is None
    assert plans["rejects"].error and "module-scope readFile failed" in plans["rejects"].error
    assert plans["throws"].error and "timer threw" in plans["throws"].error


class _Runner:
    def __init__(self, rc: int, stdout: str, stderr: str = "") -> None:
        self.rc, self.stdout, self.stderr = rc, stdout, stderr

    def __call__(
        self, argv: Sequence[str], *, env: Mapping[str, str], cwd: str | None, timeout_s: float
    ) -> AdminResult:
        return AdminResult(tuple(argv), self.rc, self.stdout.encode(), self.stderr.encode())


def _line(pid: str) -> str:
    return json.dumps(
        {
            "pluginId": pid,
            "dir": f"/t/plugins/{pid}",
            "artifactId": "1" * 64,
            "contentKey": "2" * 64,
            "path": f"/o/{pid}.json",
            "commit": None,
            "dirty": False,
        }
    )


def test_planner_keeps_complete_lines_when_node_dies(tmp_path: Path) -> None:
    state = StateDir(tmp_path / "s")
    layout = core.CoreLayout.from_state(state)
    core.ensure_layout(layout, None)
    planner = coresync.make_planner(state, layout, None)
    out = _line("secrets") + "\n" + _line("store") + '\n{"pluginId": "gatew'
    plans = planner(
        tmp_path,
        layout.artifacts_dir("a" * 40),
        ["secrets", "store", "gateway"],
        runner=_Runner(1, out, "Error: boom\n    at gateway/index.ts"),
        env={"PATH": "/usr/bin:/bin"},
    )
    assert [p.plugin_id for p in plans] == ["secrets", "store"]
    assert all(p.error is None for p in plans)


def test_planner_with_no_output_at_all_is_still_a_plan_failure(tmp_path: Path) -> None:
    state = StateDir(tmp_path / "s")
    layout = core.CoreLayout.from_state(state)
    core.ensure_layout(layout, None)
    planner = coresync.make_planner(state, layout, None)
    with pytest.raises(coresync.CorePlanError, match="exited 3"):
        planner(
            tmp_path,
            layout.artifacts_dir("a" * 40),
            ["secrets"],
            runner=_Runner(3, "", "core_plan: cannot import build-artifact.mjs"),
            env={"PATH": "/usr/bin:/bin"},
        )


# ------------------------------------------------ security-1: planner placement


def test_isolated_planner_places_its_asset_as_the_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """build/ is service-owned and the service can plant symlinks in it while core
    runs, so the planner must never mkdir/cp/chown there as inner root (the harness
    uid): every write goes through the runner, i.e. as the service itself."""
    from ams import userns
    from ams.uidmap import UidBlock

    def no_admin(*a: object, **k: object) -> AdminResult:
        raise AssertionError(f"run_admin called: {a}")

    monkeypatch.setattr(userns, "run_admin", no_admin)
    state = StateDir(tmp_path / "s")
    layout = core.CoreLayout.from_state(state)
    state.service_dir(core.CORE_ID).mkdir(parents=True)
    calls: list[list[str]] = []
    copied: dict[str, bytes] = {}

    def runner(
        argv: Sequence[str], *, env: Mapping[str, str], cwd: str | None, timeout_s: float
    ) -> AdminResult:
        calls.append(list(argv))
        if argv[0] == "cp":
            copied[argv[-1]] = Path(argv[-2]).read_bytes()
        out = _line("secrets") if argv[0] == "node" else ""
        return AdminResult(tuple(argv), 0, out.encode(), b"")

    planner = coresync.make_planner(state, layout, UidBlock(100_000, 100_000, 1024))
    out_dir = layout.artifacts_dir("a" * 40)
    plans = planner(tmp_path, out_dir, ["secrets"], runner=runner, env={"PATH": "/usr/bin:/bin"})
    assert [p.plugin_id for p in plans] == ["secrets"]
    asset = str(layout.build / "core_plan.mjs")
    assert calls[0] == ["mkdir", "-p", str(out_dir)]
    assert calls[1][0] == "cp" and calls[1][-1] == asset
    assert copied[asset] == ASSET.read_bytes()
    assert calls[2][:2] == ["node", asset]
    assert not any(c[0] == "chown" for c in calls)
    # the harness-side staging copy is gone
    assert not list(state.service_dir(core.CORE_ID).glob(".core_plan.mjs.tmp*"))


def test_isolated_planner_refuses_to_run_when_placement_fails(tmp_path: Path) -> None:
    from ams.uidmap import UidBlock

    state = StateDir(tmp_path / "s")
    layout = core.CoreLayout.from_state(state)
    state.service_dir(core.CORE_ID).mkdir(parents=True)

    def runner(
        argv: Sequence[str], *, env: Mapping[str, str], cwd: str | None, timeout_s: float
    ) -> AdminResult:
        rc = 1 if argv[0] == "cp" else 0
        return AdminResult(tuple(argv), rc, b"", b"cp: Permission denied")

    planner = coresync.make_planner(state, layout, UidBlock(100_000, 100_000, 1024))
    with pytest.raises(coresync.CorePlanError, match="Permission denied"):
        planner(
            tmp_path,
            layout.artifacts_dir("a" * 40),
            ["secrets"],
            runner=runner,
            env={"PATH": "/usr/bin:/bin"},
        )
