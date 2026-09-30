"""ams.platform.corectl: the control plane through upstream corectl.mjs (fake runner)."""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from ams.platform import corectl
from ams.platform.corectl import CoreControl, CoreControlError
from ams.userns import AdminResult

TREE = Path("/srv/core/releases/abc")
SOCK = Path("/srv/core/run/control.sock")


class FakeRunner:
    def __init__(self, replies: Sequence[tuple[int, object, str]]) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, object]] = []

    def __call__(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        cwd: str | None,
        timeout_s: float,
    ) -> AdminResult:
        self.calls.append({"argv": list(argv), "env": dict(env), "cwd": cwd, "timeout": timeout_s})
        rc, out, err = self.replies.pop(0)
        text = out if isinstance(out, str) else json.dumps(out, indent=2)
        return AdminResult(tuple(argv), rc, text.encode(), err.encode())


def _ctl(runner: FakeRunner) -> CoreControl:
    return CoreControl(runner, TREE, SOCK, "/node/bin:/usr/bin:/bin")


def test_status_runs_the_trees_corectl_against_the_socket() -> None:
    runner = FakeRunner([(0, {"autoDeploy": False, "plugins": []}, "")])
    assert _ctl(runner).status() == {"autoDeploy": False, "plugins": []}
    call = runner.calls[0]
    assert call["argv"] == [
        "node",
        str(TREE / "scripts" / "corectl.mjs"),
        "--socket",
        str(SOCK),
        "status",
    ]
    env = call["env"]
    assert isinstance(env, dict)
    assert env["PATH"] == "/node/bin:/usr/bin:/bin"
    assert env["SUDO_USER"] == corectl.ACTOR
    assert call["cwd"] == str(TREE)


def test_deploy_outcome_not_ok_is_a_result_not_a_crash() -> None:
    transition = {
        "pluginId": "timeservice",
        "outcome": "rejected",
        "reason": "dependency_unavailable: x",
    }
    runner = FakeRunner([(1, transition, "")])
    assert _ctl(runner).deploy("a" * 64) == transition
    assert runner.calls[0]["argv"][-2:] == ["deploy", "a" * 64]


def test_deploy_ok() -> None:
    transition = {"pluginId": "t", "outcome": "ok", "toArtifact": "b" * 64}
    assert _ctl(FakeRunner([(0, transition, "")])).deploy("b" * 64)["outcome"] == "ok"


def test_error_without_json_raises_with_stderr_tail() -> None:
    runner = FakeRunner([(1, "", "corectl: connect ENOENT /srv/core/run/control.sock\n")])
    with pytest.raises(CoreControlError, match="ENOENT"):
        _ctl(runner).status()


def test_rc_zero_but_garbage_raises() -> None:
    with pytest.raises(CoreControlError, match="JSON"):
        _ctl(FakeRunner([(0, "not json", "")])).status()


def test_rc_one_with_json_but_no_outcome_raises() -> None:
    # Only a transition's outcome makes exit 1 a result.
    with pytest.raises(CoreControlError):
        _ctl(FakeRunner([(1, {"plugins": []}, "boom")])).status()


def test_upload_returns_artifact_id(tmp_path: Path) -> None:
    art = tmp_path / "t.artifact.json"
    art.write_text("{}", encoding="utf-8")
    runner = FakeRunner([(0, {"artifactId": "c" * 64, "manifest": {}, "buildInfo": {}}, "")])
    assert _ctl(runner).upload(art) == "c" * 64
    assert runner.calls[0]["argv"][-2:] == ["upload", str(art)]


def test_upload_without_artifact_id_raises(tmp_path: Path) -> None:
    with pytest.raises(CoreControlError, match="artifactId"):
        _ctl(FakeRunner([(0, {"x": 1}, "")])).upload(tmp_path / "x")


def test_transitions_and_failures_argv() -> None:
    runner = FakeRunner([(0, [{"kind": "deploy"}], ""), (0, [{"class": "start:x"}], "")])
    ctl = _ctl(runner)
    assert ctl.transitions("health", limit=7) == [{"kind": "deploy"}]
    assert ctl.failures("health", limit=3) == [{"class": "start:x"}]
    assert runner.calls[0]["argv"][4:] == ["transitions", "health", "--limit", "7"]
    assert runner.calls[1]["argv"][4:] == ["failures", "health", "--limit", "3"]


def test_privileges_and_restart_argv() -> None:
    runner = FakeRunner(
        [(0, {"pluginId": "health", "privileges": ["ops.read"]}, ""), (0, {"outcome": "ok"}, "")]
    )
    ctl = _ctl(runner)
    assert ctl.privileges("health", ["ops.read"])["privileges"] == ["ops.read"]
    assert ctl.restart("health") == {"outcome": "ok"}
    assert runner.calls[0]["argv"][4:] == ["privileges", "health", "ops.read"]
    assert runner.calls[1]["argv"][4:] == ["restart", "health"]


def test_privileges_rejects_unknown_privilege_before_running() -> None:
    runner = FakeRunner([])
    with pytest.raises(ValueError, match="privilege"):
        _ctl(runner).privileges("health", ["root"])
    assert runner.calls == []


