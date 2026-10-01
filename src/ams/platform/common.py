"""Small helpers shared by the platform's one-shot commands.

Module-level on purpose: they are the seams tests replace -- the only calls
that reach the running harness or the host's subordinate-id files.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from ams.state import StateDir
from ams.uidmap import UidAllocator

log = logging.getLogger("ams.platform.common")


def uid_allocator(state: StateDir) -> UidAllocator:
    """The same allocator (and the same state file) the harness itself uses."""
    from ams.cli import harness_user

    return UidAllocator.from_host(harness_user(), state.uidmap_state)


def ctl_reload(state: StateDir, *, timeout_s: float = 60.0) -> dict[str, Any]:
    from ams.control import control_socket_path
    from ams.control import request as control_request

    return control_request(control_socket_path(state), "reload", timeout_s=timeout_s)


def ctl_restart(state: StateDir, service_id: str, *, timeout_s: float = 60.0) -> dict[str, Any]:
    from ams.control import control_socket_path
    from ams.control import request as control_request

    return control_request(control_socket_path(state), "restart", service_id, timeout_s=timeout_s)


def write_if_changed(path: Path, content: str) -> bool:
    """Write ``content`` atomically iff it differs from what is already there."""
    try:
        if path.read_text(encoding="utf-8") == content:
            return False
    except (OSError, UnicodeDecodeError):
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp{os.getpid()}")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)
    log.debug("wrote %s", path)
    return True
