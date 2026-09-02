"""End-to-end isolation on the target box: identity, ownership, limits, kill.

These are the tests that would have caught every bug found while bringing the
host up, so they assert on observed kernel state (``/proc/self/uid_map``,
``CapEff``, host ``st_uid``) rather than on our own bookkeeping.

Run with ``scripts/remote-test.sh ams-iso tests/linux``.
"""

from __future__ import annotations

import os
import select
import stat
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from ams import schema
from ams.cgroup import ServiceCgroup
from ams.hostcheck import check_host
from ams.isolated import IsolatedSpawner, make_isolated_spawner
from ams.spawn import SpawnedService, SpawnRequest
from ams.uidmap import UidBlock
from ams.userns import SpawnError, ensure_service_root, remove_service_root

pytestmark = pytest.mark.linux

# First block of harness' /etc/subuid range on the target box. In production the
# allocator hands these out; here it is pinned so ownership assertions are exact.
BLOCK = UidBlock(100_000, 100_000, 1024)
HOST_UID = BLOCK.uid_start

READ_TIMEOUT_S = 20.0

# Touch every page: bytearray(n) is calloc on fresh anonymous mmap, which the
# kernel never faults in, so it is charged nothing and no limit ever bites.
_ALLOC_ARGV = [
    "python3",
    "-c",
    "import sys; x = b'x' * int(sys.argv[1]); print('allocated', flush=True)",
]


# --------------------------------------------------------------------------- harness


@dataclass
class Ran:
    svc: SpawnedService
    stdout: str
    stderr: str
    status: int
    root: Path

    @property
    def exit_code(self) -> int:
        return os.waitstatus_to_exitcode(self.status)


def _drain(svc: SpawnedService, timeout_s: float) -> tuple[str, str]:
    """Read both pipes until EOF. The read ends are non-blocking by contract."""
    bufs = {svc.stdout_fd: bytearray(), svc.stderr_fd: bytearray()}
    live = set(bufs)
    deadline = time.monotonic() + timeout_s
    while live and time.monotonic() < deadline:
        ready, _, _ = select.select(list(live), [], [], 0.2)
        for fd in ready:
            try:
                chunk = os.read(fd, 65536)
            except BlockingIOError:
                continue
            if chunk:
                bufs[fd] += chunk
            else:
                live.discard(fd)
    return (
        bytes(bufs[svc.stdout_fd]).decode(errors="replace"),
        bytes(bufs[svc.stderr_fd]).decode(errors="replace"),
    )


@pytest.fixture(scope="module")
def spawner() -> IsolatedSpawner:
    return make_isolated_spawner(lambda _service_id: BLOCK)


@pytest.fixture(scope="module")
def base() -> Iterator[Path]:
    """Session-private services dir under the harness state dir."""
    state = Path(os.environ.get("AMS_STATE_DIR", str(Path.home() / "state")))
    root = state / f"itest-{os.getpid()}"
    root.mkdir(parents=True, exist_ok=True)
    yield root
    for child in sorted(root.iterdir(), reverse=True):
        remove_service_root(child, BLOCK)
    root.rmdir()


Spawn = Callable[..., SpawnedService]


@pytest.fixture
def start(spawner: IsolatedSpawner, base: Path) -> Iterator[Spawn]:
    """Spawn a service; guarantees kill + reap + cgroup removal afterwards."""
    live: list[SpawnedService] = []

    def _start(
        service_id: str,
        argv: list[str],
        *,
        limits: dict[str, object] | None = None,
        workdir: str = ".",
    ) -> SpawnedService:
        decl = schema.from_dict(
            {
                "id": service_id,
                "start": {"argv": argv, "workdir": workdir},
                **({"limits": limits} if limits else {}),
            }
        )
        req = SpawnRequest(decl=decl, root=base / service_id, ports={})
        svc = spawner.spawn(req)
        live.append(svc)
        return svc

    yield _start
    for svc in live:
        spawner.kill_tree(svc)
        try:
            os.waitpid(svc.pid, 0)
        except ChildProcessError:
            pass
        spawner.cleanup(svc)


@pytest.fixture
def run(spawner: IsolatedSpawner, base: Path, start: Spawn) -> Callable[..., Ran]:
    """Spawn a short-lived service and collect everything it produced."""

    def _run(service_id: str, argv: list[str], **kw: object) -> Ran:
        svc = start(service_id, argv, **kw)  # type: ignore[arg-type]
        out, err = _drain(svc, READ_TIMEOUT_S)
        _, status = os.waitpid(svc.pid, 0)
        return Ran(svc, out, err, status, base / service_id)

    return _run


