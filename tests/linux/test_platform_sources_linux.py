"""Source delivery against the real target host: reflink sharing and ownership.

The claim this file exists to check is the one the portable tests structurally
cannot: that a second service on the same commit costs metadata, not a second
copy of the tree. That is a property of XFS ``reflink=1`` plus
``cp -a --reflink=auto`` plus both paths being on the *same* filesystem, so
everything here lives under ``/home/harness/store`` (D8/D13) and is measured
with ``os.statvfs`` around each step rather than asserted from our own
bookkeeping.

``materialize`` is measured first as the control: it writes ~17 MiB of
incompressible data for real, so if the free-space measurement cannot see that,
the two "grew by almost nothing" assertions below would be meaningless.

Run with ``scripts/remote-test.sh ams-src tests/linux/test_platform_sources_linux.py``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import linuxhost
import pytest

from ams.platform.sources import SHA_MARKER, SourceMirror
from ams.userns import remove_service_root

pytestmark = [pytest.mark.linux, pytest.mark.timeout(300)]

# First block of the harness' /etc/subuid range; pinned so ownership assertions
# are exact (in production the allocator hands these out).
BLOCK = linuxhost.block(0)
HOST_UID = BLOCK.uid_start

# The reflink store. Both the canonical checkout and the service roots must be
# on this one filesystem or every cp silently degrades to a full copy.
STORE_ROOT = Path("/home/harness/store")
BASE = STORE_ROOT / "state" / "plat-src-test"

MB = 1 << 20
TREE_FILES = 17
TREE_FILE_BYTES = MB
#: How much free space a reflinked copy of the tree may cost. Metadata only.
REFLINK_BUDGET = 2 * MB
#: How much the control (a real extraction of the tree) must cost, or the
#: measurement is not sensitive enough for the assertions above to mean anything.
CONTROL_FLOOR = 10 * MB

_GIT_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "HOME": str(BASE),
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_AUTHOR_NAME": "ams tests",
    "GIT_AUTHOR_EMAIL": "ams@example.invalid",
    "GIT_COMMITTER_NAME": "ams tests",
    "GIT_COMMITTER_EMAIL": "ams@example.invalid",
    "LC_ALL": "C",
}


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), env=_GIT_ENV, capture_output=True, check=True
    )
    return proc.stdout.decode().strip()


def _free_bytes() -> int:
    """Free blocks on the store, after flushing so delayed allocation is counted."""
    os.sync()
    st = os.statvfs(STORE_ROOT)
    return st.f_bavail * st.f_frsize


@dataclass
class Fixture:
    mirror: SourceMirror
    sha1: str
    sha2: str
    roots: list[Path]
    #: Free-space deltas of the first test's steps (control, stage a, stage b).
    costs: dict[str, int] = field(default_factory=dict)


@pytest.fixture(scope="module")
def env() -> Iterator[Fixture]:
    if BASE.exists():
        shutil.rmtree(BASE, ignore_errors=True)
    BASE.mkdir(parents=True)
    upstream = BASE / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "main")
    # Random payload: incompressible, so ~17 MiB on disk really is ~17 MiB.
    # core.compression=0 keeps git from burning a minute of the box's single
    # core deflating data that cannot shrink.
    _git(upstream, "config", "core.compression", "0")
    for i in range(TREE_FILES):
        (upstream / f"blob{i:02d}.bin").write_bytes(os.urandom(TREE_FILE_BYTES))
    (upstream / "VERSION").write_text("1\n", encoding="utf-8")
    _git(upstream, "add", "-A")
    _git(upstream, "commit", "-q", "-m", "first")
    mirror = SourceMirror(BASE / "store", "plat", url=str(upstream))
    sha1 = mirror.fetch("main")

    (upstream / "VERSION").write_text("2\n", encoding="utf-8")
    _git(upstream, "add", "-A")
    _git(upstream, "commit", "-q", "-m", "second")
    sha2 = mirror.fetch("main")
    assert sha1 != sha2

    fixture = Fixture(mirror, sha1, sha2, [BASE / "root-a", BASE / "root-b"])
    yield fixture

    for root in fixture.roots:
        remove_service_root(root, BLOCK)
    shutil.rmtree(BASE, ignore_errors=True)


def test_two_services_stage_the_same_sha(env: Fixture) -> None:
    root_a, root_b = env.roots

    before = _free_bytes()
    canonical = env.mirror.materialize(env.sha1)
    after_materialize = _free_bytes()
    repo_a = env.mirror.stage(env.sha1, root_a, BLOCK)
    after_a = _free_bytes()
    repo_b = env.mirror.stage(env.sha1, root_b, BLOCK)
    after_b = _free_bytes()

    env.costs.update(
        control=before - after_materialize, a=after_materialize - after_a, b=after_a - after_b
    )
    print(
        f"\ncanonical extraction: {env.costs['control'] / MB:.2f} MiB\n"
        f"stage -> root-a:      {env.costs['a'] / MB:.2f} MiB\n"
        f"stage -> root-b:      {env.costs['b'] / MB:.2f} MiB"
    )
    tree_bytes = sum(p.stat().st_size for p in canonical.rglob("*") if p.is_file())
    assert tree_bytes >= TREE_FILES * TREE_FILE_BYTES
    assert repo_a.is_dir() and repo_b.is_dir()
    for repo in (repo_a, repo_b):
        assert (repo / SHA_MARKER).read_text().strip() == env.sha1
        assert (repo / "blob00.bin").read_bytes() == (canonical / "blob00.bin").read_bytes()


@pytest.mark.skipif(
    not linuxhost.reflink_capable(STORE_ROOT), reason="store cannot reflink (plain store)"
)
def test_a_second_service_on_the_same_sha_is_almost_free(env: Fixture) -> None:
    assert env.costs, "test_two_services_stage_the_same_sha must run first"
    # Control: the measurement can see a real ~17 MiB write.
    assert env.costs["control"] >= CONTROL_FLOOR, f"control extraction only cost {env.costs}"
    assert env.costs["a"] < REFLINK_BUDGET, f"first stage cost {env.costs['a']} bytes"
    assert env.costs["b"] < REFLINK_BUDGET, f"second stage cost {env.costs['b']} bytes"


def test_the_staged_tree_belongs_to_the_service_block(env: Fixture) -> None:
    repo = env.roots[0] / "repo"

    assert os.stat(repo).st_uid == HOST_UID
    assert os.stat(repo).st_gid == BLOCK.gid_start
    for name in (SHA_MARKER, "VERSION", "blob00.bin"):
        st = os.stat(repo / name)
        assert st.st_uid == HOST_UID, name
        assert st.st_gid == BLOCK.gid_start, name


def test_restaging_the_same_sha_touches_nothing(env: Fixture) -> None:
    marker = env.roots[0] / "repo" / SHA_MARKER
    before = (marker.stat().st_mtime_ns, marker.stat().st_ctime_ns)

    env.mirror.stage(env.sha1, env.roots[0], BLOCK)

    assert (marker.stat().st_mtime_ns, marker.stat().st_ctime_ns) == before


def test_a_new_sha_replaces_the_repo_and_leaves_no_scratch_dirs(env: Fixture) -> None:
    root = env.roots[0]

    repo = env.mirror.stage(env.sha2, root, BLOCK)

    assert (repo / SHA_MARKER).read_text().strip() == env.sha2
    assert (repo / "VERSION").read_text() == "2\n"
    assert not (root / "repo.new").exists()
    assert not (root / "repo.old").exists()
    assert os.stat(repo / "VERSION").st_uid == HOST_UID
    # `data` is created by ensure_service_root (T1.3) and is where the service
    # keeps its sqlite db: the mv/mv/rm swap above must leave it alone, because
    # a re-sync that wiped it would silently destroy the service's state.
    assert (root / "data").is_dir()
    leftovers = {p.name for p in root.iterdir()} - {"repo", "data"}
    assert leftovers == set(), f"scratch left in the service root: {sorted(leftovers)}"


def test_gc_keeps_the_live_tree(env: Fixture) -> None:
    removed = env.mirror.gc(keep=1)

    assert removed == [env.sha1]
    assert not (env.mirror.src_dir / env.sha1).exists()
    assert (env.mirror.src_dir / env.sha2).is_dir()
