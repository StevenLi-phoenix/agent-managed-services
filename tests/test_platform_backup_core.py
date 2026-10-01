"""Backup of the Cordis-based ``core`` service (PLAN-core §4.2).

The core service root is ``R = <state>/services/core/root`` and everything
worth keeping is under ``R/data`` (``CORE_STATE_DIR``):

    R/data/core.sqlite                 core's own state (transitions, artifacts index)
    R/data/artifacts/...               core's ArtifactStore (immutable, content-addressed)
    R/data/data/state.sqlite           the store plugin's state
    R/data/data/{blobs,oss-bytes,pages-content}/...   immutable, content-named bytes

What this file pins:

* both databases are found by the *existing* `discover` (its ``-maxdepth 2``
  already reaches ``data/data/state.sqlite``) and get ordinary R2 keys under
  ``daily/core/`` -- nothing about any existing key changes;
* the three byte stores (and core's ``artifacts/``) are copied with
  ``rclone copy --immutable`` (never
  ``sync``, never deleted remotely) to a stable prefix OUTSIDE the pruned
  ``daily/`` one -- otherwise the 14-day retention sweep would delete every
  blob older than 14 days;
* they are copied AFTER every sqlite snapshot, so a snapshot can never name a
  blob the bucket does not have;
* ``R/releases``, ``R/build``, ``R/etc`` (secrets, D16) and ``R/run`` are never
  looked at.

Reuses the fixtures and fakes of ``test_platform_backup.py`` (same pattern as
``test_platform_backup_pool.py``).
"""

from __future__ import annotations

import io
import json
import logging
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from test_platform_backup import (
    ACCESS_KEY,
    BLOCK,
    BUCKET,
    CREDS,
    SECRET_KEY,
    SECRET_VALUES,
    STAMP,
    FakeAllocator,
    RecordingRunner,
    local_admin,
    make_db,
    make_service,
)

from ams.platform import backup
from ams.platform.backup import (
    BackupConfig,
    ByteStore,
    discover,
    discover_byte_stores,
    prune_argv,
    run,
)
from ams.secrets import SecretStore
from ams.state import StateDir
from ams.uidmap import UidBlock
from ams.userns import AdminResult

CORE_BLOCK = UidBlock(101_024, 101_024, 1024)
RCLONE = Path("/store/bin/rclone")


@pytest.fixture
def state(tmp_path: Path) -> StateDir:
    root = tmp_path / "state"
    root.mkdir()
    return StateDir(root)


@pytest.fixture
def store(state: StateDir) -> SecretStore:
    secrets = SecretStore(state.root)
    for name, value in {
        backup.SECRET_ACCESS_KEY_ID: ACCESS_KEY,
        backup.SECRET_SECRET_ACCESS_KEY: SECRET_KEY,
        backup.SECRET_ENDPOINT: CREDS.endpoint,
        backup.SECRET_BUCKET: BUCKET,
    }.items():
        secrets.set(backup.SECRET_SERVICE_ID, name, value.encode())
    return secrets


def make_core(state: StateDir) -> Path:
    """A core service root shaped like PLAN-core §2, including the parts that
    must never be backed up (each holding bait that would match if scanned)."""
    data = make_service(state, "core")
    root = state.service_root("core")
    make_db(data / "core.sqlite")
    (data / "core.sqlite-wal").write_bytes(b"wal")
    (data / "artifacts" / "ab").mkdir(parents=True)
    (data / "artifacts" / "ab" / "abcdef.json").write_text("{}")
    make_db(data / "data" / "state.sqlite")
    for name in backup.BYTE_STORE_NAMES:
        leaf = data / "data" / name / "v1" / "ab"
        leaf.mkdir(parents=True)
        # Content-named and one of them even ends in ".db": still a blob, not a
        # database -- it is three levels down, inside a byte store.
        (leaf / "abcdef0123.db").write_bytes(b"immutable bytes")
    for excluded in ("releases/0123abc", "build/artifacts/0123abc", "etc", "run"):
        d = root / excluded
        d.mkdir(parents=True)
        make_db(d / "bait.sqlite")
        (d / "blobs").mkdir()
    return data


