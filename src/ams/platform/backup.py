"""Daily SQLite snapshot backups for every ams service that has a database.

This replaces `api/bootstrap/05-db-backup.sh` (which replaced litestream, see the
cost note in that file's header). Same shape -- `.backup` -> gzip -> one
`rclone copyto` per db per day -> a 14-day retention sweep, 04:10 UTC -- with
three differences that follow from ams's model:

1. **Every service with a data dir is covered**, not a hardcoded list of three.
   `api-architecture.md` names the hardcoded list as the platform's sharpest
   data risk: a service that grows a database is silently unprotected.
2. **The snapshot runs inside the admin user namespace** (D4/D9). A service's
   `<root>/data` is owned by its mapped uid block and the harness uid is not
   mapped into the service namespace, so the harness genuinely cannot
   `open()` the file. As inner root in the admin map it can, and the snapshot
   it writes lands on the harness uid because inner 0 *is* the harness uid --
   no chown is needed to hand it over.
3. **R2 credentials come from the SecretStore** (D16) and reach rclone as
   `RCLONE_CONFIG_R2_*` environment variables, never a config file on disk and
   never argv. `RCLONE_CONFIG` is pointed at `/dev/null` so a stray
   `~/.config/rclone/rclone.conf` cannot quietly supply a different remote.

Nothing here logs a credential. `R2Credentials` has no useful `repr`, and every
subprocess failure message is passed through `R2Credentials.redact` before it
reaches a log record, an exception or the JSON-lines report.

The restore path is the point
-----------------------------
"We have backups" is a claim; `restore()` plus `tests/linux/test_platform_backup_live.py`
is the check. The Linux test creates a service-owned database, proves the
harness cannot read it directly, snapshots it through the admin namespace,
deletes the original and restores it -- so the row coming back is evidence,
not a promise.

`restore()` deliberately only writes to a scratch path. Putting a database back
under a running service means stopping it, replacing a file owned by another
uid and restarting -- an operator decision with a blast radius, not something a
backup job should do on its own.

CLI
---
    python -m ams.platform.backup run [--dry-run] [--only ID ...]
    python -m ams.platform.backup restore <archive.db.gz> <dest.db>

`--dry-run` does everything except talk to the network: it snapshots, gzips and
prints the exact rclone argv it would have run, with the credential environment
reported as **names only**.
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

from ams.runtime import RuntimeStore
from ams.secrets import MissingSecret, SecretStore
from ams.state import StateDir
from ams.uidmap import UidBlock
from ams.userns import AdminResult, run_admin

log = logging.getLogger("ams.platform.backup")

EXIT_OK = 0
EXIT_ERROR = 1

#: Pseudo service id the R2 credentials live under in the secret store.
#: PLAN-allin Q6 wrote `_platform`, which `SERVICE_ID_RE` rejects (ids must
#: start with a lowercase letter), so the store would have refused it.
SECRET_SERVICE_ID = "platform-backup"
SECRET_ACCESS_KEY_ID = "R2_ACCESS_KEY_ID"
SECRET_SECRET_ACCESS_KEY = "R2_SECRET_ACCESS_KEY"
SECRET_ENDPOINT = "R2_ENDPOINT"
SECRET_BUCKET = "R2_BUCKET"
SECRET_NAMES = (
    SECRET_ACCESS_KEY_ID,
    SECRET_SECRET_ACCESS_KEY,
    SECRET_ENDPOINT,
    SECRET_BUCKET,
)

#: Environment variable names handed to rclone. Exposed so `--dry-run` can
#: report the names without ever touching the values.
RCLONE_ENV_NAMES = (
    "RCLONE_CONFIG",
    "RCLONE_CONFIG_R2_TYPE",
    "RCLONE_CONFIG_R2_PROVIDER",
    "RCLONE_CONFIG_R2_ACCESS_KEY_ID",
    "RCLONE_CONFIG_R2_SECRET_ACCESS_KEY",
    "RCLONE_CONFIG_R2_ENDPOINT",
)

#: Filenames under `<root>/data` that are backed up. `-wal`/`-shm` sidecars are
#: excluded by construction: they do not end in one of these.
DB_SUFFIXES = (".db", ".sqlite", ".sqlite3")

#: Databases that must never be uploaded, matched on basename. Carried over
#: verbatim from the shell script's loudest comment: registry heartbeats are
#: ephemeral liveness state, re-reported within 30s of any restore, and a
#: restored copy would resurrect stale "healthy" rows.
EXCLUDED_DB_NAMES = frozenset({"registry-runtime.db"})

#: `<root>/data` -- must stay in step with `ams.userns.DATA_DIRNAME`.
DATA_DIRNAME = "data"

#: `<root>/pool.json` -- written by the translator for a pool service
#: (PLAN-pool §3.3). Only the member ids matter here.
POOL_MANIFEST_FILENAME = "pool.json"

DEFAULT_RETENTION_DAYS = 14
DEFAULT_REMOTE = "r2"
DEFAULT_PREFIX = "daily"
GZIP_LEVEL = 9

#: A snapshot of a multi-hundred-MB database on one vCPU is slow; a hung
#: `sqlite3` connection must still not wedge the timer unit forever.
SNAPSHOT_TIMEOUT_S = 600.0
RCLONE_TIMEOUT_S = 900.0
DISCOVER_TIMEOUT_S = 60.0


class BackupError(RuntimeError):
    """A backup step failed. Message is credential-scrubbed by the caller."""


AdminRunner = Callable[..., AdminResult]


# --------------------------------------------------------------------- config


@dataclass(frozen=True)
class R2Credentials:
    """Values from the secret store. Never logged, never printed, never argv."""

    access_key_id: str
    secret_access_key: str
    endpoint: str
    bucket: str

    def __repr__(self) -> str:  # pragma: no cover - trivial, but load-bearing
        return "R2Credentials(<values withheld>)"

    __str__ = __repr__

    @classmethod
    def load(cls, store: SecretStore) -> R2Credentials:
        """Read the four values, or raise :class:`MissingSecret` naming one."""
        values = store.load(SECRET_SERVICE_ID, SECRET_NAMES)
        return cls(
            access_key_id=values[SECRET_ACCESS_KEY_ID],
            secret_access_key=values[SECRET_SECRET_ACCESS_KEY],
            endpoint=values[SECRET_ENDPOINT],
            bucket=values[SECRET_BUCKET],
        )

    def env(self) -> dict[str, str]:
        """The rclone environment. `RCLONE_CONFIG=/dev/null` is deliberate: with
        no config file rclone can only see the remote these variables define,
        so a leftover `~/.config/rclone/rclone.conf` cannot redirect an upload.
        """
        return {
            "RCLONE_CONFIG": os.devnull,
            "RCLONE_CONFIG_R2_TYPE": "s3",
            "RCLONE_CONFIG_R2_PROVIDER": "Cloudflare",
            "RCLONE_CONFIG_R2_ACCESS_KEY_ID": self.access_key_id,
            "RCLONE_CONFIG_R2_SECRET_ACCESS_KEY": self.secret_access_key,
            "RCLONE_CONFIG_R2_ENDPOINT": self.endpoint,
        }

    def redact(self, text: str) -> str:
        """Replace any credential value that appears in ``text``.

        rclone is not supposed to echo its keys, but "not supposed to" is not a
        guarantee and this text ends up in a log record and a JSON report. The
        endpoint goes too: it carries the R2 account id and a report never needs
        it.

        The **bucket is not scrubbed**, deliberately. It is a locator, not an
        authenticator, and it is half of every object key this module prints --
        a backup report that will not say where the archive went is a report an
        operator cannot restore from. It lives in the secret store only because
        that is the one place the four settings can travel together.
        """
        for value in (self.access_key_id, self.secret_access_key, self.endpoint):
            if value:
                text = text.replace(value, "<redacted>")
        return text


@dataclass(frozen=True)
class BackupConfig:
    """Where archives go and how long they live."""

    bucket: str
    remote: str = DEFAULT_REMOTE
    prefix: str = DEFAULT_PREFIX
    retention_days: int = DEFAULT_RETENTION_DAYS
    rclone: Path = field(default_factory=lambda: default_rclone_path())

    def remote_dir(self, service_id: str) -> str:
        return f"{self.remote}:{self.bucket}/{self.prefix}/{service_id}"

    def remote_object(self, service_id: str, name: str) -> str:
        return f"{self.remote_dir(service_id)}/{name}"

    def prune_target(self) -> str:
        return f"{self.remote}:{self.bucket}/{self.prefix}/"


def default_rclone_path(store: RuntimeStore | None = None) -> Path:
    """`<store>/bin/rclone` -- the pinned static binary `scripts/install-rclone.sh`
    puts there, beside the pinned Caddy. Never a distro package: the same
    reasoning as Q3's rejection of `apt install caddy`."""
    store = store if store is not None else RuntimeStore.from_env()
    return store.root / "bin" / "rclone"


