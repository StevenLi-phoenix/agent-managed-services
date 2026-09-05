"""User-namespace primitives: the fork/map handshake and the admin namespace.

Why a manual handshake instead of ``unshare(1)`` (DECISIONS D2): util-linux
writes ``uid_map`` directly and silently ignores ``newuidmap`` for ranges the
caller does not own, and ``--map-auto`` maps the whole subuid range as one
namespace-wide identity. Neither gives per-service 1024-uid blocks.

The sequence, all of it verified on the target host:

    child : unshare(CLONE_NEWUSER) -> write 1 byte to `sync` -> block on `ack`
    parent: read `sync` -> newuidmap/newgidmap <child pid> ... -> write `ack`
    child : setgroups([]) -> setresgid/setresuid(inner) -> PR_SET_NO_NEW_PRIVS -> exec

The child never returns into Python. Any child-side failure is reported to the
parent over a close-on-exec error pipe (the trick ``subprocess`` uses: a
successful ``exec`` closes the pipe, so EOF-with-no-bytes means "it worked")
and the child then ``_exit(127)``.

Two map layouts exist (DECISIONS D4):

- runtime: inner 1000..2023 <- the service's host block. The harness uid is
  deliberately absent, so harness-private files are unreadable from inside.
- admin: inner 0 <- harness uid, plus the block at inner 1000. Only the harness
  uses this, to chown/rm files that the host sees as owned by block uids.
"""

from __future__ import annotations

import contextlib
import ctypes
import logging
import os
import select
import shutil
import signal
import stat
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from ams.spawn import DATA_DIRNAME, INNER_GID, INNER_UID
from ams.uidmap import UidBlock

log = logging.getLogger("ams.userns")

CLONE_NEWUSER = 0x10000000

# Mount-namespace masking (issue #1): run_admin can hide harness-private paths
# from the process it forks, so untrusted code executed under the admin map
# (static builds, provisioning tools) cannot read what the harness uid can.
CLONE_NEWNS = 0x00020000
MS_RDONLY = 1
MS_NOSUID = 2
MS_NODEV = 4
MS_BIND = 4096
MS_REC = 16384
MS_PRIVATE = 1 << 20

PR_SET_PDEATHSIG = 1
PR_GET_PDEATHSIG = 2
PR_SET_CHILD_SUBREAPER = 36
PR_GET_CHILD_SUBREAPER = 37
PR_SET_NO_NEW_PRIVS = 38

_ACK_OK = b"\x01"
_ACK_FAIL = b"\x00"
_CHILD_EXIT_FAILED = 127

# Environment handed to the admin helpers (chown/rm/mkdir). Nothing inherited.
_ADMIN_ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LC_ALL": "C"}

# remove_service_root refuses shallower paths than this. Path("/a/b").parts is
# ("/", "a", "b"), so 3 means "at least one directory below a top-level dir".
_MIN_REMOVE_PARTS = 3


class SpawnError(RuntimeError):
    """A service could not be started (fork, map, or exec failed)."""


class MapError(RuntimeError):
    """``newuidmap``/``newgidmap`` refused to write the map."""


_libc = ctypes.CDLL(None, use_errno=True)


def _check(rc: int, what: str) -> None:
    if rc != 0:
        err = ctypes.get_errno()
        raise OSError(err, f"{what} failed: {os.strerror(err)}")


def unshare_user() -> None:
    """Enter a new (unmapped) user namespace. Caller must get maps written."""
    _check(_libc.unshare(CLONE_NEWUSER), "unshare(CLONE_NEWUSER)")


def unshare_mounts() -> None:
    """Enter a new mount namespace. Requires CAP_SYS_ADMIN in the current user
    namespace -- true for inner root after the identity drop."""
    _check(_libc.unshare(CLONE_NEWNS), "unshare(CLONE_NEWNS)")


def _mount(
    source: bytes | None, target: bytes, fstype: bytes | None, flags: int, data: bytes | None
) -> None:
    _check(_libc.mount(source, target, fstype, flags, data), f"mount {target!r}")


