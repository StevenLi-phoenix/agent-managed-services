"""Live check of the pool runner asset against a real venv and real uvicorn.

Builds a shared venv from two tiny fixture apps (``tests/fixtures/pool-members``)
with ``uv venv`` + ``uv pip install -e`` (not ``ams.runtime.provision`` -- this
test is about the runner's own behaviour, not ams's isolation, so it does not
need a user namespace or a delegated cgroup for the *provisioning* step; only
the runner subprocess itself is spawned plainly), writes a ``pool.json`` next
to a copy of ``src/ams/platform/assets/pool_runner.py``, and runs the real
thing end to end: two members on two ports, a failing member skipped without
taking the other down, and a clean ``SIGTERM``.

Run with ``scripts/remote-test.sh t2-pool-runner tests/linux/test_pool_runner_live.py``.
"""

from __future__ import annotations

import json
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.linux

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ASSET_PATH = REPO_ROOT / "src" / "ams" / "platform" / "assets" / "pool_runner.py"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "pool-members"

STORE_ROOT = Path("/home/harness/store")
UV = str(Path.home() / ".local" / "bin" / "uv")

READY_TIMEOUT_S = 60.0
STOP_TIMEOUT_S = 10.0


# --------------------------------------------------------------------------- provisioning


def _uv_env() -> dict[str, str]:
    """Reuse the harness's real uv caches (network-cheap re-runs); no reflink
    sharing claim is made or needed here -- this venv is throwaway per test."""
    return {
        "PATH": f"{Path.home() / '.local' / 'bin'}:/usr/local/bin:/usr/bin:/bin",
        "HOME": str(Path.home()),
        "UV_CACHE_DIR": str(STORE_ROOT / "uv-cache"),
        "UV_PYTHON_INSTALL_DIR": str(STORE_ROOT / "python"),
        "UV_PYTHON_PREFERENCE": "only-managed",
    }


def _run_uv(args: list[str], *, cwd: Path) -> None:
    proc = subprocess.run(
        [UV, *args], cwd=str(cwd), env=_uv_env(), capture_output=True, text=True, timeout=300
    )
    assert proc.returncode == 0, f"uv {args}: {proc.stdout}\n{proc.stderr}"


