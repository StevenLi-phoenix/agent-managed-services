"""Portable tests for `ams.platform.backup`.

What is portable here and what is not. The admin user namespace is not, so
`run_admin` is injected. But the *code it runs* is: `_SNAPSHOT_CODE` is a plain
Python program over stdlib sqlite3, so `local_admin` below executes the real
argv with `subprocess.run` and every snapshot assertion exercises the real
snapshot program -- only the namespace is faked. Same for `discover`: `find`
with the argv this module builds runs identically on macOS and Linux.

The one thing that cannot be checked without credentials is that R2 accepts the
upload. `tests/linux/test_platform_backup_live.py` covers the part that
matters more anyway (a service-owned database really does come back), and the
rclone argv is pinned here by equality so the live drill is testing transport,
not construction.
"""

from __future__ import annotations

import gzip
import io
import json
import logging
import sqlite3
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from ams.platform import backup
from ams.platform.backup import (
    BackupConfig,
    BackupError,
    R2Credentials,
    Target,
    discover,
    prune_argv,
    restore,
    run,
    snapshot,
    upload_argv,
)
from ams.secrets import SecretStore
from ams.state import StateDir
from ams.uidmap import UidBlock
from ams.userns import AdminResult

BLOCK = UidBlock(100_000, 100_000, 1024)
STAMP = "20260902"

# Deliberately distinctive so a leak into a log line or a JSON record is
# unmissable rather than a substring of something plausible.
ACCESS_KEY = "AKIAtestKEYIDvalue0001"
SECRET_KEY = "s3cr3t-do-not-log-me-0002"
ENDPOINT = "https://acct0003.r2.cloudflarestorage.com"
BUCKET = "phm-backups-test"
#: Values that must never reach a log line, an exception or the report. The
#: bucket is excluded on purpose: it is a locator and it is half of every
#: object key the report prints (see `R2Credentials.redact`).
SECRET_VALUES = (ACCESS_KEY, SECRET_KEY, ENDPOINT)

CREDS = R2Credentials(ACCESS_KEY, SECRET_KEY, ENDPOINT, BUCKET)


# ----------------------------------------------------------------- fixtures


def local_admin(argv: Sequence[str], block: UidBlock, *, timeout_s: float = 60.0) -> AdminResult:
    """`run_admin` minus the namespace: same argv, run as the test user.

    Everything `run_admin` adds (the uid map, inner root) exists so the harness
    can reach a file owned by another uid. Off the target host there is no other
    uid, so running the identical argv directly exercises the same program.
    """
    proc = subprocess.run(list(argv), capture_output=True, timeout=timeout_s, check=False)
    return AdminResult(tuple(argv), proc.returncode, proc.stdout, proc.stderr)


class FakeAllocator:
    """`UidAllocator.get` is the whole surface `discover` uses."""

    def __init__(self, blocks: dict[str, UidBlock]) -> None:
        self._blocks = blocks

    def get(self, service_id: str) -> UidBlock | None:
        return self._blocks.get(service_id)


class RecordingRunner:
    """Captures every rclone invocation; optionally fails on cue."""

    def __init__(self, returncode: int = 0, stderr: str = "") -> None:
        self.calls: list[tuple[tuple[str, ...], dict[str, str]]] = []
        self.returncode = returncode
        self.stderr = stderr

    def __call__(self, argv, env, timeout_s):  # noqa: ANN001 - matches backup.Runner
        self.calls.append((tuple(argv), dict(env)))
        return backup.CommandResult(tuple(argv), self.returncode, "", self.stderr)


