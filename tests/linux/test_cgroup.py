"""cgroup v2 behaviour on a real delegated subtree.

Run with ``scripts/remote-test.sh ams-iso tests/linux``. An interactive ssh
session's cgroup is not delegated, so these are skipped there by the marker in
tests/conftest.py rather than failing confusingly.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Iterator

import pytest

from ams.cgroup import CgroupRoot, ServiceCgroup
from ams.schema import LimitsSpec
from ams.userns import get_pdeathsig, is_subreaper, set_child_subreaper, set_pdeathsig

pytestmark = pytest.mark.linux

# Keep every process these tests start short-lived: the target box is 1 vCPU.
SLEEP_S = "30"


@pytest.fixture(scope="module")
def root() -> CgroupRoot:
    r = CgroupRoot.discover()
    r.enable_controllers()
    return r


@pytest.fixture
def svc(root: CgroupRoot, request: pytest.FixtureRequest) -> Iterator[ServiceCgroup]:
    service_id = request.node.name.replace("_", "-").replace("test-", "")[:31].strip("-")
    cg = ServiceCgroup.create(root, service_id)
    try:
        yield cg
    finally:
        cg.kill()
        cg.wait_empty(5.0)
        cg.remove()


def test_discover_and_enable_controllers_are_idempotent(root: CgroupRoot) -> None:
    again = CgroupRoot.discover()
    assert again.path == root.path
    first = root.enable_controllers()
    second = root.enable_controllers()
    assert first == second
    assert {"memory", "pids"} <= set(root.enabled_controllers())


def test_our_own_pid_is_not_directly_in_the_delegated_root(root: CgroupRoot) -> None:
    """The no-internal-processes rule: enabling controllers requires this."""
    own = [int(p) for p in (root.path / "cgroup.procs").read_text().split()]
    assert os.getpid() not in own


def test_limits_are_written_and_read_back(svc: ServiceCgroup) -> None:
    written = svc.apply_limits(LimitsSpec(memory_max="32M", cpu_max="50%", pids_max=16))
    assert written == {
        "memory.max": str(32 * 1024 * 1024),
        "pids.max": "16",
        "cpu.max": "50000 100000",
    }
    assert (svc.path / "memory.max").read_text().strip() == str(32 * 1024 * 1024)
    assert (svc.path / "pids.max").read_text().strip() == "16"
    assert (svc.path / "cpu.max").read_text().strip() == "50000 100000"


def test_add_pid_kill_and_remove(svc: ServiceCgroup) -> None:
    proc = subprocess.Popen(["sleep", SLEEP_S])
    svc.add_pid(proc.pid)
    assert proc.pid in svc.pids()
    assert svc.populated() is True

    svc.kill()
    assert svc.wait_empty(5.0) is True
    assert proc.wait(timeout=5) != 0  # SIGKILL, not a clean exit
    assert svc.pids() == []
    assert svc.remove() is True
    assert not svc.path.exists()


def test_stats_reports_the_numbers_the_supervisor_needs(svc: ServiceCgroup) -> None:
    proc = subprocess.Popen(["sleep", SLEEP_S])
    svc.add_pid(proc.pid)
    stats = svc.stats()
    assert stats["pids.current"] >= 1
    assert stats["memory.current"] >= 0
    assert "cpu.usage_usec" in stats
    proc.kill()
    proc.wait(timeout=5)


def test_pids_max_caps_a_fork_storm(svc: ServiceCgroup) -> None:
    """pids.max is the backstop for a service that forks without bound."""
    svc.apply_limits(LimitsSpec(pids_max=2))

    def enter_cgroup() -> None:  # runs in the child, before exec
        svc.add_pid(os.getpid())

    proc = subprocess.Popen(
        ["sh", "-c", "sleep 5 & sleep 5 & sleep 5 & wait"],
        preexec_fn=enter_cgroup,  # single-threaded test; must land before the forks
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and len(svc.pids()) < 2:
        time.sleep(0.02)
    assert 1 <= len(svc.pids()) <= 2, svc.pids()
    # pids.events records that the limit actually bit, not just that we counted low.
    assert "max 0" not in (svc.path / "pids.events").read_text()

    svc.kill()
    assert svc.wait_empty(5.0) is True
    proc.wait(timeout=5)


def _swap_total_bytes() -> int:
    for line in open("/proc/meminfo", encoding="utf-8"):
        if line.startswith("SwapTotal:"):
            return int(line.split()[1]) * 1024
    return 0


def test_memory_max_alone_does_not_bound_a_greedy_process(svc: ServiceCgroup) -> None:
    """DECISIONS D12, the surprising half: memory.max is a reclaim threshold.

    Once swap exists, a process 6x over its memory.max survives by swapping.
    This is why ``IsolatedSpawner`` follows ``apply_limits`` with
    ``set_swap_max(0)`` whenever the declaration sets ``memory_max``; the
    counterpart is ``test_memory_max_kills_a_greedy_service`` in
    tests/linux/test_isolated.py.
    """
    if not _swap_total_bytes():
        pytest.skip("host has no swap; memory.max is already a hard ceiling")
    svc.apply_limits(LimitsSpec(memory_max="32M"))
    assert (svc.path / "memory.swap.max").read_text().strip() != "0"

    def enter_cgroup() -> None:  # runs in the child, before exec
        svc.add_pid(os.getpid())

    proc = subprocess.Popen(
        ["python3", "-c", "x = b'x' * (200 * 1024 * 1024); print('allocated')"],
        preexec_fn=enter_cgroup,  # noqa: PLW1509 - single-threaded test
        stdout=subprocess.PIPE,
        text=True,
    )
    out, _ = proc.communicate(timeout=60)
    assert proc.returncode == 0, "200M died under memory.max=32M despite available swap"
    assert out.strip() == "allocated"


def test_set_swap_max_makes_memory_max_a_ceiling(svc: ServiceCgroup) -> None:
    svc.apply_limits(LimitsSpec(memory_max="32M"))
    svc.set_swap_max(0)
    assert (svc.path / "memory.swap.max").read_text().strip() == "0"
    svc.set_swap_max("max")
    assert (svc.path / "memory.swap.max").read_text().strip() == "max"


def test_child_subreaper_flag_round_trips() -> None:
    before = is_subreaper()
    try:
        set_child_subreaper(True)
        assert is_subreaper() is True
        set_child_subreaper(False)
        assert is_subreaper() is False
    finally:
        set_child_subreaper(before)
    assert is_subreaper() is before


def test_pdeathsig_round_trips() -> None:
    before = get_pdeathsig()
    try:
        set_pdeathsig(signal.SIGKILL)
        assert get_pdeathsig() == int(signal.SIGKILL)
        set_pdeathsig(0)
        assert get_pdeathsig() == 0
    finally:
        set_pdeathsig(before)
