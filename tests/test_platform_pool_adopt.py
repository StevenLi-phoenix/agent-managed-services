"""T8: ``ams platform pool {plan,adopt}``, the rollback refusal, the status rows.

Adoption is the one operation in the platform that moves a *database*, so every
test here is written against the real filesystem in ``tmp_path``: the only thing
replaced is the user-namespace hop itself (``run_admin_fn``), and the fake that
replaces it actually performs the four argv shapes adopt is allowed to use
(``mkdir``/``chown``/``find``/``mv``) as the test user. A test that asserted
"adopt called mv with these arguments" would pass just as happily against an
implementation that moved the file to the wrong place; asserting the file is
*there afterwards* cannot.

The control socket is injected rather than patched (``adopt(..., control=...)``)
because "is the pool running" is a question with three answers -- yes, no, and
"there is no supervisor" -- and a fake object makes all three trivial to state.

Fixtures for the rollback half come from ``test_platform_sync`` (same directory,
no package ``__init__.py``, the pattern ``test_platform_sync_pool.py`` already
uses): a throwaway git repo and one threaded loopback server that answers
``GET /health``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from test_platform_sync import (  # noqa: F401 - `registry` is a fixture, used by name
    FakeRegistry,
    _commit,
    _git,
    registry,
    service_manifest,
)

from ams.platform import pool as pool_mod
from ams.platform import rollback as rollback_mod
from ams.platform.cli import format_status
from ams.platform.pool import PoolAdoptError, adopt, plan
from ams.platform.rollback import RollbackError, rollback
from ams.platform.sources import SHA_MARKER, SourceMirror
from ams.platform.sync import (
    POOL_JSON_NAME,
    POOL_RUNNER_NAME,
    STATE_VERSION,
    SyncConfig,
    mounts_dir,
    registry_dir,
    state_path,
)
from ams.platform.translate import mangle_member
from ams.runtime import RuntimeStore, python_venv_dir
from ams.secrets import store_for
from ams.spawn import DATA_DIRNAME, INNER_GID, INNER_UID
from ams.state import StateDir
from ams.uidmap import UidBlock
from ams.userns import AdminResult

POOL = "core"
POOL_ID = "pool-core"
MEMBERS = ("alpha", "beta")
STANDALONE = "files"

POOL_BLOCK = UidBlock(200_000, 200_000, 1024)
MEMBER_BLOCKS = {
    "alpha": UidBlock(100_000, 100_000, 1024),
    "beta": UidBlock(101_024, 101_024, 1024),
}

#: The values that must never appear in a log line, a plan line or a report.
SECRET_VALUES = {
    ("alpha", "SVC_SECRET"): b"alpha-svc-secret-never-printed",
    ("alpha", "DEEPSEEK_API_KEY"): b"alpha-third-party-key-never-printed",
    ("beta", "SVC_SECRET"): b"beta-svc-secret-never-printed",
}


# --------------------------------------------------------------------------- fakes


class FakeAdmin:
    """``run_admin`` without the namespace: really runs the four allowed argvs.

    Anything else is an assertion failure rather than a silent no-op, which is
    what keeps "adopt only ever shells out to these four" a tested property
    instead of a docstring claim.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.blocks: list[UidBlock] = []
        self.chowns: list[tuple[str, str]] = []
        #: Paths that exist but that even the admin namespace cannot enumerate.
        self.unlistable: set[str] = set()
        #: Substrings; any call whose argv contains one exits non-zero, which is
        #: how a hop is interrupted halfway through a test.
        self.fail_on: set[str] = set()

    def __call__(self, argv: Any, block: UidBlock, **_kw: Any) -> AdminResult:
        args = tuple(str(a) for a in argv)
        self.calls.append(args)
        self.blocks.append(block)
        if any(needle in " ".join(args) for needle in self.fail_on):
            return AdminResult(argv=args, returncode=1, stdout=b"", stderr=b"injected failure")
        handler = {
            "mkdir": self._mkdir,
            "chown": self._chown,
            "find": self._find,
            "mv": self._mv,
        }.get(args[0])
        if handler is None:  # pragma: no cover - the point of the assertion
            raise AssertionError(f"adopt used an unexpected admin command: {args}")
        return handler(args)

    def of(self, verb: str) -> list[tuple[str, ...]]:
        return [c for c in self.calls if c[0] == verb]

    # -- the four verbs

    def _mkdir(self, args: tuple[str, ...]) -> AdminResult:
        mode, rest = 0o750, []
        it = iter(args[1:])
        for arg in it:
            if arg == "-m":
                mode = int(next(it), 8)
            elif arg == "-p":
                continue
            else:
                rest.append(arg)
        for target in rest:
            # os.*, never Path.*: the fake stands in for the admin namespace,
            # which `deny()` does not (and on the box could not) block.
            os.makedirs(target, exist_ok=True)
            os.chmod(target, mode)
        return self._ok(args)

    def _chown(self, args: tuple[str, ...]) -> AdminResult:
        rest = [a for a in args[1:] if a != "-R"]
        owner, paths = rest[0], rest[1:]
        for path in paths:
            if not os.path.exists(path):  # pragma: no cover - defensive
                raise AssertionError(f"chown on a path that does not exist: {path}")
            self.chowns.append((owner, path))
        return self._ok(args)

    def _find(self, args: tuple[str, ...]) -> AdminResult:
        """GNU ``find``'s three outcomes, including the two that look alike.

        A missing start path exits 1 having printed nothing; an unreadable one
        exits 1 having printed *itself*; a readable one exits 0. Conflating the
        first two is the bug this fake exists to expose (an absent
        ``data/<member>`` is empty, not unknown).
        """
        directory = Path(args[1])
        mindepth = int(args[args.index("-mindepth") + 1]) if "-mindepth" in args else 0
        # Deliberately os.path, not Path.is_dir: the fake is the *admin* namespace
        # and must answer even where the harness itself is denied by `deny()`.
        if not os.path.isdir(directory):
            return AdminResult(argv=args, returncode=1, stdout=b"", stderr=b"no such directory")
        head = [] if mindepth > 0 else [str(directory)]
        if str(directory) in self.unlistable:
            return AdminResult(
                argv=args,
                returncode=1,
                stdout=b"".join(e.encode() + b"\0" for e in head),
                stderr=b"Permission denied",
            )
        entries = head + sorted(str(directory / n) for n in os.listdir(directory))
        return AdminResult(
            argv=args,
            returncode=0,
            stdout=b"".join(e.encode() + b"\0" for e in entries),
            stderr=b"",
        )

    def _mv(self, args: tuple[str, ...]) -> AdminResult:
        no_clobber = "-n" in args
        rest = [a for a in args[1:] if a != "-n"]
        if "-T" in rest:
            # `mv -T src dst`: one rename(2). Onto an absent or empty directory
            # it succeeds; onto a non-empty one the kernel says ENOTEMPTY.
            src, dst = rest[rest.index("-T") + 1], rest[rest.index("-T") + 2]
            if os.path.isdir(dst) and os.listdir(dst):
                return AdminResult(
                    argv=args, returncode=1, stdout=b"", stderr=b"Directory not empty"
                )
            if os.path.isdir(dst):
                os.rmdir(dst)
            os.rename(src, dst)
            return self._ok(args)
        dest = Path(rest[rest.index("-t") + 1])
        sources = rest[rest.index("--") + 1 :]
        for src in sources:
            target = dest / os.path.basename(src)
            if os.path.exists(target) and no_clobber:  # pragma: no cover - refused earlier
                continue
            os.replace(src, target)
        return self._ok(args)

    @staticmethod
    def _ok(args: tuple[str, ...]) -> AdminResult:
        return AdminResult(argv=args, returncode=0, stdout=b"", stderr=b"")


