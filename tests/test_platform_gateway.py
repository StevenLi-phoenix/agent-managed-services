"""Tests for `ams.platform.gateway`.

PLAN-allin risk 2 is "CSP/security headers lost porting Jinja to Python", so the
bulk of this file is not "does it render" but "does *every* header from
`api/components/deployer/templates/caddy*.j2` survive, per mount type". The
golden files pin the whole output; the per-header assertions say *why* each line
is there, so a future edit that quietly drops one fails with a readable message
instead of a diff.

Regenerate the goldens with ``AMS_UPDATE_GOLDEN=1 pytest tests/test_platform_gateway.py``
and read the diff before committing it.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from ams import schema
from ams.platform import gateway
from ams.platform.gateway import (
    ADMIN_CORS_MAP,
    CSP_MAP,
    DEFAULT_CSP,
    GatewayConfig,
    GatewayError,
)
from ams.state import StateDir

GOLDEN_DIR = Path(__file__).parent / "golden" / "gateway"

# Machine-independent stand-ins so the goldens are byte-stable everywhere.
STATIC_ROOT = Path("/srv/ams/static")
LOG_DIR = Path("/srv/ams/logs")
LISTEN_PORT = 8080


def mount(service_id: str, **over: object) -> dict[str, object]:
    """A `mount.json` document (docs/platform-sidecars.md) with defaults filled in."""
    base: dict[str, object] = {
        "version": 1,
        "id": service_id,
        "kind": "service",
        "gateway": "api.lishuyu.app",
        "path": None,
        "subdomain": None,
        "port_name": "main",
        "static_root": None,
        "build": [],
        "headers": {},
    }
    base.update(over)
    return base


def cfg(**over: object) -> GatewayConfig:
    kwargs: dict[str, object] = {"listen_port": LISTEN_PORT, "static_root": STATIC_ROOT}
    kwargs.update(over)
    return GatewayConfig(**kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Scenarios. Each is (mounts, ports, config) and has a golden directory.
# --------------------------------------------------------------------------- #

PATH_MOUNTS = [
    mount("files", path="/files"),
    mount("timeservice", path="/time"),
]
PATH_PORTS = {"files": 9206, "timeservice": 9203}

SUBDOMAIN_MOUNTS = [
    mount("displayservice", gateway="display.lishuyu.app", subdomain="display"),
    mount("locationservice", gateway="location.lishuyu.app", subdomain="location"),
    mount("pages", gateway="pages.shuyuli.com", subdomain="pages"),
]
SUBDOMAIN_PORTS = {"displayservice": 9207, "locationservice": 9210, "pages": 9216}

STATIC_MOUNTS = [
    mount(
        "docs-web",
        kind="static",
        path="/docs",
        port_name=None,
        static_root="docs-web",
        build=["bun install", "bun run build"],
    ),
    mount(
        "files-web",
        kind="static",
        gateway="file.lishuyu.app",
        subdomain="file",
        port_name=None,
        static_root="files-web",
    ),
]

SCENARIOS: dict[str, tuple[list[dict[str, object]], dict[str, int], GatewayConfig]] = {
    "path": (PATH_MOUNTS, PATH_PORTS, cfg()),
    "subdomain": (SUBDOMAIN_MOUNTS, SUBDOMAIN_PORTS, cfg()),
    "static": (STATIC_MOUNTS, {}, cfg()),
    # Same inputs as "path" but with file access logs on, which is the only way
    # the deployer's roll block reaches the output.
    "logdir": (PATH_MOUNTS, PATH_PORTS, cfg(log_dir=LOG_DIR)),
    # The Phase-B TLS flip: one flag, bare hostnames, no `auto_https off`.
    "tls": (
        PATH_MOUNTS + SUBDOMAIN_MOUNTS,
        {**PATH_PORTS, **SUBDOMAIN_PORTS},
        cfg(entry_host="api.lishuyu.app", plain_http=False),
    ),
}


def rendered(name: str) -> dict[str, str]:
    mounts, ports, config = SCENARIOS[name]
    return gateway.render(mounts, ports, config)


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
# Per-header assertions: every header in the ported templates, per mount type.
# --------------------------------------------------------------------------- #


def header_values(text: str, name: str) -> list[str]:
    """Values of every unconditional ``header <name> "..."`` line in ``text``.

    Matcher-scoped lines (``header @admin_cors ...``) are excluded: they are
    CORS, not the security header block.
    """
    out = []
    for line in text.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) == 3 and parts[0] == "header" and parts[1].lower() == name.lower():
            out.append(parts[2].strip('"'))
    return out


def test_path_service_gets_frame_and_nosniff_only() -> None:
    """caddy.snippet.j2: path-mounted services get frame/nosniff only — a full
    CSP would break HTML surfaces that load CDN assets behind the gateway (e.g.
    resume's Swagger /docs) and is meaningless for pure-JSON responses."""
    text = rendered("path")["sites/files.caddy"]
    assert header_values(text, "X-Frame-Options") == ["DENY"]
    assert header_values(text, "Content-Security-Policy") == ["frame-ancestors 'none'"]
    assert header_values(text, "X-Content-Type-Options") == ["nosniff"]
    assert header_values(text, "Referrer-Policy") == []
    assert "handle_path /files/* {" in text
    assert "reverse_proxy 127.0.0.1:9206" in text


def test_subdomain_service_gets_full_csp_and_referrer_policy() -> None:
    """caddy.snippet.j2: subdomain services are the gated UIs and get the full
    default CSP plus Referrer-Policy on top of frame/nosniff."""
    text = rendered("subdomain")["sites/displayservice.caddy"]
    assert header_values(text, "X-Frame-Options") == ["DENY"]
    assert header_values(text, "Content-Security-Policy") == [DEFAULT_CSP]
    assert header_values(text, "X-Content-Type-Options") == ["nosniff"]
    assert header_values(text, "Referrer-Policy") == ["strict-origin-when-cross-origin"]
    assert text.startswith("# displayservice")
    assert "http://display.lishuyu.app:8080 {" in text


def test_default_csp_is_verbatim() -> None:
    """The exact string from caddy.snippet.j2's `default_csp`."""
    assert DEFAULT_CSP == (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
        "form-action 'self'"
    )


def test_csp_map_override_locationservice() -> None:
    """locationservice pulls Leaflet from unpkg and tiles from OSM; its CSP must
    name both origins or the map goes blank."""
    text = rendered("subdomain")["sites/locationservice.caddy"]
    (csp,) = header_values(text, "Content-Security-Policy")
    assert csp == CSP_MAP["locationservice"]
    assert csp != DEFAULT_CSP
    assert "https://unpkg.com" in csp
    assert "https://tile.openstreetmap.org" in csp


def test_csp_map_override_pages_is_a_sandbox() -> None:
    """pages hosts untrusted user HTML: `sandbox` without allow-same-origin
    gives every document an opaque origin. Losing this line would hand hosted
    documents the platform's cookies."""
    text = rendered("subdomain")["sites/pages.caddy"]
    (csp,) = header_values(text, "Content-Security-Policy")
    assert csp == CSP_MAP["pages"]
    assert csp.startswith("sandbox allow-scripts")
    assert "allow-same-origin" not in csp
    assert "frame-ancestors 'none'" in csp


def test_admin_cors_only_for_mapped_subdomains_and_only_under_api() -> None:
    """admin_cors_map: exact-match admin origin, /api/* only, so it cannot leak
    to pages' sandboxed documents (whose `Origin: null` never matches)."""
    pages = rendered("subdomain")["sites/pages.caddy"]
    origin = ADMIN_CORS_MAP["pages"]
    assert origin == "https://admin.lishuyu.app"
    assert f'header Origin "{origin}"' in pages
    assert "path /api/*" in pages
    assert f'header @admin_cors Access-Control-Allow-Origin "{origin}"' in pages
    assert 'header @admin_cors Access-Control-Allow-Credentials "true"' in pages
    assert 'header @admin_cors Vary "Origin"' in pages
    assert "respond 204" in pages

    display = rendered("subdomain")["sites/displayservice.caddy"]
    assert "admin_cors" not in display
    assert "admin.lishuyu.app" not in display


def test_static_path_mount_headers_and_spa_fallback() -> None:
    """caddy.static.j2: frame-denial + nosniff only (no full CSP — files-web
    fetches presigned R2 URLs whose origin is runtime configuration), plus the
    extensionless-only SPA fallback."""
    text = rendered("static")["sites/docs-web.caddy"]
    assert header_values(text, "X-Frame-Options") == ["DENY"]
    assert header_values(text, "Content-Security-Policy") == ["frame-ancestors 'none'"]
    assert header_values(text, "X-Content-Type-Options") == ["nosniff"]
    assert header_values(text, "Referrer-Policy") == []
    assert "root * /srv/ams/static/docs-web" in text
    assert "not file" in text and "not path *.*" in text
    assert "rewrite @spa_fallback /index.html" in text
    assert "file_server" in text
    assert "reverse_proxy" not in text


def test_static_subdomain_never_gets_the_full_csp() -> None:
    """A static SPA on a subdomain still gets Referrer-Policy, but its CSP stays
    frame-ancestors-only: default_csp's connect-src 'self' would break uploads."""
    text = rendered("static")["sites/files-web.caddy"]
    assert header_values(text, "Content-Security-Policy") == ["frame-ancestors 'none'"]
    assert header_values(text, "Referrer-Policy") == ["strict-origin-when-cross-origin"]
    assert DEFAULT_CSP not in text
    assert "http://file.lishuyu.app:8080 {" in text
    assert "root * /srv/ams/static/files-web" in text


def test_entry_site_carries_the_bootstrap_cors_and_404() -> None:
    """Ported from bootstrap/04-caddy-init.sh: the first-party SPA CORS block,
    the root responder and the JSON 404 catch-all."""
    text = rendered("path")["Caddyfile"]
    assert "http://127.0.0.1:8080 {" in text
    assert "auto_https off" in text
    assert "admin off" in text
    assert "encode gzip zstd" in text
    assert gateway.ENTRY_CORS_ORIGIN_RE in text
    assert '"GET, POST, PUT, PATCH, DELETE, OPTIONS"' in text
    assert '"Authorization, Content-Type, X-Claim-Token, X-File-Password"' in text
    assert '"86400"' in text
    assert 'header @cors_origin Access-Control-Allow-Origin "{http.request.header.origin}"' in text
    assert 'respond `{"error":"not_found","detail":"no such service"}` 404' in text
    assert 'respond "lishuyu platform — see /<service>" 200' in text


def test_entry_site_serves_the_harness_health_path() -> None:
    text = rendered("path")["Caddyfile"]
    assert f"handle {gateway.HEALTH_PATH} {{" in text
    assert 'respond "ok" 200' in text


def test_path_mounts_are_imported_inside_the_entry_site_subdomains_at_root() -> None:
    """The deployer's services-api/ vs services/ split: a `handle_path` snippet
    is only legal inside a site block, a site block only at root scope."""
    lines = rendered("tls")["Caddyfile"].splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("api.lishuyu.app {"))
    end = next(i for i, line in enumerate(lines) if i > start and line == "}")
    inside, after = lines[start + 1 : end], lines[end + 1 :]
    for service_id in ("files", "timeservice"):
        assert f"\timport sites/{service_id}.caddy" in inside
    for service_id in ("displayservice", "locationservice", "pages"):
        assert f"import sites/{service_id}.caddy" in after
        assert f"\timport sites/{service_id}.caddy" not in inside


