"""cgroup v2 control for the harness' delegated subtree.

The harness never owns the whole cgroup hierarchy. systemd hands it one
delegated subtree (``Delegate=yes``) and the harness creates one child cgroup
per service inside it. Everything here therefore refuses to touch anything
outside that subtree.

Two shapes of delegation must both work (see DECISIONS D5):

- ``systemd-run -p Delegate=yes`` (what the test runner uses): our own cgroup
  *is* the delegated root and still holds our pid. cgroup v2's
  "no internal processes" rule forbids enabling controllers in
  ``cgroup.subtree_control`` while a cgroup has both processes and children, so
  we must first move ourselves into a leaf (``<root>/harness``).
- ``DelegateSubgroup=harness`` (the production unit, systemd 255): systemd has
  already put the main pid in ``<root>/harness``, so there is nothing to move.

``discover()`` detects which case it is in and normalises to the first.
"""

from __future__ import annotations

import errno
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from ams.schema import SERVICE_ID_RE, LimitsSpec

log = logging.getLogger("ams.cgroup")

# Injectable so the portable tests can point the whole module at a tmp dir.
SYSFS_CGROUP = Path("/sys/fs/cgroup")
PROC_SELF_CGROUP = Path("/proc/self/cgroup")

# Leaf we move ourselves into when we are sitting on the delegated root.
# Same name systemd's DelegateSubgroup= uses in deploy/ams-harness.service.
HARNESS_LEAF = "harness"

# Prefix for per-service cgroup directories, so a stray dir is recognisable.
SERVICE_PREFIX = "svc-"

DEFAULT_CONTROLLERS = ("cpu", "memory", "pids")

# Controllers whose absence is survivable: without `cpu` we simply cannot apply
# a cpu.max, which is a degraded but working supervisor.
OPTIONAL_CONTROLLERS = frozenset({"cpu"})


class CgroupUnavailable(RuntimeError):
    """No writable delegated cgroup v2 subtree. The harness cannot isolate."""


def _fix_hint() -> str:
    return (
        "run the harness under systemd with Delegate=yes "
        "(unit: deploy/ams-harness.service; tests: scripts/remote-test.sh). "
        "An interactive ssh session's cgroup is never delegated."
    )


def own_cgroup(
    sysfs: Path = SYSFS_CGROUP,
    proc_self_cgroup: Path = PROC_SELF_CGROUP,
) -> Path:
    """Absolute path of the cgroup this process is in.

    cgroup v2 gives a single ``0::<path>`` line; the path is relative to the
    unified mount point.
    """
    try:
        text = proc_self_cgroup.read_text(encoding="utf-8")
    except OSError as e:
        raise CgroupUnavailable(f"cannot read {proc_self_cgroup}: {e}") from e
    for line in text.splitlines():
        parts = line.split(":", 2)
        if len(parts) == 3 and parts[0] == "0":
            return sysfs / parts[2].lstrip("/")
    raise CgroupUnavailable(
        f"{proc_self_cgroup} has no cgroup v2 (`0::`) line; host is not on the unified hierarchy"
    )


def _ours(path: Path) -> bool:
    """True if the directory itself was handed to us (delegation chowns it)."""
    return path.is_dir() and os.access(path, os.W_OK | os.X_OK)


def _writable_cgroup_dir(path: Path) -> bool:
    """True if we may both create children here and enable controllers here."""
    return _ours(path) and os.access(path / "cgroup.subtree_control", os.W_OK)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _write(path: Path, value: str, *, within: Path) -> None:
    """Write a cgroup control file, refusing anything outside ``within``."""
    resolved = path.resolve()
    root = within.resolve()
    if resolved != root and root not in resolved.parents:
        raise CgroupUnavailable(f"refusing to write {resolved}: outside delegated root {root}")
    with open(resolved, "w", encoding="utf-8") as fh:
        fh.write(value)


