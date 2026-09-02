import json
from pathlib import Path

import pytest

from ams import uidmap as uidmap_module
from ams.state import StateCorrupt, write_json_atomic
from ams.uidmap import (
    BLOCK_SIZE,
    SubidRange,
    UidAllocator,
    UidBlock,
    UidExhausted,
    admin_map_args,
    parse_subid_file,
)

# --------------------------------------------------------------------------- parsing

SUBUID_TEXT = """
# comment line
harness:100000:65536

alice:165536:65536
this-is-malformed
harness:extra:field:too:many
1000:265536:65536
bob:notanumber:65536
"""


def test_parse_subid_file_basic_and_multiple_ranges():
    assert parse_subid_file(SUBUID_TEXT, "harness") == [(100000, 65536)]
    assert parse_subid_file(SUBUID_TEXT, "alice") == [(165536, 65536)]


def test_parse_subid_file_numeric_uid_key():
    assert parse_subid_file(SUBUID_TEXT, "1000") == [(265536, 65536)]


def test_parse_subid_file_skips_malformed_lines(caplog: pytest.LogCaptureFixture):
    with caplog.at_level("WARNING"):
        result = parse_subid_file(SUBUID_TEXT, "bob")
    assert result == []  # bob's line has a non-integer start, skipped
    assert any("subid line" in r.message for r in caplog.records)


def test_parse_subid_file_unknown_user_returns_empty():
    assert parse_subid_file(SUBUID_TEXT, "nobody") == []


def test_parse_subid_file_two_ranges_same_user():
    text = "svc:1000:100\nsvc:5000:100\n"
    assert parse_subid_file(text, "svc") == [(1000, 100), (5000, 100)]


def test_subid_range_end():
    r = SubidRange(start=100000, count=65536)
    assert r.end == 165536


# --------------------------------------------------------------------------- allocation


def _allocator(tmp_path: Path, count: int = 65536, block_size: int = BLOCK_SIZE) -> UidAllocator:
    uid_ranges = [SubidRange(100000, count)]
    gid_ranges = [SubidRange(100000, count)]
    return UidAllocator(uid_ranges, gid_ranges, tmp_path / "uidmap.json", block_size=block_size)


def test_allocate_order_and_alignment(tmp_path: Path):
    alloc = _allocator(tmp_path)
    b1 = alloc.allocate("svc-a")
    b2 = alloc.allocate("svc-b")
    assert b1 == UidBlock(uid_start=100000, gid_start=100000, size=BLOCK_SIZE)
    assert b2 == UidBlock(
        uid_start=100000 + BLOCK_SIZE, gid_start=100000 + BLOCK_SIZE, size=BLOCK_SIZE
    )


def test_allocate_idempotent(tmp_path: Path):
    alloc = _allocator(tmp_path)
    b1 = alloc.allocate("svc-a")
    b2 = alloc.allocate("svc-a")
    assert b1 == b2
    assert alloc.get("svc-a") == b1


def test_get_unknown_returns_none(tmp_path: Path):
    alloc = _allocator(tmp_path)
    assert alloc.get("nope") is None


def test_assignments_reflects_all(tmp_path: Path):
    alloc = _allocator(tmp_path)
    alloc.allocate("a")
    alloc.allocate("b")
    assert set(alloc.assignments()) == {"a", "b"}


def test_persistence_round_trip(tmp_path: Path):
    state_path = tmp_path / "uidmap.json"
    uid_ranges = [SubidRange(100000, 65536)]
    gid_ranges = [SubidRange(100000, 65536)]
    alloc1 = UidAllocator(uid_ranges, gid_ranges, state_path)
    b1 = alloc1.allocate("svc-a")

    alloc2 = UidAllocator(uid_ranges, gid_ranges, state_path)
    assert alloc2.get("svc-a") == b1
    # a fresh allocate for a new id continues past what's on disk, not from 0
    b2 = alloc2.allocate("svc-b")
    assert b2.uid_start == b1.uid_start + BLOCK_SIZE