def test_invalid_plugin_id_never_reaches_argv() -> None:
    runner = FakeRunner([])
    with pytest.raises(ValueError, match="plugin id"):
        _ctl(runner).transitions("--socket")
    assert runner.calls == []


def test_ping_true_and_false() -> None:
    assert _ctl(FakeRunner([(0, {"plugins": []}, "")])).ping() is True
    assert _ctl(FakeRunner([(1, "", "corectl: connect ECONNREFUSED")])).ping() is False


def test_runner_timeout_surfaces_as_control_error() -> None:
    runner = FakeRunner([(-9, "", "timed out")])
    with pytest.raises(CoreControlError, match="rc=-9"):
        _ctl(runner).status()


# ----------------------------------------------------------------- runners


def test_plain_runner_runs_argv_without_a_shell(tmp_path: Path) -> None:
    run = corectl.plain_runner()
    py_dir = str(Path(sys.executable).parent)
    res = run(
        [
            Path(sys.executable).name,
            "-c",
            "import os,sys; print(os.getcwd()); print(os.environ['X_CORE']); sys.exit(3)",
        ],
        env={"PATH": py_dir, "X_CORE": "a;b $(c)"},
        cwd=str(tmp_path),
        timeout_s=30,
    )
    assert res.returncode == 3
    lines = res.stdout.decode().splitlines()
    assert Path(lines[0]).resolve() == tmp_path.resolve()
    assert lines[1] == "a;b $(c)"


def test_plain_runner_missing_exe_is_rc_127() -> None:
    res = corectl.plain_runner()(
        ["definitely-not-a-tool-xyz"], env={"PATH": "/nonexistent"}, cwd=None, timeout_s=5
    )
    assert res.returncode == 127
    assert b"not found" in res.stderr


def test_plain_runner_timeout() -> None:
    py = Path(sys.executable)
    res = corectl.plain_runner()(
        [py.name, "-c", "import time; time.sleep(5)"],
        env={"PATH": str(py.parent)},
        cwd=None,
        timeout_s=0.3,
    )
    assert res.returncode < 0
    assert b"timed out" in res.stderr


def test_isolated_runner_delegates_to_run_as_service(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_run_as_service(argv, block, *, env, cwd=None, timeout_s=60.0):  # noqa: ANN001, ANN202
        seen.update(argv=list(argv), block=block, env=dict(env), cwd=cwd, timeout=timeout_s)
        return AdminResult(tuple(argv), 0, b"{}", b"")

    import ams.userns

    monkeypatch.setattr(ams.userns, "run_as_service", fake_run_as_service, raising=False)
    block = object()
    run = corectl.isolated_runner(block)  # type: ignore[arg-type]
    res = run(["node", "x"], env={"PATH": "/p"}, cwd="/c", timeout_s=9)
    assert res.ok
    assert seen == {
        "argv": ["node", "x"],
        "block": block,
        "env": {"PATH": "/p"},
        "cwd": "/c",
        "timeout": 9,
    }


def test_isolated_runner_turns_a_spawn_error_into_rc_127(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tool missing on PATH (or a failed namespace setup) must reach CoreControl as a
    failed call -> CoreControlError, not escape as SpawnError past a tick's handlers."""
    import ams.userns

    def boom(argv, block, *, env, cwd=None, timeout_s=60.0):  # noqa: ANN001, ANN202
        raise ams.userns.SpawnError("'node' not found on PATH=/nowhere")

    monkeypatch.setattr(ams.userns, "run_as_service", boom)
    run = corectl.isolated_runner(object())  # type: ignore[arg-type]
    res = run(["node", "x"], env={"PATH": "/nowhere"}, cwd=None, timeout_s=1)
    assert res.returncode == 127
    assert b"not found" in res.stderr
    ctl = CoreControl(run, TREE, SOCK, "/nowhere")
    with pytest.raises(CoreControlError, match="not found"):
        ctl.status()
    assert ctl.ping() is False


# --------------------------------------------- review 2026-09-29: error codes, gc


def test_core_refusal_carries_cores_error_code(tmp_path: Path) -> None:
    # Manager.upload -> parse -> validateManifest throws; corectl prints the code.
    art = tmp_path / "t.artifact.json"
    art.write_text("{}", encoding="utf-8")
    err = "corectl: invalid_manifest: health: method ping is not provided\n"
    with pytest.raises(CoreControlError) as info:
        _ctl(FakeRunner([(1, "", err)])).upload(art)
    assert info.value.code == "invalid_manifest"


def test_transport_failure_has_no_error_code() -> None:
    runner = FakeRunner([(1, "", "corectl: connect ENOENT /srv/core/run/control.sock\n")])
    with pytest.raises(CoreControlError) as info:
        _ctl(runner).status()
    assert info.value.code is None


def test_runner_rc_127_has_no_error_code() -> None:
    with pytest.raises(CoreControlError) as info:
        _ctl(FakeRunner([(127, "", "'node' not found on PATH")])).status()
    assert info.value.code is None


def test_gc_argv_and_result() -> None:
    runner = FakeRunner([(0, {"removed": ["a" * 64], "kept": 3}, "")])
    assert _ctl(runner).gc() == {"removed": ["a" * 64], "kept": 3}
    assert runner.calls[0]["argv"][-1] == "gc"
