"""Portable tests for ``ams platform rollback``.

Same shape as ``tests/test_platform_sync.py``: a throwaway git repository and a
threaded loopback registry in ``tmp_path``, with the four calls that would touch
the host -- ``SourceMirror.stage`` (forks into the admin user namespace),
``runtime.provision`` (shells out to uv), ``bootstrap.place_jwt_key`` (admin ns
again) and the control-socket clients -- replaced by recorders. ``translate``,
``emit_toml``, the SecretStore, the port allocator and the real health gate over
a real socket are the genuine articles.

The fixtures are written here rather than imported from ``test_platform_sync``
on purpose: that module belongs to T3.1 and is being edited concurrently, and a
test suite that breaks because a sibling renamed a helper is worse than a
hundred lines of fixture. What *is* shared is the production format of the files
both modules write (``sync._write_json_if_changed``), because a byte mismatch
there would make the next sync tick rewrite what a rollback just wrote.

The starting state is built directly rather than by running a whole ``sync()``:
a rollback's inputs are a state record, a declaration and a staged tree, and
constructing those three is both smaller and a stricter test than driving the
sync loop and hoping it left the right thing behind.
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

from ams.platform import rollback as rollback_mod
from ams.platform.rollback import (
    ESCALATION_KIND,
    RollbackError,
    load_state_doc,
    main,
    rollback,
)
from ams.platform.sources import SHA_MARKER, SourceMirror
from ams.platform.sync import STATE_VERSION, SyncConfig, mounts_dir, registry_dir, state_path
from ams.platform.translate import emit_toml, translate
from ams.runtime import RuntimeStore, python_venv_dir
from ams.secrets import SecretStore
from ams.state import StateDir
from ams.uidmap import UidBlock

BLOCK = UidBlock(100_000, 100_000, 1024)
SERVICE_ID = "alpha"
#: The one value that must never appear in any output this module can observe.
SECRET_VALUE = "s3cr3t-svc-secret-must-never-be-printed"
ADMIN_TOKEN = "admin-token-must-never-be-printed"

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


def service_manifest(name: str = SERVICE_ID, *, memory: str = "200M", port: int = 9201) -> str:
    return "\n".join(
        [
            "schema_version: 1",
            "kind: service",
            f"name: {name}",
            f"audience: {name}",
            f"display_name: {name.title()} Service",
            "owner: steven",
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
            f"  exec: /srv/{name}/services/{name}/.venv/bin/uvicorn {name}.main:app "
            "--host 127.0.0.1 --port ${PORT}",
            f"  working_dir: /srv/{name}/services/{name}",
            "  environment:",
            f"    SVC_ROOT_PATH: /{name}",
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
            "",
            "registry:",
            f"  capabilities: [{name}]",
            "  health_path: /health",
            "",
        ]
    )


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
    """Ordered record of everything the rollback did to the host or the harness."""

    def __init__(self) -> None:
        self.items: list[tuple[Any, ...]] = []

    def add(self, *item: Any) -> None:
        self.items.append(item)

    def kinds(self) -> list[str]:
        return [str(c[0]) for c in self.items]

    def of(self, kind: str) -> list[tuple[Any, ...]]:
        return [c for c in self.items if c[0] == kind]


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

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
        server: Any = self.server
        server.record("GET", self.path)
        status = server.health_status if self.path.endswith("/health") else 404
        body = b"{}"
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FakeRegistry:
    """Threaded loopback server answering ``GET /health``. 200 unless told otherwise."""

    def __init__(self) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.record = self.record  # type: ignore[attr-defined]
        self.httpd.health_status = 200  # type: ignore[attr-defined]
        self.requests: list[tuple[str, str]] = []
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def port(self) -> int:
        return int(self.httpd.server_address[1])

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def record(self, method: str, path: str) -> None:
        with self._lock:
            self.requests.append((method, path))

    def set_health(self, status: int) -> None:
        self.httpd.health_status = status  # type: ignore[attr-defined]

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
def upstream(tmp_path: Path) -> tuple[Path, str, str]:
    """Two commits of one manifest whose memory cap (and therefore declaration) moves."""
    src = tmp_path / "upstream"
    src.mkdir()
    _git(src, "init", "-q", "-b", "main")
    first = _commit(
        src,
        {
            "services/alpha/service.yaml": service_manifest(memory="200M"),
            "apps/site/service.yaml": STATIC_MANIFEST,
            "README.md": "api\n",
        },
        "first",
    )
    second = _commit(
        src,
        {"services/alpha/service.yaml": service_manifest(memory="256M")},
        "second",
    )
    return src, first, second


class Env:
    state: StateDir
    store: RuntimeStore
    secrets: SecretStore
    calls: Calls
    uids: FakeUids
    registry: FakeRegistry
    stopped: str


@pytest.fixture
def env(tmp_path: Path, registry: FakeRegistry, monkeypatch: pytest.MonkeyPatch) -> Env:
    """State dir + store + every host-touching seam replaced by a recorder."""
    state = StateDir(tmp_path / "state")
    state.ensure()
    store = RuntimeStore(tmp_path / "store")
    store.ensure()
    secrets = SecretStore(state.root)  # `.dir` is <root>/secrets; store_for() builds the same
    secrets.set("registry", "REGISTRY_ADMIN_TOKEN", ADMIN_TOKEN.encode())
    secrets.set(SERVICE_ID, "SVC_SECRET", SECRET_VALUE.encode())
    calls = Calls()
    uids = FakeUids()
    e = Env()
    e.stopped = "stopped"

    def fake_stage(
        _self: SourceMirror, sha: str, service_root: Path, _block: UidBlock, **_kw: Any
    ) -> Path:
        repo = Path(service_root) / "repo"
        marker = repo / SHA_MARKER
        current = marker.read_text().strip() if marker.exists() else None
        if current == sha:
            calls.add("stage-noop", Path(service_root).parent.name, sha)
            return repo
        repo.mkdir(parents=True, exist_ok=True)
        marker.write_text(sha + "\n", encoding="utf-8")
        calls.add("stage", Path(service_root).parent.name, sha)
        return repo

    def fake_place_jwt_key(_state: StateDir, service_id: str, _block: UidBlock, **_kw: Any) -> bool:
        calls.add("jwt", service_id)
        return False

    def fake_provision(decl: Any, root: Path, _store: Any, _block: Any, **_kw: Any) -> None:
        python_venv_dir(decl, Path(root)).mkdir(parents=True, exist_ok=True)
        calls.add("provision", decl.id)

    def fake_status(_state: StateDir, **_kw: Any) -> dict[str, Any]:
        calls.add("status")
        pid = None if e.stopped in rollback_mod.DOWN_STATUSES else 4242
        return {"ok": True, "services": {SERVICE_ID: {"status": e.stopped, "pid": pid}}}

    def fake_stop(_state: StateDir, service_id: str, **_kw: Any) -> dict[str, Any]:
        calls.add("stop", service_id)
        return {"ok": True}

    def fake_reload(_state: StateDir, **_kw: Any) -> dict[str, Any]:
        calls.add("reload")
        return {"ok": True, "reload": {"added": [], "changed": [SERVICE_ID], "errors": {}}}

    def fake_restart(_state: StateDir, service_id: str, **_kw: Any) -> dict[str, Any]:
        calls.add("restart", service_id)
        return {"ok": True}

    monkeypatch.setattr(SourceMirror, "stage", fake_stage)
    monkeypatch.setattr(rollback_mod, "place_jwt_key", fake_place_jwt_key)
    monkeypatch.setattr(rollback_mod, "provision", fake_provision)
    monkeypatch.setattr(rollback_mod, "ctl_status", fake_status)
    monkeypatch.setattr(rollback_mod, "ctl_stop", fake_stop)
    monkeypatch.setattr(rollback_mod, "ctl_reload", fake_reload)
    monkeypatch.setattr(rollback_mod, "ctl_restart", fake_restart)
    monkeypatch.setattr(rollback_mod, "uid_allocator", lambda _state: uids)

    e.state, e.store, e.secrets = state, store, secrets
    e.calls, e.uids, e.registry = calls, uids, registry
    return e


# --------------------------------------------------------------------------- helpers


def make_cfg(upstream_path: Path, registry: FakeRegistry, **kw: Any) -> SyncConfig:
    defaults: dict[str, Any] = {
        "repo_url": str(upstream_path),
        "registry_url": registry.url,
        "auth_url": registry.url,
        "health_deadline_s": 3.0,
        "health_interval_s": 0.05,
    }
    defaults.update(kw)
    return SyncConfig(**defaults)


def decl_text_at(env: Env, upstream_path: Path, sha: str) -> str:
    """What the translator produces for alpha at ``sha`` -- the expected file bytes."""
    mirror = SourceMirror(env.store.root, "api", url=str(upstream_path))
    checkout = mirror.materialize(sha)
    ctx = SyncConfig(
        repo_url=str(upstream_path),
        registry_url=env.registry.url,
        auth_url=env.registry.url,
    ).context_for(sha=sha, services_dir=env.state.services_dir)
    manifest = checkout / "services" / SERVICE_ID / "service.yaml"
    return emit_toml(translate(manifest.read_text(encoding="utf-8"), ctx).decl)


def seed(
    env: Env,
    upstream_path: Path,
    sha: str,
    *,
    prev_sha: str | None,
    stage: str = "healthy",
) -> None:
    """Put the fleet in the state a completed sync at ``sha`` would have left."""
    mirror = SourceMirror(env.store.root, "api", url=str(upstream_path))
    mirror.fetch("main")
    checkout = mirror.materialize(sha)
    ctx = SyncConfig(
        repo_url=str(upstream_path),
        registry_url=env.registry.url,
        auth_url=env.registry.url,
    ).context_for(sha=sha, services_dir=env.state.services_dir)
    manifest = checkout / "services" / SERVICE_ID / "service.yaml"
    translation = translate(manifest.read_text(encoding="utf-8"), ctx)

    decl_path = env.state.service_decl_path(SERVICE_ID)
    decl_path.parent.mkdir(parents=True, exist_ok=True)
    decl_path.write_text(emit_toml(translation.decl), encoding="utf-8")

    for directory, payload in (
        (mounts_dir(env.state), translation.mount),
        (registry_dir(env.state), translation.registry),
    ):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{SERVICE_ID}.json").write_text(
            json.dumps(dict(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    repo = env.state.service_root(SERVICE_ID) / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / SHA_MARKER).write_text(sha + "\n", encoding="utf-8")
    python_venv_dir(translation.decl, env.state.service_root(SERVICE_ID)).mkdir(
        parents=True, exist_ok=True
    )

    path = state_path(env.state)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": STATE_VERSION,
                "services": {
                    SERVICE_ID: {
                        "sha": sha,
                        "prev_sha": prev_sha,
                        "deployed_sha": sha,
                        "stage": stage,
                        "error": None,
                        "manual_restart": False,
                        "escalated": False,
                        "updated_at": "2026-09-02T00:00:00Z",
                        "stage_since": "2026-09-02T00:00:00Z",
                    },
                    "site": {
                        "sha": sha,
                        "prev_sha": None,
                        "deployed_sha": sha,
                        "stage": "declared",
                        "error": None,
                        "manual_restart": False,
                        "escalated": False,
                        "updated_at": "2026-09-02T00:00:00Z",
                        "stage_since": "2026-09-02T00:00:00Z",
                        "rolled_back_from": "cafe" * 10,
                    },
                },
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    env.state.ports_state.parent.mkdir(parents=True, exist_ok=True)
    env.state.ports_state.write_text(
        json.dumps(
            {"version": 1, "ports": {SERVICE_ID: {"main": env.registry.port}}},
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def run(env: Env, upstream_path: Path, **kw: Any) -> Any:
    cfg = kw.pop("cfg", None) or make_cfg(upstream_path, env.registry)
    return rollback(
        env.state,
        env.store,
        kw.pop("service_id", SERVICE_ID),
        cfg=cfg,
        secrets=env.secrets,
        uids=env.uids,
        sleep=lambda _s: None,
        **kw,
    )


def read_state(env: Env) -> dict[str, Any]:
    return json.loads(state_path(env.state).read_text())


def snapshot(root: Path) -> dict[str, tuple[int, str]]:
    """Every file under ``root`` as ``{relpath: (size, mtime_ns_as_str)}``."""
    out: dict[str, tuple[int, str]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            st = path.stat()
            out[str(path.relative_to(root))] = (st.st_size, str(st.st_mtime_ns))
    return out


# --------------------------------------------------------------------------- prev_sha


def test_rollback_to_prev_sha_reports_healthy(env: Env, upstream: Any) -> None:
    src, first, second = upstream
    seed(env, src, second, prev_sha=first)

    report = run(env, src)

    assert report.ok, report.error
    assert report.stage == "healthy"
    assert (report.to_sha, report.from_sha) == (first, second)
    assert report.error is None
    assert report.exit_code == 0


def test_the_declaration_on_disk_is_the_one_from_the_older_commit(env: Env, upstream: Any) -> None:
    src, first, second = upstream
    seed(env, src, second, prev_sha=first)
    at_second = env.state.service_decl_path(SERVICE_ID).read_text()

    run(env, src)

    written = env.state.service_decl_path(SERVICE_ID).read_text()
    assert written == decl_text_at(env, src, first)
    assert written != at_second
    # The memory cap is the field the two commits disagree about, so it is the
    # one that proves the manifest was re-read AT the target commit rather than
    # the declaration merely being rewritten from the head.
    assert 'memory_max = "200M"' in written
    assert 'memory_max = "256M"' in at_second
    assert f'GIT_COMMIT = "{first}"' in written


def test_the_record_flips_to_the_target_commit(env: Env, upstream: Any) -> None:
    src, first, second = upstream
    seed(env, src, second, prev_sha=first)

    run(env, src)

    record = read_state(env)["services"][SERVICE_ID]
    assert record["sha"] == first
    assert record["deployed_sha"] == first
    assert record["stage"] == "healthy"
    assert record["error"] is None
    assert record["rolled_back_from"] == second
    assert record["escalated"] is False
    # "the last commit that reached healthy" is now the one in `sha`, so there
    # is no older healthy commit left to name -- and a second rollback must not
    # target the commit we are already on.
    assert record["prev_sha"] is None
    assert record["updated_at"].endswith("Z") and len(record["updated_at"]) == 20
    assert record["stage_since"] == record["updated_at"]


def test_other_services_records_pass_through_untouched(env: Env, upstream: Any) -> None:
    """Including keys this version's dataclass does not declare."""
    src, first, second = upstream
    seed(env, src, second, prev_sha=first)
    before = read_state(env)["services"]["site"]

    run(env, src)

    assert read_state(env)["services"]["site"] == before
    assert before["rolled_back_from"] == "cafe" * 10