class RcloneAdmin:
    """`run_admin` fake: runs `find`/snapshot for real, records rclone calls."""

    def __init__(self, events: list[str] | None = None, *, fail_on: str = "") -> None:
        self.calls: list[tuple[tuple[str, ...], UidBlock, dict[str, str]]] = []
        self.events = events if events is not None else []
        self.fail_on = fail_on

    def __call__(
        self,
        argv: Sequence[str],
        block: UidBlock,
        *,
        timeout_s: float = 60.0,
        env: Mapping[str, str] | None = None,
    ) -> AdminResult:
        if argv[0] == str(RCLONE):
            self.calls.append((tuple(argv), block, dict(env or {})))
            self.events.append(f"bytes:{Path(argv[-2]).name}")
            if self.fail_on and self.fail_on in argv[-2]:
                return AdminResult(tuple(argv), 1, b"", f"auth failed for {SECRET_KEY}".encode())
            return AdminResult(tuple(argv), 0, b"", b"")
        if argv[0] == sys.executable:
            self.events.append(f"snapshot:{Path(argv[-2]).name}")
        return local_admin(argv, block, timeout_s=timeout_s)


class OrderedRunner(RecordingRunner):
    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self.events = events

    def __call__(self, argv, env, timeout_s):  # noqa: ANN001 - matches backup.Runner
        self.events.append(f"{argv[1]}:{Path(argv[-2]).name if argv[1] == 'copyto' else ''}")
        return super().__call__(argv, env, timeout_s)


def _cfg(**over: object) -> BackupConfig:
    kwargs: dict[str, object] = {"bucket": BUCKET, "rclone": RCLONE}
    kwargs.update(over)
    return BackupConfig(**kwargs)  # type: ignore[arg-type]


# ----------------------------------------------------------------- databases


def test_existing_discover_finds_both_core_databases_and_nothing_else(state: StateDir) -> None:
    data = make_core(state)

    targets = discover(state, FakeAllocator({"core": CORE_BLOCK}), run_admin_fn=local_admin)

    assert [t.db_path for t in targets] == [data / "core.sqlite", data / "data" / "state.sqlite"]
    assert {t.block for t in targets} == {CORE_BLOCK}


def test_core_database_keys_are_ordinary_daily_keys(state: StateDir) -> None:
    make_core(state)
    cfg = _cfg()

    targets = discover(state, FakeAllocator({"core": CORE_BLOCK}), run_admin_fn=local_admin)

    assert [cfg.remote_object(t.service_id, t.archive_name(STAMP)) for t in targets] == [
        f"r2:{BUCKET}/daily/core/core-core-{STAMP}.db.gz",
        f"r2:{BUCKET}/daily/core/core-state-{STAMP}.db.gz",
    ]


# --------------------------------------------------------------- byte stores


def test_discover_byte_stores_finds_the_three_stores_under_data(state: StateDir) -> None:
    data = make_core(state)

    stores = discover_byte_stores(
        state, FakeAllocator({"core": CORE_BLOCK}), run_admin_fn=local_admin
    )

    assert stores == [
        ByteStore("core", data / "artifacts", CORE_BLOCK, "artifacts"),
        ByteStore("core", data / "data" / "blobs", CORE_BLOCK, "data/blobs"),
        ByteStore("core", data / "data" / "oss-bytes", CORE_BLOCK, "data/oss-bytes"),
        ByteStore("core", data / "data" / "pages-content", CORE_BLOCK, "data/pages-content"),
    ]


def test_byte_store_remote_is_a_stable_prefix_outside_the_pruned_one(state: StateDir) -> None:
    data = make_core(state)
    cfg = _cfg()
    blobs = ByteStore("core", data / "data" / "blobs", CORE_BLOCK, "data/blobs")

    remote = cfg.byte_store_remote(blobs)

    assert remote == f"r2:{BUCKET}/bytes/core/data/blobs"
    assert not remote.startswith(cfg.prune_target())


def test_a_file_named_like_a_store_is_not_a_store(state: StateDir) -> None:
    data = make_service(state, "svc")
    (data / "blobs").write_bytes(b"a file, not a dir")
    (data / "sub").mkdir()
    (data / "sub" / "oss-bytes-old").mkdir()

    assert (
        discover_byte_stores(state, FakeAllocator({"svc": BLOCK}), run_admin_fn=local_admin) == []
    )


def test_a_store_nested_inside_a_store_is_not_listed_twice(state: StateDir) -> None:
    data = make_service(state, "svc")
    (data / "blobs" / "x" / "oss-bytes").mkdir(parents=True)

    stores = discover_byte_stores(state, FakeAllocator({"svc": BLOCK}), run_admin_fn=local_admin)

    assert [s.rel for s in stores] == ["blobs"]


