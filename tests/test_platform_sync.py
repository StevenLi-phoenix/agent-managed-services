"""Portable tests for the one-shot platform sync loop.

Everything runs against a throwaway git repository and a threaded fake registry
in ``tmp_path``. The four calls that would touch the host -- ``SourceMirror.stage``
(forks into the admin user namespace), ``runtime.provision`` (shells out to uv),
``bootstrap.place_jwt_key`` (admin ns again) and the two control-socket clients
-- are replaced by recorders, so a run here can be asserted call for call
without a Linux host. ``translate``, ``gateway``, ``PlatformState``, the
SecretStore, the port allocator and the registry client are the real ones.

The fake reload does what the harness's reload does that matters here: it
allocates a port for every service that now has a declaration on disk. That is
what makes the two-pass ordering (reload, *then* render the gateway) testable
rather than merely asserted in a comment.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from ams.platform import sync as sync_mod
from ams.platform.sources import SHA_MARKER, SourceError, SourceMirror
from ams.platform.sync import (
    STAGES,
    PlatformState,
    ServiceRecord,
    SyncConfig,
    SyncError,
    discover_manifests,
    mounts_dir,
    registry_dir,
    state_path,
    sync,
)
from ams.runtime import RuntimeStore, python_venv_dir
from ams.schema import loads as load_decl
from ams.secrets import SecretStore
from ams.state import StateDir
from ams.uidmap import UidBlock

BLOCK = UidBlock(100_000, 100_000, 1024)
CADDY_PORT = 29999

_GIT_ENV = {
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "HOME": "/nonexistent-ams-test-home",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_AUTHOR_NAME": "ams tests",
    "GIT_AUTHOR_EMAIL": "ams@example.invalid",
    "GIT_COMMITTER_NAME": "ams tests",
    "GIT_COMMITTER_EMAIL": "ams@example.invalid",
    "LC_ALL": "C",
}


# --------------------------------------------------------------------------- manifests


def service_manifest(
    name: str,
    *,
    memory: str = "200M",
    port: int = 9200,
    manual_restart: bool = False,
    exec_line: str | None = None,
) -> str:
    exec_default = (
        f"/srv/{name}/services/{name}/.venv/bin/uvicorn {name}.main:build_app "
        "--factory --host 127.0.0.1 --port ${PORT}"
    )
    lines = [
        "schema_version: 1",
        "kind: service",
        f"name: {name}",
        f"audience: {name}",
        f"display_name: {name.title()} Service",
        "owner: steven",
    ]
    if manual_restart:
        lines.append("manual_restart: true")
    lines += [
        "",
        "deploy:",
        "  source:",
        "    type: git",
        "    repo: StevenLi-phoenix/api",
        "    branch: main",
        "  install:",
        f"    - cd services/{name} && uv sync",
        f"  target_dir: /srv/{name}",
        f"  user: {name}",
        "",
        "process:",
        f"  exec: {exec_line or exec_default}",
        f"  working_dir: /srv/{name}/services/{name}",
        "  environment:",
        f"    SVC_ROOT_PATH: /{name}",
        f"    {name.upper()}_DB_PATH: /var/lib/{name}/{name}.db",
        "  restart: on-failure",
        "  restart_sec: 5",
        f"  memory_max: {memory}",
        "",
        "mount:",
        "  gateway: api.lishuyu.app",
        f"  path: /{name}",
        f"  port: {port}",
        "",
        "acl:",
        '  - {action: read, principal: "user:*", effect: allow}',
        "  - {action: write, principal: admin}",
        "",
        "registry:",
        f"  capabilities: [{name}]",
        "  health_path: /health",
        "",
    ]
    return "\n".join(lines)


STATIC_MANIFEST = """\
schema_version: 1
kind: static
name: site

deploy:
  source:
    type: git
    repo: StevenLi-phoenix/api
    branch: main
    path: apps/site/dist
  install: []
  target_dir: /srv/site

mount:
  gateway: site.lishuyu.app
  subdomain: site
"""

BROKEN_MANIFEST = """\
schema_version: 1
kind: service
name: broken
audience: broken

deploy:
  source:
    type: git
    repo: StevenLi-phoenix/api
    branch: main
  install:
    - cd services/broken && make install
  target_dir: /srv/broken
  user: broken

process:
  exec: /srv/broken/services/broken/.venv/bin/uvicorn broken.main:app --port ${PORT}
  working_dir: /srv/broken/services/broken

mount:
  gateway: api.lishuyu.app
  path: /broken
  port: 9299

registry:
  capabilities: [broken]
