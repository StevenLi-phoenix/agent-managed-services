"""Translate an api ``service.yaml`` manifest into an ams declaration + sidecars.

One manifest describes three separable things: how to run a process, where it is
mounted on the gateway, and what the registry should know about it. Only the
first belongs in a ``ServiceDecl`` -- a supervisor that knows about HTTP mounts
and ACL rules stops being a supervisor (D7) -- so this module returns a
``Translation`` carrying the declaration plus the two sidecar documents
described in ``docs/platform-sidecars.md``.

The guiding rule is PLAN-allin risk 1: **reject rather than guess**. An unknown
key, an unrecognised install command, an env value pointing somewhere we cannot
reproduce -- each raises ``TranslateError`` naming the manifest field path, so
the agent repairs the manifest or extends the translator deliberately. A
mis-translated manifest deploys the wrong thing silently; a raise does not.

See ``docs/manifest-translation.md`` for the field-by-field mapping table.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

from ams.platform.yamlsubset import parse as parse_yaml
from ams.schema import (
    PORT_NAME_RE,
    RESERVED_ENV,
    RESERVED_ENV_PREFIXES,
    SERVICE_ID_RE,
    HealthSpec,
    LimitsSpec,
    LoggingSpec,
    RestartSpec,
    RuntimeSpec,
    ServiceDecl,
    StartSpec,
    StopSpec,
    parse_size,
)

__all__ = [
    "TranslateError",
    "TranslateContext",
    "Translation",
    "translate",
    "emit_toml",
    "PORT_NAME",
    "SIDECAR_VERSION",
    "DEPENDS_ON",
    "REGISTRY_SERVICE_ID",
    "IDENTITY_ENV",
    "POOL_ADMIN_PORT_NAME",
    "POOL_ID_PREFIX",
    "POOL_JSON_VERSION",
    "POOL_UVICORN_PIN",
    "PoolMember",
    "PoolTranslation",
    "build_pool",
    "mangle_member",
    "pool_member",
    "pool_port_name",
]

SIDECAR_VERSION = 1
#: Every translated service declares exactly one port under this name; the
#: manifest's ``mount.port`` is production's fixed number and is discarded.
PORT_NAME = "main"
#: Q8: manifest ``memory_max`` where present, else this, never below the floor.
DEFAULT_MEMORY_MAX = "150M"
MEMORY_FLOOR = "120M"
#: Every translated (Layer-1) service registers with the registry during its
#: FastAPI startup (``sdk.registry.start()``), and a refused connection there is
#: a hard "Application startup failed" -- observed on racknerd on 2026-09-02,
#: when a harness restart brought all 18 services up at once and 11 of them
#: burned their whole retry budget before the registry was listening. So every
#: translated declaration waits for the registry to be *healthy*. Layer 0 itself
#: (registry, auth) and the gateway are declared by ``bootstrap``/``gateway``
#: and depend on nothing.
REGISTRY_SERVICE_ID = "registry"
DEPENDS_ON = (REGISTRY_SERVICE_ID,)

# --------------------------------------------------------------------------- pool
#: The pooled runner's own admin/health listener (PLAN-pool §3.3). It is a port
#: name in the pool declaration, which is why no member id may equal it.
POOL_ADMIN_PORT_NAME = "pool"
#: ``pool = "core"`` in a ``service.ams.toml`` overlay becomes service id
#: ``pool-core``. The prefix keeps a pool from ever colliding with a member id.
POOL_ID_PREFIX = "pool-"
POOL_JSON_VERSION = 1
POOL_HEALTH_PATH = "/_pool/health"
#: PLAN-pool §4.4: the runner drives uvicorn's *internal* API (``Config.load``,
#: ``Server.lifespan``, ``Server.startup``), so the version is pinned here and
#: bumped deliberately, never by a resolver widening the range.
POOL_UVICORN_PIN = "uvicorn[standard]==0.52.4"

#: Swapped per member around build / lifespan startup / lifespan shutdown
#: (PLAN-pool §4.2, from spike-pool finding 1). Everything NOT in this set is
#: unioned into the pool's single process env, which is only safe while the
#: union is unambiguous -- hence ``build_pool``'s conflict check.
IDENTITY_ENV = frozenset(
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

_LOOPBACK_URL_RE = re.compile(r"^http://127\.0\.0\.1:(\d{1,5})$")
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
_INSTALL_RE = re.compile(r"^cd (?P<rel>[A-Za-z0-9._/-]+) && (?:/usr/local/bin/)?uv sync$")
_PORT_REF_RE = re.compile(r"\$\{([^}]*)\}")
_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")

_TOP_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "name",
        "display_name",
        "audience",
        "owner",
        "manual_restart",
        "deploy",
        "process",
        "mount",
        "acl",
        "registry",
    }
)
_DEPLOY_KEYS = frozenset({"source", "install", "target_dir", "user"})
_SOURCE_KEYS = frozenset({"type", "repo", "branch", "path"})
_PROCESS_KEYS = frozenset(
    {"exec", "working_dir", "environment", "restart", "restart_sec", "memory_max"}
)
_MOUNT_KEYS = frozenset({"gateway", "path", "subdomain", "port"})
_ACL_KEYS = frozenset({"action", "principal", "effect"})
_REGISTRY_KEYS = frozenset({"capabilities", "health_path"})

_RESTART_MAP = {"no": "never", "on-failure": "on-failure", "always": "always"}

#: Environment the harness injects for every translated service. A manifest may
#: only redefine the two URLs (production points them at fixed loopback ports;
#: the replica allocates its own), and the context value wins there too.
_MANIFEST_MAY_ALSO_SET = frozenset({"REGISTRY_URL", "AUTH_URL"})


class TranslateError(ValueError):
    """A manifest this translator refuses to translate. Message names the field path."""


def _err(path: str, msg: str) -> TranslateError:
    return TranslateError(f"{path}: {msg}" if path else msg)


def _join(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


# --------------------------------------------------------------------------- context


@dataclass(frozen=True)
class TranslateContext:
    """Everything the translation needs that the manifest cannot supply.

    ``services_dir`` is the ams state ``services/`` directory. The service root
    is ``<services_dir>/<id>/root``, and ``id`` only exists once the manifest is
    parsed -- hence a directory here rather than a per-service root.

    ``pool`` is the one field that changes what ``translate`` emits rather than
    only its numbers: a member of pool ``core`` has no root, no venv and no
    service.toml of its own, so every absolute path it is given must already
    point into ``<services_dir>/pool-core/root``. Doing that here -- rather than
    string-rewriting a finished standalone ``Translation`` afterwards -- keeps
    exactly one place that knows how a service root is spelled, and lets
    ``_rewrite_data_path``'s "points at another service's state dir" rejection
    keep working unchanged (PLAN-pool §3.3, §5.3).
    """

    sha: str
    services_dir: Path
    registry_url: str
    auth_url: str
    extra_secret_names: tuple[str, ...] = ()
    default_memory_max: str = DEFAULT_MEMORY_MAX
    memory_floor: str = MEMORY_FLOOR
    cpu_max: str = "40%"
    pids_max: int = 64
    start_period_s: float = 120.0
    #: Unprefixed pool name (``"core"``) when this manifest is a pooled member.
    pool: str | None = None
    #: PLAN-pool §3.3: derived from the n=1 import measurement, so these are
    #: starting values to be re-derived on the box (T10), not facts.
    pool_memory_base: str = "150M"
    pool_memory_per_member: str = "30M"
    pool_pids_base: int = 48
    pool_pids_per_member: int = 16
    pool_cpu_max: str = "100%"
    pool_start_period_s: float = 240.0
    pool_runner_name: str = "pool_runner.py"
    pool_uvicorn_pin: str = POOL_UVICORN_PIN

    def __post_init__(self) -> None:
        if not _SHA_RE.match(self.sha):
            raise _err("ctx.sha", f"{self.sha!r} is not a lowercase hex git sha")
        object.__setattr__(self, "services_dir", Path(self.services_dir))
        # PLAN-allin risk 5: an auto-generated SVC_SECRET plus identity creation
        # against a non-loopback registry would write into PRODUCTION. Phase A
        # allows nothing but the local replica, and this is the only gate.
        for fname in ("registry_url", "auth_url"):
            url = getattr(self, fname)
            if not _LOOPBACK_URL_RE.match(url):
                raise _err(
                    f"ctx.{fname}",
                    f"{url!r} is not loopback; Phase A requires http://127.0.0.1:<port> "
                    "so identity creation can never reach production",
                )
        try:
            floor = parse_size(self.memory_floor)
            if parse_size(self.default_memory_max) < floor:
                raise _err("ctx.default_memory_max", "must not be below ctx.memory_floor")
        except ValueError as e:
            if isinstance(e, TranslateError):
                raise
            raise _err("ctx.memory_floor", str(e)) from None
        for name in self.extra_secret_names:
            if not _ENV_NAME_RE.match(name):
                raise _err("ctx.extra_secret_names", f"{name!r} is not an env var name")
        if self.pool is not None and not SERVICE_ID_RE.match(self.pool):
            raise _err("ctx.pool", f"{self.pool!r} does not match {SERVICE_ID_RE.pattern}")
        for fname in ("pool_memory_base", "pool_memory_per_member"):
            try:
                parse_size(getattr(self, fname))
            except ValueError as e:
                raise _err(f"ctx.{fname}", str(e)) from None

    def root_for(self, service_id: str) -> PurePosixPath:
        """Absolute service root as seen on the target host."""
        return PurePosixPath(self.services_dir.as_posix()) / service_id / "root"

    @property
    def pool_id(self) -> str | None:
        """``"pool-core"`` when this context translates a member of pool ``core``."""
        return None if self.pool is None else POOL_ID_PREFIX + self.pool


@dataclass(frozen=True)
class Translation:
    """Result of translating one manifest.

    ``decl`` and ``registry`` are ``None`` for ``kind: static`` manifests: a
    static site produces no ams service and no registry record.
    """

    id: str
    kind: str
    decl: ServiceDecl | None
    mount: Mapping[str, Any]
    registry: Mapping[str, Any] | None
    flags: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))


# --------------------------------------------------------------------------- helpers


def _table(data: Any, path: str, allowed: frozenset[str]) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise _err(path, f"expected a mapping, got {type(data).__name__}")
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise _err(path, f"unknown keys {unknown}; allowed: {sorted(allowed)}")
    return data


def _req_str(data: Mapping[str, Any], key: str, path: str) -> str:
    if key not in data:
        raise _err(_join(path, key), "required")
    value = data[key]
    if not isinstance(value, str) or not value:
        raise _err(_join(path, key), "expected a non-empty string")
    return value


def _opt_str(data: Mapping[str, Any], key: str, path: str) -> str | None:
    if key not in data or data[key] is None:
        return None
    value = data[key]
    if not isinstance(value, str):
        raise _err(_join(path, key), "expected a string")
    return value


def _forbid(data: Mapping[str, Any], keys: Sequence[str], path: str, why: str) -> None:
    for key in keys:
        if key in data:
            raise _err(_join(path, key), why)


def _env_value(value: Any, path: str) -> str:
    if isinstance(value, bool):
        # systemd stringifies these; YAML/JSON/TOML disagree on the spelling, so
        # there is no honest single answer. Quote it in the manifest instead.
        raise _err(path, "boolean env value is ambiguous; quote it in the manifest")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return value
    raise _err(path, f"expected a string, got {type(value).__name__}")


# --------------------------------------------------------------------------- pieces


def _split_argv(exec_str: str, service_id: str) -> tuple[str, ...]:
    """``process.exec`` -> argv, with the venv-absolute entry point made bare.

    ``runtime_env`` prepends the provisioned venv's ``bin`` to PATH, so
    ``/srv/<n>/<rel>/.venv/bin/uvicorn`` becomes plain ``uvicorn``. Any other
    ``/srv/`` path is a location that does not exist under ams and is rejected
    rather than rewritten by guesswork.
    """
    try:
        argv = shlex.split(exec_str)
    except ValueError as e:
        raise _err("process.exec", f"cannot split into argv: {e}") from None
    if not argv:
        raise _err("process.exec", "empty command line")

    head = argv[0]
    if not head.startswith("/"):
        raise _err("process.exec", f"first token {head!r} must be an absolute path")
    p = PurePosixPath(head)
    if p.parent.name == "bin" and p.parent.parent.name == ".venv":
        argv[0] = p.name
    elif head.startswith("/srv/"):
        raise _err(
            "process.exec",
            f"{head!r} is under /srv/, which does not exist under ams, and is not a "
            "'.venv/bin/<tool>' entry point this translator knows how to rewrite",
        )

    out: list[str] = []
    for i, tok in enumerate(argv):
        refs = _PORT_REF_RE.findall(tok)
        for ref in refs:
            if ref != "PORT":
                raise _err(
                    f"process.exec[{i}]",
                    f"unsupported substitution ${{{ref}}}; only ${{PORT}} is translated",
                )
        tok = tok.replace("${PORT}", "${PORT_" + PORT_NAME + "}")
        if "$" in tok.replace("${PORT_" + PORT_NAME + "}", ""):
            raise _err(
                f"process.exec[{i}]",
                f"{tok!r} contains a '$' that no shell will expand (argv is never "
                "handed to a shell)",
            )
        out.append(tok)
    if not out[0]:
        raise _err("process.exec", "empty argv[0]")
    _ = service_id
    return tuple(out)


def _rel_under_srv(value: str, service_id: str, path: str) -> str:
    prefix = f"/srv/{service_id}"
    if value == prefix:
        return ""
    if not value.startswith(prefix + "/"):
        raise _err(path, f"{value!r} must be {prefix} or a path under it")
    rel = value[len(prefix) + 1 :]
    if not rel or ".." in PurePosixPath(rel).parts:
        raise _err(path, f"{value!r} is not a usable relative path")
    return rel


def _runtime_rel(install: Any, working_rel: str) -> str:
    """``deploy.install`` -> the uv project directory, or raise."""
    if not isinstance(install, list) or not all(isinstance(x, str) for x in install):
        raise _err("deploy.install", "expected a list of strings")
    if len(install) != 1:
        raise _err(
            "deploy.install",
            f"expected exactly one 'cd <dir> && uv sync' command, got {len(install)}",
        )
    m = _INSTALL_RE.match(install[0])
    if not m:
        raise _err(
            "deploy.install[0]",
            f"{install[0]!r} is not the one recognised form "
            "'cd <dir> && uv sync' (D1 forbids running shell strings)",
        )
    rel = m.group("rel").rstrip("/")
    if rel != working_rel:
        raise _err(
            "deploy.install[0]",
            f"installs in {rel!r} but process.working_dir is {working_rel!r}",
        )
    return rel


def _memory_max(raw: str | None, ctx: TranslateContext) -> str:
    if raw is None:
        return ctx.default_memory_max
    if not isinstance(raw, str):
        raise _err("process.memory_max", "expected a string like '200M'")
    try:
        want = parse_size(raw)
    except ValueError as e:
        raise _err("process.memory_max", str(e)) from None
    # Q8: the pilot raised kvservice from 100M to 150M because 100M sat on the
    # page-cache line. A manifest asking for less than the floor gets the floor.
    return ctx.memory_floor if want < parse_size(ctx.memory_floor) else raw


def _mount(
    data: Any,
    service_id: str,
    kind: str,
    build: Sequence[str],
    *,
    port_owner: str | None = None,
    port_name: str = PORT_NAME,
) -> dict[str, Any]:
    m = _table(data, "mount", _MOUNT_KEYS)
    gateway = _req_str(m, "gateway", "mount")
    path = _opt_str(m, "path", "mount")
    subdomain = _opt_str(m, "subdomain", "mount")
    if (path is None) == (subdomain is None):
        raise _err("mount", "exactly one of 'path' and 'subdomain' is required")
    if path is not None and not path.startswith("/"):
        raise _err("mount.path", f"{path!r} must start with '/'")
    if subdomain is not None and not gateway.startswith(subdomain + "."):
        raise _err("mount.subdomain", f"gateway {gateway!r} must start with '{subdomain}.'")

    if kind == "static":
        _forbid(m, ["port"], "mount", "forbidden for kind=static (Caddy serves files directly)")
        resolved_port_name: str | None = None
        static_root: str | None = service_id
    else:
        if "port" not in m:
            raise _err("mount.port", "required for kind=service")
        if not isinstance(m["port"], int) or isinstance(m["port"], bool):
            raise _err("mount.port", "expected an integer")
        # Discarded on purpose: ams allocates the port and the gateway renderer
        # resolves `port_name` against the live allocation (docs/platform-sidecars.md).
        resolved_port_name = port_name
        static_root = None

    out: dict[str, Any] = {
        "version": SIDECAR_VERSION,
        "id": service_id,
        "kind": kind,
        "gateway": gateway,
        "path": path,
        "subdomain": subdomain,
        "port_name": resolved_port_name,
        # Additive and OPTIONAL: absent means "the port is allocated on my own
        # id", which is every non-pooled service. Emitting it as null instead
        # would rewrite all 21 mount goldens for no gain (D26's precedent).
        **({"port_owner": port_owner} if port_owner is not None else {}),
        "static_root": static_root,
        "build": list(build),
        "headers": {},
    }
    return out


def _acl(data: Any) -> list[dict[str, str]]:
    if not isinstance(data, list):
        raise _err("acl", "expected a list of rules")
    out: list[dict[str, str]] = []
    for i, rule in enumerate(data):
        r = _table(rule, f"acl[{i}]", _ACL_KEYS)
        action = _req_str(r, "action", f"acl[{i}]")
        if action not in ("read", "write", "*"):
            raise _err(f"acl[{i}].action", f"{action!r} not one of read/write/*")
        principal = _req_str(r, "principal", f"acl[{i}]")
        if not re.match(r"^(anon|admin|user:.+|service:.+)$", principal):
            raise _err(f"acl[{i}].principal", f"{principal!r} does not match the registry regex")
        effect = _opt_str(r, "effect", f"acl[{i}]") or "allow"
        if effect not in ("allow", "deny"):
            raise _err(f"acl[{i}].effect", f"{effect!r} not one of allow/deny")
        out.append({"action": action, "principal": principal, "effect": effect})
    return out


# --------------------------------------------------------------------------- translate


def translate(text: str, ctx: TranslateContext) -> Translation:
    """Translate one ``service.yaml`` document. Raises ``TranslateError``."""
    data = parse_yaml(text)
    doc = _table(data, "", _TOP_KEYS) if isinstance(data, dict) else None
    if doc is None:
        raise _err("", "manifest must be a mapping")

    if doc.get("schema_version") != 1:
        raise _err("schema_version", f"expected 1, got {doc.get('schema_version')!r}")

    kind = doc.get("kind", "service")
    if kind not in ("service", "static"):
        raise _err("kind", f"{kind!r} not one of service/static")

    service_id = _req_str(doc, "name", "")
    if not SERVICE_ID_RE.match(service_id):
        raise _err("name", f"{service_id!r} must match ams {SERVICE_ID_RE.pattern}")

    display_name = _opt_str(doc, "display_name", "")
    owner = _opt_str(doc, "owner", "")
    manual_restart = doc.get("manual_restart", False)
    if not isinstance(manual_restart, bool):
        raise _err("manual_restart", "expected a boolean")

    deploy = _table(doc.get("deploy"), "deploy", _DEPLOY_KEYS)
    source = _table(deploy.get("source"), "deploy.source", _SOURCE_KEYS)
    if _req_str(source, "type", "deploy.source") != "git":
        raise _err("deploy.source.type", "only 'git' is supported")
    _req_str(source, "repo", "deploy.source")
    target_dir = _req_str(deploy, "target_dir", "deploy")
    if target_dir != f"/srv/{service_id}":
        raise _err("deploy.target_dir", f"expected '/srv/{service_id}', got {target_dir!r}")

    mount_flags = {"manual_restart": manual_restart if kind == "service" else False}

    if kind == "static":
        if ctx.pool is not None:
            # PLAN-pool §3.2. The overlay reader sees one file and cannot know
            # the kind, so the rejection has to land here.
            raise _err("kind", "pool is not valid for kind: static")
        return _translate_static(doc, deploy, service_id, mount_flags)
    return _translate_service(
        doc, deploy, source, service_id, display_name, owner, mount_flags, ctx
    )


def _translate_static(
    doc: Mapping[str, Any],
    deploy: Mapping[str, Any],
    service_id: str,
    flags: Mapping[str, Any],
) -> Translation:
    _forbid(
        doc,
        ["process", "audience", "acl", "registry"],
        "",
        "forbidden for kind=static (no process, no registry record)",
    )
    _forbid(deploy, ["user"], "deploy", "forbidden for kind=static (Caddy reads files directly)")
    build = deploy.get("install", [])
    if not isinstance(build, list) or not all(isinstance(x, str) for x in build):
        raise _err("deploy.install", "expected a list of strings")
    mount = _mount(doc.get("mount"), service_id, "static", build)
    return Translation(
        id=service_id,
        kind="static",
        decl=None,
        mount=MappingProxyType(mount),
        registry=None,
        flags=MappingProxyType(dict(flags)),
    )


def _translate_service(  # noqa: C901 - one flat mapping is clearer than five hops
    doc: Mapping[str, Any],
    deploy: Mapping[str, Any],
    source: Mapping[str, Any],
    service_id: str,
    display_name: str | None,
    owner: str | None,
    flags: Mapping[str, Any],
    ctx: TranslateContext,
) -> Translation:
    if "path" in source:
        raise _err(
            "deploy.source.path",
            "sub-tree staging is only supported for kind=static; a service needs the "
            "whole monorepo so its relative SDK path dependency resolves",
        )
    audience = _req_str(doc, "audience", "")
    _req_str(deploy, "user", "deploy")  # required by the api schema; ams maps uids itself

    proc = _table(doc.get("process"), "process", _PROCESS_KEYS)
    argv = _split_argv(_req_str(proc, "exec", "process"), service_id)
    working_rel = _rel_under_srv(
        _req_str(proc, "working_dir", "process"), service_id, "process.working_dir"
    )
    runtime_rel = _runtime_rel(deploy.get("install"), working_rel)
    workdir = f"repo/{runtime_rel}"

    registry_tbl = _table(doc.get("registry", {}), "registry", _REGISTRY_KEYS)
    capabilities = registry_tbl.get("capabilities", [])
    if not isinstance(capabilities, list) or not all(isinstance(x, str) for x in capabilities):
        raise _err("registry.capabilities", "expected a list of strings")
    health_path = registry_tbl.get("health_path", "/health")
    if not isinstance(health_path, str) or not health_path.startswith("/"):
        raise _err("registry.health_path", "expected a path starting with '/'")

    # A pooled member has no root of its own: it runs inside the pool process,
    # under the pool's root, with its state one directory deeper (§3.3).
    pool_id = ctx.pool_id
    root = ctx.root_for(pool_id if pool_id is not None else service_id)
    env = _build_env(
        proc.get("environment", {}),
        service_id=service_id,
        audience=audience,
        display_name=display_name,
        owner=owner,
        capabilities=capabilities,
        health_path=health_path,
        root=root,
        ctx=ctx,
        data_subdir=service_id if pool_id is not None else None,
    )

    restart_raw = proc.get("restart", "on-failure")
    if restart_raw not in _RESTART_MAP:
        raise _err("process.restart", f"{restart_raw!r} not one of {sorted(_RESTART_MAP)}")
    restart_sec = proc.get("restart_sec", 10)
    if not isinstance(restart_sec, int) or isinstance(restart_sec, bool) or restart_sec < 0:
        raise _err("process.restart_sec", "expected a non-negative integer")

    secrets = ("SVC_SECRET", *ctx.extra_secret_names)
    if len(set(secrets)) != len(secrets):
        raise _err("ctx.extra_secret_names", "duplicates SVC_SECRET or itself")

    decl = ServiceDecl(
        id=service_id,
        name=display_name or "",
        start=StartSpec(argv=argv, workdir=workdir),
        env=env,
        ports={PORT_NAME: 0},
        runtime=RuntimeSpec(kind="uv", python="3.12", sync=True),
        health=HealthSpec(
            kind="http",
            port=PORT_NAME,
            path=health_path,
            start_period_s=ctx.start_period_s,
        ),
        stop=StopSpec(signal="SIGTERM", timeout_s=10.0),
        limits=LimitsSpec(
            memory_max=_memory_max(proc.get("memory_max"), ctx),
            cpu_max=ctx.cpu_max,
            pids_max=ctx.pids_max,
        ),
        restart=RestartSpec(policy=_RESTART_MAP[restart_raw], backoff_s=float(restart_sec)),
        secrets=secrets,
        depends_on=DEPENDS_ON,
    )

    mount = _mount(
        doc.get("mount"),
        service_id,
        "service",
        (),
        port_owner=pool_id,
        port_name=pool_port_name(service_id) if pool_id is not None else PORT_NAME,
    )
    registry = {
        "version": SIDECAR_VERSION,
        "id": service_id,
        "audience": audience,
        "display_name": display_name,
        "owner": owner,
        "capabilities": list(capabilities),
        "health_path": health_path,
        "acl": _acl(doc.get("acl", [])),
    }
    return Translation(
        id=service_id,
        kind="service",
        decl=decl,
        mount=MappingProxyType(mount),
        registry=MappingProxyType(registry),
        flags=MappingProxyType(dict(flags)),
    )


def _build_env(
    raw: Any,
    *,
    service_id: str,
    audience: str,
    display_name: str | None,
    owner: str | None,
    capabilities: Sequence[str],
    health_path: str,
    root: PurePosixPath,
    ctx: TranslateContext,
    data_subdir: str | None = None,
) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise _err("process.environment", "expected a mapping")

    injected: dict[str, str] = {
        "SVC_NAME": service_id,
        "SVC_AUDIENCE": audience,
        "PORT": "${PORT_" + PORT_NAME + "}",
        "GIT_COMMIT": ctx.sha,
        "SVC_HEALTH_PATH": health_path,
        # /etc/auth/jwt-rs256.pub is unreachable for a mapped uid, so the key is
        # materialised per service under its own root (Q4).
        "SVC_M2M_PUBLIC_KEY_PATH": f"{root}/etc/jwt-rs256.pub",
        "REGISTRY_URL": ctx.registry_url,
        "AUTH_URL": ctx.auth_url,
    }
    if capabilities:
        injected["SVC_CAPABILITIES"] = ",".join(capabilities)
    if display_name:
        injected["SVC_DISPLAY_NAME"] = display_name
    if owner:
        injected["SVC_OWNER"] = owner

    env: dict[str, str] = {}
    for key, value in raw.items():
        path = f"process.environment.{key}"
        if not isinstance(key, str) or not _ENV_NAME_RE.match(key):
            raise _err(path, "invalid environment variable name")
        if key in RESERVED_ENV or key.startswith(RESERVED_ENV_PREFIXES):
            raise _err(path, "reserved: the harness sets it (PATH/HOME/AMS_*/PORT_*/...)")
        if key in injected and key not in _MANIFEST_MAY_ALSO_SET:
            raise _err(path, "the harness injects this variable; remove it from the manifest")
        env[key] = _rewrite_data_path(
            _env_value(value, path), service_id, root, path, data_subdir=data_subdir
        )
    env.update(injected)
    return env


def _rewrite_data_path(
    value: str,
    service_id: str,
    root: PurePosixPath,
    path: str,
    *,
    data_subdir: str | None = None,
) -> str:
    """``/var/lib/<name>/x`` -> ``<root>/data/x`` (the pilot's finding #5).

    Every stateful service points at ``/var/lib/<name>``, which a mapped uid
    cannot write. The declaration schema only expands ``${PORT_*}``, so the
    substitution is baked in as an absolute path rather than a token.

    ``data_subdir`` is the member id for a pooled service: N members share one
    root, so each gets ``<pool-root>/data/<member>/`` and ``backup.discover``
    can still name the owner of a database it finds (PLAN-pool §5.6).
    """
    own = f"/var/lib/{service_id}"
    data = f"{root}/data" if data_subdir is None else f"{root}/data/{data_subdir}"
    if value == own:
        return data
    if value.startswith(own + "/"):
        return f"{data}/{value[len(own) + 1 :]}"
    if value.startswith("/var/lib/"):
        raise _err(
            path,
            f"{value!r} points at another service's state dir; only /var/lib/{service_id} "
            "is rewritten to the service's own data dir",
        )
    return value


# --------------------------------------------------------------------------- pool

_APP_TARGET_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$")
_SIZE_UNITS = (("T", 1 << 40), ("G", 1 << 30), ("M", 1 << 20), ("K", 1 << 10))


@dataclass(frozen=True)
class PoolMember:
    """One manifest's contribution to a pool.

    ``translation`` is the member's own, still carrying the mount and registry
    sidecars sync writes per member -- pooling changes how a member is *run*,
    not how it is routed or registered.
    """

    id: str
    translation: Translation
    #: uv project directory relative to the repo root, e.g. ``services/kvservice``.
    runtime_rel: str
    #: ``"<module>:<attr>"`` parsed out of the manifest's ``process.exec``.
    app: str
    factory: bool
    #: The ``IDENTITY_ENV`` subset, swapped around this member's build/lifespan.
    identity_env: Mapping[str, str]
    #: Everything else, unioned into the pool's one process env.
    shared_env: Mapping[str, str]
    #: Unmangled, e.g. ``("SVC_SECRET", "DEEPSEEK_API_KEY")``.
    secret_names: tuple[str, ...]

    @property
    def port_name(self) -> str:
        return pool_port_name(self.id)

    @property
    def port_env(self) -> str:
        return "POOL_PORT_" + mangle_member(self.id)


@dataclass(frozen=True)
class PoolTranslation:
    """N manifests as one declaration plus the ``pool.json`` the runner reads."""

    id: str
    decl: ServiceDecl
    members: tuple[PoolMember, ...]
    pool_json: Mapping[str, Any]


def mangle_member(member_id: str) -> str:
    """``"notification-svc"`` -> ``"NOTIFICATION_SVC"``.

    Env var names have no hyphen, so the mapping is lossy in principle. The
    caller (``build_pool``) checks injectivity across the pool rather than
    pretending it cannot collide.
    """
    return member_id.replace("-", "_").upper()


def pool_port_name(member_id: str) -> str:
    """The pool's port name for one member: its id verbatim where that is legal.

    ``PORT_NAME_RE`` allows no hyphen, so ``files-web`` would be rejected by the
    schema; the lower-cased mangled form is used instead and recorded in
    ``pool.json`` so nothing downstream has to re-derive the rule.
    """
    return member_id if PORT_NAME_RE.match(member_id) else member_id.replace("-", "_")


def _format_size(n: int) -> str:
    for suffix, mult in _SIZE_UNITS:
        if n >= mult and n % mult == 0:
            return f"{n // mult}{suffix}"
    return str(n)


def _parse_uvicorn_exec(argv: Sequence[str], member_id: str) -> tuple[str, bool]:
    """``uvicorn <mod>:<attr> [--factory] --host 127.0.0.1 --port ...`` -> app, factory.

    The runner constructs ``uvicorn.Config`` itself, so any option that would
    have changed how the server is built is a silent behaviour change once the
    argv is thrown away. Reject it instead of dropping it.
    """
    path = f"{member_id}.process.exec"
    if not argv or PurePosixPath(argv[0]).name != "uvicorn":
        head = argv[0] if argv else ""
        raise _err(path, f"a pooled member must be a uvicorn command line, got {head!r}")
    if len(argv) < 2 or not _APP_TARGET_RE.match(argv[1]):
        got = argv[1] if len(argv) > 1 else ""
        raise _err(path, f"{got!r} is not a '<module>:<attr>' application target")

    factory = False
    i = 2
    while i < len(argv):
        tok = argv[i]
        if tok == "--factory":
            factory = True
            i += 1
        elif tok in ("--host", "--port"):
            if i + 1 >= len(argv):
                raise _err(path, f"{tok} has no value")
            if tok == "--host" and argv[i + 1] != "127.0.0.1":
                raise _err(
                    path,
                    f"--host {argv[i + 1]!r}: the pool runner binds every member to "
                    "127.0.0.1 and the gateway is the only front door",
                )
            i += 2
        else:
            raise _err(
                path,
                f"unsupported uvicorn option {tok!r}; the pool runner builds the "
                "server itself and would silently drop it",
            )
    return argv[1], factory


def pool_member(t: Translation, manifest_dir_rel: str) -> PoolMember:
    """Extract what the pool needs from one member's own ``Translation``.

    ``t`` must have been produced with ``TranslateContext.pool`` set, so its
    paths already point into the pool root.
    """
    if t.kind != "service" or t.decl is None:
        raise _err(t.id, "only kind: service can join a pool")
    decl = t.decl
    workdir = decl.start.workdir
    if workdir != "repo" and not workdir.startswith("repo/"):
        raise _err(f"{t.id}.process.working_dir", f"{workdir!r} is not under 'repo/'")
    runtime_rel = workdir[len("repo/") :] if workdir != "repo" else ""
    want = manifest_dir_rel.strip("/")
    if want != runtime_rel:
        raise _err(
            t.id,
            f"manifest directory {want!r} does not match the uv project directory "
            f"{runtime_rel!r} from deploy.install; the pool installs one editable "
            "package per member and cannot guess which is right",
        )
    app, factory = _parse_uvicorn_exec(decl.start.argv, t.id)

    identity: dict[str, str] = {}
    shared: dict[str, str] = {}
    for key, value in decl.env.items():
        # PORT is an identity key, but the runner takes it from ``port_env``;
        # a secret never reaches [env] (the schema forbids the overlap) and is
        # filtered anyway so the sidecar can never carry one.
        if key == "PORT" or key in decl.secrets:
            continue
        (identity if key in IDENTITY_ENV else shared)[key] = value

    return PoolMember(
        id=t.id,
        translation=t,
        runtime_rel=runtime_rel,
        app=app,
        factory=factory,
        identity_env=MappingProxyType(identity),
        shared_env=MappingProxyType(shared),
        secret_names=tuple(decl.secrets),
    )


def _check_pool_members(pool: str, members: Sequence[PoolMember]) -> None:
    """The cross-manifest rules of PLAN-pool §3.2, in the order they are cheapest."""
    if not members:
        raise TranslateError(f"pool {pool!r}: no members")
    if len(members) == 1:
        raise TranslateError(
            f"pool {pool!r}: only one member ({members[0].id!r}) "
            "— refusing to create a pool of one"
        )
    for m in members:
        if m.translation.kind != "service" or m.translation.decl is None:
            raise _err(m.id, "only kind: service can join a pool")
        if m.id == POOL_ADMIN_PORT_NAME:
            raise TranslateError(
                f"pool {pool!r}: member id {POOL_ADMIN_PORT_NAME!r} collides with "
                "the reserved admin port name"
            )

    by_suffix: dict[str, list[str]] = {}
    for m in members:
        by_suffix.setdefault(mangle_member(m.id), []).append(m.id)
    for suffix, ids in by_suffix.items():
        if len(ids) > 1:
            raise TranslateError(
                f"pool {pool!r}: member ids {sorted(ids)} mangle to the same env "
                f"suffix {suffix}"
            )


def _pool_env(pool: str, members: Sequence[PoolMember], ctx: TranslateContext) -> dict[str, str]:
    """The pool's one process env: the runner's own keys plus the shared union.

    Identity keys are exempt from the union because differing is their job --
    they are swapped per member per phase (§4.2). Everything else must agree,
    because after the swap there is exactly one value left for the request path
    to read (spike-pool finding 2).
    """
    union: dict[str, str] = {}
    owner: dict[str, str] = {}
    for m in members:
        for key, value in m.shared_env.items():
            if key in union and union[key] != value:
                raise TranslateError(
                    f"pool {pool!r}: members {owner[key]!r} and {m.id!r} both set "
                    f"{key} to different values ({union[key]!r} vs {value!r}); a pool "
                    "shares one process env for non-identity keys, so one of them "
                    "must change or leave the pool"
                )
            union[key] = value
            owner.setdefault(key, m.id)

    env = dict(union)
    env["GIT_COMMIT"] = ctx.sha
    env["POOL_ID"] = pool
    env["POOL_PORT_ADMIN"] = "${PORT_" + POOL_ADMIN_PORT_NAME + "}"
    for m in members:
        env[m.port_env] = "${PORT_" + m.port_name + "}"
    return env


def _pool_json(pool: str, members: Sequence[PoolMember], ctx: TranslateContext) -> dict[str, Any]:
    root = ctx.root_for(POOL_ID_PREFIX + pool)
    entries: list[dict[str, Any]] = []
    for m in members:
        decl = m.translation.decl
        assert decl is not None  # _check_pool_members
        suffix = mangle_member(m.id)
        # The harness injects AMS_DATA_DIR=<root>/data for the pool *process*,
        # which is the shared parent -- a member reading it would get its
        # neighbours' state too. It is an identity key (§4.2), so the runner
        # swaps in the per-member value from here. It cannot come through
        # ``decl.env``: the schema reserves the whole AMS_ prefix.
        member_env = {
            **m.identity_env,
            **m.shared_env,
            "AMS_DATA_DIR": f"{root}/data/{m.id}",
        }
        entries.append(
            {
                "id": m.id,
                "app": m.app,
                "factory": m.factory,
                "port_name": m.port_name,
                "port_env": m.port_env,
                "health_path": decl.health.path,
                # Names only. The values live in the harness-private secret
                # store under the *pool* id and reach the process as env.
                "secret_env": {name: f"{name}__{suffix}" for name in m.secret_names},
                "env": dict(sorted(member_env.items())),
            }
        )
    return {
        "version": POOL_JSON_VERSION,
        "pool": pool,
        "sha": ctx.sha,
        "members": entries,
    }


def build_pool(
    pool: str, members: Sequence[PoolMember], ctx: TranslateContext
) -> PoolTranslation:
    """N members -> the one ``ServiceDecl`` that runs them and its ``pool.json``.

    Raises ``TranslateError`` for every cross-manifest rule of PLAN-pool §3.2;
    each of them is a thing a per-manifest reader physically cannot see.
    """
    if not SERVICE_ID_RE.match(pool):
        raise _err("pool", f"{pool!r} does not match {SERVICE_ID_RE.pattern}")
    members = tuple(members)
    _check_pool_members(pool, members)

    pool_id = POOL_ID_PREFIX + pool
    root = ctx.root_for(pool_id)
    n = len(members)

    secrets: list[str] = []
    for m in members:
        suffix = mangle_member(m.id)
        secrets += [f"{name}__{suffix}" for name in m.secret_names]

    packages: list[str] = [ctx.pool_uvicorn_pin]
    for m in members:
        packages += ["-e", m.runtime_rel]

    memory = parse_size(ctx.pool_memory_base) + n * parse_size(ctx.pool_memory_per_member)
    decl = ServiceDecl(
        id=pool_id,
        name=f"pool {pool}",
        # `python` is bare on purpose: `runtime_env` prepends <root>/.venv/bin
        # to PATH, exactly as the per-service translation relies on for uvicorn.
        start=StartSpec(argv=("python", f"{root}/{ctx.pool_runner_name}"), workdir="repo"),
        env=_pool_env(pool, members, ctx),
        ports={POOL_ADMIN_PORT_NAME: 0, **{m.port_name: 0 for m in members}},
        # sync=false: there is no single pyproject to sync. The venv is built
        # from the pinned uvicorn plus one editable install per member.
        runtime=RuntimeSpec(kind="uv", python="3.12", sync=False, packages=tuple(packages)),
        health=HealthSpec(
            kind="http",
            port=POOL_ADMIN_PORT_NAME,
            path=POOL_HEALTH_PATH,
            start_period_s=ctx.pool_start_period_s,
        ),
        stop=StopSpec(signal="SIGTERM", timeout_s=10.0),
        limits=LimitsSpec(
            memory_max=_format_size(memory),
            cpu_max=ctx.pool_cpu_max,
            # The SDK runs up to three daemon threads per member and cgroup v2
            # counts a thread as a pid, so the per-service 64 would be wrong.
            pids_max=ctx.pool_pids_base + n * ctx.pool_pids_per_member,
        ),
        # A pool is N services in one process: "never restart" would take all N
        # down for one member's crash, so the pool always comes back.
        restart=RestartSpec(policy="always", backoff_s=10.0),
        # The runner owns the root handler and prefixes every line with a level
        # token, so the severity heuristic is turned off in favour of the fact.
        logging=LoggingSpec(format="level-prefix"),
        secrets=tuple(secrets),
        depends_on=DEPENDS_ON,
    )
    return PoolTranslation(
        id=pool_id,
        decl=decl,
        members=members,
        pool_json=MappingProxyType(_pool_json(pool, members, ctx)),
    )


# --------------------------------------------------------------------------- TOML


def _toml_str(s: str) -> str:
    out = ['"']
    for ch in s:
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return _toml_str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    raise TypeError(f"cannot emit {type(value).__name__} as TOML")


def emit_toml(decl: ServiceDecl) -> str:
    """Render a declaration as ``service.toml`` text.

    Deterministic: fixed table order, env and ports sorted by key. Round trips
    through ``ams.schema.loads`` -- the golden tests assert exactly that.
    """
    lines: list[str] = [f"id = {_toml_str(decl.id)}"]
    if decl.name:
        lines.append(f"name = {_toml_str(decl.name)}")
    if decl.secrets:
        lines.append(f"secrets = {_toml_value(list(decl.secrets))}")
    if decl.depends_on:
        lines.append(f"depends_on = {_toml_value(list(decl.depends_on))}")

    lines += ["", "[start]", f"argv = {_toml_value(list(decl.start.argv))}"]
    lines.append(f"workdir = {_toml_str(decl.start.workdir)}")

    if decl.env:
        lines += ["", "[env]"]
        lines += [f"{k} = {_toml_str(decl.env[k])}" for k in sorted(decl.env)]
    if decl.ports:
        lines += ["", "[ports]"]
        lines += [f"{k} = {decl.ports[k]}" for k in sorted(decl.ports)]

    rt = decl.runtime
    if rt.kind != "none":
        lines += ["", "[runtime]", f"kind = {_toml_str(rt.kind)}"]
        if rt.python:
            lines.append(f"python = {_toml_str(rt.python)}")
        if rt.node:
            lines.append(f"node = {_toml_str(rt.node)}")
        if rt.requirements:
            lines.append(f"requirements = {_toml_str(rt.requirements)}")
        if rt.sync:
            lines.append("sync = true")
        if rt.packages:
            lines.append(f"packages = {_toml_value(list(rt.packages))}")
        if rt.nix_packages:
            lines.append(f"nix_packages = {_toml_value(list(rt.nix_packages))}")

    h = decl.health
    if h.kind != "none":
        lines += ["", "[health]", f"kind = {_toml_str(h.kind)}"]
        if h.port:
            lines.append(f"port = {_toml_str(h.port)}")
        if h.kind == "http":
            lines.append(f"path = {_toml_str(h.path)}")
        if h.pattern:
            lines.append(f"pattern = {_toml_str(h.pattern)}")
        lines.append(f"start_period_s = {_toml_value(h.start_period_s)}")

    # Ahead of [stop], where examples/platform/caddy/service.toml puts it. Only
    # when it is not the default, so no existing golden grows a table.
    if decl.logging.format != "auto":
        lines += ["", "[logging]", f"format = {_toml_str(decl.logging.format)}"]

    lines += [
        "",
        "[stop]",
        f"signal = {_toml_str(decl.stop.signal)}",
        f"timeout_s = {_toml_value(decl.stop.timeout_s)}",
    ]

    lim = decl.limits
    if lim.memory_max or lim.cpu_max or lim.pids_max is not None:
        lines += ["", "[limits]"]
        if lim.memory_max:
            lines.append(f"memory_max = {_toml_str(lim.memory_max)}")
        if lim.cpu_max:
            lines.append(f"cpu_max = {_toml_str(lim.cpu_max)}")
        if lim.pids_max is not None:
            lines.append(f"pids_max = {lim.pids_max}")

    r = decl.restart
    lines += [
        "",
        "[restart]",
        f"policy = {_toml_str(r.policy)}",
        f"backoff_s = {_toml_value(r.backoff_s)}",
    ]
    return "\n".join(lines) + "\n"