def _apply_mask(paths: Sequence[Path]) -> None:
    """Overmount every entry in ``paths`` empty, inside a private mount ns.

    Runs in the forked admin child *after* the identity drop: as inner root it
    holds CAP_SYS_ADMIN over its own namespaces, and every mount dies with the
    process. ``/`` is made private first -- systemd marks it shared, and a
    shared peer group would propagate the masks back into the host namespace.

    Directories are covered with an empty read-only tmpfs; anything else
    (files, sockets) gets ``/dev/null`` bound over it. A path that does not
    exist is skipped so callers can pass a fixed layout. Entries must not nest:
    masking a parent after a child would hide the child's mask, not the subtree.
    """
    unshare_mounts()
    _mount(None, b"/", None, MS_PRIVATE | MS_REC, None)
    for p in paths:
        try:
            st = os.stat(p)
        except OSError:
            continue
        target = os.fsencode(p)
        if stat.S_ISDIR(st.st_mode):
            _mount(
                b"tmpfs",
                target,
                b"tmpfs",
                MS_RDONLY | MS_NOSUID | MS_NODEV,
                b"size=4096,mode=000",
            )
        else:
            _mount(b"/dev/null", target, None, MS_BIND | MS_RDONLY, None)


def _prctl(option: int, arg2: int = 0, arg3: int = 0, arg4: int = 0, arg5: int = 0) -> None:
    _check(_libc.prctl(option, arg2, arg3, arg4, arg5), f"prctl({option})")


def set_no_new_privs() -> None:
    """Make setuid/setcap binaries unable to raise privileges for this tree."""
    _prctl(PR_SET_NO_NEW_PRIVS, 1)


def set_child_subreaper(enable: bool = True) -> None:
    """Inherit orphaned descendants so the harness can reap them."""
    _prctl(PR_SET_CHILD_SUBREAPER, 1 if enable else 0)


def is_subreaper() -> bool:
    out = ctypes.c_int(0)
    rc = _libc.prctl(PR_GET_CHILD_SUBREAPER, ctypes.byref(out), 0, 0, 0)
    _check(rc, "prctl(PR_GET_CHILD_SUBREAPER)")
    return out.value != 0


def set_pdeathsig(sig: int = signal.SIGKILL) -> None:
    """Ask the kernel to signal us when our parent dies.

    Cleared across ``exec`` of a setuid binary and on uid change, so a service
    cannot rely on it; the cgroup is what actually guarantees teardown.
    """
    _prctl(PR_SET_PDEATHSIG, int(sig))


def get_pdeathsig() -> int:
    out = ctypes.c_int(0)
    _check(_libc.prctl(PR_GET_PDEATHSIG, ctypes.byref(out), 0, 0, 0), "prctl(PR_GET_PDEATHSIG)")
    return out.value


# --------------------------------------------------------------------------- maps


def write_maps(
    pid: int,
    uid_args: Sequence[str],
    gid_args: Sequence[str],
    newuidmap: str = "newuidmap",
    newgidmap: str = "newgidmap",
) -> None:
    """Run the setuid helpers to install the child's uid/gid maps.

    These must be the setuid-root helpers from the ``uidmap`` package: writing
    ``/proc/<pid>/uid_map`` ourselves can only ever map our own single uid.
    """
    for tool, args in ((newgidmap, gid_args), (newuidmap, uid_args)):
        cmd = [tool, str(pid), *args]
        proc = subprocess.run(cmd, capture_output=True, check=False)
        if proc.returncode != 0:
            raise MapError(
                f"{' '.join(cmd)} exited {proc.returncode}: "
                f"{proc.stderr.decode(errors='replace').strip() or '(no stderr)'}"
            )
    log.debug("maps written for pid %d: uid=%s gid=%s", pid, list(uid_args), list(gid_args))


# --------------------------------------------------------------------------- fork


