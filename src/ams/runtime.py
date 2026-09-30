"""Per-service runtime provisioning: dependency isolation on the reflink store.

The user namespace gives a service its own uids; it does not give it its own
dependencies. That is this module's job (DECISIONS D8/D9/D10/D13):

- every environment (``<root>/.venv``, ``<workdir>/.venv`` for a uv project with
  ``runtime.sync``, ``<workdir>/node_modules``) lives inside
  the service root, which lives on the XFS ``reflink=1`` store, next to the
  harness-owned tool caches. Same filesystem is a hard requirement: a
  cross-device clone fails and the tools silently fall back to full copies.
- provisioning runs as inner root in the ADMIN user namespace (inner 0 = the
  harness uid, inner 1000 = the service's block), because the shared caches are
  harness-owned while the environment must end up service-owned. One code path
  serves first provisioning and later package additions; the last step is
  ``chown -R 1000:1000 <root>``, which hands the tree to the service without
  touching the caches.

``run_admin`` execs with a fixed minimal environment and no cwd of its own, so
every tool invocation is wrapped in ``env -i -C <workdir> K=V ... <tool>``.
That is coreutils ``env``, never a shell: declaration content and package names
are attacker-influenced strings and must stay in ``argv``.

Two entry points, deliberately split:

- ``provision`` does I/O and is an explicit lifecycle step (CLI/agent runs it
  before starting a service).
- ``runtime_env`` / ``make_extra_env_for`` are pure lookups handed to
  ``Supervisor(extra_env_for=...)``; starting a service never provisions.

The managed Node toolchain (PLAN-core §4.1): a pnpm declaration pinning an
exact ``runtime.node = "X.Y.Z"`` (and optionally ``runtime.pnpm``) gets exactly
that node, downloaded from nodejs.org and verified against the release's
``SHASUMS256.txt``, into ``<store>/node/v<ver>``, and that pnpm, installed with
the managed node's own npm, into ``<store>/pnpm/<ver>``. Both are harness-owned
and world-readable, installed as the harness (no namespace), and swapped into
place with one ``rename`` so a half-extracted toolchain is never visible.
``provision_tree`` is the tree-level entry point for a staged repository
(install + build) that core mode uses; it also runs without a namespace for
development and ``--no-isolation``.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import platform
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ams.schema import EXACT_VERSION_RE, RuntimeSpec, ServiceDecl
from ams.spawn import INNER_GID, INNER_UID
from ams.state import StateDir
from ams.uidmap import UidBlock
from ams.userns import AdminResult, SpawnError, ensure_service_root, run_admin, run_as_service

log = logging.getLogger("ams.runtime")

# Tool invocations are dominated by network fetches on a cold cache; the
# 60 s default of ``run_admin`` is far too short for a first ``numpy`` install.
DEFAULT_TIMEOUT_S = 900.0

# How much of a failed tool's stderr goes into the ``ProvisionError`` message.
STDERR_TAIL_LINES = 40

# provision_tree runs a whole repository install plus a TypeScript build.
TREE_TIMEOUT_S = 1800.0

#: ``<root>/.cache``: the pnpm store, npm cache and HOME of provisioning steps
#: that run *as the service* (security-3). Service-owned, 0700, on the same
#: filesystem as the tree, so ``clone`` imports still reflink.
SERVICE_CACHE_DIRNAME = ".cache"
#: The system PATH tail a step run as the service gets after the toolchain.
_SERVICE_BASE_PATH = ("/usr/local/bin", "/usr/bin", "/bin")

# Where managed node releases come from. The archive and its checksum file are
# fetched from the same versioned directory.
NODE_DIST_URL = "https://nodejs.org/dist"
FETCH_TIMEOUT_S = 300.0
# ``npm install -g pnpm@<ver>`` on a cold cache.
PNPM_INSTALL_TIMEOUT_S = 600.0

_VENV_DIRNAME = ".venv"
_PACKAGE_JSON = "package.json"
_PNPM_LOCK = "pnpm-lock.yaml"
# sys.platform prefix / platform.machine() -> nodejs.org's names.
_NODE_OS = {"linux": "linux", "darwin": "darwin"}
_NODE_ARCH = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}
_PYPROJECT = "pyproject.toml"
_UV_LOCK = "uv.lock"


class ProvisionError(RuntimeError):
    """A runtime could not be provisioned. Message carries the tool's stderr tail."""


# --------------------------------------------------------------------------- store


@dataclass(frozen=True)
class RuntimeStore:
    """Harness-owned tool caches, all on the one reflink filesystem.

    ``bun_install`` is bun's own install prefix (the ``bun`` binary itself), not
    a cache we create; it is a field only so tests can point it elsewhere.
    """

    root: Path
    bun_install: Path = field(default_factory=lambda: Path.home() / ".bun")

    @property
    def uv_cache(self) -> Path:
        return self.root / "uv-cache"

    @property
    def python_dir(self) -> Path:
        """Where uv keeps its managed interpreters (``UV_PYTHON_INSTALL_DIR``)."""
        return self.root / "python"

    @property
    def pnpm_store(self) -> Path:
        return self.root / "pnpm-store"

    @property
    def pnpm_home(self) -> Path:
        """``PNPM_HOME``: holds the standalone ``pnpm`` binary in ``bin/``."""
        return self.root / "pnpm-home"

    @property
    def bun_cache(self) -> Path:
        return self.root / "bun-cache"

    @property
    def node_dir(self) -> Path:
        """Managed node releases, one ``v<ver>/`` per exact version."""
        return self.root / "node"

    @property
    def pnpm_dir(self) -> Path:
        """Managed pnpm installs, one ``<ver>/`` npm prefix per exact version."""
        return self.root / "pnpm"

    @property
    def npm_cache(self) -> Path:
        """npm's cache for the managed pnpm installs (kept off ``~/.npm``)."""
        return self.root / "npm-cache"

    def ensure(self) -> None:
        """Create any missing cache directory, harness-owned and world-traversable.

        Existing directories are left exactly as the host set them up: their
        mode is host policy (the installer may have made them group-writable),
        and provisioning has no business rewriting it.
        """
        for d in (self.root, self.uv_cache, self.python_dir, self.pnpm_store, self.bun_cache):
            if d.is_dir():
                continue
            d.mkdir(parents=True, exist_ok=True)
            os.chmod(d, 0o755)  # mkdir's mode is subject to umask; force it.

    @classmethod
    def from_env(cls, default: Path | None = None) -> RuntimeStore:
        """``AMS_STORE_DIR`` env var, else ``default``, else ``~/store``."""
        env = os.environ.get("AMS_STORE_DIR")
        if env:
            return cls(Path(env))
        if default is not None:
            return cls(default)
        return cls(Path.home() / "store")


