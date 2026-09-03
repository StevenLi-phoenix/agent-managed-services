"""Platform end-to-end on the real host: four injected failures and one rollback.

Everything below runs against a **self-contained fleet** in this test's own
state dir -- its own supervisor, its own control socket, its own uid blocks,
ports and cgroups. It never touches the live harness, the live state dir or the
live control socket. The only thing it shares with the box is the reflink store
(``/home/harness/store``), and only for the warm uv cache and the already
installed managed interpreter; its source mirror and canonical checkouts are
namespaced by a pid-derived repo name and removed at the end.

The claim under test is the one the portable suite structurally cannot make:
that a failure in one service is contained. So every injection is asserted twice
-- once on the failing service's own state record, and once on the *pids* of the
services that had nothing to do with it. A pid that did not change is proof that
no restart happened, which no amount of reading our own bookkeeping establishes.

The four injections (PLAN-allin T4.3):

a. **A service is SIGKILLed during the sync.** ``beta`` declares ``restart: no``
   and is killed the moment the reload starts it, before the health gate reaches
   it (``alpha`` is deliberately slow to bind, so the gate cannot get there
   first). It stays dead and the gate times out: one escalation, ``beta`` at
   ``failed``, the other three healthy.
b. **A manifest is corrupted in a later commit.** ``gamma``'s ``deploy.install``
   stops being the one recognised ``cd <dir> && uv sync`` form, so it fails at
   *translate* -- before anything is written -- and keeps running the code it
   already had. The commit's other services are untouched (D26).
c. **The run is pointed at a ref that does not exist.** The fetch fails before
   the state file is opened: one run-level escalation and a byte-identical
   ``state.json``.
d. **A commit pushes a service past its memory cap.** ``hog`` gets a 32M
   ``memory_max`` and an ``app.py`` that touches 200 MB, so the cgroup OOM-kills
   it and the health gate fails. ``rollback("hog")`` then re-points it at the
   commit it was last healthy at and the gate goes green.

**Process topology, and why it is not negotiable.** The supervisor lives in the
pytest process (as ``tests/linux/test_e2e.py`` does) and the sync and the
rollback run as *separate processes*, which is exactly how production is wired:
``ams platform sync`` is a one-shot process on a timer that talks to the harness
over the control socket (D17). The first version of this file ran the sync in a
thread instead, and it failed on the box for a reason worth recording: staging
and provisioning go through ``ams.userns.run_admin``, which is a bare
``os.fork()``. Forking a *multi-threaded* process gives the child only the
forking thread, so a lock another thread happened to hold is held forever in the
child -- ``rm -rf``, ``mv`` and ``uv python install`` hung and were SIGKILLed by
their own timeouts (``rc=-9``, no stderr). Nothing here may add a thread to the
pytest process; that is why even the fake registry is a subprocess.

The fleet's services are plain ``python3`` HTTP servers committed into the fake
repo, but they are declared through **real** ``service.yaml`` manifests run
through the real translator -- which always emits ``runtime.kind = "uv"`` with
``sync = true`` (PLAN-allin Q2), so ``provision`` really runs ``uv sync
--frozen`` on the box. Hence the committed uv project under
``tests/fixtures/uv-e2e-app``.

Run with::

    scripts/remote-test.sh ams-e2e tests/linux/test_platform_e2e.py
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from ams.cli import Assembly, build_supervisor
from ams.control import ControlServer, control_socket_path
from ams.reload import drain_pending_removals, reload
from ams.runtime import RuntimeStore
from ams.secrets import SecretStore
from ams.state import StateDir
from ams.supervisor import Supervisor
from ams.userns import remove_service_root

# The scenario builds a repository, provisions four uv projects, starts them and
# drives four sync runs plus a rollback on one vCPU. It is charged to the first
# test, so the module needs far more than the 60 s default.
pytestmark = [pytest.mark.linux, pytest.mark.timeout(900)]

#: The reflink store. Shared with the live harness on purpose -- for the warm uv
#: cache and the already-installed managed 3.12 only. Everything this module
#: creates in it is under a pid-derived name and is removed in teardown.
STORE_ROOT = Path(os.environ.get("AMS_STORE_DIR", "/home/harness/store"))
FIXTURE_UV_PROJECT = Path(__file__).resolve().parents[1] / "fixtures" / "uv-e2e-app"
SRC_DIR = Path(__file__).resolve().parents[2] / "src"

PUMP_SLICE_S = 0.05
#: Bounds one sync run. Generous: four ``uv sync`` calls on one core.
SYNC_TIMEOUT_S = 300.0
HEALTH_DEADLINE_S = 8.0
#: How long ``alpha`` waits before binding. It has to exceed the time this test
#: needs to notice ``beta`` was started and kill it, or injection (a) races the
#: health gate instead of preceding it.
ALPHA_START_DELAY_S = 4

SERVICES = ("alpha", "beta", "gamma", "hog")
#: Layer 0 for this fleet: declared by hand, never touched by the sync loop.
REGISTRY_ID = "registry"

_GIT_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_AUTHOR_NAME": "ams tests",
    "GIT_AUTHOR_EMAIL": "ams@example.invalid",
    "GIT_COMMITTER_NAME": "ams tests",
    "GIT_COMMITTER_EMAIL": "ams@example.invalid",
    "LC_ALL": "C",
}


# --------------------------------------------------------------------------- sources


APP_PY = '''\
"""A service small enough to be a fixture and real enough to be probed.