def test_the_call_sequence_is_stop_stage_provision_reload_restart(env: Env, upstream: Any) -> None:
    src, first, second = upstream
    seed(env, src, second, prev_sha=first)

    run(env, src)

    assert env.calls.kinds() == [
        "stop",
        "status",
        "stage",
        "jwt",
        "provision",
        "reload",
        "restart",
    ]
    assert env.calls.of("stage") == [("stage", SERVICE_ID, first)]
    assert env.calls.of("restart") == [("restart", SERVICE_ID)]
    assert env.uids.seen == [SERVICE_ID]


def test_the_service_is_stopped_before_its_tree_moves(env: Env, upstream: Any) -> None:
    """D24 (T3.2): a service restarted inside the staging window loses its venv."""
    src, first, second = upstream
    seed(env, src, second, prev_sha=first)

    run(env, src)

    kinds = env.calls.kinds()
    assert kinds.index("stop") < kinds.index("stage")
    assert kinds.index("reload") < kinds.index("restart")


def test_the_sidecars_are_rewritten_at_the_target_commit(env: Env, upstream: Any) -> None:
    src, first, second = upstream
    seed(env, src, second, prev_sha=first)

    run(env, src)

    mount = json.loads((mounts_dir(env.state) / f"{SERVICE_ID}.json").read_text())
    reg = json.loads((registry_dir(env.state) / f"{SERVICE_ID}.json").read_text())
    assert mount["version"] == 1 and mount["id"] == SERVICE_ID
    assert reg["version"] == 1 and reg["audience"] == SERVICE_ID


