"""Caddy configuration generator for the ams gateway.

The platform's edge is a single pinned, static Caddy binary run as an ordinary
ams service on an ams-allocated high port (D18 / PLAN-allin Q3). This module
owns the *config*: it turns the `mount.json` sidecars written by
`ams.platform.translate` (shape: `docs/platform-sidecars.md`) plus the live port
allocation into the files under ``<state>/gateway/``:

    <state>/gateway/Caddyfile          global options + the entry site
    <state>/gateway/sites/<id>.caddy   one snippet per mount

Nothing here talks to Caddy. `render()` is pure, `write()` only touches the
filesystem, and applying a new config is `ams ctl restart caddy` — the admin API
was rejected in Q3 (an admin endpoint on shared loopback is reachable by every
local process, and a unix admin socket is created by the Caddy uid, which the
harness uid cannot connect to).

Ordering / the port chicken-and-egg
-----------------------------------
The Caddy declaration requests ``ports.main = 0``: ams picks the number. But the
number has to appear *inside* the Caddyfile as the entry site's listen address,
so the config cannot be rendered until allocation has happened. The sync loop
therefore always runs in this order:

    1. write/refresh the declarations (including caddy's, `caddy_declaration()`)
    2. let the harness allocate ports (``PortAllocator.allocate``)
    3. ``render(mounts, resolve_ports(...), GatewayConfig(listen_port=<caddy's
       allocated main port>, ...))`` and ``write(...)``
    4. restart caddy only if ``write()`` reported changed paths

Permissions
-----------
Caddy runs as a mapped uid (inner 1000, D4); the harness uid is not mapped
inside the namespace, so harness-owned files show up as ``nobody``-owned. They
are still *readable* if the mode says so, which is why `write()` forces 0755 on
the directories and 0644 on the files. The 0600 secret store stays unreadable —
that asymmetry is the point of D4. The same rule is why file access logs are
off by default: a log file has to be *written*, and no harness-owned directory
is writable by the Caddy uid. See `GatewayConfig.log_dir`.

Core mode
---------
`render_core()` is the 1.1.0 renderer for the Cordis-based ``core`` (PLAN-core
§4.2). There the core process's own `gateway` plugin is the single HTTP entry:
it routes, authenticates and sets every response header per plugin route. Caddy
shrinks to a host -> loopback port map read from ``[[site]]`` tables, one
``sites/<host>.caddy`` per host, in the same file shape as `render()` so
`write()` is shared. Plain HTTP only: TLS terminates at Cloudflare / the tunnel.

Phase A vs Phase B
------------------
Phase A is a plain-HTTP replica on loopback: ``plain_http=True`` emits
``auto_https off`` and ``http://<host>:<listen_port>`` site addresses. The
Phase-B TLS flip is that one flag: ``plain_http=False`` emits bare hostnames as
site addresses and lets Caddy's automatic HTTPS bind 80/443 and manage certs.
No other part of the rendering changes.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ams.state import StateDir

log = logging.getLogger("ams.platform.gateway")

MOUNT_VERSION = 1
GATEWAY_DIRNAME = "gateway"
SITES_DIRNAME = "sites"
CADDYFILE_NAME = "Caddyfile"

#: Path served by the entry site for the harness's own health probe. Not a
#: service route: it answers before any import, so a gateway with zero mounts is
#: still observably up.
HEALTH_PATH = "/ams-health"

DIR_MODE = 0o755
FILE_MODE = 0o644


class GatewayError(RuntimeError):
    """A mount is unusable: unknown version/kind, no port for a service mount,
    both or neither of path/subdomain, or a value that would not survive being
    written into a Caddyfile."""


# --------------------------------------------------------------------------- #
# Ported data.
#
# Everything in this block is a VERBATIM port of the platform deployer's Jinja
# templates, which are the reviewed production configuration:
#
#   api/components/deployer/templates/caddy.snippet.j2   (kind=service)
#   api/components/deployer/templates/caddy.static.j2    (kind=static)
#   api/bootstrap/04-caddy-init.sh                       (the entry site)
#
# PLAN-allin risk 2 is "CSP/security headers lost porting Jinja to Python", so
# these are data, not code, and every line is pinned by a golden-file test.
# Editing anything here without editing tests/golden/gateway/ is a bug.
# --------------------------------------------------------------------------- #

# caddy.snippet.j2: "Security headers (audit 2026-07-11). Nothing on the
# platform is legitimately framed, so every service — HTML UI or JSON API —
# denies framing. [...] Path-mounted services get frame/nosniff only: full CSP
# would break HTML surfaces that load CDN assets behind the gateway (e.g.
# resume's Swagger /docs) and is meaningless for pure-JSON responses."
PATH_CSP = "frame-ancestors 'none'"

# caddy.snippet.j2 `default_csp`. Subdomain services are the login-/token-gated
# UIs and their pages are self-contained (sdk.ui page shell, inline
# style/script, same-origin fetch). A NEW subdomain service that pulls from a
# CDN must add its origins to CSP_MAP or its page will break — that is
# deliberate: third-party script on a gated UI is an explicit, reviewed
# decision, not a default.
DEFAULT_CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
    "form-action 'self'"
)

# caddy.snippet.j2 `csp_map`. `pages` hosts UNTRUSTED user HTML: `sandbox`
# without allow-same-origin gives every document an opaque origin (no cookies,
# no same-origin requests, no credentialed CORS against the platform). Keep in
# sync with PAGE_CSP in apps/pages/src/pages/main.py — the app sets the same
# header for dev parity; this line is the fail-safe that holds even if the
# app's header regresses.
CSP_MAP: Mapping[str, str] = {
    "locationservice": (
        "default-src 'self'; script-src 'self' 'unsafe-inline' https://unpkg.com; "
        "style-src 'self' 'unsafe-inline' https://unpkg.com; "
        "img-src 'self' data: https://unpkg.com https://tile.openstreetmap.org; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
        "form-action 'self'"
    ),
    "pages": (
        "sandbox allow-scripts allow-forms allow-popups allow-modals allow-downloads; "
        "frame-ancestors 'none'; base-uri 'none'"
    ),
}

# caddy.snippet.j2 `admin_cors_map`: subdomain services whose /api/* surface the
# admin console (admin.lishuyu.app) manages cross-origin. Single CORS layer =
# Caddy (never FastAPI). Scope is deliberately narrow: exact-match admin origin
# only, and only under /api/* — for `pages` this cannot leak to hosted
# documents, whose sandbox CSP gives them an opaque origin (`Origin: null`
# never matches the exact-match).
ADMIN_CORS_MAP: Mapping[str, str] = {
    "pages": "https://admin.lishuyu.app",
}

# caddy.static.j2: "Security headers (audit 2026-07-11): frame-denial + nosniff
# only — no full CSP for static SPAs. files-web fetches presigned R2
# upload/download URLs whose origin is runtime configuration (secretsservice),
# so a connect-src allow-list written at render time would break uploads.
# Framing denial has no such dependency."
STATIC_CSP = "frame-ancestors 'none'"

REFERRER_POLICY = "strict-origin-when-cross-origin"

# 04-caddy-init.sh: CORS for first-party SPAs that talk to the entry gateway
# cross-origin. Ported verbatim; in Phase A (plain HTTP on loopback) no request
# carries one of these https origins, so the block is inert until the Phase-B
# TLS flip — but dropping it would silently lose production behaviour.
ENTRY_CORS_ORIGIN_RE = (
    r"^https://(readme\.lishuyu\.app|files?\.lishuyu\.app|blog\.lishuyu\.app|"
    r"admin\.lishuyu\.app|llm\.lishuyu\.app|shuyuli\.com)$"
)
ENTRY_CORS_METHODS = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
ENTRY_CORS_HEADERS = "Authorization, Content-Type, X-Claim-Token, X-File-Password"
ENTRY_CORS_MAX_AGE = "86400"

ENTRY_ROOT_BODY = "lishuyu platform — see /<service>"
ENTRY_404_BODY = '{"error":"not_found","detail":"no such service"}'

# 04-caddy-init.sh, hand-managed outside the deployer's one-mount model: the
# admin SPA's `.lishuyu.app`-scoped phm_jwt cookie never rides cross-site to
# pages.shuyuli.com, so the pages management API is ALSO mounted on the entry
# host under /pages/api/*. Only /pages/api/* is proxied — the untrusted hosted
# HTML stays on its own subdomain (sandbox CSP, opaque origin) and must never be
# served from the entry host. id -> prefix stripped before proxying.
ENTRY_EXTRA_API_MOUNTS: Mapping[str, str] = {
    "pages": "/pages",
}

# caddy.snippet.j2 / caddy.static.j2 access-log rotation, used only when
# `GatewayConfig.log_dir` is set.
LOG_ROLL_SIZE = "50MiB"
LOG_ROLL_KEEP = 10
LOG_ROLL_KEEP_FOR = "90d"

# caddy.static.j2: "index.html fallback ONLY for extensionless paths (SPA routes
# like /f/<id>). Missing paths WITH an extension (/xxx.php scanner probes) fall
# through to file_server and 404 instead of 200 + index page."
SPA_FALLBACK_COMMENT = (
    "index.html fallback ONLY for extensionless paths (SPA routes like\n"
    "/f/<id>). Missing paths WITH an extension (/xxx.php scanner probes)\n"
    "fall through to file_server and 404 instead of 200 + index page."
)


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class GatewayConfig:
    """Everything the renderer needs that is not in a mount.

    ``listen_port`` is the port ams allocated to the *caddy service itself*, not
    to any backend: it is the address the entry site binds and the port the
    harness health-probes. Read it from the allocator after allocation, never
    from the declaration (which asks for ``0``).

    ``log_dir`` is ``None`` by default and that is the Phase-A choice, not an
    omission. Caddy runs as a mapped uid, so it cannot write into a
    harness-owned directory (D4); with no ``output`` the JSON access log goes to
    stderr, which the harness already reads line by line and routes through the
    decision interface. Set it only to a directory the Caddy uid owns (e.g.
    ``<state>/services/caddy/root/logs``) when you want production's rolled
    files; the roll block is then rendered verbatim from the deployer.
    """

    listen_port: int
    static_root: Path
    entry_host: str = "127.0.0.1"
    log_dir: Path | None = None
    plain_http: bool = True
    #: Extra global options, emitted verbatim (one directive per element).
    global_options: tuple[str, ...] = ()

    def site_address(self, host: str) -> str:
        """The Caddyfile site label for ``host``.

        Phase A: ``http://<host>:<listen_port>`` — an explicit scheme keeps
        automatic HTTPS off for this site even if the global option changes, and
        the explicit port is required because the gateway is not on 80.
        Phase B: the bare hostname, which is what turns automatic HTTPS on.
        """
        if self.plain_http:
            return f"http://{host}:{self.listen_port}"
        return host


class _PortSource(Protocol):
    def get(self, service_id: str) -> Mapping[str, int]: ...


def resolve_ports(mounts: Iterable[Mapping[str, Any]], allocator: _PortSource) -> dict[str, int]:
    """Resolve each service mount's ``port_name`` against the live allocation.

    `mount.json` deliberately carries the port *name*, not a number: ams
    allocates the number at runtime and the manifest's production port is
    meaningless here. This is the one place that indirection is followed, so
    `render()` can take a flat ``{service_id: port}`` mapping.

    A pooled member's mount carries two OPTIONAL keys: ``port_owner`` (the
    allocator row that actually holds the port — a pool process id) and
    ``port_name`` on that row. Both absent/null mean today's behaviour: look
    the member's own id up under ``"main"`` (PLAN-pool §3.3, §5.4).
    """
    out: dict[str, int] = {}
    for mount in mounts:
        if _kind(mount) != "service":
            continue
        service_id = _require_str(mount, "id")
        port_name = mount.get("port_name") or "main"
        owner = mount.get("port_owner") or service_id
        allocated = allocator.get(owner)
        if port_name not in allocated:
            raise GatewayError(
                f"{service_id}: no allocated port named {port_name!r} on {owner!r} "
                f"(have: {sorted(allocated)})"
            )
        out[service_id] = allocated[port_name]
    return out


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


@dataclass
class _Block:
    """A Caddyfile fragment built line by line with tab indentation."""

    lines: list[str] = field(default_factory=list)

    def add(self, text: str = "", depth: int = 0) -> None:
        self.lines.append(("\t" * depth + text) if text else "")

    def comment(self, text: str, depth: int = 0) -> None:
        for line in text.split("\n"):
            self.add(f"# {line}" if line else "#", depth)

    def render(self) -> str:
        return "\n".join(self.lines).rstrip("\n") + "\n"


def _kind(mount: Mapping[str, Any]) -> str:
    kind = mount.get("kind")
    if kind not in ("service", "static"):
        raise GatewayError(f"mount {mount.get('id')!r}: unknown kind {kind!r}")
    return kind


def _require_str(mount: Mapping[str, Any], key: str) -> str:
    value = mount.get(key)
    if not isinstance(value, str) or not value:
        raise GatewayError(f"mount {mount.get('id')!r}: {key} must be a non-empty string")
    return value


def _check_version(mount: Mapping[str, Any]) -> None:
    version = mount.get("version")
    if version != MOUNT_VERSION:
        raise GatewayError(
            f"mount {mount.get('id')!r}: unsupported sidecar version {version!r} "
            f"(this renderer knows {MOUNT_VERSION})"
        )


#: A site label and a path are written into the Caddyfile *unquoted*, so unlike
#: a header value they cannot merely be quote-checked: anything outside these
#: charsets could change the config's structure rather than one string.
_HOST_OK = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-*")
_PATH_OK = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-_/~")


def _check_token(value: str, allowed: set[str], what: str, service_id: str) -> str:
    bad = sorted(set(value) - allowed)
    if bad or not value:
        raise GatewayError(f"mount {service_id!r}: {what} {value!r} has illegal characters {bad}")
    return value


def _quote(value: str) -> str:
    """Quote a Caddyfile string argument, rejecting what cannot be quoted.

    Caddy has no escape for a literal ``"`` inside a quoted token, and a raw
    newline would end the directive. Both would silently produce a *different*
    config rather than an error, so reject instead of guessing.
    """
    if '"' in value or "\n" in value or "\r" in value:
        raise GatewayError(f"value cannot be written to a Caddyfile: {value!r}")
    return f'"{value}"'


def _header_lines(base: Sequence[tuple[str, str]], overrides: Mapping[str, Any]) -> list[str]:
    """Emit ``header <Name> "<value>"`` lines, applying per-mount overrides.

    An override whose name matches a template header (case-insensitively)
    replaces its value in place; any other override is appended in sorted order.
    An override value of ``None`` drops the header entirely — the only way an
    operator can turn one off without forking the renderer.
    """
    if not isinstance(overrides, Mapping):
        raise GatewayError(f"headers must be an object, got {type(overrides).__name__}")
    lowered = {name.lower(): name for name in overrides}
    out: list[str] = []
    used: set[str] = set()
    for name, value in base:
        key = name.lower()
        if key in lowered:
            used.add(key)
            override = overrides[lowered[key]]
            if override is None:
                continue
            value = str(override)
        out.append(f"header {name} {_quote(value)}")
    for key in sorted(set(lowered) - used):
        name = lowered[key]
        value = overrides[name]
        if value is None:
            continue
        out.append(f"header {name} {_quote(str(value))}")
    return out


def _log_block(block: _Block, cfg: GatewayConfig, name: str, depth: int) -> None:
    """The `log` directive. With no ``log_dir`` Caddy's default output (stderr)
    is kept, which is what the harness reads; see `GatewayConfig.log_dir`."""
    block.add("log {", depth)
    if cfg.log_dir is not None:
        block.add(f"output file {cfg.log_dir}/{name}-access.log {{", depth + 1)
        block.add(f"roll_size {LOG_ROLL_SIZE}", depth + 2)
        block.add(f"roll_keep {LOG_ROLL_KEEP}", depth + 2)
        block.add(f"roll_keep_for {LOG_ROLL_KEEP_FOR}", depth + 2)
        block.add("}", depth + 1)
    block.add("format json", depth + 1)
    block.add("}", depth)


def _admin_cors_block(block: _Block, origin: str, depth: int) -> None:
    """caddy.snippet.j2's admin CORS block, ported verbatim."""
    block.add("@admin_cors_preflight {", depth)
    block.add("method OPTIONS", depth + 1)
    block.add("path /api/*", depth + 1)
    block.add(f"header Origin {_quote(origin)}", depth + 1)
    block.add("}", depth)
    block.add("handle @admin_cors_preflight {", depth)
    block.add(f"header Access-Control-Allow-Origin {_quote(origin)}", depth + 1)
    block.add(f"header Access-Control-Allow-Methods {_quote(ENTRY_CORS_METHODS)}", depth + 1)
    block.add('header Access-Control-Allow-Headers "Authorization, Content-Type"', depth + 1)
    block.add('header Access-Control-Allow-Credentials "true"', depth + 1)
    block.add(f"header Access-Control-Max-Age {_quote(ENTRY_CORS_MAX_AGE)}", depth + 1)
    block.add('header Vary "Origin"', depth + 1)
    block.add("respond 204", depth + 1)
    block.add("}", depth)
    block.add("@admin_cors {", depth)
    block.add("path /api/*", depth + 1)
    block.add(f"header Origin {_quote(origin)}", depth + 1)
    block.add("}", depth)
    block.add(f"header @admin_cors Access-Control-Allow-Origin {_quote(origin)}", depth)
    block.add('header @admin_cors Access-Control-Allow-Credentials "true"', depth)
    block.add('header @admin_cors Vary "Origin"', depth)


