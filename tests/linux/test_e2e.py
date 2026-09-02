"""End-to-end: two declarations on disk to a supervised, isolated, reaped system.

This drives :func:`ams.cli.build_supervisor` -- the exact assembly ``ams run``
uses -- against real allocators, a real delegated cgroup and real user
namespaces, then asserts on kernel-observable state (host uid of the service
process, ``memory.max``, ``memory.swap.max``, cgroup dirs) rather than on our
own bookkeeping.

The scenario runs once per module and every test asserts on what it recorded,
so the (slow) spawn/crash/restart/shutdown sequence is paid for once.

Run with ``scripts/remote-test.sh ams-integ tests/linux``.
"""

from __future__ import annotations

import http.client
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from ams.cli import build_supervisor
from ams.decision import Decision, ServiceContext
from ams.events import Event, ServiceExited
from ams.state import StateDir
from ams.supervisor import Supervisor
from ams.userns import remove_service_root

# The scenario fixture spawns, crashes, restarts and shuts down real processes;
# it is charged to the first test, so the module needs more than the 60 s default.
pytestmark = [pytest.mark.linux, pytest.mark.timeout(180)]

# Bounds every wait in this module; the box is 1 vCPU, so be generous but finite.
SETTLE_TIMEOUT_S = 20.0
PUMP_SLICE_S = 0.05

# The crasher's stderr line has to classify as ERROR for DefaultPolicy to
# escalate it (ams.events.classify: an unmarked stderr line is only INFO,
# because most servers log everything to stderr). "boom" alone would be
# logged and never reach the agent, which is the thing this test is checking.
CRASH_CODE = (
    "import sys,time; print('ERROR: boom', file=sys.stderr, flush=True); "
    "time.sleep(0.2); sys.exit(3)"
)

ECHO_TOML = """
id = "echo-http"
[start]
argv = ["/usr/bin/python3", "-m", "http.server", "${PORT_main}", "--bind", "127.0.0.1"]
[ports]
main = 0
[health]
kind = "http"
port = "main"
path = "/"
interval_s = 0.5
timeout_s = 2.0
start_period_s = 1.0
[limits]
memory_max = "64M"
pids_max = 16
[stop]
timeout_s = 3.0
[runtime]
kind = "none"
"""

CRASHER_TOML = f"""
id = "crasher"
[start]
argv = ["/usr/bin/python3", "-c", {json.dumps(CRASH_CODE)}]
[restart]
max_retries = 2
backoff_s = 0.1
[stop]
timeout_s = 2.0
[runtime]
kind = "none"
"""


# --------------------------------------------------------------------------- helpers


@dataclass
class RecordingEscalation:
    """The agent side of the contract: what actually reached the sink."""

    records: list[dict[str, Any]] = field(default_factory=list)

    def escalate(self, event: Event, decision: Decision, ctx: ServiceContext) -> None:
        self.records.append(
            {
                "kind": type(event).__name__,
                "service_id": getattr(event, "service_id", None),
                "action": decision.action.value,
                "reason": decision.reason,
                "text": getattr(event, "text", ""),
                "consecutive_failures": ctx.consecutive_failures,
            }
        )

    def texts_for(self, service_id: str) -> list[str]:
        return [r["text"] for r in self.records if r["service_id"] == service_id]


