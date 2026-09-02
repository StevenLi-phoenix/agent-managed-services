"""One-shot platform sync: drive every Layer-1 service to the declared commit.

This is the *mechanical* half of PLAN-allin Q7. It fetches the api monorepo,
translates every ``service.yaml`` it finds, stages the tree, provisions the
runtime, writes the declaration and its two sidecars, reloads the harness,
renders the gateway, creates the registry identity and waits for the health
probe. Everything it cannot decide -- whether a translate failure means "fix
the manifest" or "extend the translator", whether a crash-looping service is
rolled back -- it escalates as one JSON line on stdout and leaves alone.

**It is a process, not a loop.** ``git fetch`` and ``uv sync`` block for seconds
to minutes and the supervisor is single-threaded (D17), so this runs from a
60 s systemd timer (``deploy/ams-platform-sync.timer``) and talks to the running
harness over the control socket like any other client.

Per-service progress is persisted in ``<state>/platform/state.json``, whose
shape is fixed by ``docs/platform-sidecars.md``::

    fetched -> translated -> provisioned -> declared -> reloaded -> registered -> healthy
                                                                              -> failed

``failed`` is terminal for the tick, not for the service: every service has its
own record and one failure never stops the others. ``error`` is written as
``"<transition>: <message>"`` so the record still names which transition raised
(the sidecar doc's "``error`` says which") without extending the documented
shape with a second field.

Two properties the 60 s timer depends on, and which shape most of the code:

- **A stage never moves backwards.** Every tick re-walks every phase, so a
  service that is already ``healthy`` at this sha passes through the staging and
  declaration phases as a sequence of no-ops without its record being rewritten.
- **A tick that changes nothing writes nothing** -- no state file, no
  declaration, no reload, no registry call, no probe. ``PlatformState.flush``
  is change-gated for exactly this reason.

Ordering inside one run is deliberately two-pass around the reload:

1. per service: translate, stage, provision, write declaration + sidecars;
2. **one** ``ams ctl reload`` -- which is what allocates ports for services the
   harness has never seen;
3. gateway render/write against the *now* complete port allocation, then
   ``ams ctl restart caddy`` if any gateway file changed;
4. per service: registry identity + ACL, then the health gate.

Rendering the gateway before the reload would resolve ``mount.port_name``
against an allocation that does not yet contain a brand-new service, and
``gateway.resolve_ports`` raises rather than rendering a ``reverse_proxy`` to
nothing (D21).

Safety: ``SyncConfig`` re-uses ``TranslateContext``'s loopback gate on both
URLs, so an auto-generated ``SVC_SECRET`` and an identity creation can never be
pointed at production (PLAN-allin risk 5). A generated secret goes straight
from ``secrets.token_hex`` into the SecretStore and is never logged, printed or
put in the report.
"""

from __future__ import annotations

import json
import logging
import os
import secrets as _secrets
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

from ams.platform import gateway as gateway_mod
from ams.platform import sources
from ams.platform.bootstrap import AUTH_ID, LAYER0_PORTS, REGISTRY_ID, BootstrapError, loopback_url
from ams.platform.bootstrap import place_jwt_key as place_jwt_key  # noqa: PLC0414 - patch point
from ams.platform.registryclient import RegistryClient, RegistryError, from_sidecar
from ams.platform.sources import SourceError, SourceMirror
from ams.platform.static import StaticError, load_ams_overlay, publish_static
from ams.platform.translate import (
    TranslateContext,
    TranslateError,
    Translation,
    emit_toml,
    translate,
)
from ams.runtime import RuntimeStore, python_venv_dir
from ams.runtime import provision as provision  # noqa: PLC0414 - monkeypatch point
from ams.schema import DeclError, ServiceDecl
from ams.secrets import MissingSecret, SecretStore, store_for
from ams.state import StateDir, read_json_checked, write_json_atomic
from ams.uidmap import UidAllocator, UidBlock

log = logging.getLogger("ams.platform.sync")

EXIT_OK = 0
EXIT_ERROR = 1

STATE_VERSION = 1
PLATFORM_DIRNAME = "platform"
STATE_FILENAME = "state.json"
MOUNTS_DIRNAME = "mounts"
REGISTRY_DIRNAME = "registry"

# `service.ams.toml` beside a manifest carries what the manifest cannot express
# (PLAN-allin Q2): extra secret *names* (never values -- D16) and the rare
# non-secret `[env]` tunable. T3.4's `load_ams_overlay` is the only reader.
SVC_SECRET_NAME = "SVC_SECRET"
SVC_SECRET_BYTES = 32

#: Layer-0 ports are fixed, not allocated (D22).
DEFAULT_REGISTRY_URL = loopback_url(LAYER0_PORTS[REGISTRY_ID])
DEFAULT_AUTH_URL = loopback_url(LAYER0_PORTS[AUTH_ID])

DEFAULT_MANIFEST_GLOBS: tuple[str, ...] = ("services/*/service.yaml", "apps/*/service.yaml")

#: A change under one of these redeploys *every* service. `shared/` is the
#: deployer's own fan-out lever, verbatim from
#: `api/components/deployer/src/deployer/changes.py`. `components/sdk/` is the
#: one addition ams needs and the deployer does not -- see D26.
DEFAULT_SHARED_PREFIXES: tuple[str, ...] = ("shared/", "components/sdk/")

#: Stage names in order. ``failed`` is off to the side (see the module docstring).
STAGES: tuple[str, ...] = (
    "fetched",
    "translated",
    "provisioned",
    "declared",
    "reloaded",
    "registered",
    "healthy",
)
FAILED = "failed"
#: Stages that mean "the harness is already running this declaration".
LIVE_STAGES = frozenset({"reloaded", "registered", "healthy"})

ESCALATION_KIND = "PlatformSync"
PLAN_KIND = "PlatformSyncPlan"


class SyncError(RuntimeError):
    """A run-level precondition is wrong (bad config, unusable state file)."""


# --------------------------------------------------------------------------- paths