@dataclass(frozen=True)
class CgroupRoot:
    """The delegated subtree root. Service cgroups are created as its children."""

    path: Path
    sysfs: Path = SYSFS_CGROUP

    @classmethod
    def discover(
        cls,
        sysfs: Path = SYSFS_CGROUP,
        proc_self_cgroup: Path = PROC_SELF_CGROUP,
    ) -> CgroupRoot:
        """Find the delegated root and make sure our own pid sits in a leaf.

        Idempotent: safe to call twice in one process.
        """
        if not (sysfs / "cgroup.controllers").exists():
            raise CgroupUnavailable(
                f"{sysfs}/cgroup.controllers is missing: no cgroup v2 unified hierarchy here"
            )
        own = own_cgroup(sysfs, proc_self_cgroup)
        if not own.is_dir():
            raise CgroupUnavailable(f"own cgroup {own} does not exist under {sysfs}")

        # The delegated region is contiguous from our own cgroup upwards: walk
        # up while the directory is still ours, and take the topmost node whose
        # cgroup.subtree_control we may write (that is where we enable
        # controllers and create service children).
        root: Path | None = None
        node = own
        sysfs_resolved = sysfs.resolve()
        while node.resolve() != sysfs_resolved and _ours(node):
            if _writable_cgroup_dir(node):
                root = node
            node = node.parent
        if root is None:
            raise CgroupUnavailable(
                f"cgroup {own} is not delegated to uid {os.getuid()}: {_fix_hint()}"
            )

        if own.resolve() == root.resolve():
            leaf = root / HARNESS_LEAF
            leaf.mkdir(exist_ok=True)
            # Moving ourselves out of the root is what makes it legal to enable
            # controllers in root/cgroup.subtree_control (no-internal-processes).
            _write(leaf / "cgroup.procs", str(os.getpid()), within=root)
            log.info("moved harness pid %d into leaf cgroup %s", os.getpid(), leaf)
        else:
            log.debug("already in leaf cgroup %s under delegated root %s", own, root)
        log.info("delegated cgroup root: %s", root)
        return cls(root, sysfs)

    # ---------------------------------------------------------------- controllers

    def available_controllers(self) -> tuple[str, ...]:
        return tuple(_read(self.path / "cgroup.controllers").split())

    def enabled_controllers(self) -> tuple[str, ...]:
        return tuple(_read(self.path / "cgroup.subtree_control").split())

    def enable_controllers(self, names: tuple[str, ...] = DEFAULT_CONTROLLERS) -> tuple[str, ...]:
        """Enable controllers for our children. Returns the ones now enabled.

        Absent controllers are logged and skipped. A failure to enable ``cpu``
        is a warning (we lose cpu.max, not correctness); anything else is fatal
        because memory/pids limits are the point of the exercise.
        """
        available = set(self.available_controllers())
        enabled: list[str] = []
        for name in names:
            if name not in available:
                level = log.warning if name in OPTIONAL_CONTROLLERS else log.error
                level("controller %r not available in %s (have %s)", name, self.path, available)
                if name not in OPTIONAL_CONTROLLERS:
                    raise CgroupUnavailable(
                        f"controller {name!r} not available at {self.path}; "
                        f"have {sorted(available)}"
                    )
                continue
            try:
                _write(self.path / "cgroup.subtree_control", f"+{name}", within=self.path)
            except OSError as e:
                if name in OPTIONAL_CONTROLLERS:
                    log.warning("could not enable controller %r at %s: %s", name, self.path, e)
                    continue
                raise CgroupUnavailable(
                    f"cannot enable controller {name!r} at {self.path}: {e}. "
                    "Are there processes directly in this cgroup? " + _fix_hint()
                ) from e
            enabled.append(name)
        log.info("controllers enabled at %s: %s", self.path, enabled)
        return tuple(enabled)


