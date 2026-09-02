"""Hot reload: rescan the state dir and reconcile it with the running loop.

``reload(asm)`` is the whole of ``ams ctl reload`` and of SIGHUP
(``systemctl reload ams-harness``). It compares ``services/*/service.toml`` on
disk against what the supervisor currently has and does the minimum:

- a declaration that appeared      -> register (ports, uid block, service root)
                                      through the same ``ams.cli._register``
                                      path startup uses, then start it;
- a declaration that disappeared   -> graceful stop, then remove once the
                                      process is actually gone;
- a declaration whose bytes changed-> re-read, re-allocate ports, swap the
                                      declaration in place, restart;
- everything else                  -> untouched, and that is the point. The
                                      other services do not even notice.

Change detection is a sha256 of the file's bytes (D17), not an mtime and not a
comparison of parsed declarations: mtime lies across rsync/`cp -p` deploys, and
comparing parsed objects would silently ignore a comment or formatting change
that an operator expects to be a no-op anyway. Hashing bytes is exact, cheap at
this scale (tens of files, a few hundred bytes each), and easy to explain.

Two asynchronous facts shape the code:

1. **Stopping is not instant.** ``Supervisor.remove`` refuses to drop a service
   that still has a live process, so a removal is a two-phase operation:
   ``reload`` marks the id and ``drain_pending_removals`` -- called every
   iteration from the CLI's ``on_iteration`` hook -- finishes it when the state
   machine reaches stopped. The loop's own stop-timeout timer guarantees that
   happens even for a child that ignores SIGTERM.
2. **Provisioning must stay out of the loop.** ``runtime.provision`` shells out
   to uv/pnpm/bun for seconds to minutes and the supervisor is single-threaded,
   so a reload never provisions (D17: ``ams provision`` then ``ams ctl reload``).
   A new service whose runtime is not built yet will fail to start and be
   reported like any other crash.

Port release on removal: **yes**. A removed service's port assignment is dropped
so the number returns to the pool -- otherwise a harness that churns through
declarations leaks its 20000-29999 range. The uid block is deliberately *not*
released: files under the service root on disk are owned by that block, so
re-adding the same id must get the same identity back (``UidAllocator`` is
keyed by id and has no release for exactly this reason).
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ams.schema import DeclError, ServiceDecl

if TYPE_CHECKING:  # pragma: no cover - typing only; importing cli here would cycle
    from ams.cli import Assembly

log = logging.getLogger("ams.reload")


def content_hash(path: Path) -> str:
    """sha256 of a declaration file's bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def seed_hashes(asm: Assembly) -> None:
    """Record the current on-disk hash of every registered service.

    Called once after ``build_supervisor`` so the first reload compares against
    what was actually loaded at startup instead of reporting every service as
    changed.
    """
    for service_id in list(asm.supervisor.services):
        try:
            asm.hashes[service_id] = content_hash(asm.state.service_decl_path(service_id))
        except OSError as e:  # pragma: no cover - it was just read by load_declarations
            log.warning("cannot hash the declaration of %s: %s", service_id, e)


def reload(asm: Assembly) -> dict[str, Any]:
    """Reconcile the state dir with the running supervisor.

    Returns ``{"added": [...], "removed": [...], "changed": [...],
    "unchanged": n, "errors": {id: message}, "secrets_missing": [...]}``.
    ``removed`` names the ids whose stop was *initiated*; the drop from the
    service table happens in :func:`drain_pending_removals` once the process is
    gone. ``secrets_missing`` is a heads-up over the touched ids only (D16), not
    a failure -- see :func:`_warn_missing_secrets`.

    Never raises on account of the declarations: an unreadable or invalid file
    lands in ``errors`` and the other services are still processed.
    """
    from ams.cli import _register  # noqa: PLC0415 - deferred: ams.cli imports us

    drain_pending_removals(asm)

    state = asm.state
    sup = asm.supervisor
    errors: dict[str, str] = {}
    added: list[str] = []
    changed: list[str] = []
    removed: list[str] = []
    unchanged = 0

    on_disk = state.list_service_ids()
    decls: dict[str, ServiceDecl] = {}
    hashes: dict[str, str] = {}
    for service_id in on_disk:
        try:
            hashes[service_id] = content_hash(state.service_decl_path(service_id))
            decls[service_id] = state.load_declaration(service_id)
        except (DeclError, OSError) as e:
            errors[service_id] = f"{type(e).__name__}: {e}"
            log.error("reload: %s has an unusable declaration: %s", service_id, e)

    for service_id in on_disk:
        if service_id in errors:
            continue
        decl = decls[service_id]
        if service_id in sup.services:
            if asm.hashes.get(service_id) == hashes[service_id]:
                unchanged += 1
                continue
            if _apply_change(asm, service_id, decl, errors):
                asm.hashes[service_id] = hashes[service_id]
                changed.append(service_id)
            continue
        # New (or a startup registration that failed and is being retried).
        asm.pending_removals.discard(service_id)
        if not _register(asm, service_id, decl, store=asm.store, provision=False):
            errors[service_id] = "registration failed (ports, uid block or service root); see log"
            continue
        asm.declarations[service_id] = decl
        asm.hashes[service_id] = hashes[service_id]
        if service_id not in asm.registered:
            asm.registered.append(service_id)
        sup.start(service_id)
        added.append(service_id)
        log.info("reload: added and started %s", service_id)

    for service_id in list(sup.services):
        if service_id in hashes or service_id in errors:
            continue
        removed.append(service_id)
        asm.pending_removals.add(service_id)
        log.info("reload: %s is gone from disk; stopping it", service_id)
        try:
            sup.stop(service_id)
        except Exception as e:  # pragma: no cover - stop() swallows signal errors
            errors[service_id] = f"stop failed: {type(e).__name__}: {e}"
    drain_pending_removals(asm)

    summary = {
        "added": added,
        "removed": removed,
        "changed": changed,
        "unchanged": unchanged,
        "errors": errors,
        "secrets_missing": _warn_missing_secrets(asm, [*added, *changed], decls),
    }
    log.info(
        "reload: +%d ~%d -%d =%d errors=%d",
        len(added),
        len(changed),
        len(removed),
        unchanged,
        len(errors),
    )
    return summary


