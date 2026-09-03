"""T6: pool grouping inside the one-shot sync loop (PLAN-pool §5.5, §8 T6).

Two manifests that both carry ``pool = "core"`` in their ``service.ams.toml``
must come out of a tick as **one** declared service (``pool-core``) that owns
the tree, the venv, the ports and the process -- while each member keeps
everything the rest of the platform reads about it: its ``mounts/<id>.json``
route, its ``registry/<id>.json`` identity, its own port on the pool's
allocator row, and its own health probe.

The translator half of the feature (``translate.build_pool`` / ``pool_member``
/ ``mangle_member``, PLAN-pool §5.3) is a parallel task, so the ``stub_pool``
fixture installs a thin stand-in for those three names **only when the real
ones are not importable**; where they are, every test here is an integration
test against them. The stub builds a real ``ServiceDecl`` through
``ams.schema.loads`` and hands back the members' own ``Translation`` objects
carrying the two additive mount keys -- exactly the contract §5.3 freezes and
all this module is entitled to assume.

Fixtures come from ``test_platform_sync.py`` (same directory, no package
``__init__.py``, the pattern ``test_platform_backup_pool.py`` already uses).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest
from test_platform_sync import (  # noqa: F401 - `env`/`registry` are fixtures, used by name
    CADDY_PORT,
    FakeRegistry,
    _commit,
    _git,
    env,
    make_cfg,
    read_state,
    registry,
    service_manifest,
    snapshot,
    upstream,
)

from ams.platform import sync as sync_mod
from ams.platform import translate as translate_mod
from ams.platform.sync import (
    POOL_JSON_NAME,
    POOL_RUNNER_NAME,
    PlatformState,
    ServiceRecord,
    mounts_dir,
    pool_id_for,
    pool_shadow_dir,
    registry_dir,
    state_path,
    sync,
)
from ams.platform.translate import Translation, translate
from ams.schema import ServiceDecl
from ams.schema import loads as load_decl
from ams.state import StateDir

POOL = "core"
POOL_ID = pool_id_for(POOL)
MEMBERS = ("alpha", "beta")

HAS_REAL_POOL = all(
    hasattr(translate_mod, name) for name in ("build_pool", "pool_member", "mangle_member")
)


# --------------------------------------------------------------------------- the stub


@dataclass(frozen=True)
class StubMember:
    """The §5.3 ``PoolMember`` shape, only as far as sync reads it."""

    id: str
    translation: Translation
    runtime_rel: str
    app: str
    factory: bool
    identity_env: dict[str, str]
    shared_env: dict[str, str]
    secret_names: tuple[str, ...]


@dataclass(frozen=True)
class StubPoolTranslation:
    id: str
    decl: ServiceDecl
    members: tuple[StubMember, ...]
    pool_json: dict[str, Any]


def stub_mangle(member_id: str) -> str:
    return member_id.replace("-", "_").upper()


def stub_pool_member(t: Translation, manifest_dir_rel: str) -> StubMember:
    return StubMember(
        id=t.id,
        translation=t,
        runtime_rel=manifest_dir_rel,
        app=f"{t.id}.main:build_app",
        factory=True,
        identity_env={"SVC_NAME": t.id},
        shared_env={},
        secret_names=("SVC_SECRET",),
    )


def stub_build_pool(pool: str, members: Any, ctx: Any) -> StubPoolTranslation:
    pool_id = pool_id_for(pool)
    members = list(members)
    root = ctx.root_for(pool_id)
    ids = [m.id for m in members]
    packages = ['"uvicorn[standard]==0.52.4"']
    for m in members:
        packages += ['"-e"', f'"{m.runtime_rel}"']
    lines = [
        f'id = "{pool_id}"',
        f'name = "pool {pool}"',
        "secrets = [" + ", ".join(f'"SVC_SECRET__{stub_mangle(i)}"' for i in ids) + "]",
        'depends_on = ["registry"]',
        "",
        "[start]",
        f'argv = ["python", "{root}/{POOL_RUNNER_NAME}"]',
        'workdir = "repo"',
        "",
        "[env]",
        f'GIT_COMMIT = "{ctx.sha}"',
        f'POOL_ID = "{pool}"',
        'POOL_PORT_ADMIN = "${PORT_pool}"',
        *[f'POOL_PORT_{stub_mangle(i)} = "${{PORT_{i}}}"' for i in ids],
        "",
        "[ports]",
        "pool = 0",
        *[f"{i} = 0" for i in ids],
        "",
        "[runtime]",
        'kind = "uv"',
        'python = "3.12"',
        "sync = false",
        "packages = [" + ", ".join(packages) + "]",
        "",
        "[health]",
        'kind = "http"',
        'port = "pool"',
        'path = "/_pool/health"',
        "start_period_s = 240.0",
        "",
        "[limits]",
        'memory_max = "210M"',
        'cpu_max = "100%"',
        "pids_max = 80",
        "",
        "[restart]",
        'policy = "always"',
        "backoff_s = 10.0",
        "",
    ]
    decl = load_decl("\n".join(lines))
    # The two additive mount keys (§3.3): everything else about the member's
    # sidecars stays exactly what an unpooled translation produced.
    pooled = tuple(replace(m, translation=_with_owner(m.translation, pool_id)) for m in members)
    pool_json = {
        "version": 1,
        "pool": pool,
        "sha": ctx.sha,
        "members": [
            {
                "id": m.id,
                "app": m.app,
                "factory": m.factory,
                "port_env": f"POOL_PORT_{stub_mangle(m.id)}",
                "health_path": str((m.translation.registry or {}).get("health_path", "/health")),
                "secret_env": {"SVC_SECRET": f"SVC_SECRET__{stub_mangle(m.id)}"},
                "env": dict(m.identity_env),
            }
            for m in pooled
        ],
    }
    return StubPoolTranslation(id=pool_id, decl=decl, members=pooled, pool_json=pool_json)


def _with_owner(t: Translation, pool_id: str) -> Translation:
    return replace(t, mount={**t.mount, "port_owner": pool_id, "port_name": t.id})


@pytest.fixture
def stub_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install the §5.3 interface, unless the real translator already has it."""
    if HAS_REAL_POOL:
        return
    monkeypatch.setattr(translate_mod, "build_pool", stub_build_pool, raising=False)
    monkeypatch.setattr(translate_mod, "pool_member", stub_pool_member, raising=False)
    monkeypatch.setattr(translate_mod, "mangle_member", stub_mangle, raising=False)


