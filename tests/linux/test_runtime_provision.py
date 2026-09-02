"""Runtime provisioning against the real target host.

Everything here asserts on state the kernel or the tool actually produced --
host ``st_uid`` of the environment, ``st_nlink`` of an installed file, free
blocks on the store -- rather than on our own bookkeeping, because the failure
modes this layer has are all silent: a venv the service cannot execute, a bun
install that hardlinks the harness-owned cache, a clone that quietly degraded
into a full copy.

The service roots live under ``/home/harness/store/state`` on purpose: reflink
cannot cross filesystems, so a venv on the runner's ext4 state dir would share
nothing with the XFS caches and the sharing assertion would be meaningless.

Run with ``scripts/remote-test.sh ams-rt tests/linux/test_runtime_provision.py``.
"""

from __future__ import annotations

import json
import logging
import os
import select
import signal
import stat as statmod
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from ams import schema
from ams.isolated import IsolatedSpawner, make_isolated_spawner
from ams.runtime import ProvisionError, RuntimeEnv, RuntimeStore, provision, python_venv_dir
from ams.spawn import SpawnedService, SpawnRequest
from ams.uidmap import UidBlock
from ams.userns import remove_service_root

pytestmark = pytest.mark.linux

# First block of the harness' /etc/subuid range; pinned so ownership assertions
# are exact (in production the allocator hands these out).
BLOCK = UidBlock(100_000, 100_000, 1024)
HOST_UID = BLOCK.uid_start

# The reflink store (D8/D13). Service roots must be on this filesystem.
STORE_ROOT = Path("/home/harness/store")
STATE_ROOT = STORE_ROOT / "state"

READ_TIMEOUT_S = 30.0
MB = 1 << 20


# --------------------------------------------------------------------------- helpers


@dataclass
class Ran:
    out: str
    err: str
    code: int


def _drain(svc: SpawnedService, timeout_s: float) -> tuple[str, str]:
    bufs = {svc.stdout_fd: bytearray(), svc.stderr_fd: bytearray()}
    live = set(bufs)
    deadline = time.monotonic() + timeout_s
    while live and time.monotonic() < deadline:
        ready, _, _ = select.select(list(live), [], [], 0.2)
        for fd in ready:
            try:
                chunk = os.read(fd, 65536)
            except BlockingIOError:
                continue
            if chunk:
                bufs[fd] += chunk
            else:
                live.discard(fd)
    return (
        bytes(bufs[svc.stdout_fd]).decode(errors="replace"),
        bytes(bufs[svc.stderr_fd]).decode(errors="replace"),
    )


def _wait(pid: int, timeout_s: float) -> int:
    deadline = time.monotonic() + timeout_s
    while True:
        done, status = os.waitpid(pid, os.WNOHANG)
        if done == pid:
            return status
        if time.monotonic() > deadline:
            os.kill(pid, signal.SIGKILL)
            return os.waitpid(pid, 0)[1]
        time.sleep(0.02)


def _decl(service_id: str, argv: list[str], runtime: dict[str, object]) -> schema.ServiceDecl:
    return schema.from_dict(
        {"id": service_id, "start": {"argv": argv}, "runtime": runtime},
    )


def _free_bytes(path: Path) -> int:
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


def _apparent_bytes(path: Path) -> int:
    """Sum of file sizes, ignoring how many blocks are actually allocated.

    Deliberately not ``st_blocks``: shared extents are counted in every file
    that references them, which is exactly the number we do NOT want here.
    """
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for name in files:
            p = Path(dirpath) / name
            try:
                st = p.lstat()
            except OSError:
                continue
            if statmod.S_ISREG(st.st_mode):
                total += st.st_size
    return total


def _installed_file(node_modules: Path, relative: str) -> Path:
    """The real file behind an installed package entry point.

    pnpm links ``node_modules/<pkg>`` at its content-addressed copy under
    ``.pnpm``, so the link has to be resolved before ``st_nlink`` says anything
    about how the file got there.
    """
    resolved = (node_modules / relative).resolve()
    assert resolved.is_file(), f"{node_modules / relative} does not resolve to a file"
    return resolved


# --------------------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def store() -> RuntimeStore:
    return RuntimeStore(STORE_ROOT)


@pytest.fixture(scope="module")
def spawner() -> IsolatedSpawner:
    return make_isolated_spawner(lambda _service_id: BLOCK)


