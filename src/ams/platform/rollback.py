"""Roll one service back to an earlier commit. The agent's half of Q7.

``ams platform sync`` (T3.1) is mechanical: it drives every service to the head
of the branch and escalates whatever it cannot decide. *Whether a crash-looping
service is rolled back or left failing loudly* is explicitly on the agent's side
of PLAN-allin's mechanical/policy line, and T3.3's platform policy therefore
**recommends** a rollback and never performs one (D24). This module is the
operation that recommendation names -- invoked by an agent or an operator, never
by a timer.

One service, one commit::

    ams platform rollback <id> [--to SHA]

The target is ``--to`` or the record's ``prev_sha`` (the last commit that
reached ``healthy``). Everything a deploy of that commit would have done is
redone at it: the tree is re-staged, the runtime re-provisioned, the manifest
**re-read from the manifest as it was at that commit** and re-translated, the
declaration and both sidecars rewritten, the service restarted and then gated on
its own health probe.

What a rollback does NOT touch, and why:

- ``<root>/data`` -- **never**. A rollback restores code; it does not undo a
  migration the newer code ran. Reversing data is a restore from the T2.4
  backup, which is a different operation with a different blast radius.
- **Secrets** -- never read for their value, never written, never printed.
  ``SVC_SECRET`` is a property of the service identity, not of a commit.
- **The registry** -- no identity creation, no ACL upsert. The identity is
  sha-independent, and the ACL is re-upserted from the (now rolled-back)
  ``registry.json`` by the next sync tick. Nothing here calls an admin endpoint,
  so nothing here needs the admin token.
- **The gateway** -- ``mount.json`` is rewritten, the Caddy config is not
  re-rendered. The gateway is *fleet* state assembled from every mount sidecar
  (D24) and re-rendering it restarts Caddy for all twenty routes; the next sync
  tick renders it from the sidecar this rollback just wrote. When the mount
  actually changed between the two commits the report says so.
- **Other services** -- one id in, one id out. No fleet reload of anything else.

**The sync timer will undo this.** ``sync`` drives every affected service to the
head of ``--ref``, and a commit range that reaches from the rolled-back sha to
the head necessarily touches this service (that is why it was rolled back), so
the next tick redeploys it. A rollback buys the time to revert the commit
upstream or to stop ``ams-platform-sync.timer``; it is not a pin. Both the
report and the escalation say this out loud.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any, Protocol

from ams.platform.bootstrap import BootstrapError
from ams.platform.bootstrap import place_jwt_key as place_jwt_key  # noqa: PLC0414 - patch point
from ams.platform.pool import DOWN_STATUSES as DOWN_STATUSES  # noqa: PLC0414 - re-export
from ams.platform.sources import SourceError, SourceMirror
from ams.platform.static import StaticError, load_ams_overlay
from ams.platform.sync import (
    EXIT_ERROR,
    EXIT_OK,
    FAILED,
    POOL_ID_PREFIX,
    POOL_JSON_NAME,
    POOL_RUNNER_NAME,
    STATE_VERSION,
    SVC_SECRET_NAME,
    SyncConfig,
    SyncError,
    discover_manifests,
    mounts_dir,
    pool_id_for,
    pool_runner_source,
    pool_shadow_dir,
    registry_dir,
    state_path,
    uid_allocator,
)
from ams.platform.sync import _write_if_changed as write_if_changed
from ams.platform.sync import _write_json_if_changed as write_json_if_changed
from ams.platform.sync import make_pool_data_dirs as make_pool_data_dirs  # noqa: PLC0414 - patched
from ams.platform.sync import place_pool_file as place_pool_file  # noqa: PLC0414 - patch point
from ams.platform.translate import (
    PoolTranslation,
    TranslateError,
    Translation,
    build_pool,
    emit_toml,
    pool_member,
    pool_port_name,
    translate,
)
from ams.runtime import RuntimeStore, python_venv_dir
from ams.runtime import provision as provision  # noqa: PLC0414 - patch point
from ams.schema import DeclError, ServiceDecl
from ams.secrets import SecretStore, store_for
from ams.state import StateCorrupt, StateDir, read_json_checked, write_json_atomic
from ams.uidmap import UidBlock

log = logging.getLogger("ams.platform.rollback")

ESCALATION_KIND = "PlatformRollback"
HEALTHY = "healthy"

#: Precondition failure (nothing was touched). Distinct from a rollback that ran
#: and left the service unhealthy, which is ``EXIT_ERROR``.
EXIT_PRECONDITION = 2

#: How long to wait for the harness to report the service actually stopped,
#: over and above the declaration's own ``stop.timeout_s``.
STOP_SLACK_S = 10.0
STOP_POLL_S = 0.25

#: ``DOWN_STATUSES`` -- statuses that mean "no process is running under this
#: id", which is all the staging step needs to know -- is imported from
#: :mod:`ams.platform.pool` above and re-exported here, where callers and tests
#: have always read it. ``waiting`` is in the set on purpose: it means the
#: supervisor is holding the service back because a ``depends_on`` dependency is
#: not healthy yet, so there is nothing running and nothing to wait for.


class RollbackError(RuntimeError):
    """A precondition is wrong. Raised before anything on the host is touched."""


class _Allocator(Protocol):
    def allocate(self, service_id: str) -> UidBlock: ...


# --------------------------------------------------------------------------- seams
#
# The four calls that reach the running harness, module-level so a portable test
# can replace them. ``place_jwt_key``, ``provision`` and ``SourceMirror.stage``
# are the three that fork into a user namespace and are patched the same way.


def ctl_status(state: StateDir, *, timeout_s: float = 30.0) -> dict[str, Any]:
    from ams.control import control_socket_path
    from ams.control import request as control_request

    return control_request(control_socket_path(state), "status", timeout_s=timeout_s)


def ctl_stop(state: StateDir, service_id: str, *, timeout_s: float = 60.0) -> dict[str, Any]:
    from ams.control import control_socket_path
    from ams.control import request as control_request

    return control_request(control_socket_path(state), "stop", service_id, timeout_s=timeout_s)


def ctl_reload(state: StateDir, *, timeout_s: float = 60.0) -> dict[str, Any]:
    from ams.control import control_socket_path
    from ams.control import request as control_request

    return control_request(control_socket_path(state), "reload", timeout_s=timeout_s)


def ctl_restart(state: StateDir, service_id: str, *, timeout_s: float = 60.0) -> dict[str, Any]:
    from ams.control import control_socket_path
    from ams.control import request as control_request

    return control_request(control_socket_path(state), "restart", service_id, timeout_s=timeout_s)


# --------------------------------------------------------------------------- report


@dataclass(frozen=True)
class RollbackReport:
    """What one rollback did. ``stage`` is ``healthy`` or ``failed``."""

    service_id: str
    to_sha: str
    from_sha: str | None = None
    stage: str = FAILED
    error: str | None = None
    actions: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    escalation: Mapping[str, Any] | None = None
    state_written: bool = False

    @property
    def ok(self) -> bool:
        return self.stage == HEALTHY and self.error is None

    @property
    def exit_code(self) -> int:
        return EXIT_OK if self.ok else EXIT_ERROR

    def summary(self) -> str:
        return (
            f"{self.service_id}: {(self.from_sha or '-')[:12]} -> {self.to_sha[:12]} "
            f"stage={self.stage} actions={','.join(self.actions) or '-'}"
        )


# --------------------------------------------------------------------------- helpers


def _stamp(now_s: float) -> str:
    return datetime.fromtimestamp(now_s, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _emit(stream: IO[str], record: Mapping[str, Any]) -> None:
    stream.write(json.dumps(record, default=str) + "\n")
    stream.flush()


def load_state_doc(path: Path) -> dict[str, Any]:
    """Read ``platform-state.json`` as a raw document, version-checked.

    Deliberately *not* :class:`ams.platform.sync.PlatformState`: that loader
    keeps only the keys its dataclass declares, so round-tripping the file
    through it would silently drop ``rolled_back_from`` (and any other field a
    later version adds) from every service it did not touch. A rollback rewrites
    one record inside a document it otherwise passes through untouched.
    """
    if not path.exists():
        raise RollbackError(f"{path} does not exist; nothing has been synced yet")
    try:
        data = read_json_checked(path)
    except StateCorrupt as e:
        raise RollbackError(str(e)) from None
    if not isinstance(data, dict):
        raise RollbackError(f"{path}: expected a JSON object at top level")
    if data.get("version") != STATE_VERSION:
        raise RollbackError(
            f"{path}: unsupported version {data.get('version')!r}; expected {STATE_VERSION}"
        )
    services = data.get("services")
    if not isinstance(services, dict):
        raise RollbackError(f"{path}: 'services' must be an object")
    return data


def _find_translation(
    checkout: Path, service_id: str, cfg: SyncConfig, sha: str, services_dir: Path
) -> tuple[Path, Translation, ServiceDecl]:
    """The manifest for ``service_id`` **as it was at ``sha``**, translated there.

    Every manifest in the checkout is translated because the ams service id is
    the manifest's ``name`` field, not its directory name, and nothing in the
    platform assumes those agree. Translation is pure and sub-millisecond; a
    manifest that does not parse is skipped rather than raised on, so one broken
    sibling cannot block a rollback of a healthy service.
    """
    from dataclasses import replace as _replace

    for manifest in discover_manifests(checkout, cfg.manifest_globs):
        try:
            overlay = load_ams_overlay(manifest.parent)
            ctx = cfg.context_for(
                sha=sha, services_dir=services_dir, extra_secret_names=overlay.secrets
            )
            translation = translate(manifest.read_text(encoding="utf-8"), ctx)
        except (TranslateError, StaticError, DeclError, OSError, UnicodeDecodeError) as e:
            log.debug("%s does not translate at %s (%s); skipping", manifest, sha[:12], e)
            continue
        if translation.id != service_id:
            continue
        if translation.kind != "service" or translation.decl is None:
            raise RollbackError(
                f"{service_id} is kind={translation.kind!r} at {sha[:12]}: it has no process "
                "and no declaration, so there is nothing to restart. Republish it with "
                "'ams platform sync'."
            )
        decl = translation.decl
        if overlay.env:
            # `dataclasses.replace` re-runs ServiceDecl.__post_init__, so a name
            # that collides with a declared secret raises DeclError here rather
            # than at spawn. T3.4 parses the table and leaves the merge to its
            # caller; sync.py is the other caller.
            decl = _replace(decl, env={**decl.env, **overlay.env})
        return manifest, translation, decl
    raise RollbackError(
        f"no manifest declaring service {service_id!r} exists at {sha[:12]}; "
        "that commit predates the service, or renamed it"
    )


def _manifest_rel(checkout: Path, manifest: Path) -> str:
    try:
        return manifest.parent.relative_to(checkout).as_posix()
    except ValueError:  # pragma: no cover - the manifest came from the checkout
        return manifest.parent.name


def _find_pool_translation(
    checkout: Path, pool: str, cfg: SyncConfig, sha: str, services_dir: Path
) -> tuple[PoolTranslation, dict[str, Translation]]:
    """Rebuild pool ``<pool>`` from its members' manifests **as they were at ``sha``**.

    There is no manifest for a pool: it is N manifests that each carry
    ``pool = "<name>"`` in their overlay, and the declaration exists only after
    ``build_pool`` has seen all of them together. So a pool rollback re-derives
    the group at the target commit exactly the way a sync tick derives it at the
    head -- per-member ``translate`` with ``TranslateContext.pool`` set, then one
    ``build_pool`` over the union of the members' extra secret names.

    All or nothing: a member that does not translate raises, because half a pool
    at one commit and half at another is not a declaration that can be built.
    """
    entries: list[tuple[Path, Any, Translation]] = []
    for manifest in discover_manifests(checkout, cfg.manifest_globs):
        try:
            overlay = load_ams_overlay(manifest.parent)
        except (StaticError, OSError, UnicodeDecodeError) as e:
            log.debug("%s: unreadable overlay at %s (%s); skipping", manifest, sha[:12], e)
            continue
        if overlay.pool != pool:
            continue
        try:
            ctx = cfg.context_for(
                sha=sha, services_dir=services_dir, extra_secret_names=overlay.secrets, pool=pool
            )
            translation = translate(manifest.read_text(encoding="utf-8"), ctx)
        except (TranslateError, DeclError, OSError, UnicodeDecodeError) as e:
            raise RollbackError(
                f"pool {pool!r}: member manifest {manifest.parent.name} does not translate at "
                f"{sha[:12]} ({type(e).__name__}: {e}); a pool is rolled back whole, so this "
                "commit is not a target the pool can reach"
            ) from None
        entries.append((manifest, overlay, translation))
    if not entries:
        raise RollbackError(
            f'no manifest carries pool = "{pool}" at {sha[:12]}; that commit predates the '
            "pool, or the overlay lines were added after it"
        )

    extra = sorted({name for _m, overlay, _t in entries for name in overlay.secrets})
    ctx = cfg.context_for(
        sha=sha, services_dir=services_dir, extra_secret_names=tuple(extra), pool=pool
    )
    try:
        built = build_pool(
            pool,
            [pool_member(t, _manifest_rel(checkout, m)) for m, _o, t in entries],
            ctx,
        )
    except (TranslateError, DeclError) as e:
        raise RollbackError(
            f"pool {pool!r} cannot be built at {sha[:12]}: {type(e).__name__}: {e}"
        ) from None
    return built, {t.id: t for _m, _o, t in entries}


def _wait_stopped(
    state: StateDir,
    service_id: str,
    *,
    deadline_s: float,
    now: Callable[[], float],
    sleep: Callable[[float], None],
    ctl_timeout_s: float,
) -> str:
    """Poll ``ctl status`` until the service is really down. Returns its status.

    ``ctl stop`` only *sends* the stop signal; the tree must not be swapped
    while the old process can still be exec'ing out of it. D24 (T3.2) recorded
    the live failure this prevents: a service restarted inside the staging
    window died with ``ModuleNotFoundError`` because its venv had moved.
    """
    end = now() + deadline_s
    status = "unknown"
    while True:
        response = ctl_status(state, timeout_s=ctl_timeout_s)
        row = (response.get("services") or {}).get(service_id) or {}
        status = str(row.get("status") or "unknown")
        if status in DOWN_STATUSES and not row.get("pid"):
            return status
        if now() >= end:
            log.warning(
                "%s is still %r after %.0fs; staging anyway", service_id, status, deadline_s
            )
            return status
        sleep(STOP_POLL_S)


# --------------------------------------------------------------------------- the run


@dataclass
class _Run:
    """Mutable bookkeeping for one rollback, so each step stays a small function."""

    state: StateDir
    store: RuntimeStore
    cfg: SyncConfig
    service_id: str
    to_sha: str
    from_sha: str | None
    record: dict[str, Any]
    document: dict[str, Any]
    secrets: SecretStore
    uids: _Allocator
    mirror: SourceMirror
    checkout: Path
    translation: Translation
    decl: ServiceDecl
    now: Callable[[], float]
    sleep: Callable[[float], None]
    stream: IO[str]
    actions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: Set only when ``service_id`` is a pool: the rebuilt group, and each
    #: member's own translation (its mount route and registry identity, which
    #: pooling does not change).
    pool: PoolTranslation | None = None
    member_translations: dict[str, Translation] = field(default_factory=dict)

    @property
    def is_pool(self) -> bool:
        return self.pool is not None

    @property
    def members(self) -> tuple[str, ...]:
        return tuple(m.id for m in self.pool.members) if self.pool is not None else ()

    def note(self, action: str) -> None:
        self.actions.append(action)

    def warn(self, text: str) -> None:
        log.warning("%s: %s", self.service_id, text)
        self.warnings.append(text)


def rollback(
    state: StateDir,
    store: RuntimeStore,
    service_id: str,
    *,
    cfg: SyncConfig,
    to_sha: str | None = None,
    secrets: SecretStore | None = None,
    uids: _Allocator | None = None,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    stream: IO[str] | None = None,
) -> RollbackReport:
    """Re-deploy ``service_id`` at ``to_sha`` (default: its ``prev_sha``).

    ``cfg`` is the **same** :class:`~ams.platform.sync.SyncConfig` the sync loop
    runs with. It is required rather than defaulted because every knob in it
    (the memory floor, the cpu cap, the loopback registry and auth URLs) feeds
    the ``TranslateContext``, and a rollback translated under different caps
    would write a declaration the next sync tick immediately rewrites -- an
    invisible second restart of a service that was just brought back.

    Raises :class:`RollbackError` for every precondition -- no record, no target
    commit, no manifest at that commit -- **before** the service is touched. Once
    the service has been stopped, a failure is reported in the returned
    :class:`RollbackReport` and recorded on the state record instead, because by
    then walking away silently would leave the fleet mid-operation.
    """
    stream = stream if stream is not None else sys.stdout
    secrets = secrets if secrets is not None else store_for(state)

    path = state_path(state)
    document = load_state_doc(path)
    record = document["services"].get(service_id)
    if not isinstance(record, dict):
        known = ", ".join(sorted(document["services"])) or "(none)"
        raise RollbackError(f"no platform record for {service_id!r}; known services: {known}")

    # A pooled member has no tree, no venv and no declaration of its own: the
    # pool's process serves it, and rolling "just this member" back would mean
    # deploying two commits in one address space. The pool is the unit (§5.9.1).
    member_pool = record.get("pool")
    if member_pool and not service_id.startswith(POOL_ID_PREFIX):
        pool_id = pool_id_for(str(member_pool))
        raise RollbackError(
            f"{service_id} runs inside pool {pool_id!r}: its members share one process, one "
            f"tree and one venv, so the pool is the rollback unit. Roll the whole pool back "
            f"with 'ams platform rollback {pool_id}' -- and if only this member's commit is "
            "bad, revert it upstream instead."
        )

    from_sha = record.get("sha")
    target = to_sha or record.get("prev_sha")
    if not target:
        raise RollbackError(
            f"{service_id} has no prev_sha to roll back to (it has never reached 'healthy' "
            "at more than one commit). Pass --to <sha> to name the commit explicitly."
        )
    target = _check_target(target)
    if target == from_sha:
        raise RollbackError(
            f"{service_id} is already being driven to {target[:12]}; a rollback to the "
            "same commit would do nothing. Pass --to <sha> to name an earlier one."
        )

    mirror = SourceMirror(store.root, cfg.repo_name, url=cfg.repo_url)
    try:
        # `stage` materializes on its own, but the manifest has to be read from
        # the checkout *before* anything is staged, so it happens here. This
        # writes only into the store's own sha-keyed cache, never the state dir:
        # a precondition failure below still leaves the host untouched.
        checkout = mirror.materialize(target)
    except (SourceError, OSError) as e:
        raise RollbackError(
            f"cannot materialize {target[:12]} from {mirror.mirror_dir}: {type(e).__name__}: {e}"
        ) from None

    pool_translation: PoolTranslation | None = None
    member_translations: dict[str, Translation] = {}
    if service_id.startswith(POOL_ID_PREFIX):
        pool_name = str(member_pool or service_id[len(POOL_ID_PREFIX) :])
        pool_translation, member_translations = _find_pool_translation(
            checkout, pool_name, cfg, target, state.services_dir
        )
        decl = pool_translation.decl
        translation = Translation(
            id=service_id, kind="service", decl=decl, mount={}, registry=None
        )
    else:
        _manifest, translation, decl = _find_translation(
            checkout, service_id, cfg, target, state.services_dir
        )

    run = _Run(
        state=state,
        store=store,
        cfg=cfg,
        service_id=service_id,
        to_sha=target,
        from_sha=from_sha,
        record=record,
        document=document,
        secrets=secrets,
        uids=uids if uids is not None else uid_allocator(state),
        mirror=mirror,
        checkout=checkout,
        translation=translation,
        decl=decl,
        now=now,
        sleep=sleep,
        stream=stream,
        pool=pool_translation,
        member_translations=member_translations,
    )
    if run.is_pool:
        log.info("%s has %d member(s): %s", service_id, len(run.members), ", ".join(run.members))
    log.info("rolling %s back from %s to %s", service_id, (from_sha or "-")[:12], target[:12])
    error = _perform(run)
    return _finish(run, path, error)


def _check_target(sha: Any) -> str:
    from ams.platform.sources import _check_sha

    if not isinstance(sha, str):
        raise RollbackError(f"target commit must be a string, got {sha!r}")
    try:
        return _check_sha(sha)
    except SourceError as e:
        raise RollbackError(str(e)) from None


def _perform(run: _Run) -> str | None:
    """Do the rollback. Returns ``"<transition>: <message>"`` or ``None``."""
    for step in (_step_stop, _step_stage, _step_declare, _step_reload, _step_health):
        error = step(run)
        if error is not None:
            return error
    return None


def _step_stop(run: _Run) -> str | None:
    """Stop the service before its tree moves out from under it."""
    try:
        response = ctl_stop(run.state, run.service_id, timeout_s=run.cfg.ctl_timeout_s)
        if not response.get("ok"):
            return f"stop: refused: {response.get('error') or response}"
        run.note("stop")
        status = _wait_stopped(
            run.state,
            run.service_id,
            deadline_s=run.decl.stop.timeout_s + STOP_SLACK_S,
            now=run.now,
            sleep=run.sleep,
            ctl_timeout_s=run.cfg.ctl_timeout_s,
        )
        if status not in DOWN_STATUSES:
            run.warn(f"still {status!r} when the tree was re-staged")
    except Exception as e:  # noqa: BLE001 - ControlError is behind a lazy import
        return f"stop: {type(e).__name__}: {e}"
    return None


def _step_stage(run: _Run) -> str | None:
    """Re-stage the target tree, re-place the JWT key, re-provision if needed."""
    root = run.state.service_root(run.service_id)
    try:
        block = run.uids.allocate(run.service_id)
    except (RuntimeError, OSError, ValueError) as e:
        return f"stage: uid block: {type(e).__name__}: {e}"

    from ams.platform.sources import SHA_MARKER

    marker = root / "repo" / SHA_MARKER
    try:
        at_sha = marker.read_text(encoding="utf-8").strip() == run.to_sha
    except OSError:
        at_sha = False

    try:
        run.mirror.stage(run.to_sha, root, block)
        if not at_sha:
            run.note("stage")
        place_jwt_key(run.state, run.service_id, block, store=run.store)
    except (SourceError, BootstrapError, OSError, RuntimeError) as e:
        return f"stage: {type(e).__name__}: {e}"

    venv = python_venv_dir(run.decl, root)
    if at_sha and venv.is_dir():
        log.info(
            "%s: already at %s with a venv; not re-provisioning",
            run.service_id,
            run.to_sha[:12],
        )
        return None
    try:
        provision(
            run.decl,
            root,
            run.store,
            block,
            log_path=run.state.logs_dir / f"{run.service_id}-rollback.log",
        )
    except Exception as e:  # noqa: BLE001 - any provisioning failure is this service's
        return f"provision: {type(e).__name__}: {e}"
    run.note("provision")
    return None


def _pool_files(run: _Run) -> str | None:
    """A pool's extra on-disk state: member sidecars, ``pool.json``, the runner.

    Order mirrors ``sync._declare_pool`` exactly -- sidecars, then the two files
    inside the pool root, then (back in the caller) ``service.toml``, whose
    arrival is what makes the reload start the process. What is deliberately
    *not* here: the members' secrets (a rollback never writes a secret, D16) and
    the adoption guard (adoption is an operator step and it has already run, or
    the pool would never have been declared in the first place).
    """
    assert run.pool is not None
    try:
        block = run.uids.allocate(run.service_id)
    except (RuntimeError, OSError, ValueError) as e:
        return f"declare: uid block: {type(e).__name__}: {e}"

    for member_id in run.members:
        translation = run.member_translations.get(member_id)
        if translation is None:  # pragma: no cover - built from the same entries
            continue
        try:
            if translation.mount and write_json_if_changed(
                mounts_dir(run.state) / f"{member_id}.json", translation.mount
            ):
                run.note(f"mount:{member_id}")
            if translation.registry is not None and write_json_if_changed(
                registry_dir(run.state) / f"{member_id}.json", translation.registry
            ):
                run.note(f"registry-sidecar:{member_id}")
            # A member that was standalone at the *target* commit would have had
            # its own declaration then; leaving it on disk beside the pool's
            # would start a second process serving the same identity.
            decl_path = run.state.service_decl_path(member_id)
            if decl_path.exists():
                decl_path.unlink()
                run.note(f"unpool-decl:{member_id}")
        except OSError as e:
            return f"declare: {member_id}: {type(e).__name__}: {e}"

    root = run.state.service_root(run.service_id)
    document = json.dumps(dict(run.pool.pool_json), indent=2, sort_keys=True) + "\n"
    try:
        _place_pool_root_file(run, POOL_JSON_NAME, document.encode("utf-8"), block)
        _place_pool_root_file(run, POOL_RUNNER_NAME, pool_runner_source().read_bytes(), block)
        make_pool_data_dirs(root, run.members, block)
    except (OSError, RuntimeError) as e:
        return f"declare: pool files: {type(e).__name__}: {e}"
    run.note("pool-files")
    return None


def _place_pool_root_file(run: _Run, name: str, content: bytes, block: UidBlock) -> None:
    """Push one harness-authored file into the pool root and shadow it.

    Unconditional, unlike the sync tick's version: a rollback runs once, by
    hand, and the file it is replacing is the one from the commit being rolled
    away from. The harness-owned shadow copy is what the *next* sync tick
    compares against, so writing the file without updating it would make that
    tick push the same bytes again.
    """
    place_pool_file(run.state.service_root(run.service_id), name, content, block)
    shadow = pool_shadow_dir(run.state) / run.service_id / name
    shadow.parent.mkdir(parents=True, exist_ok=True)
    tmp = shadow.with_name(f".{name}.tmp{os.getpid()}")
    tmp.write_bytes(content)
    os.replace(tmp, shadow)


def _step_declare(run: _Run) -> str | None:
    """Rewrite both sidecars and the declaration at the target commit."""
    if run.is_pool:
        error = _pool_files(run)
        if error is not None:
            return error
    try:
        if run.translation.mount and write_json_if_changed(
            mounts_dir(run.state) / f"{run.service_id}.json", run.translation.mount
        ):
            run.note("mount")
            run.warn(
                "the gateway mount changed between these commits; run 'ams platform sync' "
                "(or wait one tick) to re-render the Caddy config"
            )
        if run.translation.registry is not None and write_json_if_changed(
            registry_dir(run.state) / f"{run.service_id}.json", run.translation.registry
        ):
            run.note("registry-sidecar")
    except OSError as e:
        return f"declare: sidecar: {type(e).__name__}: {e}"

    # Never generated here: a rollback restores code, and SVC_SECRET belongs to
    # the identity, not to a commit. An unset secret is reported, not fixed --
    # the value is the operator's to supply (D16) and never enters this process.
    try:
        missing = run.secrets.missing(run.service_id, run.decl.secrets)
    except OSError as e:  # pragma: no cover - an unreadable store is not this step's failure
        log.warning("%s: cannot check declared secrets: %s", run.service_id, e)
        missing = []
    if SVC_SECRET_NAME in missing:
        return (
            f"declare: {SVC_SECRET_NAME} has no stored value; the service cannot start. "
            "Run 'ams platform sync' once to generate it."
        )
    if missing:
        run.warn(f"declared secret(s) with no value: {', '.join(missing)}")

    try:
        if write_if_changed(run.state.service_decl_path(run.service_id), emit_toml(run.decl)):
            run.note("declare")
    except OSError as e:
        return f"declare: service.toml: {type(e).__name__}: {e}"
    return None


def _step_reload(run: _Run) -> str | None:
    """Reload so the harness re-reads the declaration, then start the service.

    Both calls are needed and neither is redundant. ``reload`` is what swaps the
    new declaration into the running loop -- ``restart`` alone would re-spawn the
    *old* one, still held in memory, at the newly-staged tree. And ``reload``
    deliberately leaves a service alone when an operator stopped it (D17), which
    ``_step_stop`` just did, so the restart is what brings it back up.
    """
    try:
        response = ctl_reload(run.state, timeout_s=run.cfg.ctl_timeout_s)
        if not response.get("ok"):
            return f"reload: refused: {response.get('error') or response}"
        errors = (response.get("reload") or {}).get("errors") or {}
        if run.service_id in errors:
            return f"reload: {errors[run.service_id]}"
        run.note("reload")
    except Exception as e:  # noqa: BLE001 - ControlError is behind a lazy import
        return f"reload: {type(e).__name__}: {e}"

    try:
        response = ctl_restart(run.state, run.service_id, timeout_s=run.cfg.ctl_timeout_s)
        if not response.get("ok"):
            return f"restart: refused: {response.get('error') or response}"
        run.note("restart")
    except Exception as e:  # noqa: BLE001 - ControlError is behind a lazy import
        return f"restart: {type(e).__name__}: {e}"
    return None


def wait_healthy(
    url: str,
    deadline_s: float,
    interval_s: float = 1.0,
    *,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """GET ``url`` until it answers 200, or the deadline passes.

    Deliberately *not* :meth:`RegistryClient.wait_healthy`, even though the two
    do the same GET. That method belongs to an object that cannot be built
    without an admin token, and a rollback holds no token and calls no admin
    endpoint -- borrowing the client would mean inventing a placeholder
    credential to satisfy a constructor, which is exactly the kind of stub that
    later gets mistaken for a real one. Probing a service's own port is not a
    registry operation.

    A transport error is "not up yet", not a failure: the process may still be
    starting.
    """
    started = now()
    while True:
        try:
            with urllib.request.urlopen(url, timeout=5.0) as resp:  # noqa: S310 - loopback http
                status: int | None = resp.status
        except urllib.error.HTTPError as e:
            status = e.code
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            status = None
            log.info("health: GET %s -> not up yet: %s", url, e)
        if status == 200:
            log.info("health: GET %s -> 200", url)
            return True
        if now() - started >= deadline_s:
            return False
        sleep(interval_s)


def _member_health_path(run: _Run, member_id: str) -> str:
    """The member's own probe path: its registry sidecar, then its translation.

    The sidecar on disk is preferred because that is what the sync loop's own
    gate reads, and a rollback that gated on something else could call a pool
    healthy that the next tick calls failed.
    """
    path = registry_dir(run.state) / f"{member_id}.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(document, dict) and document.get("health_path"):
            return str(document["health_path"])
    except (OSError, json.JSONDecodeError):
        log.debug("%s: no usable registry sidecar at %s", member_id, path)
    translation = run.member_translations.get(member_id)
    registry = (translation.registry if translation is not None else None) or {}
    return str(registry.get("health_path", "/health"))


def _step_pool_health(run: _Run) -> str | None:
    """Gate on **every member's** probe, on its own port.

    One process, N members: the pool's admin probe answering says the runner is
    up, not that each member's app was built and mounted. PLAN-pool §4.5 lets a
    member fail to build without taking the process down, so a pool that gated
    only on the runner would report a rollback healthy while one of its services
    was serving nothing.
    """
    from ams.ports import PortAllocator

    ports = PortAllocator(run.state.ports_state).get(run.service_id)
    for member_id in run.members:
        name = pool_port_name(member_id)
        port = ports.get(name)
        if port is None:
            return f"health: {run.service_id} has no allocated port named {name!r} for {member_id}"
        url = f"http://127.0.0.1:{port}{_member_health_path(run, member_id)}"
        if not wait_healthy(url, run.cfg.health_deadline_s, run.cfg.health_interval_s):
            return f"health: {member_id}: {url} not 200 within {run.cfg.health_deadline_s:.0f}s"
        run.note(f"health:{member_id}")
    return None


def _step_health(run: _Run) -> str | None:
    """Gate on the service's own probe, exactly as the sync loop does."""
    from ams.ports import PortAllocator

    if run.is_pool:
        return _step_pool_health(run)

    port = PortAllocator(run.state.ports_state).get(run.service_id).get("main")
    if port is None:
        return "health: no allocated port named 'main'"
    health_path = str((run.translation.registry or {}).get("health_path", "/health"))
    url = f"http://127.0.0.1:{port}{health_path}"
    if wait_healthy(url, run.cfg.health_deadline_s, run.cfg.health_interval_s):
        run.note("health")
        return None
    return f"health: {url} not 200 within {run.cfg.health_deadline_s:.0f}s"


