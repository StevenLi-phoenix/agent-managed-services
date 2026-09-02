"""Port allocation and conflict detection for services.

Services share the host network namespace (no per-service netns, see
CLAUDE.md), so every service binds on ``127.0.0.1`` and the harness is the
only thing preventing two declarations from racing for the same port.
``PortAllocator`` picks free ports for ``0`` ("allocate for me") requests,
persists what each service got, and rejects a fixed request that collides
with another service's assignment or with something already bound on the
host.

Socket activation (evaluated, deferred)
----------------------------------------
Passing pre-bound listening sockets to the child (the systemd ``LISTEN_FDS``
convention: the harness binds before fork, the fd survives exec, the child
calls ``sd_listen_fds`` to pick it up) would give restart-without-connection-
reset -- a new process takes over an already-bound socket with no window
where the port is closed -- and it would remove the bind-then-check race
this module currently has by construction (the harness, not the child, ever
binds). The cost is that the supervised program must itself speak the
``LISTEN_FDS``/``LISTEN_PID`` protocol (or an equivalent ``--fd`` flag) to
accept a passed-in socket instead of binding its own; agent-generated
declarations wrap arbitrary user programs, most of which were never written
with socket activation in mind, so that support cannot be assumed. v1
therefore does allocation and conflict detection only, checking with a real
bind-and-close probe; a ``listen_fds`` field is reserved on the request side
for a later version that opts individual services in once their program is
known to support it.
"""

from __future__ import annotations

import logging
import socket
from collections.abc import Mapping
from pathlib import Path

from ams.schema import MAX_PORT, MIN_PORT
from ams.state import StateCorrupt, read_json_checked, write_json_atomic

log = logging.getLogger("ams.ports")

# Range auto-allocation picks from; fixed requests may be anywhere in
# [MIN_PORT, MAX_PORT] and are not restricted to this range.
DEFAULT_RANGE: tuple[int, int] = (20000, 29999)


class PortConflict(RuntimeError):
    """A requested port is unavailable: owned by another service, or bound
    by an unrelated process on the host."""


_PROBE_HOSTS: tuple[str, ...] = ("127.0.0.1", "0.0.0.0")


def is_port_free(port: int, hosts: tuple[str, ...] = _PROBE_HOSTS) -> bool:
    """True if a TCP bind to ``port`` succeeds on every host in ``hosts`` right now.

    A real bind-and-close probe rather than just checking our own records,
    so it also catches ports held by processes the harness never allocated
    (a stray dev server, a leftover from a previous harness run, etc.).
    Probes with ``SO_REUSEADDR`` *set*, matching what a typical service
    does when it binds -- a probe with it cleared is stricter than the
    real bind it is meant to predict and can report "busy" for a
    TIME_WAIT-only conflict that a real service's bind would sail through.
    Defaults to both ``127.0.0.1`` (what services actually bind, see the
    module docstring) and the wildcard ``0.0.0.0``, since a listener on
    either can block a bind on the other for the same port depending on
    platform; checking both catches the collision regardless of which
    address the other process used.
    """
    for host in hosts:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, port))
            except OSError:
                return False
    return True