def test_pages_management_api_is_mounted_on_the_entry_host() -> None:
    """04-caddy-init.sh's hand-managed rule: the admin SPA's .lishuyu.app cookie
    never rides cross-site to pages.shuyuli.com, so /pages/api/* is proxied on
    the entry host too — and ONLY /pages/api/*, never the hosted HTML."""
    text = rendered("tls")["Caddyfile"]
    assert "handle /pages/api/* {" in text
    assert "uri strip_prefix /pages" in text
    assert "reverse_proxy 127.0.0.1:9216" in text
    assert "handle /pages/* {" not in text


def test_pages_rule_absent_when_pages_is_not_deployed() -> None:
    assert "/pages/api/*" not in rendered("path")["Caddyfile"]


def test_log_dir_none_keeps_caddys_stderr_which_the_harness_reads() -> None:
    text = rendered("path")["Caddyfile"]
    assert "format json" in text
    assert "output file" not in text


def test_log_dir_set_renders_the_deployer_roll_block() -> None:
    text = rendered("logdir")["Caddyfile"]
    assert "output file /srv/ams/logs/api-access.log {" in text
    assert "roll_size 50MiB" in text
    assert "roll_keep 10" in text
    assert "roll_keep_for 90d" in text
    site = rendered("logdir")["sites/files.caddy"]
    assert "output file" not in site  # path snippets have no log directive