def platform_dir(state: StateDir) -> Path:
    return Path(state.root) / PLATFORM_DIRNAME


def state_path(state: StateDir) -> Path:
    return platform_dir(state) / STATE_FILENAME


def mounts_dir(state: StateDir) -> Path:
    return platform_dir(state) / MOUNTS_DIRNAME


def registry_dir(state: StateDir) -> Path:
    return platform_dir(state) / REGISTRY_DIRNAME


# --------------------------------------------------------------------------- config


@dataclass(frozen=True)
class SyncConfig:
    """Everything one run needs that is not on disk already.

    ``registry_admin_token_from`` is ``(service_id, secret_name)`` in the
    SecretStore, not a value: the admin token is read in-process at the moment
    the registry is called and never crosses a process boundary (D16).
    """

    repo_url: str
    ref: str = "main"
    registry_url: str = DEFAULT_REGISTRY_URL
    auth_url: str = DEFAULT_AUTH_URL
    registry_admin_token_from: tuple[str, str] = ("registry", "REGISTRY_ADMIN_TOKEN")
    include: tuple[str, ...] | None = None
    exclude: tuple[str, ...] = ()
    manifest_globs: tuple[str, ...] = DEFAULT_MANIFEST_GLOBS
    #: Path prefixes whose change redeploys every service (D26).
    shared_prefixes: tuple[str, ...] = DEFAULT_SHARED_PREFIXES
    repo_name: str = "api"
    dry_run: bool = False
    fetch_timeout_s: float = sources.DEFAULT_FETCH_TIMEOUT_S
    health_deadline_s: float = 90.0
    health_interval_s: float = 1.0
    ctl_timeout_s: float = 60.0
    gateway_entry_host: str = "127.0.0.1"
    #: Caps handed to every translation. Defaults match PLAN-allin Q8.
    default_memory_max: str = "150M"
    memory_floor: str = "120M"
    cpu_max: str = "40%"
    pids_max: int = 64
    start_period_s: float = 120.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "exclude", tuple(self.exclude))
        object.__setattr__(self, "manifest_globs", tuple(self.manifest_globs))
        object.__setattr__(self, "shared_prefixes", tuple(self.shared_prefixes))
        if self.include is not None:
            object.__setattr__(self, "include", tuple(self.include))
        token_from = tuple(self.registry_admin_token_from)
        if len(token_from) != 2:
            raise SyncError("registry_admin_token_from must be (service_id, secret_name)")
        object.__setattr__(self, "registry_admin_token_from", token_from)
        # The loopback gate is TranslateContext's (PLAN-allin risk 5) and there
        # must be exactly one of them, so it is exercised here rather than
        # re-implemented: a bad URL fails before anything is fetched instead of
        # once per manifest, and the two can never disagree.
        try:
            self.context_for(sha="0" * 40, services_dir=Path("/nonexistent"))
        except TranslateError as e:
            raise SyncError(str(e)) from None

    def context_for(
        self,
        *,
        sha: str,
        services_dir: Path,
        extra_secret_names: Sequence[str] = (),
    ) -> TranslateContext:
        return TranslateContext(
            sha=sha,
            services_dir=services_dir,
            registry_url=self.registry_url,
            auth_url=self.auth_url,
            extra_secret_names=tuple(extra_secret_names),
            default_memory_max=self.default_memory_max,
            memory_floor=self.memory_floor,
            cpu_max=self.cpu_max,
            pids_max=self.pids_max,
            start_period_s=self.start_period_s,
        )

    def selects(self, *names: str) -> bool:
        """True when any of ``names`` (manifest dir name, service id) is wanted.

        Both are accepted because ``--only`` is typed by a human who is looking
        at a directory listing, and the id only exists after the manifest
        parses -- which is exactly what a filter has to work without when the
        manifest is the thing that is broken.
        """
        wanted = [n for n in names if n]
        if any(n in self.exclude for n in wanted):
            return False
        if self.include is None:
            return True
        return any(n in self.include for n in wanted)


# --------------------------------------------------------------------------- state