def test_exhaustion_with_tiny_range(tmp_path: Path):
    alloc = _allocator(tmp_path, count=2 * BLOCK_SIZE)
    alloc.allocate("a")
    alloc.allocate("b")
    with pytest.raises(UidExhausted):
        alloc.allocate("c")


def test_release_then_reallocate_reuses_block(tmp_path: Path):
    alloc = _allocator(tmp_path, count=2 * BLOCK_SIZE)
    b_a = alloc.allocate("a")
    alloc.allocate("b")
    alloc.release("a")
    b_c = alloc.allocate("c")
    assert b_c == b_a  # freed lowest-index block reused


def test_release_unknown_is_noop(tmp_path: Path):
    alloc = _allocator(tmp_path)
    alloc.release("nope")  # must not raise


def test_block_size_mismatch_raises(tmp_path: Path):
    state_path = tmp_path / "uidmap.json"
    uid_ranges = [SubidRange(100000, 65536)]
    gid_ranges = [SubidRange(100000, 65536)]
    UidAllocator(uid_ranges, gid_ranges, state_path, block_size=1024).allocate("a")

    with pytest.raises(ValueError, match="block_size"):
        UidAllocator(uid_ranges, gid_ranges, state_path, block_size=2048)


def test_missing_state_file_tolerated(tmp_path: Path):
    alloc = _allocator(tmp_path)
    assert alloc.assignments() == {}


def test_from_host(tmp_path: Path):
    subuid = tmp_path / "subuid"
    subgid = tmp_path / "subgid"
    subuid.write_text("harness:100000:65536\n", encoding="utf-8")
    subgid.write_text("harness:100000:65536\n", encoding="utf-8")
    alloc = UidAllocator.from_host("harness", tmp_path / "state.json", subuid=subuid, subgid=subgid)
    b = alloc.allocate("svc")
    assert b.uid_start == 100000


