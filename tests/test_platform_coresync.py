"""ams.platform.coresync: one core tick, driven entirely by fakes (plain mode, portable).

The fakes model exactly the upstream behaviour coresync relies on:

- FakeCore: ``corectl status`` shapes (desired/observed/live), a deploy that is ok
  (probation), rejected, or auto-reverted after probation, privileges, restart.
- FakeSupervisor: the harness control socket (reload/start/stop/restart/status),
  recording which release ``current`` pointed at for every op, so "stop before
  flip" is checkable.
- FakePlanner: core_plan.mjs -- artifactId changes with every commit, contentKey
  only with content.
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from ams.escalations import journal_path, read_records
from ams.platform import core, coresync
from ams.platform.corectl import CoreControlError
from ams.platform.coresync import Hooks, PluginPlan
from ams.platform.sources import SourceError
from ams.runtime import ProvisionError, RuntimeStore
from ams.state import StateDir

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
SHA_D = "d" * 40
ROSTER = ["secrets", "store", "gateway", "auth", "health", "timeservice"]

CONFIG = """\
[core]
url = "/srv/upstream/api.git"
node = "24.20.0"
pnpm = "11.19.0"
gateway_port = 18080
plugins = ["secrets", "store", "gateway", "auth", "health", "timeservice"]
probation_timeout_s = 30
health_timeout_s = 20

[core.privileges]
health = ["ops.read"]

[[site]]
host = "api.example.test"
port = 18080
"""


# --------------------------------------------------------------------------- fakes


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0
        self.slept = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s
        self.slept += s


def aid_for(content_key: str, sha: str) -> str:
    return hashlib.sha256(f"{content_key}|{sha}".encode()).hexdigest()


class Registry:
    """What the planner built, so the fake core's upload can find it by path."""

    def __init__(self) -> None:
        self.by_path: dict[str, tuple[str, str]] = {}


class FakePlanner:
    def __init__(self, registry: Registry, keys: dict[str, str]) -> None:
        self.registry = registry
        self.keys = dict(keys)
        self.errors: dict[str, str] = {}
        self.calls: list[dict[str, Any]] = []
        self.fail: str | None = None

    def __call__(
        self, tree: Path, out_dir: Path, ids: Sequence[str], *, runner: Any, env: Mapping[str, str]
    ) -> list[PluginPlan]:
        sha = env["CORE_SOURCE_COMMIT"]
        self.calls.append({"tree": tree, "ids": list(ids), "sha": sha})
        if self.fail:
            raise coresync.CorePlanError(self.fail)
        out: list[PluginPlan] = []
        for pid in ids:
            if pid in self.errors:
                out.append(PluginPlan(plugin_id=pid, error=self.errors[pid]))
                continue
            ck = self.keys[pid]
            aid = aid_for(ck, sha)
            path = str(out_dir / f"{pid}-{aid[:12]}.artifact.json")
            self.registry.by_path[path] = (pid, aid)
            out.append(
                PluginPlan(
                    plugin_id=pid,
                    dir=str(tree / "plugins" / pid),
                    artifact_id=aid,
                    content_key=ck,
                    path=path,
                    commit=sha,
                    dirty=False,
                )
            )
        return out


