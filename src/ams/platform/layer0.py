"""Layer-0 bring-up: turn a bare state dir into a running replica platform.

This is the orchestrator T3.2 owns. It writes no policy of its own -- every
step is a call into a module another task built and tested -- but the *order*
is the deliverable, because most of the ordering constraints here are not
obvious and each one was learned from a failure mode:

1. **Identities before Layer-1 starts.** A translated Layer-1 declaration has no
   ``SVC_DEV``, so ``sdk.registry.Registry.start()`` does a blocking
   ``POST /api/services/register`` in the FastAPI lifespan. That endpoint 404s
   for an id the registry has never heard of, the exception escapes the
   lifespan, and uvicorn treats it as a startup failure -- i.e. a crash loop to
   ``max_retries``. So the registry must be *running* and the identity must
   *exist* before kvservice or timeservice is started. That single constraint is
   why the bring-up reloads once with the Layer-1 services deliberately left
   down, and starts them only after :meth:`RegistryClient.create_identity`.

2. **The uid block must exist before the reload, not after.** ``stage()`` and
   ``place_jwt_key()`` write into a service root through the admin namespace and
   need the block now; the harness allocates blocks during ``ams ctl reload``.
   :class:`~ams.uidmap.UidAllocator` is idempotent and persisted, so allocating
   here from the *same* state file the harness uses hands the harness the same
   answer later. Nothing is reserved twice.

3. **Fixed ports for everything the config references.** ``registry`` and
   ``auth`` are fixed by D22 because every cross reference in the replica is a
   literal URL. ``caddy`` is fixed here for the same class of reason and a
   sharper one: the entry site's own listen address is *inside* the Caddyfile,
   and the Caddyfile has to be on disk before Caddy is started by the reload
   that would have allocated the port. Rendering after the reload would mean
   starting Caddy against a config that does not exist yet. See DECISIONS D24.

4. **Stop a Layer-1 service before re-staging it.** ``stage()`` swaps
   ``<root>/repo`` by ``mv`` + ``rm -rf``, and the service's venv lives *inside*
   that tree (``repo/services/<id>/.venv``). Doing that under a running process
   leaves it executing from an unlinked directory, and the following
   ``uv sync`` rewrites files it has mapped. Stop, stage, provision, reload,
   start.

Everything is idempotent. A second ``bring_up`` over a live replica re-fetches
(cheap), finds the keypair and secrets present, finds every staged tree already
at the sha, re-renders byte-identical config, reloads (a no-op reload), and
re-``POST``s the identities (409 == success). That property is what makes this
safe to re-run after a partial failure, which is the only repair path the agent
loop has.

Run it on the target host as the harness user::

    python -m ams.platform.layer0 --repo-url <url> [--ref main] [--plan]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ams.platform import bootstrap as bootstrap_mod
from ams.platform import gateway as gateway_mod
from ams.platform import sources as sources_mod
from ams.platform import translate as translate_mod
from ams.platform.bootstrap import AUTH_ID, LAYER0_PORTS, REGISTRY_ID, BootstrapResult
from ams.platform.registryclient import RegistryClient
from ams.runtime import RuntimeStore
from ams.schema import ServiceDecl
from ams.state import StateDir, write_json_atomic
from ams.uidmap import UidBlock

log = logging.getLogger("ams.platform.layer0")

__all__ = [
    "CADDY_ID",
    "CADDY_PORT",
    "DEFAULT_LAYER1_MANIFESTS",
    "Layer0Error",
    "Layer0Report",
    "STAGES",
    "bring_up",
    "main",
]


class Layer0Error(RuntimeError):
    """A bring-up stage failed. ``stage`` names it; ``report`` is what got done.

    Never carries a secret: the only secrets in this module are read from the
    :class:`~ams.secrets.SecretStore` and handed straight to the registry
    client, which redacts its own.
    """

    def __init__(self, stage: str, message: str, report: Layer0Report) -> None:
        super().__init__(f"{stage}: {message}")
        self.stage = stage
        self.report = report


CADDY_ID = gateway_mod.CADDY_SERVICE_ID

#: Fixed, for the same reason as :data:`~ams.platform.bootstrap.LAYER0_PORTS`
#: and one more: the entry site's listen address lives *in* the Caddyfile, which
#: must be written before the reload that starts Caddy. See DECISIONS D24.
CADDY_PORT = 20180

#: The Layer-1 services this bring-up re-points at the replica. Not the whole
#: fleet -- T4.1 owns that. These two are the pilot's services, already running
#: on the box, and re-pointing them is what proves the replica works end to end.
DEFAULT_LAYER1_MANIFESTS: Mapping[str, str] = {
    "kvservice": "services/kvservice/service.yaml",
    "timeservice": "apps/timeservice/service.yaml",
}

#: Stage labels in execution order. The report's ``stages`` is a prefix of this.
STAGES: tuple[str, ...] = (
    "fetch",
    "materialize",
    "bootstrap",
    "allocate",
    "stage-layer0",
    "keys-layer0",
    "provision-layer0",
    "stop-layer1",
    "stage-layer1",
    "translate-layer1",
    "provision-layer1",
    "gateway",
    "reload",
    "health-layer0",
    "identities",
    "start-layer1",
    "health-layer1",
)

MOUNTS_DIRNAME = "mounts"
REGISTRY_SIDECAR_DIRNAME = "registry"

DEFAULT_HEALTH_DEADLINE_S = 240.0
DEFAULT_CTL_TIMEOUT_S = 30.0


# --------------------------------------------------------------------------- report


@dataclass
class Layer0Report:
    """What one :func:`bring_up` did. Mutated as stages complete, then frozen
    by being returned; a :class:`Layer0Error` carries the partial one.

    Deliberately all plain data: the transcript in
    ``.claude/state/platform-layer0.md`` is produced by dumping this next to the
    live ``ams ctl status``, and a dataclass of dicts survives ``json.dumps``.
    """

    sha: str = ""
    source: str = ""
    stages: list[str] = field(default_factory=list)
    failed_stage: str | None = None
    bootstrap: BootstrapResult | None = None
    blocks: dict[str, int] = field(default_factory=dict)
    ports: dict[str, int] = field(default_factory=dict)
    provisioned: list[str] = field(default_factory=list)
    gateway_files: list[str] = field(default_factory=list)
    reload_summary: dict[str, Any] = field(default_factory=dict)
    health: dict[str, bool] = field(default_factory=dict)
    identities: dict[str, bool] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "sha": self.sha,
            "source": self.source,
            "stages": list(self.stages),
            "failed_stage": self.failed_stage,
            "bootstrap": None
            if self.bootstrap is None
            else {
                "created": list(self.bootstrap.created),
                "updated": list(self.bootstrap.updated),
                "existing": list(self.bootstrap.existing),
            },
            "blocks": dict(self.blocks),
            "ports": dict(self.ports),
            "provisioned": list(self.provisioned),
            "gateway_files": list(self.gateway_files),
            "reload": dict(self.reload_summary),
            "health": dict(self.health),
            "identities": dict(self.identities),
        }


# --------------------------------------------------------------------------- helpers


def _ctl(state: StateDir, op: str, service_id: str | None = None) -> dict[str, Any]:
    """One control-socket round trip. Imported lazily so a unit test can
    monkeypatch this module attribute without a live harness."""
    from ams.control import control_socket_path
    from ams.control import request as control_request

    what = f"{op} {service_id}" if service_id else op
    response = control_request(
        control_socket_path(state), op, service_id, timeout_s=DEFAULT_CTL_TIMEOUT_S
    )
    if not response.get("ok"):
        # "the harness answered and refused" is a real failure of this stage,
        # not something to notice three stages later when nothing came up.
        raise RuntimeError(f"ctl {what} refused: {response.get('error', response)}")
    log.info("ctl %s -> ok", what)
    return response


def _harness_user() -> str:
    from ams.cli import harness_user

    return harness_user()


def _allocator(state: StateDir, user: str) -> Any:
    from ams.uidmap import UidAllocator

    return UidAllocator.from_host(user, state.uidmap_state)


def _provision_one(
    state: StateDir, store: RuntimeStore, decl: ServiceDecl, block: UidBlock
) -> None:
    """``ams provision <id>`` for one service, in process.

    In process rather than as a subprocess on purpose: this module already runs
    under the one interpreter AppArmor lets create user namespaces
    (``/home/harness/venv/bin/python3``), so re-exec'ing would only add a way to
    get that wrong. The provisioning log still lands in ``<state>/logs`` where
    ``ams provision`` puts it.
    """
    from ams.runtime import provision
    from ams.userns import ensure_service_root

    root = state.service_root(decl.id)
    ensure_service_root(root, block)
    provision(decl, root, store, block, log_path=state.logs_dir / f"{decl.id}-provision.log")
    log.info("provisioned %s (%s)", decl.id, decl.runtime.kind)


def _caddy_declaration_text(state: StateDir, store: RuntimeStore, port: int) -> str:
    """``gateway.caddy_declaration`` with its ``ports.main = 0`` pinned to ``port``.

    The generator asks ams to allocate, which is right for a gateway whose
    config is rendered after the fact. This bring-up cannot do that (the listen
    address is inside the Caddyfile, which must exist before Caddy starts), so
    the one line is rewritten here rather than in ``gateway.py`` -- that module
    belongs to T2.2 and its golden-file tests, and a second caller wanting a
    fixed port is not yet a reason to change its signature.
    """
    text = gateway_mod.caddy_declaration(state, store.root)
    needle = "\nmain = 0\n"
    if needle not in text:
        raise RuntimeError(
            "caddy_declaration no longer contains 'main = 0'; the fixed-port "
            "rewrite in ams.platform.layer0 is stale"
        )
    return text.replace(needle, f"\nmain = {port}\n", 1)


def _write_text(path: Path, text: str) -> bool:
    """Atomic write, ``True`` when the bytes actually changed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        return False
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return True


