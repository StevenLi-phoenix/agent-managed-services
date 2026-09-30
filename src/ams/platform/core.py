"""Core mode: host the Cordis-based ``api`` core as one ams service (PLAN-core, ams 1.1.0).

The ``api`` monorepo no longer ships ``service.yaml`` manifests: it runs as **one**
Node 24 process, ``core``, that hot-installs its own plugins through a unix control
socket and owns per-plugin safety itself (isolated apply, health, atomic swap,
probation, auto-revert). ams therefore does not translate or gate plugins; it keeps
core alive, releases core itself when core-relevant paths change, ships plugins whose
*content* changed, fronts it with Caddy and escalates. This module holds the static
half of that:

- :class:`CoreConfig` -- ``<state>/platform/core.toml``, operator written. Unknown
  keys are rejected rather than guessed at, like a service declaration.
- :class:`CoreLayout` -- where everything lives under the ``core`` service root.
- :func:`import_bundle` / :func:`place_bundle` -- the config bundle (``plugins.json``
  plus JWT keys and fonts). The harness keeps a write-only master copy under
  ``<state>/secrets/core/bundle/`` (0700/0600, D16) and copies it into the service's
  ``etc/`` when it changed. Values are never logged, printed or put in argv; the
  import returns *names*.
- :func:`core_declaration` -- the ``service.toml`` text for id ``core``.
- :func:`flip_current` -- the ``current -> releases/<sha>`` symlink swap. Callers stop
  core first: the process imports from that tree.

The moving half (fetch, stage, release, ship, gate) is :mod:`ams.platform.coresync`;
the control plane is :mod:`ams.platform.corectl`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sys
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from ams.schema import MAX_PORT, MIN_PORT, parse_size
from ams.state import StateDir
from ams.uidmap import UidBlock

log = logging.getLogger("ams.platform.core")

CORE_ID = "core"
PLATFORM_DIRNAME = "platform"
CONFIG_NAME = "core.toml"
RECORD_NAME = "core.json"
BUNDLE_DIRNAME = "bundle"
PLACED_MARKER = "bundle.placed"
PLUGINS_JSON = "plugins.json"
JWT_FILES = ("jwt.pem", "jwt.pub")
FONTS_DIRNAME = "fonts"

#: The five plugins every other one leans on, in the order they must be installed.
#: Mirrors the list in upstream ``deploy/core/core-ship``.
FOUNDATION: tuple[str, ...] = ("secrets", "store", "gateway", "auth", "health")
#: Upstream ``packages/core/state.ts`` PRIVILEGES.
PRIVILEGES: tuple[str, ...] = ("ops.read", "ops.deploy")
#: Upstream ``packages/core/util.ts`` LogLevel.
LOG_LEVELS: tuple[str, ...] = ("debug", "info", "warn", "error")

GATEWAY_PORT_NAME = "gateway"
DEFAULT_GATEWAY_PORT = 18080
#: Same fixed number as the Layer-0 bring-up's gateway (``layer0.CADDY_PORT``): a
#: public front is pointed at by a tunnel, so it must not move between hosts.
DEFAULT_CADDY_PORT = 20180
DEFAULT_CORE_PATHS: tuple[str, ...] = (
    "packages/core/",
    "package.json",
    "pnpm-lock.yaml",
    "tsconfig.json",
    "scripts/build.mjs",
)
BUILD_ARGV: tuple[str, ...] = ("node", "scripts/build.mjs")
START_ARGV: tuple[str, ...] = ("node", "dist/core/main.js")
HEALTH_PATH = "/health"
HEALTH_START_PERIOD_S = 60.0
#: upstream core.service TimeoutStopSec=40: drain every plugin generation.
STOP_TIMEOUT_S = 40.0

ETC_DIR_MODE = 0o700
ETC_FILE_MODE = 0o600
DATA_DIR_MODE = 0o750
RELEASES_DIR_MODE = 0o755

#: ``sun_path`` is 104 bytes on macOS and 108 on Linux, including the NUL.
SOCKET_PATH_MAX = 104 if sys.platform == "darwin" else 108

_EXACT_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
_PLUGIN_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_MIRROR_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")
#: ``--rebase OLD=@data`` style placeholders for the new prefix.
_REBASE_TOKENS = ("@root", "@data", "@etc")


class CoreConfigError(ValueError):
    """Invalid core config or config bundle. Never carries a secret value."""


class CoreLayoutError(RuntimeError):
    """The service root holds something the layout refuses to work through."""


# --------------------------------------------------------------------------- config


@dataclass(frozen=True)
class SiteConfig:
    """One ``[[site]]``: public host -> a core listener on loopback."""

    host: str
    port: int


@dataclass(frozen=True)
class CoreConfig:
    """``<state>/platform/core.toml`` (PLAN-core §3), validated.

    Two keys beyond the plan's example, both with defaults: ``caddy_port`` (the
    fixed listen port of the Caddy front, like Layer 0's) and ``health_timeout_s``
    (how long the release gate waits for core to come back).
    """

    url: str
    node: str
    pnpm: str
    plugins: tuple[str, ...]
    mirror: str = "api"
    ref: str = "main"
    gateway_port: int = DEFAULT_GATEWAY_PORT
    extra_ports: tuple[int, ...] = ()
    memory_max: str = "900M"
    log_level: str = "info"
    probation_timeout_s: float = 120.0
    health_timeout_s: float = 90.0
    core_paths: tuple[str, ...] = DEFAULT_CORE_PATHS
    caddy_port: int = DEFAULT_CADDY_PORT
    privileges: Mapping[str, tuple[str, ...]] = field(default_factory=lambda: MappingProxyType({}))
    sites: tuple[SiteConfig, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "privileges", MappingProxyType(dict(self.privileges)))

    @property
    def listen_ports(self) -> tuple[int, ...]:
        """Every port core itself binds (the gateway plus the extras)."""
        return (self.gateway_port, *self.extra_ports)


_CORE_KEYS = frozenset(
    {
        "mirror",
        "url",
        "ref",
        "node",
        "pnpm",
        "gateway_port",
        "extra_ports",
        "memory_max",
        "log_level",
        "probation_timeout_s",
        "health_timeout_s",
        "plugins",
        "core_paths",
        "caddy_port",
        "privileges",
    }
)
_SITE_KEYS = frozenset({"host", "port"})


def config_path(state: StateDir) -> Path:
    return state.root / PLATFORM_DIRNAME / CONFIG_NAME


def record_path(state: StateDir) -> Path:
    return state.root / PLATFORM_DIRNAME / RECORD_NAME


def _str(data: Mapping[str, Any], key: str, default: str | None = None) -> str:
    value = data.get(key, default)
    if not isinstance(value, str) or not value:
        raise CoreConfigError(f"core.{key}: expected a non-empty string")
    return value


def _port(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CoreConfigError(f"{where}: expected an integer port")
    if not MIN_PORT <= value <= MAX_PORT:
        raise CoreConfigError(f"{where}: {value} is not in [{MIN_PORT}, {MAX_PORT}] (rootless)")
    return value


def _positive(data: Mapping[str, Any], key: str, default: float) -> float:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise CoreConfigError(f"core.{key}: expected a positive number")
    return float(value)


def _str_list(value: Any, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(x, str) and x for x in value):
        raise CoreConfigError(f"{where}: expected a list of non-empty strings")
    return tuple(value)


def _check_plugins(plugins: tuple[str, ...]) -> None:
    if not plugins:
        raise CoreConfigError("core.plugins: the roster must not be empty")
    seen: set[str] = set()
    for pid in plugins:
        if not _PLUGIN_ID_RE.match(pid):
            raise CoreConfigError(f"core.plugins: {pid!r} is not a plugin id")
        if pid in seen:
            raise CoreConfigError(f"core.plugins: {pid!r} listed twice")
        seen.add(pid)
    present = [p for p in FOUNDATION if p in seen]
    if list(plugins[: len(present)]) != present:
        raise CoreConfigError(
            f"core.plugins: the foundation plugins present ({', '.join(present)}) must come "
            f"first, in the order {', '.join(FOUNDATION)}; got {', '.join(plugins)}"
        )


def _check_socket(state: StateDir) -> None:
    sock = CoreLayout.from_state(state).socket
    size = len(os.fsencode(str(sock)))
    if size >= SOCKET_PATH_MAX:
        raise CoreConfigError(
            f"core socket path {sock} is {size} bytes; a unix socket path must stay under "
            f"{SOCKET_PATH_MAX} bytes on this platform -- use a shorter AMS_STATE_DIR"
        )


def _check_sites_render(sites: Sequence[SiteConfig], caddy_port: int) -> None:
    """The ``[[site]]`` tables must render: the gateway's own rules, applied at load.

    ``gateway.CoreSite`` is the one strict host check (lowercase RFC 1123, no
    wildcard -- the host is also a file name and an ``import`` argument), and
    ``render_core`` adds the entry-host and proxy-loop refusals. Running them here
    turns what would be a ``core_gateway_failed`` escalation on every tick into a
    config error naming the table.
    """
    from ams.platform.gateway import CoreSite, GatewayConfig, GatewayError, render_core

    core_sites: list[CoreSite] = []
    for i, site in enumerate(sites):
        try:
            core_sites.append(CoreSite(host=site.host, port=site.port))
        except GatewayError as e:
            raise CoreConfigError(f"site[{i}].host: {e}") from None
    try:
        # static_root is unused by render_core; any path satisfies the dataclass.
        render_core(core_sites, GatewayConfig(listen_port=caddy_port, static_root=Path("/")))
    except GatewayError as e:
        raise CoreConfigError(f"site: {e}") from None


def parse_config(data: Mapping[str, Any], *, state: StateDir) -> CoreConfig:
    """Validate a parsed ``core.toml``. Every failure names the key."""
    unknown = sorted(set(data) - {"core", "site"})
    if unknown:
        raise CoreConfigError(f"unknown top-level keys {unknown}; allowed: ['core', 'site']")
    raw = data.get("core")
    if not isinstance(raw, dict):
        raise CoreConfigError("core: required table")
    extra = sorted(set(raw) - _CORE_KEYS)
    if extra:
        raise CoreConfigError(f"core: unknown keys {extra}; allowed: {sorted(_CORE_KEYS)}")

    from ams.platform.sources import SourceError, validate_url

    url = _str(raw, "url")
    try:
        validate_url(url)
    except SourceError as e:
        raise CoreConfigError(f"core.url: {e}") from None
    mirror = _str(raw, "mirror", "api")
    if not _MIRROR_RE.match(mirror):
        raise CoreConfigError(f"core.mirror: {mirror!r} is not a valid mirror name")
    ref = _str(raw, "ref", "main")
    if not _REF_RE.match(ref):
        raise CoreConfigError(f"core.ref: {ref!r} is not a valid branch name")
    node = _str(raw, "node")
    if not _EXACT_VERSION_RE.match(node):
        raise CoreConfigError("core.node: expected an exact version 'MAJOR.MINOR.PATCH'")
    pnpm = _str(raw, "pnpm")
    if not _EXACT_VERSION_RE.match(pnpm):
        raise CoreConfigError("core.pnpm: expected an exact version 'MAJOR.MINOR.PATCH'")

    gateway_port = _port(raw.get("gateway_port", DEFAULT_GATEWAY_PORT), "core.gateway_port")
    extra_raw = raw.get("extra_ports", [])
    if not isinstance(extra_raw, list):
        raise CoreConfigError("core.extra_ports: expected a list of ports")
    extra_ports = tuple(_port(p, "core.extra_ports") for p in extra_raw)
    if len(set((gateway_port, *extra_ports))) != 1 + len(extra_ports):
        raise CoreConfigError("core.extra_ports: duplicates the gateway port or each other")
    caddy_port = _port(raw.get("caddy_port", DEFAULT_CADDY_PORT), "core.caddy_port")
    if caddy_port in (gateway_port, *extra_ports):
        raise CoreConfigError("core.caddy_port: collides with a core listener")

    memory_max = _str(raw, "memory_max", "900M")
    try:
        parse_size(memory_max)
    except ValueError as e:
        raise CoreConfigError(f"core.memory_max: {e}") from None
    log_level = _str(raw, "log_level", "info")
    if log_level not in LOG_LEVELS:
        raise CoreConfigError(f"core.log_level: {log_level!r} not one of {list(LOG_LEVELS)}")

    plugins = _str_list(raw.get("plugins"), "core.plugins")
    _check_plugins(plugins)
    core_paths = _str_list(raw.get("core_paths", list(DEFAULT_CORE_PATHS)), "core.core_paths")
    for p in core_paths:
        if p.startswith("/") or ".." in p.split("/"):
            raise CoreConfigError(f"core.core_paths: {p!r} must be repo-relative")

    priv_raw = raw.get("privileges", {})
    if not isinstance(priv_raw, dict):
        raise CoreConfigError("core.privileges: expected a table of plugin -> [privilege]")
    privileges: dict[str, tuple[str, ...]] = {}
    for pid, wanted in priv_raw.items():
        where = f"core.privileges.{pid}"
        values = _str_list(wanted, where) if wanted != [] else ()
        bad = [v for v in values if v not in PRIVILEGES]
        if bad:
            raise CoreConfigError(f"{where}: {bad} not a subset of {list(PRIVILEGES)}")
        if not _PLUGIN_ID_RE.match(pid):
            raise CoreConfigError(f"{where}: {pid!r} is not a plugin id")
        # Not required to be in the roster: a plugin installed by hand (auto-ops in
        # PLAN-core §3) still gets its privileges once core knows it.
        privileges[pid] = tuple(dict.fromkeys(values))

    sites_raw = data.get("site", [])
    if not isinstance(sites_raw, list):
        raise CoreConfigError("site: expected [[site]] tables")
    sites: list[SiteConfig] = []
    for i, site in enumerate(sites_raw):
        where = f"site[{i}]"
        if not isinstance(site, dict):
            raise CoreConfigError(f"{where}: expected a table")
        bad_keys = sorted(set(site) - _SITE_KEYS)
        if bad_keys:
            raise CoreConfigError(
                f"{where}: unknown keys {bad_keys}; allowed: {sorted(_SITE_KEYS)}"
            )
        host = site.get("host")
        if not isinstance(host, str):
            raise CoreConfigError(f"{where}.host: {host!r} is not a hostname")
        port = _port(site.get("port"), f"{where}.port")
        if port not in (gateway_port, *extra_ports):
            raise CoreConfigError(
                f"{where}.port: {port} is not a core listener (gateway_port or extra_ports)"
            )
        sites.append(SiteConfig(host=host, port=port))
    if len({s.host for s in sites}) != len(sites):
        raise CoreConfigError("site: a host is listed twice")
    _check_sites_render(sites, caddy_port)

    cfg = CoreConfig(
        url=url,
        node=node,
        pnpm=pnpm,
        plugins=plugins,
        mirror=mirror,
        ref=ref,
        gateway_port=gateway_port,
        extra_ports=extra_ports,
        memory_max=memory_max,
        log_level=log_level,
        probation_timeout_s=_positive(raw, "probation_timeout_s", 120.0),
        health_timeout_s=_positive(raw, "health_timeout_s", 90.0),
        core_paths=core_paths,
        caddy_port=caddy_port,
        privileges=privileges,
        sites=tuple(sites),
    )
    _check_socket(state)
    log.debug(
        "core config: ref=%s node=%s pnpm=%s roster=%s sites=%d",
        cfg.ref,
        cfg.node,
        cfg.pnpm,
        ",".join(cfg.plugins),
        len(cfg.sites),
    )
    return cfg


def loads_config(text: str, *, state: StateDir) -> CoreConfig:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise CoreConfigError(f"toml: {e}") from None
    return parse_config(data, state=state)


def load_config(path: Path, *, state: StateDir | None = None) -> CoreConfig:
    """Load ``core.toml``. ``state`` defaults to the one it lives in (``<state>/platform/``)."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise CoreConfigError(f"no core config at {path}; write it first (PLAN-core §3)") from None
    except OSError as e:
        raise CoreConfigError(f"cannot read {path}: {e}") from None
    st = state if state is not None else StateDir(path.resolve().parent.parent)
    return loads_config(text, state=st)