def _spa_fallback(block: _Block, depth: int) -> None:
    """caddy.static.j2's SPA fallback, ported verbatim."""
    block.comment(SPA_FALLBACK_COMMENT, depth)
    block.add("@spa_fallback {", depth)
    block.add("not file", depth + 1)
    block.add("not path *.*", depth + 1)
    block.add("}", depth)
    block.add("rewrite @spa_fallback /index.html", depth)


def _static_body(block: _Block, mount: Mapping[str, Any], cfg: GatewayConfig, depth: int) -> None:
    service_id = _require_str(mount, "id")
    static_root = mount.get("static_root") or service_id
    if "/" in static_root or static_root in ("", ".", ".."):
        raise GatewayError(f"mount {service_id!r}: static_root must be a single path segment")
    block.add(f"root * {cfg.static_root / static_root}", depth)
    _spa_fallback(block, depth)
    block.add("file_server", depth)


def _render_site(
    mount: Mapping[str, Any], ports: Mapping[str, int], cfg: GatewayConfig
) -> tuple[str, bool]:
    """Render one mount's snippet. Returns ``(text, is_subdomain)``.

    A subdomain snippet is a whole site block and is imported at root scope; a
    path snippet is a bare ``handle_path`` and is imported *inside* the entry
    site. That split is the deployer's ``services/`` vs ``services-api/`` split,
    kept as one directory here because the entry Caddyfile emits an explicit
    ``import`` per file rather than a glob.
    """
    _check_version(mount)
    kind = _kind(mount)
    service_id = _require_str(mount, "id")
    gateway_host = _require_str(mount, "gateway")
    path = mount.get("path")
    subdomain = mount.get("subdomain")
    overrides = mount.get("headers") or {}

    if bool(path) == bool(subdomain):
        raise GatewayError(
            f"mount {service_id!r}: exactly one of path/subdomain must be set "
            f"(path={path!r}, subdomain={subdomain!r})"
        )
    _check_token(gateway_host, _HOST_OK, "gateway", service_id)
    if path is not None:
        if not str(path).startswith("/") or str(path).endswith("/"):
            raise GatewayError(
                f"mount {service_id!r}: path must start with '/' and not end with one, got {path!r}"
            )
        _check_token(str(path), _PATH_OK, "path", service_id)

    block = _Block()

    if subdomain:
        # The site label is `gateway`, the full hostname the site answers on;
        # `subdomain` is only the discriminator (docs/platform-sidecars.md:
        # "the renderer emits a whole site block for `gateway`").
        block.comment(f"{service_id} — {kind} on subdomain {subdomain} ({gateway_host})")
        block.add(f"{cfg.site_address(gateway_host)} {{")
        _log_block(block, cfg, service_id, 1)
        base = [
            ("X-Frame-Options", "DENY"),
            (
                "Content-Security-Policy",
                STATIC_CSP if kind == "static" else CSP_MAP.get(service_id, DEFAULT_CSP),
            ),
            ("X-Content-Type-Options", "nosniff"),
            ("Referrer-Policy", REFERRER_POLICY),
        ]
        for line in _header_lines(base, overrides):
            block.add(line, 1)
        if kind == "service" and service_id in ADMIN_CORS_MAP:
            _admin_cors_block(block, ADMIN_CORS_MAP[service_id], 1)
        if kind == "static":
            _static_body(block, mount, cfg, 1)
        else:
            block.add(f"reverse_proxy 127.0.0.1:{_port_for(service_id, ports)}", 1)
        block.add("}")
        return block.render(), True

    block.comment(f"{service_id} — {kind} on {gateway_host}{path}")
    block.add(f"handle_path {path}/* {{")
    base = [
        ("X-Frame-Options", "DENY"),
        ("Content-Security-Policy", STATIC_CSP if kind == "static" else PATH_CSP),
        ("X-Content-Type-Options", "nosniff"),
    ]
    for line in _header_lines(base, overrides):
        block.add(line, 1)
    if kind == "static":
        _static_body(block, mount, cfg, 1)
    else:
        block.add(f"reverse_proxy 127.0.0.1:{_port_for(service_id, ports)}", 1)
    block.add("}")
    return block.render(), False