def test_one_escalation_records_the_rollback(env: Env, upstream: Any, capsys: Any) -> None:
    src, first, second = upstream
    seed(env, src, second, prev_sha=first)

    report = run(env, src)

    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record == report.escalation
    assert record["kind"] == ESCALATION_KIND
    assert record["service_id"] == SERVICE_ID
    assert record["action"] == "log"  # a successful rollback is news, not an alarm
    assert record["event"]["sha"] == first
    assert record["event"]["rolled_back_from"] == second
    assert record["event"]["stage"] == "healthy"


def test_the_report_warns_that_the_next_sync_tick_undoes_it(env: Env, upstream: Any) -> None:
    src, first, second = upstream
    seed(env, src, second, prev_sha=first)

    report = run(env, src)

    assert any("next 'ams platform sync' tick" in w for w in report.warnings)
    assert any(second[:12] in w for w in report.warnings)


# --------------------------------------------------------------------------- --to


def test_an_explicit_target_sha_wins_over_prev_sha(env: Env, upstream: Any) -> None:
    src, first, second = upstream
    seed(env, src, second, prev_sha=None)  # nothing to fall back on

    report = run(env, src, to_sha=first)

    assert report.ok, report.error
    assert report.to_sha == first
    assert read_state(env)["services"][SERVICE_ID]["sha"] == first