def test_byte_store_find_output_is_untrusted(
    state: StateDir, caplog: pytest.LogCaptureFixture
) -> None:
    data = make_service(state, "svc")
    stdout = (
        f"/etc/blobs\n{data}/blobs\n{data}/not-a-store\n{data}/../escape/blobs\n"
        f"{data}/we ird/blobs\n{data}/blobs/inner/oss-bytes\nrelative/blobs\n\n"
    ).encode()

    def canned_admin(argv, block, *, timeout_s=60.0):  # noqa: ANN001
        return AdminResult(tuple(argv), 0, stdout, b"")

    with caplog.at_level(logging.WARNING):
        stores = discover_byte_stores(
            state, FakeAllocator({"svc": BLOCK}), run_admin_fn=canned_admin
        )

    assert [str(s.path) for s in stores] == [f"{data}/blobs"]
    for bad in ("/etc/blobs", "not-a-store", "escape", "we ird", "relative/blobs"):
        assert bad in caplog.text


def test_byte_store_find_argv_is_grouped_and_prunes() -> None:
    argv = backup._byte_store_find_argv(Path("/srv/data"))
    assert argv[:5] == ["find", "/srv/data", "-mindepth", "1", "-type"]
    assert argv.count("(") == 1 and argv.count(")") == 1
    assert argv[-2:] == ["-print", "-prune"]
    assert [argv[i + 1] for i, a in enumerate(argv) if a == "-name"] == list(
        backup.BYTE_STORE_NAMES
    )


def test_byte_store_discovery_skips_services_without_a_block_or_data(state: StateDir) -> None:
    make_service(state, "stateless", data=False)
    make_core(state)
    calls: list[Sequence[str]] = []

    def counting_admin(argv, block, *, timeout_s=60.0):  # noqa: ANN001
        calls.append(argv)
        return AdminResult(tuple(argv), 0, b"", b"")

    assert discover_byte_stores(state, FakeAllocator({}), run_admin_fn=counting_admin) == []
    assert calls == []


def test_byte_store_discovery_survives_a_failing_listing(
    state: StateDir, caplog: pytest.LogCaptureFixture
) -> None:
    make_core(state)

    def failing(argv, block, *, timeout_s=60.0):  # noqa: ANN001
        return AdminResult(tuple(argv), 1, b"", b"find: permission denied")

    with caplog.at_level(logging.WARNING):
        stores = discover_byte_stores(
            state, FakeAllocator({"core": CORE_BLOCK}), run_admin_fn=failing
        )
    assert stores == []
    assert "permission denied" in caplog.text


# ----------------------------------------------------------------- config


@pytest.mark.parametrize(
    "over",
    [
        {"bytes_prefix": "daily"},
        {"bytes_prefix": "daily/bytes"},
        {"prefix": "backups", "bytes_prefix": "backups/bytes"},
        {"prefix": "bytes/daily"},
        {"bytes_prefix": ""},
        {"bytes_prefix": "/bytes"},
        {"bytes_prefix": "bytes/"},
    ],
)
def test_a_bytes_prefix_the_prune_could_reach_is_refused(over: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="prefix"):
        _cfg(**over)


def test_default_bytes_prefix() -> None:
    assert backup.DEFAULT_BYTES_PREFIX == "bytes"
    assert _cfg().bytes_prefix == "bytes"


# --------------------------------------------------------------------- run


def test_run_copies_byte_stores_after_every_sqlite_snapshot(
    state: StateDir, store: SecretStore
) -> None:
    make_core(state)
    make_db(make_service(state, "zeta") / "z.db")  # sorts after core
    events: list[str] = []
    admin = RcloneAdmin(events)
    runner = OrderedRunner(events)

    report = run(
        state,
        FakeAllocator({"core": CORE_BLOCK, "zeta": BLOCK}),
        store,
        stamp=STAMP,
        stream=io.StringIO(),
        run_admin_fn=admin,
        runner=runner,
        cfg=_cfg(),
    )

    assert report.ok
    byte_events = [i for i, e in enumerate(events) if e.startswith("bytes:")]
    db_events = [i for i, e in enumerate(events) if e.startswith(("snapshot:", "copyto:"))]
    assert len(byte_events) == 4 and len(db_events) == 6  # 3 stores + artifacts/
    assert max(db_events) < min(byte_events)
    assert events[-1] == "delete:"  # the retention sweep still runs last