def _sidecar_path(state: StateDir, kind: str, service_id: str) -> Path:
    return state.root / "platform" / kind / f"{service_id}.json"


class _LivePorts:
    """``resolve_ports``' port source, backed by the harness's own ``ports.json``.

    Read-only by construction: it never calls ``allocate``, so it cannot hand
    out or persist anything. A service with no allocation yet returns ``{}`` and
    the renderer raises, which is the honest failure.
    """

    def __init__(
        self, state: StateDir, extra: Mapping[str, Mapping[str, int]] | None = None
    ) -> None:
        from ams.ports import PortAllocator

        self._alloc = PortAllocator(state.ports_state)
        self._extra = dict(extra or {})

    def get(self, service_id: str) -> Mapping[str, int]:
        if service_id in self._extra:
            return self._extra[service_id]
        return self._alloc.get(service_id)


# --------------------------------------------------------------------------- the loop


def bring_up(
    state: StateDir,
    store: RuntimeStore,
    *,
    repo_url: str,
    ref: str = "main",
    layer1: Mapping[str, str] = DEFAULT_LAYER1_MANIFESTS,
    caddy_port: int = CADDY_PORT,
    user: str | None = None,
    health_deadline_s: float = DEFAULT_HEALTH_DEADLINE_S,
) -> Layer0Report:
    """Bring registry, auth and the gateway up, then re-point ``layer1`` at them.

    Raises :class:`Layer0Error` naming the stage that failed; every stage before
    it has completed and is recorded in ``err.report.stages``, and no stage
    after it has run. Steps are ordered by the four constraints in the module
    docstring, not by convenience.
    """
    report = Layer0Report(source=repo_url)
    user = user or _harness_user()
    registry_url = bootstrap_mod.loopback_url(LAYER0_PORTS[REGISTRY_ID])
    auth_url = bootstrap_mod.loopback_url(LAYER0_PORTS[AUTH_ID])
    layer1 = dict(layer1)

    def done(stage: str) -> None:
        report.stages.append(stage)
        log.info("stage %s complete", stage)

    def fail(stage: str, exc: BaseException) -> Layer0Error:
        report.failed_stage = stage
        log.error("stage %s failed: %s: %s", stage, type(exc).__name__, exc)
        return Layer0Error(stage, f"{type(exc).__name__}: {exc}", report)

    mirror = sources_mod.SourceMirror(store.root, "api", url=repo_url)

    # ---------------------------------------------------------------- fetch
    try:
        sha = mirror.fetch(ref)
    except Exception as e:
        raise fail("fetch", e) from e
    report.sha = sha
    done("fetch")

    try:
        mirror.materialize(sha)
    except Exception as e:
        raise fail("materialize", e) from e
    done("materialize")

    # ------------------------------------------------------------ bootstrap
    try:
        report.bootstrap = bootstrap_mod.bootstrap(state, store)
    except Exception as e:
        raise fail("bootstrap", e) from e
    done("bootstrap")

    # ------------------------------------------------------------- allocate
    # Same allocator, same state file, same answers the harness will get on the
    # reload below. Allocating for caddy too, even though nothing stages into
    # its root: doing it here means the reload finds every block already
    # persisted and cannot hand out a different one.
    try:
        allocator = _allocator(state, user)
        blocks: dict[str, UidBlock] = {
            sid: allocator.allocate(sid)
            for sid in (REGISTRY_ID, AUTH_ID, CADDY_ID, *sorted(layer1))
        }
    except Exception as e:
        raise fail("allocate", e) from e
    report.blocks = {sid: b.uid_start for sid, b in blocks.items()}
    report.ports = {REGISTRY_ID: LAYER0_PORTS[REGISTRY_ID], AUTH_ID: LAYER0_PORTS[AUTH_ID]}
    report.ports[CADDY_ID] = caddy_port
    done("allocate")

    # -------------------------------------------------------- stage layer 0
    try:
        for sid in (REGISTRY_ID, AUTH_ID):
            mirror.stage(sha, state.service_root(sid), blocks[sid])
    except Exception as e:
        raise fail("stage-layer0", e) from e
    done("stage-layer0")

    # --------------------------------------------------------- keys layer 0
    # registry signs M2M tokens, auth signs user tokens: both need the private
    # key. auth additionally serves JWKS from the public half. Neither can read
    # the other's copy -- that is the point of a copy per service (D22).
    try:
        for sid in (REGISTRY_ID, AUTH_ID):
            bootstrap_mod.ensure_service_dirs(state, sid, blocks[sid])
            bootstrap_mod.place_jwt_key(state, sid, blocks[sid], store=store, private=True)
        bootstrap_mod.place_jwt_key(state, AUTH_ID, blocks[AUTH_ID], store=store)
    except Exception as e:
        raise fail("keys-layer0", e) from e
    done("keys-layer0")

    # ---------------------------------------------------- provision layer 0
    layer0_decls = bootstrap_mod.layer0_declarations(state)
    try:
        for sid in (REGISTRY_ID, AUTH_ID):
            _provision_one(state, store, layer0_decls[sid], blocks[sid])
            report.provisioned.append(sid)
    except Exception as e:
        raise fail("provision-layer0", e) from e
    done("provision-layer0")

    # --------------------------------------------------------- stop layer 1
    # Constraint 4: stage() unlinks the tree the running process lives in.
    try:
        for sid in sorted(layer1):
            _ctl(state, "stop", sid)
    except Exception as e:
        raise fail("stop-layer1", e) from e
    done("stop-layer1")

    try:
        for sid in sorted(layer1):
            mirror.stage(sha, state.service_root(sid), blocks[sid])
            bootstrap_mod.ensure_service_dirs(state, sid, blocks[sid])
            bootstrap_mod.place_jwt_key(state, sid, blocks[sid], store=store)
    except Exception as e:
        raise fail("stage-layer1", e) from e
    done("stage-layer1")

    # ---------------------------------------------------- translate layer 1
    canonical = mirror.checkout_dir(sha)
    ctx = translate_mod.TranslateContext(
        sha=sha,
        services_dir=state.services_dir,
        registry_url=registry_url,
        auth_url=auth_url,
    )
    translations: dict[str, Any] = {}
    try:
        for sid, rel in sorted(layer1.items()):
            text = (canonical / rel).read_text(encoding="utf-8")
            tr = translate_mod.translate(text, ctx)
            if tr.id != sid:
                raise Layer0Error(
                    "translate-layer1",
                    f"{rel} declares name {tr.id!r} but was listed under {sid!r}",
                    report,
                )
            translations[sid] = tr
            _write_text(state.service_decl_path(sid), translate_mod.emit_toml(tr.decl))
            write_json_atomic(_sidecar_path(state, MOUNTS_DIRNAME, sid), dict(tr.mount))
            if tr.registry is not None:
                write_json_atomic(
                    _sidecar_path(state, REGISTRY_SIDECAR_DIRNAME, sid), dict(tr.registry)
                )
    except Exception as e:
        raise fail("translate-layer1", e) from e
    done("translate-layer1")

    # ---------------------------------------------------- provision layer 1
    try:
        for sid in sorted(layer1):
            _provision_one(state, store, translations[sid].decl, blocks[sid])
            report.provisioned.append(sid)
    except Exception as e:
        raise fail("provision-layer1", e) from e
    done("provision-layer1")

    # -------------------------------------------------------------- gateway
    try:
        _write_text(
            state.service_decl_path(CADDY_ID), _caddy_declaration_text(state, store, caddy_port)
        )
        mounts = [dict(translations[sid].mount) for sid in sorted(layer1)]
        ports = gateway_mod.resolve_ports(mounts, _LivePorts(state))
        cfg = gateway_mod.GatewayConfig(
            listen_port=caddy_port, static_root=gateway_mod.static_root(state)
        )
        files = gateway_mod.render(mounts, ports, cfg)
        changed = gateway_mod.write(state, files)
    except Exception as e:
        raise fail("gateway", e) from e
    report.ports.update(ports)
    report.gateway_files = [p.name for p in changed]
    done("gateway")

    # --------------------------------------------------------------- reload
    try:
        response = _ctl(state, "reload")
    except Exception as e:
        raise fail("reload", e) from e
    report.reload_summary = dict(response.get("reload") or {})
    done("reload")

    # -------------------------------------------------------- health layer 0
    try:
        client = RegistryClient(registry_url, _admin_token(state))
        report.health[REGISTRY_ID] = client.wait_healthy(
            f"{registry_url}/health", health_deadline_s
        )
        report.health[AUTH_ID] = _wait_tcp(LAYER0_PORTS[AUTH_ID], health_deadline_s)
        report.health[CADDY_ID] = _wait_http(caddy_port, gateway_mod.HEALTH_PATH, health_deadline_s)
    except Exception as e:
        raise fail("health-layer0", e) from e
    unhealthy = sorted(k for k, v in report.health.items() if not v)
    if unhealthy:
        raise fail(
            "health-layer0",
            RuntimeError(f"not healthy within {health_deadline_s:.0f}s: {', '.join(unhealthy)}"),
        )
    done("health-layer0")

    # ----------------------------------------------------------- identities
    # Constraint 1: this must precede the Layer-1 start.
    try:
        for sid in sorted(layer1):
            tr = translations[sid]
            if tr.registry is None:  # pragma: no cover - kind=static has no identity
                continue
            report.identities[sid] = _register_identity(state, client, tr.registry)
    except Exception as e:
        raise fail("identities", e) from e
    done("identities")

    # ------------------------------------------------------------- layer 1
    try:
        for sid in sorted(layer1):
            _ctl(state, "start", sid)
    except Exception as e:
        raise fail("start-layer1", e) from e
    done("start-layer1")

    try:
        for sid in sorted(layer1):
            port = ports[sid]
            path = translations[sid].registry.get("health_path", "/health")
            report.health[sid] = _wait_http(port, path, health_deadline_s)
    except Exception as e:
        raise fail("health-layer1", e) from e
    still_down = sorted(sid for sid in layer1 if not report.health.get(sid))
    if still_down:
        raise fail(
            "health-layer1",
            RuntimeError(f"not healthy within {health_deadline_s:.0f}s: {', '.join(still_down)}"),
        )
    done("health-layer1")

    log.info("layer-0 bring-up complete at %s: %s", sha, ", ".join(report.stages))
    return report