def test_an_unknown_target_sha_is_a_precondition_error(env: Env, upstream: Any) -> None:
    src, _first, second = upstream
    seed(env, src, second, prev_sha=None)
    before = snapshot(env.state.root)

    with pytest.raises(RollbackError, match="materialize"):
        run(env, src, to_sha="0" * 40)

    assert snapshot(env.state.root) == before
    assert env.calls.items == []


def test_a_malformed_target_sha_is_refused_before_anything_runs(env: Env, upstream: Any) -> None:
    src, _first, second = upstream
    seed(env, src, second, prev_sha=None)

    with pytest.raises(RollbackError):
        run(env, src, to_sha="not-a-sha")
    assert env.calls.items == []


def test_rolling_back_to_the_current_commit_is_refused(env: Env, upstream: Any) -> None:
    src, first, second = upstream
    seed(env, src, second, prev_sha=first)

    with pytest.raises(RollbackError, match="already being driven"):
        run(env, src, to_sha=second)
    assert env.calls.items == []


# --------------------------------------------------------------------- no prev_sha


def test_no_prev_sha_is_an_error_with_no_side_effects(env: Env, upstream: Any) -> None:
    src, _first, second = upstream
    seed(env, src, second, prev_sha=None)
    before = snapshot(env.state.root)

    with pytest.raises(RollbackError, match="no prev_sha"):
        run(env, src)

    assert snapshot(env.state.root) == before
    assert env.calls.items == []
    assert env.registry.requests == []