argv: PORT BALLAST_MB DELAY_S
"""
import http.server
import socketserver
import sys
import time

PORT = int(sys.argv[1])
BALLAST_MB = int(sys.argv[2])
DELAY_S = int(sys.argv[3])

if DELAY_S:
    time.sleep(DELAY_S)

if BALLAST_MB:
    # Touched, not merely reserved: writing every page makes the cgroup OOM kill
    # immediate and independent of the host's overcommit setting.
    _ballast = bytearray(BALLAST_MB * 1024 * 1024)
    for _i in range(0, len(_ballast), 4096):
        _ballast[_i] = 1


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b\'{"status": "ok"}\'
        self.send_response(200 if self.path == "/health" else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


socketserver.TCPServer.allow_reuse_address = True
with socketserver.TCPServer(("127.0.0.1", PORT), Handler) as httpd:
    print("INFO listening on %d" % PORT, flush=True)
    httpd.serve_forever()
'''


REGISTRY_SRC = """\
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        return

    def _respond(self, code):
        body = b'{}'
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get('Content-Length') or 0)
        if length:
            self.rfile.read(length)
        self._respond(201 if self.path.startswith('/api/services') else 200)

    def do_GET(self):
        self._respond(200)


print('INFO registry listening', flush=True)
HTTPServer(('127.0.0.1', int(sys.argv[1])), Handler).serve_forever()
"""

#: The registry is a *service* in this fleet, not a helper thread or a helper
#: process, because the translator makes every Layer-1 declaration
#: ``depends_on = ["registry"]`` and the supervisor will not spawn a dependent
#: until that id is registered **and healthy**. Declaring it by hand (rather
#: than through a manifest) keeps it out of the sync loop's way -- it is Layer 0,
#: which `bootstrap` owns in production -- and passing the source with ``-c``
#: means the mapped service uid needs to read nothing but the interpreter.
REGISTRY_TOML = """\
id = "registry"
[start]
argv = ["/usr/bin/python3", "-c", %s, "${PORT_main}"]
[ports]
main = 0
[health]
kind = "http"
port = "main"
path = "/health"
interval_s = 0.5
timeout_s = 2.0
start_period_s = 1.0
[limits]
memory_max = "64M"
pids_max = 32
[stop]
timeout_s = 3.0
[runtime]
kind = "none"
"""


RUNNER_PY = '''\
"""One sync tick or one rollback, in its own process, reporting as JSON.