def _admin_token(state: StateDir) -> str:
    """Read ``REGISTRY_ADMIN_TOKEN`` out of the harness-private store.

    The value goes straight into :class:`RegistryClient`, which wraps it in a
    ``_Redacted`` so it cannot reach a log record or an exception message. It is
    never returned to a caller, printed, or put in the report.
    """
    from ams.secrets import store_for

    return store_for(state).load(REGISTRY_ID, ["REGISTRY_ADMIN_TOKEN"])["REGISTRY_ADMIN_TOKEN"]


def _register_identity(state: StateDir, client: RegistryClient, sidecar: Mapping[str, Any]) -> bool:
    """Create one service identity and upsert its ACL. ``True`` = newly created.

    The secret is the one the harness already injects at spawn, read back out of
    the SecretStore in this same process (D16 makes it write-only to *services*,
    not to the harness that owns it). Reading it here rather than generating a
    new one is what makes the identity match the credential the service will
    actually present.
    """
    from ams.platform.registryclient import from_sidecar
    from ams.secrets import store_for

    service_id, kwargs, rules = from_sidecar(sidecar)
    secret = store_for(state).load(service_id, ["SVC_SECRET"])["SVC_SECRET"]
    result = client.create_identity(service_id, secret, **kwargs)
    client.upsert_acl(service_id, rules)
    log.info(
        "registry identity %s: created=%s, %d acl rule(s)",
        service_id,
        result.created,
        len(rules),
    )
    return bool(result.created)