def test_an_unknown_service_is_an_error_with_no_side_effects(env: Env, upstream: Any) -> None:
    src, first, second = upstream
    seed(env, src, second, prev_sha=first)
    before = snapshot(env.state.root)

    with pytest.raises(RollbackError, match="no platform record"):
        run(env, src, service_id="nope")

    assert snapshot(env.state.root) == before
    assert env.calls.items == []


def test_a_missing_state_file_is_an_error(env: Env, upstream: Any) -> None:
    src, _first, _second = upstream
    with pytest.raises(RollbackError, match="does not exist"):
        run(env, src)


def test_an_unknown_state_version_fails_loudly(env: Env, upstream: Any) -> None:
    src, first, second = upstream
    seed(env, src, second, prev_sha=first)
    path = state_path(env.state)
    data = json.loads(path.read_text())
    data["version"] = 99
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(RollbackError, match="unsupported version"):
        run(env, src)
    assert env.calls.items == []


def test_a_static_site_has_nothing_to_roll_back(env: Env, upstream: Any) -> None:
    src, first, second = upstream
    seed(env, src, second, prev_sha=first)
    path = state_path(env.state)
    data = json.loads(path.read_text())
    data["services"]["site"]["prev_sha"] = first
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(RollbackError, match="kind='static'"):
        run(env, src, service_id="site")
    assert env.calls.items == []


