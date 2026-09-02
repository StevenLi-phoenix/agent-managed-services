"""Portable half of the runtime layer: everything that is pure path/env logic.

The provisioning itself needs Linux user namespaces and lives in
``tests/linux/test_runtime.py``. What is testable anywhere is the part the
supervisor depends on every time it starts a service (``runtime_env``), and the
part a wrong value would silently break (``provisioning_env``: a missing
``UV_LINK_MODE=clone`` degrades every venv to a full copy without an error).
"""

from __future__ import annotations

import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from ams import schema
from ams.runtime import (
    ProvisionError,
    RuntimeEnv,
    RuntimeStore,
    make_extra_env_for,
    provision,
    provisioning_env,
    python_venv_dir,
    runtime_env,
    service_workdir,
    venv_dir,
)
from ams.state import StateDir
from ams.uidmap import UidBlock

BLOCK = UidBlock(100_000, 100_000, 1024)
STORE = RuntimeStore(Path("/srv/store"), bun_install=Path("/opt/bun"))
ROOT = Path("/state/services/svc/root")


def decl(kind: str, *, workdir: str = ".", **runtime: Any) -> schema.ServiceDecl:
    return schema.from_dict(
        {
            "id": "svc",
            "start": {"argv": ["true"], "workdir": workdir},
            "runtime": {"kind": kind, **runtime},
        }
    )


# --------------------------------------------------------------------------- store


def test_store_paths_all_under_root() -> None:
    store = RuntimeStore(Path("/srv/store"))
    assert store.uv_cache == Path("/srv/store/uv-cache")
    assert store.python_dir == Path("/srv/store/python")
    assert store.pnpm_store == Path("/srv/store/pnpm-store")
    assert store.pnpm_home == Path("/srv/store/pnpm-home")
    assert store.bun_cache == Path("/srv/store/bun-cache")
    # bun installs itself outside the store; only the cache is ours to place.
    assert store.bun_install == Path.home() / ".bun"


def test_store_from_env_prefers_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AMS_STORE_DIR", "/from/env")
    assert RuntimeStore.from_env(Path("/from/default")).root == Path("/from/env")


def test_store_from_env_falls_back_to_default_then_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AMS_STORE_DIR", raising=False)
    assert RuntimeStore.from_env(Path("/from/default")).root == Path("/from/default")
    assert RuntimeStore.from_env().root == Path.home() / "store"


def test_store_ensure_creates_caches_world_traversable(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "store")
    store.ensure()
    store.ensure()  # idempotent
    for d in (store.root, store.uv_cache, store.python_dir, store.pnpm_store, store.bun_cache):
        assert d.is_dir()
        # Service uids must be able to traverse into the caches from the ns.
        assert stat.S_IMODE(d.stat().st_mode) == 0o755


# --------------------------------------------------------------------------- runtime_env


def test_runtime_env_none_is_empty() -> None:
    env = runtime_env(decl("none"), ROOT, STORE)
    assert env == RuntimeEnv()
    assert env.as_tuple() == ({}, ())


@pytest.mark.parametrize("kind", ["venv", "uv"])
def test_runtime_env_python_activates_the_venv(kind: str) -> None:
    env = runtime_env(decl(kind), ROOT, STORE)
    assert env.extra_env == {"VIRTUAL_ENV": "/state/services/svc/root/.venv"}
    assert env.path_prepend == ("/state/services/svc/root/.venv/bin",)


def test_runtime_env_pnpm() -> None:
    env = runtime_env(decl("pnpm"), ROOT, STORE)
    assert env.extra_env == {
        "PNPM_HOME": "/srv/store/pnpm-home",
        "npm_config_store_dir": "/srv/store/pnpm-store",
    }
    assert env.path_prepend == (
        "/state/services/svc/root/node_modules/.bin",
        "/srv/store/pnpm-home/bin",
    )


def test_runtime_env_bun() -> None:
    env = runtime_env(decl("bun"), ROOT, STORE)
    assert env.extra_env == {"BUN_INSTALL": "/opt/bun"}
    assert env.path_prepend == ("/state/services/svc/root/node_modules/.bin", "/opt/bun/bin")


def test_runtime_env_node_modules_follows_workdir() -> None:
    env = runtime_env(decl("pnpm", workdir="app"), ROOT, STORE)
    assert env.path_prepend[0] == "/state/services/svc/root/app/node_modules/.bin"
    env = runtime_env(decl("bun", workdir="/opt/app"), ROOT, STORE)
    assert env.path_prepend[0] == "/opt/app/node_modules/.bin"


def test_runtime_env_nix_is_not_implemented() -> None:
    with pytest.raises(NotImplementedError, match="nix runtime not implemented"):
        runtime_env(decl("nix", nix_packages=["hello"]), ROOT, STORE)


def test_service_workdir_matches_spawn_request_rule() -> None:
    from ams.spawn import SpawnRequest

    for workdir in (".", "app", "/opt/app"):
        d = decl("none", workdir=workdir)
        assert service_workdir(d, ROOT) == SpawnRequest(d, ROOT, {}).workdir


def test_venv_dir() -> None:
    assert venv_dir(ROOT) == ROOT / ".venv"


# --------------------------------------------------------------------------- uv sync mode


def test_runtime_env_sync_puts_the_venv_in_the_project() -> None:
    """`uv sync` creates <workdir>/.venv, so that is what the service must find.

    The declaration this mirrors is the api pilot: a whole monorepo copied to
    the service root, the service being one project inside it.
    """
    d = decl("uv", workdir="repo/services/kvservice", sync=True, python="3.12")
    venv = "/state/services/svc/root/repo/services/kvservice/.venv"
    env = runtime_env(d, ROOT, STORE)
    assert env.extra_env == {"VIRTUAL_ENV": venv}
    assert env.path_prepend == (venv + "/bin",)
    assert python_venv_dir(d, ROOT) == Path(venv)


