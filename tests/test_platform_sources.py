"""Portable tests for source delivery.

Everything here runs against a throwaway git repository in ``tmp_path``: the
mirror, the archive|tar extraction and the gc are plain harness-owned file
operations and need no namespace. ``stage`` is the one part that must fork into
the admin user namespace, so it is exercised twice: here with ``run_admin``
replaced by a recorder (asserting the exact argv sequence and the no-op path),
and for real in ``tests/linux/test_platform_sources.py``, which is where the
reflink claim is actually measured.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from ams.platform import sources
from ams.platform.sources import SHA_MARKER, SourceError, SourceMirror
from ams.uidmap import UidBlock
from ams.userns import AdminResult

BLOCK = UidBlock(100_000, 100_000, 1024)

_GIT_ENV = {
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "HOME": "/nonexistent-ams-test-home",
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
        ["git", *args],
        cwd=str(cwd),
        env=_GIT_ENV,
        capture_output=True,
        check=True,
    )
    return proc.stdout.decode().strip()


def _make_repo(path: Path, files: dict[str, str]) -> str:
    """Create a repo at ``path`` with one commit holding ``files``; return the sha."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    return _commit(path, files, "first")


def _commit(path: Path, files: dict[str, str], message: str) -> str:
    for rel, text in files.items():
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", message)
    return _git(path, "rev-parse", "HEAD")


@pytest.fixture
def upstream(tmp_path: Path) -> tuple[Path, str]:
    src = tmp_path / "upstream"
    sha = _make_repo(src, {"README.md": "hello\n", "svc/main.py": "print(1)\n"})
    return src, sha


@pytest.fixture
def mirror(tmp_path: Path, upstream: tuple[Path, str]) -> SourceMirror:
    return SourceMirror(tmp_path / "store", "api", url=str(upstream[0]))


# --------------------------------------------------------------------------- urls


@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:StevenLi-phoenix/api.git",
        "ssh://git@github.com/StevenLi-phoenix/api.git",
        "git://github.com/StevenLi-phoenix/api.git",
        "http://github.com/StevenLi-phoenix/api.git",
        "relative/path.git",
    ],
)
def test_only_https_file_and_absolute_paths_are_accepted(url: str, tmp_path: Path) -> None:
    with pytest.raises(SourceError):
        SourceMirror(tmp_path / "store", "api", url=url)


def test_a_url_carrying_credentials_is_refused_without_echoing_it(tmp_path: Path) -> None:
    with pytest.raises(SourceError) as excinfo:
        SourceMirror(tmp_path, "api", url="https://user:hunter2@github.com/o/r.git")
    assert "hunter2" not in str(excinfo.value)
    assert "credentials" in str(excinfo.value)


def test_plain_https_and_file_urls_are_accepted(tmp_path: Path) -> None:
    for url in ("https://github.com/StevenLi-phoenix/api.git", "file:///srv/api.git", "/srv/api"):
        assert SourceMirror(tmp_path, "api", url=url).url == url


def test_an_invalid_mirror_name_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SourceError):
        SourceMirror(tmp_path, "../escape", url="https://github.com/o/r.git")


# -------------------------------------------------------------------------- fetch


def test_fetch_clones_a_bare_mirror_and_resolves_the_branch_head(
    mirror: SourceMirror, upstream: tuple[Path, str]
) -> None:
    _, sha = upstream
    assert not mirror.mirror_dir.exists()

    got = mirror.fetch("main")

    assert got == sha
    assert (mirror.mirror_dir / "HEAD").is_file()
    assert not (mirror.mirror_dir / "refs" / "remotes").exists()  # bare mirror, not a work tree
    assert list(mirror.mirror_dir.parent.glob(".*tmp*")) == []


def test_fetch_picks_up_a_new_commit(mirror: SourceMirror, upstream: tuple[Path, str]) -> None:
    src, first = upstream
    assert mirror.fetch("main") == first

    second = _commit(src, {"README.md": "hello again\n"}, "second")

    assert second != first
    assert mirror.fetch("main") == second


def test_fetch_of_an_unknown_branch_raises(mirror: SourceMirror) -> None:
    with pytest.raises(SourceError, match="no such branch"):
        mirror.fetch("does-not-exist")