# --------------------------------------------------------------------------- layout


@dataclass(frozen=True)
class CoreLayout:
    """PLAN-core §2: everything lives under ``<state>/services/core/root``."""

    root: Path
    releases: Path
    current: Path
    data: Path
    etc: Path
    run: Path
    socket: Path
    build: Path

    @classmethod
    def from_state(cls, state: StateDir) -> CoreLayout:
        root = state.service_root(CORE_ID)
        return cls(
            root=root,
            releases=root / "releases",
            current=root / "current",
            data=root / "data",
            etc=root / "etc",
            run=root / "run",
            socket=root / "run" / "control.sock",
            build=root / "build",
        )

    def release_dir(self, sha: str) -> Path:
        if not SHA_RE.match(sha):
            raise ValueError(f"not a commit id: {sha!r}")
        return self.releases / sha

    def artifacts_dir(self, sha: str) -> Path:
        if not SHA_RE.match(sha):
            raise ValueError(f"not a commit id: {sha!r}")
        return self.build / "artifacts" / sha


def _admin(argv: Sequence[str], block: UidBlock) -> None:
    from ams.userns import run_admin

    run_admin(list(argv), block).check()


#: PATH for the coreutils run as the service (``_as_service``).
_SERVICE_PATH = "/usr/local/bin:/usr/bin:/bin"