"""


# --------------------------------------------------------------------------- git


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd), env=_GIT_ENV, capture_output=True)
    if proc.returncode != 0:  # pragma: no cover - a broken test fixture
        raise AssertionError(f"git {args}: {proc.stderr.decode()}")
    return proc.stdout.decode().strip()


def _commit(path: Path, files: dict[str, str], message: str) -> str:
    for rel, text in files.items():
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", message)
    return _git(path, "rev-parse", "HEAD")


# --------------------------------------------------------------------------- fakes


class Calls:
    """Ordered record of everything the run did to the host or the harness."""

    def __init__(self) -> None:
        self.items: list[tuple[Any, ...]] = []

    def add(self, *item: Any) -> None:
        self.items.append(item)

    def of(self, kind: str) -> list[tuple[Any, ...]]:
        return [c for c in self.items if c[0] == kind]

    def kinds(self) -> list[str]:
        return [str(c[0]) for c in self.items]


class FakeUids:
    def __init__(self) -> None:
        self.seen: list[str] = []

    def allocate(self, service_id: str) -> UidBlock:
        self.seen.append(service_id)
        return BLOCK


class _Handler(BaseHTTPRequestHandler):
    server_version = "FakeRegistry/1"

    def log_message(self, *_args: Any) -> None:  # keep pytest output clean
        return

    def _respond(self, status: int, body: bytes = b"{}") -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
        server: Any = self.server
        server.record("GET", self.path, None, dict(self.headers))
        self._respond(200 if self.path.endswith("/health") else 404)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
        server: Any = self.server
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:  # pragma: no cover - defensive
            body = {"raw": raw.decode("utf-8", "replace")}
        server.record("POST", self.path, body, dict(self.headers))
        if self.path.startswith("/api/services"):
            self._respond(server.services_status)
        elif self.path.startswith("/api/acl"):
            self._respond(server.acl_status)
        else:  # pragma: no cover - defensive
            self._respond(404)


class FakeRegistry:
    """Threaded loopback registry that also answers ``GET /health`` with 200."""

    def __init__(self) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.record = self.record  # type: ignore[attr-defined]
        self.httpd.services_status = 201  # type: ignore[attr-defined]
        self.httpd.acl_status = 200  # type: ignore[attr-defined]
        self.requests: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def port(self) -> int:
        return int(self.httpd.server_address[1])

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def record(self, method: str, path: str, body: Any, headers: dict[str, str]) -> None:
        with self._lock:
            self.requests.append({"method": method, "path": path, "body": body, "headers": headers})

    def posts(self, prefix: str) -> list[dict[str, Any]]:
        with self._lock:
            return [
                r for r in self.requests if r["method"] == "POST" and r["path"].startswith(prefix)
            ]

    def set_services_status(self, status: int) -> None:
        self.httpd.services_status = status  # type: ignore[attr-defined]

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self._thread.join(timeout=5)


# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def registry() -> Any:
    server = FakeRegistry()
    try:
        yield server
    finally:
        server.close()


@pytest.fixture
def upstream(tmp_path: Path) -> tuple[Path, str]:
    """Two commits: three manifests, then a memory bump plus a new service."""
    src = tmp_path / "upstream"
    src.mkdir()
    _git(src, "init", "-q", "-b", "main")
    first = _commit(
        src,
        {
            "services/alpha/service.yaml": service_manifest("alpha", port=9201),
            "services/beta/service.yaml": service_manifest("beta", port=9202),
            "apps/site/service.yaml": STATIC_MANIFEST,
            "README.md": "api\n",
        },
        "first",
    )
    return src, first


def add_second_commit(src: Path) -> str:
    return _commit(
        src,
        {
            "services/alpha/service.yaml": service_manifest("alpha", port=9201, memory="256M"),
            "services/gamma/service.yaml": service_manifest("gamma", port=9203),
        },
        "second",
    )


@pytest.fixture
def env(tmp_path: Path, registry: FakeRegistry, monkeypatch: pytest.MonkeyPatch) -> Any:
    """State dir + store + every host-touching seam replaced by a recorder."""
    state = StateDir(tmp_path / "state")
    state.ensure()
    store = RuntimeStore(tmp_path / "store")
    store.ensure()
    secrets = SecretStore(state.root / "secrets")
    secrets.set("registry", "REGISTRY_ADMIN_TOKEN", b"admin-token-value")
    calls = Calls()
    uids = FakeUids()

    def fake_stage(
        self: SourceMirror, sha: str, service_root: Path, block: UidBlock, **_kw: Any
    ) -> Path:
        repo = Path(service_root) / "repo"
        marker = repo / SHA_MARKER
        current = marker.read_text().strip() if marker.exists() else None
        if current == sha:
            calls.add("stage-noop", service_root.parent.name, sha)
            return repo
        repo.mkdir(parents=True, exist_ok=True)
        marker.write_text(sha + "\n", encoding="utf-8")
        calls.add("stage", service_root.parent.name, sha)
        return repo

    def fake_place_jwt_key(_state: StateDir, service_id: str, block: UidBlock, **_kw: Any) -> bool:
        calls.add("jwt", service_id)
        return False

    def fake_provision(decl: Any, root: Path, _store: Any, _block: Any, **_kw: Any) -> None:
        python_venv_dir(decl, Path(root)).mkdir(parents=True, exist_ok=True)
        calls.add("provision", decl.id)

    def fake_reload(state_arg: StateDir, **_kw: Any) -> dict[str, Any]:
        calls.add("reload")
        _allocate_ports(state_arg, registry.port)
        return {"ok": True, "added": [], "changed": [], "removed": []}

    def fake_restart(_state: StateDir, service_id: str, **_kw: Any) -> dict[str, Any]:
        calls.add("restart", service_id)
        return {"ok": True}

    monkeypatch.setattr(SourceMirror, "stage", fake_stage)
    monkeypatch.setattr(sync_mod, "place_jwt_key", fake_place_jwt_key)
    monkeypatch.setattr(sync_mod, "provision", fake_provision)
    monkeypatch.setattr(sync_mod, "ctl_reload", fake_reload)
    monkeypatch.setattr(sync_mod, "ctl_restart", fake_restart)
    monkeypatch.setattr(sync_mod, "uid_allocator", lambda _state: uids)

    _allocate_ports(state, registry.port, caddy_only=True)

    class Env:
        pass

    e = Env()
    e.state, e.store, e.secrets = state, store, secrets  # type: ignore[attr-defined]
    e.calls, e.uids, e.registry = calls, uids, registry  # type: ignore[attr-defined]
    return e


def _allocate_ports(state: StateDir, port: int, *, caddy_only: bool = False) -> None:
    """What the harness's reload does that this module cares about: assign ports.

    Every service is pointed at the fake registry's port so the real health gate
    gets a real 200 from a real socket; duplicate upstreams are meaningless to
    the renderer and keep the fixture to one server.
    """
    path = state.ports_state
    data: dict[str, Any] = {"version": 1, "ports": {}}
    if path.exists():
        data = json.loads(path.read_text())
    ports = data.setdefault("ports", {})
    ports.setdefault("caddy", {"main": CADDY_PORT})
    if not caddy_only:
        for service_id in state.list_service_ids():
            ports.setdefault(service_id, {"main": port})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def make_cfg(upstream_path: Path, registry: FakeRegistry, **kw: Any) -> SyncConfig:
    defaults: dict[str, Any] = {
        "repo_url": str(upstream_path),
        "registry_url": registry.url,
        "auth_url": registry.url,
        "health_deadline_s": 5.0,
        "health_interval_s": 0.05,
    }
    defaults.update(kw)
    return SyncConfig(**defaults)


def run_sync(env: Any, cfg: SyncConfig, capsys: Any = None) -> Any:
    return sync(env.state, env.store, cfg, secrets=env.secrets, uids=env.uids)


def read_state(state: StateDir) -> dict[str, Any]:
    return json.loads(state_path(state).read_text())


def snapshot(root: Path) -> dict[str, tuple[int, str]]:
    """Every file under ``root`` as ``{relpath: (size, mtime_ns_as_str)}``."""
    out: dict[str, tuple[int, str]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            st = path.stat()
            out[str(path.relative_to(root))] = (st.st_size, str(st.st_mtime_ns))
    return out


# --------------------------------------------------------------------------- the happy path


def test_first_run_drives_every_service_to_healthy(env: Any, upstream: Any) -> None:
    src, sha = upstream
    report = run_sync(env, make_cfg(src, env.registry))

    assert report.ok, report.services
    assert report.sha == sha
    assert set(report.ids) == {"alpha", "beta", "site"}
    stages = {o.id: o.stage for o in report.services}
    assert stages == {"alpha": "healthy", "beta": "healthy", "site": "declared"}
    assert report.escalations == ()


def test_first_run_call_sequence_is_exactly_this(env: Any, upstream: Any) -> None:
    src, sha = upstream
    run_sync(env, make_cfg(src, env.registry))

    # apps/site sorts before services/*, and a static site is staged nowhere.
    assert env.calls.items == [
        ("stage", "alpha", sha),
        ("jwt", "alpha"),
        ("provision", "alpha"),
        ("stage", "beta", sha),
        ("jwt", "beta"),
        ("provision", "beta"),
        ("reload",),
        ("restart", "caddy"),
    ]
    assert env.uids.seen == ["alpha", "beta"]


def test_state_file_matches_the_sidecar_doc_shape(env: Any, upstream: Any) -> None:
    src, sha = upstream
    run_sync(env, make_cfg(src, env.registry))

    data = read_state(env.state)
    assert data["version"] == 1
    assert sorted(data["services"]) == ["alpha", "beta", "site"]
    record = data["services"]["alpha"]
    assert sorted(record) == [
        "deployed_sha",
        "error",
        "escalated",
        "manual_restart",
        "prev_sha",
        "sha",
        "stage",
        "stage_since",
        "updated_at",
    ]
    assert record["deployed_sha"] == sha
    assert record["sha"] == sha
    assert record["prev_sha"] is None
    assert record["stage"] == "healthy"
    assert record["error"] is None
    assert record["manual_restart"] is False
    assert record["escalated"] is False
    assert record["updated_at"].endswith("Z") and len(record["updated_at"]) == 20
    assert record["stage_since"].endswith("Z")


def test_declaration_and_sidecars_are_written(env: Any, upstream: Any) -> None:
    src, _sha = upstream
    run_sync(env, make_cfg(src, env.registry))

    decl_path = env.state.service_decl_path("alpha")
    decl = load_decl(decl_path.read_text())
    assert decl.id == "alpha"
    assert decl.secrets == ("SVC_SECRET",)
    assert decl.limits.memory_max == "200M"

    mount = json.loads((mounts_dir(env.state) / "alpha.json").read_text())
    assert mount["version"] == 1
    assert mount["path"] == "/alpha"
    assert mount["port_name"] == "main"
    reg = json.loads((registry_dir(env.state) / "alpha.json").read_text())
    assert reg["audience"] == "alpha"
    assert reg["acl"][1] == {"action": "write", "principal": "admin", "effect": "allow"}


def test_static_site_gets_a_mount_but_no_declaration(env: Any, upstream: Any) -> None:
    src, _sha = upstream
    run_sync(env, make_cfg(src, env.registry))

    mount = json.loads((mounts_dir(env.state) / "site.json").read_text())
    assert mount["kind"] == "static"
    assert mount["subdomain"] == "site"
    assert mount["static_root"] == "site"
    assert not env.state.service_decl_path("site").exists()
    assert not (registry_dir(env.state) / "site.json").exists()
    # `declared` is the end of a static site's state machine: there is no probe.
    assert read_state(env.state)["services"]["site"]["stage"] == "declared"
    assert env.secrets.names("site") == []


def test_registry_identity_and_acl_are_created(env: Any, upstream: Any) -> None:
    src, _sha = upstream
    run_sync(env, make_cfg(src, env.registry))

    identities = env.registry.posts("/api/services")
    assert sorted(r["body"]["id"] for r in identities) == ["alpha", "beta"]
    alpha = next(r for r in identities if r["body"]["id"] == "alpha")
    assert alpha["body"]["capabilities"] == ["alpha"]
    assert alpha["body"]["display_name"] == "Alpha Service"
    assert alpha["headers"]["X-Admin-Token"] == "admin-token-value"
    acl = [r for r in env.registry.posts("/api/acl") if r["body"]["service_id"] == "alpha"]
    assert [(r["body"]["action"], r["body"]["principal"]) for r in acl] == [
        ("read", "user:*"),
        ("write", "admin"),
    ]


def test_gateway_is_rendered_after_the_reload_and_caddy_restarted(env: Any, upstream: Any) -> None:
    src, _sha = upstream
    report = run_sync(env, make_cfg(src, env.registry))

    assert report.caddy_restarted
    assert "Caddyfile" in report.gateway_changed
    kinds = env.calls.kinds()
    assert kinds.index("reload") < kinds.index("restart")
    caddyfile = (env.state.root / "gateway" / "Caddyfile").read_text()
    # The entry site binds the gateway host; path mounts are imported into it.
    assert f"http://127.0.0.1:{CADDY_PORT}" in caddyfile
    assert "import sites/alpha.caddy" in caddyfile
    assert (env.state.root / "gateway" / "sites" / "site.caddy").exists()


# --------------------------------------------------------------------------- secrets


def test_svc_secret_is_generated_once_and_never_printed(
    env: Any, upstream: Any, capsys: Any
) -> None:
    src, _sha = upstream
    run_sync(env, make_cfg(src, env.registry))

    assert env.secrets.names("alpha") == ["SVC_SECRET"]
    value = env.secrets.load("alpha", ["SVC_SECRET"])["SVC_SECRET"]
    assert len(value) == 64 and int(value, 16) >= 0
    posted = next(r for r in env.registry.posts("/api/services") if r["body"]["id"] == "alpha")
    assert posted["headers"]["X-Service-Secret"] == value

    captured = capsys.readouterr()
    assert value not in captured.out
    assert value not in captured.err
    assert value not in json.dumps(read_state(env.state))

    # A second run must not rotate it: that would invalidate the live identity.
    run_sync(env, make_cfg(src, env.registry))
    assert env.secrets.load("alpha", ["SVC_SECRET"])["SVC_SECRET"] == value


# --------------------------------------------------------------------------- idempotence


def test_second_run_with_nothing_changed_is_a_no_op(env: Any, upstream: Any) -> None:
    src, _sha = upstream
    run_sync(env, make_cfg(src, env.registry))
    before = snapshot(env.state.root)
    env.calls.items.clear()
    env.registry.requests.clear()

    report = run_sync(env, make_cfg(src, env.registry))

    assert set(report.unchanged) == set(report.ids)
    assert report.ok and not report.state_written and not report.reloaded
    assert not report.caddy_restarted and report.gateway_changed == ()
    assert env.calls.of("reload") == [] and env.calls.of("restart") == []
    assert env.calls.of("provision") == []
    assert env.registry.requests == []  # no identity, no ACL, no probe
    assert snapshot(env.state.root) == before


def test_second_commit_touches_only_the_changed_service_and_the_new_one(
    env: Any, upstream: Any
) -> None:
    src, first = upstream
    run_sync(env, make_cfg(src, env.registry))
    second = add_second_commit(src)
    env.calls.items.clear()
    env.registry.requests.clear()

    report = run_sync(env, make_cfg(src, env.registry))

    assert report.sha == second and report.ok
    assert set(report.ids) == {"alpha", "beta", "gamma", "site"}
    # Only `services/alpha/` and `services/gamma/` changed, so beta and the
    # static site are neither re-staged, re-provisioned nor re-declared.
    assert sorted(c[1] for c in env.calls.of("stage")) == ["alpha", "gamma"]
    assert sorted(c[1] for c in env.calls.of("provision")) == ["alpha", "gamma"]
    assert set(report.unchanged) == {"beta", "site"}
    assert "declare" in report.outcome("alpha").actions
    assert report.outcome("beta").actions == ()
    assert env.calls.of("reload") == [("reload",)]
    assert sorted(r["body"]["id"] for r in env.registry.posts("/api/services")) == [
        "alpha",
        "gamma",
    ]
    decl = load_decl(env.state.service_decl_path("alpha").read_text())
    assert decl.limits.memory_max == "256M"
    assert decl.env["GIT_COMMIT"] == second
    assert read_state(env.state)["services"]["alpha"]["prev_sha"] == first
    assert report.outcome("gamma").stage == "healthy"


def test_an_untouched_service_keeps_its_commit_and_its_declaration(env: Any, upstream: Any) -> None:
    """The whole point: `GIT_COMMIT` no longer restarts the fleet on any commit.

    `translate` injects `GIT_COMMIT=<sha>` (PLAN-allin Q2) and the harness
    restarts on a declaration content hash (D17), so a service translated at the
    new head would be rewritten and restarted by every unrelated commit. It is
    translated at the sha it is already deployed at instead (D26).
    """
    src, first = upstream
    run_sync(env, make_cfg(src, env.registry))
    beta_before = env.state.service_decl_path("beta").read_text()
    second = add_second_commit(src)
    env.registry.requests.clear()

    report = run_sync(env, make_cfg(src, env.registry))

    assert env.state.service_decl_path("beta").read_text() == beta_before
    assert load_decl(beta_before).env["GIT_COMMIT"] == first
    assert "beta" in report.unchanged
    record = read_state(env.state)["services"]["beta"]
    assert record["sha"] == first and record["deployed_sha"] == first
    assert record["stage"] == "healthy"
    # alpha did move, so the run as a whole is at the new head.
    assert report.sha == second
    registered = {r["body"]["id"] for r in env.registry.posts("/api/services")}
    assert registered == {"alpha", "gamma"}  # beta was never re-registered
    assert "beta" not in {r["body"]["service_id"] for r in env.registry.posts("/api/acl")}


def test_a_docs_only_commit_is_a_full_no_op(env: Any, upstream: Any) -> None:
    src, _first = upstream
    run_sync(env, make_cfg(src, env.registry))
    before = snapshot(env.state.root)
    head = _commit(src, {"README.md": "docs only\n"}, "docs")
    env.calls.items.clear()
    env.registry.requests.clear()

    report = run_sync(env, make_cfg(src, env.registry))

    assert report.sha == head
    assert set(report.unchanged) == set(report.ids)
    assert not report.state_written and not report.reloaded
    # `stage-noop` and `jwt` are a marker read and a stat; nothing moved.
    assert env.calls.kinds() == ["stage-noop", "jwt", "stage-noop", "jwt"]
    assert env.calls.of("provision") == [] and env.calls.of("reload") == []
    assert env.registry.requests == []
    assert snapshot(env.state.root) == before


def test_a_components_sdk_change_fans_out_to_every_service(env: Any, upstream: Any) -> None:
    src, _first = upstream
    run_sync(env, make_cfg(src, env.registry))
    head = _commit(src, {"components/sdk/src/sdk/m2m.py": "# new verifier\n"}, "sdk")
    env.calls.items.clear()

    report = run_sync(env, make_cfg(src, env.registry))

    assert sorted(c[1] for c in env.calls.of("stage")) == ["alpha", "beta"]
    assert sorted(c[1] for c in env.calls.of("provision")) == ["alpha", "beta"]
    assert report.unchanged == ()
    for service_id in ("alpha", "beta"):
        assert (
            load_decl(env.state.service_decl_path(service_id).read_text()).env["GIT_COMMIT"] == head
        )
    assert "publish" in report.outcome("site").actions


def test_a_shared_change_fans_out_to_every_service(env: Any, upstream: Any) -> None:
    """`shared/` is the deployer's own fan-out lever, carried over verbatim."""
    src, _first = upstream
    run_sync(env, make_cfg(src, env.registry))
    _commit(src, {"shared/policy.json": "{}\n"}, "shared")
    env.calls.items.clear()

    report = run_sync(env, make_cfg(src, env.registry))

    assert sorted(c[1] for c in env.calls.of("stage")) == ["alpha", "beta"]
    assert report.unchanged == ()


