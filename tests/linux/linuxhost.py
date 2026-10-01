"""The uid/gid blocks the Linux-marked tests map: carved from the running
user's real ``/etc/subuid`` / ``/etc/subgid`` range, never a hard-coded start.

racknerd happened to give ``harness`` ``100000:65536``; a CI runner or a
workstation whose first login user already owns that range gives ``harness``
the next one (``165536:65536``), and a pinned ``UidBlock(100_000, ...)`` would
then ask ``newuidmap`` for ids the harness does not own. Importable by bare
name because pytest's ``prepend`` mode puts ``tests/linux`` on ``sys.path``.
"""

from __future__ import annotations

import logging
import os
import pwd
from pathlib import Path

from ams.uidmap import BLOCK_SIZE, UidBlock, parse_subid_file

log = logging.getLogger("tests.linuxhost")

#: Used off-Linux, where every test importing this is skipped anyway.
FALLBACK_START = 100_000


def _first_start(path: Path, keys: tuple[str, ...]) -> int:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return FALLBACK_START
    starts = [start for key in keys for start, _count in parse_subid_file(text, key)]
    return min(starts) if starts else FALLBACK_START


def _keys() -> tuple[str, ...]:
    try:
        pw = pwd.getpwuid(os.getuid())
    except KeyError:
        return (str(os.getuid()),)
    return (pw.pw_name, str(pw.pw_uid))


UID_START = _first_start(Path("/etc/subuid"), _keys())
GID_START = _first_start(Path("/etc/subgid"), _keys())


def block(index: int = 0, size: int = BLOCK_SIZE) -> UidBlock:
    """The ``index``-th ``size``-wide block of this user's subordinate range --
    the same block ``UidAllocator`` hands out ``index``-th on a fresh state dir."""
    return UidBlock(UID_START + index * size, GID_START + index * size, size)


log.debug("subordinate range starts: uid=%d gid=%d", UID_START, GID_START)


def reflink_capable(directory: Path) -> bool:
    """True if ``cp --reflink=always`` works inside ``directory`` -- i.e. the
    filesystem shares extents (XFS ``reflink=1``, btrfs). On ext4 (a ``plain``
    store, CI) provisioning copies instead, which is correct but not free, so
    the tests that measure "a second copy costs ~0 bytes" skip there."""
    import shutil
    import subprocess
    import tempfile

    cp = shutil.which("cp")
    if cp is None or not directory.is_dir():
        return False
    with tempfile.TemporaryDirectory(dir=directory) as tmp:
        src = Path(tmp) / "src"
        src.write_bytes(b"x" * 4096)
        res = subprocess.run(
            [cp, "--reflink=always", str(src), str(Path(tmp) / "dst")],
            capture_output=True,
            check=False,
        )
    log.debug("reflink probe in %s: rc=%d", directory, res.returncode)
    return res.returncode == 0
