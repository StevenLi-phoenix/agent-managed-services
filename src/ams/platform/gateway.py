"""Caddy configuration for the core-mode gateway.

The edge is a single pinned, static Caddy binary run as an ordinary ams
service on a fixed high port (``caddy_port`` in ``core.toml``). The core
process's own `gateway` plugin is the single HTTP entry: it routes,
authenticates and sets every response header per plugin route. Caddy is only a
host -> loopback port map read from ``[[site]]`` tables:

    <state>/gateway/Caddyfile            global options + the entry site
    <state>/gateway/sites/<host>.caddy   one ``reverse_proxy`` per host

Nothing here talks to Caddy. `render_core()` is pure, `write()` only touches
the filesystem, and applying a new config is ``ams ctl restart caddy`` -- the
admin API is off (an admin endpoint on shared loopback is reachable by every
local process). Plain HTTP only: TLS terminates at Cloudflare / the tunnel.

Permissions
-----------
Caddy runs as a mapped uid (inner 1000, D4); the harness uid is not mapped
inside the namespace, so harness-owned files show up as ``nobody``-owned. They
are still *readable* if the mode says so, which is why `write()` forces 0755 on
the directories and 0644 on the files. The 0600 secret store stays unreadable --
that asymmetry is the point of D4. The same rule is why file access logs are
off by default: a log file has to be *written*, and no harness-owned directory
is writable by the Caddy uid. See `GatewayConfig.log_dir`.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ams.state import StateDir

log = logging.getLogger("ams.platform.gateway")

GATEWAY_DIRNAME = "gateway"
SITES_DIRNAME = "sites"
CADDYFILE_NAME = "Caddyfile"

#: Path served by the entry site for the harness's own health probe. It answers
#: before any import, so a gateway with zero sites is still observably up.
HEALTH_PATH = "/ams-health"

DIR_MODE = 0o755
FILE_MODE = 0o644

#: Access-log rotation (production's deployer values), used only when
#: `GatewayConfig.log_dir` is set.
LOG_ROLL_SIZE = "50MiB"
LOG_ROLL_KEEP = 10
LOG_ROLL_KEEP_FOR = "90d"

#: The entry site's catch-all: a request for a host Caddy has no ``[[site]]`` for.
CORE_ENTRY_404_BODY = '{"error":"not_found","detail":"no such site"}'


class GatewayError(RuntimeError):
    """A site or config that would not survive being written into a Caddyfile."""


@dataclass(frozen=True)
class GatewayConfig:
    """Everything the renderer needs that is not a site.

    ``listen_port`` is Caddy's own port (``caddy_port``): the address the entry
    site binds and the port the harness health-probes.

    ``log_dir`` is ``None`` by default on purpose. Caddy runs as a mapped uid,
    so it cannot write into a harness-owned directory (D4); with no ``output``
    the JSON access log goes to stderr, which the harness already reads line by
    line and routes through the decision interface. Set it only to a directory
    the Caddy uid owns (e.g. ``<state>/services/caddy/root/logs``).
    """

    listen_port: int
    entry_host: str = "127.0.0.1"
    log_dir: Path | None = None
    plain_http: bool = True
    #: Extra global options, emitted verbatim (one directive per element).
    global_options: tuple[str, ...] = ()

    def site_address(self, host: str) -> str:
        """``http://<host>:<listen_port>``: an explicit scheme keeps automatic
        HTTPS off for the site, and the port is required because Caddy is not
        on 80."""
        if self.plain_http:
            return f"http://{host}:{self.listen_port}"
        return host


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


#: A site label is written into the Caddyfile *unquoted*, so it cannot merely
#: be quote-checked: anything outside this charset could change the config's
#: structure rather than one string.
_HOST_OK = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-*")


def _check_token(value: str, allowed: set[str], what: str, where: str) -> str:
    bad = sorted(set(value) - allowed)
    if bad or not value:
        raise GatewayError(f"{where}: {what} {value!r} has illegal characters {bad}")
    return value


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


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

#: RFC 1123 labels, lowercase only. Stricter than ``_HOST_OK`` on purpose: a
#: site's host is also its snippet's file name and its ``import`` argument,
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
    # The charset check first, then the strict shape.
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
    # Canonical (no blank line before it), TLS off because it terminates at
    # Cloudflare, admin API off.
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

    ``Caddyfile`` plus one ``sites/<host>.caddy`` per site; `write()` applies
    it and removes snippets of sites that are gone. Pure and independent of
    input order (sites are emitted sorted by host).

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
    changes: a removed site must reach Caddy too.

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


def caddy_declaration(state: StateDir, store: Path, port: int) -> str:
    """The `service.toml` text for the gateway itself.

    Caddy is an ordinary ams service: a pinned static binary in
    ``<store>/bin/caddy`` (installed by ``deploy/install-host.sh``), no root, no
    system unit, no 80/443. ``port`` is fixed (``caddy_port``) because the
    listen address is inside the Caddyfile, which must exist before Caddy
    starts. The declaration never mentions a site, so it is written once and
    only the config underneath it changes.
    """
    if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535:
        raise GatewayError(f"caddy port must be an int in 1024..65535, got {port!r}")
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
# caddy -- the core-mode gateway, generated by ams.platform.gateway.
#
# A pinned static Caddy binary (deploy/install-host.sh) run as an unprivileged
# ams service on a fixed high port. Not `apt install caddy`: that brings a root
# systemd unit, a `caddy` system user and 80/443 binding.
#
# The config at {caddyfile_path(state)} is rendered by
# `ams.platform.gateway.render_core()` from core.toml's [[site]] tables.
# Applying a new config is `ams ctl restart caddy` -- the Caddy admin API is off
# (on shared loopback it is reachable by every local process).
id = "{CADDY_SERVICE_ID}"
name = "Caddy gateway"

[start]
argv = [
  {argv_toml},
]
workdir = "."

[ports]
# Fixed: the same number is the entry site's listen address in the Caddyfile.
main = {port}

[health]
kind = "http"
port = "main"
path = "{HEALTH_PATH}"
interval_s = 10.0
timeout_s = 5.0
start_period_s = 15.0

[logging]
# Caddy writes one JSON object per line to stderr (the `log` directive with
# `format json`), so the harness reads the level out of the record instead of
# guessing from the text.
format = "json"

[stop]
signal = "SIGTERM"
timeout_s = 10.0

[limits]
memory_max = "120M"
pids_max = 64

[restart]
policy = "always"
"""