Reads a request file, runs the real library call, writes the report to
``request["out"]`` **last** -- so the test detects completion by that file
appearing rather than by reaping the process. It cannot reap: the supervisor in
the test process is a child subreaper and its ``waitpid(-1)`` may collect this
process before ``Popen.wait`` does.
"""
import json
import sys
from pathlib import Path

from ams.platform.rollback import RollbackError, rollback
from ams.platform.sync import SyncConfig, sync
from ams.runtime import RuntimeStore
from ams.state import StateDir

request = json.loads(Path(sys.argv[1]).read_text())
state = StateDir(Path(request["state"]))
store = RuntimeStore(Path(request["store"]))
cfg = SyncConfig(**request["cfg"])
out = {"op": request["op"]}


def jsonable(value):
    return json.loads(json.dumps(value, default=str))


try:
    if request["op"] == "sync":
        report = sync(state, store, cfg)
        out.update(
            error=report.error,
            sha=report.sha,
            reloaded=report.reloaded,
            exit_code=report.exit_code,
            services=[
                {
                    "id": o.id,
                    "stage": o.stage,
                    "sha": o.sha,
                    "prev_sha": o.prev_sha,
                    "error": o.error,
                    "changed": o.changed,
                    "actions": list(o.actions),
                }
                for o in report.services
            ],
            escalations=[jsonable(e) for e in report.escalations],
        )
    else:
        report = rollback(state, store, request["id"], cfg=cfg, to_sha=request.get("to"))
        out.update(
            ok=report.ok,
            to_sha=report.to_sha,
            from_sha=report.from_sha,
            stage=report.stage,
            error=report.error,
            actions=list(report.actions),
            warnings=list(report.warnings),
            exit_code=report.exit_code,
            escalations=[jsonable(report.escalation)],
        )
except RollbackError as e:
    out.update(precondition=f"{type(e).__name__}: {e}")
except BaseException as e:
    out.update(fatal=f"{type(e).__name__}: {e}")
Path(request["out"]).write_text(json.dumps(out))
'''


def manifest(
    name: str,
    *,
    memory: str = "64M",
    restart: str = "on-failure",
    ballast_mb: int = 0,
    delay_s: int = 0,
    install: str | None = None,
) -> str:
    """A real ``service.yaml`` the real translator accepts."""
    return "\n".join(
        [
            "schema_version: 1",
            "kind: service",
            f"name: {name}",
            f"audience: {name}",
            f"display_name: {name.title()} E2E",
            "owner: ams-e2e",
            "",
            "deploy:",
            "  source:",
            "    type: git",
            "    repo: ams/e2e",
            "    branch: main",
            "  install:",
            f"    - {install or f'cd services/{name} && uv sync'}",
            f"  target_dir: /srv/{name}",
            f"  user: {name}",
            "",
            "process:",
            f"  exec: /usr/bin/python3 app.py ${{PORT}} {ballast_mb} {delay_s}",
            f"  working_dir: /srv/{name}/services/{name}",
            "  environment:",
            f"    SVC_ROOT_PATH: /{name}",
            # Quoted: the subset parser refuses a bare `no` as a YAML 1.1 boolean.
            f'  restart: "{restart}"',
            "  restart_sec: 1",
            f"  memory_max: {memory}",
            "",
            "mount:",
            "  gateway: e2e.invalid",
            f"  path: /{name}",
            "  port: 9000",
            "",
            "registry:",
            f"  capabilities: [{name}]",
            "  health_path: /health",
            "",
        ]
    )


# --------------------------------------------------------------------------- git


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), env={**_GIT_ENV, "HOME": str(cwd)}, capture_output=True
    )
    if proc.returncode != 0:  # pragma: no cover - a broken fixture
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


def _service_files(name: str, **kw: Any) -> dict[str, str]:
    """One service's whole subtree: manifest, app, and the uv project."""
    base = f"services/{name}/"
    return {
        base + "service.yaml": manifest(name, **kw),
        base + "app.py": APP_PY,
        base + "pyproject.toml": (FIXTURE_UV_PROJECT / "pyproject.toml").read_text(),
        base + "uv.lock": (FIXTURE_UV_PROJECT / "uv.lock").read_text(),
    }


