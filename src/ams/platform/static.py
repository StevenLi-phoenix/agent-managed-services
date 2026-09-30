"""Publish ``kind: static`` sites, and load third-party secret-name overlays.

Two unrelated jobs share this module because both close gaps T1.2 (the
manifest translator) deliberately left open (`docs/manifest-translation.md`):

- **Static publishing.** ``kind: static`` manifests produce no ams service --
  ``Translation.decl`` is ``None`` -- so nothing else in the platform stack
  ever builds or serves their output. This module does: it stages the site's
  built tree at ``<state>/platform/static/<id>/`` (`docs/platform-sidecars.md`),
  which the gateway's ``file_server`` mount (`ams.platform.gateway`) then
  serves directly.
- **Third-party secret names.** ``TranslateContext.extra_secret_names``
  exists so a manifest can pull in a secret it has no field for (a DeepSeek
  key, a WeChat app secret, ...), but nothing populates it -- that is this
  module's ``service.ams.toml`` overlay, read once per manifest directory and
  folded into the context by the sync loop (T3.1) before it calls
  ``translate()``.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import subprocess
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from ams.platform.gateway import static_root
from ams.runtime import RuntimeStore, harness_private_paths, provisioning_env
from ams.schema import RESERVED_ENV, RESERVED_ENV_PREFIXES, SERVICE_ID_RE
from ams.state import StateDir
from ams.uidmap import UidBlock
from ams.userns import run_admin

__all__ = [
    "StaticError",
    "Overlay",
    "load_ams_overlay",
    "overlay_secret_names",
    "overlay_pool",
    "publish_static",
]

log = logging.getLogger("ams.platform.static")

_SHA_MARKER = ".ams-sha"
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")  # mirrors sources._SHA_RE (private there)
_BUILD_TIMEOUT_S = 300.0

# The two real manifests (files-web, llm-web) never need a shell: every
# `deploy.install` line is a plain argv -- `bun install`, `bun run build`,
# `find . -mindepth 1 -maxdepth 1 ! -name dist -exec rm -rf {} +`, `cp -a
# dist/. .`, `rm -rf dist`, `rm -f a b c` -- none use `&&`, `|`, `;`, `>` or
# `$()`. Allowing exactly this tool set, argv-only, generalises past the two
# literal manifests (a future static site doing the same "install, build,
# prune" dance works unmodified) without opening a shell (D1).
_ALLOWED_BUILD_BINS = frozenset({"bun", "pnpm", "find", "cp", "rm", "mv", "mkdir"})
_SHELL_TOKENS = frozenset({"&&", "||", "|", ";", ">", ">>", "<", "<<"})


class StaticError(RuntimeError):
    """A ``kind: static`` mount could not be published, or an overlay is invalid."""


# --------------------------------------------------------------------------- overlay


# Mirrors `translate._ENV_NAME_RE` (uppercase-only), not the looser public
# `ams.schema.ENV_NAME_RE`: these names flow verbatim into
# `TranslateContext.extra_secret_names`, whose own `__post_init__` gate is the
# uppercase one. Validating against the looser pattern here would let a
# lowercase name pass `load_ams_overlay` only to blow up later inside
# `translate()` with a less specific error naming `ctx.extra_secret_names`
# instead of the overlay file. `translate._ENV_NAME_RE` is private (T1.2 owns
# that module), so this is a deliberate three-line duplication, the same
# tradeoff D19 made for `service_workdir`.
_SECRET_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_OVERLAY_FILENAME = "service.ams.toml"
_OVERLAY_TOP_KEYS = frozenset({"secrets", "env", "pool"})

# Names the harness injects into the spawn environment itself. The manifest
# may *mention* the two URLs (the TranslateContext value wins there), but an
# overlay merges after translate with no later gate -- there a mention is an
# override, e.g. REGISTRY_URL pointed off-loopback to exfiltrate the
# SVC_SECRET the harness injects alongside it (issue #4). SVC_* is the
# harness's whole identity namespace. Rejected for overlay `env` *and*
# `secrets` names alike.
_HARNESS_ENV_NAMES = frozenset({"REGISTRY_URL", "AUTH_URL", "GIT_COMMIT"})
_HARNESS_ENV_PREFIX = "SVC_"

# PLAN-pool.md §3.2: `registry`/`auth`/`caddy` are the other Layer-0/1
# services a pool id must never collide with; `pool` is reserved separately
# as the pooled runner's own admin port name (§3.3's `[ports] pool = 0`).
# The `kind: static` rejection and cross-manifest checks (member id
# collisions, an existing member id reused as the pool name) are cross-file
# checks this reader cannot make -- it sees one manifest directory at a
# time -- and are done by sync/translate instead (§3.2).
_RESERVED_POOL_NAMES = frozenset({"registry", "auth", "caddy", "pool"})


@dataclass(frozen=True)
class Overlay:
    """Parsed ``service.ams.toml``. Empty when the file does not exist."""

    secrets: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    pool: str | None = None


def _check_env_name(path: Path, name: str, *, what: str) -> None:
    if not _SECRET_NAME_RE.match(name):
        raise StaticError(
            f"{path}: {what} name {name!r} does not match {_SECRET_NAME_RE.pattern!r}"
        )
    if name in RESERVED_ENV or name.startswith(RESERVED_ENV_PREFIXES):
        raise StaticError(f"{path}: {what} name {name!r} is reserved by ams")
    if name in _HARNESS_ENV_NAMES or name.startswith(_HARNESS_ENV_PREFIX):
        raise StaticError(
            f"{path}: {what} name {name!r} is injected by ams at spawn; "
            "an overlay may not set it"
        )


def load_ams_overlay(manifest_dir: Path) -> Overlay:
    """Read ``<manifest_dir>/service.ams.toml``, or return an empty ``Overlay``.

    ``secrets`` lists **names only** -- values are never written here, they go
    into the SecretStore with ``ams secret set <id> <NAME>`` (D16). ``[env]``
    is for the rare non-secret value the manifest cannot express either (a
    tunable, a feature flag); it is folded into the declaration's ``[env]`` by
    the caller, not by this function.

    Reject rather than guess (D1/T1.2's own rule, applied here): an unknown
    top-level key, a non-list ``secrets``, a non-table ``env``, a name outside
    the env-var charset, a reserved name, or a non-string value all raise
    ``StaticError`` naming the file and the offending key -- never silently
    dropped or coerced.
    """
    path = Path(manifest_dir) / _OVERLAY_FILENAME
    if not path.is_file():
        return Overlay()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise StaticError(f"{path}: invalid TOML: {e}") from e
    if not isinstance(data, dict):
        raise StaticError(f"{path}: must be a TOML table at the top level")

    unknown = set(data) - _OVERLAY_TOP_KEYS
    if unknown:
        raise StaticError(
            f"{path}: unknown key(s) {sorted(unknown)}, expected {sorted(_OVERLAY_TOP_KEYS)}"
        )

    raw_secrets = data.get("secrets", [])
    if not isinstance(raw_secrets, list):
        raise StaticError(
            f"{path}: secrets must be a list of names, not {type(raw_secrets).__name__} "
            "-- values belong in the SecretStore (`ams secret set`), never in this file"
        )
    secrets: list[str] = []
    for entry in raw_secrets:
        if not isinstance(entry, str):
            raise StaticError(f"{path}: secrets entries must be strings, got {entry!r}")
        _check_env_name(path, entry, what="secrets")
        secrets.append(entry)
    if len(set(secrets)) != len(secrets):
        raise StaticError(f"{path}: secrets lists a name more than once")

    raw_env = data.get("env", {})
    if not isinstance(raw_env, dict):
        raise StaticError(f"{path}: [env] must be a table")
    env: dict[str, str] = {}
    for k, v in raw_env.items():
        _check_env_name(path, k, what="env")
        if not isinstance(v, str):
            raise StaticError(f"{path}: env.{k} must be a string, got {v!r}")
        env[k] = v

    pool: str | None = None
    if "pool" in data:
        raw_pool = data["pool"]
        if not isinstance(raw_pool, str):
            raise StaticError(f"{path}: pool must be a string, got {raw_pool!r}")
        if not SERVICE_ID_RE.match(raw_pool):
            raise StaticError(
                f"{path}: pool {raw_pool!r} does not match {SERVICE_ID_RE.pattern!r}"
            )
        if raw_pool in _RESERVED_POOL_NAMES:
            raise StaticError(f"{path}: pool {raw_pool!r} is reserved")
        pool = raw_pool

    return Overlay(secrets=tuple(secrets), env=MappingProxyType(env), pool=pool)


def overlay_secret_names(manifest_dir: Path) -> list[str]:
    """T3.1 hook 1: names to fold into ``TranslateContext.extra_secret_names``
    before translating the manifest at ``manifest_dir``."""
    return list(load_ams_overlay(manifest_dir).secrets)


def overlay_pool(manifest_dir: Path) -> str | None:
    """T3.1 hook 2: the pool name (unprefixed, e.g. ``"core"``) declared for
    the manifest at ``manifest_dir``, or ``None`` if it does not opt into a
    pool. Mirrors ``overlay_secret_names``."""
    return load_ams_overlay(manifest_dir).pool


# --------------------------------------------------------------------------- build steps


def _parse_build_command(service_id: str, raw: str) -> list[str]:
    try:
        argv = shlex.split(raw, comments=False, posix=True)
    except ValueError as e:
        raise StaticError(f"{service_id}: build step {raw!r} is not tokenizable: {e}") from e
    if not argv:
        raise StaticError(f"{service_id}: build step is empty")
    for tok in argv:
        if tok in _SHELL_TOKENS or "$(" in tok or "`" in tok:
            raise StaticError(
                f"{service_id}: build step {raw!r} looks like it needs a shell "
                f"(saw {tok!r}); ams never runs deploy.install through a shell (D1)"
            )
    bin_name = Path(argv[0]).name
    if bin_name not in _ALLOWED_BUILD_BINS:
        raise StaticError(
            f"{service_id}: build step {raw!r} uses {bin_name!r}, not one of "
            f"{sorted(_ALLOWED_BUILD_BINS)}"
        )
    return argv


def _build_mask(
    state: StateDir, store: RuntimeStore | None = None, keep: Sequence[Path] = ()
) -> list[Path]:
    """Harness-private paths a static build must not see (issue #1).

    The build runs under the admin map, where inner root's file identity *is*
    the harness uid -- unmasked, the build scripts (and every postinstall hook
    ``bun install`` executes) could copy the SecretStore straight into the
    published tree. Everything private under ``<state>`` is hidden except the
    static subtree itself, which contains the scratch build dir. Entries are
    disjoint (:func:`ams.userns._apply_mask` forbids nesting); absent paths
    are skipped by the mask itself.

    With ``store`` the harness-private paths outside ``<state>`` are hidden too
    (:func:`ams.runtime.harness_private_paths`: the platform signing key, the
    private mirrors, the harness HOME's credentials -- security-5); ``keep``
    names what the build reads and must stay visible (its checkout).
    """
    mask = [
        state.root / name
        for name in ("secrets", "services", "state", "logs", "control.sock")
    ]
    site_root = static_root(state)
    platform = site_root.parent
    if platform.is_dir():
        mask.extend(p for p in platform.iterdir() if p != site_root)
    if store is not None:
        mask.extend(harness_private_paths(store, keep=(state.root, site_root, *keep)))
    return mask


def _run_build_step(
    argv: Sequence[str],
    block: UidBlock,
    *,
    workdir: Path,
    env: Mapping[str, str],
    what: str,
    timeout_s: float = _BUILD_TIMEOUT_S,
    mask: Sequence[Path] = (),
) -> None:
    """Run one build step as inner root in the admin ns, never as the bare
    harness process (manifest/repo build scripts are untrusted input).

    ``run_admin`` execs with a fixed minimal environment and no cwd of its own
    (``ams.userns._ADMIN_ENV``), so both are set by wrapping the call in
    coreutils ``env -i -C <workdir> K=V ...`` -- the same trick
    ``ams.runtime._tool`` uses for provisioning, duplicated here (that helper
    is private to ``ams.runtime``).

    ``mask`` carries :func:`_build_mask`'s paths: inner root would otherwise
    read everything the harness uid can, and the published tree is public.
    """
    wrapped = [
        "env",
        "-i",
        "-C",
        str(workdir),
        *(f"{k}={v}" for k, v in sorted(env.items())),
        *argv,
    ]
    result = run_admin(wrapped, block, timeout_s=timeout_s, mask=mask)
    if not result.ok:
        stderr = result.stderr.decode(errors="replace").strip()
        raise StaticError(
            f"{what} failed (rc={result.returncode}): {' '.join(argv)}\n{stderr or '(no stderr)'}"
        )


def _copy_reflink(src: Path, dst: Path) -> None:
    """``cp -a --reflink=auto src dst``, run directly as the harness.

    Unlike ``sources.SourceMirror.stage``, this tree is never chowned to a
    service uid -- there is no ams service for a static mount -- so both ends
    are harness-owned throughout and no admin ns is needed for the copy
    itself. Still shares extents with the canonical checkout on the same
    reflink store (D8/D19).
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(  # noqa: S603 - argv list, never a shell string
        ["cp", "-a", "--reflink=auto", str(src), str(dst)],
        capture_output=True,
        timeout=120.0,
    )
    if result.returncode != 0:
        raise StaticError(
            f"reflink copy {src} -> {dst} failed (rc={result.returncode}): "
            f"{result.stderr.decode(errors='replace').strip() or '(no stderr)'}"
        )


def _harden_tree_mode(root: Path) -> None:
    """D4: a published static root is harness-owned; the Caddy uid reads it
    only through the world-readable ("other") bit, never through uid mapping.
    A build tool may have left narrower permissions on some file; force them.
    """
    os.chmod(root, 0o755)
    for dirpath, dirnames, filenames in os.walk(root):
        for d in dirnames:
            os.chmod(Path(dirpath) / d, 0o755)
        for fname in filenames:
            p = Path(dirpath) / fname
            if p.is_symlink():
                continue
            os.chmod(p, 0o644)


def _reject_symlinks(root: Path) -> None:
    """A published tree must be plain files and directories only (issue #5).

    ``cp -a`` preserves symlinks committed to the repo and a build step can
    create more; Caddy's ``file_server`` follows them, so one link pointing
    outside the docroot would publish files from anywhere the Caddy uid can
    read. Fail the publish rather than ship the escape hatch.
    """
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        for name in (*dirnames, *filenames):
            p = Path(dirpath) / name
            if p.is_symlink():
                found.append(f"{p.relative_to(root)} -> {os.readlink(p)}")
    if found:
        shown = "\n  ".join(found[:5])
        more = f"\n  ... and {len(found) - 5} more" if len(found) > 5 else ""
        raise StaticError(
            f"{root.name}: refusing to publish {len(found)} symlink(s) "
            f"(file_server would follow them out of the docroot):\n  {shown}{more}"
        )


# --------------------------------------------------------------------------- publish


def publish_static(
    mount: Mapping[str, Any],
    checkout: Path,
    state: StateDir,
    store: RuntimeStore,
    block: UidBlock | None,
) -> Path:
    """Stage a ``kind: static`` site at ``<state>/platform/static/<id>/``.

    ``mount`` is the ``mount.json`` sidecar (or ``Translation.mount``) for a
    ``kind: static`` manifest. ``checkout`` is the canonical monorepo checkout
    T1.1's ``SourceMirror.materialize`` returns (``<store>/src/api/<sha>/``);
    its directory name is taken as the sha.

    The site's source is assumed to live at ``checkout/apps/<id>`` -- true for
    both real static manifests (``files-web``, ``llm-web``); see
    ``DECISIONS.md`` for why ``deploy.source.path`` is not read instead.
    ``mount["build"]`` (``deploy.install`` verbatim) runs in that order inside
    a harness-owned scratch dir, then the result is published atomically
    (rename swap; the old tree is removed only after the swap succeeds).

    Idempotent by sha: if ``<target>/.ams-sha`` already names this sha,
    returns immediately without touching the filesystem. ``block`` is
    required only when ``mount["build"]`` is non-empty -- the published tree
    is never chowned to a service uid (there is no ams service for a static
    mount), so any ``UidBlock`` works; it exists purely so ``run_admin`` can
    build a complete two-range admin map (D4/D9).
    """
    if mount.get("kind") != "static":
        raise StaticError(f"publish_static called on a non-static mount: {mount.get('kind')!r}")
    service_id = str(mount.get("id", ""))
    if not SERVICE_ID_RE.match(service_id):
        raise StaticError(f"mount.id {service_id!r} is not a valid ams service id")

    checkout = Path(checkout)
    sha = checkout.name
    if not _SHA_RE.match(sha):
        raise StaticError(f"{service_id}: checkout {checkout} does not end in a git sha")

    source_dir = checkout / "apps" / service_id
    if not source_dir.is_dir():
        raise StaticError(
            f"{service_id}: no {source_dir} in the checkout; static publishing assumes "
            f"a site's source lives at apps/{service_id} in the monorepo"
        )

    root = static_root(state)
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o755)
    target = root / service_id
    marker = target / _SHA_MARKER
    if marker.is_file() and marker.read_text(encoding="utf-8").strip() == sha:
        log.info("%s: %s already published at %s; publish is a no-op", service_id, sha, target)
        return target

    build_root = root / ".build"
    build_root.mkdir(parents=True, exist_ok=True)
    os.chmod(build_root, 0o755)
    build_dir = build_root / f"{service_id}-{sha}"
    shutil.rmtree(build_dir, ignore_errors=True)

    try:
        _copy_reflink(source_dir, build_dir)
        build_cmds = list(mount.get("build") or [])
        if build_cmds:
            if block is None:
                raise StaticError(
                    f"{service_id}: {len(build_cmds)} build step(s) declared but no "
                    "UidBlock given -- static builds never run as the bare harness "
                    "process (D1); pass any UidBlock, it is never chowned to"
                )
            env = provisioning_env(store)
            mask = _build_mask(state, store, keep=(checkout,))
            for raw in build_cmds:
                argv = _parse_build_command(service_id, raw)
                _run_build_step(
                    argv,
                    block,
                    workdir=build_dir,
                    env=env,
                    what=f"{service_id}: {raw}",
                    mask=mask,
                )
        _reject_symlinks(build_dir)
        (build_dir / _SHA_MARKER).write_text(sha + "\n", encoding="utf-8")
        _harden_tree_mode(build_dir)

        old = root / f".{service_id}.old"
        shutil.rmtree(old, ignore_errors=True)
        if target.exists():
            os.rename(target, old)
        os.rename(build_dir, target)
        shutil.rmtree(old, ignore_errors=True)
    finally:
        shutil.rmtree(build_dir, ignore_errors=True)

    log.info("%s: published %s at %s", service_id, sha, target)
    return target
