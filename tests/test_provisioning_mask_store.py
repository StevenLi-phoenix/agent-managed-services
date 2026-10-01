"""The admin-namespace masks cover harness-private paths outside ``<state>`` too.

Review 2026-09-29, security-5. The platform RS256 signing key lives in
``<store>/platform/jwt-rs256.pem`` -- a *sibling* of the state dir on the target
host (``/home/harness/store/{state,platform}``) -- and the private api mirror in
``<store>/{upstream,repos}``; the harness home holds ``~/.ssh``, ``~/.npmrc`` and
friends. `ams provision` (uv/venv/pnpm/bun runtimes) still runs untrusted code as
inner root (the harness uid) under the admin map, so its mask must hide those too,
while leaving the caches and toolchain the tools need (and the tree they work in)
visible. Core mode no longer needs a mask: it runs those steps as the service.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ams import runtime
from ams.runtime import RuntimeStore
from ams.state import StateDir

PRIVATE = ("platform", "upstream", "repos")


def _layout(tmp_path: Path) -> tuple[RuntimeStore, StateDir, Path, Path]:
    home = tmp_path / "home"
    store = RuntimeStore(home / "store")
    state = StateDir(home / "store" / "state")
    root = state.root / "services" / "web" / "root"
    root.mkdir(parents=True)
    return store, state, root, home


def test_provisioning_mask_hides_store_private_dirs_and_home_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, state, root, home = _layout(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    mask = runtime.provisioning_mask(root, store=store)
    for name in PRIVATE:
        assert store.root / name in mask, name
    assert home / ".ssh" in mask and home / ".npmrc" in mask
    # what the tools need stays visible
    for keep in (store.uv_cache, store.pnpm_store, store.node_dir, store.python_dir, root):
        assert not any(m == keep or m in keep.parents for m in mask), keep
    # never an ancestor of the state dir or the store (that would hide everything)
    assert not any(m in state.root.parents or m == state.root for m in mask)
    # entries stay disjoint (userns._apply_mask forbids nesting)
    for a in mask:
        assert not any(a != b and a in b.parents for b in mask), a


def test_provisioning_mask_without_a_store_is_the_state_only_mask(tmp_path: Path) -> None:
    store, state, root, home = _layout(tmp_path)
    assert runtime.provisioning_mask(root) == runtime.provisioning_mask(root, store=None)
    assert not any(store.root / name in runtime.provisioning_mask(root) for name in PRIVATE)


def test_a_home_dotdir_holding_the_store_is_never_masked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    store = RuntimeStore(home / ".config" / "ams-store")
    state = StateDir(store.root / "state")
    root = state.root / "services" / "web" / "root"
    root.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    mask = runtime.provisioning_mask(root, store=store)
    assert home / ".config" not in mask
    assert home / ".ssh" in mask