def test_a_ref_that_is_not_a_branch_name_is_refused(mirror: SourceMirror) -> None:
    with pytest.raises(SourceError, match="invalid ref"):
        mirror.fetch("--upload-pack=evil")


def test_a_hung_git_raises_source_error(mirror: SourceMirror, monkeypatch) -> None:
    def boom(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 1))

    monkeypatch.setattr(sources.subprocess, "run", boom)
    with pytest.raises(SourceError, match="timed out"):
        mirror.fetch("main", timeout_s=1.0)


# -------------------------------------------------------------------- materialize


def test_materialize_extracts_the_tree_and_is_idempotent(
    mirror: SourceMirror, upstream: tuple[Path, str]
) -> None:
    sha = mirror.fetch("main")

    tree = mirror.materialize(sha)

    assert tree == mirror.src_dir / sha
    assert (tree / "README.md").read_text() == "hello\n"
    assert (tree / "svc" / "main.py").read_text() == "print(1)\n"
    assert list(mirror.src_dir.glob(".*tmp*")) == []  # atomic: nothing partial left behind

    before = (tree.stat().st_mtime_ns, (tree / "README.md").stat().st_mtime_ns)
    time.sleep(0.01)
    again = mirror.materialize(sha)

    assert again == tree
    assert (tree.stat().st_mtime_ns, (tree / "README.md").stat().st_mtime_ns) == before


def test_materialize_of_an_unknown_sha_raises_and_leaves_nothing(mirror: SourceMirror) -> None:
    mirror.fetch("main")
    absent = "0" * 40

    with pytest.raises(SourceError):
        mirror.materialize(absent)

    assert not (mirror.src_dir / absent).exists()
    assert list(mirror.src_dir.glob(".*tmp*")) == []


def test_materialize_refuses_a_non_sha(mirror: SourceMirror) -> None:
    for bad in ("../../etc", "main", "", "ZZZZZZZ"):
        with pytest.raises(SourceError, match="invalid commit sha"):
            mirror.materialize(bad)


def test_materialize_without_a_mirror_says_so(mirror: SourceMirror) -> None:
    with pytest.raises(SourceError, match="call fetch"):
        mirror.materialize("a" * 40)


# ----------------------------------------------------------------------------- gc


def test_gc_keeps_the_most_recently_used_trees(
    mirror: SourceMirror, upstream: tuple[Path, str]
) -> None:
    src, _ = upstream
    first = mirror.fetch("main")
    mirror.materialize(first)
    second = _commit(src, {"README.md": "v2\n"}, "second")
    assert mirror.fetch("main") == second
    mirror.materialize(second)

    removed = mirror.gc(keep=1)

    assert removed == [first]
    assert not (mirror.src_dir / first).exists()
    assert (mirror.src_dir / second).is_dir()
    assert mirror.index_path.is_file()


def test_gc_keeping_everything_removes_nothing(
    mirror: SourceMirror, upstream: tuple[Path, str]
) -> None:
    sha = mirror.fetch("main")
    mirror.materialize(sha)

    assert mirror.gc(keep=2) == []
    assert (mirror.src_dir / sha).is_dir()


def test_gc_sweeps_a_partial_extraction(mirror: SourceMirror) -> None:
    sha = mirror.fetch("main")
    mirror.materialize(sha)
    stale = mirror.src_dir / f".{'b' * 40}.tmp999"
    stale.mkdir()

    mirror.gc(keep=2)

    assert not stale.exists()


def test_gc_with_a_corrupt_index_falls_back_to_mtime(mirror: SourceMirror) -> None:
    sha = mirror.fetch("main")
    mirror.materialize(sha)
    mirror.index_path.write_text("{not json", encoding="utf-8")

    assert mirror.gc(keep=1) == []
    assert (mirror.src_dir / sha).is_dir()


def test_gc_rejects_a_negative_keep(mirror: SourceMirror) -> None:
    with pytest.raises(ValueError, match="keep must be"):
        mirror.gc(keep=-1)


def test_gc_on_a_store_with_nothing_materialized(mirror: SourceMirror) -> None:
    assert mirror.gc() == []


