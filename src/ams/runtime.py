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
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ams.schema import ServiceDecl
from ams.spawn import INNER_GID, INNER_UID
from ams.state import StateDir
from ams.uidmap import UidBlock
from ams.userns import AdminResult, ensure_service_root, run_admin

log = logging.getLogger("ams.runtime")

# Tool invocations are dominated by network fetches on a cold cache; the
# 60 s default of ``run_admin`` is far too short for a first ``numpy`` install.
DEFAULT_TIMEOUT_S = 900.0

# How much of a failed tool's stderr goes into the ``ProvisionError`` message.
STDERR_TAIL_LINES = 40

_VENV_DIRNAME = ".venv"
_PACKAGE_JSON = "package.json"
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
        return RuntimeEnv(
            {"PNPM_HOME": str(store.pnpm_home), "npm_config_store_dir": str(store.pnpm_store)},
            (str(workdir / "node_modules" / ".bin"), str(store.pnpm_home / "bin")),
        )
    if kind == "bun":
        return RuntimeEnv(
            {"BUN_INSTALL": str(store.bun_install)},
            (str(workdir / "node_modules" / ".bin"), str(store.bun_install / "bin")),
        )
    if kind == "nix":
        raise NotImplementedError("nix runtime not implemented")
    raise ProvisionError(f"unknown runtime kind {kind!r}")  # pragma: no cover - schema-checked


def provisioning_env(store: RuntimeStore) -> dict[str, str]:
    """Pure: the environment the provisioning tools run under, as inner root.

    ``UV_LINK_MODE=clone`` / ``npm_config_package_import_method=clone`` /
    ``--backend=copyfile`` are what make a second environment nearly free on the
    reflink store; ``UV_PYTHON_PREFERENCE=only-managed`` keeps services off the
    host interpreter (which AppArmor confines, see D3).
    """
    home = Path.home()
    path = ":".join(
        (
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
) -> AdminResult:
    """Run one command as inner root, log it, and turn failure into ProvisionError."""
    shown = " ".join(display if display is not None else argv)
    started = time.monotonic()
    result = run_admin(list(argv), block, timeout_s=timeout_s)
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
        log.warning("%s failed (rc=%d); stderr tail:\n%s", what, result.returncode, stderr)
        raise ProvisionError(
            f"{what} failed (rc={result.returncode}): {shown}\n{stderr or '(no stderr)'}"
        )
    return result


def _tool(
    argv: Sequence[str],
    block: UidBlock,
    *,
    workdir: Path,
    env: Mapping[str, str],
    what: str,
    log_path: Path | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> AdminResult:
    """Run a provisioning tool in ``workdir`` with ``env``.

    ``run_admin`` execs with its own fixed environment and inherits the
    harness cwd, so both are set by wrapping the call in coreutils ``env``
    (``-i`` for a clean slate, ``-C`` to chdir). Never a shell string.
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
    )


# --------------------------------------------------------------------------- provisioning


def _require_linux() -> None:
    if not sys.platform.startswith("linux"):
        raise ProvisionError(
            f"runtime provisioning needs Linux (user namespaces); this is {sys.platform}"
        )


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
    )


def _provision_uv_sync(
    decl: ServiceDecl,
    workdir: Path,
    block: UidBlock,
    env: Mapping[str, str],
    log_path: Path | None,
    timeout_s: float,
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
    )


def _provision_python(
    decl: ServiceDecl,
    service_root: Path,
    workdir: Path,
    block: UidBlock,
    env: Mapping[str, str],
    log_path: Path | None,
    timeout_s: float,
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
        _install_python_interpreter(decl, workdir, block, env, log_path, timeout_s)
        _provision_uv_sync(decl, workdir, block, env, log_path, timeout_s)
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

    _install_python_interpreter(decl, workdir, block, env, log_path, timeout_s)
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
) -> None:
    """``kind = pnpm | bun``: install into ``<workdir>/node_modules``.

    bun's default backend is hardlink, which would share inodes with the
    harness-owned cache and let the closing ``chown`` flip cache ownership
    (D10); ``--backend=copyfile`` reflinks on XFS and costs the same disk.
    """
    rt = decl.runtime
    if rt.node:
        log.warning(
            "%s: runtime.node=%r is not honoured; pnpm/bun use the host node 22",
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
    workdir = service_workdir(decl, service_root)
    _ensure_root(decl, service_root, workdir, block)
    env = provisioning_env(store)

    if decl.runtime.is_python:
        _provision_python(decl, service_root, workdir, block, env, log_path, timeout_s)
    else:
        _provision_node(decl, workdir, block, env, log_path, timeout_s)

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