def test_from_host_missing_uid_range_raises(tmp_path: Path):
    subuid = tmp_path / "subuid"
    subgid = tmp_path / "subgid"
    subuid.write_text("someoneelse:100000:65536\n", encoding="utf-8")
    subgid.write_text("harness:100000:65536\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="subuid"):
        UidAllocator.from_host("harness", tmp_path / "state.json", subuid=subuid, subgid=subgid)


def test_from_host_missing_gid_range_raises(tmp_path: Path):
    subuid = tmp_path / "subuid"
    subgid = tmp_path / "subgid"
    subuid.write_text("harness:100000:65536\n", encoding="utf-8")
    subgid.write_text("someoneelse:100000:65536\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="subgid"):
        UidAllocator.from_host("harness", tmp_path / "state.json", subuid=subuid, subgid=subgid)


def test_admin_map_args_exact_output():
    block = UidBlock(uid_start=100000, gid_start=100000, size=1024)
    uid_args, gid_args = admin_map_args(block, harness_uid=1000, harness_gid=1000)
    assert uid_args == ["0", "1000", "1", "1000", "100000", "1024"]
    assert gid_args == ["0", "1000", "1", "1000", "100000", "1024"]


def test_from_host_merges_numeric_uid_gid_keyed_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Some tools write /etc/subuid entries keyed by the numeric uid/gid
    instead of the username; from_host must match both and merge."""
    subuid = tmp_path / "subuid"
    subgid = tmp_path / "subgid"
    subuid.write_text("1000:100000:65536\n", encoding="utf-8")
    subgid.write_text("2000:200000:65536\n", encoding="utf-8")

    class FakePasswd:
        pw_uid = 1000
        pw_gid = 2000

    monkeypatch.setattr(uidmap_module.pwd, "getpwnam", lambda name: FakePasswd())

    alloc = UidAllocator.from_host("harness", tmp_path / "state.json", subuid=subuid, subgid=subgid)
    b = alloc.allocate("svc")
    assert b.uid_start == 100000
    assert b.gid_start == 200000


def test_from_host_no_passwd_entry_falls_back_to_name_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    subuid = tmp_path / "subuid"
    subgid = tmp_path / "subgid"
    subuid.write_text("harness:100000:65536\n", encoding="utf-8")
    subgid.write_text("harness:100000:65536\n", encoding="utf-8")

    def raise_keyerror(name: str):
        raise KeyError(name)

    monkeypatch.setattr(uidmap_module.pwd, "getpwnam", raise_keyerror)
    alloc = UidAllocator.from_host("harness", tmp_path / "state.json", subuid=subuid, subgid=subgid)
    assert alloc.allocate("svc").uid_start == 100000


# --------------------------------------------------------------------------- state corruption


def test_load_corrupt_json_raises_state_corrupt(tmp_path: Path):
    state_path = tmp_path / "uidmap.json"
    state_path.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(StateCorrupt, match="corrupt JSON"):
        _allocator_from_state(tmp_path, state_path)


def test_load_wrong_top_level_type_raises_state_corrupt(tmp_path: Path):
    state_path = tmp_path / "uidmap.json"
    state_path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    with pytest.raises(StateCorrupt):
        _allocator_from_state(tmp_path, state_path)


def test_load_blocks_wrong_type_raises_state_corrupt(tmp_path: Path):
    state_path = tmp_path / "uidmap.json"
    state_path.write_text(
        json.dumps({"version": 1, "block_size": BLOCK_SIZE, "blocks": "nope"}), encoding="utf-8"
    )
    with pytest.raises(StateCorrupt, match="'blocks'"):
        _allocator_from_state(tmp_path, state_path)


def test_load_block_missing_field_raises_state_corrupt(tmp_path: Path):
    state_path = tmp_path / "uidmap.json"
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "block_size": BLOCK_SIZE,
                "blocks": {"a": {"uid_start": 100000, "gid_start": 100000}},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(StateCorrupt, match="missing field"):
        _allocator_from_state(tmp_path, state_path)


def test_load_block_non_integer_field_raises_state_corrupt(tmp_path: Path):
    state_path = tmp_path / "uidmap.json"
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "block_size": BLOCK_SIZE,
                "blocks": {"a": {"uid_start": "100000", "gid_start": 100000, "size": BLOCK_SIZE}},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(StateCorrupt, match="not an integer"):
        _allocator_from_state(tmp_path, state_path)


def test_load_block_outside_range_raises_state_corrupt_naming_service(tmp_path: Path):
    state_path = tmp_path / "uidmap.json"
    write_json_atomic(
        state_path,
        {
            "version": 1,
            "block_size": BLOCK_SIZE,
            "blocks": {
                "orphan-svc": {"uid_start": 999999999, "gid_start": 100000, "size": BLOCK_SIZE}
            },
        },
    )
    with pytest.raises(StateCorrupt, match="orphan-svc"):
        _allocator_from_state(tmp_path, state_path)


def test_load_block_misaligned_raises_state_corrupt(tmp_path: Path):
    state_path = tmp_path / "uidmap.json"
    write_json_atomic(
        state_path,
        {
            "version": 1,
            "block_size": BLOCK_SIZE,
            # uid_start not a block_size-aligned offset from the range start
            "blocks": {"a": {"uid_start": 100001, "gid_start": 100000, "size": BLOCK_SIZE}},
        },
    )
    with pytest.raises(StateCorrupt, match="does not fit"):
        _allocator_from_state(tmp_path, state_path)


def test_subuid_range_moved_raises_state_corrupt_not_silent_recarve(tmp_path: Path):
    state_path = tmp_path / "uidmap.json"
    UidAllocator([SubidRange(100000, 65536)], [SubidRange(100000, 65536)], state_path).allocate("a")

    moved_ranges = [SubidRange(165536, 65536)]  # host's subuid start moved
    with pytest.raises(StateCorrupt, match="does not fit"):
        UidAllocator(moved_ranges, [SubidRange(100000, 65536)], state_path)


def test_gid_block_mismatched_uid_index_no_overlap_on_new_allocation(tmp_path: Path):
    """A stored block whose gid_start does not correspond to the same grid
    index as its uid_start is a legal (if unusual) placement -- each side
    independently fits its own range. Occupancy must still be computed by
    real interval overlap in both dimensions so a later allocation cannot
    collide with either side of it."""
    state_path = tmp_path / "uidmap.json"
    write_json_atomic(
        state_path,
        {
            "version": 1,
            "block_size": BLOCK_SIZE,
            "blocks": {
                "a": {
                    "uid_start": 100000,  # index 0
                    "gid_start": 100000 + BLOCK_SIZE,  # index 1
                    "size": BLOCK_SIZE,
                }
            },
        },
    )
    uid_ranges = [SubidRange(100000, 3 * BLOCK_SIZE)]
    gid_ranges = [SubidRange(100000, 3 * BLOCK_SIZE)]
    alloc = UidAllocator(uid_ranges, gid_ranges, state_path)

    b = alloc.allocate("b")
    a = alloc.get("a")
    assert a is not None
    assert not _intervals_overlap(b.uid_start, b.size, a.uid_start, a.size)
    assert not _intervals_overlap(b.gid_start, b.size, a.gid_start, a.size)


def _intervals_overlap(a_start: int, a_size: int, b_start: int, b_size: int) -> bool:
    return a_start < b_start + b_size and b_start < a_start + a_size


def _allocator_from_state(tmp_path: Path, state_path: Path) -> UidAllocator:
    uid_ranges = [SubidRange(100000, 65536)]
    gid_ranges = [SubidRange(100000, 65536)]
    return UidAllocator(uid_ranges, gid_ranges, state_path)


def test_allocate_sees_a_block_another_process_carved_after_load(tmp_path: Path) -> None:
    """The bug that broke the first live Layer-0 bring-up.

    The harness holds one allocator for its whole lifetime; `ams provision` and
    the platform bring-up allocate from a second process against the same file.
    Before the fix, the long-lived instance answered from a cache read at
    startup, carved a *different* block for an id the other process had already
    staged files under, and `ensure_service_root`'s recursive chown then ran in a
    namespace with no authority over those files. See
    `.claude/state/diagnosis-layer0.md`.
    """
    state_path = tmp_path / "uidmap.json"
    harness = _allocator_from_state(tmp_path, state_path)
    harness.allocate("hello")  # the harness's view is now warm

    other_process = _allocator_from_state(tmp_path, state_path)
    theirs = other_process.allocate("registry")

    assert harness.get("registry") is None, "precondition: the cache is stale"
    assert harness.allocate("registry") == theirs


def test_allocate_does_not_reuse_a_range_another_process_took(tmp_path: Path) -> None:
    """The re-read must also inform the free-block search, or a brand-new id
    gets handed a range that is already in use on disk."""
    state_path = tmp_path / "uidmap.json"
    harness = _allocator_from_state(tmp_path, state_path)
    harness.allocate("hello")

    other_process = _allocator_from_state(tmp_path, state_path)
    taken = other_process.allocate("registry")

    fresh = harness.allocate("caddy")
    assert not _intervals_overlap(fresh.uid_start, fresh.size, taken.uid_start, taken.size)
    assert not _intervals_overlap(fresh.gid_start, fresh.size, taken.gid_start, taken.size)


def test_allocate_does_not_reread_for_a_known_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Warm path stays free of I/O: every spawn calls allocate()."""
    state_path = tmp_path / "uidmap.json"
    alloc = _allocator_from_state(tmp_path, state_path)
    first = alloc.allocate("hello")

    def explode() -> None:
        raise AssertionError("allocate() re-read state for an id it already knew")

    monkeypatch.setattr(alloc, "_load", explode)
    assert alloc.allocate("hello") == first
