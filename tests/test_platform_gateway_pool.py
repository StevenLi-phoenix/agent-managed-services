"""Tests for `gateway.resolve_ports` honouring the pool `port_owner`/`port_name`
mount keys (PLAN-pool §3.3, §5.4).

A pooled member's mount sidecar gains two OPTIONAL keys: `port_owner` (the
allocator row that actually holds the port — the pool process id) and
`port_name` (the port name on that row). Absent/null on either means today's
behaviour: look the member's own id up under `"main"`. `_render_site`,
`_port_for` and every Caddyfile golden are untouched by this change — a pooled
member's rendered site block differs from its standalone rendering only in the
upstream port number (PLAN-pool §5.4).

Reuses `mount()`/`cfg()`/`FakeAllocator` from `test_platform_gateway.py`
(same `tests/` dir, no package `__init__.py`, so it collects as a top-level
module pytest has already put on `sys.path`).
"""

from __future__ import annotations

import pytest
import test_platform_gateway as base
from test_platform_gateway import FakeAllocator, cfg, mount

from ams.platform import gateway
from ams.platform.gateway import GatewayError

# --------------------------------------------------------------------------- #
# resolve_ports: port_owner / port_name
# --------------------------------------------------------------------------- #


def test_resolve_ports_with_owner_resolves_against_owners_allocation() -> None:
    """(a) A mount with port_owner+port_name resolves against the owner's row,
    not a row under the member's own id."""
    mounts = [mount("kvservice", path="/kv", port_name="kvservice", port_owner="pool-core")]
    alloc = FakeAllocator(
        {
            "pool-core": {"kvservice": 30011, "timeservice": 30012, "pool": 30010},
            # A stale/absent row under the member's own id must NOT be used.
            "kvservice": {"main": 9999},
        }
    )
    assert gateway.resolve_ports(mounts, alloc) == {"kvservice": 30011}


def test_resolve_ports_without_owner_matches_todays_behaviour() -> None:
    """(b) A mount without port_owner/port_name behaves byte-identically to
    today: looked up under the member's own id, "main" by default."""
    mounts = [mount("files", path="/files")]
    alloc = FakeAllocator({"files": {"main": 30001}})
    assert gateway.resolve_ports(mounts, alloc) == gateway.resolve_ports(
        [mount("files", path="/files", port_owner=None)], alloc
    )
    assert gateway.resolve_ports(mounts, alloc) == {"files": 30001}


def test_resolve_ports_null_owner_and_null_port_name_mean_self_and_main() -> None:
    """Explicit `null` for either key (not just absence) still means "my own
    id" / "main" — the sidecar writer may emit the keys with null values."""
    mounts = [mount("files", path="/files", port_owner=None, port_name=None)]
    alloc = FakeAllocator({"files": {"main": 30001}})
    assert gateway.resolve_ports(mounts, alloc) == {"files": 30001}


def test_resolve_ports_missing_port_on_owner_names_both_ids() -> None:
    """(c) A missing port on the owner raises GatewayError naming BOTH the
    member id and the owner id, and lists the available names."""
    mounts = [mount("kvservice", path="/kv", port_name="kvservice", port_owner="pool-core")]
    alloc = FakeAllocator({"pool-core": {"pool": 30010, "timeservice": 30012}})
    with pytest.raises(GatewayError) as exc_info:
        gateway.resolve_ports(mounts, alloc)
    message = str(exc_info.value)
    assert "kvservice" in message
    assert "pool-core" in message
    assert "'pool'" in message and "'timeservice'" in message


def test_resolve_ports_missing_owner_row_names_both_ids() -> None:
    """A pool that has not (yet) allocated anything reads as an empty
    mapping, which is the same "missing port" case — still names both ids."""
    mounts = [mount("kvservice", path="/kv", port_name="kvservice", port_owner="pool-core")]
    alloc = FakeAllocator({})
    with pytest.raises(GatewayError, match="kvservice.*pool-core"):
        gateway.resolve_ports(mounts, alloc)