def _stamp(now_s: float) -> str:
    return datetime.fromtimestamp(now_s, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class ServiceRecord:
    """One row of ``platform-state.json``. Field names are the file's keys."""

    sha: str | None = None
    prev_sha: str | None = None
    #: The commit whose tree is actually on disk for this service. Additive to
    #: the version-1 shape (D26); consumers read records with `.get`.
    deployed_sha: str | None = None
    stage: str = FAILED
    error: str | None = None
    manual_restart: bool = False
    escalated: bool = False
    updated_at: str = ""
    stage_since: str = ""

    def as_json(self) -> dict[str, Any]:
        return asdict(self)


class PlatformState:
    """The whole fleet's sync progress, loaded once and flushed when it changes.

    Flushing is change-gated on purpose: "a tick that changes nothing writes
    nothing" is a property the timer depends on, and an ``updated_at`` touched
    on every tick would destroy it.
    """

    def __init__(self, path: Path, records: dict[str, ServiceRecord]) -> None:
        self.path = path
        self.records = records
        self._flushed = self._snapshot()

    @classmethod
    def load(cls, path: Path) -> PlatformState:
        if not path.exists():
            return cls(path, {})
        data = read_json_checked(path)
        if not isinstance(data, dict):
            raise SyncError(f"{path}: expected a JSON object at top level")
        version = data.get("version")
        if version != STATE_VERSION:
            # The sidecar contract: a consumer reading a version it does not
            # know must fail loudly rather than guess.
            raise SyncError(f"{path}: unsupported version {version!r}; expected {STATE_VERSION}")
        services = data.get("services")
        if not isinstance(services, dict):
            raise SyncError(f"{path}: 'services' must be an object")
        records: dict[str, ServiceRecord] = {}
        for service_id, row in services.items():
            if not isinstance(row, dict):
                raise SyncError(f"{path}: services.{service_id} must be an object")
            known = {f: row[f] for f in ServiceRecord.__dataclass_fields__ if f in row}
            records[service_id] = ServiceRecord(**known)
        return cls(path, records)

    def _snapshot(self) -> str:
        return json.dumps(self.as_json(), sort_keys=True)

    def as_json(self) -> dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "services": {sid: rec.as_json() for sid, rec in sorted(self.records.items())},
        }

    def get(self, service_id: str) -> ServiceRecord:
        rec = self.records.get(service_id)
        if rec is None:
            rec = ServiceRecord()
            self.records[service_id] = rec
        return rec

    def dirty(self) -> bool:
        return self._snapshot() != self._flushed

    def flush(self) -> bool:
        """Write the file iff a record actually changed. Returns whether it did."""
        if not self.dirty():
            return False
        write_json_atomic(self.path, self.as_json())
        self._flushed = self._snapshot()
        log.debug("wrote %s", self.path)
        return True

    def begin(self, service_id: str, sha: str, *, manual_restart: bool, now_s: float) -> str:
        """Point a service at ``sha``; return the stage it was at before.

        A record already at this sha is left exactly as it is (that is what
        makes a steady-state tick free); a new sha rewinds it to ``fetched`` and
        remembers the last healthy commit in ``prev_sha``; a ``failed`` record
        is rewound so the tick retries from the top.
        """
        rec = self.get(service_id)
        prior = rec.stage
        rec.manual_restart = manual_restart
        if rec.sha != sha:
            if rec.stage == "healthy" and rec.sha:
                rec.prev_sha = rec.sha
            rec.sha = sha
            rec.updated_at = _stamp(now_s)
            self.set_stage(service_id, STAGES[0], now_s=now_s)
        # A `failed` record is deliberately NOT rewound to `fetched`: the phases
        # advance out of it on their own (a failed stage sorts below every real
        # one), and rewinding would move `stage_since` and clear `escalated` on
        # every tick -- turning "escalate once per cause" into "escalate per
        # tick", which is the exact failure T3.3 exists to prevent.
        return prior

    def set_stage(
        self, service_id: str, stage: str, *, now_s: float, error: str | None = None
    ) -> None:
        rec = self.get(service_id)
        key_before = (rec.sha, rec.stage, rec.error)
        if rec.stage != stage:
            rec.stage_since = _stamp(now_s)
        rec.stage = stage
        rec.error = error
        if key_before != (rec.sha, rec.stage, rec.error):
            # A new (sha, stage, error) is a new cause: it may escalate again,
            # and it is the only thing that touches `updated_at`. Stamping every
            # tick would rewrite the file on a run that changed nothing.
            rec.escalated = False
            rec.updated_at = _stamp(now_s)


def _stage_index(stage: str) -> int:
    """Position in the pipeline; ``failed`` and anything unknown sort below all."""
    return STAGES.index(stage) if stage in STAGES else -1


# --------------------------------------------------------------------------- report


@dataclass(frozen=True)
class ServiceOutcome:
    id: str
    kind: str
    stage: str
    sha: str | None
    prev_sha: str | None = None
    error: str | None = None
    changed: bool = False
    actions: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.stage != FAILED


@dataclass(frozen=True)
class SyncReport:
    sha: str | None = None
    services: tuple[ServiceOutcome, ...] = ()
    escalations: tuple[Mapping[str, Any], ...] = ()
    gateway_changed: tuple[str, ...] = ()
    reloaded: bool = False
    caddy_restarted: bool = False
    state_written: bool = False
    error: str | None = None

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(o.id for o in self.services)

    @property
    def unchanged(self) -> tuple[str, ...]:
        """Ids this run had nothing to do for. ``== report.ids`` means a no-op."""
        return tuple(o.id for o in self.services if not o.changed)

    @property
    def failed(self) -> tuple[str, ...]:
        return tuple(o.id for o in self.services if not o.ok)

    @property
    def ok(self) -> bool:
        return self.error is None and not self.failed

    @property
    def exit_code(self) -> int:
        return EXIT_OK if self.ok else EXIT_ERROR

    def outcome(self, service_id: str) -> ServiceOutcome:
        for o in self.services:
            if o.id == service_id:
                return o
        raise KeyError(service_id)

    def summary(self) -> str:
        return (
            f"sha={(self.sha or '-')[:12]} services={len(self.services)} "
            f"unchanged={len(self.unchanged)} failed={len(self.failed)} "
            f"reloaded={self.reloaded} gateway_changed={len(self.gateway_changed)}"
        )


# --------------------------------------------------------------------------- seams
#
# These are module-level so a test can replace them without a Linux host: they
# are the only calls that fork into a user namespace or reach the running
# harness. ``place_jwt_key`` and ``provision`` are imported above for the same
# reason, and ``SourceMirror.stage`` is patched on the class.


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


# --------------------------------------------------------------------------- helpers


def _emit(stream: IO[str], record: Mapping[str, Any]) -> None:
    stream.write(json.dumps(record, default=str) + "\n")
    stream.flush()


def _record(
    *,
    service_id: str | None,
    stage: str,
    error: str,
    sha: str | None,
    prev_sha: str | None = None,
    kind: str = ESCALATION_KIND,
    action: str = "escalate",
) -> dict[str, Any]:
    return {
        "kind": kind,
        "service_id": service_id,
        "action": action,
        "reason": error,
        "event": {
            "service_id": service_id,
            "stage": stage,
            "error": error,
            "sha": sha,
            "prev_sha": prev_sha,
        },
    }


def _read_sha_marker(repo: Path) -> str | None:
    try:
        text = (repo / sources.SHA_MARKER).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def _write_if_changed(path: Path, content: str) -> bool:
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
    return True


def _write_json_if_changed(path: Path, data: Mapping[str, Any]) -> bool:
    return _write_if_changed(path, json.dumps(dict(data), indent=2, sort_keys=True) + "\n")


def _is_disabled(path: Path, root: Path) -> bool:
    """``*.disabled`` anywhere in the path below the checkout root."""
    return any(part.endswith(".disabled") for part in path.relative_to(root).parts)