def _status_fields(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in text.splitlines():
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


# --------------------------------------------------------------------------- identity


def test_service_runs_as_the_inner_uid(run: Callable[..., Ran]) -> None:
    r = run("ident", ["id", "-u"])
    assert r.exit_code == 0, r.stderr
    assert r.stdout.strip() == "1000"
    assert run("identg", ["id", "-g"]).stdout.strip() == "1000"


def test_uid_map_is_exactly_the_service_block(run: Callable[..., Ran]) -> None:
    r = run("umap", ["cat", "/proc/self/uid_map"])
    assert r.exit_code == 0, r.stderr
    rows = [line.split() for line in r.stdout.splitlines() if line.strip()]
    assert rows == [["1000", str(BLOCK.uid_start), str(BLOCK.size)]]


def test_gid_map_is_exactly_the_service_block(run: Callable[..., Ran]) -> None:
    r = run("gmap", ["cat", "/proc/self/gid_map"])
    rows = [line.split() for line in r.stdout.splitlines() if line.strip()]
    assert rows == [["1000", str(BLOCK.gid_start), str(BLOCK.size)]]


def test_service_holds_no_capabilities_and_cannot_regain_privilege(
    run: Callable[..., Ran],
) -> None:
    r = run("caps", ["cat", "/proc/self/status"])
    fields = _status_fields(r.stdout)
    assert fields["CapEff"] == "0" * 16, fields["CapEff"]
    assert fields["CapPrm"] == "0" * 16
    assert fields["CapInh"] == "0" * 16
    assert fields["NoNewPrivs"] == "1"
    assert fields["Groups"] == ""  # setgroups([]) ran
    # CapBnd stays full (0x1ffffffffff) and that is correct: the bounding set is
    # not cleared by setresuid and only gates capabilities gained through file
    # capabilities on exec, which NoNewPrivs=1 already forbids. Verified on the
    # box, n=1. Dropping it would be redundant, so do not "fix" this.
    assert fields["CapBnd"] != "", fields


# --------------------------------------------------------------------------- files


def test_the_service_root_belongs_to_the_block_not_the_harness(run: Callable[..., Ran]) -> None:
    r = run("owner", ["cp", "/etc/hostname", "written.txt"])
    assert r.exit_code == 0, r.stderr
    assert os.stat(r.root).st_uid == HOST_UID
    written = r.root / "written.txt"
    assert written.exists()
    assert os.stat(written).st_uid == HOST_UID
    assert os.stat(written).st_gid == BLOCK.gid_start
    assert os.stat(written).st_uid != os.getuid()


def test_a_harness_private_file_is_unreadable_from_inside(run: Callable[..., Ran]) -> None:
    """The harness uid is deliberately absent from the runtime map (D4)."""
    secret = Path(f"/tmp/ams-secret-{os.getpid()}")  # /tmp is world-traversable
    secret.write_text("s3cret\n")
    secret.chmod(0o600)
    assert stat.S_IMODE(os.stat(secret).st_mode) == 0o600
    try:
        r = run("peeker", ["cat", str(secret)])
        assert r.exit_code != 0
        assert "s3cret" not in r.stdout
        assert "denied" in r.stderr.lower(), r.stderr
    finally:
        secret.unlink()


def test_ensure_service_root_does_no_work_on_an_already_owned_root(
    base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It runs before every spawn, so the warm path must not fork.

    The recursive chown it used to run unconditionally is O(files under the
    root); once a .venv or node_modules lives there, every restart walked tens
    of thousands of inodes inside a fresh user namespace.
    """
    from ams import userns

    root = base / "cheap"
    calls: list[list[str]] = []
    real = userns.run_admin

    def counting(argv: list[str], *a: object, **kw: object) -> object:
        calls.append(list(argv))
        return real(argv, *a, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(userns, "run_admin", counting)

    ensure_service_root(root, BLOCK)  # creation: must chown
    assert [c[0] for c in calls] == ["chown"], calls
    assert os.stat(root).st_uid == HOST_UID

    calls.clear()
    ensure_service_root(root, BLOCK)  # warm: must do nothing at all
    assert calls == [], f"a no-op call still ran admin commands: {calls}"

    calls.clear()
    ensure_service_root(root, BLOCK, subdirs=(root / "sub",))  # new subdir: chown again
    assert [c[0] for c in calls] == ["mkdir", "chown"], calls
    assert os.stat(root / "sub").st_uid == HOST_UID


def test_a_failed_spawn_closes_every_fd_exactly_once(
    spawner: IsolatedSpawner, base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Neither a leak nor a double close.

    A leak shows up as more open fds; a double close of a recycled number shows
    up as fewer (the cgroup teardown between the two closes opens control
    files). Asserting equality catches both, which a try/finally did not.
    """
    from ams import isolated as isolated_mod

    def boom(*_a: object, **_kw: object) -> int:
        raise SpawnError("injected: fork_in_userns failed")

    monkeypatch.setattr(isolated_mod, "fork_in_userns", boom)
    decl = schema.from_dict({"id": "fdleak", "start": {"argv": ["/bin/true"]}})
    req = SpawnRequest(decl=decl, root=base / "fdleak", ports={})

    before = sorted(os.listdir("/proc/self/fd"))
    for _ in range(5):
        with pytest.raises(SpawnError):
            spawner.spawn(req)
    after = sorted(os.listdir("/proc/self/fd"))
    assert len(after) == len(before), f"fd count changed: {before} -> {after}"
    # the cgroup is torn down too, not left behind for the next attempt
    assert not (spawner.cgroup_root.path / "svc-fdleak").exists()


def test_remove_service_root_deletes_a_service_owned_tree(run: Callable[..., Ran]) -> None:
    r = run("removable", ["cp", "/etc/hostname", "written.txt"])
    assert r.root.exists()
    remove_service_root(r.root, BLOCK)
    assert not r.root.exists()


# --------------------------------------------------------------------------- cgroup


def test_kill_tree_takes_down_grandchildren(
    spawner: IsolatedSpawner, start: Spawn, base: Path
) -> None:
    svc = start("tree", ["sh", "-c", "sleep 60 & wait"])
    cg = ServiceCgroup(Path(svc.cgroup), svc.service_id, Path(svc.cgroup).parent)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and len(cg.pids()) < 2:
        time.sleep(0.02)
    assert len(cg.pids()) >= 2, cg.pids()

    spawner.kill_tree(svc)
    assert cg.wait_empty(5.0) is True
    os.waitpid(svc.pid, 0)
    spawner.cleanup(svc)
    assert not cg.path.exists()


def test_declaring_memory_max_also_caps_swap(start: Spawn) -> None:
    """DECISIONS D12: the spawner turns a declared ceiling into a real one.

    ``apply_limits`` writes only what the declaration says; the swap cap is the
    supervisor's policy call, applied here rather than in the cgroup layer.
    ``tests/linux/test_cgroup.py`` pins the other half — that ``memory.max``
    alone is not a ceiling where swap exists.
    """
    svc = start("swapcap", ["sleep", "30"], limits={"memory_max": "64M"})
    cg = Path(svc.cgroup)
    assert (cg / "memory.max").read_text().strip() == str(64 * 1024 * 1024)
    assert (cg / "memory.swap.max").read_text().strip() == "0"


def test_undeclared_limits_leave_swap_alone(start: Spawn) -> None:
    svc = start("noswapcap", ["sleep", "30"])
    swap = (Path(svc.cgroup) / "memory.swap.max").read_text().strip()
    assert swap != "0", "swap must only be capped when the declaration asks for memory_max"


def test_memory_max_kills_a_greedy_service(spawner: IsolatedSpawner, start: Spawn) -> None:
    """The whole point of D12: 200 MB under a declared 32 MB ceiling must die."""
    svc = start("hog", [*_ALLOC_ARGV, str(200 * 1024 * 1024)], limits={"memory_max": "32M"})
    out, err = _drain(svc, READ_TIMEOUT_S)
    _, status = os.waitpid(svc.pid, 0)
    assert os.waitstatus_to_exitcode(status) != 0, f"200M survived 32M+noswap: {out!r} {err!r}"
    assert "allocated" not in out


def test_a_service_that_stays_within_its_limits_exits_cleanly(run: Callable[..., Ran]) -> None:
    r = run(
        "modest",
        [*_ALLOC_ARGV, str(4 * 1024 * 1024)],
        limits={"memory_max": "128M", "pids_max": 32, "cpu_max": "50%"},
    )
    assert r.exit_code == 0, r.stderr
    assert r.stdout.strip() == "allocated"


def test_missing_binary_fails_before_anything_is_created(
    spawner: IsolatedSpawner, base: Path
) -> None:
    decl = schema.from_dict({"id": "ghost", "start": {"argv": ["ams-no-such-binary"]}})
    req = SpawnRequest(decl=decl, root=base / "ghost", ports={})
    with pytest.raises(SpawnError, match="not found on PATH"):
        spawner.spawn(req)
    assert not (spawner.cgroup_root.path / "svc-ghost").exists()
    assert not (base / "ghost").exists()


# --------------------------------------------------------------------------- host


def test_hostcheck_reports_the_box_as_ready() -> None:
    results = check_host()
    failed = {r.name: r.detail for r in results if not r.ok}
    assert failed == {}, failed
    assert {r.name for r in results} >= {
        "subuid",
        "subgid",
        "newuidmap",
        "newgidmap",
        "cgroup-v2",
        "cgroup-delegated",
        "apparmor-userns",
        "state-traversal",
    }


def test_state_traversal_check_matches_the_actual_directory_modes() -> None:
    """Covers the state dir and the reflink store that will hold service envs."""
    dirs = [
        Path(os.environ.get("AMS_STATE_DIR", str(Path.home() / "state"))),
        Path(os.environ.get("AMS_STORE_DIR", str(Path.home() / "store"))),
    ]
    reachable = all(
        os.stat(p).st_mode & stat.S_IXOTH for d in dirs for p in [d, *d.parents] if p.exists()
    )
    result = next(r for r in check_host() if r.name == "state-traversal")
    assert result.ok is reachable, result.detail


def test_a_service_can_reach_its_own_root_by_absolute_path(
    run: Callable[..., Ran], base: Path
) -> None:
    """The reason state-traversal is a check: 0750 on the home broke exactly this."""
    r = run("abspath", ["cp", "/etc/hostname", "written.txt"])
    assert r.exit_code == 0, r.stderr
    back = run("abspath2", ["cat", str(base / "abspath" / "written.txt")])
    assert back.exit_code == 0, back.stderr
    assert back.stdout.strip() != ""


def test_a_stored_secret_is_injected_but_its_file_stays_unreadable(
    spawner: IsolatedSpawner, base: Path
) -> None:
    """The whole security argument for the secret store (D16), in one spawn.

    The service is handed the value in its environment and is simultaneously
    denied the file it came from: the harness uid is not in the runtime uid map
    (D4), so a 0600 harness-owned file is unreachable from inside even when the
    path itself is traversable.

    The store lives under /tmp and its *directories* are deliberately widened to
    0711 here. In production they are 0700, which would already stop the service
    at the directory; widening them isolates the property actually under test —
    the file mode plus the missing uid mapping — instead of letting a directory
    permission pass the test for the wrong reason.
    """
    from ams.secrets import SecretStore

    value = b"pilot-value-8e21"
    store_root = Path(f"/tmp/ams-secret-store-{os.getpid()}")
    store = SecretStore(store_root)
    secret_path = store.set("keeper", "K", value)
    for d in (store_root, store.dir, store.service_dir("keeper")):
        d.chmod(0o711)

    assert stat.S_IMODE(os.stat(secret_path).st_mode) == 0o600
    assert os.stat(secret_path).st_uid == os.getuid()  # harness-owned, not the block

    decl = schema.from_dict(
        {
            "id": "keeper",
            "secrets": ["K"],
            "start": {
                "argv": [
                    "/usr/bin/python3",
                    "-c",
                    "import os, sys; print('K=' + os.environ.get('K', '<unset>'), flush=True); "
                    "open(sys.argv[1]).read()",
                    str(secret_path),
                ]
            },
        }
    )
    req = SpawnRequest(
        decl=decl,
        root=base / "keeper",
        ports={},
        extra_env=store.load("keeper", decl.secrets),
    )
    svc = spawner.spawn(req)
    try:
        out, err = _drain(svc, READ_TIMEOUT_S)
        _, status = os.waitpid(svc.pid, 0)
    finally:
        spawner.kill_tree(svc)
        spawner.cleanup(svc)
        secret_path.unlink(missing_ok=True)
        store.service_dir("keeper").rmdir()
        store.dir.rmdir()
        store_root.rmdir()

    assert out.strip() == "K=" + value.decode()  # injected
    assert os.waitstatus_to_exitcode(status) != 0  # ... and the file was not
    assert "PermissionError" in err and "Errno 13" in err, err
    assert value.decode() not in err