class FakeCore:
    def __init__(self, registry: Registry) -> None:
        self.registry = registry
        self.plugins: dict[str, dict[str, Any]] = {}
        self.behaviour: dict[str, str] = {}  # pid -> ok | rejected | revert | slow
        self.settle_after = 1  # status polls until probation ends
        self._countdown: dict[str, int] = {}
        self.up = True
        self.calls: list[tuple[str, ...]] = []
        #: pid -> privilege it cannot pass probation without (health / ops.read)
        self.needs: dict[str, str] = {}
        #: pid -> reason: deploy is rejected with this reason (manager.ts incompatibility)
        self.reject_reason: dict[str, str] = {}
        #: pid -> error corectl raises for its upload (core-side refusal or transport)
        self.upload_error: dict[str, CoreControlError] = {}
        #: pids whose `corectl restart` transition fails
        self.restart_fail: set[str] = set()
        #: release sha -> pids whose live generation fails to boot on that core
        self.broken: dict[str, set[str]] = {}
        #: pid -> observed failure reason core reports at boot (e.g. artifact_unreadable)
        self.boot_failed: dict[str, str] = {}
        self.layout: core.CoreLayout | None = None
        self._gen = 0

    # --- helpers
    def install(self, pid: str, aid: str, privileges: Sequence[str] = ()) -> None:
        self.plugins[pid] = {
            "pluginId": pid,
            "desired": {
                "artifactId": aid,
                "enabled": True,
                "privileges": list(privileges),
                "autoDeploy": False,
            },
            "observed": {
                "artifactId": aid,
                "phase": "active",
                "reason": None,
                "generationId": "g1",
                "lastKnownGood": aid,
                "previousArtifactId": None,
            },
            "live": {"generationId": "g1", "phase": "active", "artifactId": aid, "commit": None},
        }

    def _tick_probation(self) -> None:
        for pid in list(self._countdown):
            self._countdown[pid] -= 1
            if self._countdown[pid] > 0:
                continue
            del self._countdown[pid]
            p = self.plugins[pid]
            beh = self.behaviour.get(pid, "ok")
            need = self.needs.get(pid)
            if need and need not in p["desired"]["privileges"]:
                beh = "revert"
            if beh == "revert":
                prev = p["observed"]["previousArtifactId"]
                if prev:
                    p["desired"]["artifactId"] = prev
                    p["observed"].update(
                        artifactId=prev, phase="active", reason="probation failed: 5/5"
                    )
                    p["live"]["artifactId"] = prev
                else:
                    p["observed"].update(
                        phase="active", reason="probation failed; no revert target"
                    )
            else:
                p["observed"].update(phase="active", lastKnownGood=p["observed"]["artifactId"])

    # --- corectl surface
    def status(self) -> dict[str, Any]:
        self.calls.append(("status",))
        if not self.up:
            raise CoreControlError("corectl status failed (rc=1): connect ENOENT")
        if self.behaviour.get("*") != "slow":
            self._tick_probation()
        plugins = json.loads(json.dumps(sorted(self.plugins.values(), key=lambda p: p["pluginId"])))
        running = core.current_sha(self.layout) if self.layout else None
        broken = self.broken.get(running or "", set())
        for p in plugins:
            if p["pluginId"] in broken:
                p["live"] = None
                p["observed"].update(phase="failed", reason="start failed: boom")
            if p["pluginId"] in self.boot_failed:
                p["live"] = None
                p["observed"].update(phase="failed", reason=self.boot_failed[p["pluginId"]])
        return {"autoDeploy": False, "plugins": plugins}

    def upload(self, path: Path) -> str:
        self.calls.append(("upload", str(path)))
        if not self.up:
            raise CoreControlError("down")
        pid, aid = self.registry.by_path[str(path)]
        if pid in self.upload_error:
            raise self.upload_error[pid]
        return aid

    def deploy(self, aid: str) -> dict[str, Any]:
        self.calls.append(("deploy", aid))
        pid = next(p for p, a in self.registry.by_path.values() if a == aid)
        beh = self.behaviour.get(pid, "ok")
        if pid in self.reject_reason:
            return {
                "pluginId": pid,
                "kind": "deploy",
                "outcome": "rejected",
                "reason": self.reject_reason[pid],
                "toArtifact": aid,
            }
        self.boot_failed.pop(pid, None)
        if beh == "rejected":
            return {
                "pluginId": pid,
                "kind": "deploy",
                "outcome": "rejected",
                "reason": "incompatible_contract: x drops y",
                "toArtifact": aid,
            }
        old = self.plugins.get(pid)
        prev = old["observed"]["artifactId"] if old else None
        privs = old["desired"]["privileges"] if old else []
        self.plugins[pid] = {
            "pluginId": pid,
            "desired": {
                "artifactId": aid,
                "enabled": True,
                "privileges": privs,
                "autoDeploy": False,
            },
            "observed": {
                "artifactId": aid,
                "phase": "probation",
                "reason": None,
                "generationId": "g2",
                "lastKnownGood": prev,
                "previousArtifactId": prev,
            },
            "live": {"generationId": "g2", "phase": "active", "artifactId": aid, "commit": None},
        }
        self._countdown[pid] = self.settle_after
        return {
            "pluginId": pid,
            "kind": "deploy",
            "outcome": "ok",
            "toArtifact": aid,
            "fromArtifact": prev,
        }

    def privileges(self, pid: str, privs: Sequence[str]) -> dict[str, Any]:
        self.calls.append(("privileges", pid, *privs))
        self.plugins[pid]["desired"]["privileges"] = list(privs)
        return dict(self.plugins[pid]["desired"])

    def restart(self, pid: str) -> dict[str, Any]:
        self.calls.append(("restart", pid))
        p = self.plugins[pid]
        if pid in self.restart_fail:
            return {"pluginId": pid, "kind": "restart", "outcome": "failed", "reason": "boom"}
        self._gen += 1
        gen = f"gr{self._gen}"
        p["observed"]["generationId"] = gen
        if p.get("live"):
            p["live"]["generationId"] = gen
        if p["observed"]["lastKnownGood"] != p["observed"]["artifactId"]:
            p["observed"].update(phase="probation", reason=None)
            self._countdown[pid] = self.settle_after
        return {"pluginId": pid, "outcome": "ok"}

    def gc(self) -> dict[str, Any]:
        self.calls.append(("gc",))
        return {"removed": [], "kept": len(self.plugins)}

    def transitions(self, pid: str, limit: int = 20) -> list[dict[str, Any]]:
        return [{"kind": "auto_revert", "outcome": "ok", "reason": "probation failed: 5/5 failed"}]

    def failures(self, pid: str, limit: int = 5) -> list[dict[str, Any]]:
        return [{"class": "probation", "message": "health check threw"}]

    def ping(self) -> bool:
        return self.up

    def ops(self, name: str) -> list[tuple[str, ...]]:
        return [c for c in self.calls if c[0] == name]


class FakeControl:
    def __init__(self, fake: FakeCore, tree: Path) -> None:
        self.fake = fake
        self.tree = tree

    def __getattr__(self, name: str) -> Any:
        return getattr(self.fake, name)


class FakeSupervisor:
    def __init__(self, layout: core.CoreLayout) -> None:
        self.layout = layout
        self.services: dict[str, dict[str, Any]] = {}
        self.desired: dict[str, str] = {}
        self.decls: dict[str, str] = {}
        self.ops: list[tuple[str, str | None, str | None]] = []
        self.fail_reload = False

    def _log(self, op: str, sid: str | None) -> None:
        self.ops.append((op, sid, core.current_sha(self.layout)))

    def reload(self) -> dict[str, Any]:
        """Like ``ams.reload``: a new declaration is started, a changed one is
        restarted -- unless the operator's ``desired`` is down (``ctl stop``) and it
        did not fail, which the real reload leaves down."""
        self._log("reload", None)
        if self.fail_reload:
            raise coresync.SupervisorError("reload refused")
        for sid in ("core", "caddy"):
            decl = self.layout.root.parent.parent / sid / "service.toml"
            if not decl.is_file():
                continue
            text = decl.read_text(encoding="utf-8")
            if sid in self.services and self.decls.get(sid) == text:
                continue  # unchanged: reload does nothing
            self.decls[sid] = text
            if sid in self.desired and self.desired[sid] == "down":
                if self.services.get(sid, {}).get("status") != "failed":
                    continue  # "changed but is down; leaving it down"
            self.desired[sid] = "up"
            self.services[sid] = {"status": "running"}
        return {"ok": True}

    def start(self, sid: str) -> dict[str, Any]:
        self._log("start", sid)
        if sid not in self.services:
            raise coresync.SupervisorError(f"unknown service {sid!r}")
        if self.services[sid]["status"] == "running":
            # Supervisor.start raises RuntimeError on a live process.
            raise coresync.SupervisorError(f"service {sid!r} is still running")
        self.desired[sid] = "up"
        self.services[sid] = {"status": "running"}
        return {"ok": True}

    def stop(self, sid: str) -> dict[str, Any]:
        self._log("stop", sid)
        self.desired[sid] = "down"
        self.services[sid] = {"status": "stopped"}
        return {"ok": True}

    def restart(self, sid: str) -> dict[str, Any]:
        self._log("restart", sid)
        self.desired[sid] = "up"
        self.services[sid] = {"status": "running"}
        return {"ok": True}

    def status(self) -> dict[str, Any]:
        return {"ok": True, "services": json.loads(json.dumps(self.services))}

    def names(self) -> list[tuple[str, str | None]]:
        return [(op, sid) for op, sid, _ in self.ops]