def discover_manifests(checkout: Path, globs: Iterable[str]) -> list[Path]:
    """Every manifest in the checkout, sorted, skipping ``*.disabled`` paths."""
    found: set[Path] = set()
    for pattern in globs:
        for path in checkout.glob(pattern):
            if path.is_file() and not _is_disabled(path, checkout):
                found.add(path)
    return sorted(found)


# --------------------------------------------------------------------------- the run


@dataclass
class _Pending:
    """One selected manifest, carried between the run's phases."""

    manifest: Path
    translation: Translation
    decl: ServiceDecl | None
    decl_text: str | None
    #: The commit this service is being driven to *this tick*. Equal to the repo
    #: head for an affected service, and to its already-deployed sha otherwise --
    #: which is what keeps an untouched service's declaration byte-identical.
    sha: str = ""
    #: The record as it was before this tick touched it. `fail()` dedupes
    #: against this, not against the live record, which it has already written.
    prior_stage: str = FAILED
    prior_sha: str | None = None
    prior_error: str | None = None
    prior_escalated: bool = False
    actions: list[str] = field(default_factory=list)
    changed: bool = False
    reached: str = STAGES[0]
    failed_with: str | None = None
    health_path: str = "/health"

    @property
    def id(self) -> str:
        return self.translation.id

    @property
    def kind(self) -> str:
        return self.translation.kind

    @property
    def manual_restart(self) -> bool:
        return bool(self.translation.flags.get("manual_restart", False))


class _Run:
    """One sync tick: the state file, the escalation stream and the work list."""

    def __init__(
        self,
        state: StateDir,
        store: RuntimeStore,
        cfg: SyncConfig,
        *,
        secrets: SecretStore,
        uids: UidAllocator | None,
        mirror: SourceMirror,
        checkout: Path,
        now: Callable[[], float],
        stream: IO[str],
    ) -> None:
        self.state = state
        self.store = store
        self.cfg = cfg
        self.secrets = secrets
        self.mirror = mirror
        #: ``<store>/src/<name>/<sha>`` -- what `publish_static` reads from.
        self.checkout = checkout
        self.head_sha = checkout.name
        self._diffs: dict[tuple[str, str], list[str] | None] = {}
        self._uids = uids
        self.now = now
        self.stream = stream
        self.escalations: list[Mapping[str, Any]] = []
        self.platform = PlatformState.load(state_path(state))
        self.pending: list[_Pending] = []

    # ------------------------------------------------------------ escalation

    def escalate(
        self, *, service_id: str | None, stage: str, error: str, sha: str | None
    ) -> Mapping[str, Any]:
        rec = self.platform.records.get(service_id) if service_id else None
        record = _record(
            service_id=service_id,
            stage=stage,
            error=error,
            sha=sha,
            prev_sha=rec.prev_sha if rec else None,
        )
        self.escalations.append(record)
        _emit(self.stream, record)
        log.error("escalating %s at %s: %s", service_id or "<run>", stage, error)
        return record

    def fail(self, item: _Pending, transition: str, message: str, sha: str) -> None:
        """Mark one service failed, escalating once per ``(sha, stage, error)``."""
        error = f"{transition}: {message}"
        already = (
            item.prior_escalated
            and item.prior_stage == FAILED
            and item.prior_error == error
            and item.prior_sha == sha
        )
        self.platform.set_stage(item.id, FAILED, now_s=self.now(), error=error)
        item.failed_with = error
        item.changed = True
        if already:
            # The cause has not changed since the last tick: nothing is emitted.
            # This is the "escalates once, not per tick" guarantee T3.3 needs.
            log.info("%s still failing (%s); already escalated", item.id, error)
        else:
            self.escalate(service_id=item.id, stage=transition, error=error, sha=sha)
        self.platform.get(item.id).escalated = True

    def advance(self, item: _Pending, stage: str) -> None:
        """Record forward progress. Never moves a record backwards."""
        rec = self.platform.get(item.id)
        if _stage_index(stage) > _stage_index(rec.stage):
            self.platform.set_stage(item.id, stage, now_s=self.now())
        item.reached = stage

    # -------------------------------------------------------- change detection

    def changed_paths(self, old: str, new: str) -> list[str] | None:
        """Cached ``git diff --name-only``. ``None`` = the diff is unavailable."""
        key = (old, new)
        if key not in self._diffs:
            try:
                self._diffs[key] = self.mirror.changed_paths(old, new)
            except SourceError as e:
                # A commit the mirror no longer has (gc'd, force-pushed away).
                # Not knowing what changed is not the same as nothing changing.
                log.warning(
                    "cannot diff %s..%s (%s); treating every service as affected",
                    old[:12],
                    new[:12],
                    e,
                )
                self._diffs[key] = None
        return self._diffs[key]

    def deployed_sha(self, service_id: str) -> str | None:
        """The commit whose tree is actually on disk for ``service_id``.

        The record is authoritative; the on-disk ``.ams-sha`` marker is the
        fallback, so a state file written before this field existed (or one
        restored from a backup) heals itself on the next tick instead of
        redeploying the fleet.
        """
        rec = self.platform.records.get(service_id)
        if rec is not None and rec.deployed_sha:
            return rec.deployed_sha
        return _read_sha_marker(self.state.service_root(service_id) / "repo") or _read_sha_marker(
            gateway_mod.static_root(self.state) / service_id
        )

    def target_sha(self, manifest: Path, service_id: str) -> str:
        """The sha to translate ``service_id`` at: the head, or its deployed one.

        The rule is the deployer's (`components/deployer/src/deployer/changes.py`):
        path-prefix matching of the commit's changed files against the service's
        own directory, plus a set of shared prefixes that fan out to everything.
        A service with nothing deployed, or whose deployed commit the mirror can
        no longer diff against, is affected by definition.
        """
        deployed = self.deployed_sha(service_id)
        if not deployed or deployed == self.head_sha:
            return self.head_sha
        changed = self.changed_paths(deployed, self.head_sha)
        if changed is None:
            return self.head_sha
        try:
            own = manifest.parent.relative_to(self.checkout).as_posix() + "/"
        except ValueError:  # pragma: no cover - the manifest came from the checkout
            return self.head_sha
        prefixes = (own, *self.cfg.shared_prefixes)
        if any(path.startswith(prefixes) for path in changed):
            return self.head_sha
        log.info(
            "%s: nothing under %s or %s changed since %s; staying at it",
            service_id,
            own,
            "/".join(self.cfg.shared_prefixes) or "(no shared prefix)",
            deployed[:12],
        )
        return deployed

    # ------------------------------------------------------------------ uids

    def block_for(self, service_id: str) -> UidBlock:
        if self._uids is None:
            self._uids = uid_allocator(self.state)
        return self._uids.allocate(service_id)

    # --------------------------------------------------------------- outcome

    def outcome_for(self, item: _Pending) -> ServiceOutcome:
        rec = self.platform.get(item.id)
        return ServiceOutcome(
            id=item.id,
            kind=item.kind,
            stage=rec.stage,
            sha=rec.sha,
            prev_sha=rec.prev_sha,
            error=rec.error,
            changed=item.changed,
            actions=tuple(item.actions),
        )


