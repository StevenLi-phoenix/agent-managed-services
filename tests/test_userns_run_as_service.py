"""``run_admin(env=, cwd=)`` and ``run_as_service``: everything but the namespace itself.

``fork_in_userns`` is replaced by a plain ``fork`` that runs the same
``pre_unshare`` and ``child_fn`` hooks, so the pipe plumbing, environment,
working directory, exe resolution and timeout handling are exercised for real
on any OS. What is recorded is which map and inner identity each entry point
asks for -- the part that decides whether a command runs as the harness's inner
root or as the service. The kernel side is ``tests/linux/test_run_as_service_live.py``.
"""

from __future__ import annotations

import os
import signal
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn

import pytest

from ams import userns
from ams.uidmap import UidBlock
from ams.userns import SpawnError, admin_map_args, run_admin, run_as_service

BLOCK = UidBlock(100_000, 100_000, 1024)


class FakeFork:
    """``fork_in_userns`` minus unshare/maps/setresuid: same hooks, same contract."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        uid_args: list[str],
        gid_args: list[str],
        inner_uid: int,
        inner_gid: int,
        child_fn: Callable[[], NoReturn],
        *,
        pre_unshare: Callable[[int], None] | None = None,
        **_: Any,
    ) -> int:
        self.calls.append(
            {
                "uid_args": list(uid_args),
                "gid_args": list(gid_args),
                "inner": (inner_uid, inner_gid),
                "pre_unshare": pre_unshare is not None,
            }
        )
        pid = os.fork()
        if pid == 0:  # pragma: no cover - runs in the child
            try:
                if pre_unshare is not None:
                    pre_unshare(os.getpid())
                child_fn()
            finally:
                os._exit(127)
        return pid


@pytest.fixture
def fake_fork(monkeypatch: pytest.MonkeyPatch) -> FakeFork:
    fake = FakeFork()
    monkeypatch.setattr(userns, "fork_in_userns", fake)
    return fake


SYS_PATH = "/usr/bin:/bin"


# --------------------------------------------------------------------------- run_admin


def test_run_admin_keeps_its_fixed_env_by_default(fake_fork: FakeFork) -> None:
    res = run_admin(["env"], BLOCK)
    assert res.ok
    lines = set(res.stdout.decode().splitlines())
    assert "LC_ALL=C" in lines and "PATH=/usr/local/bin:/usr/bin:/bin" in lines
    call = fake_fork.calls[0]
    uid_args, gid_args = admin_map_args(BLOCK, os.getuid(), os.getgid())
    assert call["uid_args"] == uid_args and call["gid_args"] == gid_args
    assert call["inner"] == (0, 0)


def test_run_admin_merges_env_over_the_admin_env(fake_fork: FakeFork) -> None:
    res = run_admin(["env"], BLOCK, env={"CORE_SOURCE_COMMIT": "abc", "LC_ALL": "C.UTF-8"})
    lines = set(res.stdout.decode().splitlines())
    assert "CORE_SOURCE_COMMIT=abc" in lines
    assert "LC_ALL=C.UTF-8" in lines  # the caller wins
    assert "PATH=/usr/local/bin:/usr/bin:/bin" in lines  # the rest is kept


def test_run_admin_resolves_the_exe_against_the_merged_path(
    fake_fork: FakeFork, tmp_path: Path
) -> None:
    tool = tmp_path / "bin" / "only-here"
    tool.parent.mkdir()
    tool.write_text("#!/bin/sh\necho found\n")
    tool.chmod(0o755)
    with pytest.raises(SpawnError, match="not found"):
        run_admin(["only-here"], BLOCK)
    assert fake_fork.calls == []  # refused before forking
    res = run_admin(["only-here"], BLOCK, env={"PATH": f"{tool.parent}:{SYS_PATH}"})
    assert res.stdout == b"found\n"


def test_run_admin_cwd(fake_fork: FakeFork, tmp_path: Path) -> None:
    res = run_admin(["pwd", "-P"], BLOCK, cwd=str(tmp_path))
    assert res.stdout.decode().strip() == str(tmp_path.resolve())


def test_run_admin_bad_cwd_is_a_spawn_failure(fake_fork: FakeFork, tmp_path: Path) -> None:
    res = run_admin(["pwd"], BLOCK, cwd=str(tmp_path / "missing"))
    # The fake reports a child-side failure as exit 127 (the real one raises
    # SpawnError from the error pipe); either way the command never ran.
    assert not res.ok and res.stdout == b""


# --------------------------------------------------------------------------- run_as_service


def test_run_as_service_uses_the_runtime_map_and_inner_1000(fake_fork: FakeFork) -> None:
    res = run_as_service(["true"], BLOCK, env={"PATH": SYS_PATH})
    assert res.ok and res.argv == ("true",)
    call = fake_fork.calls[0]
    assert call["uid_args"] == BLOCK.newuidmap_args()
    assert call["gid_args"] == BLOCK.newgidmap_args()
    assert call["inner"] == (1000, 1000)


def test_run_as_service_passes_exactly_the_given_env(fake_fork: FakeFork) -> None:
    res = run_as_service(["env"], BLOCK, env={"PATH": SYS_PATH, "CORE_SOCKET": "/r/run/c.sock"})
    lines = sorted(res.stdout.decode().splitlines())
    # nothing of the harness's own environment, nothing of the admin env.
    assert lines == ["CORE_SOCKET=/r/run/c.sock", f"PATH={SYS_PATH}"]


def test_run_as_service_captures_stdout_stderr_and_rc(fake_fork: FakeFork) -> None:
    res = run_as_service(
        ["sh", "-c", "echo out; echo err >&2; exit 3"], BLOCK, env={"PATH": SYS_PATH}
    )
    assert (res.returncode, res.stdout, res.stderr) == (3, b"out\n", b"err\n")
    assert not res.ok


def test_run_as_service_cwd(fake_fork: FakeFork, tmp_path: Path) -> None:
    res = run_as_service(["pwd", "-P"], BLOCK, env={"PATH": SYS_PATH}, cwd=str(tmp_path))
    assert res.stdout.decode().strip() == str(tmp_path.resolve())
    assert fake_fork.calls[0]["pre_unshare"]


def test_run_as_service_enters_the_deepest_reachable_ancestor_first(
    fake_fork: FakeFork, tmp_path: Path
) -> None:
    """A missing leaf: the harness-side walk stops at its parent, the rest fails after."""
    res = run_as_service(["pwd"], BLOCK, env={"PATH": SYS_PATH}, cwd=str(tmp_path / "a" / "b"))
    assert not res.ok and res.stdout == b""


def test_run_as_service_resolves_the_exe_against_env_path(
    fake_fork: FakeFork, tmp_path: Path
) -> None:
    bindir = tmp_path / "node" / "bin"
    bindir.mkdir(parents=True)
    node = bindir / "node"
    node.write_text('#!/bin/sh\necho "node $*"\n')
    node.chmod(0o755)
    res = run_as_service(
        ["node", "scripts/corectl.mjs", "status"], BLOCK, env={"PATH": f"{bindir}:{SYS_PATH}"}
    )
    assert res.stdout == b"node scripts/corectl.mjs status\n"
    with pytest.raises(SpawnError, match="'node' not found"):
        run_as_service(["node"], BLOCK, env={"PATH": SYS_PATH})


def test_run_as_service_needs_argv_and_path(fake_fork: FakeFork) -> None:
    with pytest.raises(ValueError, match="argv"):
        run_as_service([], BLOCK, env={"PATH": SYS_PATH})
    with pytest.raises(ValueError, match="PATH"):
        run_as_service(["true"], BLOCK, env={})
    assert fake_fork.calls == []


def test_run_as_service_timeout_kills_the_whole_group(fake_fork: FakeFork, tmp_path: Path) -> None:
    """A build that hangs must not leave its children behind holding the pipes."""
    pidfile = tmp_path / "grandchild.pid"
    started = time.monotonic()
    res = run_as_service(
        ["sh", "-c", f"sleep 30 & echo $! > {pidfile}; wait"],
        BLOCK,
        env={"PATH": SYS_PATH},
        timeout_s=0.5,
    )
    assert time.monotonic() - started < 10
    assert res.returncode == -signal.SIGKILL
    grandchild = int(pidfile.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        os.kill(grandchild, signal.SIGKILL)
        pytest.fail("grandchild survived the timeout")