def _pump(
    sup: Supervisor,
    until: Callable[[], bool],
    timeout_s: float,
    sink: list[Event] | None = None,
) -> bool:
    """Run the loop until ``until()`` or the deadline. True if the condition held.

    ``run_once`` returns the iteration's events, which is exactly what an
    embedding agent consumes, so the test collects them the same way.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        events = sup.run_once(PUMP_SLICE_S)
        if sink is not None:
            sink.extend(events)
        if until():
            return True
    return until()


def _proc_uid(pid: int) -> int | None:
    """Real uid of a live pid as the *host* sees it, or None if it is gone."""
    try:
        text = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("Uid:"):
            return int(line.split()[1])
    return None


def _http_get(port: int, path: str = "/", timeout_s: float = 5.0) -> int:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout_s)
    try:
        conn.request("GET", path)
        return conn.getresponse().status
    finally:
        conn.close()


def _read_control(cgroup: Path, name: str) -> str | None:
    try:
        return (cgroup / name).read_text(encoding="utf-8").strip()
    except OSError:
        return None


# --------------------------------------------------------------------------- scenario


@dataclass
class Observed:
    """Everything the scenario measured, so each assertion is one small test."""

    state: StateDir
    echo_healthy: bool
    echo_pid: int
    echo_port: int
    echo_uid: int | None
    echo_block_uid: int
    echo_cgroup: Path
    echo_memory_max: str | None
    echo_swap_max: str | None
    http_status: int
    crasher_cgroup: Path
    crasher_exits: list[ServiceExited]
    crasher_status: str
    crasher_attempts: int
    escalations: RecordingEscalation
    live_pids_after_shutdown: list[int]
    ports_json: dict[str, Any]
    uidmap_json: dict[str, Any]
    block_starts: dict[str, int]


@pytest.fixture(scope="module")
def observed() -> Iterator[Observed]:
    base = Path(os.environ.get("AMS_STATE_DIR", str(Path.home() / "state")))
    root = base / f"e2e-{os.getpid()}"
    for service_id, text in (("echo-http", ECHO_TOML), ("crasher", CRASHER_TOML)):
        d = root / "services" / service_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "service.toml").write_text(text, encoding="utf-8")

    state = StateDir(root)
    sink = RecordingEscalation()
    asm = build_supervisor(state, isolation=True, escalation=sink)
    sup = asm.supervisor
    assert sorted(asm.registered) == ["crasher", "echo-http"], asm.registered

    seen: list[Event] = []

    def crasher_exits() -> list[ServiceExited]:
        return [e for e in seen if isinstance(e, ServiceExited) and e.service_id == "crasher"]

    try:
        asm.start_all()
        echo = sup.services["echo-http"]
        crasher = sup.services["crasher"]

        healthy = _pump(sup, lambda: echo.healthy is True, SETTLE_TIMEOUT_S, seen)
        assert echo.spawned is not None, "echo-http died before it became healthy"
        echo_pid = echo.spawned.pid
        echo_cgroup = Path(str(echo.spawned.cgroup))
        echo_port = echo.ports["main"]
        measured = Observed(
            state=state,
            echo_healthy=healthy,
            echo_pid=echo_pid,
            echo_port=echo_port,
            echo_uid=_proc_uid(echo_pid),
            echo_block_uid=asm.uids.allocate("echo-http").uid_start,  # type: ignore[union-attr]
            echo_cgroup=echo_cgroup,
            echo_memory_max=_read_control(echo_cgroup, "memory.max"),
            echo_swap_max=_read_control(echo_cgroup, "memory.swap.max"),
            http_status=_http_get(echo_port),
            crasher_cgroup=asm.spawner.cgroup_root.path / "svc-crasher",  # type: ignore[attr-defined]
            crasher_exits=[],
            crasher_status="",
            crasher_attempts=0,
            escalations=sink,
            live_pids_after_shutdown=[],
            ports_json={},
            uidmap_json={},
            block_starts={},
        )

        _pump(sup, lambda: crasher.status == "failed", SETTLE_TIMEOUT_S, seen)
        measured.crasher_status = crasher.status
        measured.crasher_attempts = crasher.attempt
        measured.crasher_exits = crasher_exits()

        sup.shutdown()
        started_pids = (echo_pid, *(e.pid for e in measured.crasher_exits))
        measured.live_pids_after_shutdown = [
            pid for pid in started_pids if _proc_uid(pid) is not None
        ]
        measured.ports_json = json.loads(state.ports_state.read_text(encoding="utf-8"))
        measured.uidmap_json = json.loads(state.uidmap_state.read_text(encoding="utf-8"))
        measured.block_starts = {
            sid: b.uid_start
            for sid, b in asm.uids.assignments().items()  # type: ignore[union-attr]
        }
        yield measured
    finally:
        if asm.uids is not None:
            for service_id in ("echo-http", "crasher"):
                service_root = state.service_root(service_id)
                if service_root.exists():
                    remove_service_root(service_root, asm.uids.allocate(service_id))
        subprocess.run(["rm", "-rf", str(root)], check=False)


# --------------------------------------------------------------------------- echo-http


def test_the_http_service_becomes_healthy(observed: Observed) -> None:
    assert observed.echo_healthy is True


def test_the_allocated_port_actually_serves(observed: Observed) -> None:
    assert 20000 <= observed.echo_port <= 29999
    assert observed.http_status == 200


def test_the_service_process_runs_as_its_mapped_subuid(observed: Observed) -> None:
    """The host sees the service's block uid, never the harness's own uid."""
    assert observed.echo_uid == observed.echo_block_uid
    assert observed.echo_uid != os.getuid()
    assert observed.echo_uid is not None and observed.echo_uid >= 100_000


def test_blocks_are_carved_from_the_start_of_the_subuid_range(observed: Observed) -> None:
    assert sorted(observed.block_starts.values()) == [100_000, 101_024]


def test_declared_limits_reached_the_cgroup(observed: Observed) -> None:
    assert observed.echo_memory_max == str(64 * 1024 * 1024)
    assert observed.echo_swap_max == "0"  # DECISIONS D12


# --------------------------------------------------------------------------- crasher


def test_the_crasher_is_restarted_then_given_up_on(observed: Observed) -> None:
    assert [e.exit_code for e in observed.crasher_exits] == [3, 3]
    assert observed.crasher_attempts == 2  # one restart, then max_retries reached
    assert observed.crasher_status == "failed"


def test_the_crashers_stderr_and_its_terminal_state_reached_the_agent(
    observed: Observed,
) -> None:
    texts = observed.escalations.texts_for("crasher")
    assert any("boom" in t for t in texts), texts
    assert any(t.startswith("giving up on crasher") for t in texts), texts
    terminal = [r for r in observed.escalations.records if r["text"].startswith("giving up")]
    assert terminal[0]["reason"].endswith("max_retries")


# --------------------------------------------------------------------------- teardown


def test_shutdown_leaves_no_processes_and_no_cgroups(observed: Observed) -> None:
    assert observed.live_pids_after_shutdown == []
    assert not observed.echo_cgroup.exists()
    assert not observed.crasher_cgroup.exists()


def test_allocations_are_persisted_for_the_next_run(observed: Observed) -> None:
    assert set(observed.ports_json["ports"]) == {"echo-http", "crasher"}
    assert observed.ports_json["ports"]["echo-http"]["main"] == observed.echo_port
    assert set(observed.uidmap_json["blocks"]) == {"echo-http", "crasher"}


# --------------------------------------------------------------------------- cli


def _ams(*args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    src = str(Path(__file__).resolve().parent.parent.parent / "src")
    env["PYTHONPATH"] = os.pathsep.join([src, env.get("PYTHONPATH", "")])
    return subprocess.run(
        [sys.executable, "-m", "ams", *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


def test_ams_validate_on_the_box(tmp_path: Path) -> None:
    good = tmp_path / "good.toml"
    good.write_text(ECHO_TOML, encoding="utf-8")
    bad = tmp_path / "bad.toml"
    bad.write_text('id = "NOT VALID"\n[start]\nargv = ["/bin/true"]\n', encoding="utf-8")

    ok = _ams("validate", str(good))
    assert ok.returncode == 0, ok.stderr
    assert ok.stdout.strip() == "OK echo-http"
    assert _ams("validate", str(good), str(bad)).returncode == 1


def test_ams_check_host_passes_on_the_target_box() -> None:
    proc = _ams("check-host")
    assert "cgroup-delegated" in proc.stdout
    assert "subuid" in proc.stdout
    assert proc.returncode == 0, proc.stdout + proc.stderr