def _wait_tcp(port: int, deadline_s: float, interval_s: float = 1.0) -> bool:
    from ams.health import check_tcp

    return _wait(lambda: check_tcp(port, 2.0), deadline_s, interval_s, f"tcp/{port}")


def _wait_http(port: int, path: str, deadline_s: float, interval_s: float = 1.0) -> bool:
    from ams.health import check_http

    return _wait(lambda: check_http(port, path, 5.0)[0], deadline_s, interval_s, f"{port}{path}")


def _wait(probe: Any, deadline_s: float, interval_s: float, what: str) -> bool:
    started = time.monotonic()
    while True:
        if probe():
            log.info("%s is up after %.1fs", what, time.monotonic() - started)
            return True
        if time.monotonic() - started >= deadline_s:
            log.error("%s still down after %.0fs", what, deadline_s)
            return False
        time.sleep(interval_s)


# --------------------------------------------------------------------------- entry point


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m ams.platform.layer0``.

    Prints the report as one JSON object on stdout, so the bring-up script can
    tee it into the transcript. Nothing printed is derived from a secret.
    """
    parser = argparse.ArgumentParser(
        prog="python -m ams.platform.layer0",
        description="Bring up registry, auth and the gateway; re-point the pilot services.",
    )
    parser.add_argument("--repo-url", required=True, help="api repo URL or local path")
    parser.add_argument("--ref", default="main", help="branch to bring up (default: main)")
    parser.add_argument("--state", metavar="DIR", help="state dir (default: $AMS_STATE_DIR)")
    parser.add_argument("--store", metavar="DIR", help="store dir (default: $AMS_STORE_DIR)")
    parser.add_argument("--caddy-port", type=int, default=CADDY_PORT)
    parser.add_argument("--deadline", type=float, default=DEFAULT_HEALTH_DEADLINE_S)
    parser.add_argument(
        "--no-layer1",
        action="store_true",
        help=(
            "bring up Layer 0 only (registry, auth, gateway) and re-point no pilot "
            "service. For a fresh host: the sync timer owns the fleet, pools included, "
            "and a standalone kvservice/timeservice declared here would have to be "
            "adopted into pool-core on the first tick."
        ),
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    state = StateDir(Path(args.state)) if args.state else StateDir.from_env()
    store = RuntimeStore(Path(args.store)) if args.store else RuntimeStore.from_env()
    try:
        report = bring_up(
            state,
            store,
            repo_url=args.repo_url,
            ref=args.ref,
            caddy_port=args.caddy_port,
            health_deadline_s=args.deadline,
            layer1={} if args.no_layer1 else DEFAULT_LAYER1_MANIFESTS,
        )
    except Layer0Error as e:
        log.error("bring-up failed at %s", e.stage)
        print(json.dumps(e.report.as_dict(), indent=2, sort_keys=True))
        return 1
    print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
