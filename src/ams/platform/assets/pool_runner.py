"""Pool runner: N ASGI services in one process, one ``uvicorn.Server`` per member.

This file is package *data*, not part of the ``ams`` package: it ships with
ams (``src/ams/platform/assets/pool_runner.py``, no ``__init__.py`` in
``assets/``) but ams only ever ``read_bytes()``/``write_bytes()`` it into a
pool's service root. It is the pool's own venv python that imports and runs
it, and that venv has ``fastapi``/``uvicorn`` installed -- ``src/ams`` itself
must never import either. **Do not import anything from ``ams`` here.**

Contract (``.claude/state/PLAN-pool.md`` §4, §3.3):

- Reads ``Path(__file__).with_name("pool.json")`` (version 1; an unknown
  version is a loud, non-zero exit). Each member carries ``id``, ``app``
  (``"pkg.mod:attr"``), ``factory`` (bool), ``port_env`` (the env var that
  holds this member's port), ``health_path`` (informational -- the member's
  own app serves it; the runner does not read it), ``secret_env``
  (``{dst_name: src_name}`` env renames) and ``env`` (the member's full
  injected env, identity keys and shared keys both, minus ``PORT`` and minus
  secrets).
- The *identity* keys (``IDENTITY_KEYS`` below) are swapped in and back out
  around each of a member's three phases -- build, lifespan startup, lifespan
  shutdown -- because T0's AST survey found several services re-reading
  ``load_from_env()`` inside their lifespan, not only at import time. Every
  other key a member declares is folded once into the process environment as
  a union and never restored, because nothing outside lifespan reads it by an
  identity-shaped name.
- A member that raises (``Exception`` *or* ``SystemExit`` -- uvicorn turns a
  failed lifespan startup into ``sys.exit(3)`` inside the task, and
  ``SystemExit`` is a ``BaseException`` a bare ``except Exception`` will not
  catch) at build or startup is logged and marked failed; the rest of the
  pool keeps going. The process exits non-zero only when zero members
  started.
- ``uvicorn.Config.load``/``lifespan_class`` and ``uvicorn.Server.lifespan``/
  ``startup``/``main_loop``/``shutdown`` are internal API, pinned to uvicorn
  0.52.4 by the pool's own provisioning. The runner refuses to start against
  an incompatible uvicorn rather than fail halfway through.
- ``/_pool/health`` on ``POOL_PORT_ADMIN`` is a bare ASGI app (no framework):
  200 with ``{"pool", "ok", "failed"}`` when at least one member is serving,
  503 when none is, 404 otherwise.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Any, NoReturn

import uvicorn

# --------------------------------------------------------------------------- pool.json

_REQUIRED_MEMBER_KEYS = ("id", "app", "factory", "port_env", "health_path", "secret_env", "env")

#: The well-known env var carrying the runner's own admin/health port. Set by
#: the translator in the pool's ``service.toml`` (PLAN-pool §3.3), same as
#: every member's ``port_env``.
ADMIN_PORT_ENV = "POOL_PORT_ADMIN"


class PoolConfigError(Exception):
    """``pool.json`` failed to parse or validate."""


def parse_pool_doc(raw: str) -> dict[str, Any]:
    """Parse and validate a ``pool.json`` document's *text*.

    Split from the file read so it is unit-testable without touching
    ``__file__`` or the filesystem.
    """
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PoolConfigError(f"not valid JSON: {exc}") from exc
    version = doc.get("version")
    if version != 1:
        raise PoolConfigError(
            f"unsupported version {version!r}; this runner only understands version 1"
        )
    members = doc.get("members")
    if not isinstance(members, list) or not members:
        raise PoolConfigError("declares no members")
    for member in members:
        missing = [k for k in _REQUIRED_MEMBER_KEYS if k not in member]
        if missing:
            raise PoolConfigError(f"member {member.get('id', '?')!r} is missing {missing}")
    return doc


def _fatal(message: str) -> NoReturn:
    print(f"FATAL {message}", file=sys.stderr, flush=True)
    sys.exit(1)


def _read_pool_doc() -> dict[str, Any]:
    path = Path(__file__).with_name("pool.json")
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        _fatal(f"pool: cannot read {path}: {exc}")
    try:
        return parse_pool_doc(raw)
    except PoolConfigError as exc:
        _fatal(f"pool: {path}: {exc}")


# --------------------------------------------------------------------------- logging


class _MemberTagFilter(logging.Filter):
    """Injects ``record.ams_member`` for the ``%(ams_member)s`` format field.

    A record that already carries ``ams_member`` (the runner's own control-
    plane messages, logged with ``extra={"ams_member": <id>}``) is left
    alone; everything else -- a member's own loggers -- is tagged by mapping
    the record's top-level logger package to the member id that owns it,
    falling back to ``"pool"``.
    """

    def __init__(self, member_of_package: dict[str, str]) -> None:
        super().__init__()
        self._member_of_package = member_of_package

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "ams_member"):
            top_package = record.name.split(".", 1)[0]
            record.ams_member = self._member_of_package.get(top_package, "pool")
        return True


def _configure_logging(members: list[dict[str, Any]], level_name: str) -> None:
    """Own the root logger before any member is imported.

    The SDK's own ``logging.basicConfig()`` call is gated on
    ``if not logging.getLogger().handlers`` and is a no-op once this has run
    (PLAN-pool §4.7).
    """
    member_of_package: dict[str, str] = {}
    for member in members:
        module_name = str(member["app"]).split(":", 1)[0]
        top_package = module_name.split(".", 1)[0]
        member_of_package[top_package] = member["id"]

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s [%(ams_member)s] %(name)s: %(message)s"))
    handler.addFilter(_MemberTagFilter(member_of_package))

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(getattr(logging, level_name.upper(), logging.INFO))


_log = logging.getLogger("pool")


# --------------------------------------------------------------------------- identity env

#: Swapped per phase (build / lifespan startup / lifespan shutdown), never
#: folded into the shared process env. PLAN-pool §4.2, verified by
#: spike-pool.md finding 2.
IDENTITY_KEYS = frozenset(
    {
        "SVC_NAME",
        "SVC_AUDIENCE",
        "SVC_SECRET",
        "SVC_ROOT_PATH",
        "SVC_ENDPOINT",
        "SVC_CAPABILITIES",
        "SVC_HEALTH_PATH",
        "SVC_DISPLAY_NAME",
        "SVC_OWNER",
        "SVC_LOCATION",
        "PORT",
        "AMS_DATA_DIR",
    }
)


@contextlib.contextmanager
def _identity_env(overlay: dict[str, str]):
    """Apply ``overlay`` to ``os.environ`` and restore exactly on exit.

    "Restore exactly", not "pop the keys": a key ``overlay`` sets may already
    have held an ambient value the process needs afterwards, so the prior
    value (or its absence) is snapshotted per key and put back verbatim.
    """
    prior = {key: os.environ.get(key) for key in overlay}
    os.environ.update(overlay)
    try:
        yield
    finally:
        for key, value in prior.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _member_identity_overlay(member: dict[str, Any]) -> dict[str, str]:
    """The env a member's build/startup/shutdown phase runs under.

    Identity keys already present in ``member["env"]`` (``SVC_NAME`` and
    friends), plus ``PORT`` resolved from ``port_env``, plus any identity key
    that only arrives as a renamed secret (``SVC_SECRET`` is never in
    ``env`` -- it is not a plaintext value ams will write to disk).
    """
    overlay = {k: v for k, v in member["env"].items() if k in IDENTITY_KEYS}
    overlay["PORT"] = os.environ[member["port_env"]]
    for dst, src in member["secret_env"].items():
        overlay[dst] = os.environ[src]
    return overlay


def _apply_non_identity_union(members: list[dict[str, Any]]) -> None:
    """Fold every member's non-identity env keys into the process env, once.

    T0 finding 2: every request-time env read outside ``IDENTITY_KEYS`` uses a
    service-specific name, so the union is safe and nothing needs restoring.
    ``translate`` is responsible for rejecting a pool whose members disagree
    on a shared non-identity key -- the runner does not re-check that here.
    """
    for member in members:
        for key, value in member["env"].items():
            if key not in IDENTITY_KEYS:
                os.environ[key] = value


# --------------------------------------------------------------------------- uvicorn pin

_REQUIRED_UVICORN_API = (
    ("Config", "load"),
    ("Config", "lifespan_class"),
    ("Server", "lifespan"),
    ("Server", "startup"),
    ("Server", "main_loop"),
    ("Server", "shutdown"),
)


def _check_uvicorn_api(uvicorn_module: Any) -> None:
    """Refuse to start against an uvicorn missing the internal API this runner
    pins to (PLAN-pool §4.4) -- a loud refusal at second zero beats a pool
    that dies halfway through startup."""
    version = getattr(uvicorn_module, "__version__", "unknown")
    for cls_name, attr in _REQUIRED_UVICORN_API:
        cls = getattr(uvicorn_module, cls_name, None)
        if cls is None or not hasattr(cls, attr):
            _fatal(
                f"pool: uvicorn {version} lacks {cls_name}.{attr}; pool runner requires "
                "the 0.52.x internal API"
            )


# --------------------------------------------------------------------------- member lifecycle


def _resolve_app(ref: str, factory: bool) -> Any:
    module_name, sep, attr_name = ref.partition(":")
    if not sep or not attr_name:
        raise ValueError(f"malformed app reference {ref!r}; expected 'pkg.mod:attr'")
    module = importlib.import_module(module_name)
    target = getattr(module, attr_name)
    return target() if factory else target


def _build_member(member: dict[str, Any]) -> tuple[int, uvicorn.Server] | None:
    """Import, build and construct one member's ``uvicorn.Server`` -- no I/O yet.

    Catches ``Exception`` *and* ``SystemExit``: a bad import or a factory that
    calls ``sys.exit`` must not take the rest of the pool down with it.
    """
    mid = member["id"]
    try:
        port = int(os.environ[member["port_env"]])
        overlay = _member_identity_overlay(member)
        with _identity_env(overlay):
            app = _resolve_app(member["app"], member["factory"])
            config = uvicorn.Config(
                app, host="127.0.0.1", port=port, log_config=None, access_log=False
            )
            config.load()
            server = uvicorn.Server(config)
            server.lifespan = config.lifespan_class(config)
    except (Exception, SystemExit) as exc:
        _log.error(
            "pool: build failed: %s: %s", type(exc).__name__, exc, extra={"ams_member": mid}
        )
        return None
    return port, server


async def _startup_member(member: dict[str, Any], server: uvicorn.Server) -> bool:
    """Run one member's lifespan startup (binds its socket) under its identity env.

    Sequential by design -- see the module docstring and PLAN-pool §4.3 step 4.
    """
    mid = member["id"]
    try:
        overlay = _member_identity_overlay(member)
        with _identity_env(overlay):
            await server.startup()
    except (Exception, SystemExit) as exc:
        _log.error(
            "pool: startup failed: %s: %s", type(exc).__name__, exc, extra={"ams_member": mid}
        )
        return False
    return True


async def _shutdown_member(member: dict[str, Any], server: uvicorn.Server) -> None:
    """Run one member's lifespan shutdown, again under its identity env."""
    mid = member["id"]
    try:
        overlay = _member_identity_overlay(member)
        with _identity_env(overlay):
            await server.shutdown()
    except (Exception, SystemExit) as exc:
        _log.error(
            "pool: shutdown failed: %s: %s", type(exc).__name__, exc, extra={"ams_member": mid}
        )