# --------------------------------------------------------------------------- scenario


@dataclass
class Phase:
    """One sync run (or the rollback), plus what the fleet looked like after it."""

    name: str
    report: dict[str, Any]
    stages: dict[str, str] = field(default_factory=dict)
    errors: dict[str, str | None] = field(default_factory=dict)
    records: dict[str, dict[str, Any]] = field(default_factory=dict)
    pids: dict[str, int | None] = field(default_factory=dict)
    state_bytes: bytes = b""
    #: Declaration text on disk at the end of this phase. Captured per phase
    #: because the rollback rewrites it, and an assertion reading the file at
    #: test time would be asserting about the wrong commit.
    decls: dict[str, str] = field(default_factory=dict)

    @property
    def escalations(self) -> list[dict[str, Any]]:
        return list(self.report.get("escalations") or ())

    @property
    def escalated_ids(self) -> list[str | None]:
        return [e.get("service_id") for e in self.escalations]


@dataclass
class Observed:
    state: StateDir
    shas: dict[str, str]
    phases: dict[str, Phase]
    supervisor_escalations: list[dict[str, Any]]
    live_pids_after_shutdown: list[int]

    def phase(self, name: str) -> Phase:
        return self.phases[name]


class _Sink:
    """The agent side of the supervisor's contract, kept for teardown assertions."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def escalate(self, event: Any, decision: Any, _ctx: Any) -> None:
        self.records.append(
            {
                "kind": type(event).__name__,
                "service_id": getattr(event, "service_id", None),
                "reason": decision.reason,
                "text": getattr(event, "text", ""),
            }
        )


def _pids(sup: Supervisor) -> dict[str, int | None]:
    return {
        service_id: (st.spawned.pid if (st.spawned is not None and not st.reaped) else None)
        for service_id, st in sup.services.items()
    }


def _alive(pid: int) -> bool:
    try:
        status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
    except OSError:
        return False
    return "State:\tZ" not in status  # a zombie is not a running process


def _pump(asm: Assembly, seconds: float, on_tick: Callable[[], None] | None = None) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        asm.supervisor.run_once(PUMP_SLICE_S)
        drain_pending_removals(asm)
        if on_tick is not None:
            on_tick()


def _drive(
    asm: Assembly,
    runner: Path,
    request: dict[str, Any],
    env: dict[str, str],
    timeout_s: float,
    on_tick: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Run one sync/rollback in its own process while this one is the harness loop."""
    request_path = Path(request["request_path"])
    out = Path(request["out"])
    out.unlink(missing_ok=True)
    request_path.write_text(json.dumps(request), encoding="utf-8")
    proc = subprocess.Popen(  # noqa: S603 - argv list, never a shell string
        [sys.executable, str(runner), str(request_path)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + timeout_s
    try:
        while time.monotonic() < deadline:
            asm.supervisor.run_once(PUMP_SLICE_S)
            drain_pending_removals(asm)
            if on_tick is not None:
                on_tick()
            if out.exists():
                break
        else:
            raise AssertionError(f"{request['op']} did not finish within {timeout_s:.0f}s")
    finally:
        if _alive(proc.pid):
            proc.kill()
    # Let the last exits and log lines land so the recorded pids are settled.
    _pump(asm, 1.0, on_tick)
    report = json.loads(out.read_text(encoding="utf-8"))
    assert "fatal" not in report, report["fatal"]
    return report


def _snapshot(name: str, asm: Assembly, state: StateDir, report: dict[str, Any]) -> Phase:
    from ams.platform.sync import state_path

    try:
        raw = state_path(state).read_bytes()
    except OSError:
        raw = b""
    services = (json.loads(raw) if raw else {}).get("services", {})
    decls: dict[str, str] = {}
    for service_id in SERVICES:
        try:
            decls[service_id] = state.service_decl_path(service_id).read_text(encoding="utf-8")
        except OSError:
            decls[service_id] = ""
    return Phase(
        name=name,
        report=report,
        stages={sid: rec.get("stage") for sid, rec in services.items()},
        errors={sid: rec.get("error") for sid, rec in services.items()},
        records={sid: dict(rec) for sid, rec in services.items()},
        pids=_pids(asm.supervisor),
        state_bytes=raw,
        decls=decls,
    )


@pytest.fixture(scope="module")
def observed() -> Iterator[Observed]:  # noqa: C901 - one linear scenario reads better whole
    base = Path(os.environ.get("AMS_STATE_DIR", str(Path.home() / "state")))
    root = base / f"fleet-{os.getpid()}"
    repo_name = f"e2e{os.getpid()}"
    store = RuntimeStore(STORE_ROOT)
    store.ensure()
    root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ sources
    upstream = root / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "main")
    shas: dict[str, str] = {}
    shas["c1"] = _commit(
        upstream,
        {
            "README.md": "ams e2e fleet\n",
            # alpha binds late on purpose: injection (a) has to kill beta before
            # the health gate can reach it, and the gate starts with alpha.
            **_service_files("alpha", delay_s=ALPHA_START_DELAY_S),
            # `restart: no` so the SIGKILL stays a kill -- the injection is about
            # a service that does NOT come back on its own.
            **_service_files("beta", restart="no"),
            **_service_files("gamma"),
            **_service_files("hog"),
        },
        "the fleet",
    )
    _git(upstream, "branch", "at-c1")
    shas["c2"] = _commit(upstream, {"README.md": "docs only\n"}, "docs only")
    shas["c3"] = _commit(
        upstream,
        {"services/gamma/service.yaml": manifest("gamma", install="make install")},
        "break gamma's manifest",
    )
    _git(upstream, "branch", "at-c3")
    shas["c4"] = _commit(
        upstream,
        {**_service_files("hog", memory="32M", ballast_mb=200)},
        "hog outgrows its cap",
    )

    # ------------------------------------------------------------- the harness
    runner = root / "runner.py"
    runner.write_text(RUNNER_PY, encoding="utf-8")
    child_env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/home/harness"),
        "PYTHONPATH": str(SRC_DIR),
        "AMS_STATE_DIR": str(root),
        "AMS_STORE_DIR": str(store.root),
        "LC_ALL": "C",
    }

    state = StateDir(root)
    state.ensure()
    SecretStore(state.root).set("registry", "REGISTRY_ADMIN_TOKEN", b"e2e-admin-token")
    registry_decl = state.service_decl_path(REGISTRY_ID)
    registry_decl.parent.mkdir(parents=True, exist_ok=True)
    registry_decl.write_text(REGISTRY_TOML % json.dumps(REGISTRY_SRC), encoding="utf-8")

    sink = _Sink()
    # `CgroupRoot.discover` moves *this* process into `<delegated>/harness/` and
    # then enables controllers on the delegated root -- which cgroup v2 refuses
    # (EBUSY, "no internal processes") while any process still sits directly in
    # it. So nothing may be forked before this call; anything forked after it
    # inherits `harness/` and is fine.
    asm = build_supervisor(state, isolation=True, escalation=sink)
    server = ControlServer(
        control_socket_path(state), asm.supervisor, reload_fn=lambda: reload(asm)
    )
    server.open()
    asm.start_all()

    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline and not asm.supervisor.services[REGISTRY_ID].healthy:
        asm.supervisor.run_once(PUMP_SLICE_S)
    assert asm.supervisor.services[REGISTRY_ID].healthy, "the fleet's registry never came up"
    registry_url = f"http://127.0.0.1:{asm.ports.get(REGISTRY_ID)['main']}"

    cfg = {
        "repo_url": str(upstream),
        "registry_url": registry_url,
        "auth_url": registry_url,
        "repo_name": repo_name,
        "health_deadline_s": HEALTH_DEADLINE_S,
        "health_interval_s": 0.25,
        # This fleet's own caps, not the plan's: these services are 15 MB
        # interpreters, and `hog` has to be able to declare a 32M ceiling.
        "default_memory_max": "64M",
        "memory_floor": "16M",
        "cpu_max": "90%",
        "start_period_s": 2.0,
    }
    ticket = [0]

    def drive(op: str, on_tick: Callable[[], None] | None = None, **extra: Any) -> dict[str, Any]:
        ticket[0] += 1
        request = {
            "op": op,
            "state": str(root),
            "store": str(store.root),
            "cfg": {**cfg, **extra.pop("cfg", {})},
            "request_path": str(root / f"req-{ticket[0]}.json"),
            "out": str(root / f"out-{ticket[0]}.json"),
            **extra,
        }
        return _drive(asm, runner, request, child_env, SYNC_TIMEOUT_S, on_tick)

    phases: dict[str, Phase] = {}
    killed: dict[str, int | None] = {"pid": None}

    def kill_beta_once() -> None:
        if killed["pid"] is not None:
            return
        row = asm.supervisor.services.get("beta")
        if row is None or row.spawned is None or row.reaped:
            return
        killed["pid"] = row.spawned.pid
        os.kill(row.spawned.pid, signal.SIGKILL)

    try:
        # ------------------------------------------------------------ (a) kill
        report = drive("sync", kill_beta_once, cfg={"ref": "at-c1"})
        phases["kill"] = _snapshot("kill", asm, state, report)
        assert killed["pid"] is not None, "beta was never started, so nothing was killed"

        # ------------------------------------------------ (b) corrupt manifest
        # Every commit already exists; each phase names the one it wants, so an
        # injection moves exactly the services that commit range touched (D26).
        report = drive("sync", cfg={"ref": "at-c3"})
        phases["corrupt"] = _snapshot("corrupt", asm, state, report)

        # ------------------------------------------------------- (c) bad target
        report = drive("sync", cfg={"ref": "no-such-ref"})
        phases["badref"] = _snapshot("badref", asm, state, report)

        # ------------------------------------------------------ (d) memory cap
        report = drive("sync", cfg={"ref": "main"})
        phases["oom"] = _snapshot("oom", asm, state, report)

        # ------------------------------------------------------------ rollback
        report = drive("rollback", id="hog")
        phases["rollback"] = _snapshot("rollback", asm, state, report)

        started = [pid for pid in phases["rollback"].pids.values() if pid]
        asm.supervisor.shutdown()
        yield Observed(
            state=state,
            shas=shas,
            phases=phases,
            supervisor_escalations=sink.records,
            live_pids_after_shutdown=[pid for pid in started if _alive(pid)],
        )
    finally:
        try:
            asm.supervisor.shutdown()
        except Exception as e:  # pragma: no cover - already down
            print(f"teardown: shutdown raised {e}", file=sys.stderr)
        server.close()
        if asm.uids is not None:
            for service_id in (*SERVICES, REGISTRY_ID):
                service_root = state.service_root(service_id)
                if service_root.exists():
                    remove_service_root(service_root, asm.uids.allocate(service_id))
        subprocess.run(["rm", "-rf", str(root)], check=False)
        shutil.rmtree(store.root / "repos" / f"{repo_name}.git", ignore_errors=True)
        shutil.rmtree(store.root / "src" / repo_name, ignore_errors=True)