def _as_service(argv: Sequence[str], block: UidBlock) -> None:
    """Run a coreutil as the service itself (runtime map, no harness uid mapped)."""
    from ams.userns import run_as_service

    run_as_service(list(argv), block, env={"PATH": _SERVICE_PATH, "LANG": "C.UTF-8"}).check()


def ensure_layout(layout: CoreLayout, block: UidBlock | None) -> None:
    """Create ``releases/ data/ etc/ run/ build/`` owned by the service.

    ``block`` None is plain mode (dev / ``--no-isolation``): the directories belong
    to the current user. Otherwise the service root already exists (it is made by
    ``ensure_service_root``) and belongs to the block, and the missing directories
    are made **as the service** (``run_as_service``): the root is service-owned, so
    no harness privilege is needed, and a symlink the service planted can then
    only lead ``mkdir`` where the service could write anyway. (The previous
    admin-namespace ``mkdir`` + ``chown`` followed such a link and chowned its
    target -- e.g. the harness home -- to the service.) A layout entry that is a
    symlink or not a directory is refused outright: the harness itself later
    reads through ``releases/`` and nothing legitimate ever puts a link there.
    """
    dirs = (layout.data, layout.run, layout.build)
    if block is None:
        for d in (layout.releases, *dirs):
            d.mkdir(parents=True, exist_ok=True)
        layout.etc.mkdir(parents=True, exist_ok=True)
        os.chmod(layout.etc, ETC_DIR_MODE)
        return
    missing = []
    for d in (layout.releases, *dirs, layout.etc):
        if d.is_symlink() or (d.exists() and not d.is_dir()):
            raise CoreLayoutError(
                f"core: {d} is a symlink or not a directory; refusing to work through it "
                "(something in the service replaced it -- inspect before removing it)"
            )
        if not d.exists():
            missing.append(d)
    if not missing:
        return
    # releases/ is world-traversable like a legacy <root>/repo: the harness reads
    # a staged tree's .ams-sha marker, checks package.json and stats the tree
    # there, and the spawner enters `current` -> releases/<sha> as the harness
    # before it drops to the service (userns cwd walk). Nothing secret lives in
    # it; data/ run/ build/ stay 0750 and etc/ 0700. `mkdir -m` sets the mode
    # regardless of the umask, and the service's own mkdir makes it the owner.
    modes = {layout.releases: RELEASES_DIR_MODE, layout.etc: ETC_DIR_MODE}
    for mode in dict.fromkeys(modes.get(d, DATA_DIR_MODE) for d in missing):
        paths = [str(d) for d in missing if modes.get(d, DATA_DIR_MODE) == mode]
        _as_service(["mkdir", "-m", oct(mode)[2:], "-p", *paths], block)
    log.info("core: created %s under %s", ", ".join(d.name for d in missing), layout.root)


