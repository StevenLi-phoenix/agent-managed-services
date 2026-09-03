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
from ams.spawn import DATA_DIRNAME
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

    def __call__(self, argv: Any, block: UidBlock, **_kw: Any) -> AdminResult:
        args = tuple(str(a) for a in argv)
        self.calls.append(args)
        self.blocks.append(block)
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
            path = Path(target)
            path.mkdir(parents=True, exist_ok=True)
            os.chmod(path, mode)
        return self._ok(args)

    def _chown(self, args: tuple[str, ...]) -> AdminResult:
        rest = [a for a in args[1:] if a != "-R"]
        owner, paths = rest[0], rest[1:]
        for path in paths:
            if not Path(path).exists():  # pragma: no cover - defensive
                raise AssertionError(f"chown on a path that does not exist: {path}")
            self.chowns.append((owner, path))
        return self._ok(args)

    def _find(self, args: tuple[str, ...]) -> AdminResult:
        directory = Path(args[1])
        if not directory.is_dir():
            return AdminResult(argv=args, returncode=1, stdout=b"", stderr=b"no such directory")
        entries = sorted(str(p) for p in directory.iterdir())
        return AdminResult(
            argv=args,
            returncode=0,
            stdout=b"".join(e.encode() + b"\0" for e in entries),
            stderr=b"",
        )

    def _mv(self, args: tuple[str, ...]) -> AdminResult:
        no_clobber = "-n" in args
        rest = [a for a in args[1:] if a != "-n"]
        dest = Path(rest[rest.index("-t") + 1])
        sources = rest[rest.index("--") + 1 :]
        for src in sources:
            target = dest / Path(src).name
            if target.exists() and no_clobber:  # pragma: no cover - refused before we get here
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
    """A file left in the staging directory is drained, not orphaned."""
    staging = pool_mod.staging_dir(fleet.state, "alpha")
    staging.mkdir(parents=True)
    (staging / "leftover.db").write_bytes(b"half-adopted")

    run_adopt(fleet)

    assert (pool_data(fleet, "alpha") / "leftover.db").read_bytes() == b"half-adopted"
    assert not staging.exists()


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
