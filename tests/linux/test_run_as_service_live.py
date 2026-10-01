"""``run_as_service`` / ``run_admin(env=, cwd=)`` against the real kernel.

Asserts on observed kernel state -- the uid map a child sees, its credentials,
``NoNewPrivs``, the host owner of a file it wrote -- rather than on our own
bookkeeping. The portable half (pipes, env, cwd, timeouts with a plain fork) is
``tests/test_userns_run_as_service.py``.

Run with ``scripts/remote-test.sh ams-ras tests/linux/test_run_as_service_live.py``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import linuxhost
import pytest

from ams.spawn import DATA_DIRNAME, INNER_GID, INNER_UID
from ams.userns import (
    SpawnError,
    ensure_service_root,
    remove_service_root,
    run_admin,
    run_as_service,
)

pytestmark = pytest.mark.linux

BLOCK = linuxhost.block(0)
PATH = "/usr/local/bin:/usr/bin:/bin"
ENV = {"PATH": PATH, "LC_ALL": "C"}


@pytest.fixture(scope="module")
def base() -> Iterator[Path]:
    state = Path(os.environ.get("AMS_STATE_DIR", str(Path.home() / "state")))
    root = state / f"rastest-{os.getpid()}"
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o755)
    yield root
    for child in sorted(root.iterdir(), reverse=True):
        if child.is_dir():
            remove_service_root(child, BLOCK)
        else:
            child.unlink()
    root.rmdir()


@pytest.fixture(scope="module")
def svc_root(base: Path) -> Path:
    root = base / "svc"
    ensure_service_root(root, BLOCK)
    return root


def test_identity_is_the_service_not_the_harness(svc_root: Path) -> None:
    res = run_as_service(
        ["sh", "-c", "id -u; id -g; cat /proc/self/uid_map; grep NoNewPrivs /proc/self/status"],
        BLOCK,
        env=ENV,
    ).check()
    lines = [ln.split() for ln in res.stdout.decode().splitlines()]
    assert lines[0] == ["1000"] and lines[1] == ["1000"]
    assert lines[2] == ["1000", str(BLOCK.uid_start), str(BLOCK.size)]  # runtime map only
    assert lines[3] == ["NoNewPrivs:", "1"]


def test_files_it_writes_belong_to_the_block_on_the_host(svc_root: Path) -> None:
    target = svc_root / DATA_DIRNAME / "written-by-service"
    # relative to the service root: the harness-owned state dir above it need
    # not be traversable by the service's uid.
    run_as_service(
        ["touch", f"{DATA_DIRNAME}/written-by-service"], BLOCK, env=ENV, cwd=str(svc_root)
    ).check()
    # <root>/data is 0750 and the service's: the harness cannot even stat into
    # it, by design. The admin map can (inner 0 = harness, inner 1000 = the
    # block's first host id), so inner 1000:1000 is host uid_start:gid_start.
    res = run_admin(["stat", "-c", "%u:%g", str(target)], BLOCK).check()
    assert res.stdout.decode().strip() == f"{INNER_UID}:{INNER_GID}"
    assert os.stat(svc_root / DATA_DIRNAME).st_uid == BLOCK.uid_start


def test_harness_private_files_are_out_of_reach(base: Path) -> None:
    secret = base / "harness-only"
    secret.write_text("not for services\n")
    os.chmod(secret, 0o600)
    res = run_as_service(["cat", "harness-only"], BLOCK, env=ENV, cwd=str(base))
    assert not res.ok and b"not for services" not in res.stdout


def test_env_is_exactly_what_was_given_and_cwd_applies(svc_root: Path) -> None:
    res = run_as_service(
        ["sh", "-c", 'echo "$CORE_SOCKET"; pwd -P; env | wc -l'],
        BLOCK,
        env={**ENV, "CORE_SOCKET": "/x/run/control.sock"},
        cwd=str(svc_root),
    ).check()
    out = res.stdout.decode().splitlines()
    assert out[0] == "/x/run/control.sock"
    assert out[1] == str(svc_root.resolve())


def test_cwd_in_a_service_private_dir_is_entered_after_the_drop(svc_root: Path) -> None:
    """0700 and service-owned (below a 0750 data dir): the harness cannot enter
    it, the service can. The child enters the deepest ancestor it can as the
    harness (the service root) and finishes ``data/private`` after the drop."""
    private = svc_root / DATA_DIRNAME / "private"
    run_as_service(
        ["mkdir", "-m", "700", f"{DATA_DIRNAME}/private"], BLOCK, env=ENV, cwd=str(svc_root)
    ).check()
    res = run_as_service(["pwd", "-P"], BLOCK, env=ENV, cwd=str(private)).check()
    assert res.stdout.decode().strip() == str(private.resolve())


def test_exe_is_resolved_against_env_path(svc_root: Path) -> None:
    with pytest.raises(SpawnError, match="not found"):
        run_as_service(["definitely-not-a-tool"], BLOCK, env=ENV)


def test_run_admin_env_and_cwd_as_inner_root(svc_root: Path) -> None:
    res = run_admin(
        ["sh", "-c", 'id -u; echo "$FOO"; pwd -P'],
        BLOCK,
        env={"FOO": "bar"},
        cwd=str(svc_root / DATA_DIRNAME),
    ).check()
    assert res.stdout.decode().splitlines() == [
        "0",
        "bar",
        str((svc_root / DATA_DIRNAME).resolve()),
    ]