# --------------------------------------------------------------------------- phases


def _apply_overlay_env(decl: ServiceDecl | None, env: Mapping[str, str]) -> ServiceDecl | None:
    """Fold ``service.ams.toml``'s ``[env]`` into the declaration.

    T3.4 parses and validates the table but leaves the merge to its caller, and
    this is the caller. ``dataclasses.replace`` re-runs ``ServiceDecl``'s
    ``__post_init__``, so a name that collides with a declared secret, or is
    reserved, raises ``DeclError`` here instead of at spawn time.
    """
    if decl is None or not env:
        return decl
    return replace(decl, env={**decl.env, **env})


def _snapshot_prior(run: _Run, item: _Pending, service_id: str) -> None:
    rec = run.platform.records.get(service_id)
    if rec is None:
        return
    item.prior_stage = rec.stage
    item.prior_sha = rec.sha
    item.prior_error = rec.error
    item.prior_escalated = rec.escalated


def _phase_translate(run: _Run, manifests: Sequence[Path], sha: str, services_dir: Path) -> None:
    """Parse every selected manifest. A broken one fails only itself."""
    for manifest in manifests:
        dirname = manifest.parent.name
        try:
            overlay = load_ams_overlay(manifest.parent)
            ctx = run.cfg.context_for(
                sha=sha, services_dir=services_dir, extra_secret_names=overlay.secrets
            )
            translation = translate(manifest.read_text(encoding="utf-8"), ctx)
            decl = _apply_overlay_env(translation.decl, overlay.env)
        except (TranslateError, StaticError, DeclError, OSError, UnicodeDecodeError) as e:
            if not run.cfg.selects(dirname):
                log.debug("%s is not selected; ignoring its translate error", manifest)
                continue
            # The id is what the manifest failed to give us, so the record is
            # keyed by the directory -- which is what an operator greps for.
            item = _Pending(
                manifest=manifest,
                translation=Translation(
                    id=dirname, kind="service", decl=None, mount={}, registry=None
                ),
                decl=None,
                decl_text=None,
                sha=sha,
            )
            _snapshot_prior(run, item, dirname)
            item.prior_stage = run.platform.begin(
                dirname, sha, manual_restart=False, now_s=run.now()
            )
            run.fail(item, "translate", f"{type(e).__name__}: {e}", sha)
            run.pending.append(item)
            continue

        if not run.cfg.selects(dirname, translation.id):
            log.debug("%s not selected", translation.id)
            continue

        # The id only exists once the manifest parses, and the affected check is
        # keyed by it -- so translate at the head first, then re-translate at the
        # deployed sha for a service this commit did not touch. `translate` is
        # pure and sub-millisecond; the alternative is guessing the id from the
        # directory name, which nothing else in this module does.
        target = run.target_sha(manifest, translation.id)
        if target != sha:
            try:
                ctx = run.cfg.context_for(
                    sha=target, services_dir=services_dir, extra_secret_names=overlay.secrets
                )
                translation = translate(manifest.read_text(encoding="utf-8"), ctx)
                decl = _apply_overlay_env(translation.decl, overlay.env)
            except (TranslateError, DeclError, OSError, UnicodeDecodeError) as e:
                # It translated at the head a moment ago, so this is not a
                # manifest problem: fall back to the head rather than skip it.
                log.warning(
                    "%s: re-translating at %s failed (%s); using the head",
                    translation.id,
                    target[:12],
                    e,
                )
                target = sha

        item = _Pending(
            manifest=manifest,
            translation=translation,
            decl=decl,
            decl_text=emit_toml(decl) if decl is not None else None,
            sha=target,
            health_path=str((translation.registry or {}).get("health_path", "/health")),
        )
        _snapshot_prior(run, item, item.id)
        item.prior_stage = run.platform.begin(
            item.id, target, manual_restart=item.manual_restart, now_s=run.now()
        )
        run.advance(item, "translated")
        run.pending.append(item)


def _publish_static_site(run: _Run, item: _Pending) -> bool:
    """Publish a ``kind: static`` site. This is a static mount's `provisioned`.

    A uid block is allocated only when the manifest declares build steps: the
    published tree is harness-owned and never chowned to a service uid (D25), so
    the block exists purely to give ``run_admin`` a complete two-range map --
    and a site with no build steps should not consume one of the 64.
    """
    build = list(item.translation.mount.get("build") or [])
    block: UidBlock | None = None
    if build:
        try:
            block = run.block_for(item.id)
        except (RuntimeError, OSError, ValueError) as e:
            run.fail(item, "provision", f"uid block: {type(e).__name__}: {e}", item.sha)
            return False
    target = gateway_mod.static_root(run.state) / item.id
    if _read_sha_marker(target) == item.sha:
        # Already published at the sha this service is being driven to. Returning
        # here (rather than letting `publish_static` no-op) also means an
        # unaffected site never needs its old checkout re-materialised.
        run.advance(item, "provisioned")
        return True
    try:
        publish_static(item.translation.mount, run.checkout, run.state, run.store, block)
    except (StaticError, OSError) as e:
        run.fail(item, "provision", f"publish: {type(e).__name__}: {e}", item.sha)
        return False
    item.actions.append("publish")
    item.changed = True
    run.advance(item, "provisioned")
    return True


