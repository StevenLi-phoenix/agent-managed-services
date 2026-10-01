"""Core mode: the seams between the three PLAN-core slices (portable).

Task A (runtime toolchain, userns), Task B (core mode) and Task C (sources,
gateway, backup) were written in parallel against PLAN-core §4. These tests pin
the places where one slice calls another, so a later edit on either side of a
seam fails here instead of on a host:

- a ``core.toml`` that loads always renders through ``gateway.render_core``;
- the isolated layout keeps ``releases/`` traversable by the harness (the stage
  marker read, ``provision_tree``'s checks and the spawner's cwd walk need it);
- an isolated tick stages through ``SourceMirror.stage(dest="releases/<sha>")``
  and hands the uid block to provisioning and the planner;
- the §4 signatures B calls exist as B calls them;
- ``ams run --no-isolation`` gives a managed-node service its toolchain PATH;
- core's stop timeout fits the harness shutdown budget and the unit's.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import shutil
import tempfile
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest

# The fakes of the coresync suite model upstream core; reuse them rather than
# growing a second copy. Only helpers are imported (no test functions), so
# pytest does not collect that module's tests twice.
from test_platform_coresync import SHA_A, World

from ams.platform import core, coresync
from ams.platform.core import CoreConfigError, CoreLayout, loads_config
from ams.runtime import RuntimeStore
from ams.state import StateDir
from ams.uidmap import UidBlock
from ams.userns import AdminResult

REPO = Path(__file__).resolve().parent.parent

CONFIG = """\
[core]
url = "/srv/upstream/api.git"
node = "24.20.0"
pnpm = "11.19.0"
gateway_port = 18080
extra_ports = [18081]
plugins = ["secrets", "store", "gateway", "auth", "health"]

[[site]]
host = "{host}"
port = 18080
"""


@pytest.fixture
def state() -> Iterator[StateDir]:
    # Short root: the core socket path must fit in sun_path (104 bytes on macOS).
    root = Path(tempfile.mkdtemp(prefix="ams-ci-", dir="/tmp"))
    try:
        yield StateDir(root / "s")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# --------------------------------------------------------------- config -> gateway


@pytest.mark.parametrize(
    "host",
    [
        "*.lishuyu.app",  # a glob in `import sites/<host>.caddy`
        "API.lishuyu.app",  # Caddy matches case-insensitively: one site twice
        "api.lishuyu.app.",  # trailing dot
        "127.0.0.1",  # the entry (health-probe) host
    ],
)
def test_a_site_the_gateway_would_refuse_is_a_config_error(state: StateDir, host: str) -> None:
    with pytest.raises(CoreConfigError, match=r"site"):
        loads_config(CONFIG.format(host=host), state=state)


def test_a_loaded_config_always_renders(state: StateDir) -> None:
    from ams.platform import coresync as cs
    from ams.platform import gateway

    cfg = loads_config(CONFIG.format(host="api.lishuyu.app"), state=state)
    changed = cs.default_gateway(state, cfg)
    assert sorted(changed) == ["Caddyfile", "sites/api.lishuyu.app.caddy"]
    site = (gateway.gateway_dir(state) / "sites" / "api.lishuyu.app.caddy").read_text()
    assert f"http://api.lishuyu.app:{cfg.caddy_port}" in site
    assert "reverse_proxy 127.0.0.1:18080" in site
    # Unchanged on the next call: no caddy restart on a steady tick.
    assert cs.default_gateway(state, cfg) == []


# --------------------------------------------------------------- isolated layout


class FakeAdmin:
    """Plays the admin namespace locally: the file operations core mode issues.

    Ownership (``chown``) and GNU-only flags have no local meaning; the tree
    shape and the argv are what is checked.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str], block: Any, **_: Any) -> AdminResult:
        argv = list(argv)
        self.calls.append(argv)
        tool, args = argv[0], argv[1:]
        out = b""
        if tool == "mkdir":
            mode = None
            paths = []
            it = iter(args)
            for a in it:
                if a == "-m":
                    mode = int(next(it), 8)
                elif a != "-p":
                    paths.append(a)
            for p in paths:
                os.makedirs(p, exist_ok=True)
                if mode is not None:
                    os.chmod(p, mode)
        elif tool == "rm":
            for p in (a for a in args if not a.startswith("-")):
                path = Path(p)
                if path.is_symlink() or path.is_file():
                    path.unlink()
                elif path.is_dir():
                    shutil.rmtree(path)
        elif tool == "ln":
            os.symlink(args[-2], args[-1])
        elif tool == "mv":
            os.replace(args[-2], args[-1])
        elif tool == "cp":
            src, dst = args[-2], args[-1]
            if Path(src).is_dir():
                shutil.copytree(src, dst, symlinks=True)
            else:
                shutil.copy(src, dst)
        elif tool == "find":
            parent = Path(args[0])
            out = "".join(f"{p.name}\n" for p in parent.iterdir()).encode()
        elif tool not in ("chown", "chmod"):
            raise AssertionError(f"unexpected admin command {argv}")
        return AdminResult(tuple(argv), 0, out, b"")

    def modes(self) -> dict[str, str]:
        """``path -> mode`` of every ``mkdir -m`` issued."""
        seen: dict[str, str] = {}
        for argv in self.calls:
            if argv[0] == "mkdir" and "-m" in argv:
                mode = argv[argv.index("-m") + 1]
                for p in argv[argv.index("-m") + 2 :]:
                    if p != "-p":
                        seen[Path(p).name] = mode
        return seen


