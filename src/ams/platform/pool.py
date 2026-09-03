"""Adopt N standalone services into one pool (PLAN-pool §5.8, §7.2 steps 5-6).

``ams platform sync`` groups pooled manifests into one declaration but it never
moves a byte of data: a sync tick is mechanical and runs on a timer, and moving
a live SQLite file is neither. So the first tick after ``pool = "core"`` appears
in the overlays stops at the adoption guard and escalates, naming this module's
command. Adoption is then an explicit, operator-run, idempotent step::

    ams platform pool plan core      # what would move
    ams platform pool adopt core     # move it

For each member of the pool, ``adopt``:

1. stops it over the control socket (a process that is writing to a database is
   not a process whose database may move);
2. moves ``services/<member>/root/data/*`` into
   ``services/pool-<name>/root/data/<member>/`` and hands it to the pool's uid;
3. copies ``secrets/<member>/<NAME>`` to ``secrets/pool-<name>/<NAME>__<MANGLED>``
   (§6): one process, one env, so the member's id becomes the name suffix;
4. unlinks ``services/<member>/service.toml`` -- the pool's declaration is what
   runs the member now, and two declarations would mean two processes serving
   one identity.

What it never does: **delete the legacy root**. The root keeps the member's
repo, its venv and an empty ``data/`` after adoption, and an operator removes it
by hand once a backup cycle has proved the pool healthy (§7.2 step 10). Nothing
here is undone automatically either -- the honest reverse of a migration that
moved databases is a restore from the backup, and the docs say so rather than
this module pretending otherwise.

**Why the move is two hops through a harness-owned staging directory.**
``run_admin`` maps exactly one uid block into the namespace it forks (inner 0 =
the harness, inner 1000 = *that* block). A member's ``data/`` is owned by the
member's block and the pool's ``data/`` by the pool's, so no single admin
namespace holds CAP_DAC_OVERRIDE over both directories, and a direct ``mv``
fails with EACCES on whichever end is unmapped. The move therefore runs as:

    <member-root>/data  --(member block)-->  <state>/platform/adopt/<member>/data
    <state>/platform/adopt/<member>/data  --(pool block)-->  <pool-root>/data/<member>

Both hops are renames on one filesystem, so a 2 GiB database moves in constant
time and is never copied. The staging directory is owned by the harness, which
is inner 0 in *both* namespaces -- that is the whole trick. The alternative,
teaching ``run_admin`` to map two blocks at once, was rejected: it widens the
privilege of every admin fork in the harness to fix one operation that runs
once per pool in the lifetime of the fleet.

**Why each hop moves the directory rather than its files.** A rename of one
directory is atomic and carries whatever is inside it, so nothing here ever
acts on a list of names captured earlier. That matters because the plan is
computed while the fleet is still *running*: a WAL-mode SQLite service has
``x.db``, ``x.db-wal`` and ``x.db-shm`` on disk, and deletes the last two when
it shuts down cleanly, so a name list taken before ``_stop_member`` names two
files that no longer exist by the time the move runs. Moving them failed, and
because a multi-argument ``mv`` is not atomic it failed *after* moving the
database -- one member left down with an empty ``data/`` and its database in
the staging directory. Both halves of that are in
`.claude/state/diagnosis-pool-cutover.md`.

A run that dies between the hops leaves the member's ``data/`` parked in the
staging directory, and the next run finishes the second hop from there. The
member's own ``data/`` is recreated empty and handed back to its uid, so the
legacy root keeps the shape a service root has (`<root>/data` is
``AMS_DATA_DIR``) and an operator can put a database back by hand.
"""

from __future__ import annotations

import argparse
import logging
import os
import stat
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ams.platform.sync import (
    POOL_ID_PREFIX,
    PlatformState,
    SyncError,
    pool_id_for,
    state_path,
)
from ams.platform.translate import mangle_member
from ams.secrets import SecretStore, store_for
from ams.spawn import DATA_DIRNAME, INNER_GID, INNER_UID
from ams.state import StateCorrupt, StateDir
from ams.uidmap import UidBlock
from ams.userns import AdminResult
from ams.userns import run_admin as run_admin  # noqa: PLC0414 - the patch point

log = logging.getLogger("ams.platform.pool")

EXIT_OK = 0
EXIT_ERROR = 1
#: A precondition is wrong and nothing was touched. Same meaning as rollback's.
EXIT_PRECONDITION = 2

#: Where the two-hop move parks files between namespaces. Harness-owned, 0700,
#: on the same filesystem as both roots so every hop is a rename.
ADOPT_DIRNAME = "adopt"
#: The member's whole ``data/`` is renamed to ``<staging>/data`` and then
#: renamed again into the pool root. One directory, two renames, no name list:
#: see :func:`_move_member_data`.
PAYLOAD_DIRNAME = "data"
STAGING_MODE = 0o700
DATA_DIR_MODE = 0o750
SECRET_FILE_MODE = 0o600

#: Statuses that mean "no process is running under this id". Defined here rather
#: than in :mod:`ams.platform.rollback` because adoption is the older question
#: ("may I move this file?") and rollback imports it back; one definition either
#: way, and this one has no import cycle.
DOWN_STATUSES = frozenset({"stopped", "failed", "waiting"})

STOP_POLL_S = 0.25
STOP_DEADLINE_S = 30.0