class FakeUids:
    def __init__(self) -> None:
        self.seen: list[str] = []

    def allocate(self, service_id: str) -> UidBlock:
        self.seen.append(service_id)
        return MEMBER_BLOCKS.get(service_id, POOL_BLOCK)


class FakeControl:
    """The three answers the control socket can give, without a socket."""

    def __init__(self, rows: dict[str, dict[str, Any]] | None = None, *, up: bool = True) -> None:
        self.rows = rows if rows is not None else {}
        self.up = up
        self.stopped: list[str] = []
        #: What stopping the member does to its data directory. A WAL-mode
        #: SQLite service checkpoints and deletes `-wal`/`-shm` on a clean
        #: shutdown, so the disk after the stop is not the disk the plan saw.
        self.on_stop: Any = None

    def status(self) -> dict[str, Any]:
        if not self.up:
            from ams.control import ControlError

            raise ControlError("no harness listening on /nowhere/control.sock")
        return {"ok": True, "services": dict(self.rows)}

    def stop(self, service_id: str) -> dict[str, Any]:
        if not self.up:  # pragma: no cover - adopt never stops without a status
            from ams.control import ControlError

            raise ControlError("no harness listening on /nowhere/control.sock")
        self.stopped.append(service_id)
        self.rows[service_id] = {"status": "stopped", "pid": None}
        if self.on_stop is not None:
            self.on_stop(service_id)
        return {"ok": True}


# --------------------------------------------------------------------------- the fleet


class Fleet:
    state: StateDir
    store: RuntimeStore
    admin: FakeAdmin
    uids: FakeUids
    control: FakeControl


def _record(sha: str, **kw: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "sha": sha,
        "prev_sha": None,
        "deployed_sha": sha,
        "stage": "healthy",
        "error": None,
        "manual_restart": False,
        "escalated": False,
        "updated_at": "2026-09-03T00:00:00Z",
        "stage_since": "2026-09-03T00:00:00Z",
    }
    row.update(kw)
    return row


def write_platform_state(state: StateDir, sha: str, **overrides: Any) -> None:
    services: dict[str, Any] = {
        POOL_ID: _record(sha, stage="failed", pool=POOL, pool_members=list(MEMBERS)),
        STANDALONE: _record(sha),
    }
    for member in MEMBERS:
        services[member] = _record(sha, stage="failed", pool=POOL)
    services.update(overrides)
    path = state_path(state)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": STATE_VERSION, "services": services}, indent=2, sort_keys=True),
        encoding="utf-8",
    )