def _port_for(service_id: str, ports: Mapping[str, int]) -> int:
    port = ports.get(service_id)
    if not isinstance(port, int) or port <= 0:
        raise GatewayError(
            f"mount {service_id!r}: no live port allocated (got {port!r}); "
            "render() must run after port allocation"
        )
    return port


def _render_caddyfile(
    path_ids: Sequence[str],
    subdomain_ids: Sequence[str],
    ports: Mapping[str, int],
    cfg: GatewayConfig,
) -> str:
    block = _Block()
    block.comment(
        "Generated by ams.platform.gateway — do not edit by hand.\n"
        "Rendered from <state>/platform/mounts/*.json plus the live port\n"
        "allocation; `ams ctl restart caddy` applies it."
    )
    # No blank line before the global options block: `caddy fmt` removes it, and
    # output that is already canonical means a future `caddy fmt --overwrite` on
    # the live config cannot make it differ from what render() produces.
    block.add("{")
    if cfg.plain_http:
        # Phase A: no ACME, no 80/443, no cert storage. The Phase-B flip is
        # GatewayConfig(plain_http=False) and nothing else.
        block.add("auto_https off", 1)
    # The admin API is off by design: on shared loopback it is reachable by
    # every local process on the box (PLAN-allin Q3). Config reloads happen via
    # `ams ctl restart caddy`, so nothing needs it.
    block.add("admin off", 1)
    block.add("persist_config off", 1)
    for option in cfg.global_options:
        block.add(option, 1)
    block.add("}")
    block.add()

    block.comment("Entry site: path-mounted services, plus the harness health probe.")
    block.add(f"{cfg.site_address(cfg.entry_host)} {{")
    _log_block(block, cfg, "api", 1)
    block.add()
    block.add("encode gzip zstd", 1)
    block.add()

    block.comment(
        "Harness health probe (ams.health.check_http). Answers before any\n"
        "import, so a gateway with zero mounts is still observably up.",
        1,
    )
    block.add(f"handle {HEALTH_PATH} {{", 1)
    block.add('respond "ok" 200', 2)
    block.add("}", 1)
    block.add()

    block.comment(
        "CORS for first-party SPAs that talk to the entry host cross-origin.\n"
        "Add new SPA origins to ENTRY_CORS_ORIGIN_RE.",
        1,
    )
    block.add("@cors_preflight {", 1)
    block.add("method OPTIONS", 2)
    block.add(f"header_regexp Origin {_quote(ENTRY_CORS_ORIGIN_RE)}", 2)
    block.add("}", 1)
    block.add("handle @cors_preflight {", 1)
    block.add('header Access-Control-Allow-Origin "{http.request.header.origin}"', 2)
    block.add(f"header Access-Control-Allow-Methods {_quote(ENTRY_CORS_METHODS)}", 2)
    block.add(f"header Access-Control-Allow-Headers {_quote(ENTRY_CORS_HEADERS)}", 2)
    block.add('header Access-Control-Allow-Credentials "true"', 2)
    block.add(f"header Access-Control-Max-Age {_quote(ENTRY_CORS_MAX_AGE)}", 2)
    block.add('header Vary "Origin"', 2)
    block.add("respond 204", 2)
    block.add("}", 1)
    block.add()
    block.add(f"@cors_origin header_regexp Origin {_quote(ENTRY_CORS_ORIGIN_RE)}", 1)
    block.add('header @cors_origin Access-Control-Allow-Origin "{http.request.header.origin}"', 1)
    block.add('header @cors_origin Access-Control-Allow-Credentials "true"', 1)
    block.add('header @cors_origin Vary "Origin"', 1)
    block.add()

    block.add("handle / {", 1)
    block.add(f"respond {_quote(ENTRY_ROOT_BODY)} 200", 2)
    block.add("}", 1)

    for service_id, prefix in sorted(ENTRY_EXTRA_API_MOUNTS.items()):
        if service_id not in ports:
            continue
        block.add()
        block.comment(
            f"{service_id} management API on the entry host: the admin SPA's\n"
            "`.lishuyu.app`-scoped cookie never rides cross-site to the service's\n"
            f"own subdomain. ONLY {prefix}/api/* is proxied here.",
            1,
        )
        block.add(f"handle {prefix}/api/* {{", 1)
        block.add(f"uri strip_prefix {prefix}", 2)
        block.add(f"reverse_proxy 127.0.0.1:{ports[service_id]}", 2)
        block.add("}", 1)

    if path_ids:
        block.add()
        block.comment("Path-mounted services (one import per mount, never a glob:", 1)
        block.comment("a stale file can then never be picked up silently).", 1)
        for service_id in path_ids:
            block.add(f"import {SITES_DIRNAME}/{service_id}.caddy", 1)

    block.add()
    block.add("handle {", 1)
    block.add("header Content-Type application/json", 2)
    block.add(f"respond `{ENTRY_404_BODY}` 404", 2)
    block.add("}", 1)
    block.add("}")

    if subdomain_ids:
        block.add()
        block.comment("Subdomain sites (whole site blocks, imported at root scope).")
        for service_id in subdomain_ids:
            block.add(f"import {SITES_DIRNAME}/{service_id}.caddy")

    return block.render()