def test_render_pooled_member_differs_from_standalone_only_in_port() -> None:
    """(d) The rendered site block for a pooled member differs from its
    standalone rendering ONLY in the upstream reverse_proxy port number."""
    standalone_mount = mount("kvservice", path="/kv")
    pooled_mount = mount(
        "kvservice", path="/kv", port_name="kvservice", port_owner="pool-core"
    )

    standalone_ports = {"kvservice": 30001}
    pooled_ports = gateway.resolve_ports(
        [pooled_mount],
        FakeAllocator({"pool-core": {"kvservice": 30011}}),
    )
    assert pooled_ports == {"kvservice": 30011}

    standalone_text, standalone_is_subdomain = gateway._render_site(
        standalone_mount, standalone_ports, cfg()
    )
    pooled_text, pooled_is_subdomain = gateway._render_site(pooled_mount, pooled_ports, cfg())

    assert standalone_is_subdomain == pooled_is_subdomain is False
    standalone_lines = standalone_text.splitlines()
    pooled_lines = pooled_text.splitlines()
    assert len(standalone_lines) == len(pooled_lines)

    diff_lines = [
        (a, b) for a, b in zip(standalone_lines, pooled_lines, strict=True) if a != b
    ]
    assert len(diff_lines) == 1, f"expected exactly one differing line, got: {diff_lines}"
    (only_a, only_b) = diff_lines[0]
    assert "reverse_proxy 127.0.0.1:30001" == only_a.strip()
    assert "reverse_proxy 127.0.0.1:30011" == only_b.strip()


def test_render_pooled_member_full_render_matches_standalone_shape() -> None:
    """Same check as above but through the public render() entry point, so it
    also exercises `resolve_ports` end to end for a pooled mount."""
    standalone_mounts = [mount("kvservice", path="/kv")]
    pooled_mounts = [mount("kvservice", path="/kv", port_name="kvservice", port_owner="pool-core")]

    standalone_files = gateway.render(standalone_mounts, {"kvservice": 30001}, cfg())
    pooled_ports = gateway.resolve_ports(
        pooled_mounts, FakeAllocator({"pool-core": {"kvservice": 30011}})
    )
    pooled_files = gateway.render(pooled_mounts, pooled_ports, cfg())

    assert set(standalone_files) == set(pooled_files)
    site_key = "sites/kvservice.caddy"
    assert standalone_files[site_key] != pooled_files[site_key]
    assert standalone_files[site_key].replace("30001", "30011") == pooled_files[site_key]
    # The top-level Caddyfile only ever contains the site import line, never a
    # port number, so it must be untouched by which allocator row backs a mount.
    assert standalone_files[gateway.CADDYFILE_NAME] == pooled_files[gateway.CADDYFILE_NAME]


def test_render_duplicate_mount_ids_still_raise_with_pool_keys() -> None:
    """(e) Duplicate mount ids still raise, pool keys or not."""
    mounts = [
        mount("kvservice", path="/kv", port_name="kvservice", port_owner="pool-core"),
        mount("kvservice", path="/kv2", port_name="kvservice", port_owner="pool-core"),
    ]
    with pytest.raises(GatewayError, match="duplicate mount id 'kvservice'"):
        gateway.render(mounts, {"kvservice": 30011}, cfg())


# --------------------------------------------------------------------------- #
# (f) Every existing golden must still render byte-identical: this file adds
# no golden-touching mounts, but re-assert the existing goldens here too so a
# regression in resolve_ports (e.g. an accidental owner default) is caught by
# the same mechanism as test_platform_gateway.py's test_golden.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("scenario", sorted(base.SCENARIOS))
def test_goldens_unaffected_by_pool_support(scenario: str) -> None:
    mounts, ports, config = base.SCENARIOS[scenario]
    # None of the shared scenario mounts carry port_owner/port_name, so this
    # must be byte-identical to test_platform_gateway.py's own test_golden.
    files = gateway.render(mounts, ports, config)
    golden_base = base.GOLDEN_DIR / scenario
    on_disk = {
        str(path.relative_to(golden_base)): path.read_text(encoding="utf-8")
        for path in sorted(golden_base.rglob("*"))
        if path.is_file()
    }
    assert files == on_disk
