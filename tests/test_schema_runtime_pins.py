"""``runtime.node`` / ``runtime.pnpm`` / ``runtime.build``: the managed Node toolchain pins.

An exact ``X.Y.Z`` node on a pnpm service means "download and verify exactly
this node into the store" (PLAN-core §4.1). A bare major keeps the pre-1.1.0
meaning -- the host's node, with the provisioner's warning -- so every existing
declaration loads and behaves exactly as before.
"""

from __future__ import annotations

from typing import Any

import pytest

from ams import schema


def decl(kind: str = "pnpm", **runtime: Any) -> schema.ServiceDecl:
    return schema.from_dict(
        {
            "id": "core",
            "start": {"argv": ["node", "dist/core/main.js"]},
            "runtime": {"kind": kind, **runtime},
        }
    )


# --------------------------------------------------------------------------- accepted


def test_exact_node_pnpm_and_build_load_as_a_managed_toolchain() -> None:
    d = decl(node="24.20.0", pnpm="11.19.0", build=["node", "scripts/build.mjs"])
    rt = d.runtime
    assert rt.node == "24.20.0" and rt.pnpm == "11.19.0"
    assert rt.build == ("node", "scripts/build.mjs")
    assert isinstance(rt.build, tuple)
    assert rt.managed_node


def test_exact_node_without_pnpm_is_managed_node_with_host_pnpm() -> None:
    rt = decl(node="24.20.0").runtime
    assert rt.managed_node and rt.pnpm is None


@pytest.mark.parametrize("legacy", ["22", "22.12"])
def test_a_non_exact_node_keeps_the_legacy_host_node_meaning(legacy: str) -> None:
    rt = decl(node=legacy).runtime
    assert rt.node == legacy
    assert not rt.managed_node


def test_defaults_are_unchanged_for_existing_declarations() -> None:
    rt = decl().runtime
    assert rt.pnpm is None and rt.build == () and not rt.managed_node


def test_bun_with_an_exact_node_is_not_managed() -> None:
    """bun ignores node (it is its own runtime); only pnpm gets a managed toolchain."""
    rt = decl("bun", node="24.20.0").runtime
    assert not rt.managed_node


def test_build_without_a_managed_node_is_allowed() -> None:
    """A build step can run on the host node just as well; it is not tied to the pin."""
    assert decl(build=["pnpm", "run", "build"]).runtime.build == ("pnpm", "run", "build")


def test_direct_construction_validates_the_same_way() -> None:
    rt = schema.RuntimeSpec(kind="pnpm", node="24.20.0", pnpm="11.19.0", build=("node", "b.mjs"))
    d = schema.ServiceDecl(id="core", start=schema.StartSpec(argv=("node",)), runtime=rt)
    assert d.runtime.managed_node
    with pytest.raises(schema.DeclError, match="runtime.pnpm"):
        schema.ServiceDecl(
            id="core",
            start=schema.StartSpec(argv=("node",)),
            runtime=schema.RuntimeSpec(kind="pnpm", node="24", pnpm="11.19.0"),
        )


# --------------------------------------------------------------------------- rejected


@pytest.mark.parametrize(
    ("runtime", "path"),
    [
        ({"pnpm": "11.19.0"}, "runtime.pnpm"),  # no node at all
        ({"node": "24", "pnpm": "11.19.0"}, "runtime.pnpm"),  # node not exact
        ({"node": "24.20", "pnpm": "11.19.0"}, "runtime.pnpm"),
        ({"node": "24.20.0", "pnpm": "11"}, "runtime.pnpm"),  # pnpm not exact
        ({"node": "24.20.0", "pnpm": "v11.19.0"}, "runtime.pnpm"),
        ({"node": "24.20.0", "pnpm": "11.19.0-rc.1"}, "runtime.pnpm"),
        ({"node": "24.20.0", "build": []}, None),  # empty build is fine (the default)
        ({"build": ["node", ""]}, "runtime.build"),
        ({"build": ["node", "a\0b"]}, "runtime.build"),
    ],
)
def test_pin_validation(runtime: dict[str, Any], path: str | None) -> None:
    if path is None:
        decl(**runtime)
        return
    with pytest.raises(schema.DeclError, match=path.replace(".", r"\.")):
        decl(**runtime)


@pytest.mark.parametrize("kind", ["none", "venv", "uv", "bun", "nix"])
def test_pnpm_pin_only_valid_with_kind_pnpm(kind: str) -> None:
    with pytest.raises(schema.DeclError, match=r"runtime\.pnpm"):
        decl(kind, pnpm="11.19.0")


@pytest.mark.parametrize("kind", ["none", "venv", "uv", "bun", "nix"])
def test_build_only_valid_with_kind_pnpm(kind: str) -> None:
    with pytest.raises(schema.DeclError, match=r"runtime\.build"):
        decl(kind, build=["make"])


@pytest.mark.parametrize("bad", ["node scripts/build.mjs", [1, 2], {"a": "b"}])
def test_build_must_be_a_list_of_strings(bad: Any) -> None:
    with pytest.raises(schema.DeclError, match=r"runtime\.build"):
        decl(build=bad)


def test_pnpm_must_be_a_string() -> None:
    with pytest.raises(schema.DeclError, match=r"runtime\.pnpm"):
        decl(node="24.20.0", pnpm=11)


def test_toml_round_trip_of_the_core_runtime_table() -> None:
    d = schema.loads(
        'id = "core"\n'
        '[start]\nargv = ["node", "dist/core/main.js"]\nworkdir = "current"\n'
        '[runtime]\nkind = "pnpm"\nnode = "24.20.0"\npnpm = "11.19.0"\n'
        'build = ["node", "scripts/build.mjs"]\n'
    )
    assert d.runtime.managed_node
    assert d.runtime.build == ("node", "scripts/build.mjs")