# ---------------------------------------------------------------- discovery


@dataclass(frozen=True)
class Target:
    """One database to back up, plus the uid block needed to read it.

    ``label`` is the pool member id when this db was found under a pooled
    service's `data/<member>/` (PLAN-pool §5.6); empty for every non-pooled
    service, and for a db under a pool root that is not in any member's data
    dir (it keeps the pool's own id). ``service_id`` always stays the id the
    harness actually supervises -- the one with the allocated uid block -- so
    a pooled member's `Target` still carries the pool's process id there.
    """

    service_id: str
    db_path: Path
    block: UidBlock
    label: str = ""

    @property
    def stem(self) -> str:
        """`app.sqlite3` -> `app`. One suffix only; a dotted db name keeps its dots."""
        return self.db_path.name.rsplit(".", 1)[0]

    @property
    def effective_label(self) -> str:
        """The id used in the archive name and remote key: ``label`` or ``service_id``."""
        return self.label or self.service_id

    def archive_name(self, stamp: str) -> str:
        """`<label-or-id>-<db-stem>-YYYYMMDD.db.gz`.

        The label (or, unpooled, the service id) is in the object name as well
        as the key prefix so a downloaded file is still self-identifying on an
        operator's laptop, and so a pool member's key stays exactly what it was
        before pooling (PLAN-pool §5.6).
        """
        return f"{self.effective_label}-{self.stem}-{stamp}.db.gz"