class PoolAdoptError(RuntimeError):
    """A precondition is wrong. Raised before anything on the host is touched."""


# --------------------------------------------------------------------------- seams


class Control(Protocol):
    """The two control-socket calls adoption makes."""

    def status(self) -> Mapping[str, Any]: ...

    def stop(self, service_id: str) -> Mapping[str, Any]: ...


class _Allocator(Protocol):
    def allocate(self, service_id: str) -> UidBlock: ...


class _AdminFn(Protocol):
    def __call__(self, argv: Sequence[str], block: UidBlock, **kw: Any) -> AdminResult: ...


@dataclass(frozen=True)
class SocketControl:
    """The real control socket. Absent supervisor raises ``ControlError``."""

    state: StateDir
    timeout_s: float = 60.0

    def status(self) -> Mapping[str, Any]:
        return self._request("status")

    def stop(self, service_id: str) -> Mapping[str, Any]:
        return self._request("stop", service_id)

    def _request(self, op: str, service_id: str | None = None) -> Mapping[str, Any]:
        from ams.control import control_socket_path
        from ams.control import request as control_request

        return control_request(
            control_socket_path(self.state), op, service_id, timeout_s=self.timeout_s
        )


def staging_dir(state: StateDir, member_id: str) -> Path:
    """``<state>/platform/adopt/<member>`` -- the halfway house of the move."""
    return Path(state.root) / "platform" / ADOPT_DIRNAME / member_id


# --------------------------------------------------------------------------- the plan


@dataclass(frozen=True)
class MemberPlan:
    """What adopting one member would move. Empty everywhere = already adopted."""

    id: str
    legacy_root: Path
    legacy_data: Path
    target_data: Path
    #: Names (not paths) under ``legacy_data`` that would move.
    data_entries: tuple[str, ...] = ()
    #: Names left in the staging directory by an interrupted run.
    staged_entries: tuple[str, ...] = ()
    #: False when neither the harness nor the admin namespace could list the
    #: directory. Adoption refuses rather than guessing what is in there.
    data_readable: bool = True
    #: ``(source name, target name)`` pairs still to copy, e.g.
    #: ``("SVC_SECRET", "SVC_SECRET__KVSERVICE")``. Names only, never values.
    secrets: tuple[tuple[str, str], ...] = ()
    #: Target names that already have a value and will be left alone.
    secrets_present: tuple[str, ...] = ()
    decl_exists: bool = False
    running: bool = False

    @property
    def needs_adopt(self) -> bool:
        return bool(self.data_entries or self.staged_entries or self.secrets or self.decl_exists)

    def lines(self) -> list[str]:
        out = [f"  {self.id}:"]
        for name in self.data_entries:
            out.append(f"    move {self.legacy_data / name} -> {self.target_data / name}")
        for name in self.staged_entries:
            out.append(f"    finish move {name} -> {self.target_data / name} (staged)")
        for source, target in self.secrets:
            out.append(f"    copy secret {source} -> {target}")
        for target in self.secrets_present:
            out.append(f"    keep secret {target} (already set)")
        if self.decl_exists:
            out.append(f"    remove declaration {self.id}/service.toml")
        if not self.data_readable:
            out.append(f"    UNREADABLE {self.legacy_data} -- adopt will refuse")
        if len(out) == 1:
            out.append("    nothing to do")
        return out


@dataclass(frozen=True)
class PoolPlan:
    """Every member of one pool and what adoption would do to it."""

    pool: str
    pool_id: str
    pool_root: Path
    members: tuple[MemberPlan, ...]
    #: Ids the supervisor reports as up; ``None`` when no supervisor answered.
    running: tuple[str, ...] | None = None

    @property
    def is_noop(self) -> bool:
        return not any(m.needs_adopt for m in self.members)

    @property
    def pending(self) -> tuple[MemberPlan, ...]:
        return tuple(m for m in self.members if m.needs_adopt)

    def lines(self) -> list[str]:
        head = f"{self.pool_id}: {len(self.members)} member(s), root {self.pool_root}"
        if self.running is None:
            head += " (no supervisor is listening)"
        elif self.running:
            head += f" (running: {', '.join(self.running)})"
        out = [head]
        for member in self.members:
            out += member.lines()
        if self.is_noop:
            out.append("nothing to adopt: every member is already in the pool")
        return out

    def summary(self) -> str:
        if self.is_noop:
            return f"{self.pool_id}: nothing to adopt ({len(self.members)} member(s) already in)"
        return f"{self.pool_id}: {len(self.pending)} of {len(self.members)} member(s) to adopt"


@dataclass(frozen=True)
class AdoptReport:
    """What one adoption did. Names only -- a secret value never lands here."""

    pool_id: str
    members: tuple[str, ...]
    noop: bool = False
    moved: tuple[str, ...] = ()
    secrets_copied: tuple[str, ...] = ()
    declarations_removed: tuple[str, ...] = ()
    stopped: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()

    @property
    def exit_code(self) -> int:
        return EXIT_OK

    def summary(self) -> str:
        if self.noop:
            return (
                f"{self.pool_id}: nothing to adopt "
                f"({len(self.members)} member(s) already in the pool)"
            )
        return (
            f"{self.pool_id}: adopted {len(self.members)} member(s); "
            f"moved={len(self.moved)} secrets={len(self.secrets_copied)} "
            f"declarations removed={len(self.declarations_removed)}"
        )