def make_service(state: StateDir, service_id: str, *, data: bool = True) -> Path:
    """A service dir shaped the way `StateDir.list_service_ids` recognises."""
    service_dir = state.service_dir(service_id)
    service_dir.mkdir(parents=True, exist_ok=True)
    (service_dir / "service.toml").write_text(f'id = "{service_id}"\n')
    data_dir = state.service_root(service_id) / "data"
    if data:
        data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def make_db(path: Path, *, rows: Sequence[str] = ("hello",)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.execute("CREATE TABLE IF NOT EXISTS notes (body TEXT)")
        con.executemany("INSERT INTO notes (body) VALUES (?)", [(r,) for r in rows])
        con.commit()
    finally:
        con.close()
    return path


@pytest.fixture
def state(tmp_path: Path) -> StateDir:
    root = tmp_path / "state"
    root.mkdir()
    return StateDir(root)


@pytest.fixture
def store(state: StateDir) -> SecretStore:
    store = SecretStore(state.root)
    values = {
        backup.SECRET_ACCESS_KEY_ID: ACCESS_KEY,
        backup.SECRET_SECRET_ACCESS_KEY: SECRET_KEY,
        backup.SECRET_ENDPOINT: ENDPOINT,
        backup.SECRET_BUCKET: BUCKET,
    }
    assert set(values) == set(backup.SECRET_NAMES)
    for name, value in values.items():
        store.set(backup.SECRET_SERVICE_ID, name, value.encode())
    return store


# ------------------------------------------------------------------ discover


def test_discover_parses_find_output_into_targets(state: StateDir) -> None:
    data = make_service(state, "kvservice")
    make_db(data / "kv.db")
    make_db(data / "sessions.sqlite3")
    (data / "kv.db-wal").write_bytes(b"not a database")
    alloc = FakeAllocator({"kvservice": BLOCK})

    targets = discover(state, alloc, run_admin_fn=local_admin)

    assert [t.db_path.name for t in targets] == ["kv.db", "sessions.sqlite3"]
    assert {t.service_id for t in targets} == {"kvservice"}
    assert all(t.block == BLOCK for t in targets)
    # The -wal sidecar does not end in a db suffix, so it is never a target.
    assert not any("wal" in t.db_path.name for t in targets)


def test_discover_orders_by_service_then_path(state: StateDir) -> None:
    for service_id in ("zeta", "alpha"):
        data = make_service(state, service_id)
        make_db(data / "b.db")
        make_db(data / "a.db")
    alloc = FakeAllocator({"alpha": BLOCK, "zeta": BLOCK})

    targets = discover(state, alloc, run_admin_fn=local_admin)

    assert [(t.service_id, t.db_path.name) for t in targets] == [
        ("alpha", "a.db"),
        ("alpha", "b.db"),
        ("zeta", "a.db"),
        ("zeta", "b.db"),
    ]


def test_discover_skips_services_with_no_data_dir_without_forking(state: StateDir) -> None:
    make_service(state, "stateless", data=False)
    calls: list[Sequence[str]] = []

    def counting_admin(argv, block, *, timeout_s=60.0):  # noqa: ANN001
        calls.append(argv)
        return AdminResult(tuple(argv), 0, b"", b"")

    assert discover(state, FakeAllocator({"stateless": BLOCK}), run_admin_fn=counting_admin) == []
    assert calls == []


def test_discover_skips_a_service_with_no_uid_block(
    state: StateDir, caplog: pytest.LogCaptureFixture
) -> None:
    data = make_service(state, "unstarted")
    make_db(data / "x.db")
    with caplog.at_level(logging.WARNING):
        assert discover(state, FakeAllocator({}), run_admin_fn=local_admin) == []
    assert "no uid block" in caplog.text


def test_discover_only_filters_and_warns_for_a_miss(
    state: StateDir, caplog: pytest.LogCaptureFixture
) -> None:
    for service_id in ("wanted", "other"):
        make_db(make_service(state, service_id) / "x.db")
    alloc = FakeAllocator({"wanted": BLOCK, "other": BLOCK})

    with caplog.at_level(logging.WARNING):
        targets = discover(state, alloc, only=["wanted", "absent"], run_admin_fn=local_admin)

    assert [t.service_id for t in targets] == ["wanted"]
    assert "absent: no database found" in caplog.text


def test_discover_ignores_output_outside_the_data_dir(
    state: StateDir, caplog: pytest.LogCaptureFixture
) -> None:
    """`find` output describes service-written files, so it is untrusted."""
    data = make_service(state, "svc")
    stdout = f"/etc/shadow.db\n{data}/real.db\nrelative.db\n\n".encode()

    def canned_admin(argv, block, *, timeout_s=60.0):  # noqa: ANN001
        return AdminResult(tuple(argv), 0, stdout, b"")

    with caplog.at_level(logging.WARNING):
        targets = discover(state, FakeAllocator({"svc": BLOCK}), run_admin_fn=canned_admin)

    assert [str(t.db_path) for t in targets] == [f"{data}/real.db"]
    assert "/etc/shadow.db" in caplog.text
    assert "relative.db" in caplog.text


def test_discover_never_backs_up_registry_runtime_db(state: StateDir) -> None:
    """Heartbeats are ephemeral liveness state; restoring them resurrects lies."""
    data = make_service(state, "registry")
    make_db(data / "registry.db")
    make_db(data / "registry-runtime.db")

    targets = discover(state, FakeAllocator({"registry": BLOCK}), run_admin_fn=local_admin)

    assert [t.db_path.name for t in targets] == ["registry.db"]


def test_discover_survives_a_failing_listing(
    state: StateDir, caplog: pytest.LogCaptureFixture
) -> None:
    make_service(state, "broken")
    make_service(state, "fine")
    make_db(state.service_root("fine") / "data" / "ok.db")

    def flaky_admin(argv, block, *, timeout_s=60.0):  # noqa: ANN001
        if "broken" in " ".join(argv):
            return AdminResult(tuple(argv), 1, b"", b"find: permission denied")
        return local_admin(argv, block, timeout_s=timeout_s)

    alloc = FakeAllocator({"broken": BLOCK, "fine": BLOCK})
    with caplog.at_level(logging.WARNING):
        targets = discover(state, alloc, run_admin_fn=flaky_admin)

    assert [t.service_id for t in targets] == ["fine"]
    assert "permission denied" in caplog.text


def test_find_argv_is_a_grouped_argv_not_a_shell_string() -> None:
    argv = backup._find_argv(Path("/srv/data"))
    assert argv[:6] == ["find", "/srv/data", "-maxdepth", "2", "-type", "f"]
    assert argv.count("(") == 1 and argv.count(")") == 1
    assert argv[-1] == "-print"
    assert [argv[i + 1] for i, a in enumerate(argv) if a == "-name"] == [
        "*.db",
        "*.sqlite",
        "*.sqlite3",
    ]
    assert not any(" " in token and "-" not in token for token in argv[6:])


# ------------------------------------------------------------------ snapshot


def test_snapshot_names_the_archive_and_leaves_no_intermediate(
    state: StateDir, tmp_path: Path
) -> None:
    data = make_service(state, "kvservice")
    target = Target("kvservice", make_db(data / "kv.db"), BLOCK)
    workdir = tmp_path / "work"
    workdir.mkdir()

    gz = snapshot(target, workdir, stamp=STAMP, run_admin_fn=local_admin)

    assert gz.name == "kvservice-kv-20260902.db.gz"
    assert gz.parent == workdir
    assert list(workdir.iterdir()) == [gz]  # the uncompressed snapshot is gone
    assert gzip.decompress(gz.read_bytes())[:16] == b"SQLite format 3\x00"


def test_snapshot_strips_only_the_last_suffix(state: StateDir, tmp_path: Path) -> None:
    data = make_service(state, "svc")
    target = Target("svc", make_db(data / "app.v2.sqlite3"), BLOCK)

    gz = snapshot(target, tmp_path, stamp=STAMP, run_admin_fn=local_admin)

    assert gz.name == "svc-app.v2-20260902.db.gz"


def test_snapshot_does_not_modify_the_source_database(state: StateDir, tmp_path: Path) -> None:
    data = make_service(state, "svc")
    db = make_db(data / "app.db", rows=("a", "b"))
    before = db.read_bytes()

    snapshot(Target("svc", db, BLOCK), tmp_path, stamp=STAMP, run_admin_fn=local_admin)

    assert db.read_bytes() == before


def test_snapshot_raises_when_the_admin_step_fails(state: StateDir, tmp_path: Path) -> None:
    data = make_service(state, "svc")
    not_a_db = data / "junk.db"
    not_a_db.parent.mkdir(parents=True, exist_ok=True)
    not_a_db.write_bytes(b"this is not a database" * 100)
    workdir = tmp_path / "work"
    workdir.mkdir()

    with pytest.raises(BackupError, match="sqlite backup"):
        snapshot(Target("svc", not_a_db, BLOCK), workdir, stamp=STAMP, run_admin_fn=local_admin)

    assert list(workdir.iterdir()) == []  # no partial snapshot left behind


def test_snapshot_raises_when_nothing_was_written(state: StateDir, tmp_path: Path) -> None:
    def lying_admin(argv, block, *, timeout_s=60.0):  # noqa: ANN001
        return AdminResult(tuple(argv), 0, b"", b"")

    target = Target("svc", Path("/nonexistent/app.db"), BLOCK)
    with pytest.raises(BackupError, match="wrote no"):
        snapshot(target, tmp_path, stamp=STAMP, run_admin_fn=lying_admin)


def test_snapshot_argv_passes_paths_as_arguments_not_interpolation(
    state: StateDir, tmp_path: Path
) -> None:
    """The snapshot program is a fixed string; paths only ever reach it via argv."""
    data = make_service(state, "svc")
    db = make_db(data / "app.db")
    seen: list[Sequence[str]] = []

    def capturing_admin(argv, block, *, timeout_s=60.0):  # noqa: ANN001
        seen.append(list(argv))
        return local_admin(argv, block, timeout_s=timeout_s)

    snapshot(Target("svc", db, BLOCK), tmp_path, stamp=STAMP, run_admin_fn=capturing_admin)

    argv = seen[0]
    assert argv[0] == sys.executable
    assert argv[1:3] == ["-I", "-c"]
    assert argv[3] == backup._SNAPSHOT_CODE
    assert argv[4] == str(db)
    assert str(db) not in argv[3]


# -------------------------------------------------------------------- rclone


def test_upload_argv_is_exact() -> None:
    cfg = BackupConfig(bucket=BUCKET, rclone=Path("/home/harness/store/bin/rclone"))
    local = Path("/tmp/work/kvservice-kv-20260902.db.gz")

    argv = upload_argv(local, cfg.remote_object("kvservice", local.name), cfg.rclone)

    assert argv == [
        "/home/harness/store/bin/rclone",
        "copyto",
        "--s3-no-check-bucket",
        "/tmp/work/kvservice-kv-20260902.db.gz",
        f"r2:{BUCKET}/daily/kvservice/kvservice-kv-20260902.db.gz",
    ]


def test_prune_argv_is_exact() -> None:
    cfg = BackupConfig(bucket=BUCKET, rclone=Path("/home/harness/store/bin/rclone"))

    assert prune_argv(cfg) == [
        "/home/harness/store/bin/rclone",
        "delete",
        "--min-age",
        "14d",
        f"r2:{BUCKET}/daily/",
    ]


def test_rclone_env_carries_the_credentials_and_disables_the_config_file() -> None:
    env = CREDS.env()

    assert set(env) == set(backup.RCLONE_ENV_NAMES)
    assert env["RCLONE_CONFIG_R2_TYPE"] == "s3"
    assert env["RCLONE_CONFIG_R2_PROVIDER"] == "Cloudflare"
    assert env["RCLONE_CONFIG_R2_ACCESS_KEY_ID"] == ACCESS_KEY
    assert env["RCLONE_CONFIG"] == "/dev/null"


def test_credentials_never_render_themselves() -> None:
    assert ACCESS_KEY not in repr(CREDS)
    assert SECRET_KEY not in repr(CREDS)
    assert SECRET_KEY not in str(CREDS)
    assert SECRET_KEY not in f"{CREDS}"


def test_upload_redacts_credentials_that_leak_into_stderr() -> None:
    runner = RecordingRunner(returncode=1, stderr=f"auth failed for {ACCESS_KEY}:{SECRET_KEY}")
    cfg = BackupConfig(bucket=BUCKET)

    with pytest.raises(BackupError) as excinfo:
        backup.upload(Path("/tmp/a.gz"), "svc", cfg, CREDS, runner=runner)

    message = str(excinfo.value)
    assert not any(value in message for value in SECRET_VALUES)
    assert "<redacted>" in message


def test_prune_failure_is_an_error_not_a_warning() -> None:
    runner = RecordingRunner(returncode=3, stderr="quota exceeded")

    with pytest.raises(BackupError, match="retention prune exited 3"):
        backup.prune(BackupConfig(bucket=BUCKET), CREDS, runner=runner)


def test_base_env_inherits_no_rclone_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RCLONE_CONFIG_R2_ACCESS_KEY_ID", "inherited-and-wrong")
    assert not any(k.startswith("RCLONE") for k in backup._base_env())


# ------------------------------------------------------------------- restore


def test_restore_round_trips_a_row(state: StateDir, tmp_path: Path) -> None:
    data = make_service(state, "kvservice")
    db = make_db(data / "kv.db", rows=("the row that must come back",))
    gz = snapshot(Target("kvservice", db, BLOCK), tmp_path, stamp=STAMP, run_admin_fn=local_admin)

    result = restore(gz, tmp_path / "scratch" / "restored.db")

    assert result.ok and result.integrity == "ok"
    assert result.page_count > 0
    con = sqlite3.connect(result.path)
    try:
        assert con.execute("SELECT body FROM notes").fetchall() == [
            ("the row that must come back",)
        ]
    finally:
        con.close()


def test_restore_rejects_a_corrupt_archive(tmp_path: Path) -> None:
    bad = tmp_path / "bad.db.gz"
    bad.write_bytes(gzip.compress(b"SQLite format 3\x00" + b"\x00" * 4096))

    with pytest.raises(BackupError):
        restore(bad, tmp_path / "out.db")


def test_restore_rejects_a_file_that_is_not_gzip(tmp_path: Path) -> None:
    plain = tmp_path / "plain.db.gz"
    plain.write_bytes(b"not gzip at all")

    with pytest.raises(BackupError, match="cannot decompress"):
        restore(plain, tmp_path / "out.db")


# ----------------------------------------------------------------------- run


def test_run_uploads_every_target_and_prunes_once(state: StateDir, store: SecretStore) -> None:
    for service_id in ("auth", "registry"):
        make_db(make_service(state, service_id) / f"{service_id}.db")
    alloc = FakeAllocator({"auth": BLOCK, "registry": BLOCK})
    runner = RecordingRunner()
    out = io.StringIO()
    cfg = BackupConfig(bucket=BUCKET, rclone=Path("/store/bin/rclone"))

    report = run(
        state,
        alloc,
        store,
        stamp=STAMP,
        stream=out,
        run_admin_fn=local_admin,
        runner=runner,
        cfg=cfg,
    )

    assert report.ok and report.exit_code == 0
    verbs = [call[0][1] for call in runner.calls]
    assert verbs == ["copyto", "copyto", "delete"]
    assert runner.calls[0][0][-1] == f"r2:{BUCKET}/daily/auth/auth-auth-20260902.db.gz"
    assert runner.calls[2][0] == tuple(prune_argv(cfg))


def test_run_continues_past_a_failing_target_and_exits_1(
    state: StateDir, store: SecretStore
) -> None:
    good = make_service(state, "good")
    make_db(good / "good.db")
    bad = make_service(state, "bad")
    (bad / "bad.db").write_bytes(b"definitely not sqlite" * 50)
    alloc = FakeAllocator({"good": BLOCK, "bad": BLOCK})
    runner = RecordingRunner()
    out = io.StringIO()

    report = run(
        state, alloc, store, stamp=STAMP, stream=out, run_admin_fn=local_admin, runner=runner
    )

    assert report.exit_code == 1
    statuses = {r.service_id: r.status for r in report.results}
    assert statuses == {"bad": "failed", "good": "ok"}
    # The healthy service was still uploaded: one bad database must not take
    # the whole sweep down with it.
    assert [c[0][1] for c in runner.calls] == ["copyto", "delete"]
    assert "good-good-20260902.db.gz" in runner.calls[0][0][-2]

    records = [json.loads(line) for line in out.getvalue().splitlines()]
    kinds = [r["kind"] for r in records]
    assert kinds == ["BackupFailed", "BackupSucceeded", "BackupRunFinished"]
    assert records[0]["action"] == "escalate"
    assert records[-1]["event"]["failed"] == 1


def test_run_reports_a_prune_failure_as_a_failure(state: StateDir, store: SecretStore) -> None:
    make_db(make_service(state, "svc") / "svc.db")

    class PruneFails(RecordingRunner):
        def __call__(self, argv, env, timeout_s):  # noqa: ANN001
            self.calls.append((tuple(argv), dict(env)))
            rc = 5 if "delete" in argv else 0
            return backup.CommandResult(tuple(argv), rc, "", "prune blew up")

    out = io.StringIO()
    report = run(
        state,
        FakeAllocator({"svc": BLOCK}),
        store,
        stamp=STAMP,
        stream=out,
        run_admin_fn=local_admin,
        runner=PruneFails(),
    )

    assert report.exit_code == 1
    assert report.prune_error is not None
    kinds = [json.loads(line)["kind"] for line in out.getvalue().splitlines()]
    assert kinds == ["BackupSucceeded", "BackupPruneFailed", "BackupRunFinished"]


def test_run_never_writes_a_credential_to_stdout_or_the_log(
    state: StateDir, store: SecretStore, caplog: pytest.LogCaptureFixture
) -> None:
    make_db(make_service(state, "svc") / "svc.db")
    runner = RecordingRunner(returncode=1, stderr=f"boom: {SECRET_KEY} {ACCESS_KEY}")
    out = io.StringIO()

    with caplog.at_level(logging.DEBUG):
        report = run(
            state,
            FakeAllocator({"svc": BLOCK}),
            store,
            stamp=STAMP,
            stream=out,
            run_admin_fn=local_admin,
            runner=runner,
        )

    assert report.exit_code == 1
    printed = out.getvalue()
    for value in SECRET_VALUES:
        assert value not in printed, f"{value!r} reached stdout"
        assert value not in caplog.text, f"{value!r} reached a log record"
    # ... and the credentials really were handed to rclone, so the assertion
    # above is about redaction, not about them being absent everywhere.
    assert runner.calls[0][1]["RCLONE_CONFIG_R2_SECRET_ACCESS_KEY"] == SECRET_KEY


def test_dry_run_prints_the_argv_and_env_names_and_touches_no_network(
    state: StateDir, store: SecretStore
) -> None:
    make_db(make_service(state, "kvservice") / "kv.db")
    runner = RecordingRunner()
    out = io.StringIO()
    cfg = BackupConfig(bucket=BUCKET, rclone=Path("/home/harness/store/bin/rclone"))

    report = run(
        state,
        FakeAllocator({"kvservice": BLOCK}),
        store,
        dry_run=True,
        stamp=STAMP,
        stream=out,
        run_admin_fn=local_admin,
        runner=runner,
        cfg=cfg,
    )

    assert report.ok and runner.calls == []
    records = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [r["kind"] for r in records] == [
        "BackupSucceeded",
        "BackupPrunePlanned",
        "BackupRunFinished",
    ]
    assert records[0]["event"]["argv"] == [
        "/home/harness/store/bin/rclone",
        "copyto",
        "--s3-no-check-bucket",
        "kvservice-kv-20260902.db.gz",
        f"r2:{BUCKET}/daily/kvservice/kvservice-kv-20260902.db.gz",
    ]
    assert records[0]["event"]["env_names"] == list(backup.RCLONE_ENV_NAMES)
    assert records[1]["event"]["argv"] == prune_argv(cfg)
    printed = out.getvalue()
    assert ACCESS_KEY not in printed and SECRET_KEY not in printed


def test_dry_run_works_before_the_credentials_are_ever_set(state: StateDir) -> None:
    """The first thing anyone runs is a dry run on an empty store."""
    make_db(make_service(state, "svc") / "svc.db")
    empty_store = SecretStore(state.root)
    out = io.StringIO()

    report = run(
        state,
        FakeAllocator({"svc": BLOCK}),
        empty_store,
        dry_run=True,
        stamp=STAMP,
        stream=out,
        run_admin_fn=local_admin,
    )

    assert report.ok
    record = json.loads(out.getvalue().splitlines()[0])
    assert record["event"]["remote"].startswith("r2:<R2_BUCKET>/daily/svc/")


def test_a_real_run_refuses_to_start_without_credentials(state: StateDir) -> None:
    from ams.secrets import MissingSecret

    make_db(make_service(state, "svc") / "svc.db")
    with pytest.raises(MissingSecret):
        run(
            state,
            FakeAllocator({"svc": BLOCK}),
            SecretStore(state.root),
            stamp=STAMP,
            stream=io.StringIO(),
            run_admin_fn=local_admin,
        )


def test_run_with_no_targets_is_a_success_that_says_so(
    state: StateDir, store: SecretStore, caplog: pytest.LogCaptureFixture
) -> None:
    make_service(state, "stateless", data=False)
    out = io.StringIO()
    runner = RecordingRunner()

    with caplog.at_level(logging.WARNING):
        report = run(
            state,
            FakeAllocator({"stateless": BLOCK}),
            store,
            stamp=STAMP,
            stream=out,
            run_admin_fn=local_admin,
            runner=runner,
        )

    assert report.ok and runner.calls == []  # nothing to prune either
    assert "nothing to back up" in caplog.text
    assert [json.loads(line)["kind"] for line in out.getvalue().splitlines()] == [
        "BackupRunFinished"
    ]


def test_run_leaves_no_snapshot_behind(state: StateDir, store: SecretStore) -> None:
    """A snapshot is an unencrypted copy of a service's data; it must not linger."""
    make_db(make_service(state, "svc") / "svc.db")

    run(
        state,
        FakeAllocator({"svc": BLOCK}),
        store,
        stamp=STAMP,
        stream=io.StringIO(),
        run_admin_fn=local_admin,
        runner=RecordingRunner(),
    )

    workdir_root = state.root / "platform" / "backup"
    assert list(workdir_root.iterdir()) == []


def test_run_only_restricts_the_sweep(state: StateDir, store: SecretStore) -> None:
    for service_id in ("a", "b"):
        make_db(make_service(state, service_id) / f"{service_id}.db")
    runner = RecordingRunner()

    report = run(
        state,
        FakeAllocator({"a": BLOCK, "b": BLOCK}),
        store,
        only=["b"],
        stamp=STAMP,
        stream=io.StringIO(),
        run_admin_fn=local_admin,
        runner=runner,
    )

    assert [r.service_id for r in report.results] == ["b"]


# ----------------------------------------------------------------------- cli


def test_cli_restore_reports_ok(state: StateDir, tmp_path: Path, capsys) -> None:  # noqa: ANN001
    db = make_db(make_service(state, "svc") / "app.db")
    gz = snapshot(Target("svc", db, BLOCK), tmp_path, stamp=STAMP, run_admin_fn=local_admin)

    assert backup.main(["restore", str(gz), str(tmp_path / "out.db")]) == 0
    assert "integrity ok" in capsys.readouterr().out


def test_cli_restore_of_a_broken_archive_exits_1(tmp_path: Path, capsys) -> None:  # noqa: ANN001
    bad = tmp_path / "bad.db.gz"
    bad.write_bytes(b"junk")

    assert backup.main(["restore", str(bad), str(tmp_path / "out.db")]) == 1
    assert "ERROR" in capsys.readouterr().err