# --------------------------------------------------------- (a) kill mid-sync


def test_the_first_run_brings_up_every_service_it_could(observed: Observed) -> None:
    phase = observed.phase("kill")
    for service_id in ("alpha", "gamma", "hog"):
        assert phase.stages[service_id] == "healthy", (service_id, phase.errors)


def test_the_killed_service_is_the_only_one_that_failed(observed: Observed) -> None:
    phase = observed.phase("kill")
    assert phase.stages["beta"] == "failed"
    assert (phase.errors["beta"] or "").startswith("health:"), phase.errors["beta"]
    failed = [s["id"] for s in phase.report["services"] if s["stage"] == "failed"]
    assert failed == ["beta"]


def test_the_kill_produced_exactly_one_escalation(observed: Observed) -> None:
    phase = observed.phase("kill")
    assert phase.escalated_ids == ["beta"], phase.escalations
    record = phase.escalations[0]
    assert record["kind"] == "PlatformSync"
    assert record["event"]["stage"] == "health"


def test_the_killed_service_really_died_and_the_others_are_serving(observed: Observed) -> None:
    phase = observed.phase("kill")
    assert phase.pids["beta"] is None
    for service_id in ("alpha", "gamma", "hog"):
        assert (phase.pids[service_id] or 0) > 0, service_id