@pytest.fixture(scope="module")
def pool_venv(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One shared venv for the whole module: uvicorn pinned + both members editable."""
    venv = tmp_path_factory.mktemp("pool-venv") / ".venv"
    _run_uv(["venv", "-q", "--python", "3.12", str(venv)], cwd=REPO_ROOT)
    python = venv / "bin" / "python"
    _run_uv(
        ["pip", "install", "-q", "--python", str(python), "uvicorn[standard]==0.52.4"],
        cwd=REPO_ROOT,
    )
    _run_uv(
        [
            "pip",
            "install",
            "-q",
            "--python",
            str(python),
            "-e",
            str(FIXTURES / "alpha"),
            "-e",
            str(FIXTURES / "beta"),
        ],
        cwd=REPO_ROOT,
    )
    return venv


# --------------------------------------------------------------------------- pool.json


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _member_doc(member_id: str, port_env: str) -> dict[str, Any]:
    return {
        "id": member_id,
        "app": f"{member_id}.main:build_app",
        "factory": True,
        "port_env": port_env,
        "health_path": "/health",
        "secret_env": {},
        "env": {
            "SVC_NAME": member_id,
            "SVC_AUDIENCE": member_id,
            "SVC_HEALTH_PATH": "/health",
            # A shared, non-identity key: exercises the process-env union
            # (PLAN-pool §4.2) without meaning anything to the fixture apps.
            "POOL_SHARED_MARKER": "shared-value",
        },
    }


@pytest.fixture
def pool_root(tmp_path: Path) -> Path:
    root = tmp_path / "pool-root"
    root.mkdir()
    shutil.copy(ASSET_PATH, root / "pool_runner.py")
    return root


# --------------------------------------------------------------------------- process handle


class _StreamReader:
    """Continuously drains a text-mode pipe into a thread-safe line buffer."""

    def __init__(self, fh: Any) -> None:
        self._lines: list[str] = []
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, args=(fh,), daemon=True)
        self._thread.start()

    def _run(self, fh: Any) -> None:
        for line in fh:
            with self._lock:
                self._lines.append(line)

    def snapshot(self) -> list[str]:
        with self._lock:
            return list(self._lines)


@dataclass
class RunningPool:
    proc: subprocess.Popen[str]
    admin_port: int
    member_ports: dict[str, int]
    stderr: _StreamReader
    stdout: _StreamReader

    def health(self, member_id: str, timeout_s: float = 2.0) -> dict[str, Any]:
        url = f"http://127.0.0.1:{self.member_ports[member_id]}/health"
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:
            return json.loads(resp.read().decode())

    def admin_health(self, timeout_s: float = 2.0) -> tuple[int, dict[str, Any]]:
        url = f"http://127.0.0.1:{self.admin_port}/_pool/health"
        try:
            with urllib.request.urlopen(url, timeout=timeout_s) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def stderr_text(self) -> str:
        return "".join(self.stderr.snapshot())

    def stop(self, timeout_s: float = STOP_TIMEOUT_S) -> int:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
        try:
            return self.proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
            raise


@pytest.fixture
def run_pool(pool_venv: Path, pool_root: Path):
    started: list[RunningPool] = []

    def _start(
        members: list[dict[str, Any]], *, extra_env: dict[str, str] | None = None
    ) -> RunningPool:
        (pool_root / "pool.json").write_text(
            json.dumps({"version": 1, "pool": "core", "members": members}), encoding="utf-8"
        )
        admin_port = _free_port()
        env = {
            "PATH": f"{pool_venv / 'bin'}:/usr/bin:/bin",
            "HOME": str(Path.home()),
            "POOL_PORT_ADMIN": str(admin_port),
        }
        member_ports: dict[str, int] = {}
        for member in members:
            port = _free_port()
            member_ports[member["id"]] = port
            env[member["port_env"]] = str(port)
        if extra_env:
            env.update(extra_env)

        proc = subprocess.Popen(
            [str(pool_venv / "bin" / "python"), str(pool_root / "pool_runner.py")],
            cwd=str(pool_root),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        handle = RunningPool(
            proc=proc,
            admin_port=admin_port,
            member_ports=member_ports,
            stderr=_StreamReader(proc.stderr),
            stdout=_StreamReader(proc.stdout),
        )
        started.append(handle)
        return handle

    yield _start

    for handle in started:
        if handle.proc.poll() is None:
            handle.proc.send_signal(signal.SIGTERM)
            try:
                handle.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                handle.proc.kill()
                handle.proc.wait(timeout=5)


def _wait_until(
    predicate: Any, *, timeout_s: float = READY_TIMEOUT_S, interval_s: float = 0.1
) -> Any:
    deadline = time.monotonic() + timeout_s
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            result = predicate()
        except Exception as exc:  # noqa: BLE001 - retry loop, re-raised on timeout
            last_exc = exc
        else:
            if result:
                return result
        time.sleep(interval_s)
    if last_exc is not None:
        raise AssertionError(f"condition never became true: {last_exc}") from last_exc
    raise AssertionError("condition never became true")


# --------------------------------------------------------------------------- tests


@pytest.mark.timeout(120)
def test_two_members_serve_their_own_identity_at_build_and_lifespan(
    run_pool: Any,
) -> None:
    members = [
        _member_doc("alpha", "POOL_PORT_ALPHA"),
        _member_doc("beta", "POOL_PORT_BETA"),
    ]
    pool = run_pool(members)

    alpha_body = _wait_until(lambda: pool.health("alpha"))
    beta_body = _wait_until(lambda: pool.health("beta"))

    # T0 finding-1 regression: a member re-reading load_from_env() inside its
    # own lifespan must see its OWN identity, not a neighbour's.
    assert alpha_body == {"svc": "alpha", "lifespan": "alpha"}
    assert beta_body == {"svc": "beta", "lifespan": "beta"}

    status, body = pool.admin_health()
    assert status == 200
    assert sorted(body["ok"]) == ["alpha", "beta"]
    assert body["failed"] == {}
    assert body["pool"] == "core"

    exit_code = pool.stop()
    assert exit_code == 0


@pytest.mark.timeout(120)
def test_beta_build_failure_is_skipped_alpha_keeps_serving(run_pool: Any) -> None:
    members = [
        _member_doc("alpha", "POOL_PORT_ALPHA"),
        _member_doc("beta", "POOL_PORT_BETA"),
    ]
    pool = run_pool(members, extra_env={"BETA_BREAK": "1"})

    alpha_body = _wait_until(lambda: pool.health("alpha"))
    assert alpha_body == {"svc": "alpha", "lifespan": "alpha"}

    status, body = _wait_until(lambda: _admin_shows_failed(pool, "beta"))
    assert status == 200
    assert body["ok"] == ["alpha"]
    assert "beta" in body["failed"]

    # The process must still be alive: one member's bad build is not a pool outage.
    assert pool.proc.poll() is None

    stderr_lines = pool.stderr_text().splitlines()
    error_lines = [line for line in stderr_lines if "ERROR [beta]" in line]
    assert len(error_lines) == 1, stderr_lines
    assert "build failed" in error_lines[0]

    exit_code = pool.stop()
    assert exit_code == 0


@pytest.mark.timeout(120)
def test_beta_lifespan_failure_raises_systemexit_and_is_caught(run_pool: Any) -> None:
    members = [
        _member_doc("alpha", "POOL_PORT_ALPHA"),
        _member_doc("beta", "POOL_PORT_BETA"),
    ]
    pool = run_pool(members, extra_env={"BETA_BREAK": "lifespan"})

    alpha_body = _wait_until(lambda: pool.health("alpha"))
    assert alpha_body == {"svc": "alpha", "lifespan": "alpha"}

    status, body = _wait_until(lambda: _admin_shows_failed(pool, "beta"))
    assert status == 200
    assert body["ok"] == ["alpha"]
    assert "beta" in body["failed"]

    # The single most important assertion in this task (PLAN-pool §8 T2): a
    # SystemExit raised inside one member's lifespan startup must not
    # propagate out of the process.
    assert pool.proc.poll() is None

    stderr_lines = pool.stderr_text().splitlines()
    error_lines = [line for line in stderr_lines if "ERROR [beta]" in line]
    assert len(error_lines) == 1, stderr_lines
    assert "startup failed" in error_lines[0]

    exit_code = pool.stop()
    assert exit_code == 0


@pytest.mark.timeout(120)
def test_sigterm_stops_within_the_stop_timeout(run_pool: Any) -> None:
    members = [_member_doc("alpha", "POOL_PORT_ALPHA")]
    pool = run_pool(members)
    _wait_until(lambda: pool.health("alpha"))

    started = time.monotonic()
    exit_code = pool.stop(timeout_s=STOP_TIMEOUT_S)
    elapsed = time.monotonic() - started

    assert exit_code == 0
    assert elapsed < STOP_TIMEOUT_S


@pytest.mark.timeout(60)
def test_runner_refuses_a_stubbed_uvicorn_missing_the_internal_api(
    pool_venv: Path, pool_root: Path
) -> None:
    """§4.4: the runner must fail loudly, before binding anything, against an
    uvicorn build missing the internal API it pins to."""
    stub_venv = pool_root.parent / "stub-venv"
    _run_uv(["venv", "-q", "--python", "3.12", str(stub_venv)], cwd=REPO_ROOT)
    site_packages = next((stub_venv / "lib").glob("python3.*")) / "site-packages"
    (site_packages / "uvicorn").mkdir(parents=True)
    (site_packages / "uvicorn" / "__init__.py").write_text(
        "__version__ = '0.30.0'\n"
        "class Config:\n"
        "    def load(self): ...\n"
        "    @property\n"
        "    def lifespan_class(self):\n"
        "        return None\n"
        "class Server:\n"
        "    def __init__(self, config):\n"
        "        self.config = config\n"
        "    def startup(self): ...\n"
        "    def main_loop(self): ...\n"
        "    def shutdown(self): ...\n"
        # `lifespan` is deliberately not defined anywhere on this stub.
        "\n",
        encoding="utf-8",
    )

    members = [_member_doc("alpha", "POOL_PORT_ALPHA")]
    (pool_root / "pool.json").write_text(
        json.dumps({"version": 1, "pool": "core", "members": members}), encoding="utf-8"
    )
    env = {
        "PATH": f"{stub_venv / 'bin'}:/usr/bin:/bin",
        "HOME": str(Path.home()),
        "POOL_PORT_ADMIN": str(_free_port()),
        "POOL_PORT_ALPHA": str(_free_port()),
    }
    proc = subprocess.run(
        [str(stub_venv / "bin" / "python"), str(pool_root / "pool_runner.py")],
        cwd=str(pool_root),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode != 0
    pattern = r"FATAL pool: uvicorn 0\.30\.0 lacks Server\.lifespan"
    assert re.search(pattern, proc.stderr), proc.stderr


@pytest.mark.timeout(60)
def test_unknown_pool_json_version_is_a_clear_non_zero_exit(
    pool_venv: Path, pool_root: Path
) -> None:
    (pool_root / "pool.json").write_text(
        json.dumps({"version": 2, "members": []}), encoding="utf-8"
    )
    proc = subprocess.run(
        [str(pool_venv / "bin" / "python"), str(pool_root / "pool_runner.py")],
        cwd=str(pool_root),
        env={"PATH": f"{pool_venv / 'bin'}:/usr/bin:/bin", "HOME": str(Path.home())},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode != 0
    assert "version" in proc.stderr
    assert "FATAL" in proc.stderr


def _admin_shows_failed(pool: RunningPool, member_id: str) -> tuple[int, dict[str, Any]] | None:
    status, body = pool.admin_health()
    if member_id in body.get("failed", {}):
        return status, body
    return None
