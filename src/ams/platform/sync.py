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
from ams.platform import translate as translate_mod
from ams.platform.bootstrap import AUTH_ID, LAYER0_PORTS, REGISTRY_ID, BootstrapError, loopback_url
from ams.platform.bootstrap import place_jwt_key as place_jwt_key  # noqa: PLC0414 - patch point
from ams.platform.registryclient import RegistryClient, RegistryError, from_sidecar
from ams.platform.sources import SourceError, SourceMirror
from ams.platform.static import Overlay, StaticError, load_ams_overlay, publish_static
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
from ams.spawn import DATA_DIRNAME, INNER_GID, INNER_UID
from ams.state import StateDir, read_json_checked, write_json_atomic
from ams.uidmap import UidAllocator, UidBlock
from ams.userns import run_admin

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

# --- pools (PLAN-pool). N manifests share one process; the pool is a service in
# its own right (`pool-<name>`) and each member keeps its own port, its own
# mount sidecar and its own registry identity. These mirror `translate`'s frozen
# constants (PLAN-pool §5.3) rather than importing them, so this module keeps
# working while `translate.build_pool` is landing; a portable test asserts the
# two agree once it has.
POOL_ID_PREFIX = "pool-"
POOL_ADMIN_PORT_NAME = "pool"
POOL_JSON_NAME = "pool.json"
POOL_RUNNER_NAME = "pool_runner.py"
POOL_ASSETS_DIRNAME = "assets"
#: Harness-owned copies of what was pushed into the (service-owned) pool root.
#: The harness cannot read back a 0640 file it does not own, so "has this
#: changed" is answered against this shadow instead of against the real file --
#: which is what keeps a steady-state tick free of `run_admin` forks.
POOL_SHADOW_DIRNAME = "pool"
POOL_ROOT_FILE_MODE = 0o640
POOL_DATA_DIR_MODE = 0o750
POOL_HEALTH_PATH = "/_pool/health"
POOL_SECRET_PREFIX = f"{SVC_SECRET_NAME}__"
POOL_ADOPT_COMMAND = "ams platform pool adopt"
MAIN_PORT_NAME = "main"

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


class PoolError(SyncError):
    """A pool cannot be built: a cross-manifest rule broke (PLAN-pool §3.2).

    Raised while grouping, caught by :func:`_phase_translate`, and turned into
    *one* escalation against the pool id plus a ``failed`` record for each of
    its members. It is deliberately not allowed out of the phase: a pool is a
    group of services, not the fleet, and one broken pool must not stop the
    other nineteen manifests from deploying.
    """


# --------------------------------------------------------------------------- paths


def platform_dir(state: StateDir) -> Path:
    return Path(state.root) / PLATFORM_DIRNAME


def state_path(state: StateDir) -> Path:
    return platform_dir(state) / STATE_FILENAME


def mounts_dir(state: StateDir) -> Path:
    return platform_dir(state) / MOUNTS_DIRNAME


def registry_dir(state: StateDir) -> Path:
    return platform_dir(state) / REGISTRY_DIRNAME


def pool_shadow_dir(state: StateDir) -> Path:
    return platform_dir(state) / POOL_SHADOW_DIRNAME