# ------------------------------------------------------- (b) corrupt manifest


def test_a_broken_manifest_fails_only_its_own_service(observed: Observed) -> None:
    phase = observed.phase("corrupt")
    assert phase.stages["gamma"] == "failed"
    error = phase.errors["gamma"] or ""
    assert error.startswith("translate:"), error
    assert "uv sync" in error  # the translator names the form it wanted


def test_the_other_services_are_untouched_by_the_broken_manifest(observed: Observed) -> None:
    before = observed.phase("kill").pids
    after = observed.phase("corrupt").pids
    for service_id in ("alpha", "hog"):
        assert after[service_id] == before[service_id], service_id
        assert observed.phase("corrupt").stages[service_id] == "healthy"


def test_the_broken_commit_escalated_once_for_the_broken_service_only(
    observed: Observed,
) -> None:
    """``beta`` is the control: same cause, same commit, so it stays quiet."""
    phase = observed.phase("corrupt")
    assert phase.escalated_ids == ["gamma"], phase.escalations
    assert phase.stages["beta"] == "failed"


def test_the_service_with_the_broken_manifest_keeps_running_its_old_code(
    observed: Observed,
) -> None:
    """A translate failure happens before anything is written, so nothing moved."""
    assert observed.phase("corrupt").pids["gamma"] == observed.phase("kill").pids["gamma"]
    decl = observed.phase("corrupt").decls["gamma"]
    assert f'GIT_COMMIT = "{observed.shas["c1"]}"' in decl