# -------------------------------------------------------------------------- stage


class _AdminRecorder:
    """Stand-in for ``run_admin`` that records argv instead of forking."""

    def __init__(self, rc: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.rc = rc

    def __call__(self, argv, block, **kwargs) -> AdminResult:
        assert block == BLOCK
        self.calls.append(list(argv))
        return AdminResult(tuple(argv), self.rc, b"", b"")


def _install_fakes(monkeypatch, roots_created: list[Path]) -> _AdminRecorder:
    recorder = _AdminRecorder()
    monkeypatch.setattr(sources, "run_admin", recorder)

    def fake_ensure(root, block, **kwargs):
        assert block == BLOCK
        Path(root).mkdir(parents=True, exist_ok=True)
        roots_created.append(Path(root))

    monkeypatch.setattr(sources, "ensure_service_root", fake_ensure)
    return recorder


def test_stage_of_a_fresh_root_creates_it_and_runs_the_expected_admin_commands(
    mirror: SourceMirror, tmp_path: Path, monkeypatch
) -> None:
    sha = mirror.fetch("main")
    canonical = mirror.materialize(sha)
    root = tmp_path / "state" / "services" / "svc" / "root"
    created: list[Path] = []
    recorder = _install_fakes(monkeypatch, created)

    repo = mirror.stage(sha, root, BLOCK)

    assert repo == root / "repo"
    assert created == [root]
    new, old = str(root / "repo.new"), str(root / "repo.old")
    marker_cp = recorder.calls[2]
    assert recorder.calls == [
        ["rm", "-rf", new, old],
        ["cp", "-a", "--reflink=auto", str(canonical), new],
        marker_cp,
        ["chown", "-R", "1000:1000", new],
        ["mv", new, str(repo)],
    ]
    assert marker_cp[0] == "cp"
    assert marker_cp[1].endswith("/" + SHA_MARKER)
    assert marker_cp[2] == str(root / "repo.new" / SHA_MARKER)


def test_stage_over_an_existing_repo_swaps_it_out(
    mirror: SourceMirror, tmp_path: Path, monkeypatch
) -> None:
    sha = mirror.fetch("main")
    root = tmp_path / "root"
    (root / "repo").mkdir(parents=True)
    (root / "repo" / SHA_MARKER).write_text("c" * 40 + "\n", encoding="utf-8")
    created: list[Path] = []
    recorder = _install_fakes(monkeypatch, created)

    mirror.stage(sha, root, BLOCK)

    assert created == []  # the root already exists; no ensure_service_root
    tail = recorder.calls[-3:]
    assert tail == [
        ["mv", str(root / "repo"), str(root / "repo.old")],
        ["mv", str(root / "repo.new"), str(root / "repo")],
        ["rm", "-rf", str(root / "repo.old")],
    ]


def test_stage_is_a_no_op_when_the_marker_already_names_the_sha(
    mirror: SourceMirror, tmp_path: Path, monkeypatch
) -> None:
    sha = mirror.fetch("main")
    root = tmp_path / "root"
    (root / "repo").mkdir(parents=True)
    (root / "repo" / SHA_MARKER).write_text(sha + "\n", encoding="utf-8")
    recorder = _install_fakes(monkeypatch, [])

    repo = mirror.stage(sha, root, BLOCK)

    assert repo == root / "repo"
    assert recorder.calls == []
    assert not mirror.src_dir.exists()  # not even materialized


def test_a_failing_admin_command_raises_source_error(
    mirror: SourceMirror, tmp_path: Path, monkeypatch
) -> None:
    sha = mirror.fetch("main")
    mirror.materialize(sha)
    root = tmp_path / "root"
    root.mkdir()
    failing = _AdminRecorder(rc=1)
    monkeypatch.setattr(sources, "run_admin", failing)

    with pytest.raises(SourceError, match="clear stage dirs failed"):
        mirror.stage(sha, root, BLOCK)


def test_stage_refuses_a_relative_service_root(mirror: SourceMirror) -> None:
    with pytest.raises(ValueError, match="absolute"):
        mirror.stage("a" * 40, Path("relative/root"), BLOCK)