# --------------------------------------------------------------------------- fixtures


def overlay(pool: str | None = POOL, **extra: str) -> str:
    lines = [] if pool is None else [f'pool = "{pool}"']
    lines += [f'{k} = "{v}"' for k, v in extra.items()]
    return "\n".join(lines) + "\n"


@pytest.fixture
def upstream_pool(tmp_path: Path) -> tuple[Path, str]:
    """Two pooled services plus one that stays on its own."""
    src = tmp_path / "upstream-pool"
    src.mkdir()
    _git(src, "init", "-q", "-b", "main")
    sha = _commit(
        src,
        {
            "services/alpha/service.yaml": service_manifest("alpha", port=9201),
            "services/alpha/service.ams.toml": overlay(),
            "services/beta/service.yaml": service_manifest("beta", port=9202),
            "services/beta/service.ams.toml": overlay(),
            "services/solo/service.yaml": service_manifest("solo", port=9203),
            "README.md": "api\n",
        },
        "first",
    )
    return src, sha


@pytest.fixture
def pool_env(env: Any, monkeypatch: pytest.MonkeyPatch) -> Any:  # noqa: F811 - the fixture
    """``test_platform_sync.env`` with a reload that allocates *named* ports.

    The stock fake gives every service one port called ``main``; a pool has
    three (``pool`` plus one per member) and resolving a member's route is the
    whole point, so this one reads the names out of the declaration the run
    just wrote -- which is what the real harness does.
    """

    def fake_reload(state_arg: StateDir, **_kw: Any) -> dict[str, Any]:
        env.calls.add("reload")
        allocate_named_ports(state_arg, env.registry.port)
        return {"ok": True, "added": [], "changed": [], "removed": []}

    monkeypatch.setattr(sync_mod, "ctl_reload", fake_reload)
    placed: list[tuple[str, str, bytes]] = []
    dirs: list[tuple[str, tuple[str, ...]]] = []

    def fake_place(root: Path, name: str, content: bytes, _block: Any, **_kw: Any) -> None:
        Path(root).mkdir(parents=True, exist_ok=True)
        (Path(root) / name).write_bytes(content)
        placed.append((Path(root).parent.name, name, content))
        env.calls.add("place-pool-file", Path(root).parent.name, name)

    def fake_dirs(root: Path, members: Any, _block: Any) -> None:
        for member in members:
            (Path(root) / "data" / member).mkdir(parents=True, exist_ok=True)
        dirs.append((Path(root).parent.name, tuple(members)))
        env.calls.add("pool-data-dirs", Path(root).parent.name)

    monkeypatch.setattr(sync_mod, "place_pool_file", fake_place)
    monkeypatch.setattr(sync_mod, "make_pool_data_dirs", fake_dirs)
    env.placed, env.data_dirs = placed, dirs
    return env