def test_run_byte_store_argv_is_an_immutable_copy_in_the_admin_ns(
    state: StateDir, store: SecretStore
) -> None:
    data = make_core(state)
    admin = RcloneAdmin()

    run(
        state,
        FakeAllocator({"core": CORE_BLOCK}),
        store,
        stamp=STAMP,
        stream=io.StringIO(),
        run_admin_fn=admin,
        runner=RecordingRunner(),
        cfg=_cfg(),
    )

    argvs = [call[0] for call in admin.calls]
    assert argvs == [
        (
            str(RCLONE),
            "copy",
            "--immutable",
            "--s3-no-check-bucket",
            "--exclude",
            "*.tmp",
            str(data / rel),
            f"r2:{BUCKET}/bytes/core/{rel}",
        )
        for rel in ["artifacts", *(f"data/{name}" for name in backup.BYTE_STORE_NAMES)]
    ]
    for argv, block, env in admin.calls:
        assert block == CORE_BLOCK  # only inner root in the service's map can read them
        assert env["RCLONE_CONFIG_R2_SECRET_ACCESS_KEY"] == SECRET_KEY
        assert env["RCLONE_CONFIG"] == "/dev/null"
        assert not any(secret in " ".join(argv) for secret in SECRET_VALUES)
        assert not {"sync", "delete", "move", "purge"} & set(argv)


def test_run_records_byte_stores_and_keeps_the_prune_on_daily_only(
    state: StateDir, store: SecretStore
) -> None:
    make_core(state)
    out = io.StringIO()
    runner = RecordingRunner()
    cfg = _cfg()

    report = run(
        state,
        FakeAllocator({"core": CORE_BLOCK}),
        store,
        stamp=STAMP,
        stream=out,
        run_admin_fn=RcloneAdmin(),
        runner=runner,
        cfg=cfg,
    )

    records = [json.loads(line) for line in out.getvalue().splitlines()]
    kinds = [r["kind"] for r in records]
    assert kinds == [
        "BackupSucceeded",
        "BackupSucceeded",
        "ByteStoreSynced",
        "ByteStoreSynced",
        "ByteStoreSynced",
        "ByteStoreSynced",
        "BackupRunFinished",
    ]
    assert records[2]["service_id"] == "core"
    assert records[2]["event"]["remote"] == f"r2:{BUCKET}/bytes/core/artifacts"
    assert records[3]["event"]["remote"] == f"r2:{BUCKET}/bytes/core/data/blobs"
    assert records[-1]["event"]["byte_stores"] == 4
    assert records[-1]["event"]["byte_stores_failed"] == 0
    assert [len(r.remote or "") > 0 for r in report.byte_stores] == [True] * 4
    assert runner.calls[-1][0] == tuple(prune_argv(cfg))
    assert prune_argv(cfg)[-1] == f"r2:{BUCKET}/daily/"


def test_a_failing_byte_store_escalates_redacted_and_the_others_still_run(
    state: StateDir, store: SecretStore, caplog: pytest.LogCaptureFixture
) -> None:
    make_core(state)
    out = io.StringIO()
    admin = RcloneAdmin(fail_on="oss-bytes")

    with caplog.at_level(logging.DEBUG, logger="ams"):
        report = run(
            state,
            FakeAllocator({"core": CORE_BLOCK}),
            store,
            stamp=STAMP,
            stream=out,
            run_admin_fn=admin,
            runner=RecordingRunner(),
            cfg=_cfg(),
        )

    assert report.exit_code == 1
    assert [r.status for r in report.byte_stores] == ["ok", "ok", "failed", "ok"]
    assert len(admin.calls) == 4
    failed = [json.loads(line) for line in out.getvalue().splitlines()]
    failed = [r for r in failed if r["kind"] == "ByteStoreFailed"]
    assert len(failed) == 1 and failed[0]["action"] == "escalate"
    assert "<redacted>" in failed[0]["reason"]
    for secret in SECRET_VALUES:
        assert secret not in out.getvalue()
        assert secret not in caplog.text