class FakeMirror:
    def __init__(self) -> None:
        self.head = SHA_A
        self.diffs: dict[tuple[str, str], list[str]] = {}
        self.fail: str | None = None
        self.staged: list[str] = []

    def fetch(self, ref: str = "main", timeout_s: float = 0) -> str:
        if self.fail:
            raise SourceError(self.fail)
        return self.head

    def changed_paths(self, old: str, new: str, timeout_s: float = 0) -> list[str]:
        if (old, new) not in self.diffs:
            raise SourceError(f"unknown commit {old[:7]}")
        return self.diffs[(old, new)]

    def stage_plain(self, sha: str, dest_dir: Path, *, timeout_s: float = 0) -> Path:
        self.staged.append(sha)
        (dest_dir / "scripts").mkdir(parents=True, exist_ok=True)
        (dest_dir / ".ams-sha").write_text(sha + "\n", encoding="utf-8")
        return dest_dir

    def gc(self, keep: int = 2) -> list[str]:
        return []


class World:
    """Everything one test drives; ``hooks()`` wires it into coresync."""

    def __init__(self, state: StateDir, store: RuntimeStore) -> None:
        self.state = state
        self.store = store
        self.layout = core.CoreLayout.from_state(state)
        self.clock = Clock()
        self.registry = Registry()
        self.keys = {pid: f"ck-{pid}-1" for pid in ROSTER}
        self.planner = FakePlanner(self.registry, self.keys)
        self.core = FakeCore(self.registry)
        self.core.layout = self.layout
        self.sup = FakeSupervisor(self.layout)
        self.mirror = FakeMirror()
        self.provisioned: list[tuple[str, bool]] = []
        self.provision_fail: dict[bool, str] = {}
        #: releases whose /health answers 200 once core runs them
        self.healthy: set[str] = set()
        self.gateway_calls = 0
        self.stream = io.StringIO()
        self.cfg = core.loads_config(CONFIG, state=state)
        self.control_trees: list[Path] = []

    def probe(self, port: int, path: str) -> int | None:
        svc = self.sup.services.get("core")
        if not svc or svc["status"] != "running":
            return None
        if "gateway" in self.core.boot_failed:
            return None  # the gateway plugin binds the port; it did not boot
        return 200 if core.current_sha(self.layout) in self.healthy else 503

    def provision(
        self,
        tree: Path,
        spec: Any,
        *,
        block: Any,
        store: Any,
        run_build: bool = True,
        log_path: Any = None,
        env: Mapping[str, str] | None = None,
        timeout_s: float = 0,
    ) -> None:
        assert spec.kind == "pnpm" and spec.node == "24.20.0"
        assert env is not None and env["CORE_SOURCE_COMMIT"] == tree.name
        self.provisioned.append((tree.name, run_build))
        if run_build in self.provision_fail:
            raise ProvisionError(self.provision_fail[run_build])

    def gateway(self, state: StateDir, cfg: core.CoreConfig) -> list[str]:
        self.gateway_calls += 1
        return ["Caddyfile", "sites/api.example.test.caddy"] if self.gateway_calls == 1 else []

    def control_factory(self, tree: Path, runner: Any, path_env: str) -> FakeControl:
        self.control_trees.append(tree)
        return FakeControl(self.core, tree)

    def hooks(self) -> Hooks:
        return Hooks(
            mirror_factory=lambda cfg, store: self.mirror,
            control_factory=self.control_factory,
            supervisor_factory=lambda state: self.sup,
            provision=self.provision,
            planner_factory=lambda layout, block: self.planner,
            http_probe=self.probe,
            gateway=self.gateway,
            toolchain=lambda store, cfg: "/fake/node/bin:/usr/bin:/bin",
            runner_factory=lambda block: object(),
            block_factory=lambda state: None,
        )

    def tick(self, **kw: Any) -> coresync.CoreReport:
        return coresync.tick(
            self.state,
            self.store,
            self.cfg,
            isolation=False,
            escalation=self.stream,
            now=self.clock.now,
            sleep=self.clock.sleep,
            hooks=self.hooks(),
            **kw,
        )

    def escalations(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.stream.getvalue().splitlines() if line.strip()]

    def kinds(self) -> list[str]:
        return [e["event"]["kind"] for e in self.escalations()]

    def record(self) -> dict[str, Any]:
        return json.loads(core.record_path(self.state).read_text(encoding="utf-8"))


@pytest.fixture
def world() -> Iterator[World]:
    root = Path(tempfile.mkdtemp(prefix="ams-cs-", dir="/tmp"))
    try:
        state = StateDir(root / "s")
        w = World(state, RuntimeStore(root / "store"))
        bundle = root / "plugins.json"
        bundle.write_text(
            json.dumps({"plugins": {"gateway": {"config": {"port": 18080}}}}), encoding="utf-8"
        )
        core.import_bundle(state, bundle, cfg=w.cfg)
        yield w
    finally:
        shutil.rmtree(root, ignore_errors=True)


def first_release(w: World) -> coresync.CoreReport:
    w.healthy.add(SHA_A)
    return w.tick()


# --------------------------------------------------------------------------- first release


