"""Service root layout on the target box: the data dir a service may write to.

``<root>/data`` is the one directory a service is promised across restarts and
re-provisions, exported as ``AMS_DATA_DIR``. Everything here asserts on observed
kernel state (host ``st_uid``, the mode on disk, a file the service itself
wrote) rather than on our own bookkeeping.

Run with ``scripts/remote-test.sh ams-core tests/linux/test_userns.py``.
"""

from __future__ import annotations

import os
import select
import stat
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from ams import schema
from ams.isolated import IsolatedSpawner, make_isolated_spawner
from ams.spawn import DATA_DIRNAME, SpawnedService, SpawnRequest
from ams.uidmap import UidBlock
from ams.userns import ensure_service_root, remove_service_root, run_admin

pytestmark = pytest.mark.linux

# Pinned so ownership assertions are exact; in production the allocator hands
# these out. Same first block the other Linux tests use.
BLOCK = UidBlock(100_000, 100_000, 1024)
HOST_UID = BLOCK.uid_start

READ_TIMEOUT_S = 20.0


@pytest.fixture(scope="module")
def base() -> Iterator[Path]:
    state = Path(os.environ.get("AMS_STATE_DIR", str(Path.home() / "state")))
    root = state / f"utest-{os.getpid()}"
    root.mkdir(parents=True, exist_ok=True)
    yield root
    for child in sorted(root.iterdir(), reverse=True):
        remove_service_root(child, BLOCK)
    root.rmdir()


@pytest.fixture(scope="module")
def spawner() -> IsolatedSpawner:
    return make_isolated_spawner(lambda _service_id: BLOCK)


def _drain(svc: SpawnedService, timeout_s: float) -> tuple[str, str]:
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


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


# ------------------------------------------------------------------ ensure_service_root


def test_a_new_service_root_gets_a_service_owned_data_dir(base: Path) -> None:
    root = base / "fresh"
    ensure_service_root(root, BLOCK)
    data = root / DATA_DIRNAME
    assert data.is_dir()
    assert os.stat(data).st_uid == HOST_UID, "the data dir must belong to the service uid"
    assert os.stat(data).st_gid == BLOCK.gid_start
    assert _mode(data) == 0o750, oct(_mode(data))


def test_the_data_dir_is_added_to_a_root_that_predates_it(base: Path) -> None:
    """The upgrade path: a root created before AMS_DATA_DIR existed.

    Once the root belongs to the block the harness cannot mkdir inside it, so
    this branch goes through the admin namespace.
    """
    root = base / "upgraded"
    ensure_service_root(root, BLOCK)
    run_admin(["rm", "-rf", str(root / DATA_DIRNAME)], BLOCK).check()
    assert not (root / DATA_DIRNAME).exists()
    assert os.stat(root).st_uid == HOST_UID  # not ours any more

    ensure_service_root(root, BLOCK)
    data = root / DATA_DIRNAME
    assert data.is_dir()
    assert os.stat(data).st_uid == HOST_UID
    assert _mode(data) == 0o750, oct(_mode(data))


def test_an_existing_data_dir_costs_no_admin_fork(
    base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The warm path runs before every spawn and must stay free."""
    from ams import userns

    root = base / "warm"
    ensure_service_root(root, BLOCK)

    calls: list[list[str]] = []
    real = userns.run_admin

    def counting(argv: list[str], *a: object, **kw: object) -> object:
        calls.append(list(argv))
        return real(argv, *a, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(userns, "run_admin", counting)
    ensure_service_root(root, BLOCK)
    assert calls == [], f"a no-op call still ran admin commands: {calls}"


# ------------------------------------------------------------------ from inside


def test_a_spawned_service_can_write_to_its_data_dir(spawner: IsolatedSpawner, base: Path) -> None:
    code = (
        "import os, pathlib\n"
        "d = pathlib.Path(os.environ['AMS_DATA_DIR'])\n"
        "(d / 'state.db').write_text('rows')\n"
        "print(d)\n"
        "print(oct(d.stat().st_mode & 0o777))\n"
        "print(d.stat().st_uid)\n"
    )
    decl = schema.from_dict({"id": "datadir", "start": {"argv": ["python3", "-c", code]}})
    root = base / "datadir"
    svc = spawner.spawn(SpawnRequest(decl=decl, root=root, ports={}))
    try:
        out, err = _drain(svc, READ_TIMEOUT_S)
        _, status = os.waitpid(svc.pid, 0)
    finally:
        spawner.cleanup(svc)
    assert os.waitstatus_to_exitcode(status) == 0, err
    reported_dir, reported_mode, reported_uid = out.split()
    assert reported_dir == str(root / DATA_DIRNAME)
    assert reported_mode == "0o750"
    assert reported_uid == "1000", "inside the namespace the service owns it as uid 1000"
    # 0750 means the harness itself cannot read what the service wrote: that is
    # why the backup path (PLAN Q6) has to go through the admin namespace.
    written = root / DATA_DIRNAME / "state.db"
    with pytest.raises(PermissionError):
        written.read_text()
    seen = run_admin(["stat", "-c", "%u", str(written)], BLOCK).check()
    assert seen.stdout.decode().strip() == "1000"