def _warn_missing_secrets(
    asm: Assembly, touched: list[str], decls: dict[str, ServiceDecl]
) -> list[str]:
    """Heads-up for services this reload touched whose declared secrets are unset.

    Scoped to the touched ids on purpose: warning over every declaration would
    re-log the same known-missing secret on every reload, which is noise an
    operator learns to skip past. A service that was not added or restarted has
    not changed its mind about its secrets since the last time we said so.

    Never gates the reload. A missing value is a per-service start failure by
    design (``ams.secrets.make_extra_env_for`` raises at spawn, which the
    supervisor escalates), and the other services in this reload have to go
    through regardless -- so every failure mode here, including the secrets
    module being absent from the build, degrades to "no warning".
    """
    if not touched:
        return []
    try:
        from ams.secrets import warn_missing_secrets  # noqa: PLC0415 - optional layer
    except ImportError as e:  # pragma: no cover - secrets ships with the package
        log.debug("secrets layer unavailable; not checking declared secrets: %s", e)
        return []
    subset = {sid: decls[sid] for sid in touched if sid in decls}
    try:
        return warn_missing_secrets(asm.state, subset)
    except Exception as e:  # a stat error must not fail a reload
        log.warning("could not check declared secrets: %s: %s", type(e).__name__, e)
        return []


def _apply_change(
    asm: Assembly, service_id: str, decl: ServiceDecl, errors: dict[str, str]
) -> bool:
    """Swap in a changed declaration and restart the service. False = reported."""
    sup = asm.supervisor
    st = sup.services[service_id]
    try:
        # Re-allocate: the port *requests* may have changed. A name whose request
        # is unchanged keeps its existing number, so a service that only changed
        # its argv keeps serving on the same port after the restart.
        allocated = asm.ports.allocate(service_id, decl.ports)
    except Exception as e:
        errors[service_id] = f"port allocation failed: {type(e).__name__}: {e}"
        log.error("reload: %s: %s", service_id, errors[service_id])
        return False
    sup.replace_decl(service_id, decl, allocated)
    asm.declarations[service_id] = decl
    # ``desired`` is the operator's intent, and it wins: a service someone
    # stopped with `ams ctl stop` stays stopped through a declaration change.
    # The exception is "failed" -- there the *harness* gave up after a crash
    # loop, and editing the declaration is precisely how that gets fixed, so a
    # new declaration revives it. (``status`` alone is the wrong test: the
    # stopping/reaped windows read as neither "running" nor "stopped".)
    if st.desired == "down" and st.status != "failed":
        log.info("reload: %s changed but is down; leaving it down", service_id)
        return True
    sup.restart(service_id)
    log.info("reload: %s changed; restarting (was %s)", service_id, st.status)
    return True


def drain_pending_removals(asm: Assembly) -> list[str]:
    """Finish removals whose process has actually exited. Returns what was dropped.

    Cheap enough to call every loop iteration: it is a set membership test per
    pending id, and the set is empty except in the seconds after a reload that
    removed something.
    """
    if not asm.pending_removals:
        return []
    sup = asm.supervisor
    dropped: list[str] = []
    for service_id in sorted(asm.pending_removals):
        st = sup.services.get(service_id)
        if st is None:  # already gone
            dropped.append(service_id)
            continue
        if st.spawned is not None or st.status in ("starting", "running", "stopping"):
            continue  # still winding down; the loop's stop timer will get there
        try:
            sup.remove(service_id)
        except (KeyError, RuntimeError) as e:  # pragma: no cover - guarded above
            log.warning("cannot remove %s yet: %s", service_id, e)
            continue
        dropped.append(service_id)
    for service_id in dropped:
        asm.pending_removals.discard(service_id)
        asm.declarations.pop(service_id, None)
        asm.hashes.pop(service_id, None)
        if service_id in asm.registered:
            asm.registered.remove(service_id)
        try:
            asm.ports.release(service_id)
        except OSError as e:
            log.warning("could not release the ports of %s: %s", service_id, e)
        log.info("reload: removed %s (ports released, uid block kept)", service_id)
    return dropped