def current_sha(layout: CoreLayout) -> str | None:
    """The release ``current`` points at, or ``None``."""
    try:
        target = os.readlink(layout.current)
    except OSError:
        return None
    name = Path(target).name
    return name if SHA_RE.match(name) else None


def flip_current(layout: CoreLayout, sha: str, block: UidBlock | None) -> None:
    """Point ``current`` at ``releases/<sha>`` atomically (a relative symlink + rename).

    **Stop core first** (CLAUDE.md): the running process imports from the tree the
    link resolves to. Relative so the link survives a moved state dir.
    """
    target = f"releases/{sha}"
    layout.release_dir(sha)  # validates the sha
    if block is None:
        tmp = layout.root / ".current.new"
        tmp.unlink(missing_ok=True)
        os.symlink(target, tmp)
        os.replace(tmp, layout.current)
    else:
        from ams.spawn import INNER_GID, INNER_UID

        # Built in the harness-owned <state>/services/core/, not in the root: the
        # service could plant `<root>/.current.new -> <dir>` and `ln -s` would then
        # create the link inside <dir>. The relative target resolves once the
        # link is renamed into the root; rename(2) never follows a destination
        # symlink (and cannot replace a directory `current` with a link).
        tmp = layout.root.parent / ".current.new"
        _admin(["rm", "-f", str(tmp)], block)
        _admin(["ln", "-s", target, str(tmp)], block)
        _admin(["chown", "-h", f"{INNER_UID}:{INNER_GID}", str(tmp)], block)
        # GNU mv -T renames onto the old link instead of into the directory it names.
        _admin(["mv", "-T", str(tmp), str(layout.current)], block)
    log.info("core: current -> %s", target)