def test_runtime_env_sync_with_root_workdir_matches_the_default_layout() -> None:
    """workdir="." is the one case where both layouts name the same directory."""
    d = decl("uv", sync=True)
    assert python_venv_dir(d, ROOT) == venv_dir(ROOT) == ROOT / ".venv"
    assert runtime_env(d, ROOT, STORE) == runtime_env(decl("uv"), ROOT, STORE)


def test_runtime_env_sync_honours_an_absolute_workdir() -> None:
    d = decl("uv", workdir="/opt/app", sync=True)
    assert python_venv_dir(d, ROOT) == Path("/opt/app/.venv")


def test_python_venv_dir_without_sync_ignores_the_workdir() -> None:
    """Without sync the environment belongs to the root, not to the workdir."""
    assert python_venv_dir(decl("uv", workdir="app"), ROOT) == ROOT / ".venv"
    assert python_venv_dir(decl("venv", workdir="app"), ROOT) == ROOT / ".venv"


def test_make_extra_env_for_follows_sync_mode(tmp_path: Path) -> None:
    """The supervisor's lookup and the provisioner must agree on the venv path."""
    state = StateDir(tmp_path)
    lookup = make_extra_env_for(state, STORE)
    extra_env, path_prepend = lookup(decl("uv", workdir="repo/apps/timeservice", sync=True))
    venv = state.service_root("svc") / "repo" / "apps" / "timeservice" / ".venv"
    assert extra_env == {"VIRTUAL_ENV": str(venv)}
    assert path_prepend == (str(venv / "bin"),)


def test_provision_refuses_packages_in_sync_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`uv add` would rewrite the copied repository's own pyproject/uv.lock."""
    monkeypatch.setattr(sys, "platform", "linux")
    d = decl("uv", workdir="proj", sync=True, packages=["six"])
    with pytest.raises(ProvisionError, match="runtime.packages must be empty"):
        provision(d, tmp_path / "root", STORE, BLOCK)
    assert not (tmp_path / "root").exists()  # refused before touching the disk


def test_schema_allows_sync_only_for_uv_projects() -> None:
    """The runtime restriction is ours; these two are the schema's."""
    with pytest.raises(schema.DeclError, match="runtime.sync"):
        decl("venv", sync=True)
    with pytest.raises(schema.DeclError, match="runtime.sync"):
        decl("uv", sync=True, requirements="requirements.txt")


# --------------------------------------------------------------------------- provisioning env


def test_provisioning_env_pins_clone_link_modes() -> None:
    env = provisioning_env(STORE)
    assert env["UV_CACHE_DIR"] == "/srv/store/uv-cache"
    assert env["UV_PYTHON_INSTALL_DIR"] == "/srv/store/python"
    # Without these three the store's reflink sharing silently becomes copying.
    assert env["UV_LINK_MODE"] == "clone"
    assert env["npm_config_package_import_method"] == "clone"
    assert env["npm_config_store_dir"] == "/srv/store/pnpm-store"
    assert env["UV_PYTHON_PREFERENCE"] == "only-managed"
    assert env["PNPM_HOME"] == "/srv/store/pnpm-home"
    assert env["BUN_INSTALL"] == "/opt/bun"
    assert env["BUN_INSTALL_CACHE_DIR"] == "/srv/store/bun-cache"
    assert env["HOME"] == str(Path.home())
    assert env["CI"] == "1"


def test_provisioning_env_path_finds_every_tool() -> None:
    parts = provisioning_env(STORE)["PATH"].split(":")
    assert parts[:3] == [
        str(Path.home() / ".local" / "bin"),  # uv
        "/srv/store/pnpm-home/bin",  # pnpm
        "/opt/bun/bin",  # bun
    ]
    assert parts[3:] == ["/usr/local/bin", "/usr/bin", "/bin"]


# --------------------------------------------------------------------------- provision guard


def test_provision_refuses_off_linux(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    with pytest.raises(ProvisionError, match="needs Linux"):
        provision(decl("uv", packages=["six"]), tmp_path / "root", STORE, BLOCK)
    assert not (tmp_path / "root").exists()  # refused before touching the disk


def test_provision_refuses_nix(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(ProvisionError, match="nix runtime not implemented"):
        provision(decl("nix", nix_packages=["hello"]), tmp_path / "root", STORE, BLOCK)
    assert not (tmp_path / "root").exists()


def test_provision_kind_none_is_a_no_op(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    assert provision(decl("none"), tmp_path / "root", STORE, BLOCK) == RuntimeEnv()
    assert not (tmp_path / "root").exists()


# --------------------------------------------------------------------------- supervisor hook


def test_make_extra_env_for_returns_supervisor_shape(tmp_path: Path) -> None:
    state = StateDir(tmp_path)
    lookup = make_extra_env_for(state, STORE)
    extra_env, path_prepend = lookup(decl("uv"))
    venv = state.service_root("svc") / ".venv"
    assert isinstance(extra_env, dict) and isinstance(path_prepend, tuple)
    assert extra_env == {"VIRTUAL_ENV": str(venv)}
    assert path_prepend == (str(venv / "bin"),)
    assert lookup(decl("none")) == ({}, ())


def test_make_extra_env_for_does_not_provision(tmp_path: Path) -> None:
    """A start must never block on the network: the lookup touches no disk."""
    lookup = make_extra_env_for(StateDir(tmp_path), STORE)
    lookup(decl("uv", packages=["six"]))
    assert list(tmp_path.iterdir()) == []
