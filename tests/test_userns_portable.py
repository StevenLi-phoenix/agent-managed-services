"""Portable tests for the isolation layer: pure logic, no namespaces, any OS.

Everything that touches /proc, /sys/fs/cgroup or fork lives in tests/linux.
What is checked here is the part that is easy to get wrong and expensive to
debug remotely: map argument order, the rm -rf safety rails, cgroup root
discovery, and the control-file writes.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ams import schema
from ams.cgroup import (
    HARNESS_LEAF,
    CgroupRoot,
    CgroupUnavailable,
    ServiceCgroup,
    own_cgroup,
)
from ams.hostcheck import CheckResult, blocked_ancestors, parse_subid
from ams.isolated import IsolatedSpawner
from ams.spawn import SpawnRequest
from ams.uidmap import UidBlock
from ams.userns import (
    MapError,
    SpawnError,
    admin_map_args,
    ensure_service_root,
    remove_service_root,
    write_maps,
)

BLOCK = UidBlock(100_000, 100_000, 1024)


# --------------------------------------------------------------------------- maps


def test_runtime_map_args_are_inner_host_count() -> None:
    assert BLOCK.newuidmap_args() == ["1000", "100000", "1024"]
    assert BLOCK.newgidmap_args() == ["1000", "100000", "1024"]


def test_admin_map_args_map_inner_root_to_the_harness_uid() -> None:
    uid_args, gid_args = admin_map_args(BLOCK, harness_uid=1000, harness_gid=1000)
    assert uid_args == ["0", "1000", "1", "1000", "100000", "1024"]
    assert gid_args == ["0", "1000", "1", "1000", "100000", "1024"]


def test_admin_map_never_maps_the_harness_uid_to_the_service_identity() -> None:
    uid_args, _ = admin_map_args(UidBlock(200_000, 200_000), harness_uid=1000, harness_gid=1000)
    # inner 1000 must come from the block, not from the harness uid: otherwise a
    # service-owned file would be indistinguishable from a harness-owned one.
    assert uid_args[3:] == ["1000", "200000", "1024"]


def test_write_maps_invokes_both_helpers_with_the_pid_first(monkeypatch) -> None:
    calls: list[list[str]] = []

    class Done:
        returncode = 0
        stderr = b""

    monkeypatch.setattr(
        "ams.userns.subprocess.run", lambda cmd, **kw: (calls.append(cmd), Done())[1]
    )
    write_maps(4242, ["1000", "100000", "1024"], ["1000", "100000", "1024"])
    assert calls == [
        ["newgidmap", "4242", "1000", "100000", "1024"],
        ["newuidmap", "4242", "1000", "100000", "1024"],
    ]


def test_write_maps_raises_map_error_with_helper_stderr(monkeypatch) -> None:
    class Failed:
        returncode = 1
        stderr = b"newuidmap: uid range [0-1) not allowed\n"

    monkeypatch.setattr("ams.userns.subprocess.run", lambda cmd, **kw: Failed())
    with pytest.raises(MapError, match="not allowed"):
        write_maps(1, ["0", "0", "1"], ["0", "0", "1"])


# --------------------------------------------------------------------------- rm rails


@pytest.mark.parametrize(
    "bad",
    ["relative/path", "/", "/etc", "/home/../etc/passwd"],
)
def test_remove_service_root_refuses_dangerous_paths(bad: str, monkeypatch) -> None:
    monkeypatch.setattr(
        "ams.userns.run_admin", lambda *a, **k: pytest.fail("run_admin must not be reached")
    )
    with pytest.raises(ValueError):
        remove_service_root(Path(bad), BLOCK)


def test_remove_service_root_is_a_noop_when_the_tree_is_already_gone(monkeypatch) -> None:
    monkeypatch.setattr(
        "ams.userns.run_admin", lambda *a, **k: pytest.fail("run_admin must not be reached")
    )
    remove_service_root(Path("/nonexistent/ams/service-root"), BLOCK)


def test_ensure_service_root_rejects_a_relative_root() -> None:
    with pytest.raises(ValueError, match="absolute"):
        ensure_service_root(Path("services/x"), BLOCK)


def test_ensure_service_root_rejects_subdirs_outside_the_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not inside"):
        ensure_service_root(tmp_path / "root", BLOCK, subdirs=(tmp_path / "elsewhere",))


# --------------------------------------------------------------------------- hostcheck


def test_check_result_is_a_frozen_triple() -> None:
    r = CheckResult("subuid", True, "harness: 100000:65536")
    assert (r.name, r.ok, r.detail) == ("subuid", True, "harness: 100000:65536")
    with pytest.raises(AttributeError):
        r.ok = False  # type: ignore[misc]


def test_parse_subid_picks_only_the_requested_user() -> None:
    text = (
        "root:0:1\nharness:100000:65536\nharness:200000:1024\nother:300000:1\n#comment\nbad line\n"
    )
    assert parse_subid(text, "harness") == [(100_000, 65_536), (200_000, 1024)]
    assert parse_subid(text, "nobody") == []


def test_blocked_ancestors_finds_a_home_that_service_uids_cannot_traverse(
    tmp_path: Path,
) -> None:
    """0750 on the harness home is what broke absolute paths on the target box.

    Asserts membership rather than the whole list: the OS temp dir itself is
    0700 on macOS, so there are legitimately other blocked ancestors here.
    """
    home = tmp_path / "home" / "harness"
    state, store = home / "state", home / "store"
    store.mkdir(parents=True)
    state.mkdir()
    try:
        home.chmod(0o750)
        assert home in blocked_ancestors(state, store)
        home.chmod(0o711)
        assert home not in blocked_ancestors(state, store)
        assert state not in blocked_ancestors(state, store)
    finally:
        home.chmod(0o755)


def test_blocked_ancestors_orders_outermost_first_so_the_fix_is_the_useful_one(
    tmp_path: Path,
) -> None:
    outer = tmp_path / "outer"
    inner = outer / "inner"
    inner.mkdir(parents=True)
    try:
        outer.chmod(0o750)
        inner.chmod(0o750)
        found = [p for p in blocked_ancestors(inner) if p in (outer, inner)]
        assert found == [outer, inner]
    finally:
        outer.chmod(0o755)
        inner.chmod(0o755)


def test_blocked_ancestors_ignores_directories_that_do_not_exist_yet(tmp_path: Path) -> None:
    missing = tmp_path / "store-not-created-yet"
    assert missing not in blocked_ancestors(tmp_path / "state", missing)


# --------------------------------------------------------------------------- cgroup


def _fake_sysfs(tmp_path: Path, *, controllers: str = "cpu io memory pids") -> Path:
    sysfs = tmp_path / "cgroup"
    sysfs.mkdir()
    (sysfs / "cgroup.controllers").write_text(controllers)
    return sysfs


def _fake_delegated(tmp_path: Path, **kw) -> tuple[Path, Path, Path]:
    """A tmp tree shaped like a `systemd-run -p Delegate=yes` cgroup."""
    sysfs = _fake_sysfs(tmp_path, **kw)
    slice_dir = sysfs / "system.slice"
    slice_dir.mkdir()
    own = slice_dir / "run-u1.service"
    own.mkdir()
    (own / "cgroup.controllers").write_text(kw.get("controllers", "cpu io memory pids"))
    (own / "cgroup.subtree_control").write_text("")
    (own / "cgroup.procs").write_text("")
    proc = tmp_path / "proc-self-cgroup"
    proc.write_text("0::/system.slice/run-u1.service\n")
    return sysfs, own, proc


def test_own_cgroup_reads_the_unified_line(tmp_path: Path) -> None:
    proc = tmp_path / "cg"
    proc.write_text("0::/system.slice/run-u1.service\n")
    assert own_cgroup(Path("/sys/fs/cgroup"), proc) == Path(
        "/sys/fs/cgroup/system.slice/run-u1.service"
    )


def test_own_cgroup_rejects_a_v1_only_host(tmp_path: Path) -> None:
    proc = tmp_path / "cg"
    proc.write_text("6:memory:/user.slice\n3:cpu,cpuacct:/user.slice\n")
    with pytest.raises(CgroupUnavailable, match="no cgroup v2"):
        own_cgroup(Path("/sys/fs/cgroup"), proc)


def test_discover_fails_without_a_unified_hierarchy(tmp_path: Path) -> None:
    sysfs = tmp_path / "cgroup"
    sysfs.mkdir()
    proc = tmp_path / "cg"
    proc.write_text("0::/\n")
    with pytest.raises(CgroupUnavailable, match="cgroup.controllers is missing"):
        CgroupRoot.discover(sysfs, proc)


def test_discover_fails_when_nothing_is_delegated(tmp_path: Path) -> None:
    """A cgroup we can see but not control (an interactive ssh session)."""
    sysfs = _fake_sysfs(tmp_path)
    own = sysfs / "user.slice"
    own.mkdir()  # no cgroup.subtree_control -> not ours
    proc = tmp_path / "cg"
    proc.write_text("0::/user.slice\n")
    with pytest.raises(CgroupUnavailable, match="not delegated"):
        CgroupRoot.discover(sysfs, proc)


def test_discover_moves_us_into_a_leaf_when_we_sit_on_the_root(tmp_path: Path) -> None:
    sysfs, own, proc = _fake_delegated(tmp_path)
    root = CgroupRoot.discover(sysfs, proc)
    assert root.path == own
    leaf = own / HARNESS_LEAF
    assert leaf.is_dir()
    assert leaf.joinpath("cgroup.procs").read_text() == str(os.getpid())


def test_discover_is_idempotent(tmp_path: Path) -> None:
    sysfs, own, proc = _fake_delegated(tmp_path)
    first = CgroupRoot.discover(sysfs, proc)
    second = CgroupRoot.discover(sysfs, proc)
    assert first.path == second.path == own


def test_discover_keeps_the_root_when_systemd_already_made_a_subgroup(tmp_path: Path) -> None:
    """DelegateSubgroup=harness: our pid is already in a leaf, do not descend."""
    sysfs, own, _ = _fake_delegated(tmp_path)
    leaf = own / HARNESS_LEAF
    leaf.mkdir()
    (leaf / "cgroup.subtree_control").write_text("")
    (leaf / "cgroup.procs").write_text("")
    proc = tmp_path / "proc2"
    proc.write_text(f"0::/system.slice/run-u1.service/{HARNESS_LEAF}\n")
    root = CgroupRoot.discover(sysfs, proc)
    assert root.path == own


def test_enable_controllers_skips_absent_ones_and_fails_loudly_for_memory(tmp_path: Path) -> None:
    sysfs, own, proc = _fake_delegated(tmp_path, controllers="cpu pids")
    root = CgroupRoot.discover(sysfs, proc)
    with pytest.raises(CgroupUnavailable, match="memory"):
        root.enable_controllers(("cpu", "memory", "pids"))


def test_enable_controllers_tolerates_a_missing_cpu_controller(tmp_path: Path) -> None:
    sysfs, own, proc = _fake_delegated(tmp_path, controllers="memory pids")
    root = CgroupRoot.discover(sysfs, proc)
    assert root.enable_controllers(("cpu", "memory", "pids")) == ("memory", "pids")
    assert (own / "cgroup.subtree_control").read_text() == "+pids"


def test_service_cgroup_writes_only_the_declared_limits(tmp_path: Path) -> None:
    sysfs, own, proc = _fake_delegated(tmp_path)
    root = CgroupRoot.discover(sysfs, proc)
    cg = ServiceCgroup.create(root, "web")
    assert cg.path == own / "svc-web"
    written = cg.apply_limits(schema.LimitsSpec(memory_max="256M", cpu_max="50%"))
    assert written == {"memory.max": str(256 * 1024 * 1024), "cpu.max": "50000 100000"}
    assert not (cg.path / "pids.max").exists()
    # memory.swap.max is deliberately never touched.
    assert not (cg.path / "memory.swap.max").exists()


def test_service_cgroup_rejects_an_id_that_is_not_a_service_id(tmp_path: Path) -> None:
    sysfs, _, proc = _fake_delegated(tmp_path)
    root = CgroupRoot.discover(sysfs, proc)
    for bad in ("../escape", "Web", "", "a" * 40):
        with pytest.raises(ValueError):
            ServiceCgroup.create(root, bad)


def test_service_cgroup_reports_empty_when_events_say_unpopulated(tmp_path: Path) -> None:
    sysfs, _, proc = _fake_delegated(tmp_path)
    root = CgroupRoot.discover(sysfs, proc)
    cg = ServiceCgroup.create(root, "web")
    (cg.path / "cgroup.events").write_text("populated 0\nfrozen 0\n")
    assert cg.populated() is False
    assert cg.wait_empty(0.1) is True
    (cg.path / "cgroup.events").write_text("populated 1\nfrozen 0\n")
    assert cg.populated() is True
    assert cg.wait_empty(0.05) is False


def test_service_cgroup_stats_skips_files_the_kernel_did_not_provide(tmp_path: Path) -> None:
    sysfs, _, proc = _fake_delegated(tmp_path)
    root = CgroupRoot.discover(sysfs, proc)
    cg = ServiceCgroup.create(root, "web")
    (cg.path / "memory.current").write_text("1048576\n")
    (cg.path / "cpu.stat").write_text("usage_usec 12345\nnr_periods 0\n")
    assert cg.stats() == {"memory.current": 1_048_576, "cpu.usage_usec": 12_345}


# --------------------------------------------------------------------------- spawner


def _decl(service_id: str, argv: list[str]) -> schema.ServiceDecl:
    return schema.from_dict({"id": service_id, "start": {"argv": argv}})


def test_isolated_spawner_reports_a_missing_binary_before_touching_anything(
    tmp_path: Path,
) -> None:
    """A typo in the declaration must not leave a cgroup or a forked child."""
    sysfs, own, proc = _fake_delegated(tmp_path)
    root = CgroupRoot.discover(sysfs, proc)
    spawner = IsolatedSpawner(root, lambda _id: BLOCK)
    req = SpawnRequest(
        decl=_decl("ghost", ["definitely-not-a-real-binary-xyz"]),
        root=tmp_path / "svcroot",
        ports={},
    )
    with pytest.raises(SpawnError, match="not found on PATH"):
        spawner.spawn(req)
    assert not (own / "svc-ghost").exists()
    assert not (tmp_path / "svcroot").exists()


def test_isolated_spawner_asks_the_allocator_for_the_service_block(tmp_path: Path) -> None:
    sysfs, _, proc = _fake_delegated(tmp_path)
    root = CgroupRoot.discover(sysfs, proc)
    seen: list[str] = []

    def blocks(service_id: str) -> UidBlock:
        seen.append(service_id)
        return BLOCK

    spawner = IsolatedSpawner(root, blocks)
    req = SpawnRequest(decl=_decl("api", ["no-such-binary-zzz"]), root=tmp_path / "r", ports={})
    with pytest.raises(SpawnError):
        spawner.spawn(req)
    assert seen == ["api"]
