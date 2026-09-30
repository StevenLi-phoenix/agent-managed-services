"""Core mode never lets the service steer a privileged file operation (review 2026-09-29).

The service owns ``<state>/services/core/root`` and core runs as that uid while a
tick works, so anything under the root can be swapped for a symlink at any time.
Inner root in the admin namespace *is* the harness uid (with CAP_CHOWN/DAC over
the block), so a following ``chown``/``chmod -R``/``cp``/``ln`` there reaches
harness files:

- security-2: ``ensure_layout`` creates missing directories **as the service** and
  refuses a symlinked layout entry instead of chowning through it;
- security-6: ``place_bundle`` builds the new ``etc`` and ``flip_current`` builds the
  new link in the harness-owned ``<state>/services/core/`` (the service cannot plant
  anything there), then renames them into the root -- ``rename(2)`` never follows a
  symlink at its destination.

``FakeAdmin`` from the integration suite plays the admin namespace locally.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from test_platform_core_integration import BLOCK, CONFIG, FakeAdmin

from ams.platform import core
from ams.platform.core import CoreLayout, loads_config
from ams.state import StateDir
from ams.userns import AdminResult


@pytest.fixture
def state() -> Iterator[StateDir]:
    root = Path(tempfile.mkdtemp(prefix="ams-ch-", dir="/tmp"))
    try:
        yield StateDir(root / "s")
    finally:
        shutil.rmtree(root, ignore_errors=True)


class Recorder(FakeAdmin):
    """``FakeAdmin`` for the as-service path: same file semantics, own call log."""


def _no_admin(argv: Sequence[str], block: Any) -> None:
    raise AssertionError(f"inner-root op on the service root: {list(argv)}")


# ------------------------------------------------------------------ security-2


def test_isolated_layout_is_created_as_the_service_with_no_chown(
    state: StateDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = Recorder()
    monkeypatch.setattr(core, "_admin", _no_admin)
    monkeypatch.setattr(core, "_as_service", lambda argv, block: svc(argv, block).check())
    lay = CoreLayout.from_state(state)
    lay.root.mkdir(parents=True)
    core.ensure_layout(lay, BLOCK)
    assert svc.modes() == {
        "releases": "755",
        "data": "750",
        "run": "750",
        "build": "750",
        "etc": "700",
    }
    assert not any(c[0] == "chown" for c in svc.calls)
    svc.calls.clear()
    core.ensure_layout(lay, BLOCK)  # everything exists: no fork at all
    assert svc.calls == []


@pytest.mark.parametrize("entry", ["releases", "data", "run", "build", "etc"])
def test_a_symlinked_layout_entry_is_refused(
    state: StateDir, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    svc = Recorder()
    monkeypatch.setattr(core, "_admin", _no_admin)
    monkeypatch.setattr(core, "_as_service", lambda argv, block: svc(argv, block).check())
    lay = CoreLayout.from_state(state)
    lay.root.mkdir(parents=True)
    outside = state.root.parent / "harness-home"
    outside.mkdir()
    (lay.root / entry).symlink_to(outside)
    with pytest.raises(core.CoreLayoutError, match=entry):
        core.ensure_layout(lay, BLOCK)
    assert svc.calls == []


# ------------------------------------------------------------------ security-6


def _import_bundle(state: StateDir) -> None:
    cfg = loads_config(CONFIG.format(host="api.example.test"), state=state)
    bundle = state.root.parent / "plugins.json"
    bundle.write_text(
        json.dumps({"plugins": {"gateway": {"config": {"port": 18080}}}}), encoding="utf-8"
    )
    core.import_bundle(state, bundle, cfg=cfg)


def _operands(calls: list[list[str]], tool: str) -> list[str]:
    return [a for c in calls if c[0] == tool for a in c[1:] if a.startswith("/")]


def test_place_bundle_stages_etc_outside_the_service_root(
    state: StateDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = FakeAdmin()
    monkeypatch.setattr(core, "_admin", lambda argv, block: admin(argv, block).check())
    lay = CoreLayout.from_state(state)
    lay.etc.mkdir(parents=True)
    _import_bundle(state)
    assert core.place_bundle(state, lay, BLOCK) is True
    assert json.loads((lay.etc / "plugins.json").read_text())["plugins"]["gateway"]
    root = str(lay.root) + "/"
    # nothing the service can swap is ever copied into, chmod'ed or chown'ed
    for tool in ("cp", "chmod", "chown"):
        for path in _operands(admin.calls, tool):
            assert (
                not path.startswith(root)
                or tool == "cp"
                and path.startswith(str(core.bundle_master_dir(state)))
            ), (tool, path)
    chmod = next(c for c in admin.calls if c[0] == "chmod")
    assert chmod[-1] == str(lay.root.parent / ".etc.stage")
    # the chmod happens while the staged copy is still harness-owned (before chown)
    tools = [c[0] for c in admin.calls]
    assert tools.index("chmod") < tools.index("chown")
    assert ["mv", "-T", str(lay.root.parent / ".etc.stage"), str(lay.etc)] in admin.calls
    assert not (lay.root.parent / ".etc.stage").exists()
    assert not (lay.root / ".etc.old").exists()


def test_flip_current_builds_the_link_outside_the_service_root(
    state: StateDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = FakeAdmin()
    monkeypatch.setattr(core, "_admin", lambda argv, block: admin(argv, block).check())
    lay = CoreLayout.from_state(state)
    sha = "a" * 40
    lay.release_dir(sha).mkdir(parents=True)
    # a directory planted where the old code created its temporary link
    (lay.root / ".current.new").mkdir()
    core.flip_current(lay, sha, BLOCK)
    assert core.current_sha(lay) == sha
    tmp = str(lay.root.parent / ".current.new")
    ln = next(c for c in admin.calls if c[0] == "ln")
    assert ln[-1] == tmp
    assert ["mv", "-T", tmp, str(lay.current)] in admin.calls
    assert list((lay.root / ".current.new").iterdir()) == []  # nothing created inside it


def test_admin_result_type_is_what_the_fakes_return() -> None:
    assert AdminResult(("x",), 0, b"", b"").ok
