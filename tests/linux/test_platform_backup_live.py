"""The restore drill, on real service-owned data, without touching the network.

This is T2.4's deliverable. Everything else about backups is a claim; this file
is the check, and it is deliberately built so that it cannot pass by accident:

1. A service root is created and handed to a uid block, exactly as the isolated
   spawner does it. The database is written *as the service*, so it is owned by
   host uid 100000, not by the harness.
2. `test_the_harness_cannot_read_the_database_directly` asserts the harness gets
   `PermissionError` on a plain `open()`. Without that assertion, a passing
   drill would prove nothing about the admin namespace -- the snapshot could be
   an ordinary file read and nobody would know.
3. The database is then snapshotted through the real `run_admin`, the original
   is deleted through the admin namespace, and the archive is restored into a
   harness scratch path. The row has to come back.

Run with:
    scripts/remote-test.sh ams-bk tests/linux/test_platform_backup_live.py \\
        tests/test_platform_backup.py

No R2 credentials are involved: `upload` is the one step this file does not
exercise, and it is pinned by argv equality in the portable tests. T4.2 does the
live round trip.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path

import linuxhost
import pytest

from ams.platform.backup import Target, discover, restore, snapshot
from ams.uidmap import UidBlock
from ams.userns import AdminResult, ensure_service_root, remove_service_root, run_admin

pytestmark = [pytest.mark.linux, pytest.mark.timeout(300)]

# First block of the harness' /etc/subuid range, pinned so the ownership
# assertions are exact. In production the allocator hands these out.
BLOCK = linuxhost.block(0)
SERVICE_UID = BLOCK.uid_start  # host uid of inner 1000

SERVICE_ID = "plat-bk-test"
#: The reflink store (D13). The state dir the harness really uses lives here,
#: so the drill runs on the same filesystem and the same 0711 home traversal
#: rules as production rather than in /tmp.
BASE = Path("/home/harness/store/state") / SERVICE_ID

THE_ROW = "the row that must survive a delete"


class FakeAllocator:
    def __init__(self, blocks: dict[str, UidBlock]) -> None:
        self._blocks = blocks

    def get(self, service_id: str) -> UidBlock | None:
        return self._blocks.get(service_id)


class FakeStateDir:
    """Just the two members `discover` reads, pointed at this drill's root."""

    def __init__(self, root: Path, service_root: Path) -> None:
        self.root = root
        self._service_root = service_root

    def list_service_ids(self) -> list[str]:
        return [SERVICE_ID]

    def service_root(self, service_id: str) -> Path:
        assert service_id == SERVICE_ID
        return self._service_root


def _as_service(argv: Sequence[str]) -> AdminResult:
    """Run argv as the service uid, from inside the admin namespace.

    `run_admin` gives us inner root over the block, and `setpriv` drops to inner
    1000 before exec -- so the files land on host uid 100000 with no chown step
    and no chance of a harness-owned file sneaking into the drill.
    """
    inner = ["setpriv", "--reuid", "1000", "--regid", "1000", "--clear-groups", *argv]
    return run_admin(inner, BLOCK, timeout_s=120.0)


@pytest.fixture
def service_root() -> Iterator[Path]:
    root = BASE / "root"
    remove_service_root(root, BLOCK)
    BASE.mkdir(parents=True, exist_ok=True)
    ensure_service_root(root, BLOCK)
    try:
        yield root
    finally:
        remove_service_root(root, BLOCK)
        for path in (BASE,):
            if path.is_dir():
                subprocess.run(["rm", "-rf", str(path)], check=False)


@pytest.fixture
def service_db(service_root: Path) -> Path:
    """`<root>/data/app.db` with one row, written by the service uid itself."""
    db = service_root / "data" / "app.db"
    code = (
        "import sqlite3,sys\n"
        "con=sqlite3.connect(sys.argv[1])\n"
        "con.execute('CREATE TABLE notes (body TEXT)')\n"
        "con.execute('INSERT INTO notes VALUES (?)', (sys.argv[2],))\n"
        "con.commit();con.close()\n"
    )
    result = _as_service([sys.executable, "-I", "-c", code, str(db), THE_ROW])
    assert result.ok, result.stderr.decode(errors="replace")
    # Ownership has to be checked through the admin namespace: `<root>/data` is
    # 0750 owned by the block, so the harness cannot even stat what is inside it
    # -- which is the property the next test asserts directly. Inside the ns the
    # block appears as inner 1000; on the host that is SERVICE_UID.
    owner = run_admin(["stat", "-c", "%u:%g", str(db)], BLOCK).check()
    assert owner.stdout.decode().strip() == "1000:1000", (
        f"the drill needs a service-owned database; {db} is {owner.stdout!r}"
    )
    return db