# --------------------------------------------------------------------------- finish


def _finish(run: _Run, path: Path, error: str | None) -> RollbackReport:
    """Write the record, emit one escalation, and build the report."""
    stage = FAILED if error else HEALTHY
    stamp = _stamp(run.now())
    record = run.record
    record["sha"] = run.to_sha
    record["deployed_sha"] = run.to_sha
    record["stage"] = stage
    record["error"] = error
    record["updated_at"] = stamp
    record["stage_since"] = stamp
    # A new cause: the sync loop's per-transition dedupe must be allowed to
    # speak again about whatever happens next at this commit.
    record["escalated"] = False
    # The commit this service was moved *off*. `sync.ServiceRecord` does not
    # declare the field yet, so it survives only until the next tick rewrites
    # the file -- see the wiring note in the module's task report.
    record["rolled_back_from"] = run.from_sha
    for member_id in run.members:
        # A member row that still claimed the newer commit would be a lie the
        # whole platform reads: the member's code lives in the pool's tree and
        # that tree has just moved. Only the sha-ish fields are touched -- the
        # member's stage follows the pool's, and the next tick re-derives both.
        member_record = run.document["services"].get(member_id)
        if not isinstance(member_record, dict):
            continue
        member_record["sha"] = run.to_sha
        member_record["deployed_sha"] = run.to_sha
        member_record["stage"] = stage
        member_record["error"] = error
        member_record["updated_at"] = stamp
        member_record["stage_since"] = stamp
        member_record["escalated"] = False
        member_record["rolled_back_from"] = run.from_sha
        if stage == HEALTHY:
            member_record["prev_sha"] = None

    if stage == HEALTHY:
        # `prev_sha` means "the last commit that reached healthy", and after a
        # successful rollback that commit is the one now in `sha`. Leaving the
        # old value would make a second rollback target the commit we are
        # already on. The next sync tick refills it when it moves the service on.
        record["prev_sha"] = None

    run.warn(
        f"the next 'ams platform sync' tick will drive {run.service_id} back to "
        f"{(run.from_sha or 'the branch head')[:12]}; revert the commit upstream or stop "
        "ams-platform-sync.timer to make this stick"
    )

    state_written = True
    try:
        write_json_atomic(path, run.document)
    except OSError as e:  # pragma: no cover - the state dir was writable a moment ago
        state_written = False
        log.error("could not write %s: %s", path, e)
        run.warn(f"state file not updated: {e}")

    reason = error or (
        f"rolled {run.service_id} back from {(run.from_sha or '-')[:12]} to {run.to_sha[:12]}"
    )
    escalation: dict[str, Any] = {
        "kind": ESCALATION_KIND,
        "service_id": run.service_id,
        "action": "escalate" if error else "log",
        "reason": reason,
        "event": {
            "service_id": run.service_id,
            "stage": stage,
            "error": error,
            "sha": run.to_sha,
            "prev_sha": record.get("prev_sha"),
            "rolled_back_from": run.from_sha,
            "actions": list(run.actions),
            "warnings": list(run.warnings),
        },
    }
    _emit(run.stream, escalation)
    (log.error if error else log.info)("rollback %s: %s", stage, reason)

    return RollbackReport(
        service_id=run.service_id,
        to_sha=run.to_sha,
        from_sha=run.from_sha,
        stage=stage,
        error=error,
        actions=tuple(run.actions),
        warnings=tuple(run.warnings),
        escalation=escalation,
        state_written=state_written,
    )