def discover(
    state: StateDir,
    allocator: Any,
    *,
    only: Sequence[str] | None = None,
    run_admin_fn: AdminRunner = run_admin,
    timeout_s: float = DISCOVER_TIMEOUT_S,
) -> list[Target]:
    """Every `<root>/data/**.db` the harness knows about, as :class:`Target`s.

    The listing goes through the admin namespace because `<root>/data` is mode
    0750 owned by the service's uid: the harness can `stat` it (its parents are
    traversable) but cannot `listdir` it. `find` is the helper -- argv only, no
    shell, so the `(` `)` grouping tokens are literal arguments.

    A service with no data dir costs no fork: the `is_dir()` check answers from
    the harness side. A service with no allocated uid block is skipped with a
    warning rather than raised -- it has never been started, so it has no data.
    """
    wanted = set(only) if only else None
    targets: list[Target] = []
    for service_id in state.list_service_ids():
        if wanted is not None and service_id not in wanted:
            continue
        service_root = state.service_root(service_id)
        data_dir = service_root / DATA_DIRNAME
        if not data_dir.is_dir():
            log.debug("%s: no data dir at %s", service_id, data_dir)
            continue
        block = allocator.get(service_id)
        if block is None:
            log.warning("%s: no uid block allocated; cannot read %s", service_id, data_dir)
            continue
        members = _pool_members(service_root)
        result = run_admin_fn(_find_argv(data_dir), block, timeout_s=timeout_s)
        if not result.ok:
            log.warning(
                "%s: listing %s failed (rc=%d): %s",
                service_id,
                data_dir,
                result.returncode,
                result.stderr.decode(errors="replace").strip() or "(no stderr)",
            )
            continue
        for path in _parse_find_output(result.stdout, data_dir, service_id):
            label = _label_for(path, data_dir, members)
            targets.append(Target(service_id=service_id, db_path=path, block=block, label=label))
    targets.sort(key=lambda t: (t.service_id, str(t.db_path)))
    if wanted:
        found = {t.service_id for t in targets}
        for service_id in sorted(wanted - found):
            log.warning("%s: no database found (requested with --only)", service_id)
    return targets