# ------------------------------------------------------------------- the gate


def test_the_harness_cannot_read_the_database_directly(service_db: Path) -> None:
    """Without this, the rest of the file proves nothing.

    The harness uid is not mapped into the service namespace (D4) and
    `<root>/data` is 0750 owned by the block, so an ordinary `open()` must fail.
    If this ever starts passing, the isolation boundary has moved and the admin
    namespace in `snapshot` is no longer load-bearing.
    """
    with pytest.raises(PermissionError):
        service_db.open("rb").close()

    with pytest.raises(PermissionError):
        list((service_db.parent).iterdir())

    # Not even a stat: the data dir is 0750 and the harness is not the block.
    with pytest.raises(PermissionError):
        os.stat(service_db)

    # The root above it *is* traversable (0755), so this is a permission
    # boundary on the data dir specifically, not a broken path.
    assert os.stat(service_db.parent.parent).st_uid == SERVICE_UID


# ---------------------------------------------------------------- the drill


def test_snapshot_restores_a_row_after_the_original_is_deleted(
    service_root: Path, service_db: Path, tmp_path: Path
) -> None:
    state = FakeStateDir(root=BASE, service_root=service_root)
    targets = discover(state, FakeAllocator({SERVICE_ID: BLOCK}))
    assert [t.db_path for t in targets] == [service_db]

    workdir = BASE / "work"
    workdir.mkdir(parents=True, exist_ok=True)
    gz = snapshot(targets[0], workdir, stamp="20260902")

    assert gz.name == f"{SERVICE_ID}-app-20260902.db.gz"
    # inner 0 IS the harness uid, so the snapshot needs no chown to be ours.
    assert os.stat(gz).st_uid == os.getuid()
    assert gz.stat().st_size > 0

    # Destroy the original the only way the harness can: through the admin ns.
    run_admin(["rm", "-f", str(service_db)], BLOCK).check()
    assert not _service_sees(service_db)

    result = restore(gz, tmp_path / "restored.db")

    assert result.ok and result.integrity == "ok"
    con = sqlite3.connect(result.path)
    try:
        assert con.execute("SELECT body FROM notes").fetchall() == [(THE_ROW,)]
    finally:
        con.close()


def test_snapshot_leaves_the_service_able_to_write_its_database(
    service_root: Path, service_db: Path
) -> None:
    """The subtle failure this module is designed against.

    Snapshotting opens the database read-write as inner root. Any `-wal`/`-shm`
    it creates would land on the harness uid, and the service could then never
    commit again -- a backup job that breaks the thing it is protecting. The
    chown-back in `_SNAPSHOT_CODE` is what stops that, and this asserts it by
    having the service write another row afterwards.
    """
    workdir = BASE / "work2"
    workdir.mkdir(parents=True, exist_ok=True)
    snapshot(Target(SERVICE_ID, service_db, BLOCK), workdir, stamp="20260902")

    code = (
        "import sqlite3,sys\n"
        "con=sqlite3.connect(sys.argv[1])\n"
        "con.execute('INSERT INTO notes VALUES (?)', ('after the backup',))\n"
        "con.commit()\n"
        "print(con.execute('SELECT count(*) FROM notes').fetchone()[0])\n"
        "con.close()\n"
    )
    result = _as_service([sys.executable, "-I", "-c", code, str(service_db)])
    assert result.ok, result.stderr.decode(errors="replace")
    assert result.stdout.decode().strip() == "2"

    for suffix in ("-wal", "-shm", "-journal"):
        side = Path(str(service_db) + suffix)
        listing = run_admin(["stat", "-c", "%u", str(side)], BLOCK)
        if listing.ok:
            assert listing.stdout.decode().strip() == "1000", f"{side} escaped to the harness uid"


def test_discover_finds_nothing_when_the_data_dir_is_empty(service_root: Path) -> None:
    state = FakeStateDir(root=BASE, service_root=service_root)
    assert discover(state, FakeAllocator({SERVICE_ID: BLOCK})) == []


def _service_sees(path: Path) -> bool:
    return run_admin(["test", "-f", str(path)], BLOCK).ok
