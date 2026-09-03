"""A real ``ams platform pool adopt`` on the target host (T8, PLAN-pool §5.8).

What ``tests/test_platform_pool_adopt.py`` structurally cannot check, because it
replaces ``run_admin`` with a fake that runs as the test user:

1. that the **two-hop move actually works across two uid blocks** -- the member's
   ``data/`` is owned by one block and unreadable from the harness, the pool's by
   another, and no single admin namespace maps both. A same-uid fake cannot fail
   the way a wrong single-hop implementation fails here (EACCES);
2. that a real SQLite database survives the move byte-for-byte;
3. that the moved file ends up owned by the **pool's** block, not the member's or
   the harness's;
4. that a process running as the pool's mapped uid can then open that database
   **read-write** -- which is the only question the migration actually asks.

The directories are deliberately built the way the isolation layer builds them
(``ensure_service_root`` + a chown into the member's block), so the harness
genuinely cannot list the member's ``data/`` and adoption has to go through the
admin namespace to find out what is in there.

Everything lives under the reflink store, because both roots must be on one
filesystem for the hops to be renames rather than copies.

Run with:
    scripts/remote-test.sh t8-pool-adopt tests/linux/test_pool_adopt_live.py
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from ams.platform.pool import adopt, plan, staging_dir
from ams.platform.sync import STATE_VERSION, make_pool_data_dirs, state_path
from ams.secrets import SecretStore
from ams.spawn import DATA_DIRNAME
from ams.state import StateDir
from ams.uidmap import UidBlock
from ams.userns import ensure_service_root, run_admin

pytestmark = [pytest.mark.linux, pytest.mark.timeout(300)]

#: Three distinct 1024-wide blocks out of the harness' real /etc/subuid range.
#: Distinct is the whole point: one namespace can map exactly one of them.
MEMBER_BLOCKS = {
    "alpha": UidBlock(100_000, 100_000, 1024),
    "beta": UidBlock(101_024, 101_024, 1024),
}
POOL_BLOCK = UidBlock(102_048, 102_048, 1024)

STORE_ROOT = Path("/home/harness/store")
#: Private scratch under the store, never the harness' own AMS_STATE_DIR.
BASE = STORE_ROOT / "state" / "pool-adopt-test"

POOL = "core"
POOL_ID = "pool-core"
MEMBERS = ("alpha", "beta")
SECRET_VALUE = b"live-adopt-secret-must-never-be-printed"


class Uids:
    """The fixed blocks above. The seam under test is ``run_admin``, not this."""

    def allocate(self, service_id: str) -> UidBlock:
        return MEMBER_BLOCKS.get(service_id, POOL_BLOCK)


@pytest.fixture(scope="module", autouse=True)
def _clean() -> Iterator[None]:
    _rm_as_admin()
    yield
    _rm_as_admin()


def _rm_as_admin() -> None:
    """Remove the scratch tree. Some of it is owned by uids the harness is not."""
    if not BASE.exists():
        return
    for block in (*MEMBER_BLOCKS.values(), POOL_BLOCK):
        run_admin(["rm", "-rf", str(BASE)], block, timeout_s=60.0)
    shutil.rmtree(BASE, ignore_errors=True)


def _state() -> StateDir:
    state = StateDir(BASE / "state")
    state.ensure()
    return state


def _write_platform_state(state: StateDir) -> None:
    row: dict[str, Any] = {
        "sha": "a" * 40,
        "prev_sha": None,
        "deployed_sha": "a" * 40,
        "stage": "failed",
        "error": None,
        "manual_restart": False,
        "escalated": False,
        "updated_at": "2026-09-03T00:00:00Z",
        "stage_since": "2026-09-03T00:00:00Z",
    }
    services = {
        POOL_ID: {**row, "pool": POOL, "pool_members": list(MEMBERS)},
        **{member: {**row, "pool": POOL} for member in MEMBERS},
    }
    path = state_path(state)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": STATE_VERSION, "services": services}, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _make_staged_pool_root(state: StateDir) -> Path:
    """The pool root exactly as a sync tick leaves it, before adoption runs.

    This is the state the live cutover actually met and that the first version
    of this module missed by starting from *no* pool root at all: sync stages
    the tree (``ensure_service_root``) and then creates ``data/<member>`` for
    every member (``make_pool_data_dirs``), both inside the admin namespace and
    both chowned to the pool's block. The result is ``drwxr-x---
    <pool-uid>:<pool-gid>``, which the harness cannot stat into -- the exact
    directory that made ``pool adopt`` die with an unhandled ``PermissionError``
    on racknerd (`.claude/state/diagnosis-pool-cutover.md`).

    Both real functions are called rather than imitated, so this reproduces what
    sync does rather than what this module thinks sync does.
    """
    root = state.service_root(POOL_ID)
    ensure_service_root(root, POOL_BLOCK)
    make_pool_data_dirs(root, MEMBERS, POOL_BLOCK)
    with pytest.raises(PermissionError):
        # The precondition, asserted rather than assumed: if this ever stops
        # raising, the reproduction has silently stopped reproducing.
        (root / DATA_DIRNAME / MEMBERS[0]).is_dir()
    return root


def _make_legacy_member(state: StateDir, member: str) -> Path:
    """A pre-pool service root: real database inside, owned by the member's block."""
    root = state.service_root(member)
    block = MEMBER_BLOCKS[member]
    ensure_service_root(root, block)
    db = root / DATA_DIRNAME / f"{member}.db"
    # The database is built in a harness-owned scratch directory and copied in
    # through the admin namespace, because `ensure_service_root` has already
    # handed `<root>/data` to the service uid -- the harness cannot write there,
    # which is precisely the situation adoption exists for.
    seed = BASE / "seed" / f"{member}.db"
    seed.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(seed) as conn:
        conn.execute("CREATE TABLE rows (id INTEGER PRIMARY KEY, note TEXT)")
        conn.execute("INSERT INTO rows (note) VALUES (?)", (f"written-before-adoption-{member}",))
        conn.commit()
    run_admin(["cp", str(seed), str(db)], block).check()
    # Owned by the service uid at 0640, exactly as a running service leaves it.
    run_admin(["chown", "-R", "1000:1000", str(root / DATA_DIRNAME)], block).check()
    run_admin(["chmod", "640", str(db)], block).check()
    state.service_decl_path(member).write_text(f'id = "{member}"\n', encoding="utf-8")
    return db