def render(
    mounts: Iterable[Mapping[str, Any]],
    ports: Mapping[str, int],
    cfg: GatewayConfig,
) -> dict[str, str]:
    """Render the whole gateway config.

    ``mounts`` are `mount.json` documents (`docs/platform-sidecars.md`);
    ``ports`` maps a service id to its live allocated port (see
    `resolve_ports`); static mounts need no entry. Returns
    ``{relative_path: content}`` covering ``Caddyfile`` and one
    ``sites/<id>.caddy`` per mount — pure, so a caller can diff it against disk
    before deciding to restart anything.
    """
    files: dict[str, str] = {}
    path_ids: list[str] = []
    subdomain_ids: list[str] = []
    seen: set[str] = set()

    for mount in sorted(mounts, key=lambda m: str(m.get("id"))):
        service_id = _require_str(mount, "id")
        if service_id in seen:
            raise GatewayError(f"duplicate mount id {service_id!r}")
        seen.add(service_id)
        text, is_subdomain = _render_site(mount, ports, cfg)
        files[f"{SITES_DIRNAME}/{service_id}.caddy"] = text
        (subdomain_ids if is_subdomain else path_ids).append(service_id)

    files[CADDYFILE_NAME] = _render_caddyfile(path_ids, subdomain_ids, ports, cfg)
    log.debug(
        "rendered gateway config: %d path mount(s), %d subdomain site(s)",
        len(path_ids),
        len(subdomain_ids),
    )
    return files