def test_first_release_declares_flips_starts_and_installs_the_roster(world: World) -> None:
    w = world
    rep = first_release(w)
    assert rep.exit_code == 0, rep
    assert rep.sha == SHA_A
    assert rep.released and rep.release_ok
    # staged + installed, then built for the release
    assert w.mirror.staged == [SHA_A]
    assert w.provisioned == [(SHA_A, False), (SHA_A, True)]
    # declaration written and valid; current flipped BEFORE the reload that starts core
    decl = w.state.service_decl_path("core").read_text(encoding="utf-8")
    assert 'workdir = "current"' in decl
    assert w.sup.ops[0] == ("reload", None, SHA_A)
    assert core.current_sha(w.layout) == SHA_A
    # bundle placed
    assert (w.layout.etc / "plugins.json").is_file()
    # every roster plugin shipped in roster order, all live
    deployed = [w.registry.by_path[c[1]][0] for c in w.core.ops("upload")]
    assert deployed == ROSTER
    assert rep.shipped == tuple(ROSTER)
    assert rep.live == tuple(ROSTER)
    assert rep.failed == ()
    # privileges applied + the live plugin restarted so its next generation holds them
    assert w.core.ops("privileges") == [("privileges", "health", "ops.read")]
    assert w.core.ops("restart") == [("restart", "health")]
    assert rep.privileges_changed == ("health",)
    assert rep.gateway_changed == ("Caddyfile", "sites/api.example.test.caddy")
    # record
    rec = w.record()
    assert rec["release_sha"] == SHA_A
    assert rec["staged_sha"] == SHA_A
    assert rec["previous_release_sha"] is None
    assert rec["plugins"]["timeservice"]["content_key"] == "ck-timeservice-1"
    assert rec["plugins"]["timeservice"]["outcome"] == "live"
    assert rec["plugins"]["timeservice"]["sha"] == SHA_A
    assert rep.record_written
    assert w.escalations() == []


def test_controls_use_the_release_tree(world: World) -> None:
    first_release(world)
    assert world.control_trees
    assert all(t == world.layout.release_dir(SHA_A) for t in world.control_trees)


def test_no_op_tick_writes_nothing(world: World) -> None:
    w = world
    first_release(w)
    rec_path = core.record_path(w.state)
    decl_path = w.state.service_decl_path("core")
    before = (rec_path.read_bytes(), rec_path.stat().st_mtime_ns, decl_path.stat().st_mtime_ns)
    ops_before = len(w.sup.ops)
    calls_before = len(w.core.calls)
    rep = w.tick()
    assert rep.exit_code == 0
    assert not rep.record_written
    assert not rep.released
    assert rep.shipped == ()
    assert (
        rec_path.read_bytes(),
        rec_path.stat().st_mtime_ns,
        decl_path.stat().st_mtime_ns,
    ) == before
    assert w.sup.ops[ops_before:] == []  # no reload, no restart
    assert len(w.planner.calls) == 1  # not re-planned
    assert w.provisioned == [(SHA_A, False), (SHA_A, True)]
    # only reads went to core
    assert {c[0] for c in w.core.calls[calls_before:]} <= {"status"}
    assert w.escalations() == []


def test_plugin_only_change_ships_only_the_changed_content_key(world: World) -> None:
    w = world
    first_release(w)
    w.mirror.head = SHA_B
    w.mirror.diffs[(SHA_A, SHA_B)] = ["plugins/timeservice/index.ts"]
    w.keys["timeservice"] = "ck-timeservice-2"
    w.planner.keys = dict(w.keys)
    uploads_before = len(w.core.ops("upload"))
    rep = w.tick()
    assert rep.exit_code == 0, rep
    assert not rep.released  # no core path touched
    assert w.provisioned[-1] == (SHA_B, False)  # installed, not built
    assert rep.shipped == ("timeservice",)
    assert rep.live == ("timeservice",)
    new_uploads = w.core.ops("upload")[uploads_before:]
    assert [w.registry.by_path[c[1]][0] for c in new_uploads] == ["timeservice"]
    rec = w.record()
    assert rec["staged_sha"] == SHA_B and rec["release_sha"] == SHA_A
    assert rec["plugins"]["timeservice"]["sha"] == SHA_B
    assert rec["plugins"]["health"]["sha"] == SHA_A  # untouched


def test_unchanged_content_at_a_new_commit_ships_nothing(world: World) -> None:
    w = world
    first_release(w)
    w.mirror.head = SHA_B
    w.mirror.diffs[(SHA_A, SHA_B)] = ["docs/readme.md"]
    rep = w.tick()
    assert rep.shipped == ()
    assert rep.exit_code == 0
    assert w.record()["planned_sha"] == SHA_B


def test_failed_content_key_is_not_retried_but_new_content_is(world: World) -> None:
    w = world
    first_release(w)
    # commit B: timeservice changes and core rejects it
    w.mirror.head = SHA_B
    w.mirror.diffs[(SHA_A, SHA_B)] = ["plugins/timeservice/index.ts"]
    w.keys["timeservice"] = "ck-timeservice-bad"
    w.planner.keys = dict(w.keys)
    w.core.behaviour["timeservice"] = "rejected"
    rep = w.tick()
    assert rep.exit_code == 1
    assert rep.failed == ("timeservice",)
    assert w.kinds() == ["core_plugin_rejected"]
    esc = w.escalations()[0]
    assert esc["event"]["plugin"] == "timeservice"
    assert esc["event"]["service"] == "core"
    assert esc["event"]["sha"] == SHA_B
    assert esc["service_id"] == "core" and esc["action"] == "escalate"
    assert w.record()["plugins"]["timeservice"]["outcome"] == "failed"
    assert w.record()["plugins"]["timeservice"]["content_key"] == "ck-timeservice-bad"
    # the same record is in the escalation journal `ams escalations` reads
    [journaled] = read_records(journal_path(w.state))
    assert journaled["source"] == "core-sync"
    assert journaled["event"] == esc["event"] and journaled["reason"] == esc["reason"]

    # commit C: same (failed) content -> not shipped, not escalated again, exit 0
    w.mirror.head = SHA_C
    w.mirror.diffs[(SHA_A, SHA_C)] = ["plugins/timeservice/other.md"]
    uploads = len(w.core.ops("upload"))
    rep = w.tick()
    assert rep.shipped == ()
    assert len(w.core.ops("upload")) == uploads
    assert rep.exit_code == 0  # carried failure does not fail a later tick
    assert w.kinds() == ["core_plugin_rejected"]

    # commit D: new content -> retried, goes live
    w.mirror.head = SHA_D
    w.mirror.diffs[(SHA_A, SHA_D)] = ["plugins/timeservice/index.ts"]
    w.keys["timeservice"] = "ck-timeservice-fixed"
    w.planner.keys = dict(w.keys)
    w.core.behaviour["timeservice"] = "ok"
    rep = w.tick()
    assert rep.shipped == ("timeservice",)
    assert rep.live == ("timeservice",)
    assert rep.exit_code == 0
    assert w.record()["plugins"]["timeservice"]["outcome"] == "live"