# --------------------------------------------------------------------------- cli


def _add_arguments(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """The verb's flags, shared by the ``ams platform`` subparser and ``main``."""
    p.add_argument("id", metavar="service-id", help="the ams service id to roll back")
    p.add_argument(
        "--to",
        metavar="SHA",
        default=None,
        help="commit to roll back to (default: the record's prev_sha)",
    )
    p.add_argument("--repo", default=None, metavar="URL", help="source repository")
    p.add_argument("--ref", default="main", help="branch the sync loop follows (default: main)")
    p.add_argument("--registry-url", default=None, help="loopback only")
    p.add_argument("--auth-url", default=None, help="loopback only")
    p.add_argument("--state-dir", type=Path, default=None, help="overrides $AMS_STATE_DIR")
    p.add_argument("--store-dir", type=Path, default=None, help="overrides $AMS_STORE_DIR")
    p.add_argument("--log-level", default="INFO")
    return p


def add_subparser(ops: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Add ``rollback`` to ``ams platform``'s verb list.

    Kept here rather than in ``ams/platform/cli.py`` so this module owns its own
    surface; wiring it in costs two lines there (see the task report).
    """
    return _add_arguments(
        ops.add_parser("rollback", help="re-deploy one service at an earlier commit")
    )


def cmd_rollback(args: argparse.Namespace) -> int:
    """``ams platform rollback`` / ``python -m ams.platform.rollback``."""
    from ams.platform import sync as sync_mod
    from ams.platform.cli import DEFAULT_REPO_URL

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    state = StateDir(args.state_dir) if args.state_dir else StateDir.from_env()
    store = RuntimeStore(args.store_dir) if args.store_dir else RuntimeStore.from_env()
    try:
        cfg = SyncConfig(
            repo_url=args.repo or DEFAULT_REPO_URL,
            ref=args.ref,
            registry_url=args.registry_url or sync_mod.DEFAULT_REGISTRY_URL,
            auth_url=args.auth_url or sync_mod.DEFAULT_AUTH_URL,
        )
    except SyncError as e:
        print(f"ERROR {e}", file=sys.stderr)
        return EXIT_PRECONDITION
    try:
        report = rollback(state, store, args.id, cfg=cfg, to_sha=args.to)
    except (RollbackError, StateCorrupt) as e:
        print(f"ERROR {e}", file=sys.stderr)
        return EXIT_PRECONDITION
    for line in [report.summary(), *(f"warning: {w}" for w in report.warnings)]:
        print(line, file=sys.stderr)
    return report.exit_code


def main(argv: Sequence[str] | None = None) -> int:
    """Standalone entry point: ``python -m ams.platform.rollback <id> [--to SHA]``."""
    parser = _add_arguments(
        argparse.ArgumentParser(
            prog="ams platform rollback",
            description="Re-deploy one service at an earlier commit.",
        )
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    return cmd_rollback(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