# ------------------------------------------------------------- (c) bad target


def test_an_unreachable_ref_fails_the_run_not_a_service(observed: Observed) -> None:
    phase = observed.phase("badref")
    assert phase.report["error"] is not None
    assert phase.report["services"] == []
    assert len(phase.escalations) == 1
    record = phase.escalations[0]
    assert record["service_id"] is None
    assert record["event"]["stage"] == "fetch"


def test_a_failed_fetch_leaves_the_state_file_byte_identical(observed: Observed) -> None:
    assert observed.phase("badref").state_bytes == observed.phase("corrupt").state_bytes


def test_a_failed_fetch_restarts_nothing(observed: Observed) -> None:
    assert observed.phase("badref").pids == observed.phase("corrupt").pids


# -------------------------------------------------------------- (d) memory cap


def test_the_service_that_outgrew_its_cap_fails_its_health_gate(observed: Observed) -> None:
    phase = observed.phase("oom")
    assert phase.stages["hog"] == "failed"
    assert (phase.errors["hog"] or "").startswith("health:"), phase.errors["hog"]


def test_the_cap_really_reached_the_declaration(observed: Observed) -> None:
    decl = observed.phase("oom").decls["hog"]
    assert 'memory_max = "32M"' in decl
    assert f'GIT_COMMIT = "{observed.shas["c4"]}"' in decl