# --------------------------------------------------------------------------- resolving


def resolve(pool_id: str) -> tuple[str, str]:
    """``"core"`` or ``"pool-core"`` -> ``("core", "pool-core")``."""
    if not isinstance(pool_id, str) or not pool_id.strip():
        raise PoolAdoptError("a pool name is required, e.g. 'ams platform pool plan core'")
    name = pool_id[len(POOL_ID_PREFIX) :] if pool_id.startswith(POOL_ID_PREFIX) else pool_id
    if not name:
        raise PoolAdoptError(f"{pool_id!r} is not a pool name")
    return name, pool_id_for(name)


def members_of(state: StateDir, pool: str, pool_id: str) -> tuple[str, ...]:
    """The pool's members, from the platform state file.

    Two sources, unioned: the pool record's ``pool_members`` (what sync wrote
    when it grouped the manifests) and every member record carrying
    ``pool = <name>``. Either alone is enough to adopt; both exist because a
    pool that failed to build still leaves the member rows behind.
    """
    path = state_path(state)
    try:
        platform = PlatformState.load(path)
    except (StateCorrupt, SyncError) as e:
        raise PoolAdoptError(f"{path}: {e}") from None
    found: set[str] = set()
    record = platform.records.get(pool_id)
    if record is not None:
        found.update(record.pool_members)
    found.update(
        sid for sid, rec in platform.records.items() if rec.pool == pool and sid != pool_id
    )
    if not found:
        raise PoolAdoptError(
            f"no pool {pool!r} in {path}: there is no record for {pool_id} and no service "
            f'declares pool = "{pool}". Add the overlay lines and run '
            "'ams platform sync' once so the pool is grouped, then adopt it."
        )
    return tuple(sorted(found))


# --------------------------------------------------------------------------- listing


def _split0(raw: bytes) -> list[str]:
    return [chunk.decode("utf-8", "surrogateescape") for chunk in raw.split(b"\0") if chunk]


@dataclass(frozen=True)
class Listing:
    """What is directly under one directory, and how much of that is known.

    Three states, not two, because two of them look identical to a caller that
    only asks "did the listing work?" and mean opposite things:

    - ``exists=False`` -- the directory is not there. It holds nothing, so
      nothing in it can collide with anything. This is the ordinary state of
      ``<pool-root>/data/<member>`` before the first adoption.
    - ``readable=False`` -- it is there and *nothing* could enumerate it,
      neither the harness nor the admin namespace. Refuse: moving data into a
      directory whose contents are unknown is how a database gets overwritten.
    - otherwise ``entries`` is the complete list.

    Collapsing the first two into "could not list" is exactly the second trap
    named in ``.claude/state/diagnosis-pool-cutover.md``: it turns a fresh pool
    root into a refusal for a reason that is not true.
    """

    entries: tuple[str, ...] = ()
    exists: bool = True
    readable: bool = True


def _list_dir(
    directory: Path,
    *,
    service_id: str,
    uids: _Allocator | None,
    run_admin_fn: _AdminFn | None,
) -> Listing:
    """Names directly under ``directory``, from the harness or the admin ns.

    **Every** harness-side probe here is guarded, the stat as much as the read.
    ``pathlib`` swallows only ENOENT/ENOTDIR/EBADF/ELOOP, so ``Path.is_dir()``
    *re-raises* EACCES -- and both a service root and a pool root are 0750 owned
    by a uid the harness does not have, which is where the live cutover died
    (`.claude/state/diagnosis-pool-cutover.md`). An unreadable directory is not
    an error here; it is the normal case, and the admin namespace is the answer.

    "Could not list" is never reported as "empty": that is how data gets
    orphaned. It is also never reported for a directory that is merely absent.
    """
    try:
        if not directory.is_dir():
            return Listing(exists=False)
        return Listing(entries=tuple(sorted(p.name for p in directory.iterdir())))
    except OSError as e:
        log.debug(
            "%s: cannot inspect %s from the harness (%s); using the admin ns",
            service_id,
            directory,
            e,
        )
    if uids is None or run_admin_fn is None:
        return Listing(readable=False)
    try:
        block = uids.allocate(service_id)
    except (RuntimeError, OSError, ValueError) as e:  # pragma: no cover - host only
        log.warning("%s: cannot allocate a uid block to list %s (%s)", service_id, directory, e)
        return Listing(readable=False)
    # `-mindepth 0` makes find print the start path itself before its children,
    # which is what separates "absent" (exits non-zero having printed nothing)
    # from "there but unreadable" (exits non-zero having printed itself).
    result = run_admin_fn(
        ["find", str(directory), "-mindepth", "0", "-maxdepth", "1", "-print0"], block
    )
    paths = _split0(result.stdout)
    itself = str(directory)
    entries = tuple(sorted(Path(p).name for p in paths if p != itself))
    if result.ok:
        return Listing(entries=entries, exists=itself in paths or bool(entries))
    if itself in paths:
        log.warning(
            "%s: %s exists but the admin ns could not list it (rc=%d): %s",
            service_id,
            directory,
            result.returncode,
            result.stderr.decode("utf-8", "replace").strip() or "(no stderr)",
        )
        return Listing(entries=entries, readable=False)
    log.info(
        "%s: %s does not exist (find rc=%d); it holds nothing",
        service_id,
        directory,
        result.returncode,
    )
    return Listing(exists=False)