def _report_and_die(err_w: int, exc: BaseException) -> NoReturn:
    """Last thing a failed child does. Never raises, never returns."""
    try:
        msg = f"{type(exc).__name__}: {exc}".encode(errors="replace")
        os.write(err_w, msg[:4000])
    except BaseException:  # nothing useful is left to do
        pass
    os._exit(_CHILD_EXIT_FAILED)


def _reap(pid: int) -> int:
    try:
        _, status = os.waitpid(pid, 0)
    except ChildProcessError:
        return -1
    return status


def fork_in_userns(
    uid_args: Sequence[str],
    gid_args: Sequence[str],
    inner_uid: int,
    inner_gid: int,
    child_fn: Callable[[], NoReturn],
    *,
    pre_unshare: Callable[[int], None] | None = None,
    newuidmap: str = "newuidmap",
    newgidmap: str = "newgidmap",
) -> int:
    """Fork a child into a mapped user namespace and hand it to ``child_fn``.

    ``pre_unshare`` runs in the child, with the child's pid, *before* the
    namespace exists, while it is still an ordinary child of the harness. That
    is the only race-free place to put the pid into its cgroup. Keep it tiny:
    after the fork the child must not touch the logging module or any lock the
    parent might have held.

    ``child_fn`` runs after the identity drop and must ``exec``; if it returns,
    that is reported as an error rather than letting a forked copy of the
    harness escape.
    """
    sync_r, sync_w = os.pipe()
    ack_r, ack_w = os.pipe()
    err_r, err_w = os.pipe()  # close-on-exec: EOF with no bytes == exec succeeded

    pid = os.fork()
    if pid == 0:  # ---------------------------------------------------- child
        try:
            os.close(sync_r)
            os.close(ack_w)
            os.close(err_r)
            if pre_unshare is not None:
                pre_unshare(os.getpid())
            unshare_user()
            os.write(sync_w, b"\x01")
            os.close(sync_w)
            if os.read(ack_r, 1) != _ACK_OK:
                raise SpawnError("parent could not write the uid/gid maps")
            os.close(ack_r)
            os.setgroups([])
            os.setresgid(inner_gid, inner_gid, inner_gid)
            os.setresuid(inner_uid, inner_uid, inner_uid)
            set_no_new_privs()
            child_fn()
            raise SpawnError("child_fn returned without exec()")
        except BaseException as exc:  # must never unwind past here
            _report_and_die(err_w, exc)

    # ------------------------------------------------------------------ parent
    os.close(sync_w)
    os.close(ack_r)
    os.close(err_w)
    parent_err: Exception | None = None
    try:
        started = os.read(sync_r, 1) == b"\x01"
    except OSError as e:
        started, parent_err = False, e
    os.close(sync_r)

    if started:
        try:
            write_maps(pid, uid_args, gid_args, newuidmap, newgidmap)
        except (MapError, OSError) as e:
            started, parent_err = False, e
    try:
        os.write(ack_w, _ACK_OK if started else _ACK_FAIL)
    except OSError:
        pass
    os.close(ack_w)

    child_msg = b""
    try:
        while chunk := os.read(err_r, 4096):
            child_msg += chunk
    except OSError:
        pass
    os.close(err_r)

    if child_msg or not started:
        _reap(pid)
        details = [d for d in (child_msg.decode(errors="replace").strip(), str(parent_err)) if d]
        raise SpawnError("; ".join(details) or "child exited before exec") from parent_err
    return pid


# --------------------------------------------------------------------------- admin ns


@dataclass(frozen=True)
class AdminResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: bytes

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def check(self) -> AdminResult:
        if not self.ok:
            raise SpawnError(
                f"{' '.join(self.argv)} exited {self.returncode}: "
                f"{self.stderr.decode(errors='replace').strip() or '(no stderr)'}"
            )
        return self