def test_components_other_than_the_sdk_do_not_fan_out(env: Any, upstream: Any) -> None:
    """Only the configured prefixes fan out; `components/` wholesale does not.

    `changes.py` dropped `components/` as a shared prefix on 2026-07-02 because
    a registry or auth push restarted every service. Only the SDK is added back.
    """
    src, _first = upstream
    run_sync(env, make_cfg(src, env.registry))
    _commit(src, {"components/registry/src/registry/api/acl.py": "# tweak\n"}, "registry")
    env.calls.items.clear()

    report = run_sync(env, make_cfg(src, env.registry))

    assert env.calls.of("stage") == [] and env.calls.of("provision") == []
    assert env.calls.of("reload") == []
    assert set(report.unchanged) == set(report.ids)


def test_shared_prefixes_are_configurable(env: Any, upstream: Any) -> None:
    src, _first = upstream
    run_sync(env, make_cfg(src, env.registry))
    _commit(src, {"components/registry/x.py": "# tweak\n"}, "registry")
    env.calls.items.clear()

    report = run_sync(env, make_cfg(src, env.registry, shared_prefixes=("shared/", "components/")))

    assert sorted(c[1] for c in env.calls.of("stage")) == ["alpha", "beta"]
    assert report.unchanged == ()


def test_a_service_with_nothing_deployed_is_affected_by_definition(env: Any, upstream: Any) -> None:
    src, _first = upstream
    run_sync(env, make_cfg(src, env.registry))
    _commit(src, {"services/gamma/service.yaml": service_manifest("gamma", port=9203)}, "new")
    env.calls.items.clear()

    report = run_sync(env, make_cfg(src, env.registry))

    assert [c[1] for c in env.calls.of("stage")] == ["gamma"]
    assert report.outcome("gamma").stage == "healthy"