# --------------------------------------------------------------------------- #
# Core mode (PLAN-core §4.2)
# --------------------------------------------------------------------------- #

#: The entry site's catch-all in core mode: a request for a host Caddy has no
#: ``[[site]]`` for. Core-mode wording, same JSON shape as ``ENTRY_404_BODY``.
CORE_ENTRY_404_BODY = '{"error":"not_found","detail":"no such site"}'

#: RFC 1123 labels, lowercase only. Stricter than ``_HOST_OK`` on purpose: a
#: core site's host is also its snippet's file name and its ``import`` argument,
#: where a ``*`` would turn the import into a glob, and Caddy matches hosts
#: case-insensitively, so ``API.x`` and ``api.x`` would be one site twice.
_CORE_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_CORE_HOST_RE = re.compile(rf"{_CORE_LABEL}(?:\.{_CORE_LABEL})*")
_CORE_HOST_MAX = 253
CORE_PORT_MIN = 1024
CORE_PORT_MAX = 65535


def _check_core_host(host: object) -> str:
    if not isinstance(host, str):
        raise GatewayError(f"core site host must be a string, got {type(host).__name__}")
    # The shared charset check first (same guard as every mount's `gateway`),
    # then the strict shape.
    _check_token(host, _HOST_OK, "host", f"site {host}")
    if len(host) > _CORE_HOST_MAX or not _CORE_HOST_RE.fullmatch(host):
        raise GatewayError(
            f"core site host {host!r} is not a strict lowercase hostname "
            "(RFC 1123 labels, no wildcard, no trailing dot, <= 253 chars)"
        )
    return host