def _phase_materialize(run: _Run, item: _Pending) -> bool:
    """Stage the tree, place the JWT key, provision. False = this one failed."""
    if item.kind == "static":
        return _publish_static_site(run, item)
    if item.decl is None:
        return True
    root = run.state.service_root(item.id)
    try:
        block = run.block_for(item.id)
    except (RuntimeError, OSError, ValueError) as e:
        run.fail(item, "provision", f"uid block: {type(e).__name__}: {e}", item.sha)
        return False

    at_sha = _read_sha_marker(root / "repo") == item.sha
    try:
        run.mirror.stage(item.sha, root, block)
        if not at_sha:
            item.actions.append("stage")
            item.changed = True
        place_jwt_key(run.state, item.id, block, store=run.store)
    except (SourceError, BootstrapError, OSError, RuntimeError) as e:
        run.fail(item, "provision", f"stage: {type(e).__name__}: {e}", item.sha)
        return False

    venv = python_venv_dir(item.decl, root)
    if at_sha and venv.is_dir():
        log.debug(
            "%s: already at %s with %s; skipping provision", item.id, item.sha[:12], venv.name
        )
    else:
        try:
            provision(
                item.decl,
                root,
                run.store,
                block,
                log_path=run.state.logs_dir / f"{item.id}-provision.log",
            )
        except Exception as e:  # noqa: BLE001 - any provisioning failure is this service's
            run.fail(item, "provision", f"{type(e).__name__}: {e}", item.sha)
            return False
        item.actions.append("provision")
        item.changed = True
    run.advance(item, "provisioned")
    return True


def _phase_declare(run: _Run, item: _Pending) -> bool:
    """Write the sidecars, generate ``SVC_SECRET``, write ``service.toml``."""
    try:
        if _write_json_if_changed(
            mounts_dir(run.state) / f"{item.id}.json", item.translation.mount
        ):
            item.actions.append("mount")
            item.changed = True
        if item.translation.registry is not None and _write_json_if_changed(
            registry_dir(run.state) / f"{item.id}.json", item.translation.registry
        ):
            item.actions.append("registry-sidecar")
            item.changed = True
    except OSError as e:
        run.fail(item, "declare", f"sidecar: {type(e).__name__}: {e}", item.sha)
        return False

    if item.kind != "service" or item.decl is None or item.decl_text is None:
        # A static site has no process and no registry record, so `declared` is
        # where its state machine ends: claiming `healthy` would claim a probe
        # that does not exist.
        run.advance(item, "declared")
        run.platform.get(item.id).deployed_sha = item.sha
        return True

    # The secret must exist before the declaration does: a declaration listing
    # SVC_SECRET with no stored value fails at spawn (D16), and the reload two
    # phases from here is what starts the service.
    try:
        if SVC_SECRET_NAME not in run.secrets.names(item.id):
            run.secrets.set(
                item.id, SVC_SECRET_NAME, _secrets.token_hex(SVC_SECRET_BYTES).encode("ascii")
            )
            item.actions.append("secret")
            item.changed = True
    except (OSError, ValueError) as e:
        run.fail(item, "declare", f"{SVC_SECRET_NAME}: {type(e).__name__}: {e}", item.sha)
        return False

    missing = run.secrets.missing(item.id, item.decl.secrets)
    if missing:
        # Not this loop's failure to fix: third-party keys are set by hand once
        # (PLAN-allin "what stays manual"). The service fails at its own start.
        log.warning("%s: declared secret(s) with no value: %s", item.id, ", ".join(missing))

    path = run.state.service_decl_path(item.id)
    try:
        current: str | None = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        current = None
    if current == item.decl_text:
        run.advance(item, "declared")
        run.platform.get(item.id).deployed_sha = item.sha
        return True

    if item.manual_restart and current is not None:
        # Writing it would make `ams ctl reload` restart the service, and
        # manual_restart exists precisely to stop a core service restarting
        # itself mid-deploy. Report it and leave the live declaration alone.
        item.actions.append("manual-restart-pending")
        run.fail(item, "declare", "declaration changed but manual_restart is set", item.sha)
        return False

    try:
        _write_if_changed(path, item.decl_text)
    except OSError as e:
        run.fail(item, "declare", f"service.toml: {type(e).__name__}: {e}", item.sha)
        return False
    item.actions.append("declare")
    item.changed = True
    run.advance(item, "declared")
    run.platform.get(item.id).deployed_sha = item.sha
    return True


def _phase_reload(run: _Run, live: Sequence[_Pending], sha: str) -> tuple[bool, str | None]:
    """One reload for the whole fleet, iff a declaration is new or changed."""
    needed = [
        item
        for item in live
        if item.kind == "service"
        and ("declare" in item.actions or item.prior_stage not in LIVE_STAGES)
    ]
    if not needed:
        # Nothing was written, so whatever the harness is running already is
        # this declaration.
        _mark_reloaded(run, live)
        return False, None
    try:
        response = ctl_reload(run.state, timeout_s=run.cfg.ctl_timeout_s)
        if not response.get("ok"):
            raise SyncError(f"reload refused: {response.get('error') or response}")
    except Exception as e:  # noqa: BLE001 - ControlError is behind a lazy import
        error = f"{type(e).__name__}: {e}"
        run.escalate(service_id=None, stage="reload", error=error, sha=sha)
        return False, error
    _mark_reloaded(run, live)
    return True, None