def test_a_service_absent_from_the_target_commit_is_refused(
    env: Env, upstream: Any, tmp_path: Path
) -> None:
    """Rolling back past the commit that introduced the service."""
    src, _first, second = upstream
    root = tmp_path / "empty-upstream"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    empty = _commit(root, {"README.md": "nothing here\n"}, "no services")
    seed(env, src, second, prev_sha=None)
    # A second mirror under the same store, so `materialize` succeeds and only
    # the manifest lookup can fail.
    SourceMirror(env.store.root, "api2", url=str(root)).fetch("main")
    cfg = make_cfg(root, env.registry, repo_name="api2")

    with pytest.raises(RollbackError, match="no manifest declaring"):
        run(env, src, cfg=cfg, to_sha=empty)
    assert env.calls.items == []


# --------------------------------------------------------------------------- failure


def test_a_health_failure_marks_the_record_failed_and_escalates(
    env: Env, upstream: Any, capsys: Any
) -> None:
    src, first, second = upstream
    seed(env, src, second, prev_sha=first)
    env.registry.set_health(503)

    report = run(env, src, cfg=make_cfg(src, env.registry, health_deadline_s=0.4))

    assert not report.ok
    assert report.stage == "failed"
    assert report.error is not None and report.error.startswith("health:")
    assert report.exit_code == 1
    record = read_state(env)["services"][SERVICE_ID]
    assert record["stage"] == "failed"
    assert record["sha"] == first  # the tree and declaration really did move
    assert record["error"] == report.error
    assert record["rolled_back_from"] == second
    # A failed rollback leaves prev_sha alone: the older healthy commit is still
    # the last one that worked, and nothing has replaced it.
    assert record["prev_sha"] == first

    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert len(lines) == 1
    escalation = json.loads(lines[0])
    assert escalation["kind"] == ESCALATION_KIND
    assert escalation["action"] == "escalate"
    assert escalation["event"]["stage"] == "failed"
    assert "health:" in escalation["reason"]


