"""ams.platform.core: config, layout, declaration, config bundle (portable, plain mode)."""

from __future__ import annotations

import json
import logging
import os
import shutil
import stat
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from ams import schema
from ams.platform import core
from ams.platform.core import CoreConfigError, CoreLayout, load_config, loads_config
from ams.state import StateDir

URL = "/home/harness/store/upstream/api.git"

BASE = f"""\
[core]
mirror = "api"
url = "{URL}"
ref = "main"
node = "24.20.0"
pnpm = "11.19.0"
gateway_port = 18080
extra_ports = [18081]
memory_max = "900M"
log_level = "info"
probation_timeout_s = 120
plugins = ["secrets", "store", "gateway", "auth", "health", "timeservice"]
core_paths = ["packages/core/", "package.json", "pnpm-lock.yaml"]

[core.privileges]
health = ["ops.read"]
"auto-ops" = ["ops.read", "ops.deploy"]

[[site]]
host = "api.lishuyu.app"
port = 18080
"""


@pytest.fixture
def state() -> Iterator[StateDir]:
    # Short root in /tmp: the socket path under it must fit in sun_path (104 bytes
    # on macOS), and pytest's tmp_path alone is longer than that.
    root = Path(tempfile.mkdtemp(prefix="ams-core-", dir="/tmp"))
    try:
        yield StateDir(root / "s")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _cfg(state: StateDir, text: str = BASE) -> core.CoreConfig:
    return loads_config(text, state=state)


# ------------------------------------------------------------------ config


def test_loads_full_config(state: StateDir) -> None:
    cfg = _cfg(state)
    assert cfg.mirror == "api"
    assert cfg.url == URL
    assert cfg.node == "24.20.0" and cfg.pnpm == "11.19.0"
    assert cfg.gateway_port == 18080
    assert cfg.extra_ports == (18081,)
    assert cfg.plugins == ("secrets", "store", "gateway", "auth", "health", "timeservice")
    assert cfg.privileges == {"health": ("ops.read",), "auto-ops": ("ops.read", "ops.deploy")}
    assert cfg.sites == (core.SiteConfig(host="api.lishuyu.app", port=18080),)
    assert cfg.probation_timeout_s == 120.0
    # defaults
    assert cfg.caddy_port == core.DEFAULT_CADDY_PORT


def test_load_config_reads_file_and_derives_state(state: StateDir) -> None:
    st = state
    path = core.config_path(st)
    path.parent.mkdir(parents=True)
    path.write_text(BASE, encoding="utf-8")
    cfg = load_config(path)
    assert cfg.gateway_port == 18080


def test_missing_config_file_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(CoreConfigError, match="no core config"):
        load_config(tmp_path / "nope.toml")


@pytest.mark.parametrize(
    "mutation, needle",
    [
        (lambda t: t + "\n[extra]\nx = 1\n", "unknown top-level"),
        (lambda t: t.replace('ref = "main"', 'ref = "main"\nbogus = 1'), "core: unknown keys"),
        (lambda t: t + "weight = 2\n", "site[0]: unknown"),
        (lambda t: t.replace('node = "24.20.0"', 'node = "24"'), "core.node"),
        (lambda t: t.replace('pnpm = "11.19.0"', 'pnpm = "latest"'), "core.pnpm"),
        (lambda t: t.replace('log_level = "info"', 'log_level = "verbose"'), "core.log_level"),
        (lambda t: t.replace('memory_max = "900M"', 'memory_max = "lots"'), "core.memory_max"),
        (lambda t: t.replace("gateway_port = 18080", "gateway_port = 80"), "core.gateway_port"),
        (lambda t: t.replace("extra_ports = [18081]", "extra_ports = [18080]"), "extra_ports"),
        (
            lambda t: t.replace('"auto-ops" = ["ops.read", "ops.deploy"]', '"auto-ops" = ["root"]'),
            "core.privileges",
        ),
        (lambda t: t.replace('health = ["ops.read"]', '"Bad" = ["ops.read"]'), "not a plugin id"),
        (lambda t: t.replace('host = "api.lishuyu.app"', 'host = "api lishuyu"'), "site[0].host"),
        (
            lambda t: t.replace('url = "' + URL + '"', 'url = "https://tok:en@github.com/x/api"'),
            "core.url",
        ),
        (lambda t: t.replace('"timeservice"]', '"timeservice", "timeservice"]'), "twice"),
        (lambda t: t.replace('"timeservice"]', '"Bad_Id"]'), "core.plugins"),
        (
            lambda t: t.replace("probation_timeout_s = 120", "probation_timeout_s = 0"),
            "probation_timeout_s",
        ),
    ],
)
def test_config_rejections(state: StateDir, mutation, needle: str) -> None:  # noqa: ANN001
    with pytest.raises(CoreConfigError, match=needle.replace("[", r"\[").replace("]", r"\]")):
        _cfg(state, mutation(BASE))


