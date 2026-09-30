"""coresync regressions from the 2026-09-29 review: when a verdict may be retried.

Same fakes as ``test_platform_coresync`` (imported, not copied). Each test names
the finding it pins:

- correctness-1: a release that also changes core's declaration must still start core.
- correctness-2: a core-side refusal of the artifact bytes is a recorded verdict; a
  transport failure re-ships only that plugin, not the whole roster.
- correctness-3 / upstream-fit-4: a rejection about core's *state*
  (``dependency_unavailable`` ...) is retried once that state changes; a verdict is
  also retried when the core release or the config bundle it was judged under changes.
- correctness-4: while head's core change is held, nothing is built from head.
- correctness-7: a failed restart after a privilege grant is escalated and retried.
- correctness-8: a failed stage is held at its sha and the rest of the tick still runs.
- correctness-9 / upstream-fit-5: plugin-only commits do not pile up release trees.
- upstream-fit-2: a plugin core cannot read (``artifact_unreadable``) is reinstalled.
- upstream-fit-3: the release gate catches a regression even when /health was red.
- upstream-fit-7: ``corectl gc`` runs after a tick that shipped.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from test_platform_coresync import (  # noqa: F401 - `world` is a fixture, used by name
    ROSTER,
    SHA_A,
    SHA_B,
    SHA_C,
    SHA_D,
    World,
    _core_change,
    first_release,
    world,
)

from ams.platform import core, coresync
from ams.platform.corectl import CoreControlError


def _plugin_change(w: World, sha: str, *pids: str, base: str = SHA_A, tag: str = "2") -> None:
    w.mirror.head = sha
    w.mirror.diffs[(base, sha)] = [f"plugins/{pid}/index.ts" for pid in pids]
    for pid in pids:
        w.keys[pid] = f"ck-{pid}-{tag}"
    w.planner.keys = dict(w.keys)


def _uploads_of(w: World, pid: str) -> int:
    return sum(1 for c in w.core.ops("upload") if w.registry.by_path[c[1]][0] == pid)


def _rollback(w: World) -> coresync.CoreReport:
    return coresync.rollback_release(
        w.state,
        w.store,
        w.cfg,
        isolation=False,
        escalation=w.stream,
        now=w.clock.now,
        sleep=w.clock.sleep,
        hooks=w.hooks(),
    )


# ------------------------------------------------------------------ correctness-1


def test_release_with_a_declaration_change_starts_core(world: World) -> None:  # noqa: F811
    w = world
    first_release(w)
    _core_change(w, SHA_B)
    w.healthy.add(SHA_B)
    w.cfg = replace(w.cfg, memory_max="1G")  # core.toml edited in the same tick
    rep = w.tick()
    assert rep.release_ok is True, (rep, w.kinds())
    assert w.sup.services["core"]["status"] == "running"
    assert core.current_sha(w.layout) == SHA_B
    rec = w.record()
    assert rec["release_sha"] == SHA_B
    assert rec["release_failed_sha"] is None
    assert w.kinds() == []


def test_rollback_with_a_declaration_change_starts_core(world: World) -> None:  # noqa: F811
    w = world
    first_release(w)
    _core_change(w, SHA_B)
    w.healthy.add(SHA_B)
    assert w.tick().release_ok
    w.cfg = replace(w.cfg, log_level="debug")
    rep = _rollback(w)
    assert rep.exit_code == 0, (rep, w.kinds())
    assert w.sup.services["core"]["status"] == "running"
    assert core.current_sha(w.layout) == SHA_A


# ------------------------------------------------------------------ correctness-2


def test_core_side_artifact_refusal_is_recorded_and_not_replanned(world: World) -> None:  # noqa: F811
    w = world
    first_release(w)
    _plugin_change(w, SHA_B, "timeservice")
    w.core.upload_error["timeservice"] = CoreControlError(
        "corectl upload failed (rc=1): corectl: invalid_manifest: health is not provided",
        code="invalid_manifest",
    )
    rep = w.tick()
    assert rep.failed == ("timeservice",)
    assert w.kinds() == ["core_plugin_rejected"]
    rec = w.record()
    assert rec["plugins"]["timeservice"]["outcome"] == "failed"
    assert rec["plugins"]["timeservice"]["content_key"] == "ck-timeservice-2"
    assert rec["planned_sha"] == SHA_B
    plans = len(w.planner.calls)
    rep = w.tick()
    assert rep.exit_code == 0
    assert len(w.planner.calls) == plans  # no re-plan of the roster
    assert w.kinds() == ["core_plugin_rejected"]


def test_transport_failure_reships_only_that_plugin(world: World) -> None:  # noqa: F811
    w = world
    first_release(w)
    _plugin_change(w, SHA_B, "timeservice", "auth")
    w.core.upload_error["timeservice"] = CoreControlError(
        "corectl upload failed (rc=1): corectl: connect ECONNREFUSED"
    )
    rep = w.tick()
    assert rep.exit_code == 1
    assert "core_ship_error" in w.kinds()
    assert "auth" in rep.live
    rec = w.record()
    assert rec["planned_sha"] == SHA_B
    assert rec["plugins"]["timeservice"]["content_key"] == "ck-timeservice-1"
    del w.core.upload_error["timeservice"]
    rep = w.tick()
    assert w.planner.calls[-1]["ids"] == ["timeservice"]
    assert rep.shipped == ("timeservice",)
    assert rep.live == ("timeservice",)
    assert w.record()["ship_retry"] == []
    plans = len(w.planner.calls)
    w.tick()
    assert len(w.planner.calls) == plans


# ------------------------------------------------ correctness-3 / upstream-fit-4


def test_dependency_rejection_is_retried_once_core_state_changes(world: World) -> None:  # noqa: F811
    w = world
    first_release(w)
    _plugin_change(w, SHA_B, "timeservice")
    w.core.reject_reason["timeservice"] = "dependency_unavailable: clock"
    rep = w.tick()
    assert rep.failed == ("timeservice",)
    assert w.record()["plugins"]["timeservice"]["outcome"] == "blocked"
    # core unchanged: not retried, not escalated again, exit 0
    rep = w.tick()
    assert rep.shipped == () and rep.exit_code == 0
    assert _uploads_of(w, "timeservice") == 2
    # the provider appears (installed by hand here): the blocked plugin is retried
    del w.core.reject_reason["timeservice"]
    w.core.install("clock", "e" * 64)
    rep = w.tick()
    assert rep.shipped == ("timeservice",), (rep, w.kinds())
    assert rep.live == ("timeservice",)
    assert w.record()["plugins"]["timeservice"]["outcome"] == "live"


def test_generation_conflict_is_not_a_verdict_on_the_bytes(world: World) -> None:  # noqa: F811
    w = world
    first_release(w)
    _plugin_change(w, SHA_B, "timeservice")
    w.core.reject_reason["timeservice"] = "generation_conflict: expected g1, current g2"
    w.tick()
    assert w.record()["plugins"]["timeservice"]["outcome"] == "blocked"


def test_artifact_rejection_stays_failed(world: World) -> None:  # noqa: F811
    w = world
    first_release(w)
    _plugin_change(w, SHA_B, "timeservice")
    w.core.reject_reason["timeservice"] = "incompatible_contract: clock 1.x drops now"
    w.tick()
    assert w.record()["plugins"]["timeservice"]["outcome"] == "failed"
    del w.core.reject_reason["timeservice"]
    w.core.install("clock", "e" * 64)  # core state changes; the bytes did not
    rep = w.tick()
    assert rep.shipped == ()


def test_failed_plugin_is_retried_after_a_bundle_change(world: World, tmp_path: Path) -> None:  # noqa: F811
    w = world
    first_release(w)
    _plugin_change(w, SHA_B, "timeservice")
    w.core.behaviour["timeservice"] = "revert"  # e.g. its config section was missing
    w.tick()
    assert w.record()["plugins"]["timeservice"]["outcome"] == "failed"
    rep = w.tick()
    assert rep.shipped == ()
    w.core.behaviour["timeservice"] = "ok"
    bundle = tmp_path / "plugins.json"
    bundle.write_text(
        json.dumps(
            {"plugins": {"gateway": {"config": {"port": 18080}}, "timeservice": {"config": {}}}}
        ),
        encoding="utf-8",
    )
    core.import_bundle(w.state, bundle, cfg=w.cfg)
    rep = w.tick()
    assert rep.shipped == ("timeservice",), (rep, w.kinds())
    assert rep.live == ("timeservice",)


# ------------------------------------------------------------------ correctness-4


def test_held_core_release_ships_nothing_built_from_head(world: World) -> None:  # noqa: F811
    w = world
    first_release(w)
    _core_change(w, SHA_B)  # never healthy: the release fails and is held
    w.mirror.diffs[(SHA_A, SHA_B)].append("plugins/timeservice/index.ts")
    w.keys["timeservice"] = "ck-timeservice-2"
    w.planner.keys = dict(w.keys)
    rep = w.tick()
    assert rep.release_ok is False
    plans = len(w.planner.calls)
    uploads = len(w.core.ops("upload"))
    rep = w.tick()  # held at SHA_B
    assert not rep.released
    assert len(w.planner.calls) == plans
    assert len(w.core.ops("upload")) == uploads
    assert rep.exit_code == 0
    # a fix to core alone releases, and then the plugin ships
    _core_change(w, SHA_C)
    w.mirror.diffs[(SHA_A, SHA_C)].append("plugins/timeservice/index.ts")
    w.healthy.add(SHA_C)
    rep = w.tick()
    assert rep.release_ok is True
    assert "timeservice" in rep.live


def test_failed_plugin_is_retried_once_after_a_core_release(world: World) -> None:  # noqa: F811
    w = world
    first_release(w)
    _plugin_change(w, SHA_B, "timeservice")
    w.core.behaviour["timeservice"] = "revert"  # needs a core API the running core lacks
    w.tick()
    assert w.record()["plugins"]["timeservice"]["outcome"] == "failed"
    w.core.behaviour["timeservice"] = "ok"
    _core_change(w, SHA_C)
    w.mirror.diffs[(SHA_A, SHA_C)].append("plugins/timeservice/index.ts")
    w.healthy.add(SHA_C)
    rep = w.tick()
    assert rep.release_ok is True
    assert rep.shipped == ("timeservice",), (rep, w.kinds())
    assert w.record()["plugins"]["timeservice"]["outcome"] == "live"


# ------------------------------------------------------------------ correctness-7


def test_failed_restart_after_a_grant_is_escalated_and_retried(world: World) -> None:  # noqa: F811
    w = world
    first_release(w)
    w.core.plugins["health"]["desired"]["privileges"] = []
    w.core.restart_fail.add("health")
    rep = w.tick()
    assert rep.privileges_changed == ()
    assert "core_privileges_failed" in w.kinds()
    assert rep.exit_code == 0  # a privilege problem is escalated, not a failed tick
    restarts = len(w.core.ops("restart"))
    w.core.restart_fail.clear()
    rep = w.tick()
    assert len(w.core.ops("restart")) == restarts + 1
    assert rep.privileges_changed == ("health",)
    restarts = len(w.core.ops("restart"))
    w.tick()
    assert len(w.core.ops("restart")) == restarts  # settled


def test_failed_restart_in_grant_now_is_not_reported_granted(world: World) -> None:  # noqa: F811
    w = world
    w.core.restart_fail.add("health")
    rep = first_release(w)
    assert "health" not in rep.privileges_changed
    assert "core_privileges_failed" in w.kinds()


# ------------------------------------------------------------------ correctness-8


def test_stage_failure_is_held_and_the_rest_of_the_tick_runs(world: World) -> None:  # noqa: F811
    w = world
    first_release(w)
    # a probation left pending by the previous tick
    _plugin_change(w, SHA_B, "timeservice")
    w.core.behaviour["*"] = "slow"
    assert w.tick().pending == ("timeservice",)
    del w.core.behaviour["*"]
    # the next commit cannot be staged
    _plugin_change(w, SHA_C, "auth", base=SHA_A)
    w.provision_fail[False] = "ERR_PNPM_OUTDATED_LOCKFILE"
    gateway_calls = w.gateway_calls
    rep = w.tick()
    assert rep.exit_code == 1
    assert w.kinds() == ["core_stage_failed"]
    assert rep.live == ("timeservice",)  # the pending probation was judged
    assert w.gateway_calls == gateway_calls + 1
    assert w.record()["stage_failed_sha"] == SHA_C
    installs = len(w.provisioned)
    rep = w.tick()  # same head: not re-provisioned, not re-escalated, exit 0
    assert len(w.provisioned) == installs
    assert rep.exit_code == 0
    assert w.kinds() == ["core_stage_failed"]
    # a new commit is staged normally
    del w.provision_fail[False]
    _plugin_change(w, SHA_D, "auth", base=SHA_A)
    rep = w.tick()
    assert rep.exit_code == 0, (rep, w.kinds())
    assert w.record()["staged_sha"] == SHA_D
    assert w.record()["stage_failed_sha"] is None


# ------------------------------------------------------- correctness-9 / upstream-fit-5


def test_plugin_only_commits_do_not_pile_up_release_trees(world: World) -> None:  # noqa: F811
    w = world
    first_release(w)
    _plugin_change(w, SHA_B, "timeservice")
    w.tick()
    _plugin_change(w, SHA_C, "timeservice", tag="3")
    w.tick()
    releases = sorted(p.name for p in w.layout.releases.iterdir())
    assert releases == sorted([SHA_A, SHA_C])


# ------------------------------------------------------------------ upstream-fit-2


def test_unreadable_artifact_is_reinstalled(world: World) -> None:  # noqa: F811
    # core.sqlite restored without R/data/artifacts: every desired row is unreadable.
    w = world
    first_release(w)
    w.core.boot_failed["timeservice"] = "artifact_unreadable"
    rep = w.tick()
    assert rep.shipped == ("timeservice",), (rep, w.kinds())
    assert rep.live == ("timeservice",)


def test_first_release_gate_tolerates_an_unreadable_gateway(world: World) -> None:  # noqa: F811
    # Fresh host after a restore: core.sqlite wants a gateway it cannot read.
    w = world
    w.core.install("gateway", "9" * 64)
    w.core.boot_failed["gateway"] = "artifact_unreadable"
    rep = w.tick()
    assert rep.release_ok is True, (rep, w.kinds())
    assert "gateway" in rep.live


# ------------------------------------------------------------------ upstream-fit-3


def test_release_gate_catches_a_regression_when_health_was_red(world: World) -> None:  # noqa: F811
    w = world
    first_release(w)
    w.healthy.discard(SHA_A)  # one plugin already failing: /health 503 before the stop
    _core_change(w, SHA_B)
    w.core.broken[SHA_B] = {"auth", "store"}  # the new core breaks two live plugins
    rep = w.tick()
    assert rep.release_ok is False
    assert rep.rolled_back
    assert core.current_sha(w.layout) == SHA_A
    assert w.record()["release_sha"] == SHA_A
    assert "core_release_failed" in w.kinds()


def test_release_gate_passes_when_the_live_plugins_come_back(world: World) -> None:  # noqa: F811
    w = world
    first_release(w)
    w.healthy.discard(SHA_A)
    _core_change(w, SHA_B)
    rep = w.tick()
    assert rep.release_ok is True, (rep, w.kinds())


# ------------------------------------------------------------------ upstream-fit-7


def test_gc_runs_after_a_tick_that_shipped(world: World) -> None:  # noqa: F811
    w = world
    first_release(w)
    assert w.core.ops("gc")
    n = len(w.core.ops("gc"))
    w.tick()  # nothing shipped
    assert len(w.core.ops("gc")) == n
    _plugin_change(w, SHA_B, "timeservice")
    w.tick()
    assert len(w.core.ops("gc")) == n + 1


def test_roster_is_unchanged_by_the_fakes() -> None:
    assert ROSTER[:5] == list(core.FOUNDATION)