def admin_map_args(
    block: UidBlock, harness_uid: int, harness_gid: int
) -> tuple[list[str], list[str]]:
    """Admin map: inner 0 <- the harness uid, inner 1000 <- the service block."""
    return (
        ["0", str(harness_uid), "1", str(INNER_UID), str(block.uid_start), str(block.size)],
        ["0", str(harness_gid), "1", str(INNER_GID), str(block.gid_start), str(block.size)],
    )


def _drain(fds: dict[int, bytearray], deadline: float) -> None:
    open_fds = set(fds)
    while open_fds:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        ready, _, _ = select.select(list(open_fds), [], [], min(remaining, 0.5))
        for fd in ready:
            chunk = os.read(fd, 65536)
            if not chunk:
                open_fds.discard(fd)
            else:
                fds[fd] += chunk


def run_admin(
    argv: Sequence[str],
    block: UidBlock,
    *,
    harness_uid: int | None = None,
    harness_gid: int | None = None,
    timeout_s: float = 60.0,
    mask: Sequence[Path] = (),
) -> AdminResult:
    """Run ``argv`` as inner root in an admin-mapped user namespace.

    This is how the harness touches files the host reports as owned by
    ``block.uid_start``: as inner root it holds CAP_DAC_OVERRIDE/CAP_CHOWN over
    exactly the uids mapped into this namespace and nothing else.

    ``mask`` hides harness-private paths from the forked process: each entry is
    overmounted empty in a private mount namespace before exec (issue #1).
    Harness ops (chown/rm/cp) must not pass it -- they need the real files --
    but every run of *untrusted* code under the admin map (static builds,
    provisioning tools) must, or the harness uid's read access becomes the
    build's read access.
    """
    if not argv:
        raise ValueError("argv must not be empty")
    exe = shutil.which(argv[0], path=_ADMIN_ENV["PATH"])
    if exe is None:
        raise SpawnError(f"admin helper {argv[0]!r} not found on {_ADMIN_ENV['PATH']}")
    uid = os.getuid() if harness_uid is None else harness_uid
    gid = os.getgid() if harness_gid is None else harness_gid
    uid_args, gid_args = admin_map_args(block, uid, gid)

    out_r, out_w = os.pipe()
    err_r, err_w = os.pipe()
    argv = list(argv)

    def child() -> NoReturn:
        devnull = os.open(os.devnull, os.O_RDONLY)
        os.dup2(devnull, 0)
        os.dup2(out_w, 1)
        os.dup2(err_w, 2)
        for fd in (devnull, out_w, err_w):
            if fd > 2:
                os.close(fd)
        if mask:
            _apply_mask(mask)
        os.execve(exe, argv, dict(_ADMIN_ENV))
        raise SpawnError("execve returned")  # pragma: no cover

    try:
        pid = fork_in_userns(uid_args, gid_args, 0, 0, child)
    except BaseException:
        for fd in (out_r, err_r):
            os.close(fd)
        raise
    finally:
        os.close(out_w)
        os.close(err_w)

    buffers = {out_r: bytearray(), err_r: bytearray()}
    try:
        _drain(buffers, time.monotonic() + timeout_s)
    finally:
        os.close(out_r)
        os.close(err_r)
    status = _wait_with_timeout(pid, timeout_s)
    rc = os.waitstatus_to_exitcode(status) if status is not None else -signal.SIGKILL
    result = AdminResult(tuple(argv), rc, bytes(buffers[out_r]), bytes(buffers[err_r]))
    log.debug("admin %s -> rc=%d", " ".join(argv), rc)
    return result


def _wait_with_timeout(pid: int, timeout_s: float) -> int | None:
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            done, status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return None
        if done == pid:
            return status
        if time.monotonic() >= deadline:
            log.warning("admin child %d exceeded %.1fs; killing", pid, timeout_s)
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
            return _reap(pid)
        time.sleep(0.01)


# --------------------------------------------------------------------------- roots