# ---------------------------------------------------------------------- tree pins

_RANGE_TOKEN_RE = re.compile(
    r"^(>=|<=|>|<|=|\^|~)?v?(\d+|[xX*])(?:\.(\d+|[xX*]))?(?:\.(\d+|[xX*]))?$"
)
_Version = tuple[int, int, int]


def _pad(parts: Sequence[int]) -> _Version:
    a, b, c = (*parts, 0, 0, 0)[:3]
    return (a, b, c)


def _bump(parts: Sequence[int]) -> _Version:
    """The first version above a partial ``X`` / ``X.Y``: ``24`` -> 25.0.0."""
    return _pad([*parts[:-1], parts[-1] + 1])


def _token_bounds(token: str) -> list[tuple[str, _Version]] | None:
    """One semver comparator -> ``[(op, version)]`` over >=, >, <, <=, =."""
    m = _RANGE_TOKEN_RE.match(token)
    if m is None:
        return None
    op = m.group(1) or "="
    raw = [g for g in m.groups()[1:] if g is not None]
    nums: list[int] = []
    for g in raw:
        if g in ("x", "X", "*"):
            break
        nums.append(int(g))
    full = len(nums) == 3
    low = _pad(nums)
    if not nums:
        return [] if op in ("=", ">=", "^", "~") else None
    if op == "=":
        return [("=", low)] if full else [(">=", low), ("<", _bump(nums))]
    if op == "^":
        if not full:
            return [(">=", low), ("<", _bump(nums[:1]))]
        first = next((i for i, n in enumerate(nums) if n), 2)
        return [(">=", low), ("<", _bump(nums[: first + 1]))]
    if op == "~":
        return [(">=", low), ("<", _bump(nums[:2] if len(nums) >= 2 else nums))]
    if op == ">" and not full:
        return [(">=", _bump(nums))]
    if op == "<=" and not full:
        return [("<", _bump(nums))]
    return [(op, low)]


def node_satisfies(version: str, node_range: str) -> bool | None:
    """Does exact ``version`` satisfy an ``engines.node`` range? ``None`` = cannot tell.

    The subset package.json ranges use: ``||`` alternatives of space-separated
    comparators (``>=``, ``>``, ``<``, ``<=``, ``=``, ``^``, ``~``, x-ranges and
    partial versions). Anything else (hyphen ranges, tags) is not guessed at.
    """
    v = tuple(int(x) for x in version.split("."))
    checks = {
        ">=": lambda a: v >= a,
        ">": lambda a: v > a,
        "<": lambda a: v < a,
        "<=": lambda a: v <= a,
        "=": lambda a: v == a,
    }
    for alternative in node_range.split("||"):
        bounds: list[tuple[str, _Version]] = []
        for token in alternative.split():
            b = _token_bounds(token)
            if b is None:
                return None
            bounds.extend(b)
        if all(checks[op](ver) for op, ver in bounds):
            return True
    return False