def test_the_memory_failure_touched_no_other_service(observed: Observed) -> None:
    before = observed.phase("corrupt").pids
    after = observed.phase("oom").pids
    assert after["alpha"] == before["alpha"]
    assert observed.phase("oom").stages["alpha"] == "healthy"


def test_the_memory_failure_escalated_once(observed: Observed) -> None:
    assert observed.phase("oom").escalated_ids.count("hog") == 1, observed.phase("oom").escalations


def test_the_still_broken_manifest_speaks_again_only_because_the_commit_moved(
    observed: Observed,
) -> None:
    """Dedupe is keyed on ``(sha, stage, error)``, and c4 touched gamma's tree too.

    ``beta`` is the control again: still failing for the same reason at the same
    commit, so its record's ``escalated`` flag keeps it quiet across every tick.
    """
    ids = observed.phase("oom").escalated_ids
    assert ids == ["gamma", "hog"], observed.phase("oom").escalations
    assert "beta" not in ids
    assert observed.phase("oom").stages["beta"] == "failed"


# ---------------------------------------------------------------- the rollback


def test_the_rollback_puts_the_service_back_on_the_commit_it_was_healthy_at(
    observed: Observed,
) -> None:
    report = observed.phase("rollback").report
    assert report.get("ok") is True, (report.get("error"), report.get("precondition"))
    assert report["to_sha"] == observed.shas["c1"]
    assert report["from_sha"] == observed.shas["c4"]
    assert report["exit_code"] == 0
    for action in ("stop", "stage", "provision", "declare", "reload", "restart", "health"):
        assert action in report["actions"], (action, report["actions"])


def test_the_rollback_health_gate_went_green(observed: Observed) -> None:
    phase = observed.phase("rollback")
    assert phase.stages["hog"] == "healthy"
    assert phase.errors["hog"] is None
    assert (phase.pids["hog"] or 0) > 0


def test_the_rolled_back_declaration_is_the_older_commits(observed: Observed) -> None:
    decl = observed.phase("rollback").decls["hog"]
    assert 'memory_max = "64M"' in decl
    assert f'GIT_COMMIT = "{observed.shas["c1"]}"' in decl


def test_the_rollback_record_names_the_commit_it_came_from(observed: Observed) -> None:
    record = observed.phase("rollback").records["hog"]
    assert record["sha"] == observed.shas["c1"]
    assert record["deployed_sha"] == observed.shas["c1"]
    assert record["rolled_back_from"] == observed.shas["c4"]
    assert record["stage"] == "healthy"
    assert record["escalated"] is False
    assert record["prev_sha"] is None


def test_the_rollback_warns_that_the_sync_timer_will_undo_it(observed: Observed) -> None:
    warnings = observed.phase("rollback").report["warnings"]
    assert any("next 'ams platform sync' tick" in w for w in warnings), warnings


def test_the_rollback_restarted_only_its_own_service(observed: Observed) -> None:
    before = observed.phase("oom").pids
    after = observed.phase("rollback").pids
    for service_id in ("alpha", "gamma"):
        assert after[service_id] == before[service_id], f"{service_id} was restarted"


def test_one_service_stayed_up_untouched_through_every_injection(observed: Observed) -> None:
    """The whole point: four failures, and alpha never noticed any of them."""
    pids = [observed.phase(name).pids["alpha"] for name in observed.phases]
    assert len(set(pids)) == 1, pids
    assert pids[0] is not None


# --------------------------------------------------------------------- teardown


def test_shutdown_leaves_no_processes_behind(observed: Observed) -> None:
    assert observed.live_pids_after_shutdown == []