#: A member that really is running: it opens its database in WAL mode, commits
#: rows that therefore live in the `-wal` file rather than in the `.db`, and
#: waits for SIGTERM. On SIGTERM it closes the connection, which is what makes
#: SQLite checkpoint the WAL into the database and delete `-wal`/`-shm` -- the
#: disk change between plan time and move time that broke the live cutover.
HOLDER = """
import signal, sqlite3, sys, time
db = sys.argv[1]
conn = sqlite3.connect(db)
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("CREATE TABLE IF NOT EXISTS rows (id INTEGER PRIMARY KEY, note TEXT)")
conn.executemany(
    "INSERT INTO rows (note) VALUES (?)", [(f"row-{i}",) for i in range(int(sys.argv[2]))]
)
conn.commit()
def bye(*_a):
    conn.close()
    sys.exit(0)
signal.signal(signal.SIGTERM, bye)
print("ready", flush=True)
while True:
    time.sleep(0.2)
"""
HOLDER_ROWS = 25


def _make_running_member(state: StateDir, member: str) -> tuple[Path, subprocess.Popen[str]]:
    """A legacy root whose service is *up*, holding a WAL-mode database open.

    Its ``data/`` is handed to the harness rather than to the member's block --
    the one deliberate deviation from the real layout in this module, and it
    buys the thing that matters here: a real long-lived process really holding a
    real ``-wal``/``-shm`` pair open. ``beta`` keeps the true cross-block
    ownership, so nothing is lost from the coverage of the other failure.
    """
    root = state.service_root(member)
    block = MEMBER_BLOCKS[member]
    ensure_service_root(root, block)
    data = root / DATA_DIRNAME
    run_admin(["chown", "-R", "0:0", str(data)], block).check()
    db = data / f"{member}.db"
    holder: subprocess.Popen[str] = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(db), str(HOLDER_ROWS)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout is not None
    line = holder.stdout.readline().strip()
    if line != "ready":  # pragma: no cover - a broken fixture
        holder.kill()
        errors = holder.stderr.read() if holder.stderr else ""
        raise AssertionError(f"holder did not start: {line!r} {errors}")
    for suffix in ("-wal", "-shm"):
        assert (data / f"{member}.db{suffix}").exists(), f"expected a live {suffix} file"
    state.service_decl_path(member).write_text(f'id = "{member}"\n', encoding="utf-8")
    return db, holder