def check_tree_pins(tree: Path, node: str, pnpm: str) -> list[str]:
    """Problems between ``core.node``/``core.pnpm`` and the tree's ``package.json``.

    ``engines.node`` must be satisfied by ``node`` (pnpm itself only warns, so the
    release would build and run on an unsupported runtime), and a
    ``packageManager`` pin must be ``pnpm@<core.pnpm>`` (else pnpm switches to --
    and downloads -- that version inside the provisioning step). An empty list
    is fine; so is a tree without ``package.json`` (provisioning reports that).
    """
    pkg = Path(tree) / "package.json"
    try:
        data = json.loads(pkg.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        return [f"{pkg.name}: cannot read it ({type(e).__name__})"]
    if not isinstance(data, dict):
        return [f"{pkg.name}: not a JSON object"]
    problems: list[str] = []
    engines = data.get("engines")
    node_range = engines.get("node") if isinstance(engines, dict) else None
    if isinstance(node_range, str):
        ok = node_satisfies(node, node_range)
        if ok is False:
            problems.append(
                f"core.node {node} does not satisfy the tree's engines.node {node_range!r}"
            )
        elif ok is None:
            log.warning("cannot evaluate engines.node %r; not checked", node_range)
    manager = data.get("packageManager")
    if isinstance(manager, str):
        name, _, version = manager.partition("@")
        version = version.split("+", 1)[0]
        if name != "pnpm":
            problems.append(f"the tree's packageManager is {name!r}, not pnpm")
        elif version != pnpm:
            problems.append(
                f"core.pnpm {pnpm} differs from the tree's packageManager pnpm@{version}; "
                "pnpm would switch to (and download) that version -- update core.pnpm"
            )
    return problems


# ---------------------------------------------------------------------- declaration


def _q(value: str) -> str:
    """A TOML basic string. JSON's escaping is a subset TOML accepts."""
    return json.dumps(value, ensure_ascii=False)


def extra_port_name(port: int) -> str:
    return f"port_{port}"


def core_declaration(cfg: CoreConfig, layout: CoreLayout) -> str:
    """The ``service.toml`` text for id ``core`` (PLAN-core §4.3).

    Ports are **fixed**, like Layer 0's (D22): the gateway port is written inside
    ``plugins.json`` and every Caddy site names it, so it must be known before core
    starts. Declaring them also makes the harness reserve and bind-probe them.
    """
    ports = [f"{GATEWAY_PORT_NAME} = {cfg.gateway_port}"]
    ports += [f"{extra_port_name(p)} = {p}" for p in cfg.extra_ports]
    build = ", ".join(_q(a) for a in BUILD_ARGV)
    argv = ", ".join(_q(a) for a in START_ARGV)
    return f"""\
# core — the Cordis-based api core, generated by ams.platform.core (PLAN-core).
#
# One Node process that hot-installs its own plugins through {layout.socket.name}
# (corectl). ams keeps it alive, releases it by flipping `current` to a staged and
# built tree, and ships plugins through the control socket; it never restarts core
# to change a plugin. Regenerated from <state>/platform/core.toml on every sync.
id = "{CORE_ID}"
name = "api core"

[start]
argv = [{argv}]
# releases/<sha> via the `current` symlink; flipped only while core is stopped.
workdir = "current"

[env]
CORE_STATE_DIR = {_q(str(layout.data))}
CORE_SOCKET = {_q(str(layout.socket))}
CORE_PLUGIN_CONFIG = {_q(str(layout.etc / PLUGINS_JSON))}
CORE_LOG_LEVEL = {_q(cfg.log_level)}

[ports]
# Fixed, not allocated: plugins.json and every Caddy site name these numbers.
{chr(10).join(ports)}

[runtime]
kind = "pnpm"
node = {_q(cfg.node)}
pnpm = {_q(cfg.pnpm)}
build = [{build}]

[health]
# Served by the `health` plugin through the `gateway` plugin: 503 until the
# foundation is installed and health holds ops.read.
kind = "http"
port = "{GATEWAY_PORT_NAME}"
path = "{HEALTH_PATH}"
interval_s = 10.0
timeout_s = 5.0
start_period_s = {HEALTH_START_PERIOD_S}

[logging]
# core logs one JSON object per line on stdout.
format = "json"

[stop]
# upstream core.service TimeoutStopSec=40: every generation drains first.
signal = "SIGTERM"
timeout_s = {STOP_TIMEOUT_S}

[limits]
memory_max = {_q(cfg.memory_max)}

[restart]
# A crash restarts core, which converges every plugin back to its desired artifact.
policy = "always"
"""


# --------------------------------------------------------------------------- bundle


def bundle_master_dir(state: StateDir) -> Path:
    """Harness-held master copy of the config bundle (write-only store, D16)."""
    return state.root / "secrets" / CORE_ID / BUNDLE_DIRNAME


def _placed_marker(state: StateDir) -> Path:
    return state.root / "secrets" / CORE_ID / PLACED_MARKER


def _check_bundle_shape(data: Any) -> None:
    if not isinstance(data, dict):
        raise CoreConfigError("plugins.json: expected a JSON object")
    unknown = sorted(set(data) - {"plugins", "redactionReaders"})
    if unknown:
        raise CoreConfigError(
            f"plugins.json: unknown top-level keys {unknown}; allowed: plugins, redactionReaders"
        )
    plugins = data.get("plugins")
    if not isinstance(plugins, dict):
        raise CoreConfigError("plugins.json: 'plugins' must be an object of pluginId -> settings")
    for pid, entry in plugins.items():
        if not isinstance(entry, dict):
            raise CoreConfigError(f"plugins.json: plugins.{pid} must be an object")
        bad = sorted(set(entry) - {"config", "grants"})
        if bad:
            raise CoreConfigError(
                f"plugins.json: plugins.{pid}: unknown keys {bad}; allowed: config, grants"
            )
        grants = entry.get("grants", [])
        if not isinstance(grants, list):
            raise CoreConfigError(f"plugins.json: plugins.{pid}.grants must be a list")
    readers = data.get("redactionReaders", [])
    if not isinstance(readers, list) or not all(isinstance(r, str) for r in readers):
        raise CoreConfigError("plugins.json: redactionReaders must be a list of plugin ids")


def _resolve_rebase(rebase: Sequence[tuple[str, str]], layout: CoreLayout) -> list[tuple[str, str]]:
    tokens = {"@root": layout.root, "@data": layout.data, "@etc": layout.etc}
    out: list[tuple[str, str]] = []
    for old, new in rebase:
        if not old.startswith("/") or old != old.rstrip("/") or len(old) < 2:
            raise CoreConfigError(f"rebase: OLD {old!r} must be an absolute path, no trailing /")
        if new in tokens:
            new_path = str(tokens[new])
        elif new.startswith("/"):
            new_path = new.rstrip("/") or "/"
        else:
            raise CoreConfigError(
                f"rebase: NEW {new!r} must be an absolute path or one of {list(_REBASE_TOKENS)}"
            )
        out.append((old, new_path))
    # Longest prefix first, so /var/lib/core/data can be rebased apart from /var/lib/core.
    return sorted(out, key=lambda pair: len(pair[0]), reverse=True)


def _rebase_value(
    value: Any, pairs: Sequence[tuple[str, str]], path: str, counts: dict[str, int]
) -> Any:
    if isinstance(value, str):
        for old, new in pairs:
            if value == old or value.startswith(old + "/"):
                counts[old] = counts.get(old, 0) + 1
                return new + value[len(old) :]
        return value
    if isinstance(value, dict):
        return {k: _rebase_value(v, pairs, f"{path}.{k}", counts) for k, v in value.items()}
    if isinstance(value, list):
        return [_rebase_value(v, pairs, f"{path}[{i}]", counts) for i, v in enumerate(value)]
    return value


def _outside_paths(value: Any, root: str, path: str) -> list[str]:
    """Key paths of absolute-path strings that point outside the core root."""
    if isinstance(value, str):
        if value.startswith("/") and not (value == root or value.startswith(root + "/")):
            return [path]
        return []
    if isinstance(value, dict):
        return [p for k, v in value.items() for p in _outside_paths(v, root, f"{path}.{k}")]
    if isinstance(value, list):
        return [p for i, v in enumerate(value) for p in _outside_paths(v, root, f"{path}[{i}]")]
    return []


def _write_private(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, ETC_FILE_MODE)
    try:
        os.write(fd, content)
    finally:
        os.close(fd)
    os.chmod(path, ETC_FILE_MODE)


def _chmod_tree(root: Path) -> None:
    os.chmod(root, ETC_DIR_MODE)
    for dirpath, dirnames, filenames in os.walk(root):
        for d in dirnames:
            os.chmod(Path(dirpath) / d, ETC_DIR_MODE)
        for f in filenames:
            os.chmod(Path(dirpath) / f, ETC_FILE_MODE)


def _names(root: Path) -> list[str]:
    return sorted(
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file() and not p.is_symlink()
    )


def import_bundle(
    state: StateDir,
    plugins_json: Path,
    *,
    rebase: Sequence[tuple[str, str]] = (),
    jwt_dir: Path | None = None,
    fonts_dir: Path | None = None,
    cfg: CoreConfig,
) -> list[str]:
    """Validate and store the config bundle as the harness master copy.

    ``rebase`` rewrites every string value that *starts with* OLD at a path boundary
    (``/var/lib/core`` matches ``/var/lib/core/x``, never ``/var/lib/core2``); NEW may
    be ``@root``/``@data``/``@etc`` for the core layout. The gateway plugin's port must
    equal ``cfg.gateway_port`` -- a mismatch would start a gateway nothing routes to.
    The previous master is replaced as a whole (a font removed upstream is removed
    here). Returns the relative names written; values never leave this function.
    """
    layout = CoreLayout.from_state(state)
    pairs = _resolve_rebase(rebase, layout)
    try:
        data = json.loads(Path(plugins_json).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as e:
        raise CoreConfigError(f"cannot read {plugins_json}: {type(e).__name__}") from None
    except json.JSONDecodeError as e:
        # The position only: the message would quote the offending text.
        raise CoreConfigError(
            f"{plugins_json}: not valid JSON (line {e.lineno}, column {e.colno})"
        ) from None
    _check_bundle_shape(data)

    gateway = data["plugins"].get("gateway")
    port = (gateway or {}).get("config", {}).get("port") if isinstance(gateway, dict) else None
    if "gateway" in cfg.plugins or gateway is not None:
        if isinstance(port, bool) or not isinstance(port, int):
            raise CoreConfigError("plugins.json: plugins.gateway.config.port is required")
        if port != cfg.gateway_port:
            raise CoreConfigError(
                f"plugins.json: plugins.gateway.config.port is {port} but core.gateway_port is "
                f"{cfg.gateway_port}; they must match"
            )

    counts: dict[str, int] = {}
    rebased = _rebase_value(data, pairs, "", counts)
    for old, new in pairs:
        log.info("core bundle: rebased %d value(s) from %s to %s", counts.get(old, 0), old, new)
    for key_path in _outside_paths(rebased, str(layout.root), "")[:50]:
        # The key path only; the value may be anything.
        log.warning(
            "core bundle: %s is an absolute path outside the core root; core runs as the "
            "service uid and may not reach it",
            key_path.lstrip("."),
        )

    jwt_files: list[Path] = []
    if jwt_dir is not None:
        for name in JWT_FILES:
            p = Path(jwt_dir) / name
            if not p.is_file():
                raise CoreConfigError(f"jwt dir {jwt_dir} has no {name}")
            jwt_files.append(p)
    if fonts_dir is not None and not Path(fonts_dir).is_dir():
        raise CoreConfigError(f"fonts dir {fonts_dir} is not a directory")

    master = bundle_master_dir(state)
    master.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(master.parent, ETC_DIR_MODE)
    new = master.with_name(f".{BUNDLE_DIRNAME}.new{os.getpid()}")
    old = master.with_name(f".{BUNDLE_DIRNAME}.old{os.getpid()}")
    shutil.rmtree(new, ignore_errors=True)
    new.mkdir(mode=ETC_DIR_MODE)
    try:
        _write_private(
            new / PLUGINS_JSON,
            (json.dumps(rebased, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
        )
        for p in jwt_files:
            _write_private(new / p.name, p.read_bytes())
        if fonts_dir is not None:
            shutil.copytree(fonts_dir, new / FONTS_DIRNAME, symlinks=False)
        _chmod_tree(new)
        if master.exists():
            os.replace(master, old)
        os.replace(new, master)
    finally:
        shutil.rmtree(new, ignore_errors=True)
        shutil.rmtree(old, ignore_errors=True)
    names = _names(master)
    log.info("core bundle: stored %d file(s) in %s", len(names), master)
    return names


def bundle_digest(state: StateDir) -> str | None:
    """sha256 over the master's names and bytes, or ``None`` without a master."""
    master = bundle_master_dir(state)
    if not (master / PLUGINS_JSON).is_file():
        return None
    h = hashlib.sha256()
    for name in _names(master):
        h.update(name.encode("utf-8") + b"\0")
        h.update((master / name).read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def place_bundle(state: StateDir, layout: CoreLayout, block: UidBlock | None) -> bool:
    """Copy the master bundle into ``<root>/etc`` iff it changed. ``True`` = copied.

    Change detection is a harness-side marker holding the digest last placed: in
    isolated mode ``etc/`` is 0700 and service-owned, so the harness cannot read it
    back to compare. The swap is ``etc.new`` -> ``etc`` so core never sees a
    half-copied directory.
    """
    digest = bundle_digest(state)
    if digest is None:
        log.warning("core bundle: nothing imported yet (ams platform core config import)")
        return False
    marker = _placed_marker(state)
    try:
        placed = marker.read_text(encoding="utf-8").strip()
    except OSError:
        placed = ""
    if placed == digest and (block is not None or (layout.etc / PLUGINS_JSON).is_file()):
        log.debug("core bundle: already placed (%s)", digest[:12])
        return False
    master = bundle_master_dir(state)
    new = layout.root / ".etc.new"
    old = layout.root / ".etc.old"
    if block is None:
        layout.root.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(new, ignore_errors=True)
        shutil.copytree(master, new)
        _chmod_tree(new)
        shutil.rmtree(old, ignore_errors=True)
        if layout.etc.exists():
            os.replace(layout.etc, old)
        os.replace(new, layout.etc)
        shutil.rmtree(old, ignore_errors=True)
    else:
        from ams.spawn import INNER_GID, INNER_UID

        # The copy, its modes and its owner are all made in the harness-owned
        # <state>/services/core/ -- never on a name inside the service root, which
        # core (running meanwhile) could swap for a symlink between two of these
        # forks: `chmod -R` follows a command-line symlink, and `cp -R` would copy
        # the bundle wherever it pointed. chmod before chown, so the service never
        # owns the staging copy while it is still being prepared. Only renames
        # (and an `rm -rf`, which never follows links) touch the root.
        stage = layout.root.parent / ".etc.stage"
        _admin(["rm", "-rf", str(stage), str(old)], block)
        _admin(["cp", "-R", str(master), str(stage)], block)
        _admin(["chmod", "-R", "u=rwX,go=", str(stage)], block)
        _admin(["chown", "-R", f"{INNER_UID}:{INNER_GID}", str(stage)], block)
        if layout.etc.exists():
            _admin(["mv", "-T", str(layout.etc), str(old)], block)
        _admin(["mv", "-T", str(stage), str(layout.etc)], block)
        _admin(["rm", "-rf", str(old)], block)
    marker.parent.mkdir(parents=True, exist_ok=True)
    _write_private(marker, (digest + "\n").encode("ascii"))
    log.info("core bundle: placed %s into %s", digest[:12], layout.etc)
    return True