@pytest.fixture
def fleet(tmp_path: Path) -> Fleet:
    """A fleet mid-migration: the pool is grouped, the members still own the data."""
    state = StateDir(tmp_path / "state")
    state.ensure()
    store = RuntimeStore(tmp_path / "store")
    store.ensure()
    secrets = store_for(state)

    for member in MEMBERS:
        # The legacy root, exactly as an unpooled deploy leaves it.
        root = state.service_root(member)
        (root / DATA_DIRNAME).mkdir(parents=True, exist_ok=True)
        (root / DATA_DIRNAME / f"{member}.db").write_bytes(b"SQLite format 3\x00" + member.encode())
        (root / "repo").mkdir(parents=True, exist_ok=True)
        (root / "repo" / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        state.service_decl_path(member).write_text(f'id = "{member}"\n', encoding="utf-8")
    (state.service_root(STANDALONE) / DATA_DIRNAME).mkdir(parents=True, exist_ok=True)
    state.service_decl_path(STANDALONE).write_text(f'id = "{STANDALONE}"\n', encoding="utf-8")

    for (service_id, name), value in SECRET_VALUES.items():
        secrets.set(service_id, name, value)

    write_platform_state(state, "a" * 40)

    f = Fleet()
    f.state, f.store = state, store
    f.admin, f.uids, f.control = FakeAdmin(), FakeUids(), FakeControl()
    return f


def run_adopt(fleet: Fleet, pool_id: str = POOL, **kw: Any) -> Any:
    return adopt(
        fleet.state,
        pool_id,
        uids=kw.pop("uids", fleet.uids),
        run_admin_fn=kw.pop("run_admin_fn", fleet.admin),
        control=kw.pop("control", fleet.control),
        **kw,
    )


def pool_data(fleet: Fleet, member: str) -> Path:
    return fleet.state.service_root(POOL_ID) / DATA_DIRNAME / member


def legacy_data(fleet: Fleet, member: str) -> Path:
    return fleet.state.service_root(member) / DATA_DIRNAME


# --------------------------------------------------------------------------- plan


def test_plan_lists_every_member_and_what_would_move(fleet: Fleet) -> None:
    result = plan(fleet.state, POOL)

    assert result.pool_id == POOL_ID
    assert [m.id for m in result.members] == list(MEMBERS)
    alpha = result.members[0]
    assert alpha.data_entries == ("alpha.db",)
    assert alpha.decl_exists is True
    assert ("SVC_SECRET", f"SVC_SECRET__{mangle_member('alpha')}") in alpha.secrets
    assert result.is_noop is False


def test_plan_accepts_the_prefixed_and_the_bare_pool_name(fleet: Fleet) -> None:
    assert plan(fleet.state, POOL).pool_id == plan(fleet.state, POOL_ID).pool_id == POOL_ID


def test_plan_writes_nothing_and_moves_nothing(fleet: Fleet) -> None:
    before = _tree(fleet.state.root)

    plan(fleet.state, POOL)

    assert _tree(fleet.state.root) == before


def test_plan_of_an_unknown_pool_names_the_command_that_creates_one(fleet: Fleet) -> None:
    with pytest.raises(PoolAdoptError) as excinfo:
        plan(fleet.state, "nosuch")
    assert "nosuch" in str(excinfo.value)
    assert "ams platform sync" in str(excinfo.value)


def _tree(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }


# ------------------------------------------------------- unreadable directories
#
# The live cutover on racknerd died here (`.claude/state/diagnosis-pool-cutover.md`):
# a freshly staged pool root has `data/` at 0750 owned by the *pool's* uid, so
# the harness cannot stat inside it, and `pathlib` re-raises EACCES out of
# `Path.is_dir()` -- it swallows only ENOENT/ENOTDIR/EBADF/ELOOP. Every probe
# below therefore has to fall through to the admin namespace instead of raising.


def deny(monkeypatch: pytest.MonkeyPatch, *denied: Path) -> None:
    """Make every harness-side probe at or under ``denied`` raise EACCES.

    This is what a 0750 directory owned by another uid looks like from the
    harness: not "absent", not "empty" -- an exception out of the probe itself.
    The admin-namespace fake is deliberately unaffected, because on the box the
    admin namespace *can* read these paths.
    """
    roots = tuple(Path(d) for d in denied)
    reals = {name: getattr(Path, name) for name in ("is_dir", "iterdir", "exists", "stat")}

    def blocked(path: Path) -> bool:
        return any(path == root or root in path.parents for root in roots)

    def guard(name: str) -> Any:
        real = reals[name]

        def wrapper(self: Path, *args: Any, **kwargs: Any) -> Any:
            if blocked(self):
                raise PermissionError(13, "Permission denied", str(self))
            return real(self, *args, **kwargs)

        return wrapper

    for name in reals:
        monkeypatch.setattr(Path, name, guard(name))


def test_plan_lists_an_unstattable_data_dir_through_the_admin_ns(
    fleet: Fleet, monkeypatch: pytest.MonkeyPatch
) -> None:
    deny(monkeypatch, *(legacy_data(fleet, m) for m in MEMBERS))

    result = plan(fleet.state, POOL, uids=fleet.uids, run_admin_fn=fleet.admin)

    alpha = next(m for m in result.members if m.id == "alpha")
    assert alpha.data_entries == ("alpha.db",), "the admin ns knows what the harness cannot see"
    assert alpha.data_readable is True


def test_plan_without_an_admin_ns_reports_unreadable_rather_than_empty(
    fleet: Fleet, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No allocator (a state dir copied off the box): say so, never say 'empty'."""
    deny(monkeypatch, *(legacy_data(fleet, m) for m in MEMBERS))

    result = plan(fleet.state, POOL)

    alpha = next(m for m in result.members if m.id == "alpha")
    assert alpha.data_readable is False
    assert alpha.data_entries == ()
    assert "UNREADABLE" in "\n".join(result.lines())


def test_adopt_moves_data_when_the_pool_root_cannot_be_stat_ed(
    fleet: Fleet, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The racknerd failure, reproduced: `<pool-root>/data` exists and is 0750.

    ``sync`` creates it (``ensure_service_root`` + ``make_pool_data_dirs``) and
    chowns it to the pool's block, so the very first adoption of every pool
    lands on a directory the harness cannot stat into.
    """
    pool_root = fleet.state.service_root(POOL_ID)
    (pool_root / DATA_DIRNAME).mkdir(parents=True)
    deny(monkeypatch, pool_root)

    report = run_adopt(fleet)

    assert report.noop is False
    for member in MEMBERS:
        moved = pool_data(fleet, member) / f"{member}.db"
        assert moved.read_bytes() == b"SQLite format 3\x00" + member.encode()


def test_an_absent_target_directory_is_empty_not_unknown(
    fleet: Fleet, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`data/<member>` that was never created holds nothing, so nothing collides."""
    pool_root = fleet.state.service_root(POOL_ID)
    (pool_root / DATA_DIRNAME).mkdir(parents=True)
    deny(monkeypatch, pool_root)

    assert not os.path.exists(pool_root / DATA_DIRNAME / "alpha")
    report = run_adopt(fleet)

    assert sorted(report.moved) == [f"{m}:{m}.db" for m in MEMBERS]


def test_a_target_directory_that_exists_but_cannot_be_listed_refuses(
    fleet: Fleet, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unlistable is not empty: refuse rather than move data onto unknown files."""
    pool_root = fleet.state.service_root(POOL_ID)
    target = pool_root / DATA_DIRNAME / "alpha"
    target.mkdir(parents=True)
    fleet.admin.unlistable.add(str(target))
    deny(monkeypatch, pool_root)

    with pytest.raises(PoolAdoptError) as excinfo:
        run_adopt(fleet)

    assert "alpha" in str(excinfo.value)
    assert "cannot be listed" in str(excinfo.value)
    assert (legacy_data(fleet, "alpha") / "alpha.db").exists(), "nothing may move"


def test_a_collision_is_still_caught_through_the_admin_ns(
    fleet: Fleet, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal must survive the fallback, or the fix would trade one bug for a worse one."""
    target = pool_data(fleet, "alpha")
    target.mkdir(parents=True)
    (target / "alpha.db").write_bytes(b"a different database")
    deny(monkeypatch, fleet.state.service_root(POOL_ID))

    with pytest.raises(PoolAdoptError) as excinfo:
        run_adopt(fleet)

    assert "alpha.db" in str(excinfo.value)
    assert (target / "alpha.db").read_bytes() == b"a different database"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_a_real_0000_directory_does_not_raise_out_of_plan(fleet: Fleet) -> None:
    """The same probe again with real permission bits rather than a monkeypatch.

    Only ``plan`` and the collision check are exercised here, not the whole
    move: the fake admin namespace runs as the unprivileged test user and so
    genuinely cannot ``mkdir`` inside a 0000 directory, where the real one holds
    CAP_DAC_OVERRIDE and can. That half is proved on the box by
    ``tests/linux/test_pool_adopt_live.py``; what this pins down portably is
    that a real EACCES from ``Path.is_dir()`` never escapes.
    """
    data = fleet.state.service_root(POOL_ID) / DATA_DIRNAME
    data.mkdir(parents=True)
    os.chmod(data, 0o000)
    try:
        result = plan(fleet.state, POOL, uids=fleet.uids, run_admin_fn=fleet.admin)
        pool_mod._check_collisions(
            pool_mod._Adoption(
                state=fleet.state,
                pool=POOL,
                pool_id=POOL_ID,
                plan=result,
                uids=fleet.uids,
                run_admin_fn=fleet.admin,
                control=None,
                secrets=store_for(fleet.state),
                rows=None,
                sleep=lambda _s: None,
                now=lambda: 0.0,
            )
        )
    finally:
        os.chmod(data, 0o750)

    assert [m.id for m in result.members] == list(MEMBERS)
    assert all(m.data_readable for m in result.members)


# --------------------------------------------------------------------------- adopt


def test_adopt_moves_each_members_data_into_the_pool_root(fleet: Fleet) -> None:
    report = run_adopt(fleet)

    for member in MEMBERS:
        moved = pool_data(fleet, member) / f"{member}.db"
        assert moved.read_bytes() == b"SQLite format 3\x00" + member.encode()
        assert not (legacy_data(fleet, member) / f"{member}.db").exists()
    assert report.noop is False
    assert sorted(report.moved) == [f"{m}:{m}.db" for m in MEMBERS]


def test_the_moved_data_is_chowned_to_the_pools_uid_block(fleet: Fleet) -> None:
    run_adopt(fleet)

    owners = {path: owner for owner, path in fleet.admin.chowns}
    for member in MEMBERS:
        assert owners[str(pool_data(fleet, member))] == "1000:1000"
    # ...and the block used for the second hop is the pool's, not the member's.
    pool_hops = [
        block
        for argv, block in zip(fleet.admin.calls, fleet.admin.blocks, strict=True)
        if argv[0] == "mv" and str(fleet.state.service_root(POOL_ID)) in " ".join(argv)
    ]
    assert pool_hops and all(b == POOL_BLOCK for b in pool_hops)


def test_adopt_is_idempotent(fleet: Fleet) -> None:
    first = run_adopt(fleet)
    after_first = _tree(fleet.state.root)

    second = run_adopt(fleet)

    assert first.noop is False
    assert second.noop is True
    assert second.moved == ()
    assert _tree(fleet.state.root) == after_first
    assert "nothing to adopt" in second.summary()


def test_adopt_never_deletes_the_legacy_root(fleet: Fleet) -> None:
    run_adopt(fleet)

    for member in MEMBERS:
        root = fleet.state.service_root(member)
        assert root.is_dir()
        assert (root / "repo" / "pyproject.toml").is_file()
        assert legacy_data(fleet, member).is_dir()
    assert "rm" not in [c[0] for c in fleet.admin.calls]


def test_adopt_removes_only_the_member_declaration(fleet: Fleet) -> None:
    report = run_adopt(fleet)

    for member in MEMBERS:
        assert not fleet.state.service_decl_path(member).exists()
    assert fleet.state.service_decl_path(STANDALONE).exists()
    assert sorted(report.declarations_removed) == list(MEMBERS)


def test_secrets_are_copied_under_the_mangled_name_and_the_originals_stay(fleet: Fleet) -> None:
    report = run_adopt(fleet)

    secrets = store_for(fleet.state)
    assert secrets.names(POOL_ID) == [
        f"DEEPSEEK_API_KEY__{mangle_member('alpha')}",
        f"SVC_SECRET__{mangle_member('alpha')}",
        f"SVC_SECRET__{mangle_member('beta')}",
    ]
    for (service_id, name), value in SECRET_VALUES.items():
        assert secrets.path(service_id, name).read_bytes() == value
        target = secrets.path(POOL_ID, f"{name}__{mangle_member(service_id)}")
        assert target.read_bytes() == value, "the copy must be byte-exact"
        assert oct(target.stat().st_mode)[-3:] == "600"
    assert len(report.secrets_copied) == len(SECRET_VALUES)


def test_no_secret_value_reaches_a_log_line_or_the_report(
    fleet: Fleet, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("DEBUG"):
        report = run_adopt(fleet)

    haystack = "\n".join([caplog.text, report.summary(), *report.actions, str(report)])
    for value in SECRET_VALUES.values():
        assert value.decode() not in haystack


def test_an_existing_pool_secret_is_never_overwritten(fleet: Fleet) -> None:
    secrets = store_for(fleet.state)
    name = f"SVC_SECRET__{mangle_member('alpha')}"
    secrets.set(POOL_ID, name, b"generated-by-sync-already")

    run_adopt(fleet)

    assert secrets.path(POOL_ID, name).read_bytes() == b"generated-by-sync-already"


def test_adopt_stops_each_member_before_moving_its_data(fleet: Fleet) -> None:
    fleet.control.rows = {
        "alpha": {"status": "running", "pid": 11},
        "beta": {"status": "running", "pid": 12},
    }

    run_adopt(fleet)

    assert sorted(fleet.control.stopped) == list(MEMBERS)


def test_adopt_refuses_while_the_pool_is_running(fleet: Fleet) -> None:
    fleet.control.rows = {POOL_ID: {"status": "running", "pid": 4242}}

    with pytest.raises(PoolAdoptError) as excinfo:
        run_adopt(fleet)

    message = str(excinfo.value)
    assert POOL_ID in message and "running" in message
    assert (legacy_data(fleet, "alpha") / "alpha.db").exists(), "nothing may move"
    assert fleet.state.service_decl_path("alpha").exists()


def test_adopt_proceeds_when_no_supervisor_is_listening(fleet: Fleet) -> None:
    fleet.control = FakeControl(up=False)

    report = run_adopt(fleet)

    assert report.noop is False
    assert (pool_data(fleet, "alpha") / "alpha.db").exists()


def test_adopt_refuses_when_the_destination_already_holds_that_name(fleet: Fleet) -> None:
    dest = pool_data(fleet, "alpha")
    dest.mkdir(parents=True)
    (dest / "alpha.db").write_bytes(b"a different database")

    with pytest.raises(PoolAdoptError) as excinfo:
        run_adopt(fleet)

    assert "alpha.db" in str(excinfo.value)
    assert (legacy_data(fleet, "alpha") / "alpha.db").exists()
    assert (dest / "alpha.db").read_bytes() == b"a different database"


def test_a_crashed_adopt_finishes_the_move_on_the_next_run(fleet: Fleet) -> None:
    """A file left loose in the staging directory is drained, not orphaned.

    That flat layout is what the build deployed on racknerd during the failed
    cutover could leave behind; the payload directory below is what this build
    writes. Both are drained, because the recovery path has to work against the
    state the *previous* build could produce.
    """
    staging = pool_mod.staging_dir(fleet.state, "alpha")
    staging.mkdir(parents=True)
    (staging / "leftover.db").write_bytes(b"half-adopted")

    run_adopt(fleet)

    assert (pool_data(fleet, "alpha") / "leftover.db").read_bytes() == b"half-adopted"
    assert not staging.exists()


# ------------------------------------------------------ the stop changes the disk
#
# Second live blocker (`.claude/state/diagnosis-pool-cutover.md`, second half):
# `adopt` planned against a *running* fleet and then moved that name list after
# stopping the member. A WAL-mode SQLite database is three files while the
# service runs and one file once it has shut down cleanly, so the list was
# stale by construction -- and a multi-argument `mv` is not atomic, so the
# abort left one database in the staging directory and the member down.


def wal_sidecars(fleet: Fleet, member: str) -> list[Path]:
    return [legacy_data(fleet, member) / f"{member}.db{s}" for s in ("-wal", "-shm")]


def running_with_wal(fleet: Fleet) -> None:
    """Every member up, each holding a `.db` plus a live `-wal`/`-shm` pair."""
    for i, member in enumerate(MEMBERS):
        for path in wal_sidecars(fleet, member):
            path.write_bytes(b"write-ahead log")
        fleet.control.rows[member] = {"status": "running", "pid": 4000 + i}

    def clean_shutdown(member: str) -> None:
        for path in wal_sidecars(fleet, member):
            path.unlink(missing_ok=True)

    fleet.control.on_stop = clean_shutdown


def test_the_move_acts_on_the_post_stop_directory_not_on_the_plan(fleet: Fleet) -> None:
    running_with_wal(fleet)
    before = plan(fleet.state, POOL, uids=fleet.uids, run_admin_fn=fleet.admin)
    assert before.members[0].data_entries == ("alpha.db", "alpha.db-shm", "alpha.db-wal")

    report = run_adopt(fleet)

    for member in MEMBERS:
        assert (pool_data(fleet, member) / f"{member}.db").read_bytes() == (
            b"SQLite format 3\x00" + member.encode()
        )
        for name in (f"{member}.db-wal", f"{member}.db-shm"):
            assert not (pool_data(fleet, member) / name).exists(), "checkpointed away by the stop"
    assert sorted(report.moved) == [f"{m}:{m}.db" for m in MEMBERS]


def test_a_member_whose_data_vanishes_entirely_at_the_stop_is_a_no_op(fleet: Fleet) -> None:
    """The extreme of the same staleness: nothing left to move, so move nothing."""
    fleet.control.rows = {m: {"status": "running", "pid": 7} for m in MEMBERS}

    def wipe(member: str) -> None:
        for path in legacy_data(fleet, member).iterdir():
            path.unlink()

    fleet.control.on_stop = wipe

    report = run_adopt(fleet)

    assert report.moved == ()
    assert sorted(report.declarations_removed) == list(MEMBERS)
    assert len(report.secrets_copied) == len(SECRET_VALUES)


def test_an_empty_legacy_data_dir_is_a_clean_no_op(fleet: Fleet) -> None:
    for member in MEMBERS:
        (legacy_data(fleet, member) / f"{member}.db").unlink()

    report = run_adopt(fleet)

    assert report.moved == ()
    assert report.noop is False  # the declarations and the secrets are still work
    for member in MEMBERS:
        assert legacy_data(fleet, member).is_dir()


def test_the_members_data_dir_is_left_in_place_empty_and_still_its_own(fleet: Fleet) -> None:
    """The move takes the directory, so it is put back: an empty, member-owned data/."""
    run_adopt(fleet)

    for member in MEMBERS:
        data = legacy_data(fleet, member)
        assert data.is_dir()
        assert list(data.iterdir()) == []
        assert (f"{INNER_UID}:{INNER_GID}", str(data)) in fleet.admin.chowns
    assert not (pool_mod.staging_dir(fleet.state, "alpha")).exists()


def test_a_failed_second_hop_parks_the_data_and_the_next_run_finishes_it(fleet: Fleet) -> None:
    """The first hop is a rename, so an interrupted adoption is always resumable."""
    from ams.userns import SpawnError

    parked = pool_mod.staging_dir(fleet.state, "alpha") / pool_mod.PAYLOAD_DIRNAME
    # Only the second hop fails; the first has already renamed the directory.
    fleet.admin.fail_on.add(f"mv -T {parked} {pool_data(fleet, 'alpha')}")

    with pytest.raises(SpawnError):
        run_adopt(fleet)

    assert (parked / "alpha.db").read_bytes() == b"SQLite format 3\x00alpha"
    assert list(legacy_data(fleet, "alpha").iterdir()) == [], "the whole directory moved at once"

    fleet.admin.fail_on.clear()
    report = run_adopt(fleet)

    assert (pool_data(fleet, "alpha") / "alpha.db").read_bytes() == b"SQLite format 3\x00alpha"
    assert "alpha:alpha.db" in report.moved
    assert not pool_mod.staging_dir(fleet.state, "alpha").exists()


def test_a_non_empty_target_with_different_names_is_merged_not_clobbered(fleet: Fleet) -> None:
    """The whole-directory rename cannot land on a non-empty target, so it merges."""
    target = pool_data(fleet, "alpha")
    target.mkdir(parents=True)
    (target / "unrelated.db").write_bytes(b"already in the pool")

    report = run_adopt(fleet)

    assert (target / "alpha.db").read_bytes() == b"SQLite format 3\x00alpha"
    assert (target / "unrelated.db").read_bytes() == b"already in the pool"
    assert "alpha:alpha.db" in report.moved


def test_adopt_dry_run_is_exactly_plan(fleet: Fleet) -> None:
    before = _tree(fleet.state.root)

    result = adopt(
        fleet.state,
        POOL,
        uids=fleet.uids,
        run_admin_fn=fleet.admin,
        control=fleet.control,
        dry_run=True,
    )

    assert [m.id for m in result.members] == list(MEMBERS)
    assert _tree(fleet.state.root) == before


# --------------------------------------------------------------------------- cli


def test_cli_pool_help_documents_both_subcommands(capsys: Any) -> None:
    from ams.cli import main as ams_main

    with pytest.raises(SystemExit):
        ams_main(["platform", "pool", "--help"])
    out = capsys.readouterr().out
    assert "plan" in out and "adopt" in out


def test_cli_pool_plan_prints_the_members(fleet: Fleet, capsys: Any) -> None:
    from ams.cli import main as ams_main

    code = ams_main(["platform", "pool", "plan", POOL, "--state-dir", str(fleet.state.root)])

    out = capsys.readouterr().out
    assert code == 0
    assert POOL_ID in out
    for member in MEMBERS:
        assert member in out


def test_cli_pool_adopt_dry_run_changes_nothing(fleet: Fleet, capsys: Any) -> None:
    from ams.cli import main as ams_main

    before = _tree(fleet.state.root)
    code = ams_main(
        ["platform", "pool", "adopt", POOL, "--dry-run", "--state-dir", str(fleet.state.root)]
    )

    capsys.readouterr()
    assert code == 0
    assert _tree(fleet.state.root) == before


# --------------------------------------------------------------------------- status


def _status_fleet() -> dict[str, Any]:
    """The §3.4 example: one two-member pool and one standalone service."""
    return {
        POOL_ID: _record("9f1c0b2" + "0" * 33, pool=POOL, pool_members=list(MEMBERS)),
        "alpha": _record("9f1c0b2" + "0" * 33, pool=POOL),
        "beta": _record("9f1c0b2" + "0" * 33, pool=POOL),
        STANDALONE: _record("9f1c0b2" + "0" * 33),
    }


def test_status_puts_the_pool_first_with_its_member_count() -> None:
    services = _status_fleet()

    lines = format_status(services, sorted(services))

    assert lines[0].startswith(POOL_ID)
    assert "(2)" in lines[0]
    assert [line.split()[0] for line in lines] == [POOL_ID, "alpha", "beta", STANDALONE]


def test_status_indents_members_under_their_pool_and_names_it() -> None:
    services = _status_fleet()

    lines = format_status(services, sorted(services))

    assert lines[1].startswith("  alpha")
    assert lines[2].startswith("  beta")
    assert all(f" {POOL} " in line for line in lines[1:3])
    assert not lines[3].startswith(" ")


def test_status_of_one_member_adds_a_line_naming_its_pool_and_stage() -> None:
    services = _status_fleet()

    lines = format_status(services, ["alpha"])

    assert lines[0].lstrip().startswith("alpha")
    assert any(POOL_ID in line and "healthy" in line for line in lines[1:])


def test_status_of_a_fleet_with_no_pools_keeps_its_columns() -> None:
    """The pool column only exists where a pool does: no pools, no rewrite."""
    services = {STANDALONE: _record("9f1c0b2" + "0" * 33)}

    lines = format_status(services, [STANDALONE])

    assert len(lines) == 1
    assert lines[0].split() == [
        STANDALONE,
        "healthy",
        ("9f1c0b2" + "0" * 33)[:12],
        "since=2026-09-03T00:00:00Z",
    ]


# --------------------------------------------------------------------------- rollback


POOL_OVERLAY = f'pool = "{POOL}"\n'


@pytest.fixture
def pooled_upstream(tmp_path: Path) -> tuple[Path, str, str]:
    """Two commits of two pooled manifests; the second bumps alpha's memory."""
    src = tmp_path / "upstream-pool"
    src.mkdir()
    _git(src, "init", "-q", "-b", "main")
    files = {
        "README.md": "api\n",
    }
    for i, member in enumerate(MEMBERS):
        files[f"services/{member}/service.yaml"] = service_manifest(member, port=9301 + i)
        files[f"services/{member}/service.ams.toml"] = POOL_OVERLAY
    first = _commit(src, files, "first")
    second = _commit(
        src,
        {"services/alpha/service.yaml": service_manifest("alpha", port=9301, memory="256M")},
        "second",
    )
    return src, first, second


@pytest.fixture
def rollback_env(
    fleet: Fleet,
    registry: FakeRegistry,  # noqa: F811 - the imported fixture, requested by name
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    """The rollback seams that would touch the host, replaced by recorders."""
    calls: list[tuple[str, ...]] = []

    def fake_stage(_self: SourceMirror, sha: str, service_root: Path, _b: Any, **_kw: Any) -> Path:
        repo = Path(service_root) / "repo"
        repo.mkdir(parents=True, exist_ok=True)
        (repo / SHA_MARKER).write_text(sha + "\n", encoding="utf-8")
        calls.append(("stage", Path(service_root).parent.name, sha))
        return repo

    def fake_place_jwt_key(_state: StateDir, service_id: str, _b: Any, **_kw: Any) -> bool:
        calls.append(("jwt", service_id))
        return False

    def fake_provision(decl: Any, root: Path, _store: Any, _b: Any, **_kw: Any) -> None:
        python_venv_dir(decl, Path(root)).mkdir(parents=True, exist_ok=True)
        calls.append(("provision", decl.id))

    def fake_status(_state: StateDir, **_kw: Any) -> dict[str, Any]:
        return {"ok": True, "services": {POOL_ID: {"status": "stopped", "pid": None}}}

    def fake_stop(_state: StateDir, service_id: str, **_kw: Any) -> dict[str, Any]:
        calls.append(("stop", service_id))
        return {"ok": True}

    def fake_reload(_state: StateDir, **_kw: Any) -> dict[str, Any]:
        calls.append(("reload",))
        return {"ok": True, "reload": {"added": [], "changed": [POOL_ID], "errors": {}}}

    def fake_restart(_state: StateDir, service_id: str, **_kw: Any) -> dict[str, Any]:
        calls.append(("restart", service_id))
        return {"ok": True}

    def fake_place_pool_file(root: Path, name: str, content: bytes, _b: Any, **_kw: Any) -> None:
        Path(root).mkdir(parents=True, exist_ok=True)
        (Path(root) / name).write_bytes(content)
        calls.append(("pool-file", name))

    def fake_make_pool_data_dirs(root: Path, members: Any, _b: Any) -> None:
        for member in members:
            (Path(root) / DATA_DIRNAME / member).mkdir(parents=True, exist_ok=True)
        calls.append(("pool-data", *members))

    monkeypatch.setattr(SourceMirror, "stage", fake_stage)
    monkeypatch.setattr(rollback_mod, "place_jwt_key", fake_place_jwt_key)
    monkeypatch.setattr(rollback_mod, "provision", fake_provision)
    monkeypatch.setattr(rollback_mod, "ctl_status", fake_status)
    monkeypatch.setattr(rollback_mod, "ctl_stop", fake_stop)
    monkeypatch.setattr(rollback_mod, "ctl_reload", fake_reload)
    monkeypatch.setattr(rollback_mod, "ctl_restart", fake_restart)
    monkeypatch.setattr(rollback_mod, "place_pool_file", fake_place_pool_file)
    monkeypatch.setattr(rollback_mod, "make_pool_data_dirs", fake_make_pool_data_dirs)
    monkeypatch.setattr(rollback_mod, "uid_allocator", lambda _state: fleet.uids)

    fleet.calls = calls  # type: ignore[attr-defined]
    fleet.registry = registry  # type: ignore[attr-defined]
    return fleet


def seed_pool(fleet: Any, src: Path, sha: str, prev: str) -> None:
    """The state a completed sync of the pool at ``sha`` would have left."""
    write_platform_state(
        fleet.state,
        sha,
        **{
            POOL_ID: _record(sha, prev_sha=prev, pool=POOL, pool_members=list(MEMBERS)),
            "alpha": _record(sha, prev_sha=prev, pool=POOL),
            "beta": _record(sha, prev_sha=prev, pool=POOL),
        },
    )
    mirror = SourceMirror(fleet.store.root, "api", url=str(src))
    mirror.fetch("main")
    for member in MEMBERS:
        registry_dir(fleet.state).mkdir(parents=True, exist_ok=True)
        (registry_dir(fleet.state) / f"{member}.json").write_text(
            json.dumps({"version": 1, "id": member, "health_path": "/health"}), encoding="utf-8"
        )
    ports = {
        POOL_ID: {"pool": fleet.registry.port, **{m: fleet.registry.port for m in MEMBERS}},
    }
    fleet.state.ports_state.parent.mkdir(parents=True, exist_ok=True)
    fleet.state.ports_state.write_text(
        json.dumps({"version": 1, "ports": ports}, indent=2, sort_keys=True), encoding="utf-8"
    )


def pool_cfg(src: Path, registry: FakeRegistry) -> SyncConfig:  # noqa: F811 - fixture
    return SyncConfig(
        repo_url=str(src),
        registry_url=registry.url,
        auth_url=registry.url,
        health_deadline_s=3.0,
        health_interval_s=0.05,
    )


def test_rollback_of_a_pooled_member_refuses_and_names_the_pool(
    rollback_env: Any, pooled_upstream: Any
) -> None:
    src, first, second = pooled_upstream
    seed_pool(rollback_env, src, second, first)

    with pytest.raises(RollbackError) as excinfo:
        rollback(
            rollback_env.state,
            rollback_env.store,
            "alpha",
            cfg=pool_cfg(src, rollback_env.registry),
        )

    message = str(excinfo.value)
    assert POOL_ID in message
    assert "pool" in message.lower()
    assert f"rollback {POOL_ID}" in message


def test_rollback_of_the_pool_reaches_the_per_member_health_gate(
    rollback_env: Any, pooled_upstream: Any
) -> None:
    src, first, second = pooled_upstream
    seed_pool(rollback_env, src, second, first)

    report = rollback(
        rollback_env.state,
        rollback_env.store,
        POOL_ID,
        cfg=pool_cfg(src, rollback_env.registry),
        secrets=store_for(rollback_env.state),
        uids=rollback_env.uids,
        sleep=lambda _s: None,
    )

    assert report.ok, report.error
    assert report.to_sha == first
    assert [f"health:{m}" for m in MEMBERS] == [a for a in report.actions if a.startswith("health")]


def test_rollback_of_the_pool_rewrites_the_runner_and_pool_json(
    rollback_env: Any, pooled_upstream: Any
) -> None:
    src, first, second = pooled_upstream
    seed_pool(rollback_env, src, second, first)

    rollback(
        rollback_env.state,
        rollback_env.store,
        POOL_ID,
        cfg=pool_cfg(src, rollback_env.registry),
        secrets=store_for(rollback_env.state),
        uids=rollback_env.uids,
        sleep=lambda _s: None,
    )

    placed = [c[1] for c in rollback_env.calls if c[0] == "pool-file"]
    assert sorted(placed) == sorted([POOL_JSON_NAME, POOL_RUNNER_NAME])
    decl = rollback_env.state.service_decl_path(POOL_ID).read_text()
    assert f'id = "{POOL_ID}"' in decl
    for member in MEMBERS:
        assert (mounts_dir(rollback_env.state) / f"{member}.json").is_file()
        assert not rollback_env.state.service_decl_path(member).exists()