@pytest.fixture
def roots() -> Iterator[Callable[[str], Path]]:
    """Hand out service roots on the reflink store and remove them afterwards.

    They end up owned by host uid 100000, which the harness cannot unlink
    directly; ``remove_service_root`` goes through the admin namespace.
    """
    made: list[Path] = []

    def make(name: str) -> Path:
        root = STATE_ROOT / f"rt-test-{name}"
        made.append(root)
        return root

    yield make
    for root in made:
        remove_service_root(root, BLOCK)
        assert not root.exists()


Run = Callable[[schema.ServiceDecl, Path, RuntimeEnv], Ran]


@pytest.fixture
def run_service(spawner: IsolatedSpawner) -> Run:
    """Run a short-lived service with its provisioned runtime env and collect it."""

    def _run(decl: schema.ServiceDecl, root: Path, env: RuntimeEnv) -> Ran:
        extra_env, path_prepend = env.as_tuple()
        req = SpawnRequest(decl, root, {}, extra_env=extra_env, path_prepend=path_prepend)
        svc = spawner.spawn(req)
        try:
            out, err = _drain(svc, READ_TIMEOUT_S)
            status = _wait(svc.pid, 5.0)
        finally:
            spawner.cleanup(svc)
        return Ran(out, err, os.waitstatus_to_exitcode(status))

    return _run


# --------------------------------------------------------------------------- python


@pytest.mark.timeout(600)
def test_uv_venv_is_service_owned_and_grows_in_place(
    store: RuntimeStore, roots: Callable[[str], Path], run_service: Run, tmp_path: Path
) -> None:
    root = roots("uv")
    argv = ["python", "-c", "import six; print('six', six.__version__)"]
    decl = _decl("rt-uv", argv, {"kind": "uv", "python": "3.12", "packages": ["six"]})
    log_path = tmp_path / "provision.log"

    env = provision(decl, root, store, BLOCK, log_path=log_path)

    venv = root / ".venv"
    interpreter = venv / "bin" / "python"
    assert interpreter.exists()
    assert os.stat(venv).st_uid == HOST_UID
    # The interpreter itself is shared, not copied: the venv links at the
    # uv-managed one in the store, which stays harness-owned. Only what the venv
    # actually owns changes hands.
    assert interpreter.resolve().is_relative_to(store.python_dir)
    assert os.stat(venv / "lib" / "python3.12" / "site-packages" / "six.py").st_uid == HOST_UID
    assert env.path_prepend == (str(venv / "bin"),)
    assert env.extra_env == {"VIRTUAL_ENV": str(venv)}
    assert "uv venv" in log_path.read_text()

    ran = run_service(decl, root, env)
    assert ran.code == 0, ran.err
    assert ran.out.startswith("six ")

    # Re-provisioning adds a package without rebuilding the environment: that is
    # what makes "agent installs one more dependency" cheap and non-destructive.
    stamp = (venv / "pyvenv.cfg").stat().st_mtime_ns
    argv2 = ["python", "-c", "import six, attrs; print('both', attrs.__version__)"]
    decl2 = _decl("rt-uv", argv2, {"kind": "uv", "python": "3.12", "packages": ["six", "attrs"]})
    env2 = provision(decl2, root, store, BLOCK)

    assert (venv / "pyvenv.cfg").stat().st_mtime_ns == stamp
    ran2 = run_service(decl2, root, env2)
    assert ran2.code == 0, ran2.err
    assert ran2.out.startswith("both ")


@pytest.mark.timeout(300)
def test_missing_requirements_file_names_the_path(
    store: RuntimeStore, roots: Callable[[str], Path]
) -> None:
    root = roots("req")
    decl = _decl("rt-req", ["true"], {"kind": "uv", "requirements": "requirements.txt"})

    with pytest.raises(ProvisionError) as excinfo:
        provision(decl, root, store, BLOCK)

    assert str(root / "requirements.txt") in str(excinfo.value)
    # Rejected before any work: no half-built environment is left behind.
    assert not (root / ".venv").exists()


