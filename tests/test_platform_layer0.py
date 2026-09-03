"""Orchestration tests for ``ams.platform.layer0``.

Every building block the bring-up calls is replaced by a recorder, so what is
under test is exactly the thing this module owns and nothing else: **the order**.
The live behaviour of ``SourceMirror``, ``bootstrap``, ``translate``, the gateway
renderer and the registry client is covered by their own suites (and, for the
combination, by the racknerd transcript in `.claude/state/platform-layer0.md`).

Two properties get their own tests because a regression in either is a live
outage rather than a failing assertion:

* ``create_identity`` must happen **before** a Layer-1 service is started -- a
  translated declaration has no ``SVC_DEV``, so the SDK registers in its FastAPI
  lifespan and a 404 there is a uvicorn startup failure, i.e. a crash loop;
* the caddy declaration must carry the **fixed** port, because the Caddyfile
  holds the entry site's own listen address and is written before the reload
  that starts Caddy.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ams.platform import layer0
from ams.platform.bootstrap import AUTH_ID, LAYER0_PORTS, REGISTRY_ID, BootstrapResult
from ams.runtime import RuntimeStore
from ams.state import StateDir

SHA = "a" * 40
REPO_URL = "https://example.invalid/api"


# --------------------------------------------------------------------------- fakes


@dataclass
class _Rig:
    """Everything the test needs to drive and inspect one bring-up."""

    state: StateDir
    store: RuntimeStore
    calls: list[str]
    canonical: Path
    #: stage label -> a zero-arg callable that makes that stage raise
    breakers: dict[str, Any]

    def index(self, prefix: str) -> int:
        for i, call in enumerate(self.calls):
            if call.startswith(prefix):
                return i
        raise AssertionError(f"no call starting with {prefix!r} in {self.calls}")

    def has(self, prefix: str) -> bool:
        return any(call.startswith(prefix) for call in self.calls)


class _FakeBlock:
    """Stands in for ``UidBlock``; only ``uid_start`` is read by the report."""

    def __init__(self, uid_start: int) -> None:
        self.uid_start = uid_start


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Rig:
    state = StateDir(tmp_path / "state")
    state.ensure()
    store = RuntimeStore(tmp_path / "store")
    calls: list[str] = []
    canonical = tmp_path / "canonical"
    for rel in layer0.DEFAULT_LAYER1_MANIFESTS.values():
        path = canonical / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# manifest {rel}\n", encoding="utf-8")

    broken: dict[str, bool] = {}

    def boom(stage: str) -> None:
        if broken.get(stage):
            raise RuntimeError(f"injected failure in {stage}")

    class FakeMirror:
        def __init__(self, store_root: Path, name: str, *, url: str) -> None:
            calls.append(f"SourceMirror({name},{url})")

        def fetch(self, ref: str = "main") -> str:
            calls.append(f"fetch:{ref}")
            boom("fetch")
            return SHA

        def materialize(self, sha: str) -> Path:
            calls.append(f"materialize:{sha[:7]}")
            boom("materialize")
            return canonical

        def checkout_dir(self, sha: str) -> Path:
            return canonical

        def stage(self, sha: str, root: Path, block: Any) -> Path:
            service_id = Path(root).parent.name
            calls.append(f"stage:{service_id}")
            boom(f"stage:{service_id}")
            return Path(root) / "repo"

    monkeypatch.setattr(layer0.sources_mod, "SourceMirror", FakeMirror)

    def fake_bootstrap(st: StateDir, sto: RuntimeStore) -> BootstrapResult:
        calls.append("bootstrap")
        boom("bootstrap")
        return BootstrapResult(created=("key:private",))

    monkeypatch.setattr(layer0.bootstrap_mod, "bootstrap", fake_bootstrap)
    monkeypatch.setattr(
        layer0.bootstrap_mod,
        "ensure_service_dirs",
        lambda st, sid, block, **kw: calls.append(f"dirs:{sid}") or True,
    )

    def fake_place(st: StateDir, sid: str, block: Any, *, store: Any, private: bool = False, **kw):
        calls.append(f"key:{sid}:{'private' if private else 'public'}")
        boom(f"key:{sid}")
        return True

    monkeypatch.setattr(layer0.bootstrap_mod, "place_jwt_key", fake_place)
    monkeypatch.setattr(
        layer0.bootstrap_mod,
        "layer0_declarations",
        lambda st, **kw: {
            REGISTRY_ID: SimpleNamespace(id=REGISTRY_ID, runtime=SimpleNamespace(kind="uv")),
            AUTH_ID: SimpleNamespace(id=AUTH_ID, runtime=SimpleNamespace(kind="uv")),
        },
    )

    monkeypatch.setattr(layer0, "_harness_user", lambda: "harness")

    def fake_allocator(st: StateDir, user: str) -> Any:
        calls.append(f"allocator:{user}")
        boom("allocate")
        counter = {"n": 0}

        def allocate(service_id: str) -> _FakeBlock:
            counter["n"] += 1
            return _FakeBlock(100000 + 1024 * counter["n"])

        return SimpleNamespace(allocate=allocate)

    monkeypatch.setattr(layer0, "_allocator", fake_allocator)

    def fake_provision(st: StateDir, sto: RuntimeStore, decl: Any, block: Any) -> None:
        calls.append(f"provision:{decl.id}")
        boom(f"provision:{decl.id}")

    monkeypatch.setattr(layer0, "_provision_one", fake_provision)

    def fake_translate(text: str, ctx: Any) -> Any:
        service_id = "kvservice" if "kvservice" in text else "timeservice"
        calls.append(f"translate:{service_id}")
        boom(f"translate:{service_id}")
        return SimpleNamespace(
            id=service_id,
            decl=SimpleNamespace(id=service_id, runtime=SimpleNamespace(kind="uv")),
            mount={"version": 1, "id": service_id, "kind": "service", "port_name": "main"},
            registry={
                "version": 1,
                "id": service_id,
                "audience": service_id,
                "health_path": "/health",
                "acl": [{"action": "read", "principal": "anon", "effect": "allow"}],
            },
        )

    monkeypatch.setattr(layer0.translate_mod, "translate", fake_translate)
    monkeypatch.setattr(layer0.translate_mod, "emit_toml", lambda decl: f'id = "{decl.id}"\n')

    monkeypatch.setattr(
        layer0.gateway_mod, "caddy_declaration", lambda st, sto, port_name="main": "\nmain = 0\n"
    )

    def fake_resolve(mounts: Any, allocator: Any) -> dict[str, int]:
        calls.append("resolve_ports")
        return {m["id"]: 20002 + i for i, m in enumerate(mounts)}

    monkeypatch.setattr(layer0.gateway_mod, "resolve_ports", fake_resolve)

    def fake_render(mounts: Any, ports: Any, cfg: Any) -> dict[str, str]:
        calls.append(f"render:{cfg.listen_port}")
        boom("gateway")
        return {"Caddyfile": "# caddy\n"}

    monkeypatch.setattr(layer0.gateway_mod, "render", fake_render)
    monkeypatch.setattr(
        layer0.gateway_mod,
        "write",
        lambda st, files: [Path(st.root) / "gateway" / name for name in files],
    )

    def fake_ctl(st: StateDir, op: str, service_id: str | None = None) -> dict[str, Any]:
        calls.append(f"ctl:{op}" + (f":{service_id}" if service_id else ""))
        boom(f"ctl:{op}" + (f":{service_id}" if service_id else ""))
        return {"ok": True, "reload": {"added": [REGISTRY_ID, AUTH_ID, layer0.CADDY_ID]}}

    monkeypatch.setattr(layer0, "_ctl", fake_ctl)

    class FakeClient:
        def __init__(self, base_url: str, admin_token: str, *a: Any, **kw: Any) -> None:
            calls.append(f"RegistryClient({base_url})")
            assert admin_token == "TOKEN-NOT-REAL"

        def wait_healthy(self, url: str, deadline_s: float, interval_s: float = 1.0) -> bool:
            calls.append(f"wait_healthy:{url}")
            boom("health-layer0")
            return True

        def create_identity(self, service_id: str, secret: str, **kw: Any) -> Any:
            calls.append(f"create_identity:{service_id}")
            boom(f"identity:{service_id}")
            assert secret == f"secret-{service_id}"
            return SimpleNamespace(created=True)

        def upsert_acl(self, service_id: str, rules: Any) -> None:
            calls.append(f"upsert_acl:{service_id}:{len(list(rules))}")

    monkeypatch.setattr(layer0, "RegistryClient", FakeClient)
    monkeypatch.setattr(layer0, "_admin_token", lambda st: "TOKEN-NOT-REAL")
    monkeypatch.setattr(
        layer0,
        "_register_identity",
        lambda st, client, sidecar: (
            (
                client.create_identity(sidecar["id"], f"secret-{sidecar['id']}"),
                client.upsert_acl(sidecar["id"], sidecar["acl"]),
            )[0].created
        ),
    )

    def fake_wait_tcp(port: int, deadline_s: float, interval_s: float = 1.0) -> bool:
        calls.append(f"wait_tcp:{port}")
        return True

    def fake_wait_http(port: int, path: str, deadline_s: float, interval_s: float = 1.0) -> bool:
        calls.append(f"wait_http:{port}{path}")
        boom(f"wait_http:{port}")
        return True

    monkeypatch.setattr(layer0, "_wait_tcp", fake_wait_tcp)
    monkeypatch.setattr(layer0, "_wait_http", fake_wait_http)

    def breaker(stage: str) -> Any:
        def enable() -> None:
            broken[stage] = True

        return enable

    return _Rig(state=state, store=store, calls=calls, canonical=canonical, breakers=breaker)


def _run(rig: _Rig) -> layer0.Layer0Report:
    return layer0.bring_up(rig.state, rig.store, repo_url=REPO_URL, ref="main")


# --------------------------------------------------------------------------- order


def test_every_stage_runs_in_the_documented_order(rig: _Rig) -> None:
    report = _run(rig)
    assert report.stages == list(layer0.STAGES)
    assert report.failed_stage is None
    assert report.sha == SHA


def test_call_sequence(rig: _Rig) -> None:
    _run(rig)
    assert rig.calls == [
        f"SourceMirror(api,{REPO_URL})",
        "fetch:main",
        f"materialize:{SHA[:7]}",
        "bootstrap",
        "allocator:harness",
        f"stage:{REGISTRY_ID}",
        f"stage:{AUTH_ID}",
        f"dirs:{REGISTRY_ID}",
        f"key:{REGISTRY_ID}:private",
        f"dirs:{AUTH_ID}",
        f"key:{AUTH_ID}:private",
        f"key:{AUTH_ID}:public",
        f"provision:{REGISTRY_ID}",
        f"provision:{AUTH_ID}",
        "ctl:stop:kvservice",
        "ctl:stop:timeservice",
        "stage:kvservice",
        "dirs:kvservice",
        "key:kvservice:public",
        "stage:timeservice",
        "dirs:timeservice",
        "key:timeservice:public",
        "translate:kvservice",
        "translate:timeservice",
        "provision:kvservice",
        "provision:timeservice",
        "resolve_ports",
        f"render:{layer0.CADDY_PORT}",
        "ctl:reload",
        f"RegistryClient(http://127.0.0.1:{LAYER0_PORTS[REGISTRY_ID]})",
        f"wait_healthy:http://127.0.0.1:{LAYER0_PORTS[REGISTRY_ID]}/health",
        f"wait_tcp:{LAYER0_PORTS[AUTH_ID]}",
        f"wait_http:{layer0.CADDY_PORT}/ams-health",
        "create_identity:kvservice",
        "upsert_acl:kvservice:1",
        "create_identity:timeservice",
        "upsert_acl:timeservice:1",
        "ctl:start:kvservice",
        "ctl:start:timeservice",
        "wait_http:20002/health",
        "wait_http:20003/health",
    ]


def test_identities_are_created_before_layer1_starts(rig: _Rig) -> None:
    """The constraint that would otherwise crash-loop both services."""
    _run(rig)
    assert rig.index("create_identity:kvservice") < rig.index("ctl:start:kvservice")
    assert rig.index("create_identity:timeservice") < rig.index("ctl:start:timeservice")
    # ... and the registry must be answering before an identity is asked for.
    assert rig.index("wait_healthy") < rig.index("create_identity")


def test_layer1_is_stopped_before_its_tree_is_restaged(rig: _Rig) -> None:
    """``stage()`` unlinks the directory a running service executes from."""
    _run(rig)
    assert rig.index("ctl:stop:kvservice") < rig.index("stage:kvservice")
    assert rig.index("ctl:stop:timeservice") < rig.index("stage:timeservice")


def test_gateway_config_is_written_before_the_reload_that_starts_caddy(rig: _Rig) -> None:
    _run(rig)
    assert rig.index("render") < rig.index("ctl:reload")


def test_provisioning_precedes_the_reload(rig: _Rig) -> None:
    """A reload never provisions (D17), so the venv must already exist."""
    _run(rig)
    for service_id in (REGISTRY_ID, AUTH_ID, "kvservice", "timeservice"):
        assert rig.index(f"provision:{service_id}") < rig.index("ctl:reload")


# --------------------------------------------------------------------------- failures


#: (injected break, the stage it must be reported as)
FAILURES = [
    ("fetch", "fetch"),
    ("materialize", "materialize"),
    ("bootstrap", "bootstrap"),
    ("allocate", "allocate"),
    (f"stage:{REGISTRY_ID}", "stage-layer0"),
    (f"key:{AUTH_ID}", "keys-layer0"),
    (f"provision:{REGISTRY_ID}", "provision-layer0"),
    ("ctl:stop:kvservice", "stop-layer1"),
    ("stage:timeservice", "stage-layer1"),
    ("translate:timeservice", "translate-layer1"),
    ("provision:kvservice", "provision-layer1"),
    ("gateway", "gateway"),
    ("ctl:reload", "reload"),
    ("health-layer0", "health-layer0"),
    ("identity:kvservice", "identities"),
    ("ctl:start:timeservice", "start-layer1"),
    ("wait_http:20002", "health-layer1"),
]


@pytest.mark.parametrize(("injected", "stage"), FAILURES, ids=[s for _, s in FAILURES])
def test_a_failure_reports_its_stage_and_stops(rig: _Rig, injected: str, stage: str) -> None:
    rig.breakers(injected)()
    with pytest.raises(layer0.Layer0Error) as excinfo:
        _run(rig)
    err = excinfo.value
    assert err.stage == stage
    assert err.report.failed_stage == stage
    # Every earlier stage completed; the failing one and everything after it
    # did not. This is the property the sync loop's repair path depends on.
    assert err.report.stages == list(layer0.STAGES[: layer0.STAGES.index(stage)])


@pytest.mark.parametrize(("injected", "stage"), FAILURES, ids=[s for _, s in FAILURES])
def test_no_later_stage_runs_after_a_failure(rig: _Rig, injected: str, stage: str) -> None:
    rig.breakers(injected)()
    with pytest.raises(layer0.Layer0Error):
        _run(rig)
    later = layer0.STAGES[layer0.STAGES.index(stage) + 1 :]
    markers = {
        "gateway": "render",
        "reload": "ctl:reload",
        "identities": "create_identity",
        "start-layer1": "ctl:start",
        "health-layer1": "wait_http:20002",
    }
    for name in later:
        marker = markers.get(name)
        if marker is not None:
            assert not rig.has(marker), f"{name} ran after {stage} failed"


def test_a_refused_ctl_response_fails_the_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ok: false`` from the harness is a stage failure, not something to
    discover three stages later when nothing came up."""
    import ams.control

    monkeypatch.setattr(
        ams.control, "request", lambda *a, **kw: {"ok": False, "error": "unknown id"}
    )
    state = StateDir(tmp_path / "state")
    state.ensure()
    with pytest.raises(RuntimeError, match="refused"):
        layer0._ctl(state, "stop", "nope")


