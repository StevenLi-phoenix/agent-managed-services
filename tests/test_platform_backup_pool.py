"""T7 (backup half): pool member labeling (PLAN-pool §5.6).

A pool service (`pool-core`) hosts N member processes in one address space;
its root has `<root>/pool.json` naming the members, and each member's data
lives under `<root>/data/<member>/`. `discover` must give each member's
database a `label` so its R2 key stays exactly what it was before pooling --
`.../kvservice/kvservice-kv-<stamp>.db.gz` -- rather than collapsing every
member's backups under `.../pool-core/...`.

Reuses `state`/`make_service`/`make_db`/`local_admin`/`FakeAllocator`/`BLOCK`/
`STAMP` from `test_platform_backup.py` (same `tests/` dir, no package
`__init__.py`, so it collects as a top-level module pytest has already put on
`sys.path` -- same pattern as `test_platform_gateway_pool.py`).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_platform_backup import BLOCK, STAMP, FakeAllocator, local_admin, make_db, make_service

from ams.platform.backup import BackupConfig, Target, discover, snapshot
from ams.state import StateDir

BUCKET = "phm-backups-test"


@pytest.fixture
def state(tmp_path: Path) -> StateDir:
    """Same fixture as `test_platform_backup.py`'s -- defined locally rather
    than imported so a `state: StateDir` test parameter is a use of the
    fixture, not a lint-flagged redefinition of an imported name."""
    root = tmp_path / "state"
    root.mkdir()
    return StateDir(root)


def make_pool(state_dir: StateDir, pool_id: str, members: list[str]) -> Path:
    """A pool service root: `service.toml` + `<root>/pool.json` (PLAN-pool §5.6/§3.3)."""
    data_dir = make_service(state_dir, pool_id)
    manifest = {
        "version": 1,
        "pool": pool_id.removeprefix("pool-"),
        "sha": "a" * 40,
        "members": [{"id": member} for member in members],
    }
    (state_dir.service_root(pool_id) / "pool.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    return data_dir


# --------------------------------------------------------------------- discover


def test_pool_member_db_gets_the_member_label(state: StateDir) -> None:
    data = make_pool(state, "pool-core", ["kvservice", "timeservice"])
    make_db(data / "kvservice" / "kv.db")
    alloc = FakeAllocator({"pool-core": BLOCK})

    targets = discover(state, alloc, run_admin_fn=local_admin)

    assert len(targets) == 1
    target = targets[0]
    assert target.service_id == "pool-core"  # still the id the harness supervises
    assert target.label == "kvservice"
    assert target.effective_label == "kvservice"


def test_pool_member_remote_key_matches_the_pre_pooling_key(state: StateDir) -> None:
    data = make_pool(state, "pool-core", ["kvservice", "timeservice"])
    make_db(data / "kvservice" / "kv.db")
    alloc = FakeAllocator({"pool-core": BLOCK})

    target = discover(state, alloc, run_admin_fn=local_admin)[0]
    cfg = BackupConfig(bucket=BUCKET)

    assert target.archive_name(STAMP) == f"kvservice-kv-{STAMP}.db.gz"
    assert (
        cfg.remote_object(target.effective_label, target.archive_name(STAMP))
        == f"r2:{BUCKET}/daily/kvservice/kvservice-kv-{STAMP}.db.gz"
    )


def test_db_under_a_non_member_subdir_keeps_the_pool_id(state: StateDir) -> None:
    """A db in a pool root that is not under any member's data dir is not
    attributed to a member -- it keeps the pool's own id, same as an unpooled
    service would."""
    data = make_pool(state, "pool-core", ["kvservice"])
    make_db(data / "shared" / "misc.db")  # "shared" is not a declared member
    alloc = FakeAllocator({"pool-core": BLOCK})

    target = discover(state, alloc, run_admin_fn=local_admin)[0]

    assert target.label == ""
    assert target.effective_label == "pool-core"


def test_db_directly_under_pool_data_dir_keeps_the_pool_id(state: StateDir) -> None:
    """A db with no subdir at all (`data/x.db`, not `data/<member>/x.db`) is
    not a member's -- it belongs to the pool process itself."""
    data = make_pool(state, "pool-core", ["kvservice"])
    make_db(data / "pool.db")
    alloc = FakeAllocator({"pool-core": BLOCK})

    target = discover(state, alloc, run_admin_fn=local_admin)[0]

    assert target.label == ""
    assert target.effective_label == "pool-core"


def test_multiple_members_each_get_their_own_label(state: StateDir) -> None:
    data = make_pool(state, "pool-core", ["kvservice", "timeservice"])
    make_db(data / "kvservice" / "kv.db")
    make_db(data / "timeservice" / "time.db")
    alloc = FakeAllocator({"pool-core": BLOCK})

    targets = discover(state, alloc, run_admin_fn=local_admin)

    assert {t.label for t in targets} == {"kvservice", "timeservice"}
    assert all(t.service_id == "pool-core" for t in targets)


@pytest.mark.parametrize(
    "manifest",
    [
        "not json at all {",
        json.dumps({"version": 1}),  # no "members" key
        json.dumps({"version": 1, "members": "not-a-list"}),
        json.dumps({"version": 1, "members": [{"no_id": "x"}]}),
    ],
)
def test_a_malformed_pool_manifest_falls_back_to_unpooled_behaviour(
    state: StateDir, manifest: str
) -> None:
    data = make_service(state, "pool-core")
    (state.service_root("pool-core") / "pool.json").write_text(manifest, encoding="utf-8")
    make_db(data / "kvservice" / "kv.db")
    alloc = FakeAllocator({"pool-core": BLOCK})

    target = discover(state, alloc, run_admin_fn=local_admin)[0]

    assert target.label == ""
    assert target.effective_label == "pool-core"


# ------------------------------------------------------------ non-pooled parity


def test_non_pooled_service_remote_key_is_byte_identical_to_today(state: StateDir) -> None:
    """No `pool.json` at all -- the exact scenario every existing backup test
    already covers -- must produce the exact same key as before this change."""
    data = make_service(state, "kvservice")
    make_db(data / "kv.db")
    alloc = FakeAllocator({"kvservice": BLOCK})
    cfg = BackupConfig(bucket=BUCKET)

    target = discover(state, alloc, run_admin_fn=local_admin)[0]

    assert target.label == ""
    assert target.archive_name(STAMP) == f"kvservice-kv-{STAMP}.db.gz"
    assert (
        cfg.remote_object(target.effective_label, target.archive_name(STAMP))
        == f"r2:{BUCKET}/daily/kvservice/kvservice-kv-{STAMP}.db.gz"
    )
    # Same shape `Target` construction the pre-pool tests use still works.
    assert target == Target("kvservice", data / "kv.db", BLOCK, label="")


# ---------------------------------------------------------------------- snapshot


def test_snapshot_of_a_pool_member_is_named_by_label_not_pool_id(
    state: StateDir, tmp_path: Path
) -> None:
    data = make_pool(state, "pool-core", ["kvservice"])
    make_db(data / "kvservice" / "kv.db")
    target = discover(state, FakeAllocator({"pool-core": BLOCK}), run_admin_fn=local_admin)[0]
    workdir = tmp_path / "work"
    workdir.mkdir()

    gz = snapshot(target, workdir, stamp=STAMP, run_admin_fn=local_admin)

    assert gz.name == f"kvservice-kv-{STAMP}.db.gz"
    assert "pool-core" not in gz.name
