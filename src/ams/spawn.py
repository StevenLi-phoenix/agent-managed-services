"""Spawner interface.

The supervisor never forks a service itself; it asks a ``Spawner``. Two
implementations exist:

- ``PlainSpawner`` (this module): plain subprocess, no isolation. Used for
  unit tests on any OS and for ``ams run --no-isolation`` during development.
- ``IsolatedSpawner`` (``ams.isolated``): Linux-only. Moves the child into its
  own cgroup, creates a user namespace, applies the per-service uid/gid map via
  ``newuidmap``/``newgidmap`` and drops to the inner uid before exec.

Both return a ``SpawnedService`` whose stdout/stderr fds the harness owns and
reads inline; nothing goes through journald.
"""

from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from ams.schema import ServiceDecl, expand_ports

log = logging.getLogger("ams.spawn")

# Inner uid/gid every service runs as. Mapped to the service's subuid block.
INNER_UID = 1000
INNER_GID = 1000

# The one directory under the service root that is meant to survive a
# re-provision: databases, uploads, anything the service owns. Exported as
# ``AMS_DATA_DIR`` and created by ``userns.ensure_service_root`` (0750, owned by
# the service uid) so a service never has to guess where it may write.
DATA_DIRNAME = "data"


@dataclass(frozen=True)
class SpawnRequest:
    decl: ServiceDecl
    root: Path  # service root: workdir/runtime/home live under it
    ports: Mapping[str, int]  # allocated port per declared name
    extra_env: Mapping[str, str] = field(default_factory=dict)  # runtime activation etc.
    path_prepend: tuple[str, ...] = ()  # e.g. venv bin dir

    @property
    def workdir(self) -> Path:
        wd = Path(self.decl.start.workdir)
        return wd if wd.is_absolute() else self.root / wd

    @property
    def data_dir(self) -> Path:
        return self.root / DATA_DIRNAME

    def argv(self) -> list[str]:
        return [expand_ports(a, self.ports) for a in self.decl.start.argv]

    def env(self) -> dict[str, str]:
        """Minimal, explicit environment. Nothing from the harness leaks in."""
        base_path = ":".join((*self.path_prepend, "/usr/local/bin", "/usr/bin", "/bin"))
        env: dict[str, str] = {
            "PATH": base_path,
            "HOME": str(self.root),
            "LANG": "C.UTF-8",
            "PYTHONUNBUFFERED": "1",
            "AMS_SERVICE_ID": self.decl.id,
            "AMS_DATA_DIR": str(self.data_dir),
        }
        for name, port in self.ports.items():
            env[f"PORT_{name}"] = str(port)
        env.update(self.extra_env)
        # Declared env cannot override reserved names (rejected by schema.validate);
        # everything else from the declaration wins over the defaults above.
        for k, v in self.decl.env.items():
            env[k] = expand_ports(v, self.ports)
        return env


@dataclass
class SpawnedService:
    service_id: str
    pid: int
    stdout_fd: int  # read end, non-blocking, owned by the harness
    stderr_fd: int
    cgroup: Path | None = None  # None for PlainSpawner

    def close_fds(self) -> None:
        for fd in (self.stdout_fd, self.stderr_fd):
            try:
                os.close(fd)
            except OSError:
                pass


@runtime_checkable
class Spawner(Protocol):
    def spawn(self, req: SpawnRequest) -> SpawnedService: ...

    def kill_tree(self, svc: SpawnedService) -> None:
        """Hard-kill the service and every descendant (cgroup.kill when isolated)."""
        ...

    def cleanup(self, svc: SpawnedService) -> None:
        """Release per-spawn resources (cgroup dir) after the process is reaped."""
        ...


def _pipe_nonblocking_read_end() -> tuple[int, int]:
    r, w = os.pipe()
    os.set_blocking(r, False)
    os.set_inheritable(w, True)
    return r, w


class PlainSpawner:
    """No isolation: the child runs as the harness user in the harness cgroup."""

    def spawn(self, req: SpawnRequest) -> SpawnedService:
        # Everything that can fail runs before or inside the try so that no fd
        # survives a failed spawn (the supervisor retries spawn failures).
        argv, env = req.argv(), req.env()
        req.workdir.mkdir(parents=True, exist_ok=True)
        # No namespace here, so no admin ns is needed: create the data dir the
        # same way the isolated spawner does, so AMS_DATA_DIR is never a
        # dangling path under `ams run --no-isolation`.
        req.data_dir.mkdir(parents=True, exist_ok=True)
        req.data_dir.chmod(0o750)
        fds: list[int] = []
        try:
            out_r, out_w = _pipe_nonblocking_read_end()
            fds += [out_r, out_w]
            err_r, err_w = _pipe_nonblocking_read_end()
            fds += [err_r, err_w]
            proc = subprocess.Popen(
                argv,
                cwd=str(req.workdir),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=out_w,
                stderr=err_w,
                start_new_session=True,  # own process group so we can signal the group
                close_fds=True,
            )
        except BaseException:
            for fd in fds:
                try:
                    os.close(fd)
                except OSError:
                    pass
            raise
        os.close(out_w)
        os.close(err_w)
        log.info("spawned %s pid=%d (plain)", req.decl.id, proc.pid)
        # Detach the Popen object: the supervisor reaps via waitpid itself.
        proc.returncode = 0  # suppress ResourceWarning on GC
        return SpawnedService(req.decl.id, proc.pid, out_r, err_r)

    def kill_tree(self, svc: SpawnedService) -> None:
        try:
            os.killpg(svc.pid, 9)
        except ProcessLookupError:
            pass

    def cleanup(self, svc: SpawnedService) -> None:
        svc.close_fds()