def test_dry_run_plans_byte_store_copies_without_running_rclone(state: StateDir) -> None:
    make_core(state)
    out = io.StringIO()
    admin = RcloneAdmin()

    report = run(
        state,
        FakeAllocator({"core": CORE_BLOCK}),
        SecretStore(state.root),  # empty: dry runs work before credentials exist
        dry_run=True,
        stamp=STAMP,
        stream=out,
        run_admin_fn=admin,
        runner=RecordingRunner(),
        cfg=_cfg(),
    )

    assert report.ok
    assert admin.calls == []
    planned = [json.loads(line) for line in out.getvalue().splitlines()]
    planned = [r for r in planned if r["kind"] == "ByteStoreSynced"]
    assert len(planned) == 4
    assert planned[0]["event"]["argv"][:4] == [
        str(RCLONE),
        "copy",
        "--immutable",
        "--s3-no-check-bucket",
    ]
    assert planned[0]["event"]["env_names"] == list(backup.RCLONE_ENV_NAMES)
    assert "dry run" in planned[0]["reason"]


def test_only_restricts_byte_stores_too(state: StateDir, store: SecretStore) -> None:
    make_core(state)
    other = make_service(state, "files")
    (other / "blobs").mkdir()
    admin = RcloneAdmin()

    run(
        state,
        FakeAllocator({"core": CORE_BLOCK, "files": BLOCK}),
        store,
        only=["files"],
        stamp=STAMP,
        stream=io.StringIO(),
        run_admin_fn=admin,
        runner=RecordingRunner(),
        cfg=_cfg(),
    )

    assert [Path(call[0][-2]) for call in admin.calls] == [other / "blobs"]


def test_a_service_without_byte_stores_makes_no_rclone_admin_call(
    state: StateDir, store: SecretStore
) -> None:
    make_db(make_service(state, "svc") / "svc.db")
    admin = RcloneAdmin()

    report = run(
        state,
        FakeAllocator({"svc": BLOCK}),
        store,
        stamp=STAMP,
        stream=io.StringIO(),
        run_admin_fn=admin,
        runner=RecordingRunner(),
        cfg=_cfg(),
    )

    assert report.ok and report.byte_stores == ()
    assert admin.calls == []


# ------------------------------------- review 2026-09-29: artifacts/, *.tmp staging


def test_core_artifact_store_is_a_byte_store(state: StateDir) -> None:
    """upstream-fit-2: core.sqlite without R/data/artifacts restores every plugin
    as ``artifact_unreadable``; upstream core-daily-backup copies artifacts/ too.
    The files are content-addressed (``<artifactId>.json``) and immutable."""
    data = make_core(state)

    stores = discover_byte_stores(
        state, FakeAllocator({"core": CORE_BLOCK}), run_admin_fn=local_admin
    )

    artifacts = [s for s in stores if s.rel == "artifacts"]
    assert artifacts == [ByteStore("core", data / "artifacts", CORE_BLOCK, "artifacts")]
    assert _cfg().byte_store_remote(artifacts[0]) == f"r2:{BUCKET}/bytes/core/artifacts"


def test_an_artifacts_dir_of_another_service_is_not_a_byte_store(state: StateDir) -> None:
    data = make_service(state, "svc")
    (data / "artifacts").mkdir()
    (data / "artifacts" / "x.json").write_text("{}")

    assert (
        discover_byte_stores(state, FakeAllocator({"svc": BLOCK}), run_admin_fn=local_admin) == []
    )


def test_nested_artifacts_dir_under_core_data_is_not_the_artifact_store(state: StateDir) -> None:
    data = make_core(state)
    (data / "data" / "artifacts").mkdir()

    stores = discover_byte_stores(
        state, FakeAllocator({"core": CORE_BLOCK}), run_admin_fn=local_admin
    )

    assert [s.rel for s in stores if "artifacts" in s.rel] == ["artifacts"]


def test_byte_store_copy_skips_in_flight_tmp_files(state: StateDir) -> None:
    """upstream-fit-9: LocalOssBytes stages uploads as ``<uuid>.tmp`` and
    ArtifactStore.put as ``<id>.json.<pid>.<ts>.tmp`` inside the live store; a
    copy must neither upload them for good nor fail when one vanishes."""
    data = make_core(state)
    argv = backup.byte_store_argv(
        ByteStore("core", data / "data" / "blobs", CORE_BLOCK, "data/blobs"), _cfg()
    )
    i = argv.index("--exclude")
    assert argv[i + 1] == "*.tmp"
    assert i < len(argv) - 2  # an option, before source and destination