def _pool_members(service_root: Path) -> set[str] | None:
    """The member ids named in `<service_root>/pool.json`, or None if absent,
    unreadable or malformed (PLAN-pool §5.6 -- non-pooled services must be
    byte-identical to today, so anything but a well-formed manifest is treated
    as "not a pool" rather than raised).
    """
    try:
        data = json.loads((service_root / POOL_MANIFEST_FILENAME).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    members = data.get("members")
    if not isinstance(members, list):
        return None
    ids = {
        member.get("id")
        for member in members
        if isinstance(member, dict) and isinstance(member.get("id"), str)
    }
    return ids or None


def _label_for(path: Path, data_dir: Path, members: set[str] | None) -> str:
    """The pool member id owning ``path``, or "" (= keep the pool's own id).

    A db two levels under `data_dir` (`data/<member>/x.db`) whose first
    component names a known member gets that member as its label. Anything
    else -- a bare `data/x.db`, or a subdir that is not in the member list --
    keeps the pool id, exactly like an unpooled service.
    """
    if not members:
        return ""
    try:
        rel_parts = path.relative_to(data_dir).parts
    except ValueError:
        return ""
    if len(rel_parts) < 2:
        return ""
    member = rel_parts[0]
    return member if member in members else ""


def _find_argv(data_dir: Path) -> list[str]:
    argv = ["find", str(data_dir), "-maxdepth", "2", "-type", "f", "("]
    for i, suffix in enumerate(DB_SUFFIXES):
        if i:
            argv.append("-o")
        argv += ["-name", f"*{suffix}"]
    argv += [")", "-print"]
    return argv


def _parse_find_output(stdout: bytes, data_dir: Path, service_id: str) -> list[Path]:
    """Lines of `find -print` -> paths, with everything unexpected dropped.

    The output describes files created by a service, so it is untrusted input:
    a path is kept only if it is inside the data dir we asked about and ends in
    a suffix we asked for. A filename containing a newline would split into two
    lines here and both halves would fail those checks, which is the right
    outcome -- skip it and say so.
    """
    out: list[Path] = []
    prefix = str(data_dir).rstrip("/") + "/"
    for raw in stdout.decode("utf-8", "replace").splitlines():
        line = raw.strip()
        if not line:
            continue
        if not line.startswith(prefix) or not line.endswith(DB_SUFFIXES):
            log.warning("%s: ignoring unexpected find output %r", service_id, line)
            continue
        path = Path(line)
        if path.name in EXCLUDED_DB_NAMES:
            log.info("%s: skipping %s (excluded: ephemeral state)", service_id, path.name)
            continue
        out.append(path)
    return out


# ----------------------------------------------------------------- snapshot

# Run as inner root in the admin namespace, with the two paths as argv. A fixed
# code string is not a shell string: nothing here is interpolated, and the
# arguments arrive through `sys.argv` where no quoting rule can reach them.
#
# `Connection.backup` replaces the `sqlite3` CLI's `.backup` because the CLI is
# not installed on the target host (checked 2026-09-02) while the stdlib module
# is the same SQLite library through the same online-backup API.
#
# The chown loop is the subtle part: opening the source read-write can create
# or recreate `-wal`/`-shm`/`-journal` beside it, and as inner root those land
# on the harness uid -- after which the service itself could no longer write
# them and would fail on its next transaction. They are handed straight back to
# whoever owns the database. The alternative, opening read-only, cannot recover
# a hot WAL and would silently back up a stale snapshot.
_SNAPSHOT_CODE = """\
import os
import sqlite3
import sys

src, dst = sys.argv[1], sys.argv[2]
st = os.stat(src)
con = sqlite3.connect(src, timeout=60.0)
try:
    con.execute("PRAGMA busy_timeout=60000")
    out = sqlite3.connect(dst)
    try:
        con.backup(out)
    finally:
        out.close()
finally:
    con.close()
for suffix in ("-wal", "-shm", "-journal"):
    side = src + suffix
    try:
        side_st = os.stat(side)
    except FileNotFoundError:
        continue
    if (side_st.st_uid, side_st.st_gid) != (st.st_uid, st.st_gid):
        os.chown(side, st.st_uid, st.st_gid)
os.chmod(dst, 0o600)
"""


def snapshot(
    target: Target,
    workdir: Path,
    *,
    stamp: str | None = None,
    run_admin_fn: AdminRunner = run_admin,
    python: str | None = None,
    timeout_s: float = SNAPSHOT_TIMEOUT_S,
) -> Path:
    """A consistent, gzipped copy of ``target``'s database in ``workdir``.

    Two steps with a deliberate split. The `.backup` runs in the admin
    namespace because only inner root can read a service-owned file; the gzip
    runs in the harness because the snapshot already belongs to the harness
    (inner 0 maps to the harness uid, so the file it wrote is harness-owned on
    the host) and compressing it needs no privilege at all.

    `workdir` must be a harness-owned directory: inner root writes the snapshot
    there, and the harness reads it back.

    Returns the `.db.gz` path. The uncompressed intermediate is removed.
    """
    stamp = stamp or utc_stamp()
    interpreter = python or sys.executable
    workdir = Path(workdir)
    gz = workdir / target.archive_name(stamp)
    raw = gz.with_suffix("")  # strip the trailing ".gz"
    argv = [interpreter, "-I", "-c", _SNAPSHOT_CODE, str(target.db_path), str(raw)]
    try:
        result = run_admin_fn(argv, target.block, timeout_s=timeout_s)
        if not result.ok:
            raise BackupError(
                f"{target.service_id}: sqlite backup of {target.db_path} exited "
                f"{result.returncode}: "
                f"{result.stderr.decode(errors='replace').strip() or '(no stderr)'}"
            )
        if not raw.is_file():
            raise BackupError(
                f"{target.service_id}: sqlite backup reported success but wrote no {raw}"
            )
        _gzip_file(raw, gz)
    finally:
        # Always: on failure a partial snapshot is worse than none, and it is
        # an unencrypted copy of a service's data sitting in a temp dir.
        raw.unlink(missing_ok=True)
    log.info(
        "snapshot %s -> %s (%d bytes gzipped)",
        target.db_path,
        gz.name,
        gz.stat().st_size,
    )
    return gz


def _gzip_file(src: Path, dst: Path) -> None:
    """`gzip -9`, done with the stdlib rather than a subprocess.

    Same format, one fewer external binary to depend on, and it runs on the
    macOS dev machine so the naming and round-trip are testable off the target
    host. `mtime=0` keeps the output byte-stable for a given input.
    """
    # `filename=""` keeps the archive's embedded name empty; with mtime=0 the
    # output depends only on the bytes going in.
    with open(dst, "wb") as fh_raw:
        with gzip.GzipFile(
            filename="", mode="wb", compresslevel=GZIP_LEVEL, fileobj=fh_raw, mtime=0
        ) as fh_out:
            with open(src, "rb") as fh_in:
                shutil.copyfileobj(fh_in, fh_out, length=1 << 20)
    os.chmod(dst, 0o600)


def utc_stamp(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y%m%d")


# ------------------------------------------------------------------- rclone


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


Runner = Callable[[Sequence[str], Mapping[str, str], float], CommandResult]


def _run(argv: Sequence[str], env: Mapping[str, str], timeout_s: float) -> CommandResult:
    """`subprocess.run` with an explicit environment and no shell.

    The environment carries credentials, so it is never logged; the argv is,
    because it contains only paths and a bucket-qualified object key.
    """
    log.debug("running %s", " ".join(argv))
    try:
        proc = subprocess.run(  # noqa: S603 - argv list, shell=False, fixed binary
            list(argv),
            env=dict(env),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        return CommandResult(tuple(argv), -1, "", f"timed out after {e.timeout}s")
    except OSError as e:
        return CommandResult(tuple(argv), -1, "", f"{type(e).__name__}: {e}")
    return CommandResult(tuple(argv), proc.returncode, proc.stdout, proc.stderr)


def upload_argv(local: Path, remote_object: str, rclone: Path) -> list[str]:
    """The exact `rclone copyto` argv. Pure, so `--dry-run` and the test assert
    on the same value the real run uses.

    `--s3-no-check-bucket` is carried over from the shell script: it drops a
    HeadBucket per upload, which is a Class-A operation on R2 and the whole
    reason that script exists.
    """
    return [str(rclone), "copyto", "--s3-no-check-bucket", str(local), remote_object]


def prune_argv(cfg: BackupConfig) -> list[str]:
    return [
        str(cfg.rclone),
        "delete",
        "--min-age",
        f"{cfg.retention_days}d",
        cfg.prune_target(),
    ]


def _base_env() -> dict[str, str]:
    """Non-secret environment rclone needs. Nothing is inherited: an inherited
    `RCLONE_*` variable from the unit or a shell would silently reconfigure the
    remote this job is careful to define explicitly."""
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": os.environ.get("HOME", "/home/harness"),
        "LC_ALL": "C",
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),  # noqa: S108 - rclone scratch, not our data
    }


def upload(
    local: Path,
    service_id: str,
    cfg: BackupConfig,
    creds: R2Credentials,
    *,
    runner: Runner = _run,
    timeout_s: float = RCLONE_TIMEOUT_S,
) -> str:
    """Copy one archive to `r2:<bucket>/<prefix>/<id>/<name>`; returns the key."""
    remote_object = cfg.remote_object(service_id, local.name)
    argv = upload_argv(local, remote_object, cfg.rclone)
    result = runner(argv, {**_base_env(), **creds.env()}, timeout_s)
    if not result.ok:
        raise BackupError(
            f"{service_id}: rclone copyto exited {result.returncode}: "
            f"{creds.redact(result.stderr.strip() or result.stdout.strip() or '(no output)')}"
        )
    log.info("uploaded %s", remote_object)
    return remote_object


def prune(
    cfg: BackupConfig,
    creds: R2Credentials,
    *,
    runner: Runner = _run,
    timeout_s: float = RCLONE_TIMEOUT_S,
) -> str:
    """Delete archives older than the retention window; returns the target path.

    Unlike the shell script this replaces, a failed prune is a *failure*, not a
    warning nobody reads. Retention that quietly stops working is unbounded
    object growth and an unbounded bill, and it is invisible until it is
    expensive -- exactly the failure mode the litestream incident was.
    """
    argv = prune_argv(cfg)
    result = runner(argv, {**_base_env(), **creds.env()}, timeout_s)
    if not result.ok:
        raise BackupError(
            f"retention prune exited {result.returncode}: "
            f"{creds.redact(result.stderr.strip() or result.stdout.strip() or '(no output)')}"
        )
    log.info("pruned archives older than %dd under %s", cfg.retention_days, cfg.prune_target())
    return cfg.prune_target()


# ------------------------------------------------------------------ restore


@dataclass(frozen=True)
class RestoreResult:
    path: Path
    integrity: str
    page_count: int

    @property
    def ok(self) -> bool:
        return self.integrity == "ok"


def restore(gz_path: Path, dest_db: Path) -> RestoreResult:
    """Decompress an archive to ``dest_db`` and verify it opens clean.

    ``dest_db`` is a scratch path owned by the caller. Putting a database back
    under a live service is an operator decision (stop, replace a file owned by
    another uid, restart) and is not automated here.

    Raises :class:`BackupError` if `PRAGMA integrity_check` is anything but
    ``ok`` -- an archive that restores to a corrupt database is the failure this
    whole module exists to detect, so it must not return quietly.
    """
    gz_path = Path(gz_path)
    dest_db = Path(dest_db)
    dest_db.parent.mkdir(parents=True, exist_ok=True)
    try:
        with gzip.open(gz_path, "rb") as fh_in, open(dest_db, "wb") as fh_out:
            shutil.copyfileobj(fh_in, fh_out, length=1 << 20)
    except (OSError, EOFError, gzip.BadGzipFile) as e:
        raise BackupError(f"cannot decompress {gz_path}: {type(e).__name__}: {e}") from e

    con = sqlite3.connect(dest_db)
    try:
        integrity = str(con.execute("PRAGMA integrity_check").fetchone()[0])
        page_count = int(con.execute("PRAGMA page_count").fetchone()[0])
    except sqlite3.DatabaseError as e:
        raise BackupError(f"restored {dest_db} is not a usable database: {e}") from e
    finally:
        con.close()
    result = RestoreResult(path=dest_db, integrity=integrity, page_count=page_count)
    if not result.ok:
        raise BackupError(f"restored {dest_db} failed integrity_check: {integrity}")
    log.info("restored %s -> %s (%d pages, integrity ok)", gz_path.name, dest_db, page_count)
    return result


# --------------------------------------------------------------------- run


@dataclass(frozen=True)
class TargetResult:
    service_id: str
    db: str
    status: str  # "ok" | "failed"
    archive: str | None = None
    remote: str | None = None
    size_bytes: int | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass(frozen=True)
class Report:
    started_at: str
    stamp: str
    dry_run: bool
    results: tuple[TargetResult, ...] = ()
    prune_error: str | None = None

    @property
    def ok(self) -> bool:
        return self.prune_error is None and all(r.ok for r in self.results)

    @property
    def exit_code(self) -> int:
        return EXIT_OK if self.ok else EXIT_ERROR


def run(
    state: StateDir,
    allocator: Any,
    store: SecretStore,
    *,
    dry_run: bool = False,
    only: list[str] | None = None,
    cfg: BackupConfig | None = None,
    stamp: str | None = None,
    stream: IO[str] | None = None,
    run_admin_fn: AdminRunner = run_admin,
    runner: Runner = _run,
    python: str | None = None,
) -> Report:
    """Snapshot, upload and prune. One failing target never stops the others.

    Each target is independent: a service whose database is locked, corrupt or
    unreadable produces one record and the run continues, because the argument
    for backing up *every* data dir collapses the moment one bad service can
    take the whole sweep down with it.

    Every record goes to ``stream`` (stdout by default) as one JSON object per
    line, in the shape `ams.decision.JsonLinesEscalation` uses, so the agent
    loop reads backup outcomes through the same channel as everything else.
    """
    out = stream if stream is not None else sys.stdout
    stamp = stamp or utc_stamp()
    started_at = datetime.now(UTC).isoformat(timespec="seconds")

    creds, cfg = _resolve_credentials(store, cfg, dry_run=dry_run)
    targets = discover(state, allocator, only=only, run_admin_fn=run_admin_fn)
    if not targets:
        log.warning("no service database found; nothing to back up")

    results: list[TargetResult] = []
    workdir_root = state.root / "platform" / "backup"
    workdir_root.mkdir(parents=True, exist_ok=True)
    os.chmod(workdir_root, 0o700)  # snapshots are plaintext copies of service data
    workdir = Path(tempfile.mkdtemp(prefix=f"run-{stamp}-", dir=workdir_root))
    try:
        for target in targets:
            result = _run_one(
                target,
                workdir,
                cfg,
                creds,
                stamp=stamp,
                dry_run=dry_run,
                run_admin_fn=run_admin_fn,
                runner=runner,
                python=python,
            )
            results.append(result)
            _emit(out, _record_for(result, cfg, dry_run=dry_run))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    prune_error: str | None = None
    if dry_run:
        _emit(
            out,
            _record(
                "BackupPrunePlanned",
                None,
                "log",
                "dry run: retention sweep not executed",
                {"argv": prune_argv(cfg), "env_names": list(RCLONE_ENV_NAMES)},
            ),
        )
    elif results:
        try:
            prune(cfg, creds, runner=runner)
        except BackupError as e:
            prune_error = str(e)
            _emit(
                out,
                _record(
                    "BackupPruneFailed",
                    None,
                    "escalate",
                    prune_error,
                    {"argv": prune_argv(cfg), "retention_days": cfg.retention_days},
                ),
            )

    report = Report(
        started_at=started_at,
        stamp=stamp,
        dry_run=dry_run,
        results=tuple(results),
        prune_error=prune_error,
    )
    _emit(
        out,
        _record(
            "BackupRunFinished",
            None,
            "log" if report.ok else "escalate",
            "all targets backed up" if report.ok else "one or more targets failed",
            {
                "started_at": started_at,
                "stamp": stamp,
                "dry_run": dry_run,
                "targets": len(results),
                "failed": sum(1 for r in results if not r.ok),
                "prune_error": prune_error,
            },
        ),
    )
    return report


def _resolve_credentials(
    store: SecretStore, cfg: BackupConfig | None, *, dry_run: bool
) -> tuple[R2Credentials, BackupConfig]:
    """Credentials plus the config that depends on them (the bucket).

    A dry run must work on a host where the store has never been filled in --
    that is the first thing anyone tries -- so a missing credential becomes a
    visible placeholder in the printed argv instead of an abort. A real run
    fails loudly: uploading nowhere is worse than not running.
    """
    try:
        creds = R2Credentials.load(store)
    except MissingSecret:
        if not dry_run:
            raise
        missing = store.missing(SECRET_SERVICE_ID, SECRET_NAMES)
        log.warning(
            "dry run with unset credentials (%s); the printed argv uses placeholders",
            ", ".join(missing),
        )
        creds = R2Credentials("", "", "", "")
    bucket = creds.bucket or f"<{SECRET_BUCKET}>"
    if cfg is None:
        return creds, BackupConfig(bucket=bucket)
    return creds, cfg


def _run_one(
    target: Target,
    workdir: Path,
    cfg: BackupConfig,
    creds: R2Credentials,
    *,
    stamp: str,
    dry_run: bool,
    run_admin_fn: AdminRunner,
    runner: Runner,
    python: str | None,
) -> TargetResult:
    gz: Path | None = None
    try:
        gz = snapshot(target, workdir, stamp=stamp, run_admin_fn=run_admin_fn, python=python)
        size = gz.stat().st_size
        remote_object = cfg.remote_object(target.effective_label, gz.name)
        if not dry_run:
            upload(gz, target.effective_label, cfg, creds, runner=runner)
        return TargetResult(
            service_id=target.service_id,
            db=str(target.db_path),
            status="ok",
            archive=gz.name,
            remote=remote_object,
            size_bytes=size,
        )
    except (BackupError, OSError) as e:
        return TargetResult(
            service_id=target.service_id,
            db=str(target.db_path),
            status="failed",
            error=creds.redact(f"{type(e).__name__}: {e}"),
        )
    finally:
        if gz is not None:
            gz.unlink(missing_ok=True)


def _record_for(result: TargetResult, cfg: BackupConfig, *, dry_run: bool) -> dict[str, Any]:
    """One JSON record for one target.

    A dry run carries the exact argv the real run would have executed, plus
    the credential environment reported by NAME only -- the whole point of
    the mode is to make the command auditable without exposing what it
    authenticates with.
    """
    event = asdict(result)
    if result.ok and dry_run and result.remote is not None:
        event["argv"] = upload_argv(Path(result.archive or ""), result.remote, cfg.rclone)
        event["env_names"] = list(RCLONE_ENV_NAMES)
    return _record(
        "BackupSucceeded" if result.ok else "BackupFailed",
        result.service_id,
        "log" if result.ok else "escalate",
        ("dry run: snapshot taken, upload skipped" if dry_run else "backup uploaded")
        if result.ok
        else (result.error or "backup failed"),
        event,
    )


def _record(
    kind: str, service_id: str | None, action: str, reason: str, event: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "kind": kind,
        "service_id": service_id,
        "action": action,
        "reason": reason,
        "event": dict(event),
    }


def _emit(stream: IO[str], record: Mapping[str, Any]) -> None:
    stream.write(json.dumps(record, default=str) + "\n")
    stream.flush()


# --------------------------------------------------------------------- cli


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ams.platform.backup",
        description=(
            "Daily SQLite snapshot backups of every ams service data dir to R2, "
            "and the restore path that proves they work."
        ),
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--log-level", default="INFO")
    ops = parser.add_subparsers(dest="command", required=True)

    p_run = ops.add_parser("run", parents=[common], help="snapshot, upload and prune")
    p_run.add_argument("--state-dir", type=Path, default=None, help="overrides $AMS_STATE_DIR")
    p_run.add_argument(
        "--dry-run",
        action="store_true",
        help="snapshot and print the rclone argv (env reported by name only); no network",
    )
    p_run.add_argument(
        "--only",
        action="append",
        metavar="ID",
        help="back up only this service id (repeatable)",
    )

    p_restore = ops.add_parser(
        "restore", parents=[common], help="decompress an archive and verify it opens clean"
    )
    p_restore.add_argument("archive", type=Path, help="a <id>-<db>-YYYYMMDD.db.gz file")
    p_restore.add_argument("dest", type=Path, help="scratch path to write the database to")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    level = getattr(logging, str(args.log_level).upper(), logging.INFO)
    # stderr: stdout is the JSON-lines channel the agent loop parses.
    logging.basicConfig(
        level=level, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr
    )
    logging.getLogger("ams").setLevel(level)
    handlers: dict[str, Callable[[argparse.Namespace], int]] = {
        "run": _cmd_run,
        "restore": _cmd_restore,
    }
    try:
        return handlers[args.command](args)
    except (BackupError, MissingSecret) as e:
        print(f"ERROR {e}", file=sys.stderr)
        return EXIT_ERROR
    except OSError as e:
        print(f"ERROR {type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_ERROR


def _cmd_run(args: argparse.Namespace) -> int:
    from ams.cli import _state_dir, uid_allocator
    from ams.secrets import store_for

    state = _state_dir(args.state_dir)
    report = run(
        state,
        uid_allocator(state),
        store_for(state),
        dry_run=bool(args.dry_run),
        only=list(args.only) if args.only else None,
    )
    return report.exit_code


def _cmd_restore(args: argparse.Namespace) -> int:
    result = restore(args.archive, args.dest)
    print(f"OK {result.path} ({result.page_count} pages, integrity {result.integrity})")
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