def test_plain_http_flag_is_the_whole_tls_flip() -> None:
    text = rendered("tls")["Caddyfile"]
    assert "auto_https off" not in text
    assert "\napi.lishuyu.app {" in text
    assert "http://" not in text
    site = rendered("tls")["sites/pages.caddy"]
    assert "\npages.shuyuli.com {" in site
    # The security headers do not depend on the flip.
    assert header_values(site, "Content-Security-Policy") == [CSP_MAP["pages"]]


def test_empty_mount_set_still_renders_a_valid_entry_site() -> None:
    files = gateway.render([], {}, cfg())
    assert set(files) == {"Caddyfile"}
    assert "import sites/" not in files["Caddyfile"]
    assert gateway.HEALTH_PATH in files["Caddyfile"]


# --------------------------------------------------------------------------- #
# Per-mount header overrides
# --------------------------------------------------------------------------- #


def test_header_override_replaces_a_template_header_in_place() -> None:
    m = mount("files", path="/files", headers={"Content-Security-Policy": "default-src 'none'"})
    text = gateway.render([m], {"files": 9206}, cfg())["sites/files.caddy"]
    assert header_values(text, "Content-Security-Policy") == ["default-src 'none'"]
    assert header_values(text, "X-Frame-Options") == ["DENY"]
    lines = [line.strip() for line in text.splitlines() if line.strip().startswith("header ")]
    assert lines[1].startswith("header Content-Security-Policy")  # position preserved


