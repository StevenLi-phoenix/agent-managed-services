import os
import select
import sys
import time

import pytest

from ams.schema import loads
from ams.spawn import PlainSpawner, SpawnedService, Spawner, SpawnRequest


def _read_all(fd: int, timeout: float = 5.0) -> bytes:
    out = b""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r, _, _ = select.select([fd], [], [], 0.1)
        if not r:
            continue
        chunk = os.read(fd, 4096)
        if not chunk:
            return out
        out += chunk
    raise TimeoutError("no EOF on fd")


def test_spawn_request_env_and_argv(tmp_path):
    d = loads(
        'id="svc"\n[start]\nargv=["prog", "--port", "${PORT_main}"]\nworkdir="app"\n'
        '[env]\nURL="http://127.0.0.1:${PORT_main}"\nSECRET="s"\n[ports]\nmain=0'
    )
    req = SpawnRequest(
        d, tmp_path, {"main": 4321}, extra_env={"VIRTUAL_ENV": "/v"}, path_prepend=("/v/bin",)
    )
    assert req.argv() == ["prog", "--port", "4321"]
    assert req.workdir == tmp_path / "app"
    env = req.env()
    assert env["URL"] == "http://127.0.0.1:4321"
    assert env["PORT_main"] == "4321"
    assert env["HOME"] == str(tmp_path)
    assert env["PATH"].startswith("/v/bin:")
    assert env["VIRTUAL_ENV"] == "/v" and env["SECRET"] == "s"
    assert env["AMS_SERVICE_ID"] == "svc"
    # nothing from the harness environment leaks in
    assert "AMS_STATE_DIR" not in env and "SSH_AUTH_SOCK" not in env


def test_plain_spawner_runs_child_and_holds_fds(tmp_path):
    code = (
        "import os,sys; print('out', os.getcwd(), os.environ['PORT_main']); "
        "print('err', file=sys.stderr); sys.exit(3)"
    )
    d = loads(f'id="svc"\n[start]\nargv=["{sys.executable}", "-c", "{code}"]\n[ports]\nmain=0')
    spawner = PlainSpawner()
    assert isinstance(spawner, Spawner)
    svc = spawner.spawn(SpawnRequest(d, tmp_path, {"main": 5555}))
    assert isinstance(svc, SpawnedService) and svc.cgroup is None
    out = _read_all(svc.stdout_fd)
    err = _read_all(svc.stderr_fd)
    assert out.split() == [b"out", str(tmp_path.resolve()).encode(), b"5555"]
    assert err.strip() == b"err"
    _, status = os.waitpid(svc.pid, 0)
    assert os.waitstatus_to_exitcode(status) == 3
    spawner.cleanup(svc)
    # fds are closed after cleanup
    for fd in (svc.stdout_fd, svc.stderr_fd):
        with pytest.raises(OSError):
            os.fstat(fd)


def test_plain_spawner_kill_tree_kills_process_group(tmp_path):
    code = (
        "import subprocess,sys; subprocess.run([sys.executable,'-c','import time; time.sleep(30)'])"
    )
    d = loads(f'id="svc"\n[start]\nargv=["{sys.executable}", "-c", "{code}"]')
    spawner = PlainSpawner()
    svc = spawner.spawn(SpawnRequest(d, tmp_path, {}))
    time.sleep(0.5)
    spawner.kill_tree(svc)
    _, status = os.waitpid(svc.pid, 0)
    assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == 9
    # the grandchild died too: reading stdout hits EOF because no writer holds the pipe
    assert _read_all(svc.stdout_fd) == b""
    spawner.cleanup(svc)


def test_plain_spawner_leaks_no_fds_on_failed_spawn(tmp_path):
    d = loads('id="svc"\n[start]\nargv=["/nonexistent/binary-zzz"]')
    spawner = PlainSpawner()
    before = len(os.listdir("/dev/fd"))
    for _ in range(5):
        with pytest.raises(OSError):
            spawner.spawn(SpawnRequest(d, tmp_path, {}))
    assert len(os.listdir("/dev/fd")) == before