def test_probation_auto_revert_is_a_failure_with_evidence(world: World) -> None:
    w = world
    first_release(w)
    w.mirror.head = SHA_B
    w.mirror.diffs[(SHA_A, SHA_B)] = ["plugins/timeservice/index.ts"]
    w.keys["timeservice"] = "ck-timeservice-2"
    w.planner.keys = dict(w.keys)
    w.core.behaviour["timeservice"] = "revert"
    rep = w.tick()
    assert rep.failed == ("timeservice",)
    assert rep.exit_code == 1
    assert w.kinds() == ["core_plugin_not_live"]
    cause = w.escalations()[0]["event"]["cause"]
    assert "auto_revert" in cause and "health check threw" in cause
    assert w.record()["plugins"]["timeservice"]["outcome"] == "failed"
    # not retried at the same content
    w.mirror.head = SHA_C
    w.mirror.diffs[(SHA_A, SHA_C)] = []
    rep = w.tick()
    assert rep.shipped == () and rep.exit_code == 0


def test_probation_still_running_is_pending_and_resolved_next_tick(world: World) -> None:
    w = world
    first_release(w)
    w.mirror.head = SHA_B
    w.mirror.diffs[(SHA_A, SHA_B)] = ["plugins/timeservice/index.ts"]
    w.keys["timeservice"] = "ck-timeservice-2"
    w.planner.keys = dict(w.keys)
    w.core.behaviour["*"] = "slow"  # probation never ends while this is set
    rep = w.tick()
    assert rep.pending == ("timeservice",)
    assert rep.failed == ()
    assert rep.exit_code == 0
    assert w.record()["plugins"]["timeservice"]["outcome"] == "probation"
    assert w.clock.slept >= w.cfg.probation_timeout_s
    del w.core.behaviour["*"]
    rep = w.tick()
    assert rep.live == ("timeservice",)
    assert w.record()["plugins"]["timeservice"]["outcome"] == "live"
    assert w.core.ops("upload")[-1][1].endswith(".artifact.json")
    assert len([c for c in w.core.ops("upload") if "timeservice" in c[1]]) == 2  # A and B only


def test_plan_build_error_escalates_once_per_commit(world: World) -> None:
    w = world
    first_release(w)
    w.mirror.head = SHA_B
    w.mirror.diffs[(SHA_A, SHA_B)] = ["plugins/timeservice/index.ts"]
    w.planner.errors["timeservice"] = "Transform failed with 1 error"
    rep = w.tick()
    assert rep.exit_code == 1
    assert w.kinds() == ["core_plugin_build_failed"]
    rep = w.tick()  # same head: no re-plan, no new escalation
    assert rep.exit_code == 0
    assert w.kinds() == ["core_plugin_build_failed"]


def test_whole_plan_failure_is_retried_next_tick(world: World) -> None:
    w = world
    first_release(w)
    w.mirror.head = SHA_B
    w.mirror.diffs[(SHA_A, SHA_B)] = ["plugins/timeservice/index.ts"]
    w.planner.fail = "node: esbuild missing"
    rep = w.tick()
    assert rep.exit_code == 1
    assert w.kinds() == ["core_plan_failed"]
    assert w.record()["planned_sha"] == SHA_A
    w.planner.fail = None
    w.keys["timeservice"] = "ck-timeservice-2"
    w.planner.keys = dict(w.keys)
    rep = w.tick()
    assert rep.shipped == ("timeservice",)
    assert w.record()["planned_sha"] == SHA_B


def test_transport_error_while_shipping_is_retried(world: World) -> None:
    w = world
    first_release(w)
    w.mirror.head = SHA_B
    w.mirror.diffs[(SHA_A, SHA_B)] = ["plugins/timeservice/index.ts"]
    w.keys["timeservice"] = "ck-timeservice-2"
    w.planner.keys = dict(w.keys)
    real_upload = w.core.upload

    def flaky(path: Path) -> str:
        raise CoreControlError("corectl upload failed (rc=1): socket hang up")

    w.core.upload = flaky  # type: ignore[method-assign]
    rep = w.tick()
    assert rep.exit_code == 1
    assert "core_ship_error" in w.kinds()
    assert w.record()["plugins"]["timeservice"]["content_key"] == "ck-timeservice-1"
    w.core.upload = real_upload  # type: ignore[method-assign]
    rep = w.tick()
    assert rep.shipped == ("timeservice",)


# --------------------------------------------------------------------------- release


def _core_change(w: World, sha: str, base: str = SHA_A) -> None:
    w.mirror.head = sha
    w.mirror.diffs[(base, sha)] = ["packages/core/manager.ts"]


