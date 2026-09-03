"""Static, portable checks on the pool runner asset (PLAN-pool.md §4, T2).

Nothing here runs the runner's real ``main()`` -- that needs a real uvicorn
and a real member app, and is exercised on the target host by
``tests/linux/test_pool_runner_live.py``. This module checks the two
properties that must hold everywhere:

1. the asset stays outside the ``ams`` package's import graph (``ams`` must
   never import fastapi/uvicorn -- CLAUDE.md), and it targets 3.12;
2. its pure logic (pool.json validation, the identity-env swap, the admin
   ASGI app) is correct, exercised by loading the file directly with
   ``uvicorn`` stubbed in ``sys.modules`` so the module-level ``import
   uvicorn`` succeeds without the real package installed.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
ASSET_PATH = SRC_ROOT / "ams" / "platform" / "assets" / "pool_runner.py"

# --------------------------------------------------------------------------- structural


def test_no_assets_init_file() -> None:
    """``assets/`` is deliberately not a Python package (PLAN-pool.md §4.1)."""
    assert not (ASSET_PATH.parent / "__init__.py").exists()


def test_asset_parses_under_feature_version_3_12() -> None:
    source = ASSET_PATH.read_text(encoding="utf-8")
    ast.parse(source, filename=str(ASSET_PATH), feature_version=(3, 12))


def test_asset_imports_nothing_from_ams() -> None:
    tree = ast.parse(ASSET_PATH.read_text(encoding="utf-8"), filename=str(ASSET_PATH))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.split(".")[0] == "ams", alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.module is not None:
                assert node.module.split(".")[0] != "ams", node.module


def test_ams_package_never_imports_fastapi_or_uvicorn() -> None:
    """Walking every ``ams.*`` module must not drag fastapi/uvicorn into ``sys.modules``.

    Run as a fresh subprocess: importing ``ams`` in-process here would pollute
    ``sys.modules`` for the rest of this test session (and other tests already
    imported bits of ``ams`` before this one runs).
    """
    script = (
        "import pkgutil, importlib, sys\n"
        "import ams\n"
        "for info in pkgutil.walk_packages(ams.__path__, ams.__name__ + '.'):\n"
        # __main__ modules run their CLI's main() as a side effect of import
        # (the standard `python -m ams` shape) -- they are entry scripts, not
        # library modules, so they are excluded from this import-graph walk.
        "    if info.name.rsplit('.', 1)[-1] == '__main__':\n"
        "        continue\n"
        "    importlib.import_module(info.name)\n"
        "assert 'fastapi' not in sys.modules, sys.modules.keys()\n"
        "assert 'uvicorn' not in sys.modules, sys.modules.keys()\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(REPO_ROOT),
        env={"PYTHONPATH": str(SRC_ROOT), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout


# --------------------------------------------------------------------------- loading the asset


@pytest.fixture
def pool_runner(monkeypatch: pytest.MonkeyPatch):
    """Load the asset as a real module, with a stub ``uvicorn`` in ``sys.modules``.

    The asset does ``import uvicorn`` at module level (needed so the pinning
    check in ``main()`` can inspect the real thing at runtime); a minimal stub
    is enough here because none of the functions under test in this module
    touch ``uvicorn.Config``/``uvicorn.Server``.
    """
    stub = types.ModuleType("uvicorn")

    class _StubConfig:  # pragma: no cover - never called by these tests
        pass

    class _StubServer:  # pragma: no cover - never called by these tests
        pass

    stub.Config = _StubConfig  # type: ignore[attr-defined]
    stub.Server = _StubServer  # type: ignore[attr-defined]
    stub.__version__ = "0.52.4"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "uvicorn", stub)

    spec = importlib.util.spec_from_file_location("pool_runner_under_test", ASSET_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "pool_runner_under_test", module)
    spec.loader.exec_module(module)
    return module


def _member(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "alpha",
        "app": "alpha.main:build_app",
        "factory": True,
        "port_env": "POOL_PORT_ALPHA",
        "health_path": "/health",
        "secret_env": {},
        "env": {"SVC_NAME": "alpha", "SVC_AUDIENCE": "alpha"},
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------- pool.json validation


def test_parse_pool_doc_accepts_version_1(pool_runner: types.ModuleType) -> None:
    import json as _json

    raw = _json.dumps({"version": 1, "pool": "core", "members": [_member()]})
    doc = pool_runner.parse_pool_doc(raw)
    assert doc["version"] == 1
    assert doc["members"][0]["id"] == "alpha"


@pytest.mark.parametrize("version", [None, 0, 2, "1"])
def test_parse_pool_doc_rejects_unknown_version(
    pool_runner: types.ModuleType, version: object
) -> None:
    import json as _json

    raw = _json.dumps({"version": version, "pool": "core", "members": [_member()]})
    with pytest.raises(pool_runner.PoolConfigError, match="version"):
        pool_runner.parse_pool_doc(raw)


def test_parse_pool_doc_rejects_invalid_json(pool_runner: types.ModuleType) -> None:
    with pytest.raises(pool_runner.PoolConfigError, match="JSON"):
        pool_runner.parse_pool_doc("{not json")


def test_parse_pool_doc_rejects_no_members(pool_runner: types.ModuleType) -> None:
    import json as _json

    raw = _json.dumps({"version": 1, "pool": "core", "members": []})
    with pytest.raises(pool_runner.PoolConfigError, match="no members"):
        pool_runner.parse_pool_doc(raw)


def test_parse_pool_doc_rejects_member_missing_a_required_key(
    pool_runner: types.ModuleType,
) -> None:
    import json as _json

    member = _member()
    del member["port_env"]
    raw = _json.dumps({"version": 1, "pool": "core", "members": [member]})
    with pytest.raises(pool_runner.PoolConfigError, match="port_env"):
        pool_runner.parse_pool_doc(raw)


# --------------------------------------------------------------------------- identity env swap


def test_identity_env_restore_is_exact(
    pool_runner: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    # SVC_NAME had an ambient value before the swap (simulates a stale value
    # left by a previous member's phase, or an operator's shell) -- it must
    # come back exactly, not merely disappear.
    monkeypatch.setenv("SVC_NAME", "previous-value")
    monkeypatch.delenv("SVC_AUDIENCE", raising=False)

    with pool_runner._identity_env({"SVC_NAME": "alpha", "SVC_AUDIENCE": "alpha"}):
        import os

        assert os.environ["SVC_NAME"] == "alpha"
        assert os.environ["SVC_AUDIENCE"] == "alpha"

    import os

    assert os.environ["SVC_NAME"] == "previous-value"
    assert "SVC_AUDIENCE" not in os.environ


def test_identity_env_restore_survives_a_failure_inside(
    pool_runner: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SVC_NAME", raising=False)
    with pytest.raises(RuntimeError):
        with pool_runner._identity_env({"SVC_NAME": "alpha"}):
            raise RuntimeError("boom")
    import os

    assert "SVC_NAME" not in os.environ


def test_member_identity_overlay_applies_secret_env_renames(
    pool_runner: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("POOL_PORT_ALPHA", "21001")
    monkeypatch.setenv("SVC_SECRET__ALPHA", "s3kr3t")
    member = _member(secret_env={"SVC_SECRET": "SVC_SECRET__ALPHA"})

    overlay = pool_runner._member_identity_overlay(member)

    assert overlay["SVC_NAME"] == "alpha"
    assert overlay["SVC_AUDIENCE"] == "alpha"
    assert overlay["PORT"] == "21001"
    assert overlay["SVC_SECRET"] == "s3kr3t"
    # The secret's own mangled name never leaks into the overlay under its own key.
    assert "SVC_SECRET__ALPHA" not in overlay


def test_member_identity_overlay_excludes_non_identity_keys(
    pool_runner: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("POOL_PORT_ALPHA", "21001")
    member = _member(
        env={
            "SVC_NAME": "alpha",
            "SVC_AUDIENCE": "alpha",
            "REGISTRY_URL": "http://127.0.0.1:20100",
        }
    )
    overlay = pool_runner._member_identity_overlay(member)
    assert "REGISTRY_URL" not in overlay


def test_apply_non_identity_union_leaves_identity_keys_alone(
    pool_runner: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SVC_NAME", raising=False)
    monkeypatch.delenv("REGISTRY_URL", raising=False)
    members = [
        _member(env={"SVC_NAME": "alpha", "REGISTRY_URL": "http://x", "A_ONLY": "1"}),
        _member(id="beta", env={"SVC_NAME": "beta", "REGISTRY_URL": "http://x", "B_ONLY": "2"}),
    ]
    pool_runner._apply_non_identity_union(members)
    import os

    assert os.environ["REGISTRY_URL"] == "http://x"
    assert os.environ["A_ONLY"] == "1"
    assert os.environ["B_ONLY"] == "2"
    # Identity keys are never folded into the permanent process env.
    assert "SVC_NAME" not in os.environ


# --------------------------------------------------------------------------- admin ASGI app


async def _drive_http(app: Any, method: str, path: str) -> tuple[int, dict[str, Any]]:
    import json as _json

    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:  # pragma: no cover - not used for GET
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {"type": "http", "method": method, "path": path}
    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    body_msg = next(m for m in sent if m["type"] == "http.response.body")
    return start["status"], _json.loads(body_msg["body"])


def test_admin_app_health_ok_when_a_member_is_serving(pool_runner: types.ModuleType) -> None:
    app = pool_runner.make_admin_app("core", {"alpha": (21001, object())}, {})
    status, body = asyncio_run(_drive_http(app, "GET", "/_pool/health"))
    assert status == 200
    assert body == {"pool": "core", "ok": ["alpha"], "failed": {}}


def test_admin_app_health_503_when_no_member_is_serving(pool_runner: types.ModuleType) -> None:
    app = pool_runner.make_admin_app("core", {}, {"alpha": "build failed"})
    status, body = asyncio_run(_drive_http(app, "GET", "/_pool/health"))
    assert status == 503
    assert body == {"pool": "core", "ok": [], "failed": {"alpha": "build failed"}}


def test_admin_app_404_on_anything_else(pool_runner: types.ModuleType) -> None:
    app = pool_runner.make_admin_app("core", {"alpha": (21001, object())}, {})
    status, _body = asyncio_run(_drive_http(app, "GET", "/other"))
    assert status == 404
    status2, _body2 = asyncio_run(_drive_http(app, "POST", "/_pool/health"))
    assert status2 == 404


def asyncio_run(coro: Any) -> Any:
    import asyncio

    return asyncio.run(coro)


# --------------------------------------------------------------------------- uvicorn pin


def test_check_uvicorn_api_refuses_a_stub_missing_server_lifespan(
    pool_runner: types.ModuleType,
) -> None:
    stub = types.ModuleType("uvicorn")

    class _Config:
        def load(self) -> None: ...
        @property
        def lifespan_class(self) -> Any:
            return None

    class _Server:
        def startup(self) -> None: ...
        def main_loop(self) -> None: ...
        def shutdown(self) -> None: ...
        # deliberately missing `lifespan`

    stub.Config = _Config  # type: ignore[attr-defined]
    stub.Server = _Server  # type: ignore[attr-defined]
    stub.__version__ = "0.30.0"  # type: ignore[attr-defined]

    with pytest.raises(SystemExit) as excinfo:
        pool_runner._check_uvicorn_api(stub)
    assert excinfo.value.code == 1


def test_check_uvicorn_api_accepts_a_complete_stub(pool_runner: types.ModuleType) -> None:
    stub = types.ModuleType("uvicorn")

    class _Config:
        def load(self) -> None: ...
        @property
        def lifespan_class(self) -> Any:
            return None

    class _Server:
        lifespan = None

        def startup(self) -> None: ...
        def main_loop(self) -> None: ...
        def shutdown(self) -> None: ...

    stub.Config = _Config  # type: ignore[attr-defined]
    stub.Server = _Server  # type: ignore[attr-defined]
    stub.__version__ = "0.52.4"  # type: ignore[attr-defined]

    pool_runner._check_uvicorn_api(stub)  # must not raise
