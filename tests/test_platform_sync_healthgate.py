"""The health gate's retry rule for a service that is already failed.

Live finding, `.claude/state/pool-migration.md` step 6b: after the cutover a
steady-state tick took **3 min 2 s**. Two pool members (`resume`,
`secretsservice`) are dead for reasons no tick can fix, and each burned the full
90 s health deadline on *every* tick; with a 60 s timer the runs then overlapped
back to back. `--only` is not an escape hatch for them, because naming any
member of a pool selects the whole pool.

So a tick that changes nothing must not re-probe a service that already failed
its probe at this very commit. What counts as "something changed" is asserted
here rather than described: a moved commit, a rewritten declaration, a
re-declared pool, or the retry window elapsing.

Fixtures come from ``test_platform_sync.py`` and ``test_platform_sync_pool.py``
(same directory, no package ``__init__.py``).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from test_platform_sync import (  # noqa: F401 - several of these are fixtures, used by name
    _commit,
    env,
    make_cfg,
    read_state,
    registry,
    service_manifest,
    snapshot,
    upstream,
)
from test_platform_sync_pool import (  # noqa: F401 - fixtures again
    MEMBERS,
    POOL_ID,
    pool_env,
    stub_pool,
    upstream_pool,
)

from ams.platform import sync as sync_mod
from ams.platform.sync import sync
from ams.state import StateDir


class Clock:
    """A hand-wound clock, so the retry window is asserted and not waited on."""

    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def probes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every health probe the run makes, and never a 200: these are the dead."""
    seen: list[str] = []

    def never_healthy(_self: Any, url: str, _deadline: float, _interval: float) -> bool:
        seen.append(url)
        return False

    monkeypatch.setattr(sync_mod.RegistryClient, "wait_healthy", never_healthy)
    return seen


@pytest.fixture
def clock() -> Clock:
    return Clock()


def run_at(e: Any, src: Path, clock: Clock, **kw: Any) -> Any:
    return sync(e.state, e.store, make_cfg(src, e.registry, **kw), secrets=e.secrets, now=clock)


def probe_count(probes: list[str], port_owner_free_substring: str) -> int:
    return sum(1 for url in probes if port_owner_free_substring in url)


# --------------------------------------------------------------------------- standalone


def test_a_failed_service_is_not_re_probed_at_an_unchanged_commit(
    env: Any,  # noqa: F811
    upstream: Any,  # noqa: F811
    probes: list[str],
    clock: Clock,
) -> None:
    src, sha = upstream
    first = run_at(env, src, clock)
    assert set(first.failed) == {"alpha", "beta"}
    assert len(probes) == 2

    clock.advance(60.0)
    probes.clear()
    second = run_at(env, src, clock)

    assert probes == [], "a dead service was re-probed at a commit that did not move"
    assert set(second.failed) == {"alpha", "beta"}, "and it is still reported as failed"
    assert read_state(env.state)["services"]["alpha"]["sha"] == sha