def _exists(path: Path, *, unknown: bool) -> bool:
    """``path.exists()`` that cannot raise. ``unknown`` is the EACCES answer.

    Same trap as :func:`_list_dir`: these paths can sit under a directory the
    harness has no ``x`` on, and ``Path.exists()`` re-raises EACCES. Each caller
    picks the answer that fails safe for it, and the choice is written down at
    the call site.
    """
    try:
        return path.exists()
    except OSError as e:
        log.warning("cannot stat %s (%s); assuming exists=%s", path, e, unknown)
        return unknown


def _control_rows(control: Control | None) -> dict[str, Any] | None:
    """The supervisor's service table, or ``None`` when nothing is listening."""
    if control is None:
        return None
    from ams.control import ControlError

    try:
        response = control.status()
    except ControlError as e:
        log.info("no supervisor answered (%s); adopting against a stopped fleet", e)
        return None
    rows = response.get("services")
    return dict(rows) if isinstance(rows, Mapping) else {}


def _is_up(rows: Mapping[str, Any] | None, service_id: str) -> bool:
    if not rows:
        return False
    row = rows.get(service_id)
    if not isinstance(row, Mapping):
        return False
    return bool(row.get("pid")) or str(row.get("status") or "unknown") not in DOWN_STATUSES


# --------------------------------------------------------------------------- plan()


def plan(
    state: StateDir,
    pool_id: str,
    *,
    uids: _Allocator | None = None,
    run_admin_fn: _AdminFn | None = None,
    control: Control | None = None,
    secrets: SecretStore | None = None,
) -> PoolPlan:
    """What ``adopt`` would do, without doing any of it. Reads only.

    ``uids``/``run_admin_fn`` are optional because a plan must work from a
    laptop against a copied state dir; without them an unlistable ``data/``
    comes back as ``data_readable=False`` rather than as an empty directory.
    """
    pool, resolved = resolve(pool_id)
    member_ids = members_of(state, pool, resolved)
    secrets = secrets if secrets is not None else store_for(state)
    rows = _control_rows(control)
    pool_root = state.service_root(resolved)
    have = set(secrets.names(resolved))

    members: list[MemberPlan] = []
    for member in member_ids:
        legacy_root = state.service_root(member)
        legacy = legacy_root / DATA_DIRNAME
        listing = _list_dir(legacy, service_id=member, uids=uids, run_admin_fn=run_admin_fn)
        # Two shapes of interrupted run: this build parks the member's whole
        # `data/` as `<staging>/data`, the build that failed on racknerd parked
        # loose files directly in `<staging>`. Both are reported, and both are
        # drained by the next adoption.
        staging = staging_dir(state, member)
        parked = _list_dir(
            staging / PAYLOAD_DIRNAME, service_id=member, uids=uids, run_admin_fn=run_admin_fn
        )
        loose = _list_dir(staging, service_id=member, uids=uids, run_admin_fn=run_admin_fn)
        staged_names = tuple(
            sorted(
                set(parked.entries) | {n for n in loose.entries if n != PAYLOAD_DIRNAME}
            )
        )
        # Names only: which of the member's secrets still need a copy under the
        # pool id, and which the pool already has (sync generates SVC_SECRET__X
        # when it declares the pool, and a value that exists is never replaced).
        suffix = mangle_member(member)
        names = secrets.names(member)
        pairs_full = tuple(
            (name, f"{name}__{suffix}") for name in names if f"{name}__{suffix}" not in have
        )
        present = tuple(
            sorted(f"{name}__{suffix}" for name in names if f"{name}__{suffix}" in have)
        )
        members.append(
            MemberPlan(
                id=member,
                legacy_root=legacy_root,
                legacy_data=legacy,
                target_data=pool_root / DATA_DIRNAME / member,
                data_entries=listing.entries,
                staged_entries=staged_names,
                data_readable=listing.readable,
                secrets=pairs_full,
                secrets_present=present,
                # unknown=True: a declaration that might be there is treated as
                # there, so adoption tries to remove it and says so if it
                # cannot. The opposite guess leaves a second declaration behind,
                # which is a second process serving one identity.
                decl_exists=_exists(state.service_decl_path(member), unknown=True),
                running=_is_up(rows, member),
            )
        )
    return PoolPlan(
        pool=pool,
        pool_id=resolved,
        pool_root=pool_root,
        members=tuple(members),
        running=None if rows is None else tuple(sorted(sid for sid in rows if _is_up(rows, sid))),
    )


# --------------------------------------------------------------------------- adopt()


@dataclass
class _Adoption:
    """Mutable bookkeeping for one adoption, so each step stays a small function."""

    state: StateDir
    pool: str
    pool_id: str
    plan: PoolPlan
    uids: _Allocator
    run_admin_fn: _AdminFn
    control: Control | None
    secrets: SecretStore
    rows: dict[str, Any] | None
    sleep: Callable[[float], None]
    now: Callable[[], float]
    moved: list[str] = field(default_factory=list)
    secrets_copied: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    stopped: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)

    @property
    def pool_block(self) -> UidBlock:
        return self.uids.allocate(self.pool_id)