class PortAllocator:
    """Assigns and persists port numbers per service, keyed by port name."""

    def __init__(
        self,
        state_path: Path,
        range: tuple[int, int] = DEFAULT_RANGE,
        bind_check: bool = True,
    ) -> None:
        self.state_path = state_path
        self.range = range
        self.bind_check = bind_check
        self._assignments: dict[str, dict[str, int]] = {}
        self._load()

    def _load(self) -> None:
        if not self.state_path.exists():
            return
        data = read_json_checked(self.state_path)
        if not isinstance(data, dict):
            raise StateCorrupt(f"{self.state_path}: expected a JSON object at top level")
        ports_obj = data.get("ports", {})
        if not isinstance(ports_obj, dict):
            raise StateCorrupt(f"{self.state_path}: 'ports' must be an object")
        for service_id, ports in ports_obj.items():
            self._assignments[service_id] = self._parse_ports(service_id, ports)

    def _parse_ports(self, service_id: str, ports: object) -> dict[str, int]:
        if not isinstance(ports, dict):
            raise StateCorrupt(f"{self.state_path}: ports for {service_id!r} must be an object")
        parsed: dict[str, int] = {}
        for name, port in ports.items():
            if not isinstance(port, int) or isinstance(port, bool):
                raise StateCorrupt(
                    f"{self.state_path}: port {name!r} for {service_id!r} is not an "
                    f"integer: {port!r}"
                )
            if not (MIN_PORT <= port <= MAX_PORT):
                raise StateCorrupt(
                    f"{self.state_path}: port {name!r}={port} for {service_id!r} is "
                    f"outside [{MIN_PORT}, {MAX_PORT}]"
                )
            parsed[name] = port
        return parsed

    def _save(self) -> None:
        write_json_atomic(self.state_path, {"version": 1, "ports": self._assignments})

    def get(self, service_id: str) -> dict[str, int]:
        return dict(self._assignments.get(service_id, {}))

    def assignments(self) -> Mapping[str, Mapping[str, int]]:
        return {sid: dict(ports) for sid, ports in self._assignments.items()}

    def release(self, service_id: str) -> None:
        if service_id in self._assignments:
            del self._assignments[service_id]
            self._save()

    def _ports_used_by_others(self, service_id: str) -> dict[int, str]:
        used: dict[int, str] = {}
        for sid, ports in self._assignments.items():
            if sid == service_id:
                continue
            for port in ports.values():
                used[port] = sid
        return used

    def allocate(self, service_id: str, requests: Mapping[str, int]) -> dict[str, int]:
        """Resolve every name in ``requests`` to a port and persist the result.

        A name whose request is unchanged from what this service already
        has (``0`` and an existing assignment, or the same fixed port) keeps
        that assignment without re-running conflict/bind checks -- so a
        service can safely re-request its own already-bound port. A changed
        fixed request, or a brand-new name, is checked and (re)assigned. A
        name previously assigned to this service but absent from
        ``requests`` is dropped.
        """
        for name, port in requests.items():
            if port != 0 and not (MIN_PORT <= port <= MAX_PORT):
                raise ValueError(f"port {name!r}={port} out of range [{MIN_PORT}, {MAX_PORT}]")

        current = dict(self._assignments.get(service_id, {}))
        used_elsewhere = self._ports_used_by_others(service_id)
        result: dict[str, int] = {}
        pending_auto: list[str] = []

        for name, requested in requests.items():
            existing = current.get(name)
            if requested == 0:
                if existing is not None:
                    result[name] = existing
                else:
                    pending_auto.append(name)
                continue
            if existing == requested:
                result[name] = existing
                continue
            owner = used_elsewhere.get(requested)
            if owner is not None:
                raise PortConflict(
                    f"port {requested} requested by {service_id!r} as {name!r} is "
                    f"already assigned to {owner!r}"
                )
            if self.bind_check and not is_port_free(requested):
                raise PortConflict(
                    f"port {requested} requested by {service_id!r} as {name!r} is "
                    "bound by another process on the host"
                )
            result[name] = requested

        for name in pending_auto:
            result[name] = self._pick_free(used_elsewhere, result)

        self._assignments[service_id] = result
        self._save()
        return dict(result)

    def _pick_free(self, used_elsewhere: dict[int, str], already_picked: dict[str, int]) -> int:
        taken = set(used_elsewhere) | set(already_picked.values())
        lo, hi = self.range
        for port in range(lo, hi + 1):
            if port in taken:
                continue
            if self.bind_check and not is_port_free(port):
                continue
            return port
        raise PortConflict(f"no free port in auto-allocation range {lo}..{hi}")