def allocate_named_ports(state: StateDir, port: int) -> None:
    path = state.ports_state
    data: dict[str, Any] = {"version": 1, "ports": {}}
    if path.exists():
        data = json.loads(path.read_text())
    ports = data.setdefault("ports", {})
    ports.setdefault("caddy", {"main": CADDY_PORT})
    for service_id in state.list_service_ids():
        try:
            names = list(state.load_declaration(service_id).ports) or ["main"]
        except Exception:  # pragma: no cover - a broken declaration is another test's
            names = ["main"]
        ports.setdefault(service_id, {name: port for name in names})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def run(e: Any, src: Path, **kw: Any) -> Any:
    return sync(e.state, e.store, make_cfg(src, e.registry, **kw), secrets=e.secrets)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- grouping


def test_two_pooled_manifests_produce_one_pool_and_no_member_declaration(
    pool_env: Any, upstream_pool: Any, stub_pool: None
) -> None:
    src, sha = upstream_pool
    report = run(pool_env, src)

    assert report.ok, [(o.id, o.error) for o in report.services]
    assert set(report.ids) == {POOL_ID, "alpha", "beta", "solo"}
    stages = {o.id: o.stage for o in report.services}
    assert stages == {POOL_ID: "healthy", "alpha": "healthy", "beta": "healthy", "solo": "healthy"}

    state = pool_env.state
    assert state.service_decl_path(POOL_ID).is_file()
    for member in MEMBERS:
        assert not state.service_decl_path(member).exists(), f"{member} got its own declaration"
    assert sorted(state.list_service_ids()) == [POOL_ID, "solo"]


def test_only_the_pool_is_staged_provisioned_and_given_a_key(
    pool_env: Any, upstream_pool: Any, stub_pool: None
) -> None:
    src, _sha = upstream_pool
    run(pool_env, src)

    staged = [c[1] for c in pool_env.calls.of("stage")]
    provisioned = [c[1] for c in pool_env.calls.of("provision")]
    keyed = [c[1] for c in pool_env.calls.of("jwt")]
    assert sorted(staged) == [POOL_ID, "solo"]
    assert sorted(provisioned) == [POOL_ID, "solo"]
    assert sorted(keyed) == [POOL_ID, "solo"]
    for member in MEMBERS:
        assert member not in staged and member not in provisioned and member not in keyed


def test_member_sidecars_survive_and_the_registry_one_is_byte_identical(
    pool_env: Any, upstream_pool: Any, stub_pool: None
) -> None:
    src, sha = upstream_pool
    run(pool_env, src)

    state = pool_env.state
    for member in MEMBERS:
        mount = read_json(mounts_dir(state) / f"{member}.json")
        assert mount["id"] == member
        assert mount["port_owner"] == POOL_ID
        assert mount["port_name"] == member

        # What an *unpooled* deploy of the same manifest at the same commit
        # would have written, byte for byte. The registry never learns that
        # pooling exists (PLAN-pool §3.3).
        ctx = make_cfg(src, pool_env.registry).context_for(sha=sha, services_dir=state.services_dir)
        manifest = pool_env.store.root / "src" / "api" / sha / "services" / member / "service.yaml"
        expected = translate(manifest.read_text(encoding="utf-8"), ctx).registry
        written = (registry_dir(state) / f"{member}.json").read_text(encoding="utf-8")
        assert written == json.dumps(dict(expected), indent=2, sort_keys=True) + "\n"

    assert not (mounts_dir(state) / f"{POOL_ID}.json").exists()
    assert not (registry_dir(state) / f"{POOL_ID}.json").exists()