class HolderControl:
    """The control socket, standing in for a supervisor that owns the holder.

    ``stop`` really terminates the process, so the ``-wal``/``-shm`` files
    really disappear between the plan and the move -- which is the whole point.
    """

    def __init__(self, running: dict[str, subprocess.Popen[str]]) -> None:
        self.running = running
        self.rows = {m: {"status": "running", "pid": p.pid} for m, p in running.items()}
        self.stopped: list[str] = []

    def status(self) -> dict[str, Any]:
        return {"ok": True, "services": dict(self.rows)}

    def stop(self, service_id: str) -> dict[str, Any]:
        process = self.running.get(service_id)
        if process is not None:
            process.terminate()
            process.wait(timeout=30)
        self.stopped.append(service_id)
        self.rows[service_id] = {"status": "stopped", "pid": None}
        return {"ok": True}


def _stat_in_pool_ns(path: Path) -> tuple[int, str]:
    """``(uid, mode)`` as seen from the pool's admin namespace.

    Inside it the pool's block maps to 1000; a file owned by any *other* block
    would show up as 65534 (nobody), which is exactly the failure this asserts
    against. The harness cannot stat these paths at all -- it cannot traverse a
    0750 directory owned by a uid it does not have.
    """
    result = run_admin(["stat", "-c", "%u %a", str(path)], POOL_BLOCK, timeout_s=30.0).check()
    uid, mode = result.stdout.decode().split()
    return int(uid), mode


def _make_traversable(state: StateDir) -> None:
    """``o+x`` down to the pool's service directory, as ``ams run`` does.

    The harness-owned directories above a service root are 0750 and the service
    uid is neither their owner nor in their group, so without this a process
    running as the pool's uid cannot resolve a path into its own root -- which
    is ``ams.cli._ensure_traversable``'s whole reason for existing. Only ``o+x``,
    never ``o+r``: the directories stay unlistable.
    """
    for directory in (
        BASE,
        state.root,
        state.services_dir,
        state.service_dir(POOL_ID),
    ):
        directory.chmod(directory.stat().st_mode | 0o001)


def _sqlite_rw_as_pool_uid(db: Path) -> str:
    """Open ``db`` read-write as the pool's mapped uid and read a row back.

    ``setpriv`` drops inner root to inner uid 1000 first, so this is the same
    identity the pooled process runs as -- not the admin identity that moved the
    file, which can read anything in the namespace and would prove nothing.
    """
    script = (
        "import sqlite3,sys;"
        "c=sqlite3.connect(sys.argv[1]);"
        "c.execute('INSERT INTO rows (note) VALUES (?)',('written-after-adoption',));"
        "c.commit();"
        "print(';'.join(r[0] for r in c.execute('SELECT note FROM rows ORDER BY id')))"
    )
    result = run_admin(
        [
            "setpriv",
            "--reuid",
            "1000",
            "--regid",
            "1000",
            "--clear-groups",
            sys.executable,
            "-c",
            script,
            str(db),
        ],
        POOL_BLOCK,
        timeout_s=60.0,
    )
    result.check()
    return result.stdout.decode().strip()


@pytest.fixture(scope="module")
def adopted() -> Any:
    """One real adoption against one running member and one stopped member."""
    state = _state()
    state.service_dir(POOL_ID).mkdir(parents=True, exist_ok=True)
    _make_staged_pool_root(state)
    _db, holder = _make_running_member(state, "alpha")
    _make_legacy_member(state, "beta")
    SecretStore(state.root).set("alpha", "SVC_SECRET", SECRET_VALUE)
    _write_platform_state(state)

    # The harness genuinely cannot see into beta's data dir: that is the
    # precondition that makes the admin-ns listing load-bearing rather than a
    # fallback nothing exercises.
    with pytest.raises(PermissionError):
        list((state.service_root("beta") / DATA_DIRNAME).iterdir())

    control = HolderControl({"alpha": holder})
    try:
        before = plan(state, POOL, uids=Uids(), run_admin_fn=run_admin, control=control)
        report = adopt(state, POOL, uids=Uids(), run_admin_fn=run_admin, control=control)
        after = plan(state, POOL, uids=Uids(), run_admin_fn=run_admin)
    finally:
        if holder.poll() is None:  # pragma: no cover - only on a failed adoption
            holder.kill()
            holder.wait(timeout=10)
    _make_traversable(state)
    return state, before, report, after, control