BLOCK = UidBlock(100000, 1024)


def test_isolated_layout_keeps_releases_traversable(
    state: StateDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Made as the service (security-2): the service's own mkdir makes it the owner.
    admin = FakeAdmin()
    monkeypatch.setattr(core, "_as_service", lambda argv, block: admin(argv, block).check())
    lay = CoreLayout.from_state(state)
    lay.root.mkdir(parents=True)
    core.ensure_layout(lay, BLOCK)
    assert admin.modes() == {
        "releases": "755",
        "data": "750",
        "run": "750",
        "build": "750",
        "etc": "700",
    }
    assert not any(c[0] == "chown" for c in admin.calls)
    admin.calls.clear()
    core.ensure_layout(lay, BLOCK)  # everything exists: no fork at all
    assert admin.calls == []


# --------------------------------------------------------------- isolated tick


class IsolatedMirror:
    """``FakeMirror`` with the namespaced ``stage`` instead of ``stage_plain``."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.stage_calls: list[dict[str, Any]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def stage_plain(self, *a: Any, **k: Any) -> Path:
        raise AssertionError("isolated mode must not stage_plain")

    def stage(self, sha: str, service_root: Path, block: UidBlock, *, dest: str = "repo") -> Path:
        self.stage_calls.append({"sha": sha, "root": service_root, "block": block, "dest": dest})
        tree = Path(service_root) / dest
        (tree / "scripts").mkdir(parents=True, exist_ok=True)
        (tree / ".ams-sha").write_text(sha + "\n", encoding="utf-8")
        return tree


def test_an_isolated_tick_stages_into_releases_and_passes_the_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ams.userns

    root = Path(tempfile.mkdtemp(prefix="ams-ci-", dir="/tmp"))
    try:
        state = StateDir(root / "s")
        w = World(state, RuntimeStore(root / "store"))
        bundle = root / "plugins.json"
        bundle.write_text(
            json.dumps({"plugins": {"gateway": {"config": {"port": 18080}}}}), encoding="utf-8"
        )
        core.import_bundle(state, bundle, cfg=w.cfg)

        admin = FakeAdmin()
        monkeypatch.setattr(core, "_admin", lambda argv, block: admin(argv, block).check())
        monkeypatch.setattr(core, "_as_service", lambda argv, block: admin(argv, block).check())
        monkeypatch.setattr(coresync, "_admin", lambda argv, block: admin(argv, block).check())
        monkeypatch.setattr(ams.userns, "run_admin", admin)
        roots: list[tuple[Path, UidBlock]] = []

        def fake_ensure_service_root(root_: Path, block: UidBlock, **_: Any) -> None:
            roots.append((root_, block))
            Path(root_).mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr(ams.userns, "ensure_service_root", fake_ensure_service_root)

        provision_blocks: list[Any] = []
        plain_provision = w.provision

        def provision(tree: Path, spec: Any, **kw: Any) -> None:
            provision_blocks.append(kw["block"])
            plain_provision(tree, spec, **kw)

        planner_blocks: list[Any] = []
        runner_blocks: list[Any] = []
        mirror = IsolatedMirror(w.mirror)

        def planner_factory(layout: CoreLayout, block: Any) -> Any:
            planner_blocks.append(block)
            return w.planner

        def runner_factory(block: Any) -> Any:
            runner_blocks.append(block)
            return object()

        from dataclasses import replace

        hooks = replace(
            w.hooks(),
            mirror_factory=lambda cfg, store: mirror,
            provision=provision,
            planner_factory=planner_factory,
            runner_factory=runner_factory,
            block_factory=lambda state_: BLOCK,
        )
        w.healthy.add(SHA_A)
        rep = coresync.tick(
            state,
            w.store,
            w.cfg,
            isolation=True,
            escalation=w.stream,
            now=w.clock.now,
            sleep=w.clock.sleep,
            hooks=hooks,
        )
        assert rep.error is None, rep.summary()
        assert rep.release_ok is True
        assert roots == [(w.layout.root, BLOCK)]
        assert mirror.stage_calls == [
            {"sha": SHA_A, "root": w.layout.root, "block": BLOCK, "dest": f"releases/{SHA_A}"}
        ]
        # install (stage) then install + build (release), both in the admin ns.
        assert w.provisioned == [(SHA_A, False), (SHA_A, True)]
        assert provision_blocks == [BLOCK, BLOCK]
        assert planner_blocks == [BLOCK] and runner_blocks == [BLOCK]
        # current flipped through the admin namespace, onto the staged release.
        assert core.current_sha(w.layout) == SHA_A
        # (the temporary link is built outside the service-owned root: security-6)
        assert ["mv", "-T", str(w.layout.root.parent / ".current.new"), str(w.layout.current)] in (
            admin.calls
        )
        # The bundle went into etc/ via the namespace, never a harness-side copy.
        assert (w.layout.etc / "plugins.json").is_file()
        assert any(c[:2] == ["cp", "-R"] for c in admin.calls)
        assert set(rep.live) == set(w.cfg.plugins)
    finally:
        shutil.rmtree(root, ignore_errors=True)


# --------------------------------------------------------------- §4 signatures


def _params(fn: Any) -> dict[str, inspect.Parameter]:
    return dict(inspect.signature(fn).parameters)


def test_the_calls_core_mode_makes_match_the_slices_that_serve_them() -> None:
    from ams import runtime, userns
    from ams.platform import gateway
    from ams.platform.sources import SourceMirror

    # coresync._Tick.stage / release -> SourceMirror (Task C)
    inspect.signature(SourceMirror.stage).bind(
        object(), SHA_A, Path("/r"), BLOCK, dest=f"releases/{SHA_A}"
    )
    inspect.signature(SourceMirror.stage_plain).bind(object(), SHA_A, Path("/r/releases/x"))
    # coresync.default_provision -> runtime.provision_tree (Task A), exactly its kwargs
    inspect.signature(runtime.provision_tree).bind(
        Path("/t"),
        object(),
        block=None,
        store=object(),
        run_build=False,
        log_path=Path("/l"),
        env={"CORE_SOURCE_COMMIT": SHA_A},
    )
    # default_toolchain / live_status -> the toolchain (Task A)
    inspect.signature(runtime.ensure_node_toolchain).bind(object(), "24.20.0", "11.19.0")
    tc = runtime.node_toolchain(RuntimeStore(Path("/st")), "24.20.0", "11.19.0")
    assert tc.bin_dirs == ("/st/pnpm/11.19.0/bin", "/st/node/v24.20.0/bin")
    assert tc.node_bin == Path("/st/node/v24.20.0/bin/node")
    # corectl.isolated_runner -> userns.run_as_service; backup -> run_admin(env=)
    inspect.signature(userns.run_as_service).bind(
        ["node"], BLOCK, env={"PATH": "/p"}, cwd="/c", timeout_s=1.0
    )
    assert {"env", "cwd"} <= set(_params(userns.run_admin))
    # default_gateway / bootstrap -> gateway
    inspect.signature(gateway.render_core).bind([], object())
    assert {"host", "port"} == set(_params(gateway.CoreSite))
    inspect.signature(gateway.caddy_declaration).bind(object(), object(), 20180)


def test_the_declared_runtime_is_the_managed_toolchain(state: StateDir) -> None:
    from ams import runtime, schema

    cfg = loads_config(CONFIG.format(host="api.lishuyu.app"), state=state)
    decl = schema.loads(core.core_declaration(cfg, CoreLayout.from_state(state)))
    assert decl.runtime.managed_node
    assert decl.runtime.build == core.BUILD_ARGV
    store = RuntimeStore(Path("/st"))
    tc = runtime.managed_toolchain(decl.runtime, store)
    assert tc is not None and tc.pnpm_version == "11.19.0"
    env = runtime.runtime_env(decl, state.service_root("core"), store)
    extra, prepend = env.as_tuple()
    assert prepend[0].endswith("current/node_modules/.bin")
    assert prepend[1:3] == tc.bin_dirs
    # a pinned pnpm replaces PNPM_HOME/bin, it is not added behind it
    assert str(store.pnpm_home / "bin") not in prepend


# --------------------------------------------------------------- ams run --no-isolation


def test_no_isolation_run_still_gets_the_managed_toolchain_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The local end-to-end runs core under ``ams run --no-isolation``: without the
    runtime layer the spawn PATH is /usr/local/bin:/usr/bin:/bin and core dies
    with ``node`` not found."""
    from ams.cli import build_supervisor

    store = tmp_path / "store"
    monkeypatch.setenv("AMS_STORE_DIR", str(store))
    state = StateDir(tmp_path / "state")
    decl_dir = state.services_dir / "web"
    decl_dir.mkdir(parents=True)
    (decl_dir / "service.toml").write_text(
        'id = "web"\n[start]\nargv = ["node", "main.js"]\n'
        '[runtime]\nkind = "pnpm"\nnode = "24.20.0"\npnpm = "11.19.0"\n',
        encoding="utf-8",
    )
    asm = build_supervisor(state, isolation=False)
    assert asm.registered == ["web"]
    extra, prepend = asm.supervisor._extra_env_for(asm.declarations["web"])
    assert str(store / "node" / "v24.20.0" / "bin") in prepend
    assert str(store / "pnpm" / "11.19.0" / "bin") in prepend


# --------------------------------------------------------------- stop budget


def test_core_stop_timeout_fits_the_harness_shutdown_budget_and_the_unit() -> None:
    from ams.cli import SHUTDOWN_BUDGET_S

    assert core.STOP_TIMEOUT_S <= SHUTDOWN_BUDGET_S
    unit = (REPO / "deploy" / "ams-harness.service").read_text(encoding="utf-8")
    m = re.search(r"^TimeoutStopSec=(\d+)$", unit, re.MULTILINE)
    assert m is not None
    # ams.cli: the supervisor spends up to the budget + ~1.5 s before it force-kills.
    assert int(m.group(1)) >= SHUTDOWN_BUDGET_S + 2