def ensure_service_root(
    root: Path,
    block: UidBlock,
    *,
    subdirs: Sequence[Path] = (),
    harness_uid: int | None = None,
    harness_gid: int | None = None,
) -> None:
    """Create the service root (and ``subdirs``) and give it to the service uid.

    ``<root>/data`` (``AMS_DATA_DIR``) is always part of the root: it is the one
    directory a service may treat as persistent, so it must exist before the
    first start rather than after the first service learns to mkdir it. Mode
    0750 -- inside the namespace only the service uid exists, and on the host it
    keeps a service's databases out of a `find`-able world-readable tree.

    The harness can only create the top directory: once it is chowned to the
    block, the harness has no write access to it, so everything below is made
    through the admin namespace.

    Called before every spawn, so the common case -- an existing root already
    owned by the block -- must be cheap. It costs one ``stat`` and no fork. The
    recursive ``chown`` runs only when something was actually created or the
    ownership is wrong; running it unconditionally is O(files under the root),
    which once a ``.venv``/``node_modules`` lives there means every restart
    walks tens of thousands of inodes inside a fresh user namespace.
    """
    root = Path(root)
    if not root.is_absolute():
        raise ValueError(f"service root must be absolute, got {root}")
    for sub in subdirs:
        sub = Path(sub)
        if not sub.is_absolute() or (root != sub and root not in sub.parents):
            raise ValueError(f"{sub} is not inside the service root {root}")

    created = not root.exists()
    if created:
        root.mkdir(parents=True, mode=0o755)
        log.info("created service root %s", root)
    # While the root is still ours the data dir costs no fork; the admin ns is
    # only needed for an existing root that has already been handed over (an
    # upgrade of a service created before AMS_DATA_DIR existed). Doing it the
    # cheap way keeps the "warm path forks nothing" invariant intact.
    data_dir = root / DATA_DIRNAME
    made_data = not data_dir.exists()
    if made_data:
        if os.stat(root).st_uid == (os.getuid() if harness_uid is None else harness_uid):
            data_dir.mkdir(mode=0o750)
            data_dir.chmod(0o750)  # mkdir's mode is masked by the umask
        else:
            run_admin(
                ["mkdir", "-m", "750", "-p", str(data_dir)],
                block,
                harness_uid=harness_uid,
                harness_gid=harness_gid,
            ).check()
    # Made through the admin ns as inner root, so they land on the harness uid
    # and always need the chown below.
    missing = [str(p) for p in subdirs if not Path(p).exists()]
    if missing:
        run_admin(
            ["mkdir", "-p", *missing],
            block,
            harness_uid=harness_uid,
            harness_gid=harness_gid,
        ).check()

    owner = os.stat(root).st_uid
    if created or made_data or missing or owner != block.uid_start:
        run_admin(
            ["chown", "-R", f"{INNER_UID}:{INNER_GID}", str(root)],
            block,
            harness_uid=harness_uid,
            harness_gid=harness_gid,
        ).check()
        owner = os.stat(root).st_uid
    else:
        log.debug("service root %s already owned by %d; no chown", root, owner)
    if owner != block.uid_start:
        raise SpawnError(
            f"service root {root} is owned by host uid {owner}, expected {block.uid_start}"
        )


def remove_service_root(
    root: Path,
    block: UidBlock,
    *,
    harness_uid: int | None = None,
    harness_gid: int | None = None,
) -> None:
    """Delete a service-owned tree through the admin namespace.

    Safety rails, because this is an ``rm -rf`` driven by a config file: the
    path must be absolute, free of ``..`` and not a top-level directory.
    """
    root = Path(root)
    if not root.is_absolute():
        raise ValueError(f"refusing to remove non-absolute path {root}")
    if ".." in root.parts:
        raise ValueError(f"refusing to remove path containing '..': {root}")
    if len(root.parts) < _MIN_REMOVE_PARTS:
        raise ValueError(
            f"refusing to remove shallow path {root} (< {_MIN_REMOVE_PARTS} components)"
        )
    if not root.exists():
        return
    run_admin(
        ["rm", "-rf", str(root)],
        block,
        harness_uid=harness_uid,
        harness_gid=harness_gid,
    ).check()
    log.info("removed service root %s", root)