def _mark_reloaded(run: _Run, live: Sequence[_Pending]) -> None:
    """``declared`` -> ``reloaded`` for services only.

    A ``kind: static`` mount has no process and no registry record, so
    ``declared`` is the end of its state machine (`docs/platform-sidecars.md`
    gives it neither a reload nor a probe).
    """
    for item in live:
        if item.kind == "service" and item.reached == "declared":
            run.advance(item, "reloaded")


def load_mounts(state: StateDir) -> list[dict[str, Any]]:
    """Every ``mount.json`` sidecar on disk, sorted by id.

    The gateway is fleet-wide state, so it is rendered from what is *declared*,
    not from the subset one tick happened to process: rendering from the run's
    own list would drop the ``sites/<id>.caddy`` of every service excluded by
    ``--only``, or of one whose manifest broke this tick, and take a working
    route off the gateway because of an unrelated typo.
    """
    out: list[dict[str, Any]] = []
    for path in sorted(mounts_dir(state).glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
            log.warning("ignoring unreadable mount sidecar %s: %s", path.name, e)
            continue
        if isinstance(data, dict):
            out.append(data)
    return out


def _phase_gateway(run: _Run) -> tuple[list[str], bool]:
    """Render and write the Caddy config, restarting Caddy when it changed.

    Strictly after the reload: ``resolve_ports`` follows ``mount.port_name``
    into the *live* allocation, and a service the harness saw for the first time
    in this run has no port until the reload registers it (D21).
    """
    from ams.ports import PortAllocator

    all_mounts = load_mounts(run.state)
    if not all_mounts:
        return [], False
    ports = PortAllocator(run.state.ports_state)
    listen = ports.get(gateway_mod.CADDY_SERVICE_ID).get("main")
    if listen is None:
        log.warning(
            "no allocated port for %r; skipping the gateway render (Layer 0 is not up yet -- T3.2)",
            gateway_mod.CADDY_SERVICE_ID,
        )
        return [], False

    # A declared service with no allocated port yet (its first reload has not
    # happened, or failed) is left out rather than raised on: one such service
    # must not stop the gateway being rendered for the other twenty.
    mounts: list[dict[str, Any]] = []
    for mount in all_mounts:
        service_id = str(mount.get("id") or "")
        if mount.get("kind") == "service" and not ports.get(service_id):
            log.warning("%s: declared but not yet allocated a port; not on the gateway", service_id)
            continue
        mounts.append(mount)
    if not mounts:
        return [], False

    cfg = gateway_mod.GatewayConfig(
        listen_port=listen,
        static_root=gateway_mod.static_root(run.state),
        entry_host=run.cfg.gateway_entry_host,
    )
    try:
        files = gateway_mod.render(mounts, gateway_mod.resolve_ports(mounts, ports), cfg)
        changed = gateway_mod.write(run.state, files)
    except (gateway_mod.GatewayError, OSError) as e:
        run.escalate(
            service_id=gateway_mod.CADDY_SERVICE_ID,
            stage="gateway",
            error=f"{type(e).__name__}: {e}",
            sha=None,
        )
        return [], False
    if not changed:
        return [], False
    names = [p.name for p in changed]
    try:
        response = ctl_restart(
            run.state, gateway_mod.CADDY_SERVICE_ID, timeout_s=run.cfg.ctl_timeout_s
        )
        if not response.get("ok"):
            raise SyncError(f"restart refused: {response.get('error') or response}")
    except Exception as e:  # noqa: BLE001 - ControlError is behind a lazy import
        run.escalate(
            service_id=gateway_mod.CADDY_SERVICE_ID,
            stage="gateway",
            error=f"restart caddy: {type(e).__name__}: {e}",
            sha=None,
        )
        return names, False
    return names, True


def _phase_register(run: _Run, client: RegistryClient, item: _Pending) -> bool:
    reg = item.translation.registry
    if reg is None:
        return True
    try:
        service_id, kwargs, rules = from_sidecar(reg)
        secret = run.secrets.load(item.id, [SVC_SECRET_NAME])[SVC_SECRET_NAME]
        client.create_identity(service_id, secret, **kwargs)
        client.upsert_acl(service_id, rules)
    except (RegistryError, MissingSecret, KeyError) as e:
        run.fail(item, "register", f"{type(e).__name__}: {e}", item.sha)
        return False
    item.actions.append("register")
    run.advance(item, "registered")
    return True


def _phase_health(run: _Run, client: RegistryClient, item: _Pending, port: int) -> bool:
    url = f"http://127.0.0.1:{port}{item.health_path}"
    if client.wait_healthy(url, run.cfg.health_deadline_s, run.cfg.health_interval_s):
        item.actions.append("health")
        run.advance(item, "healthy")
        return True
    run.fail(item, "health", f"{url} not 200 within {run.cfg.health_deadline_s:.0f}s", item.sha)
    return False


def _phase_finish(run: _Run, live: Sequence[_Pending], sha: str) -> None:
    """Registry identity + ACL, then the health gate, for what reached ``reloaded``.

    A service that is unchanged and already ``healthy`` at this sha is skipped
    entirely -- no HTTP round trip to the registry, no probe. That is what makes
    a steady-state tick cost one ``git fetch`` and nothing else.
    """
    from ams.ports import PortAllocator

    # A re-staged tree alone is not a reason to re-register or re-probe: the
    # harness restarts a service only when its declaration's content hash
    # changes (D17), so a service whose declaration is byte-identical is still
    # running the process that was already registered and already healthy.
    work = [
        item
        for item in live
        if item.kind == "service"
        and item.reached == "reloaded"
        and ("declare" in item.actions or run.platform.get(item.id).stage != "healthy")
    ]
    if not work:
        return

    service_id, secret_name = run.cfg.registry_admin_token_from
    try:
        admin_token = run.secrets.load(service_id, [secret_name])[secret_name]
    except MissingSecret as e:
        run.escalate(service_id=None, stage="register", error=str(e), sha=sha)
        return
    client = RegistryClient(run.cfg.registry_url, admin_token)
    ports = PortAllocator(run.state.ports_state)

    for item in work:
        if not _phase_register(run, client, item):
            continue
        allocated = ports.get(item.id).get("main")
        if allocated is None:
            run.fail(item, "health", "no allocated port named 'main'", item.sha)
            continue
        _phase_health(run, client, item, allocated)


# --------------------------------------------------------------------------- entry


def sync(
    state: StateDir,
    store: RuntimeStore,
    cfg: SyncConfig,
    *,
    secrets: SecretStore | None = None,
    uids: UidAllocator | None = None,
    now: Callable[[], float] = time.time,
    stream: IO[str] | None = None,
) -> SyncReport:
    """Drive every selected manifest to ``cfg.ref``. One shot, never a loop.

    ``store`` is the :class:`~ams.runtime.RuntimeStore` -- the reflink store
    holding the source mirror, the tool caches and the replica keypair. The
    SecretStore defaults to the one derived from ``state`` (D16 fixes it at
    ``<state>/secrets``) and ``uids`` to the host allocator reading the same
    file the harness uses, so a block allocated here is the block the service is
    later spawned with.

    Never raises on account of one service: a failure is one escalation on
    ``stream`` plus a ``failed`` record, and the other services still run.
    """
    stream = stream if stream is not None else sys.stdout
    secrets = secrets if secrets is not None else store_for(state)
    mirror = SourceMirror(store.root, cfg.repo_name, url=cfg.repo_url)
    try:
        sha = mirror.fetch(cfg.ref, timeout_s=cfg.fetch_timeout_s)
        checkout = mirror.materialize(sha)
    except (SourceError, OSError) as e:
        # Before the state file is even opened: there is no per-service record
        # to move, so this is one record straight to the stream.
        error = f"{type(e).__name__}: {e}"
        record = _record(service_id=None, stage="fetch", error=error, sha=None)
        _emit(stream, record)
        log.error("escalating <run> at fetch: %s", error)
        return SyncReport(escalations=(record,), error=error)

    run = _Run(
        state,
        store,
        cfg,
        secrets=secrets,
        uids=uids,
        mirror=mirror,
        checkout=checkout,
        now=now,
        stream=stream,
    )

    manifests = discover_manifests(checkout, cfg.manifest_globs)
    log.info("%s -> %s: %d manifest(s)", cfg.ref, sha[:12], len(manifests))

    if cfg.dry_run:
        return _dry_run(run, manifests, sha, state.services_dir)

    state.ensure()
    platform_dir(state).mkdir(parents=True, exist_ok=True)

    _phase_translate(run, manifests, sha, state.services_dir)
    for item in run.pending:
        if item.failed_with:
            continue
        if _phase_materialize(run, item):
            _phase_declare(run, item)
    run.platform.flush()

    live = [item for item in run.pending if not item.failed_with]
    reloaded, reload_error = _phase_reload(run, live, sha)

    gateway_changed: list[str] = []
    caddy_restarted = False
    if reload_error is None:
        gateway_changed, caddy_restarted = _phase_gateway(run)
        _phase_finish(run, live, sha)

    state_written = run.platform.flush()
    report = SyncReport(
        sha=sha,
        services=tuple(run.outcome_for(item) for item in run.pending),
        escalations=tuple(run.escalations),
        gateway_changed=tuple(gateway_changed),
        reloaded=reloaded,
        caddy_restarted=caddy_restarted,
        state_written=state_written,
        error=reload_error,
    )
    log.info("sync done: %s", report.summary())
    return report


def _dry_run(run: _Run, manifests: Sequence[Path], sha: str, services_dir: Path) -> SyncReport:
    """Print what a real run would do. Touches nothing under the state dir.

    The source mirror is still updated -- that is a cache in the *store*, and
    without it there would be no manifests to plan against.
    """
    outcomes: list[ServiceOutcome] = []
    records: list[Mapping[str, Any]] = []

    def emit(record: Mapping[str, Any]) -> None:
        records.append(record)
        _emit(run.stream, record)

    for manifest in manifests:
        dirname = manifest.parent.name
        try:
            overlay = load_ams_overlay(manifest.parent)
            ctx = run.cfg.context_for(
                sha=sha, services_dir=services_dir, extra_secret_names=overlay.secrets
            )
            translation = translate(manifest.read_text(encoding="utf-8"), ctx)
            _apply_overlay_env(translation.decl, overlay.env)
        except (TranslateError, StaticError, DeclError, OSError, UnicodeDecodeError) as e:
            if not run.cfg.selects(dirname):
                continue
            error = f"translate: {type(e).__name__}: {e}"
            emit(
                _record(
                    service_id=dirname,
                    stage="translate",
                    error=error,
                    sha=sha,
                    kind=PLAN_KIND,
                    action="log",
                )
            )
            outcomes.append(
                ServiceOutcome(
                    id=dirname, kind="service", stage=FAILED, sha=sha, error=error, changed=True
                )
            )
            continue
        if not run.cfg.selects(dirname, translation.id):
            continue
        rec = run.platform.records.get(translation.id)
        actions = ["translate"]
        if rec is None or rec.sha != sha or rec.stage == FAILED:
            actions += ["stage", "provision", "declare"]
            if translation.kind == "service":
                actions += ["reload", "register", "health"]
        emit(
            _record(
                service_id=translation.id,
                stage="plan",
                error=f"would run: {', '.join(actions)}",
                sha=sha,
                prev_sha=rec.prev_sha if rec else None,
                kind=PLAN_KIND,
                action="log",
            )
        )
        outcomes.append(
            ServiceOutcome(
                id=translation.id,
                kind=translation.kind,
                stage=rec.stage if rec else STAGES[0],
                sha=sha,
                prev_sha=rec.prev_sha if rec else None,
                changed=False,
                actions=tuple(actions),
            )
        )
    return SyncReport(sha=sha, services=tuple(outcomes), escalations=tuple(records))