def test_foundation_order_enforced(state: StateDir) -> None:
    text = BASE.replace(
        '["secrets", "store", "gateway", "auth", "health", "timeservice"]',
        '["store", "secrets", "gateway", "auth", "health", "timeservice"]',
    )
    with pytest.raises(CoreConfigError, match="foundation"):
        _cfg(state, text)


def test_foundation_must_precede_other_plugins(state: StateDir) -> None:
    text = BASE.replace(
        '["secrets", "store", "gateway", "auth", "health", "timeservice"]',
        '["secrets", "timeservice", "store", "gateway", "auth", "health"]',
    )
    with pytest.raises(CoreConfigError, match="foundation"):
        _cfg(state, text)


def test_partial_foundation_keeps_relative_order(state: StateDir) -> None:
    text = BASE.replace(
        '["secrets", "store", "gateway", "auth", "health", "timeservice"]',
        '["secrets", "gateway", "timeservice"]',
    ).replace('health = ["ops.read"]\n"auto-ops" = ["ops.read", "ops.deploy"]\n', "")
    assert _cfg(state, text).plugins == ("secrets", "gateway", "timeservice")


def test_site_port_must_be_a_core_listener(state: StateDir) -> None:
    text = BASE.replace(
        '[[site]]\nhost = "api.lishuyu.app"\nport = 18080',
        '[[site]]\nhost = "x.lishuyu.app"\nport = 19999',
    )
    with pytest.raises(CoreConfigError, match="site"):
        _cfg(state, text)


def test_socket_path_length_is_validated(tmp_path: Path) -> None:
    deep = StateDir(tmp_path / ("d" * 120))
    with pytest.raises(CoreConfigError, match="socket path"):
        loads_config(BASE, state=deep)


# ------------------------------------------------------------------ layout


def test_layout_paths(state: StateDir) -> None:
    lay = CoreLayout.from_state(state)
    root = state.service_root("core")
    assert lay.root == root
    assert lay.releases == root / "releases"
    assert lay.current == root / "current"
    assert lay.data == root / "data"
    assert lay.etc == root / "etc"
    assert lay.run == root / "run"
    assert lay.socket == root / "run" / "control.sock"
    assert lay.build == root / "build"
    assert lay.release_dir("a" * 40) == root / "releases" / ("a" * 40)


def test_flip_current_plain(state: StateDir) -> None:
    lay = CoreLayout.from_state(state)
    a, b = "a" * 40, "b" * 40
    lay.release_dir(a).mkdir(parents=True)
    lay.release_dir(b).mkdir(parents=True)
    core.flip_current(lay, a, None)
    assert os.readlink(lay.current) == f"releases/{a}"
    core.flip_current(lay, b, None)
    assert os.readlink(lay.current) == f"releases/{b}"
    assert core.current_sha(lay) == b


def test_current_sha_none_when_absent(state: StateDir) -> None:
    assert core.current_sha(CoreLayout.from_state(state)) is None


# ------------------------------------------------------------- declaration


def test_declaration_passes_schema_and_says_what_it_should(state: StateDir) -> None:
    cfg = _cfg(state)
    lay = CoreLayout.from_state(state)
    text = core.core_declaration(cfg, lay)
    decl = schema.loads(text)
    schema.validate(decl)
    assert decl.id == "core"
    assert decl.start.argv == ("node", "dist/core/main.js")
    assert decl.start.workdir == "current"
    assert decl.runtime.kind == "pnpm"
    assert decl.runtime.node == "24.20.0"
    assert getattr(decl.runtime, "pnpm", None) == "11.19.0"
    assert tuple(getattr(decl.runtime, "build", ())) == ("node", "scripts/build.mjs")
    assert decl.env["CORE_STATE_DIR"] == str(lay.data)
    assert decl.env["CORE_SOCKET"] == str(lay.socket)
    assert decl.env["CORE_PLUGIN_CONFIG"] == str(lay.etc / "plugins.json")
    assert decl.env["CORE_LOG_LEVEL"] == "info"
    assert dict(decl.ports) == {"gateway": 18080, "port_18081": 18081}
    assert decl.health.kind == "http"
    assert decl.health.port == "gateway"
    assert decl.health.path == "/health"
    assert decl.health.start_period_s == 60.0
    assert decl.stop.signal == "SIGTERM" and decl.stop.timeout_s == 40.0
    assert decl.restart.policy == "always"
    assert decl.limits.memory_max == "900M"
    assert decl.logging.format == "json"


