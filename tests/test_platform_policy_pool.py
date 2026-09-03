"""T7 (policy half): cause-key separation across pool members + pool-aware
crash-loop escalations (PLAN-pool §4.5, §5.7).

Two independent claims:

1. The pool runner tags every log line `LEVEL [<member>] <logger>: <msg>`
   (PLAN-pool §4.5) while `LogLine.service_id` is the pool's own id for every
   member. Two members' otherwise-identical error text must still produce
   *different* cause keys, or one member's error would silence the other's.
   PLAN-pool §5.7 says to assert what `normalize_cause_text` actually does to
   a leading `[<member>]` token *before* asserting the key separation, and to
   adjust the normalizer only if the assertion fails.
2. `_maybe_crash_loop` reads `pool_members` off the exited id's
   `platform/state.json` record (defensively -- that field is written by
   `ams.platform.sync`, not this module) and names the members in the
   escalation, so "pool-core exited" reads as "N services are down".

Reuses `state_dir`/`write_state`/`record`/`FakeClock`/`ctx_for`/`_epoch` from
`test_platform_policy.py` (same `tests/` dir, no package `__init__.py`, so it
collects as a top-level module already on `sys.path` -- same pattern as
`test_platform_gateway_pool.py`).
"""

from __future__ import annotations

from test_platform_policy import (
    FakeClock,
    _epoch,
    ctx_for,
    record,
    state_dir,
    write_state,
)

from ams.decision import Action
from ams.events import ServiceExited
from ams.platform.policy import DEFAULT_HEALTH_GRACE_S, cause_key, make_policy, normalize_cause_text

# --------------------------------------------------------- cause-key separation


def test_normalize_cause_text_does_not_touch_a_leading_member_tag() -> None:
    """The finding this task was asked to record: `normalize_cause_text` leaves
    a `[member]` bracket token untouched -- it is not numeric, hex-looking, a
    timestamp or an address, so none of the `_NORM_RULES` match it. Pinned here
    as a regression guard: if a future normalizer rule ever starts eating short
    bracketed words, this is the test that will catch it before it silently
    re-collapses every pool member's errors into one cause."""
    text = "ERROR [kvservice] kvservice.main: boom"
    assert normalize_cause_text(text) == text


def test_two_members_identical_error_text_produce_different_cause_keys() -> None:
    """The runner tags both members' error text identically apart from the
    bracket (PLAN-pool §4.5); because that tag survives normalization
    unchanged (see the test above), the two causes are already distinct under
    `cause_key` with no normalizer change needed -- both are logged against the
    pool's own service_id, so the tag in the text is the only thing that can
    tell them apart."""
    a = cause_key("pool-core", "LogLine", "ERROR [kvservice] kvservice.main: boom")
    b = cause_key("pool-core", "LogLine", "ERROR [timeservice] timeservice.main: boom")
    assert a != b


def test_same_member_same_text_still_collapses_to_one_cause() -> None:
    """Dedup must still work *within* one member: two occurrences of the exact
    same tagged line are one cause, not two."""
    a = cause_key("pool-core", "LogLine", "ERROR [kvservice] kvservice.main: boom")
    b = cause_key("pool-core", "LogLine", "ERROR [kvservice] kvservice.main: boom")
    assert a == b


# ------------------------------------------------------- pool-aware crash loop


def test_crash_loop_of_a_pool_names_its_members(tmp_path) -> None:  # noqa: ANN001
    state = state_dir(tmp_path)
    write_state(
        state,
        {
            "pool-core": {
                **record(stage="reloaded", updated_at="2026-09-02T13:00:00Z"),
                "pool_members": ["kvservice", "timeservice"],
            }
        },
    )
    clock = FakeClock(_epoch("2026-09-02T13:00:30Z"))
    policy = make_policy(state, clock=clock)
    ctx = ctx_for("pool-core", consecutive_failures=2)

    d = policy.decide(ServiceExited("pool-core", 1, 1, None, 0.4), ctx)
    assert d.action is Action.RESTART  # the delegated action is untouched

    emitted = policy.flush()
    assert len(emitted) == 1
    text = emitted[0].event.text
    assert "pool of 2: kvservice, timeservice" in text
    assert "rollback to prev_sha" in text  # existing behaviour, unchanged


def test_crash_loop_of_a_non_pooled_service_has_no_pool_suffix(tmp_path) -> None:  # noqa: ANN001
    """A record with no `pool_members` key at all -- every service today, and
    every non-pooled service after this change -- must escalate exactly as it
    did before this task (byte-identical to `test_platform_policy.py`'s own
    `test_crash_loop_after_a_sync_escalates_with_the_sha_pair`)."""
    state = state_dir(tmp_path)
    write_state(state, {"files": record(stage="reloaded", updated_at="2026-09-02T13:00:00Z")})
    clock = FakeClock(_epoch("2026-09-02T13:00:30Z"))
    policy = make_policy(state, clock=clock)
    ctx = ctx_for("files", consecutive_failures=2)

    policy.decide(ServiceExited("files", 1, 1, None, 0.4), ctx)
    emitted = policy.flush()

    assert len(emitted) == 1
    assert "pool of" not in emitted[0].event.text


def test_crash_loop_pool_members_field_with_junk_is_ignored(tmp_path) -> None:  # noqa: ANN001
    """Defensive read: a malformed `pool_members` (not a list, or a list of
    non-strings) must not raise and must not add a suffix."""
    state = state_dir(tmp_path)
    write_state(
        state,
        {
            "pool-core": {
                **record(stage="reloaded", updated_at="2026-09-02T13:00:00Z"),
                "pool_members": "kvservice",  # not a list
            }
        },
    )
    clock = FakeClock(_epoch("2026-09-02T13:00:30Z"))
    policy = make_policy(state, clock=clock)
    ctx = ctx_for("pool-core", consecutive_failures=2)

    policy.decide(ServiceExited("pool-core", 1, 1, None, 0.4), ctx)
    emitted = policy.flush()

    assert len(emitted) == 1
    assert "pool of" not in emitted[0].event.text


# ------------------------------------------------------------- health gate parity


def test_health_gate_is_unchanged_for_a_pool_member_record(tmp_path) -> None:  # noqa: ANN001
    """Members keep their own per-service `ServiceRecord`s (PLAN-pool §5.7 --
    `_health_gate` is unchanged); a member stuck past the grace period gates
    exactly as any other service would, `pool` key on its record or not."""
    state = state_dir(tmp_path)
    write_state(
        state,
        {
            "kvservice": {
                **record(stage="failed", error="boom"),
                "pool": "core",
            }
        },
    )
    clock = FakeClock(_epoch("2026-09-02T13:00:00Z") + DEFAULT_HEALTH_GRACE_S + 1)
    policy = make_policy(state, clock=clock)

    emitted = policy.flush()

    assert len(emitted) == 1
    assert "kvservice" in emitted[0].event.text
    assert "rollback to prev_sha" in emitted[0].event.text
