"""Linux isolated spawner: one user namespace + one cgroup per service.

Mirrors ``ams.spawn.PlainSpawner`` so the supervisor is written once against
the ``Spawner`` protocol. What this adds over the plain one:

- the child is moved into ``<delegated root>/svc-<id>`` before it forks
  anything, so ``cgroup.kill`` reaches every descendant no matter what the
  service does with process groups;
- ``memory.max`` / ``pids.max`` / ``cpu.max`` come from the declaration;
- the child runs in its own user namespace as inner uid 1000, which the host
  sees as the service's private subuid block, with no capabilities and
  ``no_new_privs`` set.

There is no silent fallback: if the cgroup subtree is not delegated, spawning
raises rather than quietly running a service unconfined.
"""

from __future__ import annotations

import logging
import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

from ams.cgroup import CgroupRoot, ServiceCgroup
from ams.spawn import INNER_GID, INNER_UID, SpawnedService, SpawnRequest
from ams.uidmap import UidBlock
from ams.userns import SpawnError, ensure_service_root, fork_in_userns

log = logging.getLogger("ams.isolated")

# How long cleanup waits for a killed cgroup to drain before giving up on rmdir.
CLEANUP_DRAIN_S = 2.0

BlockLookup = Callable[[str], UidBlock]


class IsolatedSpawner:
    """Spawner protocol implementation for Linux with userns + cgroup v2."""

    def __init__(self, cgroup_root: CgroupRoot, blocks: BlockLookup) -> None:
        self._root = cgroup_root
        self._blocks = blocks

    @property
    def cgroup_root(self) -> CgroupRoot:
        """The delegated root every service cgroup is created under."""
        return self._root

    # ---------------------------------------------------------------- spawn

    def spawn(self, req: SpawnRequest) -> SpawnedService:
        block = self._blocks(req.decl.id)
        env = req.env()
        argv = req.argv()
        exe = shutil.which(argv[0], path=env["PATH"])
        if exe is None:
            # Resolve here, in the harness, so a typo in the declaration is a
            # clean error instead of an exec failure inside the namespace.
            raise SpawnError(f"{req.decl.id}: {argv[0]!r} not found on PATH={env['PATH']}")

        workdir = req.workdir
        subdirs = (workdir,) if workdir == req.root or req.root in workdir.parents else ()
        ensure_service_root(req.root, block, subdirs=subdirs)

        svc_cg = ServiceCgroup.create(self._root, req.decl.id)
        svc_cg.apply_limits(req.decl.limits)
        if req.decl.limits.memory_max is not None:
            # DECISIONS D12: with swap present `memory.max` is only a reclaim
            # threshold, so a declaration asking for a memory ceiling would get
            # a service that overshoots into swap instead of dying. The
            # declaration cannot express host swap, so the supervisor decides.
            svc_cg.set_swap_max(0)
            log.debug("%s: memory.swap.max=0 (memory_max declared)", req.decl.id)

        out_r, out_w = _pipe_for_child()
        err_r, err_w = _pipe_for_child()

        def pre_unshare(_pid: int) -> None:
            # Runs in the child while it is still an ordinary harness process:
            # the only race-free point to put it in the cgroup, and the only
            # point at which the workdir is reachable by path (the state dir
            # lives under the harness home, which the service uid cannot
            # traverse; an inherited cwd needs no traversal rights).
            svc_cg.add_pid(_pid)
            os.chdir(workdir)

        def child() -> NoReturn:
            devnull = os.open(os.devnull, os.O_RDONLY)
            os.dup2(devnull, 0)
            os.dup2(out_w, 1)
            os.dup2(err_w, 2)
            for fd in (devnull, out_w, err_w, out_r, err_r):
                if fd > 2:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            os.setsid()
            os.execve(exe, argv, env)
            raise SpawnError("execve returned")  # pragma: no cover

        try:
            pid = fork_in_userns(
                block.newuidmap_args(),
                block.newgidmap_args(),
                INNER_UID,
                INNER_GID,
                child,
                pre_unshare=pre_unshare,
            )
        except BaseException:
            # Every fd is closed exactly once. It used to be `finally`, which
            # double-closed the write ends: the cgroup teardown below opens
            # control files in between, so the second close could land on a
            # recycled fd number -- and `_close` swallows the EBADF that would
            # have shown it. `else` is what keeps the two paths disjoint.
            for fd in (out_r, out_w, err_r, err_w):
                _close(fd)
            svc_cg.kill()
            svc_cg.wait_empty(CLEANUP_DRAIN_S)
            svc_cg.remove()
            raise
        else:
            # The child holds its own dups of the write ends; the harness keeps
            # only the read ends.
            _close(out_w)
            _close(err_w)

        log.info(
            "spawned %s pid=%d cgroup=%s block=%d+%d",
            req.decl.id,
            pid,
            svc_cg.path,
            block.uid_start,
            block.size,
        )
        return SpawnedService(req.decl.id, pid, out_r, err_r, cgroup=svc_cg.path)

    # ---------------------------------------------------------------- teardown

    def kill_tree(self, svc: SpawnedService) -> None:
        cg = self._cgroup_of(svc)
        if cg is None:
            log.warning("%s has no cgroup; falling back to killpg", svc.service_id)
            try:
                os.killpg(svc.pid, 9)
            except (ProcessLookupError, PermissionError) as e:
                log.debug("killpg(%d) failed: %s", svc.pid, e)
            return
        cg.kill()

    def cleanup(self, svc: SpawnedService) -> None:
        svc.close_fds()
        cg = self._cgroup_of(svc)
        if cg is None:
            return
        if not cg.wait_empty(CLEANUP_DRAIN_S):
            log.warning("cgroup %s still populated at cleanup; leaving it", cg.path)
            return
        cg.remove()

    def stats(self, svc: SpawnedService) -> dict[str, int]:
        cg = self._cgroup_of(svc)
        return cg.stats() if cg is not None else {}

    def _cgroup_of(self, svc: SpawnedService) -> ServiceCgroup | None:
        if svc.cgroup is None:
            return None
        return ServiceCgroup(Path(svc.cgroup), svc.service_id, self._root.path)


def _pipe_for_child() -> tuple[int, int]:
    """Read end non-blocking and harness-owned; write end inherited by the child."""
    r, w = os.pipe()
    os.set_blocking(r, False)
    os.set_inheritable(w, True)
    return r, w


def _close(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


def make_isolated_spawner(blocks: BlockLookup) -> IsolatedSpawner:
    """Discover the delegated cgroup root, enable controllers, build the spawner.

    Raises ``CgroupUnavailable`` when the process was not started with a
    delegated cgroup; that message names the fix.
    """
    root = CgroupRoot.discover()
    root.enable_controllers()
    return IsolatedSpawner(root, blocks)