def test_the_skipped_gates_are_counted_in_the_log(
    env: Any,  # noqa: F811
    upstream: Any,  # noqa: F811
    probes: list[str],
    clock: Clock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    src, _sha = upstream
    run_at(env, src, clock)

    clock.advance(60.0)
    with caplog.at_level(logging.INFO, logger="ams.platform.sync"):
        run_at(env, src, clock)

    lines = [r.getMessage() for r in caplog.records if "health gate" in r.getMessage()]
    assert lines, "a tick that skipped a gate has to say so"
    assert "2" in lines[0] and "alpha" in lines[0] and "beta" in lines[0]


def test_a_no_op_tick_over_a_failed_service_still_writes_nothing(
    env: Any,  # noqa: F811
    upstream: Any,  # noqa: F811
    probes: list[str],
    clock: Clock,
) -> None:
    src, _sha = upstream
    run_at(env, src, clock)
    clock.advance(60.0)
    run_at(env, src, clock)  # the tick that stamps nothing new

    before = snapshot(env.state.root)
    clock.advance(60.0)
    report = run_at(env, src, clock)

    assert not report.state_written
    assert snapshot(env.state.root) == before
    assert report.unchanged == report.ids


def test_the_gate_runs_again_once_the_retry_window_has_passed(
    env: Any,  # noqa: F811
    upstream: Any,  # noqa: F811
    probes: list[str],
    clock: Clock,
) -> None:
    src, _sha = upstream
    run_at(env, src, clock, failed_health_retry_s=900.0)

    clock.advance(899.0)
    probes.clear()
    run_at(env, src, clock, failed_health_retry_s=900.0)
    assert probes == []

    clock.advance(2.0)
    run_at(env, src, clock, failed_health_retry_s=900.0)
    assert len(probes) == 2, "a hand-fixed service must be able to heal on its own"


def test_the_gate_runs_again_when_the_commit_moves(
    env: Any,  # noqa: F811
    upstream: Any,  # noqa: F811
    probes: list[str],
    clock: Clock,
) -> None:
    src, _sha = upstream
    run_at(env, src, clock)

    _commit(
        src,
        {"services/alpha/service.yaml": service_manifest("alpha", port=9201, memory="256M")},
        "alpha bump",
    )
    clock.advance(60.0)
    probes.clear()
    report = run_at(env, src, clock)

    assert len(probes) == 1, "the service whose declaration was rewritten is re-probed"
    assert "declare" in report.outcome("alpha").actions


def test_a_healthy_service_is_still_skipped_exactly_as_before(
    env: Any,  # noqa: F811
    upstream: Any,  # noqa: F811
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rule this change must not touch: an unchanged healthy service costs
    no registry round trip and no probe."""
    seen: list[str] = []
    real = sync_mod.RegistryClient.wait_healthy

    def counted(self: Any, url: str, deadline: float, interval: float) -> bool:
        seen.append(url)
        return bool(real(self, url, deadline, interval))

    monkeypatch.setattr(sync_mod.RegistryClient, "wait_healthy", counted)
    src, _sha = upstream
    first = run_at(env, src, clock)
    assert first.ok and len(seen) == 2

    clock.advance(60.0)
    seen.clear()
    second = run_at(env, src, clock)
    assert seen == []
    assert second.unchanged == second.ids


def test_a_failure_at_register_still_retries_every_tick(
    env: Any,  # noqa: F811
    upstream: Any,  # noqa: F811
    clock: Clock,
) -> None:
    """Only the *health* transition is held off. A registry that was down is a
    transient the next tick should retry, and its record says which transition
    failed."""
    env.registry.set_services_status(500)
    src, _sha = upstream
    run_at(env, src, clock)
    before = len(env.registry.posts("/api/services"))
    assert before >= 2

    clock.advance(60.0)
    report = run_at(env, src, clock)

    assert len(env.registry.posts("/api/services")) > before
    assert (report.outcome("alpha").error or "").startswith("register:")


# --------------------------------------------------------------------------- pooled


def test_a_failed_pool_member_is_not_re_probed_but_a_moved_pool_re_gates_it(
    pool_env: Any,  # noqa: F811
    upstream_pool: Any,  # noqa: F811
    stub_pool: None,  # noqa: F811
    probes: list[str],
    clock: Clock,
) -> None:
    """The live case: two dead members inside a pool that `--only` cannot
    exclude, and the pool's own commit is what re-gates them."""
    src, _sha = upstream_pool
    first = run_at(pool_env, src, clock)
    assert set(first.failed) == {POOL_ID, *MEMBERS, "solo"}
    assert len(probes) == 4, "the pool's admin port, one per member, plus the unpooled one"

    clock.advance(60.0)
    probes.clear()
    run_at(pool_env, src, clock)
    assert probes == []

    # A commit under one member moves the whole pool, so every member is worth
    # re-probing: they share one process and it was just restarted.
    _commit(
        src,
        {"services/beta/service.yaml": service_manifest("beta", port=9202, memory="256M")},
        "beta bump",
    )
    clock.advance(60.0)
    probes.clear()
    run_at(pool_env, src, clock)
    # Three: the pool's admin port and both members. `solo` is unaffected by
    # that commit (D26) and still held, so it is not probed again.
    assert len(probes) == 3


def test_a_docs_only_commit_does_not_re_gate_a_dead_pool_member(
    pool_env: Any,  # noqa: F811
    upstream_pool: Any,  # noqa: F811
    stub_pool: None,  # noqa: F811
    probes: list[str],
    clock: Clock,
) -> None:
    src, _sha = upstream_pool
    run_at(pool_env, src, clock)

    _commit(src, {"README.md": "api, documented\n"}, "docs only")
    clock.advance(60.0)
    probes.clear()
    report = run_at(pool_env, src, clock)

    assert probes == []
    assert not report.state_written


def test_the_failed_stamp_is_on_the_record_and_survives_a_rewrite(
    env: Any,  # noqa: F811
    upstream: Any,  # noqa: F811
    probes: list[str],
    clock: Clock,
) -> None:
    src, _sha = upstream
    run_at(env, src, clock)

    services = read_state(env.state)["services"]
    assert services["alpha"]["health_failed_at"].endswith("Z")
    assert "health_failed_at" not in services["site"], "unset keys stay off the record"

    stamped = services["alpha"]["health_failed_at"]
    clock.advance(60.0)
    run_at(env, src, clock)
    assert read_state(env.state)["services"]["alpha"]["health_failed_at"] == stamped


def _decl_exists(state: StateDir, service_id: str) -> bool:
    return state.service_decl_path(service_id).is_file()


def test_the_record_still_names_the_health_failure_after_a_skipped_tick(
    env: Any,  # noqa: F811
    upstream: Any,  # noqa: F811
    probes: list[str],
    clock: Clock,
    capsys: Any,
) -> None:
    """Skipping the probe must not quietly move the record forward: it stays
    `failed`, keeps its reason, and is not escalated a second time."""
    src, _sha = upstream
    run_at(env, src, clock)
    first_out = capsys.readouterr().out
    assert first_out.count('"service_id": "alpha"') >= 1

    clock.advance(60.0)
    report = run_at(env, src, clock)

    record = read_state(env.state)["services"]["alpha"]
    assert record["stage"] == "failed"
    assert record["error"].startswith("health:")
    assert record["escalated"] is True
    assert report.escalations == ()
    assert record["sha"] == report.sha
    assert _decl_exists(env.state, "alpha")