# --------------------------------------------------------------------------- node toolchain


@dataclass(frozen=True)
class NodeToolchain:
    """A managed node (and optionally pnpm) in the store. Paths only; see ``ensure``."""

    node_version: str
    pnpm_version: str | None
    node_dir: Path  # <store>/node/v<ver>
    pnpm_dir: Path | None = None  # <store>/pnpm/<ver>, an npm global prefix

    @property
    def node_bin(self) -> Path:
        return self.node_dir / "bin" / "node"

    @property
    def bin_dirs(self) -> tuple[str, ...]:
        """What to PREPEND to PATH: pnpm's shim dir (if pinned), then node's bin."""
        dirs: list[str] = []
        if self.pnpm_dir is not None:
            dirs.append(str(self.pnpm_dir / "bin"))
        dirs.append(str(self.node_dir / "bin"))
        return tuple(dirs)


def node_toolchain(store: RuntimeStore, node: str, pnpm: str | None = None) -> NodeToolchain:
    """Pure: where the managed ``node``/``pnpm`` live (or will live) in ``store``."""
    return NodeToolchain(
        node_version=node,
        pnpm_version=pnpm,
        node_dir=store.node_dir / f"v{node}",
        pnpm_dir=store.pnpm_dir / pnpm if pnpm is not None else None,
    )


def managed_toolchain(spec: RuntimeSpec, store: RuntimeStore) -> NodeToolchain | None:
    """Pure: the toolchain a spec pins, or None for everything that uses the host."""
    if not spec.managed_node or spec.node is None:
        return None
    return node_toolchain(store, spec.node, spec.pnpm)


def node_platform_tag(sys_platform: str | None = None, machine: str | None = None) -> str:
    """nodejs.org's ``<os>-<arch>`` for this host (or the given one)."""
    sys_platform = sys.platform if sys_platform is None else sys_platform
    machine = platform.machine() if machine is None else machine
    os_name = next((v for k, v in _NODE_OS.items() if sys_platform.startswith(k)), None)
    arch = _NODE_ARCH.get(machine.lower())
    if os_name is None or arch is None:
        raise ProvisionError(
            f"no managed node for platform {sys_platform!r}/{machine!r}; "
            f"supported: {sorted(_NODE_OS)} x {sorted(set(_NODE_ARCH.values()))}"
        )
    return f"{os_name}-{arch}"


def node_archive_name(node: str, tag: str) -> str:
    """The release archive to fetch.

    nodejs.org publishes both ``.tar.gz`` and ``.tar.xz`` for all four supported
    tags (checked against v24.20.0's ``SHASUMS256.txt``). ``.tar.gz`` is used
    everywhere: one code path, and it needs only zlib, which every CPython
    build has, where ``lzma`` is an optional module a source-built interpreter
    can lack.
    """
    return f"node-v{node}-{tag}.tar.gz"


def _default_fetch(url: str) -> bytes:
    log.info("downloading %s", url)
    try:
        with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_S) as resp:  # noqa: S310 - fixed https URL
            data: bytes = resp.read()
    except (urllib.error.URLError, OSError) as e:
        raise ProvisionError(f"download failed: {url}: {e}") from e
    log.info("downloaded %s (%d bytes)", url, len(data))
    return data


def _require_exact(what: str, version: str) -> None:
    if not EXACT_VERSION_RE.match(version):
        raise ProvisionError(f"{what} {version!r}: a managed toolchain needs an exact 'X.Y.Z'")


def _make_world_readable_dir(d: Path) -> None:
    if d.is_dir():
        return
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o755)


def _checksum_for(sums: str, name: str) -> str:
    for line in sums.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == name:
            return parts[0].lower()
    raise ProvisionError(f"{name} is not listed in SHASUMS256.txt")


def _extract_node(archive: bytes, top: str, dest: Path) -> Path:
    """Extract a node release under ``dest``; return ``dest/<top>``. Raises ProvisionError.

    tarfile's ``data`` filter refuses absolute paths, ``..`` and links that leave
    the destination, and never chowns; on top of that every member must sit
    under the one top-level directory a real release has.
    """
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            members = tar.getmembers()
            stray = [m.name for m in members if m.name != top and not m.name.startswith(top + "/")]
            if stray:
                raise ProvisionError(f"archive has entries outside {top}/: {stray[:3]}")
            tar.extractall(dest, members=members, filter="data")
    except (tarfile.TarError, EOFError, OSError, ValueError) as e:
        raise ProvisionError(f"could not extract the node archive: {e}") from e
    extracted = dest / top
    if not (extracted / "bin" / "node").is_file():
        raise ProvisionError(f"node archive has no {top}/bin/node")
    return extracted


def _install_into_place(built: Path, target: Path, what: str) -> None:
    """``rename`` a finished directory into place; a concurrent winner is fine."""
    os.chmod(built, 0o755)  # mkdtemp is 0700; services must traverse it
    try:
        os.rename(built, target)
    except OSError as e:
        if not target.is_dir():
            raise ProvisionError(f"could not install {what} into {target}: {e}") from e
        log.info("%s appeared at %s concurrently; keeping that one", what, target)