def test_a_refused_stop_fails_before_the_tree_is_touched(
    env: Env, upstream: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    src, first, second = upstream
    seed(env, src, second, prev_sha=first)
    marker = env.state.service_root(SERVICE_ID) / "repo" / SHA_MARKER

    def refuse(_state: StateDir, service_id: str, **_kw: Any) -> dict[str, Any]:
        env.calls.add("stop", service_id)
        return {"ok": False, "error": "unknown service 'alpha'"}

    monkeypatch.setattr(rollback_mod, "ctl_stop", refuse)
    report = run(env, src)

    assert not report.ok
    assert report.error is not None and report.error.startswith("stop: refused")
    assert marker.read_text().strip() == second  # untouched
    assert env.calls.kinds() == ["stop"]
    assert read_state(env)["services"][SERVICE_ID]["stage"] == "failed"


def test_a_reload_error_for_this_service_fails_the_rollback(
    env: Env, upstream: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    src, first, second = upstream
    seed(env, src, second, prev_sha=first)

    def bad_reload(_state: StateDir, **_kw: Any) -> dict[str, Any]:
        env.calls.add("reload")
        return {"ok": True, "reload": {"errors": {SERVICE_ID: "DeclError: bad argv"}}}

    monkeypatch.setattr(rollback_mod, "ctl_reload", bad_reload)
    report = run(env, src)

    assert not report.ok
    assert report.error == "reload: DeclError: bad argv"
    assert "restart" not in env.calls.kinds()


def test_a_service_parked_waiting_on_a_dependency_counts_as_down(env: Env, upstream: Any) -> None:
    """``waiting`` means the supervisor is holding it back, so no process is running.

    T4.5 added the status: a service whose ``depends_on`` are not healthy yet sits
    at ``waiting`` with no pid. Treating it as neither up nor down would make
    ``_wait_stopped`` spin to its deadline and then warn about a service that was
    never running -- so the poll must return on the first answer.
    """
    src, first, second = upstream
    seed(env, src, second, prev_sha=first)
    env.stopped = "waiting"

    report = run(env, src)

    assert report.ok, report.error
    assert not any("still" in w for w in report.warnings), report.warnings
    assert len(env.calls.of("status")) == 1, "it polled instead of accepting 'waiting'"
    assert env.calls.kinds() == ["stop", "status", "stage", "jwt", "provision", "reload", "restart"]


def test_a_service_that_will_not_stop_is_a_warning_not_a_failure(env: Env, upstream: Any) -> None:
    src, first, second = upstream
    seed(env, src, second, prev_sha=first)
    env.stopped = "running"  # ctl status never reports it down

    report = run(env, src)

    assert report.ok, report.error
    assert any("still 'running'" in w for w in report.warnings)
    assert len(env.calls.of("status")) > 1  # it really polled


# --------------------------------------------------------------------------- reuse


def test_a_matching_tree_is_not_re_provisioned(env: Env, upstream: Any) -> None:
    """The declaration still moves; only the expensive step is skipped."""
    src, first, second = upstream
    seed(env, src, second, prev_sha=first)
    # Pretend the older tree is already on disk with its venv built.
    (env.state.service_root(SERVICE_ID) / "repo" / SHA_MARKER).write_text(
        first + "\n", encoding="utf-8"
    )

    report = run(env, src)

    assert report.ok, report.error
    assert env.calls.kinds() == ["stop", "status", "stage-noop", "jwt", "reload", "restart"]
    assert "provision" not in report.actions
    assert env.state.service_decl_path(SERVICE_ID).read_text() == decl_text_at(env, src, first)


# --------------------------------------------------------------------------- secrets


def _all_output(env: Env, report: Any, capsys: Any) -> str:
    captured = capsys.readouterr()
    return "\n".join(
        [
            captured.out,
            captured.err,
            json.dumps(report.escalation),
            report.summary(),
            "\n".join(report.warnings),
            repr(report),
            state_path(env.state).read_text(),
            env.state.service_decl_path(SERVICE_ID).read_text(),
        ]
    )


def test_no_secret_value_reaches_any_output_on_success(
    env: Env, upstream: Any, capsys: Any, caplog: Any
) -> None:
    src, first, second = upstream
    seed(env, src, second, prev_sha=first)

    report = run(env, src)

    haystack = _all_output(env, report, capsys) + "\n".join(r.getMessage() for r in caplog.records)
    assert SECRET_VALUE not in haystack
    assert ADMIN_TOKEN not in haystack
    # The declaration lists the name, which is the whole point of D16.
    assert "SVC_SECRET" in env.state.service_decl_path(SERVICE_ID).read_text()


def test_no_secret_value_reaches_any_output_on_failure(
    env: Env, upstream: Any, capsys: Any, caplog: Any
) -> None:
    src, first, second = upstream
    seed(env, src, second, prev_sha=first)
    env.registry.set_health(500)

    report = run(env, src, cfg=make_cfg(src, env.registry, health_deadline_s=0.3))

    haystack = _all_output(env, report, capsys) + "\n".join(r.getMessage() for r in caplog.records)
    assert not report.ok
    assert SECRET_VALUE not in haystack
    assert ADMIN_TOKEN not in haystack


def test_an_unset_svc_secret_stops_the_rollback_before_the_restart(env: Env, upstream: Any) -> None:
    src, first, second = upstream
    seed(env, src, second, prev_sha=first)
    env.secrets.remove(SERVICE_ID, "SVC_SECRET")

    report = run(env, src)

    assert not report.ok
    assert report.error is not None and "SVC_SECRET" in report.error
    assert "restart" not in env.calls.kinds()
    # Nothing generated a replacement: a rollback restores code, not identity.
    assert env.secrets.names(SERVICE_ID) == []


def test_the_rollback_never_calls_the_registry_admin_api(env: Env, upstream: Any) -> None:
    src, first, second = upstream
    seed(env, src, second, prev_sha=first)

    run(env, src)

    assert all(method == "GET" for method, _path in env.registry.requests)
    assert all(path.endswith("/health") for _method, path in env.registry.requests)


# --------------------------------------------------------------------------- data dir


def test_the_data_dir_is_never_touched(env: Env, upstream: Any) -> None:
    src, first, second = upstream
    seed(env, src, second, prev_sha=first)
    data = env.state.service_root(SERVICE_ID) / "data"
    data.mkdir(parents=True, exist_ok=True)
    db = data / "alpha.db"
    db.write_bytes(b"rows the newer code migrated")
    before = snapshot(data)

    run(env, src)

    assert snapshot(data) == before
    assert db.read_bytes() == b"rows the newer code migrated"


# --------------------------------------------------------------------------- cli


def test_the_module_is_runnable_and_reports_a_precondition_separately(
    env: Env, upstream: Any, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    src, _first, second = upstream
    seed(env, src, second, prev_sha=None)
    monkeypatch.setenv("AMS_STATE_DIR", str(env.state.root))
    monkeypatch.setenv("AMS_STORE_DIR", str(env.store.root))

    code = main([SERVICE_ID, "--repo", str(src), "--registry-url", env.registry.url])

    assert code == rollback_mod.EXIT_PRECONDITION
    assert "no prev_sha" in capsys.readouterr().err


def test_the_module_rolls_back_from_the_command_line(
    env: Env, upstream: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    src, first, second = upstream
    seed(env, src, second, prev_sha=first)
    monkeypatch.setenv("AMS_STATE_DIR", str(env.state.root))
    monkeypatch.setenv("AMS_STORE_DIR", str(env.store.root))

    code = main(
        [
            SERVICE_ID,
            "--repo",
            str(src),
            "--registry-url",
            env.registry.url,
            "--auth-url",
            env.registry.url,
        ]
    )

    assert code == 0
    assert read_state(env)["services"][SERVICE_ID]["sha"] == first


def test_load_state_doc_keeps_keys_this_version_does_not_declare(env: Env, upstream: Any) -> None:
    src, first, second = upstream
    seed(env, src, second, prev_sha=first)

    doc = load_state_doc(state_path(env.state))

    assert doc["services"]["site"]["rolled_back_from"] == "cafe" * 10
