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
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from ams.platform.pool import adopt, plan, staging_dir
from ams.platform.sync import STATE_VERSION, state_path
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
    """One real adoption, shared by the assertions below."""
    state = _state()
    state.service_dir(POOL_ID).mkdir(parents=True, exist_ok=True)
    for member in MEMBERS:
        _make_legacy_member(state, member)
    SecretStore(state.root).set("alpha", "SVC_SECRET", SECRET_VALUE)
    _write_platform_state(state)

    # The harness genuinely cannot see into the member's data dir: that is the
    # precondition that makes the admin-ns listing load-bearing rather than a
    # fallback nothing exercises.
    with pytest.raises(PermissionError):
        list((state.service_root("alpha") / DATA_DIRNAME).iterdir())

    before = plan(state, POOL, uids=Uids(), run_admin_fn=run_admin)
    report = adopt(state, POOL, uids=Uids(), run_admin_fn=run_admin)
    after = plan(state, POOL, uids=Uids(), run_admin_fn=run_admin)
    _make_traversable(state)
    return state, before, report, after


def test_the_plan_sees_the_databases_through_the_admin_namespace(adopted: Any) -> None:
    _state_dir, before, _report, _after = adopted
    alpha = next(m for m in before.members if m.id == "alpha")
    assert alpha.data_readable is True
    assert alpha.data_entries == ("alpha.db",)


def test_every_members_database_lands_in_the_pool_root(adopted: Any) -> None:
    state, _before, report, _after = adopted
    assert sorted(report.moved) == [f"{m}:{m}.db" for m in MEMBERS]
    for member in MEMBERS:
        db = state.service_root(POOL_ID) / DATA_DIRNAME / member / f"{member}.db"
        uid, _mode = _stat_in_pool_ns(db)
        assert uid == 1000, "the database must be owned by the pool's block, not the member's"


def test_the_pools_uid_can_open_the_moved_database_read_write(adopted: Any) -> None:
    state, _before, _report, _after = adopted
    db = state.service_root(POOL_ID) / DATA_DIRNAME / "alpha" / "alpha.db"

    notes = _sqlite_rw_as_pool_uid(db)

    assert notes == "written-before-adoption-alpha;written-after-adoption"


def test_the_legacy_root_survives_and_the_declaration_does_not(adopted: Any) -> None:
    state, _before, _report, _after = adopted
    for member in MEMBERS:
        assert state.service_root(member).is_dir()
        assert (state.service_root(member) / DATA_DIRNAME).is_dir()
        assert not state.service_decl_path(member).exists()
    assert not staging_dir(state, "alpha").exists()


def test_the_secret_is_copied_under_the_mangled_name(adopted: Any) -> None:
    state, _before, _report, _after = adopted
    secrets = SecretStore(state.root)
    assert secrets.path("alpha", "SVC_SECRET").read_bytes() == SECRET_VALUE
    copied = secrets.path(POOL_ID, "SVC_SECRET__ALPHA")
    assert copied.read_bytes() == SECRET_VALUE
    assert oct(copied.stat().st_mode)[-3:] == "600"


def test_a_second_adoption_is_a_no_op(adopted: Any) -> None:
    state, _before, _report, after = adopted
    assert after.is_noop is True

    again = adopt(state, POOL, uids=Uids(), run_admin_fn=run_admin)

    assert again.noop is True
    db = state.service_root(POOL_ID) / DATA_DIRNAME / "alpha" / "alpha.db"
    assert _stat_in_pool_ns(db)[0] == 1000