def pool_id_for(pool: str) -> str:
    """``core`` -> ``pool-core``. The prefix is what keeps a pool id from ever
    colliding with a member id (PLAN-pool §3.2)."""
    return f"{POOL_ID_PREFIX}{pool}"


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
    #: How long a service that already failed its probe *at this same commit* is
    #: left alone before the gate is tried again. The live cost of not having
    #: this: two permanently dead pool members burned 90 s each on every tick,
    #: so a steady-state tick took 3 min 2 s and the 60 s timer ran runs back to
    #: back (`.claude/state/pool-migration.md` step 6b). Not zero, because a
    #: service fixed by hand (a secret set, a permission granted) has to be able
    #: to heal without waiting for an unrelated commit.
    failed_health_retry_s: float = 900.0
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
        pool: str | None = None,
    ) -> TranslateContext:
        return TranslateContext(
            sha=sha,
            services_dir=services_dir,
            registry_url=self.registry_url,
            auth_url=self.auth_url,
            extra_secret_names=tuple(extra_secret_names),
            pool=pool,
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


def _unstamp(text: str) -> float | None:
    """Inverse of :func:`_stamp`. ``None`` for anything it did not write."""
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp()
    except (ValueError, TypeError):
        return None


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
    #: Set by :mod:`ams.platform.rollback` to the commit this service was moved
    #: *away* from. Declared here so a sync tick's rewrite of the file preserves
    #: it -- :meth:`PlatformState.load` keeps only fields the dataclass declares
    #: (D27/T4.3), so an undeclared key would be dropped on the next flush.
    rolled_back_from: str | None = None
    #: On a *member* record: the pool it runs inside (``"core"``, unprefixed).
    #: Additive to the version-1 shape for the same reason `deployed_sha` was
    #: (D26): every reader takes it with `.get`, and it must be declared here or
    #: `PlatformState.load` drops it on the next flush (PLAN-pool §5.5).
    pool: str | None = None
    #: On a *pool* record: the member ids sharing this process, sorted.
    pool_members: list[str] = field(default_factory=list)
    #: When the health gate last *failed* for this service. Written only on a
    #: failure, so a record that has always been healthy never carries the key,
    #: and read only while the record is ``failed`` -- it is the clock the retry
    #: window in :func:`_health_gate_due` runs on. Declared here because
    #: `PlatformState.load` keeps only what the dataclass declares.
    health_failed_at: str = ""
    escalated: bool = False
    updated_at: str = ""
    stage_since: str = ""

    def as_json(self) -> dict[str, Any]:
        data = asdict(self)
        # The two pool keys are omitted when they are unset. A fleet with no
        # pools then keeps the exact record shape `docs/platform-sidecars.md`
        # documents, and -- the reason that matters operationally -- deploying
        # this build does not rewrite all twenty-one records just to add two
        # nulls, which would break "a tick that changes nothing writes nothing"
        # on the very first tick after the upgrade. `load` reads both keys with
        # membership tests, so an absent key and a null one are the same thing.
        if data.get("pool") is None:
            data.pop("pool", None)
        if not data.get("pool_members"):
            data.pop("pool_members", None)
        if not data.get("health_failed_at"):
            data.pop("health_failed_at", None)
        return data


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


def pool_runner_source() -> Path:
    """The runner asset shipped with ams (PLAN-pool §4.1).

    Package *data*, never an import: ``src/ams`` must not depend on fastapi or
    uvicorn, and the runner does. ams only ever reads its bytes.
    """
    return Path(__file__).resolve().parent / POOL_ASSETS_DIRNAME / POOL_RUNNER_NAME


def place_pool_file(
    root: Path, name: str, content: bytes, block: UidBlock, *, mode: int = POOL_ROOT_FILE_MODE
) -> None:
    """Put a harness-authored file inside the (service-owned) pool root.

    Same shape as :func:`bootstrap.place_jwt_key`: the root has already been
    handed to the service uid, so the harness writes a temp file in the
    *parent* directory -- which it still owns -- and copies it in through the
    admin namespace. No shell, four argv lists (D1).
    """
    root = Path(root)
    dst = root / name
    tmp = root.parent / f".{name}.tmp{os.getpid()}"
    tmp.write_bytes(content)
    try:
        os.chmod(tmp, mode)
        run_admin(["cp", str(tmp), str(dst)], block).check()
        run_admin(["chown", f"{INNER_UID}:{INNER_GID}", str(dst)], block).check()
        run_admin(["chmod", oct(mode)[2:].zfill(4), str(dst)], block).check()
    finally:
        tmp.unlink(missing_ok=True)
    log.info("placed %s in %s (mode %o)", name, root, mode)


def make_pool_data_dirs(root: Path, members: Sequence[str], block: UidBlock) -> None:
    """``<root>/data/<member>`` for every member, owned by the pool's uid.

    One directory per member rather than one shared one: a member's manifest
    asks for ``/var/lib/<name>/x.db`` and the translator rewrites it under its
    own subdirectory, so the databases stay separable -- which is what lets the
    backup keep its per-service R2 key after pooling (PLAN-pool §5.6).
    """
    data = Path(root) / DATA_DIRNAME
    dirs = [str(data / m) for m in members]
    if not dirs:
        return
    run_admin(["mkdir", "-m", oct(POOL_DATA_DIR_MODE)[2:], "-p", *dirs], block).check()
    run_admin(["chown", "-R", f"{INNER_UID}:{INNER_GID}", str(data)], block).check()


def legacy_data_is_nonempty(state: StateDir, member_id: str, block: UidBlock) -> bool:
    """Does the *pre-pool* ``services/<member>/root/data`` still hold anything?

    The adoption guard (PLAN-pool §5.5). The harness cannot list a 0750
    service-owned directory, so the answer comes from the admin namespace --
    but only when the directory exists at all, which on a fleet that never ran
    unpooled is never, and costs one ``stat``.
    """
    data = state.service_root(member_id) / DATA_DIRNAME
    if not data.is_dir():
        return False
    try:
        return any(data.iterdir())
    except OSError:
        pass
    result = run_admin(["find", str(data), "-mindepth", "1", "-maxdepth", "1", "-print"], block)
    if not result.ok:
        # Unreadable is not the same as empty, and this guard exists to stop
        # data being orphaned: treat it as occupied and make a human look.
        log.warning(
            "%s: cannot list %s (rc=%d); treating it as non-empty",
            member_id,
            data,
            result.returncode,
        )
        return True
    return bool(result.stdout.strip())


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
    #: The record exactly as this tick found it. Every phase walks a record
    #: *forward* (`advance` never moves one back), so by the time the health
    #: gate runs, a service that was `failed` when the tick started is sitting
    #: at `reloaded` again. Holding the gate therefore has to put the record
    #: back, and putting it back byte for byte is what keeps a tick that
    #: changed nothing from writing the state file.
    prior_record: ServiceRecord | None = None
    #: Set by :func:`_phase_hold`: still dead at an unchanged commit, so this
    #: tick neither reloads for it nor probes it.
    health_held: bool = False
    actions: list[str] = field(default_factory=list)
    changed: bool = False
    reached: str = STAGES[0]
    failed_with: str | None = None
    health_path: str = "/health"
    #: The pool this item belongs to, unprefixed. Set on a member *and* on the
    #: pool item itself; `is_pool` is what tells them apart (PLAN-pool §5.5).
    pool: str | None = None
    #: Pool item only: the member ids, sorted.
    pool_members: list[str] = field(default_factory=list)
    #: Pool item only: the ``<root>/pool.json`` document.
    pool_json: Mapping[str, Any] | None = None
    #: The allocator row that holds this item's port. ``""`` means "my own id",
    #: which is every unpooled service.
    port_owner: str = ""
    #: The name of the port on that row. A pooled member's port is named after
    #: the member; the pool's own admin/health port is named ``pool``.
    port_name: str = MAIN_PORT_NAME
    #: Where this item's ``SVC_SECRET`` lives. A pooled member's is stored under
    #: the *pool* id as ``SVC_SECRET__<MANGLED>`` -- one process, one env, so the
    #: names cannot collide (PLAN-pool §3.3).
    secret_owner: str = ""
    secret_name: str = SVC_SECRET_NAME

    @property
    def id(self) -> str:
        return self.translation.id

    @property
    def is_pool(self) -> bool:
        return bool(self.pool) and self.id.startswith(POOL_ID_PREFIX)

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
        #: pool name -> the error that stopped it. A member of a failed pool is
        #: marked failed without an escalation of its own: one broken process is
        #: one cause, and N copies of it is the noise T3.3 exists to prevent.
        self.failed_pools: dict[str, str] = {}

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
        if item.is_pool and item.pool:
            self.failed_pools[item.pool] = error
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
    item.prior_record = replace(rec, pool_members=list(rec.pool_members))


@dataclass
class _Parsed:
    """One manifest after the head translation, before any pool grouping."""

    manifest: Path
    dirname: str
    overlay: Overlay | None = None
    translation: Translation | None = None
    decl: ServiceDecl | None = None
    #: Set instead of the three above when the manifest did not translate.
    failed: _Pending | None = None

    @property
    def pool(self) -> str | None:
        return self.overlay.pool if self.overlay is not None else None

    @property
    def id(self) -> str:
        return self.translation.id if self.translation is not None else self.dirname


@dataclass
class _PoolGroup:
    """Every member of one pool, plus the pool's own pending item."""

    name: str
    entries: list[_Parsed]
    item: _Pending
    sha: str
    #: member id -> ``translate.PoolMember``; empty when ``build_pool`` failed.
    members: dict[str, Any] = field(default_factory=dict)
    #: member id -> the port name ``pool.json`` gives it.
    port_names: dict[str, str] = field(default_factory=dict)


def _translate_at(
    run: _Run,
    manifest: Path,
    overlay: Overlay,
    sha: str,
    services_dir: Path,
    *,
    pool: str | None = None,
) -> tuple[Translation, ServiceDecl | None]:
    ctx = run.cfg.context_for(
        sha=sha, services_dir=services_dir, extra_secret_names=overlay.secrets, pool=pool
    )
    translation = translate(manifest.read_text(encoding="utf-8"), ctx)
    return translation, _apply_overlay_env(translation.decl, overlay.env)


def _pool_api() -> tuple[Any, Any, Any]:
    """``(build_pool, pool_member, mangle_member)`` from :mod:`translate`.

    Looked up at call time rather than imported at module scope: sync must keep
    working -- and every unpooled service must keep deploying -- while the
    translator half of the feature is landing, and a missing symbol is then one
    pool's failure instead of an ImportError for the whole harness.
    """
    wanted = ("build_pool", "pool_member", "mangle_member")
    missing = [n for n in wanted if not hasattr(translate_mod, n)]
    if missing:
        raise PoolError(
            f"ams.platform.translate is missing {missing}; this build of ams cannot build pools"
        )
    return (translate_mod.build_pool, translate_mod.pool_member, translate_mod.mangle_member)


def _check_pool(name: str, entries: Sequence[_Parsed]) -> None:
    """The two cross-manifest rules ``build_pool`` cannot make for itself.

    Everything else in PLAN-pool §3.2 -- a pool of one, ``kind: static``, the
    reserved ``pool`` port name, ids that mangle alike, two members binding one
    shared env key to different values -- is checked by ``translate`` and raised
    from there, so there is exactly one copy of each rule. What is left needs
    the *pool name* next to the member ids, which the translator is never handed
    together.
    """
    broken = [e for e in entries if e.failed is not None]
    if broken:
        # Including the underlying message: `kind: static` and a malformed
        # manifest both land here, and "the pool did not build" without the
        # reason sends the reader to the wrong file.
        detail = "; ".join(f"{e.dirname}: {e.failed.failed_with}" for e in broken if e.failed)
        raise PoolError(f"pool {name!r}: member manifest(s) did not translate -- {detail}")
    ids = [e.id for e in entries]
    duplicates = sorted({sid for sid in ids if ids.count(sid) > 1})
    if duplicates:
        raise PoolError(f"pool {name!r}: duplicate member id(s) {duplicates}")
    if name in ids:
        raise PoolError(f"pool {name!r}: the pool name is also a member id")


def _pool_target_sha(run: _Run, entries: Sequence[_Parsed], head: str) -> str:
    """The commit the whole pool moves to (PLAN-pool §5.5).

    One process, one tree, one ``GIT_COMMIT``: the members cannot sit at
    different commits, so D26's per-member rule decides only *whether* the pool
    moves. Any affected member moves all of them; members that disagree about
    where they already are can only be reconciled at the head.
    """
    deployed: set[str] = set()
    for entry in entries:
        target = run.target_sha(entry.manifest, entry.id)
        if target == head:
            return head
        deployed.add(target)
    if len(deployed) == 1:
        return deployed.pop()
    log.info(
        "pool members disagree about their deployed commit (%s); moving the pool to the head",
        ", ".join(sorted(sha[:12] for sha in deployed)),
    )
    return head


def _translate_pool_members(
    run: _Run, entries: Sequence[_Parsed], target: str, services_dir: Path, pool: str
) -> None:
    """Re-translate every member at ``target`` *as a pooled member*.

    Only ever needed to move a pool *off* the head: the first pass already
    translated every member with ``TranslateContext.pool`` set, so the paths
    already point into the pool root and a tick that does not move the pool
    re-translates nothing.

    All or nothing: half a pool at one commit and half at another is a
    declaration that cannot be built, so a member that fails here raises for the
    whole group rather than being quietly dropped out of it.
    """
    out: list[tuple[Translation, ServiceDecl | None]] = []
    for entry in entries:
        assert entry.overlay is not None  # only successfully parsed entries are grouped
        out.append(
            _translate_at(run, entry.manifest, entry.overlay, target, services_dir, pool=pool)
        )
    for entry, (translation, decl) in zip(entries, out, strict=True):
        entry.translation, entry.decl = translation, decl


def _manifest_rel(run: _Run, manifest: Path) -> str:
    try:
        return manifest.parent.relative_to(run.checkout).as_posix()
    except ValueError:  # pragma: no cover - the manifest came from the checkout
        return manifest.parent.name


def _pool_health_path(decl: ServiceDecl | None) -> str:
    if decl is None:
        return POOL_HEALTH_PATH
    return getattr(decl.health, "path", None) or POOL_HEALTH_PATH


def _select_pools(run: _Run, parsed: Sequence[_Parsed]) -> dict[str, list[_Parsed]]:
    """Group the pooled manifests, keeping the pools this run is meant to touch.

    ``--only <one member>`` selects the *whole* pool: the members share one
    process, so there is no declaration that deploys half of it.
    """
    groups: dict[str, list[_Parsed]] = {}
    for entry in parsed:
        if entry.pool:
            groups.setdefault(entry.pool, []).append(entry)
    wanted: dict[str, list[_Parsed]] = {}
    for name, entries in sorted(groups.items()):
        selected = [e for e in entries if run.cfg.selects(e.dirname, e.id)]
        if not selected and not run.cfg.selects(pool_id_for(name)):
            log.debug("pool %s: no member selected", name)
            continue
        if selected and len(selected) != len(entries):
            log.info(
                "pool %s: %d of %d members named; a pool is one process, so all of it is selected",
                name,
                len(selected),
                len(entries),
            )
        wanted[name] = entries
    return wanted


def _build_pool_group(
    run: _Run, name: str, entries: list[_Parsed], head: str, services_dir: Path
) -> _PoolGroup:
    """Translate one pool into its own ``_Pending``, or fail it as a unit."""
    pool_id = pool_id_for(name)
    sha = head
    decl: ServiceDecl | None = None
    pool_json: Mapping[str, Any] | None = None
    members: dict[str, Any] = {}
    port_names: dict[str, str] = {}
    error: str | None = None
    try:
        build_pool, pool_member, _mangle = _pool_api()
        _check_pool(name, entries)
        target = _pool_target_sha(run, entries, head)
        try:
            if target != head:
                _translate_pool_members(run, entries, target, services_dir, name)
            sha = target
        except (TranslateError, DeclError, OSError, UnicodeDecodeError) as e:
            if target == head:
                raise
            # It translated at the head a moment ago, so this is not a manifest
            # problem: take the whole pool to the head rather than drop it.
            log.warning(
                "pool %s: re-translating at %s failed (%s); using the head", name, target[:12], e
            )
            _translate_pool_members(run, entries, head, services_dir, name)
            sha = head
        extra: set[str] = set()
        for entry in entries:
            assert entry.overlay is not None
            extra.update(entry.overlay.secrets)
        ctx = run.cfg.context_for(
            sha=sha,
            services_dir=services_dir,
            extra_secret_names=tuple(sorted(extra)),
            pool=name,
        )
        built = build_pool(
            name,
            [pool_member(e.translation, _manifest_rel(run, e.manifest)) for e in entries],
            ctx,
        )
        decl, pool_json = built.decl, built.pool_json
        members = {pm.id: pm for pm in built.members}
        port_names = {
            str(m["id"]): str(m["port_name"])
            for m in (pool_json or {}).get("members", ())
            if isinstance(m, Mapping) and m.get("id") and m.get("port_name")
        }
    except (PoolError, TranslateError, StaticError, DeclError, OSError, UnicodeDecodeError) as e:
        error = str(e) if isinstance(e, PoolError) else f"{type(e).__name__}: {e}"

    manual = any(
        bool(e.translation.flags.get("manual_restart", False))
        for e in entries
        if e.translation is not None
    )
    item = _Pending(
        manifest=entries[0].manifest,
        translation=Translation(
            id=pool_id,
            kind="service",
            decl=decl,
            mount={},
            registry=None,
            flags={"manual_restart": manual},
        ),
        decl=decl,
        decl_text=emit_toml(decl) if decl is not None else None,
        sha=sha,
        health_path=_pool_health_path(decl),
        pool=name,
        pool_members=sorted(e.id for e in entries),
        pool_json=pool_json,
        port_name=POOL_ADMIN_PORT_NAME,
    )
    _snapshot_prior(run, item, pool_id)
    item.prior_stage = run.platform.begin(
        pool_id, sha, manual_restart=item.manual_restart, now_s=run.now()
    )
    run.platform.get(pool_id).pool_members = list(item.pool_members)
    if error is not None:
        run.fail(item, "translate", error, sha)
    else:
        run.advance(item, "translated")
    return _PoolGroup(
        name=name,
        entries=entries,
        item=item,
        sha=sha,
        members=members,
        port_names=port_names,
    )


def _member_item(run: _Run, entry: _Parsed, group: _PoolGroup, mangle: Any) -> _Pending:
    """One pooled member: sidecars and an identity, but no declaration.

    ``decl``/``decl_text`` are deliberately ``None``. The member's
    ``service.toml`` is not written, because the process that serves it is the
    pool's -- which is the whole point of the feature (PLAN-pool §5.5).

    The port name is read out of ``pool.json``, which carries it for every
    member; the mount sidecar is the fallback. Either way it is the
    translator's, never re-derived here -- ``pool_port_name`` mangles a member
    id that is not a legal port name, and a second copy of that rule is how the
    gateway and the allocator come to disagree about one service.
    """
    member = group.members.get(entry.id)
    translation = member.translation if member is not None else entry.translation
    assert translation is not None
    mount = translation.mount or {}
    port_name = group.port_names.get(entry.id) or mount.get("port_name") or entry.id
    item = _Pending(
        manifest=entry.manifest,
        translation=translation,
        decl=None,
        decl_text=None,
        sha=group.sha,
        health_path=str((translation.registry or {}).get("health_path", "/health")),
        pool=group.name,
        port_owner=str(mount.get("port_owner") or group.item.id),
        port_name=str(port_name),
        secret_owner=group.item.id,
        secret_name=f"{POOL_SECRET_PREFIX}{mangle(entry.id)}",
    )
    _snapshot_prior(run, item, item.id)
    item.prior_stage = run.platform.begin(item.id, group.sha, manual_restart=False, now_s=run.now())
    run.platform.get(item.id).pool = group.name
    run.advance(item, "translated")
    return item


def _fallback_mangle(member_id: str) -> str:
    """Only used to name a secret when ``translate.mangle_member`` is missing --
    in which case the pool has already failed and nothing reads the name."""
    return member_id.replace("-", "_").upper()


def _phase_translate(run: _Run, manifests: Sequence[Path], sha: str, services_dir: Path) -> None:
    """Parse every selected manifest. A broken one fails only itself.

    Two passes, because which commit a pool moves to is a property of its
    *members*: every manifest is translated at the head first, then the pooled
    ones are grouped and re-translated together at the one sha the pool moves to
    (PLAN-pool §5.5). A checkout with no ``pool =`` overlay never enters the
    second pass, and every unpooled service takes exactly the path it did
    before.
    """
    parsed: list[_Parsed] = []
    for manifest in manifests:
        dirname = manifest.parent.name
        overlay: Overlay | None = None
        try:
            # The pool is read *before* the translation and passed into it: a
            # pooled member's absolute paths have to point into the pool root
            # from the start, and rewriting a finished standalone `Translation`
            # afterwards would be a second place that knows how a service root
            # is spelled (PLAN-pool §5.3, T3's contract).
            overlay = load_ams_overlay(manifest.parent)
            translation, decl = _translate_at(
                run, manifest, overlay, sha, services_dir, pool=overlay.pool
            )
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
            # The overlay is kept when it parsed, so a broken *pooled* manifest
            # is still grouped -- and takes its pool down with it rather than
            # letting the survivors deploy as a process quietly missing a member.
            parsed.append(_Parsed(manifest=manifest, dirname=dirname, overlay=overlay, failed=item))
            continue
        parsed.append(_Parsed(manifest, dirname, overlay, translation, decl))

    groups = {
        name: _build_pool_group(run, name, entries, sha, services_dir)
        for name, entries in _select_pools(run, parsed).items()
    }
    mangle = getattr(translate_mod, "mangle_member", _fallback_mangle)

    for entry in parsed:
        if entry.failed is not None:
            run.pending.append(entry.failed)
            continue
        assert entry.translation is not None
        if entry.pool is not None:
            group = groups.get(entry.pool)
            if group is None:
                continue
            run.pending.append(_member_item(run, entry, group, mangle))
            continue
        if not run.cfg.selects(entry.dirname, entry.id):
            log.debug("%s not selected", entry.id)
            continue

        # The id only exists once the manifest parses, and the affected check is
        # keyed by it -- so translate at the head first, then re-translate at the
        # deployed sha for a service this commit did not touch. `translate` is
        # pure and sub-millisecond; the alternative is guessing the id from the
        # directory name, which nothing else in this module does.
        translation, decl = entry.translation, entry.decl
        target = run.target_sha(entry.manifest, entry.id)
        if target != sha:
            assert entry.overlay is not None
            try:
                translation, decl = _translate_at(
                    run, entry.manifest, entry.overlay, target, services_dir
                )
            except (TranslateError, DeclError, OSError, UnicodeDecodeError) as e:
                # It translated at the head a moment ago, so this is not a
                # manifest problem: fall back to the head rather than skip it.
                log.warning(
                    "%s: re-translating at %s failed (%s); using the head",
                    entry.id,
                    target[:12],
                    e,
                )
                target = sha
                translation, decl = entry.translation, entry.decl

        item = _Pending(
            manifest=entry.manifest,
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

    # Pools first: a member is not declarable until its pool is, and processing
    # the pool first is what lets a failed pool skip its members without a
    # second escalation each.
    run.pending[:0] = [group.item for group in groups.values()]


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
    """Stage the tree, place the JWT key, provision. False = this one failed.

    A pooled member has neither a tree nor a venv of its own: its pool stages
    once into ``services/pool-<n>/root/repo`` and provisions once with every
    member installed editable into that one venv, which is what turns twelve
    ``cp --reflink`` + twelve ``uv sync`` into one of each (PLAN-pool §5.5).
    """
    if item.kind == "static":
        return _publish_static_site(run, item)
    if item.pool and not item.is_pool:
        log.debug("%s: pooled into %s; nothing to stage or provision", item.id, item.port_owner)
        return True
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


def _unlink_member_decl(run: _Run, item: _Pending) -> bool:
    """Remove a pooled member's *stale* ``service.toml``, never its root.

    A service that used to run on its own leaves a declaration behind, and the
    harness would keep starting it beside the pool -- two processes serving one
    identity, on two ports, both registering. The root is deliberately left
    alone: the member's data still lives in it until `ams platform pool adopt`
    moves it, and sync never moves data (PLAN-pool §5.5).
    """
    path = run.state.service_decl_path(item.id)
    try:
        path.unlink()
    except FileNotFoundError:
        return True
    except OSError as e:
        run.fail(item, "declare", f"stale service.toml: {type(e).__name__}: {e}", item.sha)
        return False
    log.info("%s: removed the pre-pool declaration %s", item.id, path)
    item.actions.append("unpool-decl")
    item.changed = True
    return True


def _member_secret_names(item: _Pending, mangle: Any) -> list[str]:
    return [f"{POOL_SECRET_PREFIX}{mangle(member)}" for member in item.pool_members]


def _adoption_blocked(run: _Run, item: _Pending) -> list[str]:
    """Members whose pre-pool ``root/data`` still holds something."""
    blocked: list[str] = []
    for member in item.pool_members:
        if not (run.state.service_root(member) / DATA_DIRNAME).is_dir():
            continue
        try:
            block = run.block_for(member)
        except (RuntimeError, OSError, ValueError) as e:  # pragma: no cover - host only
            log.warning("%s: cannot allocate a uid block to inspect its data (%s)", member, e)
            blocked.append(member)
            continue
        if legacy_data_is_nonempty(run.state, member, block):
            blocked.append(member)
    return blocked


def _place_pool_root_file(
    run: _Run, item: _Pending, name: str, content: bytes, block: UidBlock, action: str
) -> bool:
    """Push one harness-authored file into the pool root, iff it is not there.

    "Is it there" is answered against the harness-owned shadow copy plus a
    ``stat`` of the destination, because the destination itself is 0640 and
    owned by a uid the harness cannot read as. The shadow is written *after* the
    copy succeeds, so a failed placement is retried on the next tick.
    """
    root = run.state.service_root(item.id)
    shadow = pool_shadow_dir(run.state) / item.id / name
    try:
        unchanged = shadow.read_bytes() == content and (root / name).exists()
    except OSError:
        unchanged = False
    if unchanged:
        return False
    place_pool_file(root, name, content, block)
    shadow.parent.mkdir(parents=True, exist_ok=True)
    tmp = shadow.with_name(f".{name}.tmp{os.getpid()}")
    tmp.write_bytes(content)
    os.replace(tmp, shadow)
    item.actions.append(action)
    item.changed = True
    return True


def _declare_pool(run: _Run, item: _Pending) -> bool:
    """Everything a pool needs on disk before its declaration is written.

    Order is load-bearing: the adoption guard first (it refuses the whole
    declaration), then the member secrets (a declaration naming a secret with no
    value fails at spawn, D16), then ``pool.json`` and the runner (the argv in
    the declaration points at the runner, and the runner reads ``pool.json``
    beside itself), and only then -- back in the caller -- ``service.toml``,
    whose arrival is what makes the next reload start the process.
    """
    blocked = _adoption_blocked(run, item)
    if blocked:
        run.fail(
            item,
            "declare",
            f"member(s) {blocked} still have data in their pre-pool "
            f"services/<id>/root/{DATA_DIRNAME}; run `{POOL_ADOPT_COMMAND} {item.pool}` to move "
            "it into the pool root -- sync never moves data",
            item.sha,
        )
        return False

    try:
        block = run.block_for(item.id)
    except (RuntimeError, OSError, ValueError) as e:
        run.fail(item, "declare", f"uid block: {type(e).__name__}: {e}", item.sha)
        return False

    mangle = getattr(translate_mod, "mangle_member", _fallback_mangle)
    try:
        for name in _member_secret_names(item, mangle):
            if name not in run.secrets.names(item.id):
                run.secrets.set(item.id, name, _secrets.token_hex(SVC_SECRET_BYTES).encode("ascii"))
                item.actions.append("secret")
                item.changed = True
    except (OSError, ValueError) as e:
        run.fail(item, "declare", f"member secret: {type(e).__name__}: {e}", item.sha)
        return False

    document = json.dumps(dict(item.pool_json or {}), indent=2, sort_keys=True) + "\n"
    try:
        placed = _place_pool_root_file(
            run, item, POOL_JSON_NAME, document.encode("utf-8"), block, "pool-json"
        )
        _place_pool_root_file(
            run, item, POOL_RUNNER_NAME, pool_runner_source().read_bytes(), block, "pool-runner"
        )
        if placed:
            # The member set can only change when pool.json does, and `mkdir -p`
            # is idempotent, so this rides on that write instead of forking into
            # the admin namespace on every tick.
            make_pool_data_dirs(run.state.service_root(item.id), item.pool_members, block)
            item.actions.append("pool-data")
    except (OSError, RuntimeError) as e:
        run.fail(item, "declare", f"pool files: {type(e).__name__}: {e}", item.sha)
        return False
    return True


def _phase_declare(run: _Run, item: _Pending) -> bool:
    """Write the sidecars, generate ``SVC_SECRET``, write ``service.toml``."""
    try:
        if item.translation.mount and _write_json_if_changed(
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

    if item.pool and not item.is_pool:
        # A pooled member's two sidecars are fleet state (its gateway route and
        # its registry identity) and are written above like anyone else's. What
        # it does not get is a `service.toml`: the pool's declaration is the one
        # that starts the process serving it.
        if not _unlink_member_decl(run, item):
            return False
        run.advance(item, "declared")
        run.platform.get(item.id).deployed_sha = item.sha
        return True

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
    if item.is_pool:
        if not _declare_pool(run, item):
            return False
    else:
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
        and not item.health_held
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
        # A pooled member's port lives on its pool's allocator row, so "has this
        # been allocated yet" has to be asked of the owner -- the same
        # indirection `gateway.resolve_ports` follows one call later. An absent
        # `port_owner` means "my own id", which is every unpooled service.
        owner = str(mount.get("port_owner") or service_id)
        if mount.get("kind") == "service" and not ports.get(owner):
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
        # A pooled member's SVC_SECRET is stored under the *pool* id as
        # SVC_SECRET__<MANGLED>: one process holds every member's identity, so
        # the names have to be distinct in one env (PLAN-pool §3.3). The value
        # the registry is given is unchanged -- the registry never learns that
        # pooling exists.
        owner = item.secret_owner or item.id
        secret = run.secrets.load(owner, [item.secret_name])[item.secret_name]
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
    # The clock the retry window runs on. Written here rather than in `fail`
    # because it is only the health transition that is held off, and only a
    # failure stamps it -- so a record that has never failed a probe never
    # carries the key and the documented record shape is unchanged for it.
    run.platform.get(item.id).health_failed_at = _stamp(run.now())
    return False


def _pool_blocked(run: _Run, item: _Pending) -> bool:
    """True when this member's pool failed, in which case it is failed with it.

    Marked failed, *not* escalated: one process failing is one cause, and N
    escalations for it is exactly the per-tick noise the dedupe in
    :meth:`_Run.fail` exists to prevent. The pool's own escalation names the
    reason and its record names the members.
    """
    if item.is_pool or not item.pool:
        return False
    error = run.failed_pools.get(item.pool)
    if error is None:
        return False
    message = f"pool {pool_id_for(item.pool)}: {error}"
    run.platform.set_stage(item.id, FAILED, now_s=run.now(), error=message)
    run.platform.get(item.id).escalated = True
    item.failed_with = message
    item.changed = True
    log.info("%s: not declared -- its pool failed (%s)", item.id, error)
    return True


def _health_gate_held(run: _Run, item: _Pending, redeclared: set[str | None], now_s: float) -> bool:
    """Is this service's health gate held off on this tick?

    Only ever true for a service that was already ``failed`` at its **health**
    probe, at this same commit, with nothing about it moved. Four signals say
    "no, gate it", and all four are read off the run and the record -- nothing
    here asks the host or the harness:

    1. **its declaration was rewritten** (``"declare"`` in this tick's actions),
       which is the only thing that makes the harness restart it (D17);
    2. **its pool was re-declared**, which restarted the one process serving
       every member -- a pooled member has no declaration of its own to watch;
    3. **anything else moved for it** (``item.changed``): a re-stage, a
       provision, a sidecar, a freshly generated secret;
    4. **it was not failed at the health transition when the tick started** --
       a new service, one still being driven to ``healthy``, or one that failed
       at ``register`` instead, which is usually a registry that was briefly
       down and which the next tick should therefore retry. The transition is
       already the first word of ``error``, so no new field records it.

    The prior record is what all of this is read from, never the live one:
    every phase before this walks a record *forward* (``advance`` never moves
    one back), so a service that was ``failed`` when the tick started is
    already sitting at ``declared`` by the time this runs.

    An operator's ``ams ctl restart`` is deliberately **not** a signal, because
    it leaves no trace in ``platform/state.json`` for this function to read.
    ``failed_health_retry_s`` covers that case instead: a service repaired by
    hand is re-probed on its own within one window rather than staying
    ``failed`` in the record until an unrelated commit happens to touch it.
    """
    if item.kind != "service":
        return False
    if "declare" in item.actions or item.changed:
        return False
    if item.pool and not item.is_pool and item.pool in redeclared:
        return False
    if item.prior_stage != FAILED or item.prior_sha != item.sha:
        return False
    if not (item.prior_error or "").startswith("health:"):
        return False
    last = _unstamp(run.platform.get(item.id).health_failed_at)
    if last is None:
        # A record from before this field existed, or one restored from a
        # backup: gate it once, which stamps it, and hold it from then on.
        return False
    return (now_s - last) < run.cfg.failed_health_retry_s


def _phase_hold(run: _Run, live: Sequence[_Pending]) -> list[str]:
    """Hold the health gate for what is still dead at an unchanged commit.

    Runs *before* the mid-run flush, not inside :func:`_phase_finish`, for one
    reason: the phases have already walked a failed record forward to
    ``declared``, and a flush in between would persist that -- so the tick
    would write the state file twice and could never be a no-op. Held items are
    left at their prior stage, which also keeps them out of ``_mark_reloaded``
    and therefore out of the gate.

    The live cost of not doing this, measured on the box: two permanently dead
    pool members burned 90 s each on every tick, a steady-state tick took
    3 min 2 s, and the 60 s timer ran ticks back to back
    (`.claude/state/pool-migration.md` step 6b). ``--only`` cannot exclude them,
    because naming any member of a pool selects the whole pool.
    """
    redeclared = {
        item.pool for item in live if item.is_pool and item.pool and "declare" in item.actions
    }
    now_s = run.now()
    held: list[str] = []
    for item in live:
        if not _health_gate_held(run, item, redeclared, now_s):
            continue
        if item.prior_record is None:  # pragma: no cover - a failed record always exists
            continue
        # Byte for byte, including `updated_at`: an identical record is what
        # makes `PlatformState.flush` a no-op, which is the property the timer
        # depends on.
        run.platform.records[item.id] = item.prior_record
        item.reached = item.prior_record.stage
        item.health_held = True
        held.append(item.id)
    if held:
        log.info(
            "health gate held for %d service(s) still failed at the same commit and "
            "unchanged since the last probe (retry in %.0fs): %s",
            len(held),
            run.cfg.failed_health_retry_s,
            ", ".join(sorted(held)),
        )
    return held


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
    # A pooled member has no declaration of its own, so "my declaration changed"
    # has to be read off its pool: when the pool was re-declared its process was
    # restarted, and every member in it is worth re-probing.
    redeclared = {
        item.pool for item in live if item.is_pool and item.pool and "declare" in item.actions
    }
    work = [
        item
        for item in live
        if item.kind == "service"
        and item.reached == "reloaded"
        and (
            "declare" in item.actions
            or (item.pool in redeclared and not item.is_pool)
            or run.platform.get(item.id).stage != "healthy"
        )
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
        owner = item.port_owner or item.id
        allocated = ports.get(owner).get(item.port_name)
        if allocated is None:
            run.fail(
                item,
                "health",
                f"no allocated port named {item.port_name!r} on {owner!r}",
                item.sha,
            )
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
        if _pool_blocked(run, item):
            continue
        if _phase_materialize(run, item):
            _phase_declare(run, item)

    live = [item for item in run.pending if not item.failed_with]
    _phase_hold(run, live)
    run.platform.flush()
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


def _dry_plan_pool(
    run: _Run,
    name: str,
    entries: Sequence[_Parsed],
    head: str,
    emit: Callable[[Mapping[str, Any]], None],
) -> list[ServiceOutcome]:
    """The plan for one pool: its own line, then one indented line per member.

    This is what PLAN-pool §7.2 step 4 is read against before the migration's
    first state-changing tick, so it answers that step's four questions from
    what is actually on disk rather than from what the code intends: one new
    service, N declarations going away, N mount sidecars gaining a
    ``port_owner``, and zero registry sidecar changes.
    """
    pool_id = pool_id_for(name)
    out: list[ServiceOutcome] = []
    broken = [e for e in entries if e.failed is not None]
    if broken:
        error = (
            f"pool {name!r} will not build: member manifest(s) "
            f"{sorted(e.dirname for e in broken)} did not translate"
        )
        emit(
            _record(
                service_id=pool_id,
                stage="plan",
                error=error,
                sha=head,
                kind=PLAN_KIND,
                action="log",
            )
        )
        return [ServiceOutcome(id=pool_id, kind="service", stage=FAILED, sha=head, error=error)]

    pool_sha = _pool_target_sha(run, entries, head)
    members = sorted(e.id for e in entries)
    rec = run.platform.records.get(pool_id)
    actions = ["translate"]
    if rec is None or rec.sha != pool_sha or rec.stage == FAILED:
        # A pool has no registry identity of its own, so `register` is a
        # member's action and never the pool's.
        actions += ["stage", "provision", "declare", "reload", "health"]
    emit(
        _record(
            service_id=pool_id,
            stage="plan",
            error=(
                f"would run: {', '.join(actions)} (pool of {len(members)}: {', '.join(members)})"
            ),
            sha=pool_sha,
            prev_sha=rec.prev_sha if rec else None,
            kind=PLAN_KIND,
            action="log",
        )
    )
    out.append(
        ServiceOutcome(
            id=pool_id,
            kind="service",
            stage=rec.stage if rec else STAGES[0],
            sha=pool_sha,
            prev_sha=rec.prev_sha if rec else None,
            actions=tuple(actions),
        )
    )

    for entry in entries:
        assert entry.translation is not None
        member_rec = run.platform.records.get(entry.id)
        changes: list[str] = []
        current = _read_sidecar(mounts_dir(run.state) / f"{entry.id}.json")
        wanted = dict(entry.translation.mount)
        if current == wanted:
            changes.append("mount-unchanged")
        elif not (current or {}).get("port_owner"):
            changes.append(f"mount-gains-port_owner={wanted.get('port_owner') or pool_id}")
        else:
            changes.append("mount-changes")
        changes.append(
            "declaration-unlinked"
            if run.state.service_decl_path(entry.id).is_file()
            else "no-declaration"
        )
        registry_now = _read_sidecar(registry_dir(run.state) / f"{entry.id}.json")
        wanted_registry = dict(entry.translation.registry or {})
        changes.append(
            "registry-unchanged" if registry_now == wanted_registry else "registry-changes"
        )
        changes.append("register")
        changes.append("health")
        emit(
            _record(
                service_id=entry.id,
                stage="plan",
                # Two spaces: members read as indented under their pool, the
                # same shape `ams platform status` uses (§3.4).
                error=f"  in pool {name}: {', '.join(changes)}",
                sha=pool_sha,
                prev_sha=member_rec.prev_sha if member_rec else None,
                kind=PLAN_KIND,
                action="log",
            )
        )
        out.append(
            ServiceOutcome(
                id=entry.id,
                kind="service",
                stage=member_rec.stage if member_rec else STAGES[0],
                sha=pool_sha,
                prev_sha=member_rec.prev_sha if member_rec else None,
                actions=tuple(changes),
            )
        )
    return out


def _read_sidecar(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _dry_run(run: _Run, manifests: Sequence[Path], sha: str, services_dir: Path) -> SyncReport:
    """Print what a real run would do. Touches nothing under the state dir.

    The source mirror is still updated -- that is a cache in the *store*, and
    without it there would be no manifests to plan against.

    Pooled manifests are planned as a group, at the position of the pool's first
    member, so the ordering of every unpooled line is exactly what it was before
    pools existed.
    """
    outcomes: list[ServiceOutcome] = []
    records: list[Mapping[str, Any]] = []

    def emit(record: Mapping[str, Any]) -> None:
        records.append(record)
        _emit(run.stream, record)

    parsed: list[_Parsed] = []
    for manifest in manifests:
        dirname = manifest.parent.name
        overlay: Overlay | None = None
        try:
            overlay = load_ams_overlay(manifest.parent)
            translation, _decl = _translate_at(
                run, manifest, overlay, sha, services_dir, pool=overlay.pool
            )
        except (TranslateError, StaticError, DeclError, OSError, UnicodeDecodeError) as e:
            if not run.cfg.selects(dirname):
                continue
            error = f"translate: {type(e).__name__}: {e}"
            failed = _Pending(
                manifest=manifest,
                translation=Translation(
                    id=dirname, kind="service", decl=None, mount={}, registry=None
                ),
                decl=None,
                decl_text=None,
                sha=sha,
            )
            failed.failed_with = error
            parsed.append(
                _Parsed(manifest=manifest, dirname=dirname, overlay=overlay, failed=failed)
            )
            continue
        parsed.append(_Parsed(manifest, dirname, overlay, translation, None))

    pools = _select_pools(run, parsed)
    planned: set[str] = set()
    for entry in parsed:
        if entry.failed is not None and entry.pool is None:
            error = entry.failed.failed_with or "translate failed"
            emit(
                _record(
                    service_id=entry.dirname,
                    stage="translate",
                    error=error,
                    sha=sha,
                    kind=PLAN_KIND,
                    action="log",
                )
            )
            outcomes.append(
                ServiceOutcome(
                    id=entry.dirname,
                    kind="service",
                    stage=FAILED,
                    sha=sha,
                    error=error,
                    changed=True,
                )
            )
            continue
        if entry.pool is not None:
            if entry.pool not in pools or entry.pool in planned:
                continue
            planned.add(entry.pool)
            outcomes += _dry_plan_pool(run, entry.pool, pools[entry.pool], sha, emit)
            continue
        assert entry.translation is not None
        if not run.cfg.selects(entry.dirname, entry.id):
            continue
        rec = run.platform.records.get(entry.id)
        actions = ["translate"]
        if rec is None or rec.sha != sha or rec.stage == FAILED:
            actions += ["stage", "provision", "declare"]
            if entry.translation.kind == "service":
                actions += ["reload", "register", "health"]
        emit(
            _record(
                service_id=entry.id,
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
                id=entry.id,
                kind=entry.translation.kind,
                stage=rec.stage if rec else STAGES[0],
                sha=sha,
                prev_sha=rec.prev_sha if rec else None,
                changed=False,
                actions=tuple(actions),
            )
        )
    return SyncReport(sha=sha, services=tuple(outcomes), escalations=tuple(records))