@dataclass(frozen=True)
class CoreSite:
    """One ``[[site]]`` of ``<state>/platform/core.toml``: a host Caddy answers
    on and the loopback port it proxies to (core's gateway, or another fixed
    listener such as pages). Validated on construction, so a bad table fails at
    config load rather than as a Caddy that will not start."""

    host: str
    port: int

    def __post_init__(self) -> None:
        _check_core_host(self.host)
        port = self.port
        if (
            not isinstance(port, int)
            or isinstance(port, bool)
            or not CORE_PORT_MIN <= port <= CORE_PORT_MAX
        ):
            raise GatewayError(
                f"core site {self.host!r}: port must be an int in "
                f"{CORE_PORT_MIN}..{CORE_PORT_MAX}, got {port!r}"
            )

    @property
    def snippet(self) -> str:
        """Relative path of this site's file under ``<state>/gateway/``."""
        return f"{SITES_DIRNAME}/{self.host}.caddy"


def _render_core_site(site: CoreSite, cfg: GatewayConfig) -> str:
    block = _Block()
    block.comment(f"{site.host} — core on 127.0.0.1:{site.port}")
    block.add(f"{cfg.site_address(site.host)} {{")
    _log_block(block, cfg, site.host, 1)
    block.add(f"reverse_proxy 127.0.0.1:{site.port}", 1)
    block.add("}")
    return block.render()