def test_header_override_is_case_insensitive() -> None:
    m = mount("files", path="/files", headers={"x-frame-options": "SAMEORIGIN"})
    text = gateway.render([m], {"files": 9206}, cfg())["sites/files.caddy"]
    assert header_values(text, "X-Frame-Options") == ["SAMEORIGIN"]
    assert text.count("frame-options") + text.count("Frame-Options") == 1


def test_header_override_appends_unknown_names_sorted() -> None:
    m = mount("files", path="/files", headers={"Permissions-Policy": "geolocation=()", "A": "b"})
    text = gateway.render([m], {"files": 9206}, cfg())["sites/files.caddy"]
    lines = [line.strip() for line in text.splitlines() if line.strip().startswith("header ")]
    assert lines[-2:] == ['header A "b"', 'header Permissions-Policy "geolocation=()"']
    assert lines[0] == 'header X-Frame-Options "DENY"'  # template headers stay first


def test_header_override_none_drops_the_header() -> None:
    m = mount("files", path="/files", headers={"Referrer-Policy": None})
    text = gateway.render([m], {"files": 9206}, cfg())["sites/files.caddy"]
    assert header_values(text, "Referrer-Policy") == []


def test_header_value_with_a_quote_is_rejected_not_mangled() -> None:
    m = mount("files", path="/files", headers={"X-Bad": 'a"b'})
    with pytest.raises(GatewayError, match="cannot be written"):
        gateway.render([m], {"files": 9206}, cfg())


def test_headers_must_be_an_object() -> None:
    m = mount("files", path="/files", headers=["X-Frame-Options"])
    with pytest.raises(GatewayError, match="must be an object"):
        gateway.render([m], {"files": 9206}, cfg())


# --------------------------------------------------------------------------- #
# Rejections: a mis-shaped mount must fail loudly, never render "something".
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "over, match",
    [
        ({"version": 2}, "unsupported sidecar version"),
        ({"version": None}, "unsupported sidecar version"),
        ({"kind": "webapp"}, "unknown kind"),
        ({"path": "/files", "subdomain": "files"}, "exactly one of path/subdomain"),
        ({}, "exactly one of path/subdomain"),
        ({"path": "files"}, "must start with"),
        ({"path": "/files/"}, "must start with"),
        ({"path": "/files*"}, "illegal characters"),
        ({"path": "/files", "gateway": "api.lishuyu.app {"}, "illegal characters"),
        ({"path": "/files", "gateway": ""}, "gateway must be a non-empty string"),
        ({"subdomain": "f", "gateway": "f x.app"}, "illegal characters"),
    ],
)
def test_bad_mount_is_rejected(over: dict[str, object], match: str) -> None:
    with pytest.raises(GatewayError, match=match):
        gateway.render([mount("files", **over)], {"files": 9206}, cfg())