# --------------------------------------------------------------------------- caddy port


def test_caddy_declaration_gets_the_fixed_port(tmp_path: Path) -> None:
    """The rendered declaration must ask for :data:`layer0.CADDY_PORT`, not 0,
    and must still parse -- this is the one place gateway.py's text is edited."""
    from ams.schema import loads

    state = StateDir(tmp_path / "state")
    store = RuntimeStore(tmp_path / "store")
    text = layer0._caddy_declaration_text(state, store, layer0.CADDY_PORT)
    decl = loads(text)
    assert decl.ports == {"main": layer0.CADDY_PORT}
    assert decl.id == layer0.CADDY_ID


def test_a_gateway_generator_that_stops_asking_for_0_is_caught(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If T2.2 ever changes the declaration's port line, fail loudly rather than
    silently shipping a Caddy that allocates a port its own config cannot know."""
    monkeypatch.setattr(
        layer0.gateway_mod, "caddy_declaration", lambda st, sto, port_name="main": "main = 9\n"
    )
    with pytest.raises(RuntimeError, match="stale"):
        layer0._caddy_declaration_text(
            StateDir(tmp_path / "s"), RuntimeStore(tmp_path / "t"), layer0.CADDY_PORT
        )


# --------------------------------------------------------------------------- artefacts


def test_declarations_and_sidecars_land_on_disk(rig: _Rig) -> None:
    _run(rig)
    for service_id in ("kvservice", "timeservice"):
        assert rig.state.service_decl_path(service_id).is_file()
        mount = json.loads(
            (rig.state.root / "platform" / "mounts" / f"{service_id}.json").read_text()
        )
        assert mount["id"] == service_id
        reg = json.loads(
            (rig.state.root / "platform" / "registry" / f"{service_id}.json").read_text()
        )
        assert reg["audience"] == service_id
    assert rig.state.service_decl_path(layer0.CADDY_ID).is_file()


def test_no_layer1_runs_every_stage_and_touches_no_layer1_service(rig: _Rig) -> None:
    """A fresh host has no pilot services to re-point and, since pools, must not
    get standalone kvservice/timeservice declarations that the sync tick would
    then have to adopt into pool-core. ``layer1={}`` brings up Layer 0 only: every
    stage label is still reported (the report shape does not change), but no
    Layer-1 service is stopped, staged, registered or started, and the gateway
    is rendered with zero mounts."""
    report = layer0.bring_up(rig.state, rig.store, repo_url=REPO_URL, ref="main", layer1={})
    assert report.stages == list(layer0.STAGES)
    assert report.failed_stage is None
    assert not rig.has("ctl:stop:")
    assert not rig.has("ctl:start:")
    assert not rig.has("stage:kvservice")
    assert not rig.has("identity:")
    assert report.identities == {}
    assert rig.state.service_decl_path(layer0.CADDY_ID).is_file()
    assert set(report.health) == {REGISTRY_ID, AUTH_ID, layer0.CADDY_ID}


def test_cli_no_layer1_passes_an_empty_pilot_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: dict[str, Any] = {}

    def fake_bring_up(state: Any, store: Any, **kw: Any) -> Any:
        seen.update(kw)
        return SimpleNamespace(as_dict=lambda: {"ok": True})

    monkeypatch.setattr(layer0, "bring_up", fake_bring_up)
    common = ["--repo-url", REPO_URL, "--state", str(tmp_path / "s"), "--store", str(tmp_path / "r")]
    assert layer0.main([*common, "--no-layer1"]) == 0
    assert seen["layer1"] == {}
    seen.clear()
    assert layer0.main(common) == 0
    assert seen["layer1"] == layer0.DEFAULT_LAYER1_MANIFESTS
    capsys.readouterr()


def test_report_carries_no_secret(rig: _Rig) -> None:
    """The admin token and both SVC_SECRETs pass through this module; none of
    them may reach the report, which ``main`` prints verbatim."""
    report = _run(rig)
    blob = json.dumps(report.as_dict())
    assert "TOKEN-NOT-REAL" not in blob
    assert "secret-kvservice" not in blob
    assert report.blocks[REGISTRY_ID] > 0
    assert report.ports[layer0.CADDY_ID] == layer0.CADDY_PORT
    assert report.identities == {"kvservice": True, "timeservice": True}


def test_a_second_run_is_the_same_sequence(rig: _Rig) -> None:
    """Idempotence at the orchestration level: repairing a partial bring-up is
    a plain re-run, so the second pass must not skip or reorder anything."""
    _run(rig)
    first = list(rig.calls)
    rig.calls.clear()
    _run(rig)
    assert rig.calls == first
