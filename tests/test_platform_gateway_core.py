"""Tests for `ams.platform.gateway.render_core` (PLAN-core §4.2).

In core mode Caddy is a thin host -> port front: core's own `gateway` plugin is
the single HTTP entry and owns routing, auth, CORS and every security header
(each plugin route sets its own CSP), so the Caddy side is deliberately small.
What must hold, and is pinned here:

* the output shape is ``Caddyfile`` + ``sites/<host>.caddy``, and `write()`
  sweeps snippets of sites that are gone;
* the harness health probe on the entry site survives, because the caddy
  service declaration health-checks ``/ams-health``;
* plain HTTP only (TLS terminates at Cloudflare / the tunnel), no admin API;
* every value that lands unquoted in the Caddyfile is validated first;
* the text is `caddy fmt`-canonical (checked against a real caddy when one is
  on PATH).

Regenerate the goldens with
``AMS_UPDATE_GOLDEN=1 pytest tests/test_platform_gateway_core.py`` and read the
diff before committing it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from ams.platform import gateway
from ams.platform.gateway import CoreSite, GatewayConfig, GatewayError, render_core
from ams.state import StateDir

LISTEN_PORT = 8080


def cfg(**overrides: object) -> GatewayConfig:
    """The renderer config every scenario uses; Caddy listens on 8080."""
    return GatewayConfig(listen_port=LISTEN_PORT, **overrides)  # type: ignore[arg-type]


GOLDEN_DIR = Path(__file__).parent / "golden" / "gateway" / "core"
LOG_DIR = Path("/srv/ams/logs")

API = CoreSite(host="api.lishuyu.app", port=18080)
PAGES = CoreSite(host="pages.shuyuli.com", port=18081)

SCENARIOS: dict[str, tuple[list[CoreSite], gateway.GatewayConfig]] = {
    # The production shape: the api host on core's gateway port, pages on the
    # pages plugin's own listener. Input order is deliberately not sorted.
    "basic": ([PAGES, API], cfg()),
    # Same sites with file access logs on: the deployer's roll block per site.
    "logdir": ([API, PAGES], cfg(log_dir=LOG_DIR)),
    # No site yet (first bootstrap): the entry site alone, still health-probeable.
    "empty": ([], cfg()),
}


def rendered(name: str) -> dict[str, str]:
    sites, config = SCENARIOS[name]
    return render_core(sites, config)


# --------------------------------------------------------------------------- #
# Golden files
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_golden(scenario: str) -> None:
    files = rendered(scenario)
    base = GOLDEN_DIR / scenario
    if os.environ.get("AMS_UPDATE_GOLDEN"):
        for path in sorted(base.rglob("*")) if base.is_dir() else []:
            if path.is_file():
                path.unlink()
        for rel, content in files.items():
            out = base / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(content, encoding="utf-8")

    on_disk = {
        str(p.relative_to(base)): p.read_text(encoding="utf-8")
        for p in sorted(base.rglob("*"))
        if p.is_file()
    }
    assert on_disk == files


# --------------------------------------------------------------------------- #
# Shape
# --------------------------------------------------------------------------- #


def test_output_shape() -> None:
    files = rendered("basic")
    assert sorted(files) == [
        "Caddyfile",
        "sites/api.lishuyu.app.caddy",
        "sites/pages.shuyuli.com.caddy",
    ]


def test_each_site_is_a_plain_http_reverse_proxy_to_its_loopback_port() -> None:
    site = rendered("basic")["sites/api.lishuyu.app.caddy"]
    assert "http://api.lishuyu.app:8080 {" in site
    assert "\treverse_proxy 127.0.0.1:18080\n" in site
    assert "https://" not in site
    # Core's plugins set their own per-route headers; Caddy must not override.
    assert "header " not in site


def test_every_site_has_an_access_log() -> None:
    for name in ("sites/api.lishuyu.app.caddy", "sites/pages.shuyuli.com.caddy"):
        text = rendered("basic")[name]
        assert "\tlog {\n\t\tformat json\n\t}\n" in text


def test_log_dir_renders_one_rolled_file_per_host() -> None:
    site = rendered("logdir")["sites/pages.shuyuli.com.caddy"]
    assert f"output file {LOG_DIR}/pages.shuyuli.com-access.log {{" in site
    assert "roll_size 50MiB" in site


def test_caddyfile_turns_off_https_and_the_admin_api() -> None:
    caddyfile = rendered("basic")["Caddyfile"]
    head = caddyfile.split("\n}\n", 1)[0] + "\n"
    assert "\tauto_https off\n" in head
    assert "\tadmin off\n" in head
    assert "\tpersist_config off\n" in head


def test_entry_site_keeps_the_harness_health_probe() -> None:
    caddyfile = rendered("empty")["Caddyfile"]
    assert "http://:8080 {" in caddyfile
    assert f"handle {gateway.HEALTH_PATH} {{" in caddyfile
    assert 'respond "ok" 200' in caddyfile


def test_sites_are_imported_explicitly_in_sorted_order_never_by_glob() -> None:
    caddyfile = rendered("basic")["Caddyfile"]
    imports = [line for line in caddyfile.splitlines() if line.startswith("import ")]
    assert imports == [
        "import sites/api.lishuyu.app.caddy",
        "import sites/pages.shuyuli.com.caddy",
    ]
    assert "*" not in "".join(imports)


def test_global_options_are_passed_through() -> None:
    caddyfile = render_core([API], cfg(global_options=("grace_period 5s",)))["Caddyfile"]
    assert "\tgrace_period 5s\n" in caddyfile.split("\n}\n", 1)[0] + "\n"


def test_render_core_is_pure_and_order_independent() -> None:
    assert render_core([API, PAGES], cfg()) == render_core([PAGES, API], cfg())


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "host",
    [
        "",
        "*.lishuyu.app",  # a wildcard would also turn `import` into a glob
        "api.lishuyu.app.",
        ".lishuyu.app",
        "api..lishuyu.app",
        "-api.lishuyu.app",
        "api-.lishuyu.app",
        "API.lishuyu.app",
        "api lishuyu.app",
        "api.lishuyu.app {",
        "http://api.lishuyu.app",
        "api.lishuyu.app:443",
        "api_x.lishuyu.app",
        "a" * 64 + ".example",
        ".".join(["a" * 63] * 4),  # 255 chars: over the 253 total
    ],
)
def test_a_host_that_is_not_a_strict_hostname_is_rejected(host: str) -> None:
    with pytest.raises(GatewayError, match="host"):
        CoreSite(host=host, port=18080)


@pytest.mark.parametrize("port", [0, 80, 1023, 65536, -1, True])
def test_a_port_outside_1024_65535_is_rejected(port: int) -> None:
    with pytest.raises(GatewayError, match="port"):
        CoreSite(host="api.lishuyu.app", port=port)


def test_a_non_string_host_is_rejected() -> None:
    with pytest.raises(GatewayError, match="host"):
        CoreSite(host=123, port=18080)  # type: ignore[arg-type]


def test_boundary_ports_are_accepted() -> None:
    assert CoreSite("a.example", 1024).port == 1024
    assert CoreSite("a.example", 65535).port == 65535


def test_duplicate_hosts_are_rejected() -> None:
    with pytest.raises(GatewayError, match="duplicate"):
        render_core([API, CoreSite("api.lishuyu.app", 18081)], cfg())


def test_a_site_on_the_entry_host_is_rejected() -> None:
    with pytest.raises(GatewayError, match="entry"):
        render_core([CoreSite("127.0.0.1", 18080)], cfg())


def test_a_site_proxying_to_caddys_own_port_is_rejected() -> None:
    with pytest.raises(GatewayError, match="loop"):
        render_core([CoreSite("api.lishuyu.app", 8080)], cfg())


def test_tls_is_refused_in_core_mode() -> None:
    with pytest.raises(GatewayError, match="plain HTTP"):
        render_core([API], cfg(plain_http=False))


def test_a_non_coresite_is_rejected() -> None:
    with pytest.raises(GatewayError, match="CoreSite"):
        render_core([("api.lishuyu.app", 18080)], cfg())  # type: ignore[list-item]


# --------------------------------------------------------------------------- #
# write() reuse
# --------------------------------------------------------------------------- #


def test_write_sweeps_snippets_of_sites_that_are_gone(tmp_path: Path) -> None:
    state = StateDir(tmp_path / "state")
    gone = render_core([CoreSite("files.lishuyu.app", 9206)], cfg())
    gateway.write(state, gone)

    changed = gateway.write(state, rendered("basic"))

    sites = gateway.gateway_dir(state) / "sites"
    assert sorted(p.name for p in sites.iterdir()) == [
        "api.lishuyu.app.caddy",
        "pages.shuyuli.com.caddy",
    ]
    assert sites / "files.lishuyu.app.caddy" in changed
    assert gateway.write(state, rendered("basic")) == []


# --------------------------------------------------------------------------- #
# Against a real caddy, when one is installed (dev machines; not the CI box)
# --------------------------------------------------------------------------- #

CADDY = shutil.which("caddy")
needs_caddy = pytest.mark.skipif(CADDY is None, reason="caddy not on PATH")


@needs_caddy
@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_output_is_caddy_fmt_canonical(scenario: str, tmp_path: Path) -> None:
    for rel, content in rendered(scenario).items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        proc = subprocess.run(
            [str(CADDY), "fmt", str(path)], capture_output=True, text=True, check=False
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == content, f"{rel} is not caddy-fmt canonical"


@needs_caddy
def test_output_passes_caddy_validate(tmp_path: Path) -> None:
    for rel, content in rendered("basic").items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    proc = subprocess.run(
        [
            str(CADDY),
            "validate",
            "--adapter",
            "caddyfile",
            "--config",
            str(tmp_path / "Caddyfile"),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(tmp_path),
    )
    assert proc.returncode == 0, proc.stderr


# --------------------------------------------------------------------------- #
# The caddy service declaration
# --------------------------------------------------------------------------- #


def test_the_caddy_declaration_is_a_valid_fixed_port_service(tmp_path: Path) -> None:
    from ams.schema import loads

    state = StateDir(tmp_path / "state")
    decl = loads(gateway.caddy_declaration(state, tmp_path / "store", 20180))
    assert decl.id == gateway.CADDY_SERVICE_ID
    assert decl.ports == {"main": 20180}
    assert decl.health.kind == "http" and decl.health.path == gateway.HEALTH_PATH
    assert decl.start.argv[0] == str(tmp_path / "store" / "bin" / "caddy")
    assert decl.restart.policy == "always"


@pytest.mark.parametrize("port", [0, 80, 65536, True, "20180"])
def test_the_caddy_declaration_refuses_a_bad_port(tmp_path: Path, port: object) -> None:
    with pytest.raises(GatewayError, match="caddy port"):
        gateway.caddy_declaration(StateDir(tmp_path), tmp_path, port)  # type: ignore[arg-type]


def test_the_entry_site_catches_every_host_without_a_site() -> None:
    """Found live: with the entry site on `http://127.0.0.1:<port>` only Host
    127.0.0.1 reached the JSON 404, and any other unknown host got Caddy's
    default empty 200 -- indistinguishable from a working route. The entry is a
    port-wide catch-all; Caddy matches the specific [[site]] hosts first."""
    caddyfile = rendered("basic")["Caddyfile"]
    assert f"http://:{LISTEN_PORT} {{" in caddyfile
    assert "http://127.0.0.1:" not in caddyfile