def test_service_mount_without_a_live_port_is_rejected() -> None:
    with pytest.raises(GatewayError, match="no live port allocated"):
        gateway.render([mount("files", path="/files")], {}, cfg())


def test_duplicate_mount_id_is_rejected() -> None:
    mounts = [mount("files", path="/files"), mount("files", path="/files2")]
    with pytest.raises(GatewayError, match="duplicate mount id"):
        gateway.render(mounts, {"files": 9206}, cfg())


def test_static_root_must_be_one_segment() -> None:
    m = mount("x", kind="static", path="/x", static_root="../../etc")
    with pytest.raises(GatewayError, match="single path segment"):
        gateway.render([m], {}, cfg())


# --------------------------------------------------------------------------- #
# resolve_ports
# --------------------------------------------------------------------------- #


class FakeAllocator:
    def __init__(self, table: dict[str, dict[str, int]]) -> None:
        self.table = table

    def get(self, service_id: str) -> dict[str, int]:
        return self.table.get(service_id, {})


def test_resolve_ports_follows_port_name_and_skips_static() -> None:
    mounts = [
        mount("files", path="/files"),
        mount("weird", path="/weird", port_name="http"),
        mount("files-web", kind="static", subdomain="file", gateway="f.x", port_name=None),
    ]
    alloc = FakeAllocator({"files": {"main": 30001}, "weird": {"http": 30002, "main": 30003}})
    assert gateway.resolve_ports(mounts, alloc) == {"files": 30001, "weird": 30002}


def test_resolve_ports_raises_when_the_named_port_is_missing() -> None:
    alloc = FakeAllocator({"files": {"metrics": 30001}})
    with pytest.raises(GatewayError, match="no allocated port named 'main'"):
        gateway.resolve_ports([mount("files", path="/files")], alloc)


# --------------------------------------------------------------------------- #
# write()
# --------------------------------------------------------------------------- #