def test_the_pool_root_gets_pool_json_the_runner_and_a_data_dir_per_member(
    pool_env: Any, upstream_pool: Any, stub_pool: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    asset = pool_env.state.root / "fake-runner.py"
    asset.write_bytes(b"# ams pool runner\nprint('hi')\n")
    monkeypatch.setattr(sync_mod, "pool_runner_source", lambda: asset)

    src, sha = upstream_pool
    run(pool_env, src)

    root = pool_env.state.service_root(POOL_ID)
    assert (root / POOL_RUNNER_NAME).read_bytes() == asset.read_bytes()
    document = read_json(root / POOL_JSON_NAME)
    assert document["pool"] == POOL and document["sha"] == sha
    assert [m["id"] for m in document["members"]] == list(MEMBERS)
    assert [d[1] for d in pool_env.data_dirs] == [MEMBERS]
    for member in MEMBERS:
        assert (root / "data" / member).is_dir()
    # And the shadow copies that make the next tick free.
    for name in (POOL_JSON_NAME, POOL_RUNNER_NAME):
        assert (pool_shadow_dir(pool_env.state) / POOL_ID / name).is_file()


def test_each_member_gets_its_own_secret_under_the_pool_id(
    pool_env: Any, upstream_pool: Any, stub_pool: None
) -> None:
    src, _sha = upstream_pool
    run(pool_env, src)

    names = pool_env.secrets.names(POOL_ID)
    assert sorted(names) == ["SVC_SECRET__ALPHA", "SVC_SECRET__BETA"]
    assert "SVC_SECRET" not in names, "the pool has no identity of its own"
    for member in MEMBERS:
        assert pool_env.secrets.names(member) == []
    values = pool_env.secrets.load(POOL_ID, names)
    assert len(set(values.values())) == 2, "members must not share one secret"


def test_the_registry_identity_uses_the_members_own_secret(
    pool_env: Any, upstream_pool: Any, stub_pool: None
) -> None:
    src, _sha = upstream_pool
    run(pool_env, src)

    # The secret travels in a header, never in the body (registryclient).
    posted = {
        p["body"]["id"]: p["headers"].get("X-Service-Secret")
        for p in pool_env.registry.posts("/api/services")
        if isinstance(p["body"], dict) and "id" in p["body"]
    }
    stored = pool_env.secrets.load(POOL_ID, ["SVC_SECRET__ALPHA", "SVC_SECRET__BETA"])
    assert posted["alpha"] == stored["SVC_SECRET__ALPHA"]
    assert posted["beta"] == stored["SVC_SECRET__BETA"]
    assert POOL_ID not in posted, "a pool is not an identity"


# --------------------------------------------------------------------------- the record


def test_pool_and_pool_members_survive_a_load_flush_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = PlatformState(path, {})
    state.records[POOL_ID] = ServiceRecord(sha="a" * 40, stage="healthy", pool_members=["a", "b"])
    state.records["a"] = ServiceRecord(sha="a" * 40, stage="healthy", pool="core")
    assert state.flush()

    again = PlatformState.load(path)
    assert again.records[POOL_ID].pool_members == ["a", "b"]
    assert again.records["a"].pool == "core"
    # And a second flush of what was just loaded is a no-op: the two keys made
    # the round trip without being dropped and re-added.
    assert not again.flush()

    document = json.loads(path.read_text())
    assert document["version"] == 1
    assert "pool" not in document["services"][POOL_ID], "unset keys stay off the record"
    assert document["services"]["a"]["pool"] == "core"


def test_the_records_name_the_pool_in_both_directions(
    pool_env: Any, upstream_pool: Any, stub_pool: None
) -> None:
    src, _sha = upstream_pool
    run(pool_env, src)

    services = read_state(pool_env.state)["services"]
    assert services[POOL_ID]["pool_members"] == list(MEMBERS)
    for member in MEMBERS:
        assert services[member]["pool"] == POOL
        assert "pool_members" not in services[member]
    assert "pool" not in services["solo"]


# --------------------------------------------------------------------------- change detection


def test_a_tick_that_changes_nothing_writes_nothing(
    pool_env: Any, upstream_pool: Any, stub_pool: None
) -> None:
    src, _sha = upstream_pool
    run(pool_env, src)

    before = snapshot(pool_env.state.root)
    pool_env.calls.items.clear()
    report = run(pool_env, src)

    assert report.ok
    assert report.unchanged == report.ids
    assert not report.state_written
    assert snapshot(pool_env.state.root) == before
    assert pool_env.calls.of("place-pool-file") == []
    assert pool_env.calls.of("pool-data-dirs") == []
    assert pool_env.calls.of("reload") == []


def test_the_pool_moves_to_head_only_when_a_member_is_affected(
    pool_env: Any, upstream_pool: Any, stub_pool: None
) -> None:
    src, first = upstream_pool
    run(pool_env, src)

    # A commit that touches neither member: the pool stays where it is.
    docs = _commit(src, {"README.md": "api, documented\n"}, "docs only")
    report = run(pool_env, src)
    assert report.sha == docs
    assert report.unchanged == report.ids
    services = read_state(pool_env.state)["services"]
    assert services[POOL_ID]["sha"] == first
    assert [services[m]["sha"] for m in MEMBERS] == [first, first]

    # One member changes -> the whole pool moves, because one tree and one
    # process cannot be at two commits.
    bumped = _commit(
        src,
        {"services/beta/service.yaml": service_manifest("beta", port=9202, memory="256M")},
        "beta bump",
    )
    report = run(pool_env, src)
    assert report.sha == bumped
    services = read_state(pool_env.state)["services"]
    assert services[POOL_ID]["sha"] == bumped
    assert [services[m]["sha"] for m in MEMBERS] == [bumped, bumped]
    assert "stage" in report.outcome(POOL_ID).actions


def test_members_at_different_deployed_commits_are_reconciled_at_the_head(
    pool_env: Any, upstream_pool: Any, stub_pool: None
) -> None:
    src, first = upstream_pool
    run(pool_env, src)
    later = _commit(src, {"README.md": "later\n"}, "docs only")

    document = json.loads(state_path(pool_env.state).read_text())
    document["services"]["beta"]["deployed_sha"] = later
    state_path(pool_env.state).write_text(json.dumps(document), encoding="utf-8")

    report = run(pool_env, src)
    assert report.sha == later
    assert read_state(pool_env.state)["services"][POOL_ID]["sha"] == later


# --------------------------------------------------------------------------- ports


def test_finish_looks_the_port_up_on_the_owner(
    pool_env: Any, upstream_pool: Any, stub_pool: None
) -> None:
    src, _sha = upstream_pool
    report = run(pool_env, src)
    assert report.ok

    allocated = json.loads(pool_env.state.ports_state.read_text())["ports"]
    assert sorted(allocated[POOL_ID]) == ["alpha", "beta", "pool"]
    for member in MEMBERS:
        assert member not in allocated, "a pooled member owns no allocator row"
    # The gateway resolved every member's route through `port_owner` anyway.
    caddyfile = (pool_env.state.root / "gateway" / "sites" / "alpha.caddy").read_text()
    assert f":{allocated[POOL_ID]['alpha']}" in caddyfile


def test_a_member_whose_pool_has_no_port_fails_naming_the_owner(
    pool_env: Any, upstream_pool: Any, stub_pool: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    src, _sha = upstream_pool

    def reload_without_pool_ports(state_arg: StateDir, **_kw: Any) -> dict[str, Any]:
        path = state_arg.ports_state
        data = json.loads(path.read_text()) if path.exists() else {"version": 1, "ports": {}}
        data.setdefault("ports", {}).setdefault("caddy", {"main": CADDY_PORT})
        path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        return {"ok": True, "added": [], "changed": [], "removed": []}

    monkeypatch.setattr(sync_mod, "ctl_reload", reload_without_pool_ports)
    report = run(pool_env, src)

    error = report.outcome("alpha").error or ""
    assert "no allocated port named 'alpha' on 'pool-core'" in error


# --------------------------------------------------------------------------- migration


def test_a_stale_member_declaration_is_unlinked_while_its_root_survives(
    pool_env: Any, upstream_pool: Any, stub_pool: None
) -> None:
    src, _sha = upstream_pool
    state = pool_env.state
    # What the pre-pool deployment left behind.
    for member in MEMBERS:
        state.service_dir(member).mkdir(parents=True, exist_ok=True)
        state.service_decl_path(member).write_text('id = "x"\n', encoding="utf-8")
        (state.service_root(member) / "repo").mkdir(parents=True, exist_ok=True)

    report = run(pool_env, src)

    assert report.ok, [(o.id, o.error) for o in report.services]
    for member in MEMBERS:
        assert not state.service_decl_path(member).exists()
        assert state.service_root(member).is_dir(), "the root holds the data; only the file goes"
        assert "unpool-decl" in report.outcome(member).actions


def test_a_non_empty_legacy_data_dir_blocks_the_declare_with_the_adopt_command(
    pool_env: Any, upstream_pool: Any, stub_pool: None, capsys: Any
) -> None:
    src, _sha = upstream_pool
    legacy = pool_env.state.service_root("beta") / "data"
    legacy.mkdir(parents=True, exist_ok=True)
    (legacy / "beta.db").write_bytes(b"sqlite")

    report = run(pool_env, src)

    assert not report.ok
    pool = report.outcome(POOL_ID)
    assert pool.stage == "failed"
    assert "['beta']" in (pool.error or "")
    assert "ams platform pool adopt core" in (pool.error or "")
    assert not pool_env.state.service_decl_path(POOL_ID).exists()

    # Its members are failed with it, but the cause is escalated exactly once.
    assert {o.id for o in report.services if not o.ok} == {POOL_ID, "alpha", "beta"}
    assert [e["service_id"] for e in report.escalations] == [POOL_ID]
    assert report.outcome("alpha").error == f"pool {POOL_ID}: {pool.error}"
    # The unpooled service still deployed.
    assert report.outcome("solo").stage == "healthy"

    # Second tick, same cause: no second escalation.
    second = run(pool_env, src)
    assert second.escalations == ()


def test_an_empty_legacy_data_dir_does_not_block_anything(
    pool_env: Any, upstream_pool: Any, stub_pool: None
) -> None:
    src, _sha = upstream_pool
    (pool_env.state.service_root("beta") / "data").mkdir(parents=True, exist_ok=True)

    report = run(pool_env, src)

    assert report.ok, [(o.id, o.error) for o in report.services]
    assert report.outcome(POOL_ID).stage == "healthy"


# --------------------------------------------------------------------------- refusals


@pytest.mark.parametrize(
    ("files", "message"),
    [
        pytest.param(
            {"services/beta/service.ams.toml": overlay(pool=None)},
            "only one member ('alpha')",
            id="pool-of-one",
        ),
        pytest.param(
            {
                "services/pool/service.yaml": service_manifest("pool", port=9204),
                "services/pool/service.ams.toml": overlay(),
            },
            "member id 'pool' collides with the reserved admin port name",
            id="reserved-port-name",
        ),
        pytest.param(
            {
                "services/core/service.yaml": service_manifest("core", port=9205),
                "services/core/service.ams.toml": overlay(),
            },
            "the pool name is also a member id",
            id="name-collides-with-member",
        ),
    ],
)
def test_cross_manifest_rules_fail_the_pool_and_nothing_else(
    pool_env: Any,
    upstream_pool: Any,
    stub_pool: None,
    files: dict[str, str],
    message: str,
) -> None:
    src, _sha = upstream_pool
    _commit(src, files, "break the pool")

    report = run(pool_env, src)

    assert not report.ok
    assert message in (report.outcome(POOL_ID).error or "")
    assert report.outcome("solo").stage == "healthy", "one broken pool is not the fleet"
    assert [e["service_id"] for e in report.escalations] == [POOL_ID]


def test_a_pool_on_a_static_manifest_is_refused(
    pool_env: Any, upstream_pool: Any, stub_pool: None
) -> None:
    src, _sha = upstream_pool
    static = (
        "schema_version: 1\nkind: static\nname: site\n\n"
        "deploy:\n  source:\n    type: git\n    repo: StevenLi-phoenix/api\n"
        "    branch: main\n    path: apps/site/dist\n  install: []\n  target_dir: /srv/site\n\n"
        "mount:\n  gateway: site.lishuyu.app\n  subdomain: site\n"
    )
    _commit(
        src,
        {"apps/site/service.yaml": static, "apps/site/service.ams.toml": overlay()},
        "pool a static site",
    )

    report = run(pool_env, src)

    assert "pool is not valid for kind: static" in (report.outcome(POOL_ID).error or "")


def test_duplicate_member_ids_are_refused_before_the_translator_sees_them() -> None:
    """Two manifests cannot share a directory, so this guard is unreachable from
    a checkout -- and is asserted directly rather than left untested."""
    entries = [
        sync_mod._Parsed(Path("services/alpha/service.yaml"), "alpha"),
        sync_mod._Parsed(Path("apps/alpha/service.yaml"), "alpha"),
    ]
    with pytest.raises(sync_mod.PoolError, match=r"duplicate member id\(s\) \['alpha'\]"):
        sync_mod._check_pool(POOL, entries)


def test_a_member_whose_manifest_is_broken_takes_its_pool_with_it(
    pool_env: Any, upstream_pool: Any, stub_pool: None
) -> None:
    """A pool is one process. Deploying the members that happen to parse would
    silently serve two of three identities and leave the third's route pointing
    at nothing, so the group fails together and the reason names the file."""
    src, _sha = upstream_pool
    _commit(
        src,
        {
            "services/gamma/service.yaml": "schema_version: 1\nkind: service\nname: gamma\n",
            "services/gamma/service.ams.toml": overlay(),
        },
        "a third member that does not parse",
    )

    report = run(pool_env, src)

    error = report.outcome(POOL_ID).error or ""
    assert "member manifest(s) did not translate" in error and "gamma" in error
    assert not pool_env.state.service_decl_path(POOL_ID).exists()
    assert {o.id for o in report.services if not o.ok} == {POOL_ID, "alpha", "beta", "gamma"}
    assert report.outcome("solo").stage == "healthy"


def test_a_build_without_the_translator_half_fails_only_the_pool(
    pool_env: Any, upstream_pool: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("build_pool", "pool_member", "mangle_member"):
        monkeypatch.delattr(translate_mod, name, raising=False)
    src, _sha = upstream_pool

    report = run(pool_env, src)

    assert "cannot build pools" in (report.outcome(POOL_ID).error or "")
    assert report.outcome("solo").stage == "healthy"


# --------------------------------------------------------------------------- selection


def test_naming_one_member_selects_the_whole_pool(
    pool_env: Any, upstream_pool: Any, stub_pool: None
) -> None:
    src, _sha = upstream_pool
    report = run(pool_env, src, include=("alpha",))

    assert set(report.ids) == {POOL_ID, "alpha", "beta"}
    assert report.outcome(POOL_ID).stage == "healthy"


# --------------------------------------------------------------------------- the real thing


@pytest.mark.skipif(
    not HAS_REAL_POOL,
    reason="translate.build_pool/pool_member/mangle_member have not landed yet (T3)",
)
def test_real_build_pool_drives_a_pool_to_healthy(
    pool_env: Any, upstream_pool: Any, stub_pool: None
) -> None:
    """The same tick, against the real §5.3 interface rather than the stub."""
    src, sha = upstream_pool
    report = run(pool_env, src)

    assert report.ok, [(o.id, o.error) for o in report.services]
    assert set(report.ids) == {POOL_ID, "alpha", "beta", "solo"}
    state = pool_env.state
    assert state.service_decl_path(POOL_ID).is_file()
    for member in MEMBERS:
        assert not state.service_decl_path(member).exists()
        mount = read_json(mounts_dir(state) / f"{member}.json")
        assert mount["port_owner"] == POOL_ID and mount["port_name"] == member
    document = read_json(state.service_root(POOL_ID) / POOL_JSON_NAME)
    assert document["sha"] == sha
    assert sorted(m["id"] for m in document["members"]) == list(MEMBERS)


@pytest.mark.skipif(not HAS_REAL_POOL, reason="translate's pool constants have not landed yet (T3)")
def test_the_two_modules_agree_on_the_pool_constants() -> None:
    assert sync_mod.POOL_ID_PREFIX == translate_mod.POOL_ID_PREFIX
    assert sync_mod.POOL_ADMIN_PORT_NAME == translate_mod.POOL_ADMIN_PORT_NAME


def test_the_runner_asset_ships_with_ams_or_is_a_known_gap() -> None:
    """T2's asset. Named here so the gap is visible rather than implicit."""
    source = sync_mod.pool_runner_source()
    if not source.is_file():
        pytest.skip(f"{source} has not landed yet (T2)")
    assert not (source.parent / "__init__.py").exists(), "assets/ must not be a package"
    assert source.read_bytes(), "the runner asset is empty"


# --------------------------------------------------------------------------- dry run


def test_dry_run_output_for_unpooled_services_is_byte_identical(
    env: Any,  # noqa: F811 - the fixture
    upstream: Any,  # noqa: F811 - the fixture
    capsys: Any,
) -> None:
    """The pool code must be invisible to a checkout that declares no pool.

    The four records are pinned literally rather than compared against a helper,
    so a change to the plan's wording has to be made here on purpose.
    """
    src, sha = upstream
    report = run(env, src, dry_run=True)

    lines = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert [r["service_id"] for r in lines] == ["site", "alpha", "beta"]
    assert lines[0] == {
        "kind": "PlatformSyncPlan",
        "service_id": "site",
        "action": "log",
        "reason": "would run: translate, stage, provision, declare",
        "event": {
            "service_id": "site",
            "stage": "plan",
            "error": "would run: translate, stage, provision, declare",
            "sha": sha,
            "prev_sha": None,
        },
    }
    full = "would run: translate, stage, provision, declare, reload, register, health"
    for line, service_id in zip(lines[1:], ["alpha", "beta"], strict=True):
        assert line == {
            "kind": "PlatformSyncPlan",
            "service_id": service_id,
            "action": "log",
            "reason": full,
            "event": {
                "service_id": service_id,
                "stage": "plan",
                "error": full,
                "sha": sha,
                "prev_sha": None,
            },
        }
    assert {o.id for o in report.services} == {"alpha", "beta", "site"}


def test_dry_run_answers_the_migration_step_four_questions(
    pool_env: Any, tmp_path: Path, stub_pool: None, capsys: Any
) -> None:
    """PLAN-pool §7.2 step 4, rehearsed: deploy two services standalone, add the
    two overlay lines, and read the plan. It must report one new service, both
    declarations going away, both mounts gaining a ``port_owner`` and zero
    registry sidecar changes -- against what is on disk, not what is intended.
    """
    src = tmp_path / "upstream-migrate"
    src.mkdir()
    _git(src, "init", "-q", "-b", "main")
    _commit(
        src,
        {
            "services/alpha/service.yaml": service_manifest("alpha", port=9201),
            "services/beta/service.yaml": service_manifest("beta", port=9202),
            "services/solo/service.yaml": service_manifest("solo", port=9203),
        },
        "unpooled",
    )
    assert run(pool_env, src).ok

    pooled = _commit(
        src,
        {
            "services/alpha/service.ams.toml": overlay(),
            "services/beta/service.ams.toml": overlay(),
        },
        "join the pool",
    )
    capsys.readouterr()
    run(pool_env, src, dry_run=True)

    lines = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    by_id = {r["service_id"]: r for r in lines}
    assert [r["service_id"] for r in lines] == [POOL_ID, "alpha", "beta", "solo"]

    pool = by_id[POOL_ID]
    assert pool["event"]["sha"] == pooled
    assert "pool of 2: alpha, beta" in pool["reason"]
    for action in ("provision", "declare", "reload"):
        assert action in pool["reason"]

    for member in MEMBERS:
        reason = by_id[member]["reason"]
        assert reason.startswith(f"  in pool {POOL}: ")
        assert f"mount-gains-port_owner={POOL_ID}" in reason
        assert "declaration-unlinked" in reason
        assert "registry-unchanged" in reason
        assert by_id[member]["event"]["sha"] == pooled

    # `solo` is unpooled, so its line is the pre-pool one, unchanged: the dry
    # run plans every unpooled service against the head (it does not apply
    # D26's per-service rule), and that behaviour is deliberately untouched.
    assert by_id["solo"]["reason"] == (
        "would run: translate, stage, provision, declare, reload, register, health"
    )

    # And it planned only: nothing under the state dir moved.
    assert not pool_env.state.service_decl_path(POOL_ID).exists()
    for member in MEMBERS:
        assert pool_env.state.service_decl_path(member).is_file()


def test_dry_run_says_a_pool_will_not_build_when_a_member_is_broken(
    pool_env: Any, upstream_pool: Any, stub_pool: None, capsys: Any
) -> None:
    src, _sha = upstream_pool
    _commit(
        src,
        {
            "services/gamma/service.yaml": "schema_version: 1\nkind: service\nname: gamma\n",
            "services/gamma/service.ams.toml": overlay(),
        },
        "a third member that does not parse",
    )
    capsys.readouterr()
    report = run(pool_env, src, dry_run=True)

    lines = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    pool = next(r for r in lines if r["service_id"] == POOL_ID)
    assert "will not build" in pool["reason"] and "gamma" in pool["reason"]
    assert report.outcome(POOL_ID).stage == "failed"
    assert report.outcome("solo").actions[0] == "translate"