@pytest.mark.timeout(600)
def test_bad_package_reports_tool_stderr_and_stays_provisionable(
    store: RuntimeStore, roots: Callable[[str], Path], run_service: Run
) -> None:
    root = roots("fail")
    bad = "this-package-does-not-exist-zzz"
    decl = _decl("rt-fail", ["true"], {"kind": "uv", "python": "3.12", "packages": [bad]})

    with pytest.raises(ProvisionError) as excinfo:
        provision(decl, root, store, BLOCK)

    message = str(excinfo.value)
    assert bad in message  # the tool's own stderr, not a generic wrapper
    assert "uv pip install" in message
    assert os.stat(root).st_uid == HOST_UID  # root still belongs to the service

    good = _decl(
        "rt-fail",
        ["python", "-c", "import six; print('recovered')"],
        {"kind": "uv", "python": "3.12", "packages": ["six"]},
    )
    env = provision(good, root, store, BLOCK)
    ran = run_service(good, root, env)
    assert ran.code == 0, ran.err
    assert ran.out.strip() == "recovered"


@pytest.mark.timeout(900)
def test_second_venv_shares_extents_with_the_first(
    store: RuntimeStore, roots: Callable[[str], Path], run_service: Run
) -> None:
    """A second service asking for numpy must cost disk it can share, not copy."""
    spec: dict[str, object] = {"kind": "uv", "python": "3.12", "packages": ["numpy"]}
    argv = ["python", "-c", "import numpy; print('numpy', numpy.__version__)"]

    root1 = roots("np1")
    provision(_decl("rt-np-one", argv, spec), root1, store, BLOCK)

    before = _free_bytes(STORE_ROOT)
    root2 = roots("np2")
    decl2 = _decl("rt-np-two", argv, spec)
    env2 = provision(decl2, root2, store, BLOCK)
    grew = before - _free_bytes(STORE_ROOT)

    apparent = _apparent_bytes(root2 / ".venv")
    assert apparent > 10 * MB, f"numpy is not really in the second venv ({apparent} bytes)"
    assert grew < 5 * MB, f"second venv consumed {grew} bytes; reflink sharing is not happening"

    ran = run_service(decl2, root2, env2)
    assert ran.code == 0, ran.err
    assert ran.out.startswith("numpy ")


# --------------------------------------------------------------------------- uv sync

# A uv *project* whose only dependency is a sibling directory installed
# editable. That is the shape the api monorepo has
# (``sdk = { path = "../../components/sdk", editable = true }``), and it is the
# reason the whole tree has to be copied into the service root: the source is
# resolved relative to the project, outside the workdir but inside the root.
_SYNC_PROJECT = """\
[project]
name = "proj"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["mylib"]

[tool.uv]
package = false

[tool.uv.sources]
mylib = { path = "../lib", editable = true }
"""

_SYNC_LIB = """\
[project]
name = "mylib"
version = "0.1.0"
requires-python = ">=3.12"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
"""


def _write_sync_project(root: Path) -> Path:
    """Lay the project out under ``root`` as the harness, before provisioning.

    Mirrors production: the agent rsyncs a repository into the service root and
    only then provisions, so ``_ensure_root`` finds both root and workdir
    present and leaves the ownership to the closing chown.
    """
    proj = root / "proj"
    lib = root / "lib" / "mylib"
    lib.mkdir(parents=True)
    proj.mkdir()
    (proj / "pyproject.toml").write_text(_SYNC_PROJECT, encoding="utf-8")
    (root / "lib" / "pyproject.toml").write_text(_SYNC_LIB, encoding="utf-8")
    (lib / "__init__.py").write_text('VALUE = "from-path-dep"\n', encoding="utf-8")
    return proj