def _ensure_node(
    store: RuntimeStore, tc: NodeToolchain, tag: str, fetch: Callable[[str], bytes]
) -> None:
    if tc.node_bin.is_file():
        log.debug("managed node %s already at %s", tc.node_version, tc.node_dir)
        return
    base = f"{NODE_DIST_URL}/v{tc.node_version}/"
    name = node_archive_name(tc.node_version, tag)
    try:
        sums = fetch(base + "SHASUMS256.txt").decode("utf-8", errors="replace")
        expected = _checksum_for(sums, name)
        archive = fetch(base + name)
    except ProvisionError:
        raise
    except Exception as e:  # an injected fetch may raise anything
        raise ProvisionError(f"could not fetch node {tc.node_version}: {e}") from e
    actual = hashlib.sha256(archive).hexdigest()
    if actual != expected:
        log.error(
            "node %s: sha256 mismatch for %s (%s != %s)", tc.node_version, name, actual, expected
        )
        raise ProvisionError(
            f"sha256 mismatch for {name}: got {actual}, SHASUMS256 says {expected}"
        )
    log.info("node %s: %s verified (sha256 %s)", tc.node_version, name, actual[:16])

    _make_world_readable_dir(store.node_dir)
    tmp = Path(tempfile.mkdtemp(prefix=f".tmp-v{tc.node_version}-", dir=store.node_dir))
    try:
        top = name.removesuffix(".tar.gz")
        extracted = _extract_node(archive, top, tmp)
        _install_into_place(extracted, tc.node_dir, f"node {tc.node_version}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    log.info("installed managed node %s at %s", tc.node_version, tc.node_dir)


def _ensure_pnpm(store: RuntimeStore, tc: NodeToolchain) -> None:
    assert tc.pnpm_dir is not None and tc.pnpm_version is not None
    if (tc.pnpm_dir / "bin" / "pnpm").exists():
        log.debug("managed pnpm %s already at %s", tc.pnpm_version, tc.pnpm_dir)
        return
    _make_world_readable_dir(store.pnpm_dir)
    npm = tc.node_dir / "bin" / "npm"
    env = {
        "PATH": ":".join((str(tc.node_dir / "bin"), "/usr/local/bin", "/usr/bin", "/bin")),
        "HOME": str(Path.home()),
        "LANG": "C.UTF-8",
        "npm_config_cache": str(store.npm_cache),
        "npm_config_update_notifier": "false",
        "npm_config_fund": "false",
        "npm_config_audit": "false",
        # This runs as the harness with no namespace. pnpm's published package
        # has no install lifecycle scripts (a self-contained bundle), so refusing
        # scripts costs nothing and keeps a compromised release from running code
        # at install time. Only the node tarball is checksum-verified.
        "npm_config_ignore_scripts": "true",
    }
    tmp = Path(tempfile.mkdtemp(prefix=f".tmp-{tc.pnpm_version}-", dir=store.pnpm_dir))
    try:
        # npm writes the prefix's bin/pnpm as a relative symlink into
        # lib/node_modules, so the finished prefix can be renamed as a whole.
        argv = [str(npm), "install", "-g", "--prefix", str(tmp), f"pnpm@{tc.pnpm_version}"]
        _run_plain(
            argv,
            cwd=tmp,
            env=env,
            what=f"pnpm {tc.pnpm_version}: npm install",
            log_path=None,
            timeout_s=PNPM_INSTALL_TIMEOUT_S,
        )
        if not (tmp / "bin" / "pnpm").exists():
            raise ProvisionError(f"npm install of pnpm@{tc.pnpm_version} left no bin/pnpm")
        _install_into_place(tmp, tc.pnpm_dir, f"pnpm {tc.pnpm_version}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    log.info("installed managed pnpm %s at %s", tc.pnpm_version, tc.pnpm_dir)


def ensure_node_toolchain(
    store: RuntimeStore,
    node: str,
    pnpm: str | None = None,
    *,
    fetch: Callable[[str], bytes] | None = None,
    platform_tag: str | None = None,
) -> NodeToolchain:
    """Make the managed node (and pnpm) exist in the store; idempotent.

    Runs as the HARNESS, in no namespace: the store is harness-owned and
    world-readable, services only ever read and execute from it. A present
    ``bin/node`` / ``bin/pnpm`` means installed (they only ever appear through
    the final ``rename``), so a warm call touches the network not at all.
    ``fetch`` (url -> bytes) is injectable for tests; the default is urllib.
    """
    _require_exact("runtime.node", node)
    if pnpm is not None:
        _require_exact("runtime.pnpm", pnpm)
    tc = node_toolchain(store, node, pnpm)
    tag = platform_tag or node_platform_tag()
    _ensure_node(store, tc, tag, fetch or _default_fetch)
    if pnpm is not None:
        _ensure_pnpm(store, tc)
    return tc


# --------------------------------------------------------------------------- env


@dataclass(frozen=True)
class RuntimeEnv:
    """What the supervisor injects into a service so it finds its runtime."""

    extra_env: Mapping[str, str] = field(default_factory=dict)
    path_prepend: tuple[str, ...] = ()

    def as_tuple(self) -> tuple[dict[str, str], tuple[str, ...]]:
        """Exactly the shape ``Supervisor(extra_env_for=...)`` expects."""
        return dict(self.extra_env), self.path_prepend


def service_workdir(decl: ServiceDecl, service_root: Path) -> Path:
    """``start.workdir`` resolved against the service root.

    Same rule as ``SpawnRequest.workdir``; duplicated (three lines) rather than
    imported so this module never needs allocated ports to answer a question
    about paths.
    """
    wd = Path(decl.start.workdir)
    return wd if wd.is_absolute() else Path(service_root) / wd


def venv_dir(service_root: Path) -> Path:
    return Path(service_root) / _VENV_DIRNAME


def python_venv_dir(decl: ServiceDecl, service_root: Path) -> Path:
    """Where this declaration's Python environment lives.

    Two layouts, because uv has two modes and they disagree about the venv:

    - default (``uv venv`` + ``uv pip install``): ``<service_root>/.venv``. The
      workdir is just a place to run from; the environment belongs to the root.
    - ``runtime.sync = true`` (the workdir is a uv *project*): ``<workdir>/.venv``,
      which is where ``uv sync`` puts it when ``UV_PROJECT_ENVIRONMENT`` is
      unset. We deliberately leave that variable unset rather than forcing the
      environment back to the root: a project's ``[tool.uv.sources]`` path
      dependencies (``../../components/sdk``) are resolved relative to the
      project, and moving the environment out of the tree is the configuration
      uv itself warns about. One less thing that can silently differ from what
      ``uv run`` would do in the same directory.
    """
    if decl.runtime.sync:
        return service_workdir(decl, service_root) / _VENV_DIRNAME
    return venv_dir(service_root)


def runtime_env(decl: ServiceDecl, service_root: Path, store: RuntimeStore) -> RuntimeEnv:
    """Pure: the env/PATH additions a provisioned service needs. No I/O.

    Raises ``NotImplementedError`` for ``kind = "nix"``: there is no nix on the
    target host, so the interface exists but the implementation does not.
    """
    kind = decl.runtime.kind
    root = Path(service_root)
    if kind == "none":
        return RuntimeEnv()
    if kind in ("venv", "uv"):
        venv = python_venv_dir(decl, root)
        return RuntimeEnv({"VIRTUAL_ENV": str(venv)}, (str(venv / "bin"),))
    workdir = service_workdir(decl, root)
    if kind == "pnpm":
        tc = managed_toolchain(decl.runtime, store)
        prepend: tuple[str, ...] = (str(workdir / "node_modules" / ".bin"),)
        if tc is not None:
            prepend += tc.bin_dirs
        if tc is None or tc.pnpm_dir is None:
            prepend += (str(store.pnpm_home / "bin"),)
        return RuntimeEnv(
            {"PNPM_HOME": str(store.pnpm_home), "npm_config_store_dir": str(store.pnpm_store)},
            prepend,
        )
    if kind == "bun":
        return RuntimeEnv(
            {"BUN_INSTALL": str(store.bun_install)},
            (str(workdir / "node_modules" / ".bin"), str(store.bun_install / "bin")),
        )
    if kind == "nix":
        raise NotImplementedError("nix runtime not implemented")
    raise ProvisionError(f"unknown runtime kind {kind!r}")  # pragma: no cover - schema-checked


def provisioning_env(store: RuntimeStore, spec: RuntimeSpec | None = None) -> dict[str, str]:
    """Pure: the environment the provisioning tools run under, as inner root.

    ``UV_LINK_MODE=clone`` / ``npm_config_package_import_method=clone`` /
    ``--backend=copyfile`` are what make a second environment nearly free on the
    reflink store; ``UV_PYTHON_PREFERENCE=only-managed`` keeps services off the
    host interpreter (which AppArmor confines, see D3). For a ``spec`` that
    pins a managed node, its bin dirs come first on PATH, so ``node``, ``npm``,
    ``pnpm`` and every ``#!/usr/bin/env node`` script resolve to the pinned ones.
    """
    home = Path.home()
    tc = managed_toolchain(spec, store) if spec is not None else None
    path = ":".join(
        (
            *(tc.bin_dirs if tc is not None else ()),
            str(home / ".local" / "bin"),
            str(store.pnpm_home / "bin"),
            str(store.bun_install / "bin"),
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
        )
    )
    return {
        "PATH": path,
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "CI": "1",
        "UV_CACHE_DIR": str(store.uv_cache),
        "UV_PYTHON_INSTALL_DIR": str(store.python_dir),
        "UV_LINK_MODE": "clone",
        "UV_PYTHON_PREFERENCE": "only-managed",
        "PNPM_HOME": str(store.pnpm_home),
        "npm_config_store_dir": str(store.pnpm_store),
        "npm_config_package_import_method": "clone",
        "BUN_INSTALL": str(store.bun_install),
        "BUN_INSTALL_CACHE_DIR": str(store.bun_cache),
    }


def service_provisioning_env(
    store: RuntimeStore, spec: RuntimeSpec | None, service_root: Path
) -> dict[str, str]:
    """Pure: the environment of a provisioning step run **as the service**.

    Nothing of the harness's own: HOME, the pnpm store, the npm cache and pnpm's
    home all live in the service's ``<root>/.cache`` -- the shared caches are
    harness-owned and the service cannot write them, which is the point. The
    toolchain (``<store>/node``, ``<store>/pnpm``, world-readable) comes first on
    PATH; the host pnpm shims are read-only there.
    """
    cache = Path(service_root) / SERVICE_CACHE_DIRNAME
    tc = managed_toolchain(spec, store) if spec is not None else None
    path = ":".join(
        (
            *(tc.bin_dirs if tc is not None else ()),
            str(store.pnpm_home / "bin"),
            *_SERVICE_BASE_PATH,
        )
    )
    return {
        "PATH": path,
        "HOME": str(cache / "home"),
        "LANG": "C.UTF-8",
        "CI": "1",
        "XDG_CACHE_HOME": str(cache / "xdg"),
        "PNPM_HOME": str(cache / "pnpm-home"),
        "npm_config_store_dir": str(cache / "pnpm-store"),
        "npm_config_cache": str(cache / "npm"),
        "npm_config_package_import_method": "clone",
    }


# --------------------------------------------------------------------------- running tools


def _tail(text: str, lines: int = STDERR_TAIL_LINES) -> str:
    kept = text.strip().splitlines()[-lines:]
    return "\n".join(kept)


def _append_log(log_path: Path | None, header: str, result: AdminResult) -> None:
    if log_path is None:
        return
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(header)
            for stream, blob in (("stdout", result.stdout), ("stderr", result.stderr)):
                text = blob.decode(errors="replace").rstrip()
                if text:
                    fh.write(f"--- {stream} ---\n{text}\n")
    except OSError as e:  # a log we cannot write must not fail a provision
        log.warning("could not append provisioning log %s: %s", log_path, e)


def _admin(
    argv: Sequence[str],
    block: UidBlock,
    *,
    what: str,
    display: Sequence[str] | None = None,
    log_path: Path | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    mask: Sequence[Path] = (),
) -> AdminResult:
    """Run one command as inner root, log it, and turn failure into ProvisionError.

    ``mask`` forwards to :func:`ams.userns.run_admin` -- set only when the
    command executes untrusted code (see :func:`provisioning_mask`); harness
    ops like the closing ``chown`` need the real files.
    """
    shown = " ".join(display if display is not None else argv)
    started = time.monotonic()
    result = run_admin(list(argv), block, timeout_s=timeout_s, mask=mask)
    return _finish(result, what=what, shown=shown, started=started, log_path=log_path)


def _finish(
    result: AdminResult,
    *,
    what: str,
    shown: str,
    started: float,
    log_path: Path | None,
    note: str = "",
) -> AdminResult:
    """Log a finished tool run and turn failure into a ProvisionError with the stderr tail."""
    elapsed = time.monotonic() - started
    log.info("%s: %s -> rc=%d in %.1fs", what, shown, result.returncode, elapsed)
    _append_log(
        log_path,
        f"\n=== {time.strftime('%Y-%m-%dT%H:%M:%S')} {what}: {shown} "
        f"rc={result.returncode} {elapsed:.1f}s\n",
        result,
    )
    if not result.ok:
        stderr = _tail(result.stderr.decode(errors="replace"))
        if not stderr:  # some tools (pnpm, tsc) report on stdout only
            stderr = _tail(result.stdout.decode(errors="replace"))
        log.warning("%s failed (rc=%d)%s; output tail:\n%s", what, result.returncode, note, stderr)
        raise ProvisionError(
            f"{what} failed (rc={result.returncode}){note}: {shown}\n{stderr or '(no output)'}"
        )
    return result


def _run_plain(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    what: str,
    log_path: Path | None,
    timeout_s: float,
) -> AdminResult:
    """Run a tool as the current user, no namespace: dev, ``--no-isolation``, macOS.

    A bare ``argv[0]`` is resolved against ``env["PATH"]`` up front for a clean
    error; a path with a slash is left for exec to resolve against ``cwd``. The
    tool leads its own session so a timeout kills everything it started.
    """
    argv = list(argv)
    shown = " ".join(argv)
    exe = argv[0]
    if "/" not in exe:
        found = shutil.which(exe, path=env.get("PATH", ""))
        if found is None:
            raise ProvisionError(f"{what}: {exe!r} not found on PATH={env.get('PATH', '')}")
        exe = found
    started = time.monotonic()
    try:
        proc = subprocess.Popen(  # noqa: S603 - argv list, never a shell
            argv,
            executable=exe,
            cwd=cwd,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as e:
        raise ProvisionError(f"{what}: could not run {shown}: {e}") from e
    note = ""
    try:
        out, err = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        log.warning("%s: %s exceeded %.0fs; killing its process group", what, shown, timeout_s)
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        out, err = proc.communicate()
        note = f" (timed out after {timeout_s:.0f}s)"
    rc = proc.returncode if not note else -signal.SIGKILL
    result = AdminResult(tuple(argv), rc, out, err)
    return _finish(result, what=what, shown=shown, started=started, log_path=log_path, note=note)


def _service_tool(
    argv: Sequence[str],
    block: UidBlock,
    *,
    workdir: Path | None,
    env: Mapping[str, str],
    what: str,
    log_path: Path | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> AdminResult:
    """Run one provisioning step as the service (``run_as_service``), logged.

    For untrusted code -- package lifecycle scripts, a repository's build: the
    runtime map has no harness uid in it, so the step can neither read the
    harness's 0600/0700 files nor write anything the harness owns, and there is
    no namespace-local mount mask to lift (security-3/-4).
    """
    shown = " ".join(argv)
    started = time.monotonic()
    try:
        result = run_as_service(
            list(argv),
            block,
            env=env,
            cwd=str(workdir) if workdir is not None else None,
            timeout_s=timeout_s,
        )
    except SpawnError as e:
        raise ProvisionError(f"{what}: could not run {shown} as the service: {e}") from e
    return _finish(result, what=what, shown=shown, started=started, log_path=log_path)


def _tool(
    argv: Sequence[str],
    block: UidBlock,
    *,
    workdir: Path,
    env: Mapping[str, str],
    what: str,
    log_path: Path | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    mask: Sequence[Path] = (),
) -> AdminResult:
    """Run a provisioning tool in ``workdir`` with ``env``.

    ``run_admin`` execs with its own fixed environment and inherits the
    harness cwd, so both are set by wrapping the call in coreutils ``env``
    (``-i`` for a clean slate, ``-C`` to chdir). Never a shell string.
    ``mask`` must carry :func:`provisioning_mask`'s paths: the tool executes
    package build hooks, which are untrusted code running with the harness
    uid's read access unless the private paths are mounted over first.
    """
    wrapped = [
        "env",
        "-i",
        "-C",
        str(workdir),
        *(f"{k}={v}" for k, v in sorted(env.items())),
        *argv,
    ]
    return _admin(
        wrapped,
        block,
        what=what,
        display=argv,
        log_path=log_path,
        timeout_s=timeout_s,
        mask=mask,
    )


# --------------------------------------------------------------------------- provisioning


def _require_linux() -> None:
    if not sys.platform.startswith("linux"):
        raise ProvisionError(
            f"runtime provisioning needs Linux (user namespaces); this is {sys.platform}"
        )


#: Store entries that are harness-private: the platform-wide RS256 signing key
#: (``<store>/platform``, D16) and the private api mirror (bare repos, upstream).
STORE_PRIVATE_NAMES = ("platform", "upstream", "repos", "src")
#: Entries of the harness HOME that hold credentials or keys.
HOME_PRIVATE_NAMES = (
    ".ssh",
    ".gnupg",
    ".config",
    ".aws",
    ".docker",
    ".kube",
    ".npmrc",
    ".netrc",
    ".pypirc",
    ".git-credentials",
    ".env",
    ".bash_history",
)


def harness_private_paths(
    store: RuntimeStore, *, keep: Sequence[Path] = (), home: Path | None = None
) -> list[Path]:
    """Harness-private paths *outside* ``<state>`` for an admin-namespace mask.

    ``<store>/{platform,upstream,repos,src}`` and the credential dotfiles of the
    harness HOME (security-5): on the target host the signing key's
    ``<store>/platform`` is a sibling of the state dir, which the ``<state>``-only
    masks never covered. An entry that contains (or is) any ``keep`` path -- the
    tree being built, the state dir, the store itself -- is left out, so a mask
    never hides what the tool works in and entries never nest with the
    ``<state>`` ones. Absent paths are fine: the mask skips them.
    """
    home = Path(home) if home is not None else Path.home()
    kept = [Path(k) for k in (*keep, store.root)]
    out: list[Path] = []
    for p in (
        *(store.root / n for n in STORE_PRIVATE_NAMES),
        *(home / n for n in HOME_PRIVATE_NAMES),
    ):
        if any(p == k or p in k.parents for k in kept):
            continue
        out.append(p)
    return out


def provisioning_mask(service_root: Path, store: RuntimeStore | None = None) -> list[Path]:
    """Harness-private paths provisioning tools must not see (issue #1).

    Provisioning runs untrusted code -- uv/pnpm/bun execute package build
    hooks -- as inner root under the admin map, where the harness uid's read
    access is the tool's read access. The service's *own* tree stays visible
    (the tools work in it); the SecretStore, harness state, the platform dir,
    the control socket and every *sibling* service tree (other services' data,
    Layer-0 key material) are mounted over empty in the child's private mount
    namespace.

    Entries are disjoint by construction (never nest -- see
    :func:`ams.userns._apply_mask`). Paths that do not exist are skipped by
    the mask itself, so a fixed layout is safe to pass.

    With ``store``, :func:`harness_private_paths` is added: the platform key and
    private mirrors under the store and the credential dotfiles of the harness
    HOME (security-5).
    """
    root = Path(service_root)
    state = root.parent.parent.parent  # <state>/services/<id>/root -> <state>
    own = root.parent  # <state>/services/<id>
    mask = [
        state / "secrets",
        state / "state",
        state / "logs",
        state / "control.sock",
        state / "platform",
    ]
    services = state / "services"
    if services.is_dir():
        mask.extend(p for p in services.iterdir() if p != own)
    if store is not None:
        mask.extend(harness_private_paths(store, keep=(state, root)))
    return mask


def _ensure_root(decl: ServiceDecl, service_root: Path, workdir: Path, block: UidBlock) -> None:
    """Make sure the service root and its workdir exist and belong to the service."""
    inside = workdir == service_root or service_root in workdir.parents
    if service_root.exists() and workdir.exists():
        return
    if not inside and not workdir.exists():
        raise ProvisionError(
            f"{decl.id}: workdir {workdir} is outside the service root and does not exist"
        )
    ensure_service_root(service_root, block, subdirs=(workdir,) if inside else ())


def _install_python_interpreter(
    decl: ServiceDecl,
    workdir: Path,
    block: UidBlock,
    env: Mapping[str, str],
    log_path: Path | None,
    timeout_s: float,
    mask: Sequence[Path] = (),
) -> None:
    """``uv python install <version>`` into the shared managed-interpreter dir.

    A no-op once the version is present, so it is safe on every re-provision.
    """
    if not decl.runtime.python:
        return
    _tool(
        ["uv", "python", "install", "-q", decl.runtime.python],
        block,
        workdir=workdir,
        env=env,
        what=f"{decl.id}: uv python install",
        log_path=log_path,
        timeout_s=timeout_s,
        mask=mask,
    )


def _provision_uv_sync(
    decl: ServiceDecl,
    workdir: Path,
    block: UidBlock,
    env: Mapping[str, str],
    log_path: Path | None,
    timeout_s: float,
    mask: Sequence[Path] = (),
) -> None:
    """``kind = "uv"`` + ``sync = true``: the workdir is a uv project.

    ``uv sync`` reads ``pyproject.toml`` (and ``uv.lock`` when present) and
    builds ``<workdir>/.venv``. This is the mode that makes a real repository
    runnable as a service without translating its dependency metadata into a
    declaration: workspace members and ``[tool.uv.sources]`` path dependencies
    (``sdk = { path = "../../components/sdk", editable = true }``) resolve
    exactly as they do for a developer running ``uv sync`` by hand, provided the
    whole tree was copied into the service root.

    ``--frozen`` is the default because a declaration that pins nothing is not a
    reproducible service: with a lock file present, provisioning installs the
    locked versions and never re-resolves. Without one there is nothing to
    freeze, so we fall back to a resolving ``uv sync`` and WARN, because the
    versions the service ends up with are then whatever the index served today.
    The absence of ``uv.lock`` is checked directly rather than by letting
    ``--frozen`` fail: uv's failure for a missing lock and its failure for a
    *stale* lock are both non-zero exits, and only the first is safe to retry.
    """
    if not (workdir / _PYPROJECT).is_file():
        raise ProvisionError(
            f"{decl.id}: runtime.sync=true needs a uv project, but there is no "
            f"{_PYPROJECT} in {workdir}"
        )
    argv = ["uv", "sync", "--frozen"]
    if not (workdir / _UV_LOCK).is_file():
        log.warning(
            "%s: no %s in %s; falling back to a resolving 'uv sync'. "
            "Dependency versions are not pinned by the declaration.",
            decl.id,
            _UV_LOCK,
            workdir,
        )
        argv = ["uv", "sync"]
    _tool(
        argv,
        block,
        workdir=workdir,
        env=env,
        what=f"{decl.id}: {' '.join(argv)}",
        log_path=log_path,
        timeout_s=timeout_s,
        mask=mask,
    )


def _provision_python(
    decl: ServiceDecl,
    service_root: Path,
    workdir: Path,
    block: UidBlock,
    env: Mapping[str, str],
    log_path: Path | None,
    timeout_s: float,
    mask: Sequence[Path] = (),
) -> None:
    """``kind = venv | uv``: both are provisioned by uv.

    The two kinds differ in intent only (``venv`` says "a Python virtualenv",
    ``uv`` says "and I know it is uv doing it"); one code path means one set of
    failure modes, and uv is the only Python tool on the box that can clone out
    of the shared cache.

    ``runtime.sync`` splits off before any of that: a uv project owns its own
    environment and dependency set, so there is no venv for us to create and no
    package list for us to install (see :func:`_provision_uv_sync`).
    """
    rt = decl.runtime
    if rt.sync:
        _install_python_interpreter(decl, workdir, block, env, log_path, timeout_s, mask)
        _provision_uv_sync(decl, workdir, block, env, log_path, timeout_s, mask)
        return

    venv = venv_dir(service_root)
    install: list[str] = []
    if rt.requirements:
        # Checked before anything is built: a typo in the declaration should
        # not cost an interpreter download and a venv the caller cannot use.
        req = Path(rt.requirements)
        req = req if req.is_absolute() else workdir / req
        if not req.is_file():
            raise ProvisionError(f"{decl.id}: requirements file not found: {req}")
        install += ["-r", str(req)]
    install += list(rt.packages)

    _install_python_interpreter(decl, workdir, block, env, log_path, timeout_s, mask)
    if not (venv / "pyvenv.cfg").exists():
        argv = ["uv", "venv", "-q"]
        if rt.python:
            argv += ["--python", rt.python]
        _tool(
            [*argv, str(venv)],
            block,
            workdir=workdir,
            env=env,
            what=f"{decl.id}: uv venv",
            log_path=log_path,
            timeout_s=timeout_s,
            mask=mask,
        )
    else:
        log.info("%s: reusing existing venv %s", decl.id, venv)

    if not install:
        return
    _tool(
        ["uv", "pip", "install", "-q", "--python", str(venv / "bin" / "python"), *install],
        block,
        workdir=workdir,
        env=env,
        what=f"{decl.id}: uv pip install",
        log_path=log_path,
        timeout_s=timeout_s,
        mask=mask,
    )


def _write_package_json(decl: ServiceDecl, target: Path, block: UidBlock) -> None:
    """Seed a minimal ``package.json`` the service uid owns.

    The workdir already belongs to the service, so the harness cannot write into
    it: stage the file in a harness-owned temp dir and ``cp`` it in as inner
    root (which sees the harness uid as its own uid 0).
    """
    content = json.dumps({"name": decl.id, "private": True}, indent=2) + "\n"
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / _PACKAGE_JSON
        src.write_text(content, encoding="utf-8")
        os.chmod(src, 0o644)
        _admin(
            ["cp", str(src), str(target)],
            block,
            what=f"{decl.id}: seed {_PACKAGE_JSON}",
            timeout_s=60.0,
        )
    log.info("%s: created minimal %s", decl.id, target)


def _provision_node(
    decl: ServiceDecl,
    workdir: Path,
    block: UidBlock,
    env: Mapping[str, str],
    log_path: Path | None,
    timeout_s: float,
    mask: Sequence[Path] = (),
    *,
    store: RuntimeStore | None = None,
    service_root: Path | None = None,
) -> None:
    """``kind = pnpm | bun``: install into ``<workdir>/node_modules``.

    bun's default backend is hardlink, which would share inodes with the
    harness-owned cache and let the closing ``chown`` flip cache ownership
    (D10); ``--backend=copyfile`` reflinks on XFS and costs the same disk.
    """
    rt = decl.runtime
    if rt.node and not rt.managed_node:
        log.warning(
            "%s: runtime.node=%r is not honoured; pnpm/bun use the host node 22 "
            "(pin an exact 'X.Y.Z' on kind=pnpm for a managed node)",
            decl.id,
            rt.node,
        )
    pkg = workdir / _PACKAGE_JSON
    if not pkg.exists():
        if not rt.packages:
            raise ProvisionError(
                f"{decl.id}: no {_PACKAGE_JSON} in {workdir} and runtime.packages is empty; "
                "nothing to install"
            )
        _write_package_json(decl, pkg, block)

    if rt.kind == "pnpm":
        install, add = ["pnpm", "install", "--silent"], ["pnpm", "add", "--silent"]
    else:
        install = ["bun", "install", "--backend=copyfile", "--silent"]
        add = ["bun", "add", "--backend=copyfile"]
    _tool(
        install,
        block,
        workdir=workdir,
        env=env,
        what=f"{decl.id}: {rt.kind} install",
        log_path=log_path,
        timeout_s=timeout_s,
        mask=mask,
    )
    if rt.packages:
        _tool(
            [*add, *rt.packages],
            block,
            workdir=workdir,
            env=env,
            what=f"{decl.id}: {rt.kind} add",
            log_path=log_path,
            timeout_s=timeout_s,
            mask=mask,
        )
    if rt.build:
        # Repository code: run it as the service, not as inner root (security-3).
        # The install above ran as inner root, so the tree is handed over first.
        if store is None or service_root is None:
            raise ProvisionError(f"{decl.id}: a build step needs the store and service root")
        _admin(
            ["chown", "-R", f"{INNER_UID}:{INNER_GID}", str(service_root)],
            block,
            what=f"{decl.id}: chown to service before build",
            log_path=log_path,
            timeout_s=timeout_s,
        )
        _service_tool(
            list(rt.build),
            block,
            workdir=workdir,
            env=service_provisioning_env(store, rt, service_root),
            what=f"{decl.id}: build",
            log_path=log_path,
            timeout_s=timeout_s,
        )


def provision(
    decl: ServiceDecl,
    service_root: Path,
    store: RuntimeStore,
    block: UidBlock,
    *,
    log_path: Path | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> RuntimeEnv:
    """Create or update the service's runtime; return what to inject at spawn.

    Idempotent: an existing venv is reused (so re-running only adds packages),
    ``uv python install`` and ``pnpm/bun install`` are no-ops when satisfied.
    On failure the tree is left as it is, still rooted at a service-owned
    directory; the leftovers inside belong to the harness until a later
    successful run (or the spawner's ``ensure_service_root``) chowns them.
    """
    _require_linux()
    service_root = Path(service_root)
    kind = decl.runtime.kind
    if kind == "nix":
        raise ProvisionError(f"{decl.id}: nix runtime not implemented")
    # Rejected here, before an interpreter is downloaded or a root is created:
    # the schema cannot express "these two fields are exclusive" without turning
    # a runtime restriction into a load-time one, and this is a runtime
    # restriction. `uv add` into a synced project would edit the repository's own
    # pyproject.toml/uv.lock inside the service root, so the running service
    # would no longer match the source it was copied from. Declare the
    # dependency in the project instead.
    if decl.runtime.sync and decl.runtime.packages:
        raise ProvisionError(
            f"{decl.id}: runtime.sync=true takes its dependencies from "
            f"{_PYPROJECT}/{_UV_LOCK}; runtime.packages must be empty "
            f"(got {list(decl.runtime.packages)})"
        )
    result = runtime_env(decl, service_root, store)
    if kind == "none":
        log.debug("%s: runtime kind=none, nothing to provision", decl.id)
        return result

    store.ensure()
    if decl.runtime.managed_node and decl.runtime.node is not None:
        # As the harness, before the root exists: a failed download must not
        # leave a half-made service root behind.
        ensure_node_toolchain(store, decl.runtime.node, decl.runtime.pnpm)
    workdir = service_workdir(decl, service_root)
    _ensure_root(decl, service_root, workdir, block)
    env = provisioning_env(store, decl.runtime)
    mask = provisioning_mask(service_root, store=store)

    if decl.runtime.is_python:
        _provision_python(decl, service_root, workdir, block, env, log_path, timeout_s, mask)
    else:
        _provision_node(
            decl,
            workdir,
            block,
            env,
            log_path,
            timeout_s,
            mask,
            store=store,
            service_root=service_root,
        )

    _admin(
        ["chown", "-R", f"{INNER_UID}:{INNER_GID}", str(service_root)],
        block,
        what=f"{decl.id}: chown to service",
        log_path=log_path,
        timeout_s=timeout_s,
    )
    owner = os.stat(service_root).st_uid
    if owner != block.uid_start:
        raise ProvisionError(
            f"{decl.id}: service root {service_root} is owned by host uid {owner}, "
            f"expected {block.uid_start}"
        )
    log.info("%s: provisioned runtime kind=%s at %s", decl.id, kind, service_root)
    return result


def _containing_service_root(tree: Path) -> Path:
    """The ``<state>/services/<id>/root`` directory ``tree`` lives in."""
    for p in (tree, *tree.parents):
        if p.name == "root" and p.parent.parent.name == "services":
            return p
    raise ProvisionError(
        f"provision_tree: {tree} is not inside a service root (<state>/services/<id>/root)"
    )


def provision_tree(
    tree: Path,
    spec: RuntimeSpec,
    *,
    block: UidBlock | None,
    store: RuntimeStore,
    run_build: bool = True,
    log_path: Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout_s: float = TREE_TIMEOUT_S,
) -> None:
    """Install (and build) a staged pnpm repository tree in place.

    ``pnpm install --frozen-lockfile`` when ``pnpm-lock.yaml`` exists (a
    resolving install, with a warning, when it does not), then ``spec.build`` if
    ``run_build``. A managed spec gets its toolchain ensured first and put
    first on PATH.

    ``block`` given: every step runs **as the service** (``run_as_service``),
    never as inner root. The install runs package lifecycle scripts and the
    build runs repository code -- untrusted code that, as inner root, held the
    harness uid's write access (the ams source, its venv, ``~/.ssh``, the shared
    toolchain) and could unmount the issue-#1 mask in its own namespace
    (review 2026-09-29, security-3/-4/-5). As the service the harness uid is
    not mapped at all. The tree must already belong to the service (``stage``
    chowns it); the pnpm store, caches and HOME are the service's own under
    ``<root>/.cache`` (:func:`service_provisioning_env`), so no chown follows.
    ``block`` None: a plain subprocess as the current user, for development,
    ``--no-isolation`` and macOS; there the pnpm import method is
    ``clone-or-copy``, because a dev tree and store need not share a filesystem
    and a hard ``clone`` would fail.

    ``env`` is merged over the provisioning environment, the caller winning
    (e.g. ``CORE_SOURCE_COMMIT``); it must never carry a secret.
    """
    tree = Path(tree)
    if spec.kind != "pnpm":
        raise ProvisionError(f"provision_tree supports kind=pnpm only, got kind={spec.kind}")
    if not tree.is_dir():
        raise ProvisionError(f"provision_tree: tree {tree} does not exist")
    if not (tree / _PACKAGE_JSON).is_file():
        raise ProvisionError(f"provision_tree: no {_PACKAGE_JSON} in {tree}")
    service_root: Path | None = None
    if block is not None:
        _require_linux()
        service_root = _containing_service_root(tree)
        owner = os.stat(tree).st_uid
        if owner != block.uid_start:
            raise ProvisionError(
                f"tree {tree} is owned by host uid {owner}, expected {block.uid_start} "
                "(stage hands the tree to the service before it is provisioned)"
            )

    store.ensure()
    if spec.managed_node and spec.node is not None:
        ensure_node_toolchain(store, spec.node, spec.pnpm)
    if service_root is None:
        tool_env = provisioning_env(store, spec)
        tool_env["npm_config_package_import_method"] = "clone-or-copy"
    else:
        tool_env = service_provisioning_env(store, spec, service_root)
    tool_env.update(env or {})

    install = ["pnpm", "install", "--frozen-lockfile"]
    if not (tree / _PNPM_LOCK).is_file():
        log.warning(
            "%s: no %s; falling back to a resolving 'pnpm install'. "
            "Dependency versions are whatever the registry serves today.",
            tree,
            _PNPM_LOCK,
        )
        install = ["pnpm", "install"]
    steps: list[tuple[list[str], str]] = [(install, "pnpm install")]
    if run_build and spec.build:
        steps.append((list(spec.build), "build"))
    mode = "plain" if block is None else f"as the service (block {block.uid_start})"
    log.info("provision_tree %s: %d step(s), %s", tree, len(steps), mode)

    if block is not None and service_root is not None:
        cache = service_root / SERVICE_CACHE_DIRNAME
        _service_tool(
            ["mkdir", "-m", "700", "-p", str(cache), str(cache / "home")],
            block,
            workdir=None,
            env={"PATH": ":".join(_SERVICE_BASE_PATH), "LANG": "C.UTF-8"},
            what=f"{tree.name}: service cache dir",
            log_path=log_path,
            timeout_s=60.0,
        )
    for argv, what in steps:
        label = f"{tree.name}: {what}"
        if block is None:
            _run_plain(
                argv, cwd=tree, env=tool_env, what=label, log_path=log_path, timeout_s=timeout_s
            )
        else:
            _service_tool(
                argv,
                block,
                workdir=tree,
                env=tool_env,
                what=label,
                log_path=log_path,
                timeout_s=timeout_s,
            )
    log.info("provision_tree %s: done (%s)", tree, mode)


def make_extra_env_for(
    state: StateDir, store: RuntimeStore
) -> Callable[[ServiceDecl], tuple[dict[str, str], tuple[str, ...]]]:
    """Adapter for ``Supervisor(extra_env_for=...)``.

    Pure lookup: starting a service never provisions it. Provisioning is an
    explicit lifecycle step so that a start never blocks on the network and a
    package install failure is reported where the agent asked for it.
    """

    def lookup(decl: ServiceDecl) -> tuple[dict[str, str], tuple[str, ...]]:
        return runtime_env(decl, state.service_root(decl.id), store).as_tuple()

    return lookup