def _render_core_caddyfile(sites: Sequence[CoreSite], cfg: GatewayConfig) -> str:
    block = _Block()
    block.comment(
        "Generated by ams.platform.gateway (core mode) — do not edit by hand.\n"
        "Rendered from the [[site]] tables of <state>/platform/core.toml. Core's\n"
        "gateway plugin routes, authenticates and sets headers; Caddy only maps\n"
        "host -> loopback port. `ams ctl restart caddy` applies it."
    )
    # Same global block as render(): canonical (no blank line before it), TLS
    # off because it terminates at Cloudflare, admin API off (PLAN-allin Q3).
    block.add("{")
    block.add("auto_https off", 1)
    block.add("admin off", 1)
    block.add("persist_config off", 1)
    for option in cfg.global_options:
        block.add(option, 1)
    block.add("}")
    block.add()

    block.comment("Entry site: the harness health probe only.")
    block.add(f"{cfg.site_address(cfg.entry_host)} {{")
    _log_block(block, cfg, "entry", 1)
    block.add()
    block.comment(
        "Harness health probe (ams.health.check_http). Answers before any\n"
        "import, so a gateway with zero sites is still observably up.",
        1,
    )
    block.add(f"handle {HEALTH_PATH} {{", 1)
    block.add('respond "ok" 200', 2)
    block.add("}", 1)
    block.add()
    block.add("handle {", 1)
    block.add("header Content-Type application/json", 2)
    block.add(f"respond `{CORE_ENTRY_404_BODY}` 404", 2)
    block.add("}", 1)
    block.add("}")

    if sites:
        block.add()
        block.comment("Host -> port sites (one import per site, never a glob:")
        block.comment("a stale file can then never be picked up silently).")
        for site in sites:
            block.add(f"import {site.snippet}")
    return block.render()


def render_core(sites: Sequence[CoreSite], cfg: GatewayConfig) -> dict[str, str]:
    """Render the core-mode gateway config: ``{relative_path: content}``.

    Same shape as `render()` -- ``Caddyfile`` plus one ``sites/<host>.caddy``
    per site -- so `write()` applies it unchanged, including removing the
    legacy mode's per-mount snippets on the first core-mode write. Pure and
    independent of input order (sites are emitted sorted by host).

    Refused rather than guessed: TLS (``cfg.plain_http=False``), a duplicate
    host, a site on the entry host (it would collide with the health-probe
    site), and a site proxying to Caddy's own listen port (a request loop).
    """
    if not cfg.plain_http:
        raise GatewayError(
            "core mode is plain HTTP only (TLS terminates at Cloudflare / the tunnel); "
            "got GatewayConfig(plain_http=False)"
        )
    seen: set[str] = set()
    for site in sites:
        if not isinstance(site, CoreSite):
            raise GatewayError(f"render_core takes CoreSite values, got {type(site).__name__}")
        if site.host in seen:
            raise GatewayError(f"duplicate core site host {site.host!r}")
        if site.host == cfg.entry_host:
            raise GatewayError(
                f"core site {site.host!r} collides with the entry host (the health-probe site)"
            )
        if site.port == cfg.listen_port:
            raise GatewayError(
                f"core site {site.host!r} proxies to Caddy's own listen port "
                f"{cfg.listen_port}: a request loop"
            )
        seen.add(site.host)

    ordered = sorted(sites, key=lambda s: s.host)
    files = {site.snippet: _render_core_site(site, cfg) for site in ordered}
    files[CADDYFILE_NAME] = _render_core_caddyfile(ordered, cfg)
    log.debug(
        "rendered core gateway config: %d site(s): %s",
        len(ordered),
        ", ".join(f"{s.host}->{s.port}" for s in ordered) or "(none)",
    )
    return files


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #


def gateway_dir(state: StateDir) -> Path:
    return state.root / GATEWAY_DIRNAME


def caddyfile_path(state: StateDir) -> Path:
    return gateway_dir(state) / CADDYFILE_NAME


def static_root(state: StateDir) -> Path:
    """Document-root parent for ``kind: static`` mounts (`docs/platform-sidecars.md`)."""
    return state.root / "platform" / "static"


def _write_atomic(path: Path, content: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp{os.getpid()}")
    tmp.write_text(content, encoding="utf-8")
    os.chmod(tmp, FILE_MODE)  # write_text honours the umask; force the mode.
    os.replace(tmp, path)


def write(state: StateDir, files: Mapping[str, str]) -> list[Path]:
    """Write ``files`` under ``<state>/gateway/``, returning what actually changed.

    Unchanged content is not rewritten, so an empty return value means "nothing
    to apply" and the caller can skip the Caddy restart. Stale
    ``sites/*.caddy`` files that are not in ``files`` are removed and counted as
    changes: a removed mount must reach Caddy too.

    Directories are 0755 and files 0644 because Caddy reads them as a mapped
    uid, for which every harness-owned file is ``nobody``-owned (D4).
    """
    root = gateway_dir(state)
    sites = root / SITES_DIRNAME
    for directory in (root, sites):
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, DIR_MODE)

    changed: list[Path] = []
    for rel in sorted(files):
        path = root / rel
        content = files[rel]
        try:
            current: str | None = path.read_text(encoding="utf-8")
            mode: int | None = path.stat().st_mode & 0o777
        except (OSError, UnicodeDecodeError):
            current, mode = None, None
        if current == content and mode == FILE_MODE:
            continue
        _write_atomic(path, content)
        changed.append(path)

    keep = {root / rel for rel in files}
    for existing in sorted(sites.glob("*.caddy")):
        if existing not in keep:
            existing.unlink()
            changed.append(existing)
            log.info("removed stale gateway snippet %s", existing.name)

    if changed:
        log.info("gateway config changed: %s", ", ".join(p.name for p in changed))
    return changed


# --------------------------------------------------------------------------- #
# The Caddy service declaration
# --------------------------------------------------------------------------- #

CADDY_SERVICE_ID = "caddy"


def caddy_declaration(state: StateDir, store: Path, port_name: str = "main") -> str:
    """The `service.toml` text for the gateway itself.

    Caddy is an ordinary ams service: a pinned static binary in
    ``<store>/bin/caddy`` (installed by ``deploy/install-host.sh``), no root, no
    system unit, no 80/443. It asks for ``ports.<port_name> = 0`` and ams picks
    the number — which is exactly why the Caddyfile can only be rendered *after*
    allocation (see the module docstring).

    The declaration is static: it never mentions a port number or a mount, so it
    is written once and only the config underneath it changes.
    """
    argv = [
        str(store / "bin" / "caddy"),
        "run",
        "--config",
        str(caddyfile_path(state)),
        # Required: the file is named "Caddyfile" but an explicit adapter means
        # a rename can never silently switch Caddy into JSON-config mode.
        "--adapter",
        "caddyfile",
    ]
    argv_toml = ",\n  ".join(f'"{a}"' for a in argv)
    return f"""\
# caddy — the platform gateway, generated by ams.platform.gateway.
#
# A pinned static Caddy binary (deploy/install-host.sh) run as an unprivileged
# ams service on an ams-allocated high port. Not `apt install caddy`: that
# brings a root systemd unit, a `caddy` system user and 80/443 binding, none of
# which Phase A wants (PLAN-allin Q3).
#
# The config at {caddyfile_path(state)} is rendered by
# `ams.platform.gateway.render()` AFTER port allocation, because the entry
# site's listen address is the port ams assigns to this very service. Applying a
# new config is `ams ctl restart caddy` — the Caddy admin API is off (on shared
# loopback it is reachable by every local process).
id = "{CADDY_SERVICE_ID}"
name = "Caddy gateway"

[start]
argv = [
  {argv_toml},
]
workdir = "."

[ports]
# 0 = allocate. The number reaches Caddy through the rendered Caddyfile, not
# through argv or the environment.
{port_name} = 0

[health]
kind = "http"
port = "{port_name}"
path = "{HEALTH_PATH}"
interval_s = 10.0
timeout_s = 5.0
start_period_s = 15.0

[logging]
# Caddy is not an SDK service: it writes one JSON object per line to stderr
# (the entry site's `log` directive with `format json`), so the harness reads the level
# out of the record instead of guessing from the text (PLAN-allin Q5b).
format = "json"

[stop]
signal = "SIGTERM"
timeout_s = 10.0

[limits]
# Measured headroom, not a guess to be trusted: Caddy with a handful of
# reverse-proxy routes sits well under this, but the value is unverified at
# fleet size — revisit with cgroup memory.current once all mounts are live.
memory_max = "120M"
pids_max = 64

[restart]
policy = "always"
"""