def modes(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_write_creates_world_readable_files_in_traversable_dirs(tmp_path: Path) -> None:
    """Caddy runs as a mapped uid for which every harness file is nobody-owned
    (D4); it can still read them iff the modes say so."""
    state = StateDir(tmp_path)
    changed = gateway.write(state, rendered("path"))
    root = tmp_path / "gateway"
    assert modes(root) == 0o755
    assert modes(root / "sites") == 0o755
    for path in changed:
        assert modes(path) == 0o644
    assert sorted(p.name for p in changed) == ["Caddyfile", "files.caddy", "timeservice.caddy"]
    assert (root / "Caddyfile").read_text() == rendered("path")["Caddyfile"]


def test_write_leaves_no_temp_files_behind(tmp_path: Path) -> None:
    state = StateDir(tmp_path)
    gateway.write(state, rendered("path"))
    leftovers = [p.name for p in (tmp_path / "gateway").rglob(".*")]
    assert leftovers == []


def test_unchanged_content_is_not_rewritten(tmp_path: Path) -> None:
    """An empty return value is the caller's signal to skip the Caddy restart."""
    state = StateDir(tmp_path)
    files = rendered("path")
    gateway.write(state, files)
    before = (tmp_path / "gateway" / "Caddyfile").stat().st_mtime_ns
    assert gateway.write(state, files) == []
    assert (tmp_path / "gateway" / "Caddyfile").stat().st_mtime_ns == before


def test_a_wrong_mode_is_repaired_even_when_content_matches(tmp_path: Path) -> None:
    state = StateDir(tmp_path)
    files = rendered("path")
    gateway.write(state, files)
    path = tmp_path / "gateway" / "sites" / "files.caddy"
    os.chmod(path, 0o600)  # unreadable by the Caddy uid
    assert gateway.write(state, files) == [path]
    assert modes(path) == 0o644


def test_changed_content_is_rewritten(tmp_path: Path) -> None:
    state = StateDir(tmp_path)
    gateway.write(state, rendered("path"))
    changed = gateway.write(state, rendered("logdir"))
    assert [p.name for p in changed] == ["Caddyfile"]


def test_stale_site_files_are_removed_and_reported(tmp_path: Path) -> None:
    """A removed mount must reach Caddy too, so the removal counts as a change."""
    state = StateDir(tmp_path)
    gateway.write(state, rendered("path"))
    stale = tmp_path / "gateway" / "sites" / "gone.caddy"
    stale.write_text("# left over from a deleted service\n")
    smaller = gateway.render([PATH_MOUNTS[0]], PATH_PORTS, cfg())
    changed = gateway.write(state, smaller)
    assert stale in changed
    assert not stale.exists()
    assert not (tmp_path / "gateway" / "sites" / "timeservice.caddy").exists()
    assert (tmp_path / "gateway" / "sites" / "files.caddy").exists()


def test_write_ignores_unrelated_files_in_the_sites_dir(tmp_path: Path) -> None:
    state = StateDir(tmp_path)
    gateway.write(state, rendered("path"))
    note = tmp_path / "gateway" / "sites" / "README.md"
    note.write_text("not a snippet\n")
    assert gateway.write(state, rendered("path")) == []
    assert note.exists()


# --------------------------------------------------------------------------- #
# The Caddy service declaration
# --------------------------------------------------------------------------- #


def test_caddy_declaration_is_a_valid_ams_declaration(tmp_path: Path) -> None:
    state = StateDir(tmp_path / "state")
    decl = schema.loads(gateway.caddy_declaration(state, tmp_path / "store"))
    assert decl.id == "caddy"
    assert decl.start.argv[0] == str(tmp_path / "store" / "bin" / "caddy")
    assert decl.start.argv[1] == "run"
    assert "--adapter" in decl.start.argv and "caddyfile" in decl.start.argv
    assert str(gateway.caddyfile_path(state)) in decl.start.argv


def test_caddy_declaration_asks_the_harness_to_allocate_its_port(tmp_path: Path) -> None:
    """The chicken-and-egg: the declaration cannot name a port, because the
    Caddyfile's listen address IS the port ams hands out for this service."""
    decl = schema.loads(gateway.caddy_declaration(StateDir(tmp_path), tmp_path))
    assert dict(decl.ports) == {"main": 0}
    assert not any("${PORT_" in a for a in decl.start.argv)


def test_caddy_declaration_health_probes_the_entry_site(tmp_path: Path) -> None:
    decl = schema.loads(gateway.caddy_declaration(StateDir(tmp_path), tmp_path))
    assert decl.health.kind == "http"
    assert decl.health.port == "main"
    assert decl.health.path == gateway.HEALTH_PATH
    entry = gateway.render([], {}, cfg())["Caddyfile"]
    assert f"handle {decl.health.path} {{" in entry


def test_caddy_declaration_is_capped(tmp_path: Path) -> None:
    decl = schema.loads(gateway.caddy_declaration(StateDir(tmp_path), tmp_path))
    assert decl.limits.memory_max == "120M"
    assert decl.limits.pids_max == 64


def test_caddy_declaration_honours_a_custom_port_name(tmp_path: Path) -> None:
    decl = schema.loads(gateway.caddy_declaration(StateDir(tmp_path), tmp_path, port_name="edge"))
    assert dict(decl.ports) == {"edge": 0}
    assert decl.health.port == "edge"


def test_shipped_example_matches_the_generator() -> None:
    """`examples/platform/caddy/service.toml` is documentation, so it must not
    drift from what `caddy_declaration` actually emits for the racknerd paths."""
    example = Path(__file__).parents[1] / "examples" / "platform" / "caddy" / "service.toml"
    state = StateDir(Path("/home/harness/store/state"))
    assert example.read_text(encoding="utf-8") == gateway.caddy_declaration(
        state, Path("/home/harness/store")
    )


def test_caddy_declaration_tells_the_harness_its_logs_are_json(tmp_path: Path) -> None:
    """Caddy is not an SDK service, so the severity heuristic would have to guess
    at a JSON line. The `[logging]` hint (T1.3) makes it read the real level."""
    decl = schema.loads(gateway.caddy_declaration(StateDir(tmp_path), tmp_path))
    assert decl.logging.format == "json"
    assert "format json" in gateway.render([], {}, cfg())["Caddyfile"]