def test_core_path_change_releases_with_stop_before_flip(world: World) -> None:
    w = world
    first_release(w)
    _core_change(w, SHA_B)
    w.healthy.add(SHA_B)
    n = len(w.sup.ops)
    rep = w.tick()
    assert rep.exit_code == 0, rep
    assert rep.released and rep.release_ok
    ops = w.sup.ops[n:]
    stop_i = next(i for i, o in enumerate(ops) if o[:2] == ("stop", "core"))
    start_i = next(i for i, o in enumerate(ops) if o[:2] == ("start", "core"))
    assert ops[stop_i][2] == SHA_A  # still the old tree while stopping
    assert ops[start_i][2] == SHA_B  # flipped before start
    assert stop_i < start_i
    rec = w.record()
    assert rec["release_sha"] == SHA_B and rec["previous_release_sha"] == SHA_A
    assert (SHA_B, True) in w.provisioned


def test_release_gate_failure_flips_back_and_is_not_retried(world: World) -> None:
    w = world
    first_release(w)
    _core_change(w, SHA_B)  # SHA_B is never healthy
    rep = w.tick()
    assert rep.exit_code == 1
    assert rep.released and rep.release_ok is False
    assert rep.rolled_back
    assert core.current_sha(w.layout) == SHA_A
    assert w.kinds() == ["core_release_failed"]
    rec = w.record()
    assert rec["release_sha"] == SHA_A
    assert rec["release_failed_sha"] == SHA_B
    # the next tick at the same head does not try again
    n = len(w.sup.ops)
    builds = len(w.provisioned)
    rep = w.tick()
    assert not rep.released
    assert len(w.provisioned) == builds
    assert [o for o in w.sup.ops[n:] if o[1] == "core"] == []
    assert rep.exit_code == 0
    assert w.kinds() == ["core_release_failed"]


def test_release_gate_and_flip_back_both_failing_is_core_down(world: World) -> None:
    w = world
    first_release(w)
    _core_change(w, SHA_B)
    w.core.up = False  # core's socket stops answering: neither tree passes a gate
    rep = w.tick()
    assert rep.exit_code == 1
    assert sorted(w.kinds()) == ["core_down", "core_release_failed"]
    assert core.current_sha(w.layout) == SHA_A


def _crash_looping_on(w: World, sha: str) -> None:
    """Core on ``sha`` dies at startup: its socket never answers, and the harness
    gives up on it (``failed``: restart policy exhausted, it will not restart)."""
    real_status, real_start = w.core.status, w.sup.start

    def status() -> dict[str, Any]:
        if core.current_sha(w.layout) == sha:
            raise CoreControlError("corectl status failed (rc=1): connect ENOENT")
        return real_status()

    def start(sid: str) -> dict[str, Any]:
        out = real_start(sid)
        if sid == "core" and core.current_sha(w.layout) == sha:
            w.sup.services[sid] = {"status": "failed"}
        return out

    w.core.status = status  # type: ignore[method-assign]
    w.sup.start = start  # type: ignore[method-assign]


def test_release_gate_gives_up_as_soon_as_the_harness_gave_up_on_core(world: World) -> None:
    # Seen in the local end-to-end run (2026-09-30): a release whose core died at
    # startup was abandoned by the harness after ~15 s (5 failures >= max_retries),
    # yet the gate kept waiting out the whole health_timeout_s -- ~90 s of outage --
    # before flipping back. A service the harness marked `failed` is never restarted,
    # so waiting on it cannot pass the gate.
    w = world
    first_release(w)
    _core_change(w, SHA_B)
    _crash_looping_on(w, SHA_B)
    started = w.clock.now()
    rep = w.tick()
    assert rep.release_ok is False and rep.rolled_back
    assert core.current_sha(w.layout) == SHA_A
    assert w.kinds() == ["core_release_failed"]
    assert "gave up" in w.escalations()[0]["event"]["cause"]
    assert w.clock.now() - started < w.cfg.health_timeout_s
    assert w.record()["release_failed_sha"] == SHA_B


def test_release_build_failure_does_not_touch_the_running_core(world: World) -> None:
    w = world
    first_release(w)
    _core_change(w, SHA_B)
    w.provision_fail[True] = "tsc: error TS2322"
    n = len(w.sup.ops)
    rep = w.tick()
    assert rep.exit_code == 1
    assert w.kinds() == ["core_release_failed"]
    assert "TS2322" in w.escalations()[0]["event"]["cause"]
    assert [o for o in w.sup.ops[n:] if o[1] == "core"] == []
    assert core.current_sha(w.layout) == SHA_A
    assert w.record()["release_failed_sha"] == SHA_B


def test_unknown_diff_base_is_treated_as_a_core_change(world: World) -> None:
    w = world
    first_release(w)
    w.mirror.head = SHA_B  # no diff registered -> SourceError
    w.healthy.add(SHA_B)
    rep = w.tick()
    assert rep.released and rep.release_ok


def test_first_release_gate_only_needs_the_control_socket(world: World) -> None:
    # Fresh core: no plugin installed, so /health cannot answer yet.
    w = world
    rep = w.tick()  # nothing in w.healthy
    assert rep.release_ok is True
    assert rep.exit_code == 0


def test_first_release_with_core_never_answering_is_core_down(world: World) -> None:
    w = world
    w.core.up = False
    rep = w.tick()
    assert rep.release_ok is False
    assert sorted(set(w.kinds())) == ["core_down", "core_release_failed"]
    assert rep.exit_code == 1
    # a first release is not held: the next tick tries again and heals
    assert w.record()["release_failed_sha"] is None
    w.core.up = True
    assert w.tick().release_ok is True


def test_stage_failure_escalates_and_keeps_record(world: World) -> None:
    w = world
    first_release(w)
    w.mirror.head = SHA_B
    w.mirror.diffs[(SHA_A, SHA_B)] = ["plugins/timeservice/index.ts"]
    w.provision_fail[False] = "ERR_PNPM_OUTDATED_LOCKFILE"
    rep = w.tick()
    assert rep.exit_code == 1
    assert w.kinds() == ["core_stage_failed"]
    assert w.record()["staged_sha"] == SHA_A