@pytest.mark.timeout(900)
def test_uv_sync_project_with_editable_path_dependency(
    store: RuntimeStore,
    roots: Callable[[str], Path],
    run_service: Run,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``runtime.sync``: the venv lands in the project and the path dep resolves.

    Two provisions in one test on purpose. The first has no ``uv.lock``, which
    is the case that must warn and still work; the second runs against the lock
    the first one wrote, which is the ``--frozen`` path every subsequent restart
    takes.
    """
    root = roots("sync")
    proj = _write_sync_project(root)
    argv = ["python", "-c", "import mylib; print('mylib', mylib.VALUE)"]
    decl = schema.from_dict(
        {
            "id": "rt-sync",
            "start": {"argv": argv, "workdir": "proj"},
            "runtime": {"kind": "uv", "python": "3.12", "sync": True},
        }
    )
    log_path = tmp_path / "provision.log"

    with caplog.at_level(logging.WARNING, logger="ams.runtime"):
        env = provision(decl, root, store, BLOCK, log_path=log_path)

    venv = proj / ".venv"
    assert python_venv_dir(decl, root) == venv
    assert (venv / "bin" / "python").exists()
    assert env.extra_env == {"VIRTUAL_ENV": str(venv)}
    assert env.path_prepend == (str(venv / "bin"),)
    # The whole tree, path dependency included, belongs to the service now.
    assert os.stat(venv).st_uid == HOST_UID
    assert os.stat(root / "lib" / "mylib" / "__init__.py").st_uid == HOST_UID

    # No lock file existed, so provisioning resolved instead of freezing and said so.
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("uv.lock" in m and "resolving" in m for m in warnings), warnings
    text = log_path.read_text()
    assert "uv sync" in text and "uv sync --frozen" not in text

    ran = run_service(decl, root, env)
    assert ran.code == 0, ran.err
    assert ran.out.strip() == "mylib from-path-dep"

    # uv wrote the lock; the next provision must take the --frozen path silently.
    assert (proj / "uv.lock").is_file()
    caplog.clear()
    log2 = tmp_path / "provision2.log"
    with caplog.at_level(logging.WARNING, logger="ams.runtime"):
        env2 = provision(decl, root, store, BLOCK, log_path=log2)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING], caplog.records
    assert "uv sync --frozen" in log2.read_text()
    assert env2 == env

    ran2 = run_service(decl, root, env2)
    assert ran2.code == 0, ran2.err
    assert ran2.out.strip() == "mylib from-path-dep"


@pytest.mark.timeout(120)
def test_uv_sync_without_a_project_names_the_missing_file(
    store: RuntimeStore, roots: Callable[[str], Path]
) -> None:
    root = roots("sync-empty")
    decl = schema.from_dict(
        {
            "id": "rt-sync-empty",
            "start": {"argv": ["true"], "workdir": "proj"},
            "runtime": {"kind": "uv", "sync": True},
        }
    )

    with pytest.raises(ProvisionError) as excinfo:
        provision(decl, root, store, BLOCK)

    assert "pyproject.toml" in str(excinfo.value)
    assert str(root / "proj") in str(excinfo.value)
    assert not (root / "proj" / ".venv").exists()


# --------------------------------------------------------------------------- node


@pytest.mark.timeout(600)
def test_pnpm_installs_into_the_workdir_without_sharing_inodes(
    store: RuntimeStore, roots: Callable[[str], Path], run_service: Run
) -> None:
    root = roots("pnpm")
    argv = ["node", "-e", "const f=require('is-odd'); if(!f(3)) process.exit(1); console.log('ok')"]
    decl = _decl("rt-pnpm", argv, {"kind": "pnpm", "packages": ["is-odd"]})

    env = provision(decl, root, store, BLOCK)

    node_modules = root / "node_modules"
    assert node_modules.is_dir()
    assert json.loads((root / "package.json").read_text())["name"] == "rt-pnpm"
    assert os.stat(node_modules).st_uid == HOST_UID
    assert env.path_prepend[0] == str(node_modules / ".bin")

    # nlink == 1 proves the clone import method, not the hardlink one: a shared
    # inode would have let the closing chown flip the harness-owned cache.
    installed = _installed_file(node_modules, "is-odd/index.js")
    assert installed.lstat().st_nlink == 1, installed
    assert installed.stat().st_uid == HOST_UID

    ran = run_service(decl, root, env)
    assert ran.code == 0, ran.err
    assert ran.out.strip() == "ok"


@pytest.mark.timeout(600)
def test_bun_installs_with_copyfile_backend(
    store: RuntimeStore, roots: Callable[[str], Path], run_service: Run
) -> None:
    root = roots("bun")
    argv = ["bun", "-e", "const f=require('is-odd'); if(!f(3)) process.exit(1); console.log('ok')"]
    decl = _decl("rt-bun", argv, {"kind": "bun", "packages": ["is-odd"]})

    env = provision(decl, root, store, BLOCK)

    node_modules = root / "node_modules"
    assert node_modules.is_dir()
    assert os.stat(node_modules).st_uid == HOST_UID
    assert env.extra_env == {"BUN_INSTALL": str(store.bun_install)}

    installed = _installed_file(node_modules, "is-odd/index.js")
    assert installed.lstat().st_nlink == 1, installed
    assert installed.stat().st_uid == HOST_UID

    ran = run_service(decl, root, env)
    assert ran.code == 0, ran.err
    assert ran.out.strip() == "ok"
