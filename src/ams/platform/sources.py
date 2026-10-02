"""Source delivery: one bare mirror, one canonical checkout per sha, reflink copies.

The pilot rsynced the repository into every service root: 31 MiB of *unshared*
blocks each, so twenty services would have cost ~620 MiB of a 6 GB store to hold
twenty copies of one 17 MiB tree. This module replaces that with three layers on
the single XFS ``reflink=1`` store (D8/D13):

``<store>/repos/<name>.git``
    One bare mirror, harness-owned, updated by ``git fetch``. The only thing
    that talks to the network.

``<store>/src/<name>/<sha>/``
    One canonical checkout per commit, produced by ``git archive <sha>`` piped
    into ``tar -x``. Harness-owned, read-only in practice, shared by every
    service on that sha. ``gc(keep)`` prunes the least recently used.

``<service_root>/repo``
    A per-service copy made with ``cp -dR --reflink=auto`` *inside the admin user
    namespace*, then chowned to the service block. Same filesystem as the
    canonical tree, so the extents are shared and the second service on a sha
    costs metadata only. ``stage(dest=...)`` puts it elsewhere under the root
    (core mode stages ``releases/<sha>`` and flips a ``current`` symlink,
    PLAN-core §2); ``stage_plain`` is the same copy without a namespace, for dev
    hosts and ``--no-isolation``.

Why ``git archive | tar`` rather than ``git worktree``: a worktree's metadata
(``.git`` file, ``worktrees/<id>/`` in the bare repo) is written by git as the
harness uid, while the checkout itself must be chowned to the service uid. Git
would then be managing a tree it can no longer read or write, and every service
would still need its own full copy of the files. The archive pipe has no such
split: the canonical tree is inert data, and the per-service copy is a plain
``cp``.

Why the copy runs through :func:`ams.userns.run_admin`: the service root is
owned by the service's host uid block, which the harness cannot write. As inner
root in the admin map the harness holds CAP_DAC_OVERRIDE over exactly the mapped
uids, which is the same mechanism runtime provisioning uses (D9).

Phase A fetches public HTTPS only. ``git@host:path`` and ``ssh://`` are rejected
outright rather than silently depending on an agent key, and an ``https://`` URL
carrying credentials is refused so no password can reach a log line or an
exception message.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import IO
from urllib.parse import urlsplit

from ams.spawn import INNER_GID, INNER_UID
from ams.state import write_json_atomic
from ams.uidmap import UidBlock
from ams.userns import AdminResult, ensure_service_root, run_admin

log = logging.getLogger("ams.platform.sources")

# git fetch over the network; the sync loop is a one-shot process, so a hung
# fetch costs one tick and never blocks the supervisor (Q1, risk 6).
DEFAULT_FETCH_TIMEOUT_S = 120.0
# archive|tar and cp of a whole tree: slow on a 1 vCPU box, still bounded.
DEFAULT_ARCHIVE_TIMEOUT_S = 300.0
DEFAULT_STAGE_TIMEOUT_S = 300.0

STDERR_TAIL_LINES = 20

#: Name of the file recording which commit a staged ``repo`` holds.
SHA_MARKER = ".ams-sha"

_INDEX_NAME = "index.json"
_REPO_DIRNAME = "repo"
# A staged tree is built at "<dest>.new" and the one it replaces parked at
# "<dest>.old", beside it: same directory, so both mv's are renames.
_NEW_SUFFIX = ".new"
_OLD_SUFFIX = ".old"

# Full or abbreviated commit ids only. Anything else would let a caller name a
# directory outside <store>/src/<name>/.
_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")
# Branch/tag names we are willing to interpolate into "refs/heads/<ref>".
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
# One component of a stage destination. It reaches admin argv as part of a path,
# so a leading "-" (an option to rm/cp/mv) and anything a human would misread
# are refused along with "." and "..".
_DEST_PART_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*")

_TOOL_PATH = "/usr/local/bin:/usr/bin:/bin"



#: How a staged tree is copied inside the admin namespace. Not ``-a``: that
#: preserves POSIX ACLs and xattrs, and an ACL naming a host user who is not
#: mapped into the namespace fails with EINVAL ("preserving permissions ...
#: Invalid argument") -- seen on GitHub's runners. Mode bits still come from
#: the source (masked by the umask); ownership is set by the chown that follows.
STAGE_CP_FLAGS: tuple[str, ...] = ("-dR", "--preserve=timestamps,links", "--reflink=auto")

class SourceError(RuntimeError):
    """A git/tar/cp step failed, timed out, or the input was refused.

    The message carries the argv and a tail of the tool's stderr; it never
    carries a credential, because credential-bearing URLs are rejected before
    any subprocess runs.
    """


def _tail(text: str, lines: int = STDERR_TAIL_LINES) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def _git_env() -> dict[str, str]:
    """A hermetic environment for git.

    ``GIT_TERMINAL_PROMPT=0`` and an empty ``GIT_ASKPASS`` are what stop a
    private URL from turning a 120 s timeout into a credential prompt that hangs
    for the whole timeout. The config files are pinned to ``/dev/null`` so a
    stray ``~/.gitconfig`` on a dev machine cannot change what the harness
    fetches.
    """
    return {
        "PATH": _TOOL_PATH,
        "HOME": str(Path.home()),
        "LC_ALL": "C",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }


def _which(tool: str) -> str:
    exe = shutil.which(tool) or shutil.which(tool, path=_TOOL_PATH)
    if exe is None:
        raise SourceError(f"{tool!r} not found on PATH; source delivery needs git and tar")
    return exe


def validate_url(url: str) -> str:
    """Return ``url`` if this Phase-A harness may fetch it, else raise.

    Allowed: ``https://`` without credentials, ``file://``, and an absolute
    local path (the last two exist so tests can drive a throwaway repository).
    """
    if not url or url.strip() != url:
        raise SourceError(f"invalid source URL {url!r}")
    if "://" not in url:
        if url.startswith("/"):
            return url
        raise SourceError(
            f"unsupported source URL {url!r}: ssh/scp syntax (user@host:path) is not "
            "supported in Phase A; use a plain https:// URL"
        )
    parts = urlsplit(url)
    if parts.scheme == "https":
        if parts.username or parts.password:
            raise SourceError(
                f"source URL for host {parts.hostname!r} carries credentials; "
                "refusing (put the secret in the SecretStore, not the URL)"
            )
        if not parts.hostname:
            raise SourceError(f"invalid https source URL {url!r}: no host")
        return url
    if parts.scheme == "file":
        return url
    raise SourceError(
        f"unsupported source URL scheme {parts.scheme!r}; Phase A supports https:// only "
        "(ssh:// and git:// are rejected deliberately)"
    )


def _read_marker(path: Path) -> str | None:
    """The sha a staged tree claims to hold, or ``None`` if it cannot be read."""
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None


class SourceMirror:
    """One upstream repository, mirrored and materialised on the reflink store."""

    def __init__(self, store_root: Path, name: str = "api", *, url: str) -> None:
        if not _NAME_RE.match(name):
            raise SourceError(f"invalid mirror name {name!r}")
        self.store_root = Path(store_root)
        self.name = name
        self.url = validate_url(url)
        self._git_exe = _which("git")
        self._tar_exe = _which("tar")

    # ----------------------------------------------------------------- layout

    @property
    def mirror_dir(self) -> Path:
        return self.store_root / "repos" / f"{self.name}.git"

    @property
    def src_dir(self) -> Path:
        return self.store_root / "src" / self.name

    @property
    def index_path(self) -> Path:
        return self.src_dir / _INDEX_NAME

    def checkout_dir(self, sha: str) -> Path:
        return self.src_dir / _check_sha(sha)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"SourceMirror(name={self.name!r}, store_root={str(self.store_root)!r})"

    # ------------------------------------------------------------------- git

    def _git(self, args: Sequence[str], *, what: str, timeout_s: float) -> str:
        argv = [self._git_exe, *args]
        started = time.monotonic()
        try:
            proc = subprocess.run(  # noqa: S603 - argv list, never a shell string
                argv,
                capture_output=True,
                timeout=timeout_s,
                env=_git_env(),
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            log.warning("%s: timed out after %.0fs: %s", what, timeout_s, " ".join(argv))
            raise SourceError(f"{what}: timed out after {timeout_s:.0f}s: {' '.join(argv)}") from e
        except OSError as e:
            raise SourceError(f"{what}: could not run {' '.join(argv)}: {e}") from e
        elapsed = time.monotonic() - started
        log.info("%s: %s -> rc=%d in %.1fs", what, " ".join(argv), proc.returncode, elapsed)
        if proc.returncode != 0:
            stderr = _tail(proc.stderr.decode(errors="replace"))
            raise SourceError(
                f"{what} failed (rc={proc.returncode}): {' '.join(argv)}\n{stderr or '(no stderr)'}"
            )
        return proc.stdout.decode(errors="replace").strip()

    def _ensure_mirror(self, timeout_s: float) -> None:
        """Clone the bare mirror if it is missing. Atomic: clone aside, rename."""
        if (self.mirror_dir / "HEAD").exists():
            return
        self.mirror_dir.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.mirror_dir.with_name(f".{self.mirror_dir.name}.tmp{os.getpid()}")
        if tmp.exists():
            shutil.rmtree(tmp)
        try:
            self._git(
                ["clone", "--mirror", "--quiet", self.url, str(tmp)],
                what=f"{self.name}: git clone --mirror",
                timeout_s=timeout_s,
            )
            try:
                os.rename(tmp, self.mirror_dir)
            except OSError:
                # Another process won the race; its mirror is as good as ours.
                if not (self.mirror_dir / "HEAD").exists():
                    raise
        finally:
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)
        log.info("%s: mirror created at %s", self.name, self.mirror_dir)

    def fetch(self, ref: str = "main", timeout_s: float = DEFAULT_FETCH_TIMEOUT_S) -> str:
        """Update the mirror and resolve ``refs/heads/<ref>`` to a commit sha."""
        if not _REF_RE.match(ref):
            raise SourceError(f"invalid ref name {ref!r}")
        self._ensure_mirror(timeout_s)
        self._git(
            ["--git-dir", str(self.mirror_dir), "fetch", "--prune", "--quiet", "origin"],
            what=f"{self.name}: git fetch",
            timeout_s=timeout_s,
        )
        try:
            sha = self._git(
                ["--git-dir", str(self.mirror_dir), "rev-parse", "--verify", f"refs/heads/{ref}"],
                what=f"{self.name}: git rev-parse {ref}",
                timeout_s=timeout_s,
            )
        except SourceError as e:
            raise SourceError(f"{self.name}: no such branch {ref!r} in the mirror") from e
        if not _SHA_RE.match(sha):
            raise SourceError(f"{self.name}: rev-parse returned {sha!r}, not a commit id")
        log.info("%s: %s -> %s", self.name, ref, sha)
        return sha

    def changed_paths(
        self, old_sha: str, new_sha: str, timeout_s: float = DEFAULT_FETCH_TIMEOUT_S
    ) -> list[str]:
        """Repo-relative paths that differ between two commits, sorted.

        ``git diff --name-only <old> <new>`` against the bare mirror. This is
        what lets the sync loop redeploy only the services a commit actually
        touched (D26) instead of the whole fleet.

        ``--no-renames`` on purpose: with rename detection (git's default since
        2.9) a file moved from one service's directory to another's is reported
        under the destination only, and the service that *lost* it would not be
        seen as changed. Listing both sides costs nothing and cannot under-report.

        Raises :class:`SourceError` if either commit is unknown to the mirror --
        which is a real answer, not a detail to swallow: a caller that cannot
        diff must treat every service as affected rather than assume none is.
        """
        old = _check_sha(old_sha)
        new = _check_sha(new_sha)
        if old == new:
            return []
        out = self._git(
            [
                "--git-dir",
                str(self.mirror_dir),
                "diff",
                "--name-only",
                "--no-renames",
                old,
                new,
            ],
            what=f"{self.name}: git diff {old[:12]}..{new[:12]}",
            timeout_s=timeout_s,
        )
        paths = sorted({line.strip() for line in out.splitlines() if line.strip()})
        log.info("%s: %s..%s touched %d path(s)", self.name, old[:12], new[:12], len(paths))
        return paths

    # ------------------------------------------------------------- materialise

    def materialize(self, sha: str, *, timeout_s: float = DEFAULT_ARCHIVE_TIMEOUT_S) -> Path:
        """Extract ``sha`` into ``<store>/src/<name>/<sha>/``. Idempotent, atomic.

        The tree is built in a sibling ``.<sha>.tmpNNN`` directory and renamed
        into place, so a crashed extraction can never be mistaken for a complete
        checkout by a later ``stage``.
        """
        sha = _check_sha(sha)
        dest = self.src_dir / sha
        if dest.is_dir():
            self._touch_index(sha)
            log.debug("%s: %s already materialized at %s", self.name, sha, dest)
            return dest
        if not (self.mirror_dir / "HEAD").exists():
            raise SourceError(f"{self.name}: no mirror at {self.mirror_dir}; call fetch() first")

        self.src_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.src_dir / f".{sha}.tmp{os.getpid()}"
        if tmp.exists():
            shutil.rmtree(tmp)
        tmp.mkdir(parents=True)
        try:
            self._archive_into(sha, tmp, timeout_s)
            try:
                os.rename(tmp, dest)
            except OSError:
                if not dest.is_dir():
                    raise
                log.info("%s: %s materialized concurrently; keeping the winner", self.name, sha)
        finally:
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)
        self._touch_index(sha)
        log.info("%s: materialized %s at %s", self.name, sha, dest)
        return dest

    def _archive_into(self, sha: str, dest: Path, timeout_s: float) -> None:
        """``git archive <sha> | tar -x -f - -C <dest>``: two Popen, no shell.

        Both children write stderr to temporary files rather than pipes: with
        pipes, a tool that produces more than one pipe buffer of diagnostics
        while we are blocked in ``communicate`` on the *other* child deadlocks.
        """
        git_argv = [
            self._git_exe,
            "--git-dir",
            str(self.mirror_dir),
            "archive",
            "--format=tar",
            sha,
        ]
        tar_argv = [self._tar_exe, "-x", "-f", "-", "-C", str(dest)]
        started = time.monotonic()
        with tempfile.TemporaryFile() as git_err, tempfile.TemporaryFile() as tar_err:
            git_proc = subprocess.Popen(  # noqa: S603 - argv list, never a shell string
                git_argv, stdout=subprocess.PIPE, stderr=git_err, env=_git_env()
            )
            try:
                assert git_proc.stdout is not None
                tar_proc = subprocess.Popen(  # noqa: S603 - argv list, never a shell string
                    tar_argv,
                    stdin=git_proc.stdout,
                    stdout=subprocess.DEVNULL,
                    stderr=tar_err,
                    env={"PATH": _TOOL_PATH, "LC_ALL": "C"},
                )
            finally:
                # The parent's copy of the pipe must go, or tar never sees EOF.
                if git_proc.stdout is not None:
                    git_proc.stdout.close()
            try:
                tar_rc = tar_proc.wait(timeout=timeout_s)
                git_rc = git_proc.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired as e:
                for proc in (tar_proc, git_proc):
                    proc.kill()
                    proc.wait()
                raise SourceError(
                    f"{self.name}: git archive|tar for {sha} timed out after {timeout_s:.0f}s"
                ) from e
            elapsed = time.monotonic() - started
            log.info(
                "%s: %s | %s -> rc=%d,%d in %.1fs",
                self.name,
                " ".join(git_argv),
                " ".join(tar_argv),
                git_rc,
                tar_rc,
                elapsed,
            )
            if git_rc != 0 or tar_rc != 0:
                raise SourceError(
                    f"{self.name}: git archive|tar for {sha} failed "
                    f"(git rc={git_rc}, tar rc={tar_rc})\n"
                    f"{_tail(_read_temp(git_err) + _read_temp(tar_err)) or '(no stderr)'}"
                )

    # ------------------------------------------------------------------ stage

    def stage(
        self,
        sha: str,
        service_root: Path,
        block: UidBlock,
        *,
        dest: str = _REPO_DIRNAME,
        harness_uid: int | None = None,
        harness_gid: int | None = None,
        timeout_s: float = DEFAULT_STAGE_TIMEOUT_S,
    ) -> Path:
        """Put the tree for ``sha`` at ``<service_root>/<dest>``, owned by ``block``.

        ``dest`` is a plain relative path under the root: ``repo`` (the default,
        every translated service) or ``releases/<sha>`` (core mode). Missing
        parent directories are created through the admin namespace and chowned
        to the service -- one level at a time, never ``-R``: a parent that
        already exists may hold other releases, and walking them is exactly the
        O(files) chown ``ensure_service_root`` is careful to avoid.

        A no-op when ``<dest>/.ams-sha`` already names ``sha``, which is the
        common case on every sync tick. Otherwise the new tree is built beside
        the old one and swapped in with two ``mv``s, so a service root is never
        left holding a half-copied checkout.
        """
        sha = _check_sha(sha)
        rel = _check_dest(dest)
        service_root = Path(service_root)
        if not service_root.is_absolute():
            raise ValueError(f"service root must be absolute, got {service_root}")
        repo = service_root.joinpath(*rel)
        if _read_marker(repo / SHA_MARKER) == sha:
            log.info("%s: %s already at %s; stage is a no-op", self.name, repo, sha)
            return repo

        canonical = self.materialize(sha)
        new = repo.with_name(repo.name + _NEW_SUFFIX)
        old = repo.with_name(repo.name + _OLD_SUFFIX)
        if not service_root.exists():
            ensure_service_root(
                service_root, block, harness_uid=harness_uid, harness_gid=harness_gid
            )

        admin_kwargs = {
            "block": block,
            "harness_uid": harness_uid,
            "harness_gid": harness_gid,
            "timeout_s": timeout_s,
        }
        # Parents between the root and the tree ("releases/"). For the default
        # dest there are none, so the argv sequence is exactly the pre-1.1 one.
        missing = [p for p in _between(service_root, repo.parent) if not p.exists()]
        if missing:
            log.info(
                "%s: creating %s under %s for the block",
                self.name,
                ", ".join(str(p.relative_to(service_root)) for p in missing),
                service_root,
            )
            self._admin(
                ["mkdir", "-p", str(repo.parent)], what="create stage parents", **admin_kwargs
            )
            self._admin(
                ["chown", f"{INNER_UID}:{INNER_GID}", *(str(p) for p in missing)],
                what="chown stage parents",
                **admin_kwargs,
            )
        with tempfile.TemporaryDirectory() as td:
            # The marker cannot be written directly: <root>/repo.new belongs to
            # the service uid the moment it is chowned, and belongs to inner
            # root before that -- either way the harness has no write access.
            # Inner root *is* the harness uid, so it can read this staging file.
            os.chmod(td, 0o755)
            marker_src = Path(td) / SHA_MARKER
            marker_src.write_text(sha + "\n", encoding="utf-8")
            os.chmod(marker_src, 0o644)
            self._admin(["rm", "-rf", str(new), str(old)], what="clear stage dirs", **admin_kwargs)
            self._admin(
                ["cp", *STAGE_CP_FLAGS, str(canonical), str(new)],
                what=f"reflink copy {sha}",
                **admin_kwargs,
            )
            self._admin(
                ["cp", str(marker_src), str(new / SHA_MARKER)],
                what="write .ams-sha",
                **admin_kwargs,
            )
            self._admin(
                ["chown", "-R", f"{INNER_UID}:{INNER_GID}", str(new)],
                what="chown to the service",
                **admin_kwargs,
            )
        had_old = repo.exists()
        if had_old:
            self._admin(["mv", str(repo), str(old)], what="retire the old repo", **admin_kwargs)
        self._admin(["mv", str(new), str(repo)], what="swap in the new repo", **admin_kwargs)
        if had_old:
            self._admin(["rm", "-rf", str(old)], what="drop the old repo", **admin_kwargs)
        self._touch_index(sha)
        log.info("%s: staged %s into %s", self.name, sha, repo)
        return repo

    def stage_plain(
        self,
        sha: str,
        dest_dir: Path,
        *,
        timeout_s: float = DEFAULT_STAGE_TIMEOUT_S,
    ) -> Path:
        """Put the tree for ``sha`` at ``dest_dir`` as the current user, no namespace.

        The dev / ``--no-isolation`` twin of :meth:`stage`: same marker, same
        build-beside-then-swap, but plain ``cp`` and ``os.rename`` because the
        destination is ours to write. macOS has no user namespace at all, which
        is the main reason this exists.

        ``cp -a --reflink=auto`` is tried first (GNU cp; shares extents on XFS
        and btrfs), then plain ``cp -a`` -- the stock macOS ``cp`` rejects the
        flag with "illegal option". A copy that fails both ways raises and leaves
        neither ``dest_dir`` nor its ``.new`` sibling behind.
        """
        sha = _check_sha(sha)
        dest_dir = Path(dest_dir)
        if not dest_dir.is_absolute():
            raise ValueError(f"stage_plain destination must be absolute, got {dest_dir}")
        if _read_marker(dest_dir / SHA_MARKER) == sha:
            log.info("%s: %s already at %s; stage_plain is a no-op", self.name, dest_dir, sha)
            return dest_dir

        canonical = self.materialize(sha)
        new = dest_dir.with_name(dest_dir.name + _NEW_SUFFIX)
        old = dest_dir.with_name(dest_dir.name + _OLD_SUFFIX)
        dest_dir.parent.mkdir(parents=True, exist_ok=True)
        for leftover in (new, old):
            if leftover.exists() or leftover.is_symlink():
                log.info("%s: removing leftover %s from an interrupted stage", self.name, leftover)
                _remove(leftover)
        try:
            self._copy_tree(canonical, new, timeout_s)
            (new / SHA_MARKER).write_text(sha + "\n", encoding="utf-8")
        except BaseException:
            _remove(new)
            raise
        had_old = dest_dir.exists() or dest_dir.is_symlink()
        if had_old:
            os.rename(dest_dir, old)
        os.rename(new, dest_dir)
        if had_old:
            _remove(old)
        self._touch_index(sha)
        log.info("%s: staged %s into %s (plain, no namespace)", self.name, sha, dest_dir)
        return dest_dir

    def _copy_tree(self, src: Path, dst: Path, timeout_s: float) -> None:
        """``cp -a`` ``src`` to a not-yet-existing ``dst``, reflinking where cp can."""
        cp = _which("cp")
        attempts = (
            [cp, "-a", "--reflink=auto", str(src), str(dst)],
            [cp, "-a", str(src), str(dst)],
        )
        failures: list[str] = []
        for argv in attempts:
            started = time.monotonic()
            try:
                proc = subprocess.run(  # noqa: S603 - argv list, never a shell string
                    argv,
                    capture_output=True,
                    timeout=timeout_s,
                    env={"PATH": _TOOL_PATH, "LC_ALL": "C"},
                    check=False,
                )
            except subprocess.TimeoutExpired as e:
                _remove(dst)
                raise SourceError(
                    f"{self.name}: copy timed out after {timeout_s:.0f}s: {' '.join(argv)}"
                ) from e
            except OSError as e:
                raise SourceError(f"{self.name}: could not run {' '.join(argv)}: {e}") from e
            log.info(
                "%s: %s -> rc=%d in %.1fs",
                self.name,
                " ".join(argv),
                proc.returncode,
                time.monotonic() - started,
            )
            if proc.returncode == 0:
                return
            stderr = _tail(proc.stderr.decode(errors="replace")) or "(no stderr)"
            failures.append(f"{' '.join(argv)} (rc={proc.returncode}): {stderr}")
            log.warning("%s: copy attempt failed, %s", self.name, failures[-1])
            _remove(dst)
        raise SourceError(f"{self.name}: copying {src} failed:\n" + "\n".join(failures))

    def _admin(
        self,
        argv: Sequence[str],
        *,
        block: UidBlock,
        what: str,
        harness_uid: int | None = None,
        harness_gid: int | None = None,
        timeout_s: float = DEFAULT_STAGE_TIMEOUT_S,
    ) -> AdminResult:
        started = time.monotonic()
        result = run_admin(
            list(argv),
            block,
            harness_uid=harness_uid,
            harness_gid=harness_gid,
            timeout_s=timeout_s,
        )
        elapsed = time.monotonic() - started
        log.info("%s: %s -> rc=%d in %.1fs", what, " ".join(argv), result.returncode, elapsed)
        if not result.ok:
            raise SourceError(
                f"{self.name}: {what} failed (rc={result.returncode}): {' '.join(argv)}\n"
                f"{_tail(result.stderr.decode(errors='replace')) or '(no stderr)'}"
            )
        return result

    # --------------------------------------------------------------------- gc

    def _read_index(self) -> dict[str, float]:
        """Last-use timestamps per sha.

        A corrupt index is a *cache* of usage hints, not allocator state: losing
        it degrades ``gc`` to directory mtimes, which is why this warns and
        continues instead of raising ``StateCorrupt`` the way the uid/port
        allocators do. Nothing here can hand out a resource twice.
        """
        try:
            raw = self.index_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as e:
            log.warning("%s: cannot read %s: %s", self.name, self.index_path, e)
            return {}
        try:
            data = json.loads(raw)
            used = data["used"]
            return {str(k): float(v) for k, v in used.items()}
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            log.warning("%s: ignoring corrupt %s: %s", self.name, self.index_path, e)
            return {}

    def _write_index(self, used: dict[str, float]) -> None:
        write_json_atomic(self.index_path, {"version": 1, "used": used})

    def _touch_index(self, sha: str) -> None:
        used = self._read_index()
        used[sha] = time.time()
        self._write_index(used)

    def gc(self, keep: int = 2) -> list[str]:
        """Delete all but the ``keep`` most recently used canonical checkouts.

        Returns the shas removed. Stale ``.<sha>.tmpNNN`` directories from a
        crashed extraction are swept at the same time.
        """
        if keep < 0:
            raise ValueError(f"keep must be >= 0, got {keep}")
        if not self.src_dir.is_dir():
            return []
        used = self._read_index()
        trees = [p for p in self.src_dir.iterdir() if p.is_dir() and _SHA_RE.match(p.name)]
        ranked = sorted(
            trees,
            key=lambda p: (used.get(p.name, p.stat().st_mtime), p.name),
            reverse=True,
        )
        removed: list[str] = []
        for tree in ranked[keep:]:
            log.info("%s: gc removing %s", self.name, tree)
            shutil.rmtree(tree, ignore_errors=True)
            removed.append(tree.name)
        for stale in self.src_dir.glob(".*.tmp*"):
            if stale.is_dir():
                log.info("%s: gc removing stale partial %s", self.name, stale)
                shutil.rmtree(stale, ignore_errors=True)
        kept = {p.name for p in ranked[:keep]}
        self._write_index({k: v for k, v in used.items() if k in kept})
        return removed


def _check_sha(sha: str) -> str:
    if not isinstance(sha, str) or not _SHA_RE.match(sha):
        raise SourceError(f"invalid commit sha {sha!r}")
    return sha


def _check_dest(dest: str) -> tuple[str, ...]:
    """Split a stage destination into components, refusing anything but a plain
    relative path. Split on "/" by hand rather than through ``PurePosixPath``,
    which would quietly normalise ``a//b`` and ``./a`` into something valid."""
    if not isinstance(dest, str) or not dest:
        raise SourceError(f"invalid stage dest {dest!r}: must be a non-empty relative path")
    parts = tuple(dest.split("/"))
    bad = [p for p in parts if not _DEST_PART_RE.fullmatch(p)]
    if dest.startswith("/") or bad:
        raise SourceError(
            f"invalid stage dest {dest!r}: must be a relative path of plain components "
            f"(no '..', '.', empty or option-like parts; offending: {bad or [dest]})"
        )
    return parts


def _between(root: Path, parent: Path) -> list[Path]:
    """Directories strictly below ``root`` down to and including ``parent``,
    outermost first; empty when ``parent`` is ``root`` itself."""
    out: list[Path] = []
    cur = parent
    while cur != root:
        out.append(cur)
        cur = cur.parent
    return out[::-1]


def _remove(path: Path) -> None:
    """Remove a file, symlink or tree we own; missing is fine."""
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path)


def _read_temp(fh: IO[bytes]) -> str:
    """Read a ``tempfile.TemporaryFile`` used as a child's stderr, from the top."""
    try:
        fh.seek(0)
        return fh.read().decode(errors="replace")
    except OSError:  # pragma: no cover - defensive
        return ""