def test_declaration_is_deterministic(state: StateDir) -> None:
    cfg = _cfg(state)
    lay = CoreLayout.from_state(state)
    assert core.core_declaration(cfg, lay) == core.core_declaration(cfg, lay)


# ------------------------------------------------------------------ bundle

PLUGINS_JSON = {
    "plugins": {
        "store": {"config": {"dataDirectory": "/var/lib/core/data", "other": "/var/lib/core2"}},
        "gateway": {"config": {"port": 18080}},
        "auth": {
            "config": {
                "jwtPrivateKeyFile": "/etc/core/jwt.pem",
                "sessionSecret": "TOPSECRET-VALUE-123",
            },
            "grants": ["x"],
        },
        "pages": {"config": {"port": 18081, "root": "/srv/elsewhere"}},
    },
    "redactionReaders": ["logservice"],
}


def _write_bundle(tmp_path: Path, data: object = PLUGINS_JSON) -> Path:
    src = tmp_path / "in"
    src.mkdir(exist_ok=True)
    p = src / "plugins.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_import_bundle_rebases_writes_master_and_returns_names_only(
    state: StateDir, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cfg = _cfg(state)
    lay = CoreLayout.from_state(state)
    jwt = tmp_path / "jwt"
    jwt.mkdir()
    (jwt / "jwt.pem").write_text("PRIVATE-KEY-BYTES", encoding="utf-8")
    (jwt / "jwt.pub").write_text("PUBLIC-KEY-BYTES", encoding="utf-8")
    fonts = tmp_path / "fonts"
    (fonts / "sub").mkdir(parents=True)
    (fonts / "a.ttf").write_bytes(b"\x00font")
    (fonts / "sub" / "b.ttf").write_bytes(b"\x00font2")

    caplog.set_level(logging.DEBUG)
    names = core.import_bundle(
        state,
        _write_bundle(tmp_path),
        rebase=[("/var/lib/core", "@data"), ("/etc/core", "@etc")],
        jwt_dir=jwt,
        fonts_dir=fonts,
        cfg=cfg,
    )
    assert names == ["fonts/a.ttf", "fonts/sub/b.ttf", "jwt.pem", "jwt.pub", "plugins.json"]
    master = core.bundle_master_dir(state)
    written = json.loads((master / "plugins.json").read_text(encoding="utf-8"))
    store_cfg = written["plugins"]["store"]["config"]
    assert store_cfg["dataDirectory"] == str(lay.data / "data")
    # a prefix match must stop at a path boundary
    assert store_cfg["other"] == "/var/lib/core2"
    assert written["plugins"]["auth"]["config"]["jwtPrivateKeyFile"] == str(lay.etc / "jwt.pem")
    assert written["plugins"]["auth"]["config"]["sessionSecret"] == "TOPSECRET-VALUE-123"
    # perms: dirs 0700, files 0600
    assert stat.S_IMODE(master.stat().st_mode) == 0o700
    assert stat.S_IMODE((master / "fonts").stat().st_mode) == 0o700
    for rel in names:
        assert stat.S_IMODE((master / rel).stat().st_mode) == 0o600, rel
    # never a value in the log
    assert "TOPSECRET" not in caplog.text
    assert "PRIVATE-KEY-BYTES" not in caplog.text
    # an absolute path left outside the core root is reported by its key path only
    assert "plugins.pages.config.root" in caplog.text
    assert "/srv/elsewhere" not in caplog.text


def test_import_bundle_replaces_previous_master(state: StateDir, tmp_path: Path) -> None:
    cfg = _cfg(state)
    fonts = tmp_path / "fonts"
    fonts.mkdir()
    (fonts / "old.ttf").write_bytes(b"x")
    core.import_bundle(state, _write_bundle(tmp_path), fonts_dir=fonts, cfg=cfg)
    names = core.import_bundle(state, _write_bundle(tmp_path), cfg=cfg)
    assert names == ["plugins.json"]
    assert not (core.bundle_master_dir(state) / "fonts").exists()