def test_rollback_release_flips_to_previous_and_holds_the_sha(world: World) -> None:
    w = world
    first_release(w)
    _core_change(w, SHA_B)
    w.healthy.add(SHA_B)
    w.tick()
    rep = coresync.rollback_release(
        w.state,
        w.store,
        w.cfg,
        isolation=False,
        escalation=w.stream,
        now=w.clock.now,
        sleep=w.clock.sleep,
        hooks=w.hooks(),
    )
    assert rep.exit_code == 0, rep
    assert core.current_sha(w.layout) == SHA_A
    rec = w.record()
    assert rec["release_sha"] == SHA_A and rec["previous_release_sha"] == SHA_B
    assert rec["release_failed_sha"] == SHA_B
    # the timer does not undo the operator's rollback
    rep = w.tick()
    assert not rep.released
    assert core.current_sha(w.layout) == SHA_A


def test_rollback_without_previous_is_an_error(world: World) -> None:
    w = world
    first_release(w)
    rep = coresync.rollback_release(
        w.state,
        w.store,
        w.cfg,
        isolation=False,
        escalation=w.stream,
        now=w.clock.now,
        sleep=w.clock.sleep,
        hooks=w.hooks(),
    )
    assert rep.exit_code == 1
    assert "previous" in (rep.error or "")


# --------------------------------------------------------------------------- drift / privileges


def test_manual_drift_is_reported_once_and_not_overridden(world: World) -> None:
    w = world
    first_release(w)
    other = "f" * 64
    w.core.plugins["timeservice"]["desired"]["artifactId"] = other
    uploads = len(w.core.ops("upload"))
    rep = w.tick()
    assert rep.drift == ("timeservice",)
    assert w.kinds() == ["core_plugin_drift"]
    assert len(w.core.ops("upload")) == uploads
    assert rep.exit_code == 0
    rep = w.tick()
    assert rep.drift == ("timeservice",)
    assert w.kinds() == ["core_plugin_drift"]  # not again


def test_plugin_missing_from_core_is_reinstalled(world: World) -> None:
    w = world
    first_release(w)
    del w.core.plugins["timeservice"]
    rep = w.tick()
    assert rep.shipped == ("timeservice",)
    assert rep.live == ("timeservice",)


def test_privileges_only_when_different(world: World) -> None:
    w = world
    first_release(w)
    assert len(w.core.ops("privileges")) == 1
    w.tick()
    assert len(w.core.ops("privileges")) == 1
    w.core.plugins["health"]["desired"]["privileges"] = []
    rep = w.tick()
    assert rep.privileges_changed == ("health",)
    assert w.core.ops("privileges")[-1] == ("privileges", "health", "ops.read")


def test_privileges_for_a_plugin_core_does_not_know_are_skipped(world: World) -> None:
    w = world
    w.cfg = replace(w.cfg, privileges={"health": ("ops.read",), "timeservice": ("ops.read",)})
    w.core.behaviour["timeservice"] = "rejected"
    first_release(w)
    assert ("privileges", "timeservice", "ops.read") not in w.core.calls


# --------------------------------------------------------------------------- escalation


def test_fetch_failure_escalates_once_then_rearms_after_recovery(world: World) -> None:
    w = world
    first_release(w)
    w.mirror.fail = "git fetch failed (rc=128): could not read from remote"
    rep = w.tick()
    assert rep.exit_code == 1
    rep = w.tick()
    assert rep.exit_code == 1
    assert w.kinds() == ["core_fetch_failed"]
    w.mirror.fail = None
    assert w.tick().exit_code == 0
    w.mirror.fail = "git fetch failed (rc=128): could not read from remote"
    w.tick()
    assert w.kinds() == ["core_fetch_failed", "core_fetch_failed"]


def test_core_unreachable_after_release_escalates_once(world: World) -> None:
    w = world
    first_release(w)
    w.core.up = False
    rep = w.tick()
    assert rep.exit_code == 1
    rep = w.tick()
    assert w.kinds() == ["core_unreachable"]


def test_escalation_records_are_jsonl_with_the_existing_envelope(world: World) -> None:
    w = world
    first_release(w)
    w.mirror.fail = "boom"
    w.tick()
    (rec,) = w.escalations()
    assert rec["kind"] == coresync.ESCALATION_KIND
    assert rec["service_id"] == "core"
    assert rec["action"] == "escalate"
    assert rec["reason"].startswith("core_fetch_failed: ")
    assert set(rec["event"]) >= {"kind", "service", "plugin", "cause", "sha"}


def test_gateway_restarts_caddy_only_when_declared_and_changed(world: World) -> None:
    w = world
    caddy = w.state.service_decl_path("caddy")
    caddy.parent.mkdir(parents=True)
    caddy.write_text('id = "caddy"\n', encoding="utf-8")
    w.sup.services["caddy"] = {"status": "running"}
    first_release(w)
    assert ("restart", "caddy") in w.sup.names()
    n = len(w.sup.ops)
    w.tick()
    assert ("restart", "caddy") not in w.sup.names()[n:]


def test_declaration_change_without_release_reloads(world: World) -> None:
    w = world
    first_release(w)
    w.cfg = replace(w.cfg, memory_max="1G")
    n = len(w.sup.ops)
    rep = w.tick()
    assert rep.exit_code == 0
    assert ("reload", None) in w.sup.names()[n:]
    assert 'memory_max = "1G"' in w.state.service_decl_path("core").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- ship command


def test_ship_force_redeploys_an_unchanged_plugin(world: World) -> None:
    w = world
    first_release(w)
    uploads = len(w.core.ops("upload"))
    rep = coresync.ship(
        w.state,
        w.store,
        w.cfg,
        ["timeservice"],
        force=True,
        isolation=False,
        escalation=w.stream,
        now=w.clock.now,
        sleep=w.clock.sleep,
        hooks=w.hooks(),
    )
    assert rep.shipped == ("timeservice",)
    assert rep.live == ("timeservice",)
    assert len(w.core.ops("upload")) == uploads + 1