# --------------------------------------------------------------------------- admin listener


async def _admin_lifespan(receive: Any, send: Any) -> None:
    while True:
        message = await receive()
        if message["type"] == "lifespan.startup":
            await send({"type": "lifespan.startup.complete"})
        elif message["type"] == "lifespan.shutdown":
            await send({"type": "lifespan.shutdown.complete"})
            return


def make_admin_app(pool_name: str, started: dict[str, Any], failed: dict[str, str]) -> Any:
    """A bare ASGI callable (no FastAPI) serving ``GET /_pool/health``.

    ``started``/``failed`` are read live at request time, not snapshotted, so
    the endpoint reflects the pool's state at every poll -- exposed at module
    level so tests can drive it directly with a hand-rolled scope/receive/send.
    """

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            await _admin_lifespan(receive, send)
            return
        if scope["type"] != "http":
            return
        if scope["method"] == "GET" and scope["path"] == "/_pool/health":
            ok = sorted(started)
            status = 200 if ok else 503
            body = json.dumps({"pool": pool_name, "ok": ok, "failed": dict(failed)}).encode()
        else:
            status = 404
            body = b'{"detail": "not found"}'
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body})

    return app


# --------------------------------------------------------------------------- orchestration


async def _serve(doc: dict[str, Any]) -> int:
    members: list[dict[str, Any]] = doc["members"]
    _apply_non_identity_union(members)

    built: dict[str, tuple[int, uvicorn.Server]] = {}
    failed: dict[str, str] = {}
    for member in members:
        result = _build_member(member)
        if result is None:
            failed[member["id"]] = "build failed"
        else:
            built[member["id"]] = result

    started: dict[str, tuple[int, uvicorn.Server]] = {}
    for member in members:
        mid = member["id"]
        if mid not in built:
            continue
        port, server = built[mid]
        if await _startup_member(member, server):
            started[mid] = (port, server)
        else:
            failed[mid] = "startup failed"

    if not started:
        _log.error("pool: no members started; exiting", extra={"ams_member": "pool"})
        return 1

    admin_app = make_admin_app(doc.get("pool", ""), started, failed)
    admin_port = int(os.environ[ADMIN_PORT_ENV])
    admin_config = uvicorn.Config(
        admin_app, host="127.0.0.1", port=admin_port, log_config=None, access_log=False
    )
    admin_config.load()
    admin_server = uvicorn.Server(admin_config)
    admin_server.lifespan = admin_config.lifespan_class(admin_config)
    try:
        await admin_server.startup()
    except (Exception, SystemExit) as exc:
        _log.error(
            "pool: admin listener failed to start: %s: %s",
            type(exc).__name__,
            exc,
            extra={"ams_member": "pool"},
        )
        return 1

    running_servers = [server for _, server in started.values()] + [admin_server]

    loop = asyncio.get_running_loop()

    def _request_exit(*_args: object) -> None:
        for server in running_servers:
            server.should_exit = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _request_exit)

    tasks = [asyncio.create_task(server.main_loop()) for server in running_servers]
    await asyncio.gather(*tasks)

    for member in members:
        mid = member["id"]
        if mid in started:
            _, server = started[mid]
            await _shutdown_member(member, server)
    await admin_server.shutdown()

    return 0


def main() -> int:
    doc = _read_pool_doc()
    _configure_logging(doc["members"], os.environ.get("LOG_LEVEL", "INFO"))
    _check_uvicorn_api(uvicorn)
    return asyncio.run(_serve(doc))


if __name__ == "__main__":
    sys.exit(main())