def test_the_plan_of_a_running_member_names_its_wal_files(adopted: Any) -> None:
    """The plan is a forecast taken against a running fleet -- three files, not one."""
    _state_dir, before, _report, _after, _control = adopted
    alpha = next(m for m in before.members if m.id == "alpha")
    assert alpha.data_readable is True
    assert alpha.data_entries == ("alpha.db", "alpha.db-shm", "alpha.db-wal")


def test_the_plan_sees_a_stopped_members_database_through_the_admin_namespace(
    adopted: Any,
) -> None:
    _state_dir, before, _report, _after, _control = adopted
    beta = next(m for m in before.members if m.id == "beta")
    assert beta.data_readable is True
    assert beta.data_entries == ("beta.db",)


def test_adoption_stopped_the_running_member_before_moving_it(adopted: Any) -> None:
    _state_dir, _before, _report, _after, control = adopted
    assert control.stopped == ["alpha"]
    assert control.running["alpha"].poll() is not None, "the holder really exited"


def test_every_members_database_lands_in_the_pool_root(adopted: Any) -> None:
    state, _before, report, _after, _control = adopted
    assert sorted(report.moved) == [f"{m}:{m}.db" for m in MEMBERS]
    for member in MEMBERS:
        db = state.service_root(POOL_ID) / DATA_DIRNAME / member / f"{member}.db"
        uid, _mode = _stat_in_pool_ns(db)
        assert uid == 1000, "the database must be owned by the pool's block, not the member's"


def test_the_checkpointed_wal_files_are_gone_and_did_not_break_the_move(adopted: Any) -> None:
    """The two files the stop deleted are neither moved nor mourned."""
    state, _before, report, _after, _control = adopted
    for name in ("alpha.db-wal", "alpha.db-shm"):
        assert f"alpha:{name}" not in report.moved
        assert (
            run_admin(
                ["find", str(state.service_root(POOL_ID) / DATA_DIRNAME / "alpha"), "-name", name],
                POOL_BLOCK,
            )
            .check()
            .stdout
            == b""
        )


def test_the_pools_uid_can_open_the_moved_database_read_write(adopted: Any) -> None:
    state, _before, _report, _after, _control = adopted
    db = state.service_root(POOL_ID) / DATA_DIRNAME / "alpha" / "alpha.db"

    notes = _sqlite_rw_as_pool_uid(db)

    rows = notes.split(";")
    # Every row the holder committed was in the `-wal`, not the `.db`, until the
    # stop checkpointed it. Losing them is exactly what moving the database
    # without its WAL would look like, so this count is the real assertion.
    assert len(rows) == HOLDER_ROWS + 1
    assert rows[0] == "row-0"
    assert rows[-1] == "written-after-adoption"


def test_the_legacy_root_survives_with_an_empty_data_dir(adopted: Any) -> None:
    state, _before, _report, _after, _control = adopted
    for member in MEMBERS:
        assert state.service_root(member).is_dir()
        data = state.service_root(member) / DATA_DIRNAME
        block = MEMBER_BLOCKS[member]
        listing = run_admin(
            ["find", str(data), "-mindepth", "1", "-maxdepth", "1"], block
        ).check()
        assert listing.stdout == b"", "the data dir is put back, and put back empty"
        stat_out = run_admin(["stat", "-c", "%u %a", str(data)], block).check().stdout
        uid, mode = stat_out.decode().split()
        assert (int(uid), mode) == (1000, "750"), "and handed back to the member's own uid"
        assert not state.service_decl_path(member).exists()
    assert not staging_dir(state, "alpha").exists()


def test_the_secret_is_copied_under_the_mangled_name(adopted: Any) -> None:
    state, _before, _report, _after, _control = adopted
    secrets = SecretStore(state.root)
    assert secrets.path("alpha", "SVC_SECRET").read_bytes() == SECRET_VALUE
    copied = secrets.path(POOL_ID, "SVC_SECRET__ALPHA")
    assert copied.read_bytes() == SECRET_VALUE
    assert oct(copied.stat().st_mode)[-3:] == "600"


def test_a_second_adoption_is_a_no_op(adopted: Any) -> None:
    state, _before, _report, after, _control = adopted
    assert after.is_noop is True

    again = adopt(state, POOL, uids=Uids(), run_admin_fn=run_admin)

    assert again.noop is True
    db = state.service_root(POOL_ID) / DATA_DIRNAME / "alpha" / "alpha.db"
    assert _stat_in_pool_ns(db)[0] == 1000