def adopt(
    state: StateDir,
    pool_id: str,
    *,
    uids: _Allocator | None = None,
    run_admin_fn: _AdminFn | None = None,
    control: Control | None = None,
    secrets: SecretStore | None = None,
    dry_run: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> AdoptReport | PoolPlan:
    """Move each member's data, secrets and declaration into the pool.

    Idempotent: a second run finds nothing to move and says so. Refuses, before
    touching anything, while the pool process is up, and refuses when a
    member's data would land on a file the pool root already holds -- that
    collision is an operator's decision, not a supervisor's.

    ``dry_run=True`` returns the :class:`PoolPlan` instead, which is exactly
    what ``ams platform pool plan`` prints.
    """
    pool, resolved = resolve(pool_id)
    uids = uids if uids is not None else _default_uids(state)
    run_admin_fn = run_admin_fn if run_admin_fn is not None else run_admin
    control = control if control is not None else SocketControl(state)
    secrets = secrets if secrets is not None else store_for(state)

    rows = _control_rows(control)
    if _is_up(rows, resolved):
        row = (rows or {}).get(resolved) or {}
        raise PoolAdoptError(
            f"{resolved} is running (status={row.get('status')!r} pid={row.get('pid')}); "
            f"adoption moves the data that process is reading. Stop it first: "
            f"ams ctl stop {resolved}"
        )

    the_plan = plan(
        state,
        resolved,
        uids=uids,
        run_admin_fn=run_admin_fn,
        control=None,  # already asked, and asking twice can disagree with itself
        secrets=secrets,
    )
    the_plan = _with_rows(the_plan, rows)
    if dry_run:
        return the_plan

    members = tuple(m.id for m in the_plan.members)
    if the_plan.is_noop:
        log.info(
            "%s: nothing to adopt; %d member(s) are already in the pool", resolved, len(members)
        )
        return AdoptReport(pool_id=resolved, members=members, noop=True)

    run = _Adoption(
        state=state,
        pool=pool,
        pool_id=resolved,
        plan=the_plan,
        uids=uids,
        run_admin_fn=run_admin_fn,
        control=control,
        secrets=secrets,
        rows=rows,
        sleep=sleep,
        now=now,
    )
    _check_collisions(run)
    _ensure_pool_dirs(run)
    for member in the_plan.members:
        if not member.needs_adopt:
            log.info("%s: already in %s; skipping", member.id, resolved)
            continue
        _stop_member(run, member)
        _move_member_data(run, member)
        _copy_member_secrets(run, member)
        _remove_member_declaration(run, member)
    report = AdoptReport(
        pool_id=resolved,
        members=members,
        moved=tuple(run.moved),
        secrets_copied=tuple(run.secrets_copied),
        declarations_removed=tuple(run.removed),
        stopped=tuple(run.stopped),
        actions=tuple(run.actions),
    )
    log.info("%s", report.summary())
    return report


def _default_uids(state: StateDir) -> _Allocator:
    from ams.platform.sync import uid_allocator

    return uid_allocator(state)


def _optional_uids(state: StateDir) -> _Allocator | None:
    """The allocator when this host has one, else ``None``.

    Only ``plan`` uses this. A plan is a read, and reading a state directory
    copied off the box (no ``/etc/subuid`` entry for this user, no setuid
    helpers) must still print something useful -- it says the directory is
    unreadable rather than dying on the allocator.
    """
    try:
        return _default_uids(state)
    except Exception as e:  # noqa: BLE001 - any host problem means "no admin ns here"
        log.debug(
            "no uid allocator on this host (%s: %s); planning without one", type(e).__name__, e
        )
        return None


def _with_rows(the_plan: PoolPlan, rows: dict[str, Any] | None) -> PoolPlan:
    """Fold the status table this run already fetched into the plan it prints."""
    from dataclasses import replace

    members = tuple(replace(m, running=_is_up(rows, m.id)) for m in the_plan.members)
    running = None if rows is None else tuple(sorted(sid for sid in rows if _is_up(rows, sid)))
    return replace(the_plan, members=members, running=running)


# --------------------------------------------------------------------------- steps


def _check_collisions(run: _Adoption) -> None:
    """Refuse *before* the first move when a name already exists in the pool.

    ``mv`` would happily overwrite, and the file it would overwrite is a
    database. The two directories are compared by name only -- deciding which of
    two ``kvservice.db`` files is the real one is not a decision a tool gets to
    make.
    """
    block = run.pool_block
    for member in run.plan.members:
        if not member.data_readable:
            raise PoolAdoptError(
                f"{member.id}: cannot list {member.legacy_data}, so adoption cannot know what "
                "would move. Check the directory's ownership and the harness's subuid range."
            )
        if not member.needs_adopt:
            continue
        target = _list_dir(
            member.target_data, service_id=run.pool_id, uids=run.uids, run_admin_fn=run.run_admin_fn
        )
        if not target.readable:
            raise PoolAdoptError(
                f"{member.id}: {member.target_data} exists but its contents cannot be listed, "
                "not even from the admin namespace; refusing to move data into a directory "
                "whose contents are unknown"
            )
        # A target that does not exist yet holds nothing and collides with
        # nothing -- `_ensure_pool_dirs` creates it a few lines later. Only a
        # directory that is really there can really clash.
        clash = sorted(set(target.entries) & set(member.data_entries + member.staged_entries))
        if clash:
            raise PoolAdoptError(
                f"{member.id}: {member.target_data} already holds {clash}; adoption will not "
                "overwrite a database. Move or delete the copy in the pool root by hand, then "
                "run adopt again."
            )
    log.debug("%s: no name collisions between the members and the pool root (block %s)",
              run.pool_id, block.uid_start)


def _ensure_pool_dirs(run: _Adoption) -> None:
    """``<pool-root>/data/<member>`` for every member, owned by the pool's uid.

    Non-recursive chown on purpose: the pool root may already hold a staged repo
    and a venv, and ``chown -R`` over those is minutes of io to fix nothing.
    """
    root = run.state.service_root(run.pool_id)
    data = root / DATA_DIRNAME
    targets = [str(data / m.id) for m in run.plan.members]
    block = run.pool_block
    run.run_admin_fn(
        ["mkdir", "-m", oct(DATA_DIR_MODE)[2:], "-p", str(root), *targets], block
    ).check()
    run.run_admin_fn(
        ["chown", f"{INNER_UID}:{INNER_GID}", str(root), str(data), *targets], block
    ).check()
    run.actions.append("pool-data")


def _stop_member(run: _Adoption, member: MemberPlan) -> None:
    """Stop the member, then wait for the supervisor to agree that it is down."""
    if run.control is None or not _is_up(run.rows, member.id):
        return
    from ams.control import ControlError

    try:
        response = run.control.stop(member.id)
    except ControlError as e:  # pragma: no cover - the socket answered a moment ago
        raise PoolAdoptError(
            f"{member.id}: cannot stop it ({e}); refusing to move its data"
        ) from None
    if not response.get("ok"):
        raise PoolAdoptError(
            f"{member.id}: the supervisor refused to stop it "
            f"({response.get('error') or response}); refusing to move its data"
        )
    run.stopped.append(member.id)
    run.actions.append(f"stop:{member.id}")
    end = run.now() + STOP_DEADLINE_S
    while True:
        rows = _control_rows(run.control)
        if not _is_up(rows, member.id):
            run.rows = rows
            return
        if run.now() >= end:
            raise PoolAdoptError(
                f"{member.id} is still running {STOP_DEADLINE_S:.0f}s after 'stop'; "
                "refusing to move the data of a live process"
            )
        run.sleep(STOP_POLL_S)


def _move_member_data(run: _Adoption, member: MemberPlan) -> None:
    """The two-hop move: one ``rename(2)`` per hop, both after the stop.

    Neither hop takes a list of names from the plan. The plan is a forecast made
    against a *running* fleet, and a WAL-mode SQLite service is three files
    while it runs (``x.db``, ``x.db-wal``, ``x.db-shm``) and one file once it has
    shut down cleanly -- so a name list captured before ``_stop_member`` is stale
    by construction, and moving it fails on the two files the stop deleted. That
    is the second failure in `.claude/state/diagnosis-pool-cutover.md`.

    Moving the *directory* removes the question. Each hop is a single rename, so
    it either happened or it did not: there is no half-moved database, and an
    interrupted adoption is resumed by running it again.
    """
    _hop_out_of_the_member_root(run, member)
    _hop_into_the_pool_root(run, member)


def _hop_out_of_the_member_root(run: _Adoption, member: MemberPlan) -> None:
    """``<member-root>/data`` -> ``<staging>/data``, in the member's namespace.

    One ``mv -T``: a rename of the whole directory, whatever is in it at this
    moment. The member's ``data/`` is then recreated empty and handed back to
    the member's uid -- the legacy root is never deleted (§5.8) and a service
    root without its ``AMS_DATA_DIR`` is not a service root.
    """
    staging = staging_dir(run.state, member.id)
    payload = staging / PAYLOAD_DIRNAME
    if _list_dir(
        payload, service_id=member.id, uids=run.uids, run_admin_fn=run.run_admin_fn
    ).exists:
        log.info(
            "%s: an earlier run already parked its data in %s; resuming there", member.id, payload
        )
        return

    # Re-listed here, after the stop, and never taken from the plan.
    live = _list_dir(
        member.legacy_data, service_id=member.id, uids=run.uids, run_admin_fn=run.run_admin_fn
    )
    if not live.readable:
        raise PoolAdoptError(
            f"{member.id}: cannot list {member.legacy_data} now that it is stopped; "
            "refusing to move a directory whose contents are unknown"
        )
    if not live.exists or not live.entries:
        log.info("%s: %s holds nothing; there is no data to adopt", member.id, member.legacy_data)
        return
    if live.entries != member.data_entries:
        # Expected, not alarming: this is what a clean SQLite shutdown looks like.
        log.info(
            "%s: %s changed between the plan and the stop (%s -> %s); moving what is there now",
            member.id,
            member.legacy_data,
            list(member.data_entries),
            list(live.entries),
        )
    try:
        staging.mkdir(parents=True, exist_ok=True)
        os.chmod(staging, STAGING_MODE)
    except OSError as e:
        raise PoolAdoptError(
            f"{member.id}: cannot prepare the staging directory {staging}: {e}"
        ) from None

    block = run.uids.allocate(member.id)
    run.run_admin_fn(["mv", "-T", str(member.legacy_data), str(payload)], block).check()
    # Hand the payload to the harness: it is inner 0 in *both* namespaces, and
    # the pool's namespace cannot even chown a file owned by an unmapped uid.
    run.run_admin_fn(["chown", "-R", "0:0", str(payload)], block).check()
    run.run_admin_fn(
        ["mkdir", "-m", oct(DATA_DIR_MODE)[2:], "-p", str(member.legacy_data)], block
    ).check()
    run.run_admin_fn(
        ["chown", f"{INNER_UID}:{INNER_GID}", str(member.legacy_data)], block
    ).check()
    run.actions.append(f"data-out:{member.id}")
    log.info(
        "%s: %s moved to %s in one rename (%d entr(ies)); an empty data/ left behind",
        member.id,
        member.legacy_data,
        payload,
        len(live.entries),
    )


def _hop_into_the_pool_root(run: _Adoption, member: MemberPlan) -> None:
    """``<staging>/data`` -> ``<pool-root>/data/<member>``, in the pool's namespace.

    ``_ensure_pool_dirs`` pre-created the target empty, and ``mv -T`` onto an
    empty directory is a rename, so the normal path is again one atomic step. A
    target that already holds something (an interrupted earlier adoption, or a
    pool that has already run) cannot be renamed onto, so those are merged entry
    by entry -- never overwriting, and re-checked against what is on disk now
    rather than against the plan.
    """
    staging = staging_dir(run.state, member.id)
    payload = staging / PAYLOAD_DIRNAME
    parked = _list_dir(
        payload, service_id=member.id, uids=run.uids, run_admin_fn=run.run_admin_fn
    )
    if not parked.readable:
        raise PoolAdoptError(
            f"{member.id}: cannot list {payload}; its contents belong to an interrupted "
            "adoption and must be moved into the pool root by hand"
        )
    if parked.exists:
        _land(run, member, payload, parked.entries)

    # A flat staging directory is what the build that failed on racknerd could
    # leave behind. It is drained the same way, so recovering from that build
    # needs nothing but a re-run of this one.
    loose = _list_dir(
        staging, service_id=member.id, uids=run.uids, run_admin_fn=run.run_admin_fn
    )
    names = tuple(n for n in loose.entries if n != PAYLOAD_DIRNAME)
    if loose.exists and names:
        _merge(run, member, staging, names)
    if loose.exists:
        _rmdir(staging)


def _land(run: _Adoption, member: MemberPlan, payload: Path, entries: Sequence[str]) -> None:
    """Put the parked directory in the pool root, whole if it can be."""
    existing = _list_dir(
        member.target_data, service_id=run.pool_id, uids=run.uids, run_admin_fn=run.run_admin_fn
    )
    if not existing.readable:
        raise PoolAdoptError(
            f"{member.id}: {member.target_data} exists but cannot be listed; refusing to move "
            "data into a directory whose contents are unknown"
        )
    if existing.entries:
        _merge(run, member, payload, entries)
        _rmdir(payload)
        return
    if not entries:
        _rmdir(payload)
        return
    run.run_admin_fn(["mv", "-T", str(payload), str(member.target_data)], run.pool_block).check()
    run.run_admin_fn(
        ["chown", "-R", f"{INNER_UID}:{INNER_GID}", str(member.target_data)], run.pool_block
    ).check()
    for name in entries:
        run.moved.append(f"{member.id}:{name}")
        log.info("%s: %s is now %s", member.id, name, member.target_data / name)
    run.actions.append(f"data:{member.id}")


def _merge(run: _Adoption, member: MemberPlan, source: Path, names: Sequence[str]) -> None:
    """Move entries one by one into a target that already holds something.

    ``-n`` plus the overlap check below means a file in the pool root is never
    replaced; anything that could not move stays in the staging directory, which
    is what makes a partial merge resumable rather than lost.
    """
    if not names:
        return
    existing = _list_dir(
        member.target_data, service_id=run.pool_id, uids=run.uids, run_admin_fn=run.run_admin_fn
    )
    clash = sorted(set(existing.entries) & set(names))
    if clash:
        raise PoolAdoptError(
            f"{member.id}: {member.target_data} already holds {clash}; adoption will not "
            "overwrite a database. Move or delete the copy in the pool root by hand, then "
            "run adopt again."
        )
    run.run_admin_fn(
        ["mv", "-n", "-t", str(member.target_data), "--", *[str(source / n) for n in names]],
        run.pool_block,
    ).check()
    run.run_admin_fn(
        ["chown", "-R", f"{INNER_UID}:{INNER_GID}", str(member.target_data)], run.pool_block
    ).check()
    left = _list_dir(source, service_id=member.id, uids=run.uids, run_admin_fn=run.run_admin_fn)
    if left.entries:
        raise PoolAdoptError(
            f"{member.id}: {sorted(left.entries)} could not be moved out of {source}; they are "
            "still there and a second 'pool adopt' will retry them"
        )
    for name in names:
        if f"{member.id}:{name}" not in run.moved:
            run.moved.append(f"{member.id}:{name}")
            log.info("%s: %s is now %s", member.id, name, member.target_data / name)
    run.actions.append(f"data:{member.id}")


def _rmdir(path: Path) -> None:
    try:
        path.rmdir()
    except OSError as e:  # pragma: no cover - a leftover here is not a failure
        log.warning("could not remove the staging directory %s: %s", path, e)


def _copy_member_secrets(run: _Adoption, member: MemberPlan) -> None:
    """``secrets/<member>/NAME`` -> ``secrets/<pool>/NAME__<MANGLED>``.

    Byte-exact rather than ``SecretStore.set``: that method normalizes human
    input (it strips one trailing newline and rejects an empty value), which is
    right for ``ams secret set`` and wrong for a copy -- a value that already
    went through it must not be normalized a second time. The value is read into
    memory and written straight back out; it is never logged, never put in an
    argv, and never returned.
    """
    if not member.secrets:
        return
    directory = run.secrets.service_dir(run.pool_id)
    try:
        for parent in (run.secrets.dir, directory):
            parent.mkdir(parents=True, exist_ok=True)
            if stat.S_IMODE(parent.stat().st_mode) != 0o700:
                os.chmod(parent, 0o700)
    except OSError as e:
        raise PoolAdoptError(f"{run.pool_id}: cannot prepare {directory}: {e}") from None
    for source, target in member.secrets:
        src_path = run.secrets.path(member.id, source)
        dst_path = directory / target
        # unknown=False: an unstattable target is treated as absent, and the
        # O_EXCL create below is what actually decides -- so an existing value
        # is still never overwritten, whichever way the stat went.
        if _exists(dst_path, unknown=False):
            log.info("%s: %s already has a value; leaving it alone", run.pool_id, target)
            continue
        try:
            payload = src_path.read_bytes()
        except OSError as e:
            raise PoolAdoptError(f"{member.id}: cannot read secret {source}: {e}") from None
        tmp = dst_path.with_name(f".{target}.tmp{os.getpid()}")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, SECRET_FILE_MODE)
        except FileExistsError:  # pragma: no cover - a concurrent adoption
            raise PoolAdoptError(
                f"{run.pool_id}: {tmp} already exists; another adoption is running"
            ) from None
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, SECRET_FILE_MODE)  # O_CREAT's mode is subject to the umask
            os.replace(tmp, dst_path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        finally:
            del payload
        run.secrets_copied.append(f"{member.id}:{source} -> {target}")
        log.info("%s: copied secret %s to %s/%s", member.id, source, run.pool_id, target)
    run.actions.append(f"secrets:{member.id}")


def _remove_member_declaration(run: _Adoption, member: MemberPlan) -> None:
    """Unlink ``services/<member>/service.toml``. The root is left alone."""
    if not member.decl_exists:
        return
    path = run.state.service_decl_path(member.id)
    try:
        path.unlink()
    except FileNotFoundError:  # pragma: no cover - it existed a moment ago
        return
    except OSError as e:
        raise PoolAdoptError(f"{member.id}: cannot remove {path}: {e}") from None
    run.removed.append(member.id)
    run.actions.append(f"undeclare:{member.id}")
    log.info("%s: removed the pre-pool declaration %s (the root is left in place)", member.id, path)


# --------------------------------------------------------------------------- cli


def add_subparser(ops: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Add ``pool`` (with ``plan`` and ``adopt``) to ``ams platform``'s verbs."""
    parser = ops.add_parser(
        "pool",
        help="inspect and adopt a pool: move N services' data into one pool root",
        description=(
            "A pool runs N manifests in one process. 'plan' prints what adoption would "
            "move; 'adopt' moves it: each member's data/ into the pool root, its secrets "
            "under <NAME>__<MEMBER>, and its now-redundant service.toml away. Adoption is "
            "idempotent, refuses while the pool is running, and never deletes a legacy root."
        ),
    )
    sub = parser.add_subparsers(dest="pool_command", required=True)
    p_plan = sub.add_parser("plan", help="print what 'pool adopt' would move; writes nothing")
    p_adopt = sub.add_parser("adopt", help="move each member's data, secrets and declaration")
    p_adopt.add_argument(
        "--dry-run", action="store_true", help="print the plan instead of moving anything"
    )
    for p in (p_plan, p_adopt):
        p.add_argument("id", metavar="pool-id", help="the pool name ('core') or id ('pool-core')")
        p.add_argument("--state-dir", type=Path, default=None, help="overrides $AMS_STATE_DIR")
        p.add_argument("--log-level", default="INFO")
    return parser


def cmd_pool(args: argparse.Namespace) -> int:
    """``ams platform pool {plan,adopt}``."""
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    state = StateDir(args.state_dir) if args.state_dir else StateDir.from_env()
    dry_run = args.pool_command == "plan" or bool(getattr(args, "dry_run", False))
    try:
        result = (
            plan(
                state,
                args.id,
                uids=_optional_uids(state),
                run_admin_fn=run_admin,
                control=SocketControl(state),
            )
            if dry_run
            else adopt(state, args.id)
        )
    except (PoolAdoptError, StateCorrupt) as e:
        print(f"ERROR {e}", file=sys.stderr)
        return EXIT_PRECONDITION
    except (RuntimeError, OSError, ValueError) as e:
        # An admin-namespace failure (SpawnError), an exhausted subuid range or
        # an unwritable state dir. One line for the operator, not a traceback --
        # and never a partial success reported as one: adoption is idempotent,
        # so the fix is to repair the host and run it again.
        print(f"ERROR {type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_ERROR
    if isinstance(result, PoolPlan):
        for line in result.lines():
            print(line)
        return EXIT_OK
    print(result.summary())
    for action in result.actions:
        print(f"  {action}")
    return result.exit_code


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - thin wrapper
    """``python -m ams.platform.pool {plan,adopt} <pool-id>``."""
    parser = argparse.ArgumentParser(prog="ams platform pool")
    sub = parser.add_subparsers(dest="command", required=True)
    add_subparser(sub)
    args = parser.parse_args(list(argv) if argv is not None else None)
    return cmd_pool(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
