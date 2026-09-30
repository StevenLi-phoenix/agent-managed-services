"""Portable tests for the 1.1.0 additions to source delivery (PLAN-core §4.2).

Two additions, both driven by core mode:

* ``SourceMirror.stage(..., dest=...)`` -- the tree can land anywhere under the
  service root, not only at ``repo``. Core stages ``releases/<sha>`` and flips a
  ``current`` symlink between releases, so the staging location is a path, and
  its parent (``releases/``) may not exist yet. The default ``dest="repo"`` must
  produce the exact admin argv sequence it always did; that is pinned by the
  unchanged tests in ``test_platform_sources.py`` and restated here.
* ``SourceMirror.stage_plain(sha, dest_dir)`` -- the same copy without a user
  namespace, for dev hosts and ``--no-isolation`` (macOS has no userns at all).
  It runs for real here: the copy, the marker, the swap, and the fallback from
  ``cp -a --reflink=auto`` to plain ``cp -a`` on a BSD ``cp`` that rejects the
  flag.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from test_platform_sources import BLOCK, _commit, _install_fakes, _make_repo

from ams.platform import sources
from ams.platform.sources import SHA_MARKER, SourceError, SourceMirror


@pytest.fixture
def upstream(tmp_path: Path) -> tuple[Path, str]:
    src = tmp_path / "upstream"
    sha = _make_repo(src, {"README.md": "hello\n", "svc/main.py": "print(1)\n"})
    return src, sha


@pytest.fixture
def mirror(tmp_path: Path, upstream: tuple[Path, str]) -> SourceMirror:
    return SourceMirror(tmp_path / "store", "api", url=str(upstream[0]))


# ------------------------------------------------------------ stage(dest=...)


def test_default_dest_is_byte_identical_to_the_old_repo_stage(
    mirror: SourceMirror, tmp_path: Path, monkeypatch
) -> None:
    sha = mirror.fetch("main")
    canonical = mirror.materialize(sha)
    root = tmp_path / "root"
    root.mkdir()
    recorder = _install_fakes(monkeypatch, [])

    explicit = mirror.stage(sha, root, BLOCK, dest="repo")

    new = str(root / "repo.new")
    assert explicit == root / "repo"
    assert [c for i, c in enumerate(recorder.calls) if i != 2] == [
        ["rm", "-rf", new, str(root / "repo.old")],
        ["cp", "-a", "--reflink=auto", str(canonical), new],
        ["chown", "-R", "1000:1000", new],
        ["mv", new, str(root / "repo")],
    ]


def test_a_nested_dest_creates_and_chowns_only_the_missing_parents(
    mirror: SourceMirror, tmp_path: Path, monkeypatch
) -> None:
    sha = mirror.fetch("main")
    canonical = mirror.materialize(sha)
    root = tmp_path / "root"
    root.mkdir()
    recorder = _install_fakes(monkeypatch, [])

    staged = mirror.stage(sha, root, BLOCK, dest=f"releases/{sha}")

    target = root / "releases" / sha
    new, old = str(target) + ".new", str(target) + ".old"
    assert staged == target
    marker_cp = recorder.calls[4]
    assert recorder.calls == [
        ["mkdir", "-p", str(root / "releases")],
        ["chown", "1000:1000", str(root / "releases")],
        ["rm", "-rf", new, old],
        ["cp", "-a", "--reflink=auto", str(canonical), new],
        marker_cp,
        ["chown", "-R", "1000:1000", new],
        ["mv", new, str(target)],
    ]
    assert marker_cp[0] == "cp" and marker_cp[2] == f"{new}/{SHA_MARKER}"


def test_every_missing_parent_level_is_chowned_non_recursively(
    mirror: SourceMirror, tmp_path: Path, monkeypatch
) -> None:
    sha = mirror.fetch("main")
    root = tmp_path / "root"
    root.mkdir()
    recorder = _install_fakes(monkeypatch, [])

    mirror.stage(sha, root, BLOCK, dest="a/b/tree")

    assert recorder.calls[:2] == [
        ["mkdir", "-p", str(root / "a" / "b")],
        ["chown", "1000:1000", str(root / "a"), str(root / "a" / "b")],
    ]


def test_an_existing_parent_costs_no_admin_fork(
    mirror: SourceMirror, tmp_path: Path, monkeypatch
) -> None:
    sha = mirror.fetch("main")
    root = tmp_path / "root"
    (root / "releases").mkdir(parents=True)
    recorder = _install_fakes(monkeypatch, [])

    mirror.stage(sha, root, BLOCK, dest=f"releases/{sha}")

    assert recorder.calls[0][:2] == ["rm", "-rf"]
    assert not any(call[0] == "mkdir" for call in recorder.calls)


def test_a_nested_dest_over_an_existing_tree_swaps_it_out(
    mirror: SourceMirror, tmp_path: Path, monkeypatch
) -> None:
    sha = mirror.fetch("main")
    root = tmp_path / "root"
    target = root / "releases" / "tree"
    target.mkdir(parents=True)
    (target / SHA_MARKER).write_text("c" * 40 + "\n", encoding="utf-8")
    recorder = _install_fakes(monkeypatch, [])

    mirror.stage(sha, root, BLOCK, dest="releases/tree")

    assert recorder.calls[-3:] == [
        ["mv", str(target), str(target) + ".old"],
        ["mv", str(target) + ".new", str(target)],
        ["rm", "-rf", str(target) + ".old"],
    ]


def test_a_nested_dest_is_a_no_op_when_its_marker_names_the_sha(
    mirror: SourceMirror, tmp_path: Path, monkeypatch
) -> None:
    sha = mirror.fetch("main")
    root = tmp_path / "root"
    target = root / "releases" / sha
    target.mkdir(parents=True)
    (target / SHA_MARKER).write_text(sha + "\n", encoding="utf-8")
    recorder = _install_fakes(monkeypatch, [])

    assert mirror.stage(sha, root, BLOCK, dest=f"releases/{sha}") == target
    assert recorder.calls == []


def test_a_fresh_root_is_created_before_the_parents(
    mirror: SourceMirror, tmp_path: Path, monkeypatch
) -> None:
    sha = mirror.fetch("main")
    root = tmp_path / "state" / "services" / "core" / "root"
    created: list[Path] = []
    recorder = _install_fakes(monkeypatch, created)

    mirror.stage(sha, root, BLOCK, dest=f"releases/{sha}")

    assert created == [root]
    assert recorder.calls[0] == ["mkdir", "-p", str(root / "releases")]


@pytest.mark.parametrize(
    "dest",
    [
        "",
        "/abs/path",
        "..",
        "../escape",
        "releases/../../escape",
        "releases/./x",
        "releases//x",
        "releases/",
        "./repo",
        "rel eases/x",
        "releases/$(id)",
        "-rf",
        "a\nb",
        "releases\n",
    ],
)
def test_a_dest_that_is_not_a_plain_relative_path_is_refused(
    mirror: SourceMirror, tmp_path: Path, monkeypatch, dest: str
) -> None:
    recorder = _install_fakes(monkeypatch, [])
    with pytest.raises(SourceError, match="dest"):
        mirror.stage("a" * 40, tmp_path / "root", BLOCK, dest=dest)
    assert recorder.calls == []


# --------------------------------------------------------------- stage_plain


def _files(tree: Path) -> dict[str, str]:
    return {
        str(p.relative_to(tree)): p.read_text(encoding="utf-8")
        for p in sorted(tree.rglob("*"))
        if p.is_file()
    }


def test_stage_plain_copies_the_tree_and_writes_the_marker(
    mirror: SourceMirror, tmp_path: Path
) -> None:
    sha = mirror.fetch("main")
    dest = tmp_path / "root" / "releases" / sha  # parents do not exist yet

    staged = mirror.stage_plain(sha, dest)

    assert staged == dest
    assert _files(dest) == {
        "README.md": "hello\n",
        "svc/main.py": "print(1)\n",
        SHA_MARKER: sha + "\n",
    }
    assert sorted(p.name for p in dest.parent.iterdir()) == [sha]  # no .new/.old left
    # The canonical checkout is shared by everyone on the sha and stays pristine.
    assert not (mirror.checkout_dir(sha) / SHA_MARKER).exists()


def test_stage_plain_preserves_the_executable_bit(
    mirror: SourceMirror, tmp_path: Path, upstream
) -> None:
    src, _ = upstream
    script = src / "run.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    script.chmod(0o755)
    _commit(src, {}, "exec")
    sha = mirror.fetch("main")

    dest = mirror.stage_plain(sha, tmp_path / "tree")

    assert os.stat(dest / "run.sh").st_mode & stat.S_IXUSR


def test_stage_plain_is_a_no_op_when_the_marker_names_the_sha(
    mirror: SourceMirror, tmp_path: Path, monkeypatch
) -> None:
    sha = mirror.fetch("main")
    dest = tmp_path / "tree"
    mirror.stage_plain(sha, dest)
    (dest / "local-edit").write_text("kept\n", encoding="utf-8")

    def no_subprocess(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("a no-op stage must not run anything")

    monkeypatch.setattr(sources.subprocess, "run", no_subprocess)
    assert mirror.stage_plain(sha, dest) == dest
    assert (dest / "local-edit").exists()


def test_stage_plain_swaps_a_tree_at_an_older_sha(
    mirror: SourceMirror, tmp_path: Path, upstream
) -> None:
    src, _ = upstream
    first = mirror.fetch("main")
    dest = tmp_path / "tree"
    mirror.stage_plain(first, dest)
    second = _commit(src, {"README.md": "v2\n"}, "second")
    assert mirror.fetch("main") == second

    mirror.stage_plain(second, dest)

    assert (dest / "README.md").read_text(encoding="utf-8") == "v2\n"
    assert (dest / SHA_MARKER).read_text(encoding="utf-8") == second + "\n"
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("tree")) == ["tree"]


def test_stage_plain_falls_back_to_plain_cp_when_reflink_is_unsupported(
    mirror: SourceMirror, tmp_path: Path, monkeypatch
) -> None:
    """A BSD ``cp`` (stock macOS) rejects ``--reflink``: fall back, do not fail."""
    real_cp = sources._which("cp")
    fake = tmp_path / "bin" / "cp"
    fake.parent.mkdir()
    log_file = tmp_path / "cp.log"
    fake.write_text(
        "#!/bin/sh\n"
        f'echo "$*" >> "{log_file}"\n'
        'for a in "$@"; do\n'
        '  if [ "$a" = "--reflink=auto" ]; then echo "cp: illegal option -- -" >&2; exit 64; fi\n'
        "done\n"
        f'exec "{real_cp}" "$@"\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)
    monkeypatch.setattr(sources, "_which", lambda tool: str(fake) if tool == "cp" else real_cp)
    sha = mirror.fetch("main")

    dest = mirror.stage_plain(sha, tmp_path / "tree")

    assert (dest / "README.md").read_text(encoding="utf-8") == "hello\n"
    attempts = log_file.read_text(encoding="utf-8").splitlines()
    assert len(attempts) == 2
    assert attempts[0].startswith("-a --reflink=auto ")
    assert attempts[1].startswith("-a ") and "--reflink" not in attempts[1]


def test_stage_plain_raises_when_every_copy_attempt_fails(
    mirror: SourceMirror, tmp_path: Path, monkeypatch
) -> None:
    fake = tmp_path / "bin" / "cp"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\necho 'cp: disk on fire' >&2\nexit 1\n", encoding="utf-8")
    fake.chmod(0o755)
    real_which = sources._which
    monkeypatch.setattr(
        sources, "_which", lambda tool: str(fake) if tool == "cp" else real_which(tool)
    )
    sha = mirror.fetch("main")
    dest = tmp_path / "tree"

    with pytest.raises(SourceError, match="disk on fire"):
        mirror.stage_plain(sha, dest)
    assert not dest.exists()
    assert not (tmp_path / "tree.new").exists()


def test_stage_plain_refuses_a_relative_dest(mirror: SourceMirror) -> None:
    with pytest.raises(ValueError, match="absolute"):
        mirror.stage_plain("a" * 40, Path("relative/tree"))


def test_stage_plain_refuses_a_bad_sha(mirror: SourceMirror, tmp_path: Path) -> None:
    with pytest.raises(SourceError, match="invalid commit sha"):
        mirror.stage_plain("not-a-sha", tmp_path / "tree")