@dataclass(frozen=True)
class ServiceCgroup:
    """One service's cgroup: limits, membership, kill, teardown."""

    path: Path
    service_id: str
    root: Path

    @classmethod
    def create(cls, root: CgroupRoot, service_id: str) -> ServiceCgroup:
        if not SERVICE_ID_RE.match(service_id):
            raise ValueError(f"service id {service_id!r} must match {SERVICE_ID_RE.pattern}")
        path = root.path / f"{SERVICE_PREFIX}{service_id}"
        try:
            path.mkdir(exist_ok=True)
        except OSError as e:
            raise CgroupUnavailable(f"cannot create cgroup {path}: {e}") from e
        log.debug("service cgroup ready: %s", path)
        return cls(path, service_id, root.path)

    # ---------------------------------------------------------------- limits

    def apply_limits(self, limits: LimitsSpec) -> dict[str, str]:
        """Write only the limits the declaration actually sets.

        ``memory.swap.max`` is deliberately NOT written here: this method writes
        exactly what the declaration says, and the declaration has no swap
        field. Where swap exists (the target box has 1 GB, so this is not
        hypothetical) ``memory.max`` alone caps resident memory but not total
        memory, so a service that overshoots is pushed into swap instead of
        being killed — see ``test_memory_max_alone_does_not_bound_a_greedy_process``.
        Turning ``memory_max`` into a hard ceiling is a supervisor policy call
        (DECISIONS D12): ``IsolatedSpawner.spawn`` follows this with
        ``set_swap_max(0)`` whenever ``limits.memory_max`` is set.
        """
        written: dict[str, str] = {}
        mem = limits.memory_max_bytes
        if mem is not None:
            self._write_control("memory.max", str(mem))
            written["memory.max"] = str(mem)
        if limits.pids_max is not None:
            self._write_control("pids.max", str(limits.pids_max))
            written["pids.max"] = str(limits.pids_max)
        cpu = limits.cpu_max_value
        if cpu is not None:
            self._write_control("cpu.max", cpu)
            written["cpu.max"] = cpu
        if written:
            log.info("limits for %s: %s", self.service_id, written)
        return written

    def set_swap_max(self, value: int | str = 0) -> None:
        """Cap (or release, with ``"max"``) this cgroup's swap.

        Separate from ``apply_limits`` because it is a policy choice the
        supervisor makes, not something the service declaration expresses.
        Setting it to 0 is what turns ``memory.max`` into a hard ceiling that
        the OOM killer enforces rather than a reclaim threshold.
        """
        self._write_control("memory.swap.max", str(value))

    def _write_control(self, name: str, value: str) -> None:
        try:
            _write(self.path / name, value, within=self.path)
        except OSError as e:
            raise CgroupUnavailable(
                f"cannot write {name}={value!r} in {self.path}: {e}. "
                "Is the controller enabled in the parent's cgroup.subtree_control?"
            ) from e

    # ---------------------------------------------------------------- membership

    def add_pid(self, pid: int) -> None:
        _write(self.path / "cgroup.procs", str(pid), within=self.path)

    def pids(self) -> list[int]:
        try:
            return [int(x) for x in _read(self.path / "cgroup.procs").split()]
        except FileNotFoundError:
            return []

    def populated(self) -> bool:
        try:
            for line in _read(self.path / "cgroup.events").splitlines():
                key, _, value = line.partition(" ")
                if key == "populated":
                    return value.strip() == "1"
        except FileNotFoundError:
            return False
        return False

    # ---------------------------------------------------------------- teardown

    def kill(self) -> None:
        """SIGKILL every process in the cgroup, including ones that re-forked.

        Returns immediately; the kernel does the walk. Use ``wait_empty`` to
        observe the result and ``waitpid`` to reap our own direct child.
        """
        try:
            _write(self.path / "cgroup.kill", "1", within=self.path)
        except FileNotFoundError:
            log.warning("cgroup %s already gone; nothing to kill", self.path)
        except OSError as e:
            log.warning("cgroup.kill on %s failed: %s", self.path, e)

    def wait_empty(self, timeout_s: float) -> bool:
        """Poll ``cgroup.events`` until no processes remain. True if it emptied."""
        deadline = time.monotonic() + timeout_s
        while True:
            if not self.populated():
                return True
            if time.monotonic() >= deadline:
                log.warning(
                    "cgroup %s still populated after %.1fs: %s", self.path, timeout_s, self.pids()
                )
                return False
            time.sleep(0.02)

    def remove(self, timeout_s: float = 1.0) -> bool:
        """rmdir the cgroup. Briefly retries EBUSY (the kernel frees lazily)."""
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                self.path.rmdir()
                log.debug("removed cgroup %s", self.path)
                return True
            except FileNotFoundError:
                return True
            except OSError as e:
                if e.errno != errno.EBUSY or time.monotonic() >= deadline:
                    log.warning("cannot remove cgroup %s: %s", self.path, e)
                    return False
                time.sleep(0.02)

    # ---------------------------------------------------------------- reporting

    def stats(self) -> dict[str, int]:
        """Small set of numbers the supervisor reports. Missing keys are skipped."""
        out: dict[str, int] = {}
        for name in ("memory.current", "memory.peak", "pids.current", "pids.peak"):
            try:
                out[name] = int(_read(self.path / name).strip())
            except (OSError, ValueError):
                continue
        try:
            for line in _read(self.path / "cpu.stat").splitlines():
                key, _, value = line.partition(" ")
                if key in ("usage_usec", "user_usec", "system_usec"):
                    out[f"cpu.{key}"] = int(value)
        except OSError:
            pass
        return out