def test_an_undiffable_deployed_sha_falls_back_to_the_head(
    env: Any, upstream: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A commit the mirror no longer has must mean "assume affected", not "skip"."""
    src, _first = upstream
    run_sync(env, make_cfg(src, env.registry))
    _commit(src, {"README.md": "docs only\n"}, "docs")
    env.calls.items.clear()

    def gone(self: SourceMirror, old: str, new: str, timeout_s: float = 0.0) -> list[str]:
        raise SourceError(f"no such commit {old}")

    monkeypatch.setattr(SourceMirror, "changed_paths", gone)
    report = run_sync(env, make_cfg(src, env.registry))

    assert sorted(c[1] for c in env.calls.of("stage")) == ["alpha", "beta"]
    assert report.unchanged == ()


def test_an_untouched_service_keeps_its_healthy_record_untouched(env: Any, upstream: Any) -> None:
    src, first = upstream
    run_sync(env, make_cfg(src, env.registry))
    add_second_commit(src)

    run_sync(env, make_cfg(src, env.registry))

    record = read_state(env.state)["services"]["beta"]
    assert record["sha"] == first
    assert record["deployed_sha"] == first
    assert record["stage"] == "healthy"
    assert record["prev_sha"] is None  # it never adopted a second sha


# --------------------------------------------------------------------------- failures


def test_fetch_failure_escalates_once_and_stops(env: Any, tmp_path: Path, capsys: Any) -> None:
    cfg = make_cfg(tmp_path / "does-not-exist", env.registry)
    report = sync(env.state, env.store, cfg, secrets=env.secrets, uids=env.uids)

    assert not report.ok and report.services == ()
    assert len(report.escalations) == 1
    record = report.escalations[0]
    assert record["kind"] == "PlatformSync"
    assert record["service_id"] is None
    assert record["event"]["stage"] == "fetch"
    assert not state_path(env.state).exists()
    assert len(capsys.readouterr().out.strip().splitlines()) == 1


def test_a_broken_manifest_fails_only_itself(env: Any, upstream: Any) -> None:
    src, sha = upstream
    _commit(src, {"services/broken/service.yaml": BROKEN_MANIFEST}, "add broken")

    report = run_sync(env, make_cfg(src, env.registry))

    assert {o.id for o in report.services if not o.ok} == {"broken"}
    assert {o.id: o.stage for o in report.services if o.ok} == {
        "alpha": "healthy",
        "beta": "healthy",
        "site": "declared",
    }
    record = read_state(env.state)["services"]["broken"]
    assert record["stage"] == "failed"
    assert record["error"].startswith("translate: TranslateError")
    assert record["escalated"] is True
    assert record["sha"] != sha  # the third commit
    escalations = [e for e in report.escalations if e["service_id"] == "broken"]
    assert len(escalations) == 1
    assert escalations[0]["event"]["stage"] == "translate"


def test_a_repeated_translate_failure_escalates_once_not_per_tick(env: Any, upstream: Any) -> None:
    src, _sha = upstream
    _commit(src, {"services/broken/service.yaml": BROKEN_MANIFEST}, "add broken")
    first = run_sync(env, make_cfg(src, env.registry))
    assert len([e for e in first.escalations if e["service_id"] == "broken"]) == 1

    second = run_sync(env, make_cfg(src, env.registry))

    assert second.escalations == ()
    assert read_state(env.state)["services"]["broken"]["stage"] == "failed"


def test_provision_failure_leaves_the_service_at_failed(
    env: Any, upstream: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    src, _sha = upstream

    def boom(decl: Any, root: Path, store: Any, block: Any, **_kw: Any) -> None:
        if decl.id == "alpha":
            raise RuntimeError("uv sync exploded")
        python_venv_dir(decl, Path(root)).mkdir(parents=True, exist_ok=True)
        env.calls.add("provision", decl.id)

    monkeypatch.setattr(sync_mod, "provision", boom)
    report = run_sync(env, make_cfg(src, env.registry))

    assert report.failed == ("alpha",)
    record = read_state(env.state)["services"]["alpha"]
    assert record["stage"] == "failed"
    assert record["error"] == "provision: RuntimeError: uv sync exploded"
    assert not env.state.service_decl_path("alpha").exists()
    assert len([e for e in report.escalations if e["service_id"] == "alpha"]) == 1
    # beta is untouched by alpha's failure.
    assert report.outcome("beta").stage == "healthy"


def test_registry_403_leaves_the_service_at_reloaded(env: Any, upstream: Any) -> None:
    src, _sha = upstream
    env.registry.set_services_status(403)

    report = run_sync(env, make_cfg(src, env.registry))

    assert set(report.failed) == {"alpha", "beta"}
    record = read_state(env.state)["services"]["alpha"]
    assert record["stage"] == "failed"
    assert record["error"].startswith("register: RegistryError")
    assert len([e for e in report.escalations if e["service_id"] == "alpha"]) == 1
    # The declaration is on disk and the reload happened: only the registry step
    # is missing, and the next tick retries exactly that.
    assert env.state.service_decl_path("alpha").exists()
    assert report.reloaded


def test_health_timeout_records_the_previous_healthy_sha(env: Any, upstream: Any) -> None:
    src, first = upstream
    run_sync(env, make_cfg(src, env.registry))
    second = add_second_commit(src)
    # Point alpha at a port nothing is listening on, so the probe cannot pass.
    data = json.loads(env.state.ports_state.read_text())
    data["ports"]["alpha"]["main"] = 65533
    env.state.ports_state.write_text(json.dumps(data), encoding="utf-8")

    report = run_sync(env, make_cfg(src, env.registry, health_deadline_s=0.0))

    assert report.failed == ("alpha",)
    record = read_state(env.state)["services"]["alpha"]
    assert record["stage"] == "failed"
    assert record["sha"] == second
    assert record["prev_sha"] == first
    assert "not 200 within" in record["error"]
    escalation = next(e for e in report.escalations if e["service_id"] == "alpha")
    assert escalation["event"]["sha"] == second
    assert escalation["event"]["prev_sha"] == first
    assert report.outcome("gamma").stage == "healthy"


def test_reload_failure_stops_the_run_with_one_escalation(
    env: Any, upstream: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    src, _sha = upstream

    def boom(_state: StateDir, **_kw: Any) -> dict[str, Any]:
        raise RuntimeError("no harness listening")

    monkeypatch.setattr(sync_mod, "ctl_reload", boom)
    report = run_sync(env, make_cfg(src, env.registry))

    assert not report.ok and report.error is not None
    assert len(report.escalations) == 1
    assert report.escalations[0]["event"]["stage"] == "reload"
    # Every service is left at `declared`: the files are written, the harness
    # just does not know about them yet.
    assert read_state(env.state)["services"]["alpha"]["stage"] == "declared"
    assert env.registry.posts("/api/services") == []
    assert report.gateway_changed == ()


def test_a_run_recovers_from_a_failed_reload_on_the_next_tick(
    env: Any, upstream: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    src, _sha = upstream
    working = sync_mod.ctl_reload
    monkeypatch.setattr(
        sync_mod, "ctl_reload", lambda _s, **_k: (_ for _ in ()).throw(RuntimeError("down"))
    )
    run_sync(env, make_cfg(src, env.registry))
    monkeypatch.setattr(sync_mod, "ctl_reload", working)

    report = run_sync(env, make_cfg(src, env.registry))

    assert report.ok and report.reloaded
    assert report.outcome("alpha").stage == "healthy"


# --------------------------------------------------------------------------- manual restart


def test_manual_restart_declaration_change_is_not_written(env: Any, upstream: Any) -> None:
    src, _sha = upstream
    _commit(
        src,
        {"services/alpha/service.yaml": service_manifest("alpha", port=9201, manual_restart=True)},
        "alpha becomes manual_restart",
    )
    run_sync(env, make_cfg(src, env.registry))
    before = env.state.service_decl_path("alpha").read_text()
    assert read_state(env.state)["services"]["alpha"]["manual_restart"] is True

    _commit(
        src,
        {
            "services/alpha/service.yaml": service_manifest(
                "alpha", port=9201, memory="256M", manual_restart=True
            )
        },
        "bump alpha memory",
    )
    report = run_sync(env, make_cfg(src, env.registry))

    assert report.failed == ("alpha",)
    assert env.state.service_decl_path("alpha").read_text() == before
    record = read_state(env.state)["services"]["alpha"]
    assert record["stage"] == "failed"
    assert record["error"] == "declare: declaration changed but manual_restart is set"
    assert "manual-restart-pending" in report.outcome("alpha").actions
    assert len([e for e in report.escalations if e["service_id"] == "alpha"]) == 1
    # And it escalates once, not on every tick.
    assert run_sync(env, make_cfg(src, env.registry)).escalations == ()


def test_manual_restart_first_declaration_is_still_written(env: Any, upstream: Any) -> None:
    """Nothing is running yet, so there is no restart to avoid."""
    src, _sha = upstream
    _commit(
        src,
        {"services/alpha/service.yaml": service_manifest("alpha", port=9201, manual_restart=True)},
        "alpha becomes manual_restart",
    )
    report = run_sync(env, make_cfg(src, env.registry))

    assert report.ok
    assert env.state.service_decl_path("alpha").exists()


# --------------------------------------------------------------------------- selection


def test_only_selects_by_id_and_leaves_the_rest_alone(env: Any, upstream: Any) -> None:
    src, _sha = upstream
    report = run_sync(env, make_cfg(src, env.registry, include=("alpha",)))

    assert report.ids == ("alpha",)
    assert not env.state.service_decl_path("beta").exists()
    assert sorted(read_state(env.state)["services"]) == ["alpha"]


def test_exclude_wins_over_include(env: Any, upstream: Any) -> None:
    src, _sha = upstream
    report = run_sync(
        env, make_cfg(src, env.registry, include=("alpha", "beta"), exclude=("beta",))
    )
    assert report.ids == ("alpha",)


def test_a_broken_manifest_that_is_not_selected_is_silent(env: Any, upstream: Any) -> None:
    src, _sha = upstream
    _commit(src, {"services/broken/service.yaml": BROKEN_MANIFEST}, "add broken")

    report = run_sync(env, make_cfg(src, env.registry, exclude=("broken",)))

    assert "broken" not in report.ids
    assert report.escalations == ()


def test_disabled_manifests_are_skipped(env: Any, upstream: Any, tmp_path: Path) -> None:
    src, _sha = upstream
    _commit(
        src,
        {"services/retired.disabled/service.yaml": service_manifest("retired", port=9299)},
        "retire a service",
    )
    report = run_sync(env, make_cfg(src, env.registry))
    assert "retired" not in report.ids


def test_discover_manifests_sorts_and_skips_disabled(tmp_path: Path) -> None:
    root = tmp_path / "checkout"
    for rel in (
        "services/b/service.yaml",
        "services/a/service.yaml",
        "apps/z/service.yaml",
        "services/old.disabled/service.yaml",
        "services/a/service.yaml.disabled",
    ):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")
    found = discover_manifests(root, ("services/*/service.yaml", "apps/*/service.yaml"))
    assert [str(p.relative_to(root)) for p in found] == [
        "apps/z/service.yaml",
        "services/a/service.yaml",
        "services/b/service.yaml",
    ]


# --------------------------------------------------------------------------- extra secrets


def test_service_ams_toml_adds_secret_names(env: Any, upstream: Any) -> None:
    src, _sha = upstream
    _commit(
        src,
        {"services/alpha/service.ams.toml": 'secrets = ["OPENAI_API_KEY"]\n'},
        "alpha needs a third-party key",
    )
    report = run_sync(env, make_cfg(src, env.registry))

    decl = load_decl(env.state.service_decl_path("alpha").read_text())
    assert decl.secrets == ("SVC_SECRET", "OPENAI_API_KEY")
    # The unset name is a warning, not this loop's failure to fix.
    assert report.outcome("alpha").stage == "healthy"
    assert env.secrets.names("alpha") == ["SVC_SECRET"]


def test_a_malformed_service_ams_toml_fails_only_that_service(env: Any, upstream: Any) -> None:
    src, _sha = upstream
    _commit(src, {"services/alpha/service.ams.toml": "secrets = 3\n"}, "bad sidefile")

    report = run_sync(env, make_cfg(src, env.registry))

    assert report.failed == ("alpha",)
    record = read_state(env.state)["services"]["alpha"]
    assert record["error"].startswith("translate: StaticError")
    assert "secrets must be a list of names" in record["error"]
    assert report.outcome("beta").stage == "healthy"


# --------------------------------------------------------------------------- dry run


def test_dry_run_writes_nothing_and_prints_the_plan(env: Any, upstream: Any, capsys: Any) -> None:
    src, sha = upstream
    before = snapshot(env.state.root)

    report = run_sync(env, make_cfg(src, env.registry, dry_run=True))

    assert snapshot(env.state.root) == before
    assert env.calls.items == []
    assert env.registry.requests == []
    assert not state_path(env.state).exists()
    assert {o.id for o in report.services} == {"alpha", "beta", "site"}

    lines = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert {r["service_id"] for r in lines} == {"alpha", "beta", "site"}
    assert all(r["kind"] == "PlatformSyncPlan" and r["action"] == "log" for r in lines)
    alpha = next(r for r in lines if r["service_id"] == "alpha")
    assert alpha["event"]["sha"] == sha
    assert "provision" in alpha["reason"] and "register" in alpha["reason"]


# --------------------------------------------------------------------------- config


@pytest.mark.parametrize(
    "url",
    [
        "https://registry.lishuyu.app",
        "http://10.0.0.5:20100",
        "http://localhost:20100",
        "http://127.0.0.1:20100/",
    ],
)
def test_non_loopback_urls_are_rejected(url: str) -> None:
    with pytest.raises(SyncError, match="loopback"):
        SyncConfig(repo_url="/tmp/x", registry_url=url)
    with pytest.raises(SyncError, match="loopback"):
        SyncConfig(repo_url="/tmp/x", auth_url=url)


def test_loopback_urls_are_accepted() -> None:
    cfg = SyncConfig(repo_url="/tmp/x", registry_url="http://127.0.0.1:20100")
    assert cfg.registry_url == "http://127.0.0.1:20100"
    assert cfg.registry_admin_token_from == ("registry", "REGISTRY_ADMIN_TOKEN")


def test_selects_matches_dir_name_or_id() -> None:
    cfg = SyncConfig(repo_url="/tmp/x", include=("alpha",), exclude=("beta",))
    assert cfg.selects("alpha") and cfg.selects("services-alpha", "alpha")
    assert not cfg.selects("beta") and not cfg.selects("alpha", "beta")
    assert not cfg.selects("gamma")


# --------------------------------------------------------------------------- state file


def test_platform_state_refuses_an_unknown_version(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"version": 2, "services": {}}), encoding="utf-8")
    with pytest.raises(SyncError, match="unsupported version"):
        PlatformState.load(path)


def test_platform_state_flush_is_change_gated(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    ps = PlatformState(path, {})
    assert not ps.flush() and not path.exists()
    ps.records["a"] = ServiceRecord(sha="abc1234", stage="healthy")
    assert ps.flush() and path.exists()
    assert not ps.flush()


def test_begin_remembers_the_last_healthy_sha(tmp_path: Path) -> None:
    ps = PlatformState(tmp_path / "s.json", {})
    ps.begin("a", "a" * 40, manual_restart=False, now_s=0.0)
    ps.set_stage("a", "healthy", now_s=1.0)
    prior = ps.begin("a", "b" * 40, manual_restart=False, now_s=2.0)
    assert prior == "healthy"
    rec = ps.get("a")
    assert rec.sha == "b" * 40 and rec.prev_sha == "a" * 40 and rec.stage == STAGES[0]


def test_begin_leaves_an_unchanged_record_alone(tmp_path: Path) -> None:
    ps = PlatformState(tmp_path / "s.json", {})
    ps.begin("a", "a" * 40, manual_restart=False, now_s=0.0)
    ps.set_stage("a", "healthy", now_s=1.0)
    ps.flush()
    assert ps.begin("a", "a" * 40, manual_restart=False, now_s=99.0) == "healthy"
    assert not ps.dirty()


# --------------------------------------------------------------------------- cli


def test_platform_is_wired_into_the_ams_parser() -> None:
    from ams.cli import build_parser

    args = build_parser().parse_args(["platform", "sync", "--ref", "topic", "--dry-run"])
    assert args.command == "platform" and args.platform_command == "sync"
    assert args.ref == "topic" and args.dry_run is True
    status = build_parser().parse_args(["platform", "status", "alpha"])
    assert status.platform_command == "status" and status.ids == ["alpha"]


def test_platform_status_prints_the_state(env: Any, upstream: Any, capsys: Any) -> None:
    from ams.cli import main as ams_main

    src, sha = upstream
    run_sync(env, make_cfg(src, env.registry))
    capsys.readouterr()

    code = ams_main(["platform", "status", "--state-dir", str(env.state.root)])
    out = capsys.readouterr().out

    assert code == 0
    assert "alpha" in out and "healthy" in out and sha[:12] in out
    assert "beta" in out and "site" in out


def test_platform_status_reports_a_failed_service(env: Any, upstream: Any, capsys: Any) -> None:
    from ams.cli import main as ams_main

    src, _sha = upstream
    _commit(src, {"services/broken/service.yaml": BROKEN_MANIFEST}, "add broken")
    run_sync(env, make_cfg(src, env.registry))
    capsys.readouterr()

    code = ams_main(["platform", "status", "--state-dir", str(env.state.root)])
    out = capsys.readouterr().out

    assert code == 1
    assert "failed" in out and "error: translate:" in out


def test_platform_status_without_a_state_file_is_exit_2(tmp_path: Path, capsys: Any) -> None:
    from ams.cli import main as ams_main

    assert ams_main(["platform", "status", "--state-dir", str(tmp_path / "empty")]) == 2
    assert "run 'ams platform sync'" in capsys.readouterr().err


def test_format_status_renders_flags_and_the_last_healthy_sha() -> None:
    from ams.platform.cli import format_status

    services = {
        "alpha": {
            "stage": "failed",
            "sha": "b" * 40,
            "prev_sha": "a" * 40,
            "error": "health: no",
            "manual_restart": True,
            "escalated": True,
            "stage_since": "2026-09-02T13:00:00Z",
        }
    }
    lines = format_status(services, ["alpha"])
    assert "manual_restart, escalated" in lines[0]
    assert "failed" in lines[0] and "b" * 12 in lines[0]
    assert lines[1].strip() == "error: health: no"
    assert lines[2].strip() == "last healthy: " + "a" * 12


def test_a_service_that_fails_this_tick_keeps_its_gateway_route(env: Any, upstream: Any) -> None:
    """The gateway is fleet state, rendered from the sidecars on disk.

    Rendering from the run's own list would delete ``sites/alpha.caddy`` the
    moment alpha's manifest broke, taking a still-running service off the
    gateway because of an unrelated typo.
    """
    src, _sha = upstream
    run_sync(env, make_cfg(src, env.registry))
    _commit(src, {"services/alpha/service.yaml": BROKEN_MANIFEST}, "break alpha")

    report = run_sync(env, make_cfg(src, env.registry))

    assert report.failed == ("alpha",)
    assert (env.state.root / "gateway" / "sites" / "alpha.caddy").exists()
    assert "import sites/alpha.caddy" in (env.state.root / "gateway" / "Caddyfile").read_text()


def test_only_still_renders_the_whole_fleet_gateway(env: Any, upstream: Any) -> None:
    src, _sha = upstream
    run_sync(env, make_cfg(src, env.registry))
    (env.state.root / "gateway" / "Caddyfile").unlink()

    run_sync(env, make_cfg(src, env.registry, include=("alpha",)))

    caddyfile = (env.state.root / "gateway" / "Caddyfile").read_text()
    assert "import sites/beta.caddy" in caddyfile
    assert "import sites/site.caddy" in caddyfile


def test_a_declared_service_with_no_port_yet_is_left_off_the_gateway(
    env: Any, upstream: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    src, _sha = upstream

    def reload_without_allocating(_state: StateDir, **_kw: Any) -> dict[str, Any]:
        env.calls.add("reload")
        return {"ok": True}

    monkeypatch.setattr(sync_mod, "ctl_reload", reload_without_allocating)
    report = run_sync(env, make_cfg(src, env.registry))

    # Nothing raised; the static site still renders, the two portless services
    # do not, and the next tick picks them up once the harness allocates.
    assert "Caddyfile" in report.gateway_changed
    caddyfile = (env.state.root / "gateway" / "Caddyfile").read_text()
    assert "import sites/site.caddy" in caddyfile
    assert "alpha.caddy" not in caddyfile


# --------------------------------------------------------------------- static publishing


def test_a_static_site_is_published_to_the_document_root(env: Any, upstream: Any) -> None:
    """T3.4's `publish_static` is a static mount's `provisioned` step."""
    from ams.platform.gateway import static_root

    src, sha = upstream
    report = run_sync(env, make_cfg(src, env.registry))

    target = static_root(env.state) / "site"
    assert (target / ".ams-sha").read_text().strip() == sha
    assert (target / "service.yaml").is_file()
    assert "publish" in report.outcome("site").actions
    assert report.outcome("site").stage == "declared"


def test_republishing_the_same_sha_is_a_no_op(env: Any, upstream: Any) -> None:
    from ams.platform.gateway import static_root

    src, _sha = upstream
    run_sync(env, make_cfg(src, env.registry))
    marker = static_root(env.state) / "site" / ".ams-sha"
    before = marker.stat().st_mtime_ns

    report = run_sync(env, make_cfg(src, env.registry))

    assert marker.stat().st_mtime_ns == before
    assert report.outcome("site").actions == ()
    assert "site" in report.unchanged


def test_a_static_site_with_no_source_dir_fails_only_itself(env: Any, upstream: Any) -> None:
    src, _sha = upstream
    # A static manifest under services/ has no apps/<id> tree to publish.
    _commit(
        src,
        {"services/orphan/service.yaml": STATIC_MANIFEST.replace("site", "orphan")},
        "static site with no apps/ source",
    )
    report = run_sync(env, make_cfg(src, env.registry))

    assert report.failed == ("orphan",)
    record = read_state(env.state)["services"]["orphan"]
    assert record["stage"] == "failed"
    assert record["error"].startswith("provision: publish: StaticError")
    assert report.outcome("alpha").stage == "healthy"


def test_the_overlay_reader_is_t34s_and_names_are_folded_in(env: Any, upstream: Any) -> None:
    """Hook 1: `ams.platform.static.load_ams_overlay` is the only overlay reader."""
    from ams.platform import static as static_mod

    src, _sha = upstream
    _commit(
        src,
        {"services/alpha/service.ams.toml": 'secrets = ["OPENAI_API_KEY", "STRIPE_KEY"]\n'},
        "alpha needs two third-party keys",
    )
    seen: list[Path] = []
    original = static_mod.load_ams_overlay

    def spy(manifest_dir: Path) -> Any:
        seen.append(Path(manifest_dir))
        return original(manifest_dir)

    sync_mod.load_ams_overlay = spy  # type: ignore[assignment]
    try:
        run_sync(env, make_cfg(src, env.registry))
    finally:
        sync_mod.load_ams_overlay = original  # type: ignore[assignment]

    assert [p.name for p in seen] == ["site", "alpha", "beta"]
    decl = load_decl(env.state.service_decl_path("alpha").read_text())
    assert decl.secrets == ("SVC_SECRET", "OPENAI_API_KEY", "STRIPE_KEY")


def test_the_overlay_env_table_reaches_the_declaration(env: Any, upstream: Any) -> None:
    """T3.4 validates `[env]` and leaves the merge to its caller. This is it."""
    src, _sha = upstream
    _commit(
        src,
        {"services/alpha/service.ams.toml": '[env]\nALPHA_FEATURE_FLAG = "on"\n'},
        "alpha gets a tunable",
    )
    report = run_sync(env, make_cfg(src, env.registry))

    decl = load_decl(env.state.service_decl_path("alpha").read_text())
    assert decl.env["ALPHA_FEATURE_FLAG"] == "on"
    assert decl.env["SVC_NAME"] == "alpha"  # the translated env survives the merge
    assert report.outcome("alpha").stage == "healthy"


def test_an_overlay_env_name_colliding_with_a_secret_fails_only_that_service(
    env: Any, upstream: Any
) -> None:
    src, _sha = upstream
    _commit(
        src,
        {"services/alpha/service.ams.toml": '[env]\nSVC_SECRET = "nope"\n'},
        "an overlay that collides with the declared secret",
    )
    report = run_sync(env, make_cfg(src, env.registry))

    assert report.failed == ("alpha",)
    assert read_state(env.state)["services"]["alpha"]["error"].startswith("translate: DeclError")
    assert report.outcome("beta").stage == "healthy"