def test_import_bundle_gateway_port_mismatch(state: StateDir, tmp_path: Path) -> None:
    cfg = _cfg(state)
    data = json.loads(json.dumps(PLUGINS_JSON))
    data["plugins"]["gateway"]["config"]["port"] = 18090
    with pytest.raises(CoreConfigError, match="gateway"):
        core.import_bundle(state, _write_bundle(tmp_path, data), cfg=cfg)
    assert not core.bundle_master_dir(state).exists()


def test_import_bundle_missing_gateway_config(state: StateDir, tmp_path: Path) -> None:
    cfg = _cfg(state)
    data = {"plugins": {"store": {"config": {}}}}
    with pytest.raises(CoreConfigError, match="gateway"):
        core.import_bundle(state, _write_bundle(tmp_path, data), cfg=cfg)


@pytest.mark.parametrize(
    "data, needle",
    [
        ([], "object"),
        ({"plugins": []}, "plugins"),
        ({"plugins": {"gateway": {"config": {"port": 18080}}}, "extra": 1}, "unknown"),
        ({"plugins": {"gateway": {"config": {"port": 18080}, "nope": 1}}}, "unknown"),
        (
            {"plugins": {"gateway": {"config": {"port": 18080}}}, "redactionReaders": "x"},
            "redactionReaders",
        ),
        ({"plugins": {"gateway": {"config": {"port": 18080}, "grants": "x"}}}, "grants"),
    ],
)
def test_import_bundle_shape_rejections(
    state: StateDir, tmp_path: Path, data: object, needle: str
) -> None:
    with pytest.raises(CoreConfigError, match=needle):
        core.import_bundle(state, _write_bundle(tmp_path, data), cfg=_cfg(state))


def test_import_bundle_invalid_json(state: StateDir, tmp_path: Path) -> None:
    p = tmp_path / "plugins.json"
    p.write_text("{nope", encoding="utf-8")
    with pytest.raises(CoreConfigError, match="JSON"):
        core.import_bundle(state, p, cfg=_cfg(state))


def test_import_bundle_bad_rebase(state: StateDir, tmp_path: Path) -> None:
    with pytest.raises(CoreConfigError, match="rebase"):
        core.import_bundle(
            state, _write_bundle(tmp_path), rebase=[("relative", "@data")], cfg=_cfg(state)
        )
    with pytest.raises(CoreConfigError, match="rebase"):
        core.import_bundle(
            state, _write_bundle(tmp_path), rebase=[("/var/lib/core", "@nowhere")], cfg=_cfg(state)
        )


def test_jwt_dir_missing_files(state: StateDir, tmp_path: Path) -> None:
    jwt = tmp_path / "jwt"
    jwt.mkdir()
    with pytest.raises(CoreConfigError, match="jwt"):
        core.import_bundle(state, _write_bundle(tmp_path), jwt_dir=jwt, cfg=_cfg(state))


def test_place_bundle_plain_copies_only_when_changed(state: StateDir, tmp_path: Path) -> None:
    cfg = _cfg(state)
    lay = CoreLayout.from_state(state)
    assert core.place_bundle(state, lay, None) is False  # nothing imported yet
    core.import_bundle(state, _write_bundle(tmp_path), cfg=cfg)
    assert core.place_bundle(state, lay, None) is True
    placed = lay.etc / "plugins.json"
    assert placed.is_file()
    assert stat.S_IMODE(lay.etc.stat().st_mode) == 0o700
    assert stat.S_IMODE(placed.stat().st_mode) == 0o600
    assert core.place_bundle(state, lay, None) is False
    data = json.loads(json.dumps(PLUGINS_JSON))
    data["redactionReaders"] = ["a", "b"]
    core.import_bundle(state, _write_bundle(tmp_path, data), cfg=cfg)
    assert core.place_bundle(state, lay, None) is True
    assert json.loads(placed.read_text(encoding="utf-8"))["redactionReaders"] == ["a", "b"]


def test_ensure_layout_plain(state: StateDir) -> None:
    lay = CoreLayout.from_state(state)
    core.ensure_layout(lay, None)
    for d in (lay.releases, lay.data, lay.etc, lay.run, lay.build):
        assert d.is_dir()
    assert stat.S_IMODE(lay.etc.stat().st_mode) == 0o700