def test_ship_without_force_skips_unchanged(world: World) -> None:
    w = world
    first_release(w)
    rep = coresync.ship(
        w.state,
        w.store,
        w.cfg,
        ["timeservice"],
        isolation=False,
        escalation=w.stream,
        now=w.clock.now,
        sleep=w.clock.sleep,
        hooks=w.hooks(),
    )
    assert rep.shipped == ()
    assert rep.exit_code == 0


def test_ship_rejects_ids_outside_the_roster(world: World) -> None:
    w = world
    first_release(w)
    rep = coresync.ship(
        w.state,
        w.store,
        w.cfg,
        ["nope"],
        isolation=False,
        escalation=w.stream,
        now=w.clock.now,
        sleep=w.clock.sleep,
        hooks=w.hooks(),
    )
    assert rep.exit_code == 1
    assert "roster" in (rep.error or "")


# --------------------------------------------------------------------------- pieces


def test_parse_plan_output() -> None:
    text = "\n".join(
        [
            "some stray line",
            json.dumps(
                {
                    "pluginId": "health",
                    "dir": "/t/plugins/health",
                    "artifactId": "1" * 64,
                    "contentKey": "2" * 64,
                    "path": "/o/h.json",
                    "commit": SHA_A,
                    "dirty": False,
                }
            ),
            json.dumps({"pluginId": "x", "error": "boom"}),
            json.dumps([1, 2]),
            "",
        ]
    )
    plans = coresync.parse_plan_output(text)
    assert [p.plugin_id for p in plans] == ["health", "x"]
    assert plans[0].content_key == "2" * 64 and plans[0].error is None
    assert plans[1].error == "boom"


def test_parse_plan_output_rejects_incomplete_success_line() -> None:
    plans = coresync.parse_plan_output(json.dumps({"pluginId": "health", "artifactId": "1" * 64}))
    assert plans[0].error and "incomplete" in plans[0].error


def test_record_corrupt_is_refused(world: World) -> None:
    p = core.record_path(world.state)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json", encoding="utf-8")
    rep = world.tick()
    assert rep.exit_code == 1
    assert "core.json" in (rep.error or "")


def test_tick_lock_refuses_a_concurrent_run(world: World) -> None:
    with coresync.locked(world.state):
        rep = world.tick()
    assert rep.exit_code == 1
    assert "another" in (rep.error or "")


def test_old_release_trees_are_garbage_collected(world: World) -> None:
    w = world
    first_release(w)
    for sha in (SHA_B, SHA_C):
        _core_change(w, sha, base=w.record()["release_sha"])
        w.healthy.add(sha)
        assert w.tick().release_ok
    releases = sorted(p.name for p in w.layout.releases.iterdir())
    assert releases == sorted([SHA_B, SHA_C])  # A is neither release, previous nor staged


def test_harness_unreachable_before_release_is_retried(world: World) -> None:
    w = world

    def down() -> dict[str, Any]:
        raise coresync.SupervisorError("no harness listening")

    real = w.sup.status
    w.sup.status = down  # type: ignore[method-assign]
    rep = w.tick()
    assert rep.exit_code == 1
    assert w.kinds() == ["core_release_failed"]
    assert w.record()["release_failed_sha"] is None
    assert w.provisioned == [(SHA_A, False)]  # the expensive build never ran
    w.sup.status = real  # type: ignore[method-assign]
    rep = w.tick()
    assert rep.release_ok is True


def test_bundle_reimport_is_placed_without_a_release(world: World, tmp_path: Path) -> None:
    w = world
    first_release(w)
    bundle = tmp_path / "plugins.json"
    bundle.write_text(
        json.dumps(
            {"plugins": {"gateway": {"config": {"port": 18080}}}, "redactionReaders": ["x"]}
        ),
        encoding="utf-8",
    )
    core.import_bundle(w.state, bundle, cfg=w.cfg)
    rep = w.tick()
    assert rep.exit_code == 0
    placed = json.loads((w.layout.etc / "plugins.json").read_text(encoding="utf-8"))
    assert placed["redactionReaders"] == ["x"]


def test_bootstrap_declares_caddy_before_the_first_tick(world: World) -> None:
    w = world
    rep = coresync.bootstrap(
        w.state,
        w.store,
        w.cfg,
        isolation=False,
        escalation=w.stream,
        now=w.clock.now,
        sleep=w.clock.sleep,
        hooks=w.hooks(),
    )
    assert rep.exit_code == 0, rep
    caddy = w.state.service_decl_path("caddy").read_text(encoding="utf-8")
    assert "\nmain = 20180\n" in caddy
    assert w.gateway_calls >= 1
    # caddy's reload came first, core's declaration with the first release
    assert w.sup.ops[0][:2] == ("reload", None)
    assert w.state.service_decl_path("core").is_file()
    assert rep.live == tuple(ROSTER)


def test_privileges_are_granted_before_probation_judges_a_new_plugin(world: World) -> None:
    # health without ops.read fails every /health probe: it must get it first.
    w = world
    w.core.needs["health"] = "ops.read"
    w.core.settle_after = 2
    rep = first_release(w)
    assert rep.exit_code == 0, rep
    assert "health" in rep.live
    aid_to_pid = {aid: pid for pid, aid in w.registry.by_path.values()}
    calls = [
        ("deploy", aid_to_pid[c[1]]) if c[0] == "deploy" else c
        for c in w.core.calls
        if c[0] in ("deploy", "privileges", "restart")
    ]
    grant = calls.index(("privileges", "health", "ops.read"))
    assert calls[grant - 1] == ("deploy", "health")
    assert calls[grant + 1] == ("restart", "health")
    assert calls[grant + 2] == ("deploy", "timeservice")  # before the next plugin ships


def test_without_the_early_grant_health_would_fail(world: World) -> None:
    """Guards the fake: a plugin needing a privilege it lacks does fail probation."""
    w = world
    w.core.needs["timeservice"] = "ops.read"  # timeservice has no configured privileges
    rep = first_release(w)
    assert rep.failed == ("timeservice",)
