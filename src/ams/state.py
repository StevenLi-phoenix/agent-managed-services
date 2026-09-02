"""State directory layout and declaration loading.

The harness keeps everything it owns under one root (``AMS_STATE_DIR``, or
``~/ams-state`` in dev): per-service declarations + working directories,
harness-wide allocator state (uid/gid blocks, ports), and logs. This module
defines that layout and the atomic-JSON-write helper shared by the
allocators; it intentionally imports nothing from ``ams.uidmap``/``ams.ports``
so those two can import from here without a cycle.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ams.schema import SERVICE_ID_RE, DeclError, ServiceDecl, load

log = logging.getLogger("ams.state")


class StateCorrupt(RuntimeError):
    """A persisted allocator/state JSON file is corrupt, truncated, or has an
    unexpected shape: invalid JSON, the wrong top-level type, missing or
    mistyped fields, or a semantically invalid value (an out-of-range port,
    a uid/gid block outside its configured subid range, and the like).

    Raised in place of the underlying ``json.JSONDecodeError``/``KeyError``/
    ``TypeError`` so callers get one actionable, catchable error naming the
    file and the offending record, instead of a raw parser traceback. This
    is deliberately not "start empty": a corrupt state file needs operator
    attention (inspect/repair/delete), and silently re-carving over it risks
    handing out already-in-use uids, gids, or ports.
    """


def write_json_atomic(path: Path, data: Any) -> None:
    """Write ``data`` as JSON to ``path`` atomically (temp file + ``os.replace``)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def read_json_checked(path: Path) -> Any:
    """Parse the JSON at ``path``, raising ``StateCorrupt`` (naming the path)
    instead of letting ``json.JSONDecodeError`` escape on a corrupt or
    truncated file."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise StateCorrupt(f"{path}: corrupt JSON: {e}") from e


@dataclass(frozen=True)
class StateDir:
    """Filesystem layout for one harness instance."""

    root: Path

    @property
    def services_dir(self) -> Path:
        return self.root / "services"

    def service_dir(self, service_id: str) -> Path:
        return self.services_dir / service_id

    def service_decl_path(self, service_id: str) -> Path:
        return self.service_dir(service_id) / "service.toml"

    def service_root(self, service_id: str) -> Path:
        """The service's HOME/workdir parent. Created (and owned) by the isolation
        layer with the service's mapped uid/gid, not by ``ensure()``."""
        return self.service_dir(service_id) / "root"

    def service_runtime_dir(self, service_id: str) -> Path:
        return self.service_dir(service_id) / "runtime"

    @property
    def runtime_state_dir(self) -> Path:
        return self.root / "state"

    @property
    def uidmap_state(self) -> Path:
        return self.runtime_state_dir / "uidmap.json"

    @property
    def ports_state(self) -> Path:
        return self.runtime_state_dir / "ports.json"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    def ensure(self) -> None:
        """Create the directories the harness itself owns.

        Service roots are NOT created here: they must be owned by the
        service's mapped uid/gid, which only the isolation layer (running
        with the admin uid/gid map, see ``ams.uidmap.admin_map_args``) can
        set up correctly.
        """
        for d in (self.services_dir, self.runtime_state_dir, self.logs_dir):
            d.mkdir(parents=True, exist_ok=True)
            os.chmod(d, 0o750)  # mkdir's mode= is subject to umask; force it.

    def list_service_ids(self) -> list[str]:
        """Sorted ids of service dirs that contain ``service.toml`` and whose
        directory name is a valid service id. Invalid dir names are skipped
        with a warning rather than raised, so one bad directory does not stop
        the harness from starting the rest."""
        if not self.services_dir.is_dir():
            return []
        ids: list[str] = []
        for entry in self.services_dir.iterdir():
            if not entry.is_dir() or not (entry / "service.toml").is_file():
                continue
            if not SERVICE_ID_RE.match(entry.name):
                log.warning("skipping service directory with invalid id: %r", entry.name)
                continue
            ids.append(entry.name)
        return sorted(ids)

    def load_declaration(self, service_id: str) -> ServiceDecl:
        """Load one declaration; raises ``DeclError`` if invalid."""
        return load(self.service_decl_path(service_id))

    def load_declarations(self) -> dict[str, ServiceDecl]:
        """Load every valid declaration. A bad file is logged and skipped
        rather than raised, so one broken declaration does not take down the
        whole harness."""
        out: dict[str, ServiceDecl] = {}
        for service_id in self.list_service_ids():
            try:
                out[service_id] = self.load_declaration(service_id)
            except DeclError as e:
                log.warning("skipping invalid declaration %r: %s", service_id, e)
        return out

    @classmethod
    def from_env(cls, default: Path | None = None) -> StateDir:
        """``AMS_STATE_DIR`` env var, else ``default``, else ``~/ams-state``."""
        env = os.environ.get("AMS_STATE_DIR")
        if env:
            return cls(Path(env))
        if default is not None:
            return cls(default)
        return cls(Path.home() / "ams-state")
