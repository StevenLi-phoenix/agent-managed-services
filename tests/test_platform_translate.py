"""Golden-file tests for the manifest translator and its YAML subset parser.

The 21 api manifests are committed under ``tests/golden/platform/manifests/``
because ``api/`` is a gitignored read-only clone: without the copies the suite
could not run on the remote Linux host. ``test_fixture_manifests_match_api_clone``
is the drift guard -- it runs only where the clone exists.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from ams import cli
from ams.platform.translate import (
    TranslateContext,
    TranslateError,
    Translation,
    emit_toml,
    translate,
)
from ams.platform.yamlsubset import YamlSubsetError
from ams.platform.yamlsubset import parse as yparse
from ams.schema import loads

GOLDEN = pathlib.Path(__file__).parent / "golden" / "platform"
MANIFESTS = GOLDEN / "manifests"
API_CLONE = pathlib.Path(__file__).resolve().parents[1] / "api"

# Frozen so the goldens are reproducible: a real run passes the fetched sha and
# the live state dir.
SHA = "0123456789abcdef0123456789abcdef01234567"
SERVICES_DIR = pathlib.Path("/home/harness/state/services")

ALL_IDS = sorted(p.stem for p in MANIFESTS.glob("*.yaml"))
STATIC_IDS = ["files-web", "llm-web"]
SERVICE_IDS = [i for i in ALL_IDS if i not in STATIC_IDS]


def ctx(**over: object) -> TranslateContext:
    kwargs: dict = {
        "sha": SHA,
        "services_dir": SERVICES_DIR,
        "registry_url": "http://127.0.0.1:19100",
        "auth_url": "http://127.0.0.1:19101",
    }
    kwargs.update(over)
    return TranslateContext(**kwargs)  # type: ignore[arg-type]


def manifest(service_id: str) -> str:
    return (MANIFESTS / f"{service_id}.yaml").read_text(encoding="utf-8")


def tr(service_id: str) -> Translation:
    return translate(manifest(service_id), ctx())


# --------------------------------------------------------------------- fixtures


def test_the_fleet_is_all_21_manifests():
    assert len(ALL_IDS) == 21
    assert len(SERVICE_IDS) == 19


@pytest.mark.skipif(not API_CLONE.is_dir(), reason="api/ clone not present")
def test_fixture_manifests_match_api_clone():
    """The committed copies must stay byte-identical to the manifests they mirror."""
    live = {p.parent.name: p for p in API_CLONE.glob("services/*/service.yaml")}
    live |= {p.parent.name: p for p in API_CLONE.glob("apps/*/service.yaml")}
    assert sorted(live) == ALL_IDS
    for service_id, path in sorted(live.items()):
        assert path.read_bytes() == (MANIFESTS / f"{service_id}.yaml").read_bytes(), service_id


@pytest.mark.skipif(not API_CLONE.is_dir(), reason="api/ clone not present")
def test_disabled_manifests_are_reported_not_translated():
    """`.disabled` files are skipped by name; nothing tries to parse them."""
    disabled = sorted(p.name for p in API_CLONE.rglob("*.disabled"))
    assert disabled == ["admin-frontend.caddy.disabled", "service.yaml.disabled"]
    assert not any(name.endswith(".yaml") for name in disabled)


# --------------------------------------------------------------- YAML subset


@pytest.mark.parametrize("service_id", ALL_IDS)
def test_yaml_subset_matches_pyyaml(service_id: str):
    """PyYAML is a DEV-ONLY oracle; ams never imports it."""
    yaml = pytest.importorskip("yaml", reason="PyYAML is a dev-only test oracle")
    text = manifest(service_id)
    assert yparse(text) == yaml.safe_load(text)


@pytest.mark.parametrize(
    "text",
    [
        'a: "line one\n   line two"\nb: 2\n',
        "acl:\n  - action: read\n    principal: anon\n  - action: write\n    principal: admin\n",
        "top:\n- a\n- b\n",
        "m: {a: [1, 2], b: {c: d}}\n",
        "n: null\nx: ~\ny:\nz: true\nw: false\nf: 1.5\ni: -3\n",
        "s: 'it''s ok'\nd: \"tab\\there\"\n",
        "c: value # comment\nh: not#comment\n",
        "e: []\nf: {}\n",
        "l:\n  -   action: read\n      principal: anon\n",
        "u: https://example.com/a:b\n",
    ],
)
def test_yaml_subset_matches_pyyaml_on_synthetic_cases(text: str):
    yaml = pytest.importorskip("yaml", reason="PyYAML is a dev-only test oracle")
    assert yparse(text) == yaml.safe_load(text)


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ("a: &x 1\nb: 1\n", "anchor"),
        ("a: 1\nb: *x\n", "alias"),
        ("a: !!str 1\n", "tag"),
        ("---\na: 1\n", "multi-document"),
        ("a: |\n  block\n", "literal block scalar"),
        ("a: >\n  folded\n", "folded block scalar"),
        ("base: &b 1\nd:\n  <<: x\n", "anchor"),
        ("d:\n  <<: x\n", "merge key"),
        ("a: 1\na: 2\n", "duplicate mapping key"),
        ("restart: no\n", "YAML 1.1 boolean"),
        ("a:\n\tb: 1\n", "tab in indentation"),
        ("a: 0755\n", "ambiguous numeric"),
        ("a: 'unterminated\n", "unterminated"),
        ("a: {b: 1\n", "flow mapping"),
        ("a: [1, 2\n", "flow sequence"),
    ],
)
def test_yaml_subset_rejects_unsupported_constructs(text: str, needle: str):
    with pytest.raises(YamlSubsetError) as exc:
        yparse(text)
    assert needle in str(exc.value)
    assert str(exc.value).startswith("line ")


# ------------------------------------------------------------------- goldens


@pytest.mark.parametrize("service_id", SERVICE_IDS)
def test_declaration_matches_golden(service_id: str):
    t = tr(service_id)
    assert t.kind == "service"
    assert t.decl is not None
    assert emit_toml(t.decl) == (GOLDEN / f"{service_id}.toml").read_text(encoding="utf-8")


@pytest.mark.parametrize("service_id", ALL_IDS)
def test_mount_sidecar_matches_golden(service_id: str):
    t = tr(service_id)
    expected = json.loads((GOLDEN / f"{service_id}.mount.json").read_text(encoding="utf-8"))
    assert dict(t.mount) == expected
    assert expected["version"] == 1


@pytest.mark.parametrize("service_id", SERVICE_IDS)
def test_registry_sidecar_matches_golden(service_id: str):
    t = tr(service_id)
    expected = json.loads((GOLDEN / f"{service_id}.registry.json").read_text(encoding="utf-8"))
    assert t.registry is not None
    assert dict(t.registry) == expected


@pytest.mark.parametrize("service_id", SERVICE_IDS)
def test_every_service_waits_for_the_registry(service_id: str):
    """Layer-1 services register during FastAPI startup; a refused connection
    there is a fatal "Application startup failed", not a retry (racknerd,
    2026-09-02). The gate is the harness's answer, so it is not optional."""
    decl = tr(service_id).decl
    assert decl is not None
    assert decl.depends_on == ("registry",)
    assert 'depends_on = ["registry"]' in emit_toml(decl)


def test_the_registry_dependency_is_in_every_service_golden():
    """One assertion over the files themselves: a regenerated golden that lost
    the line would otherwise only fail for the one service that changed."""
    for service_id in SERVICE_IDS:
        text = (GOLDEN / f"{service_id}.toml").read_text(encoding="utf-8")
        assert 'depends_on = ["registry"]' in text, service_id


@pytest.mark.parametrize("service_id", SERVICE_IDS)
def test_emit_toml_round_trips(service_id: str):
    decl = tr(service_id).decl
    assert decl is not None
    assert loads(emit_toml(decl)) == decl


def test_ams_validate_accepts_every_emitted_declaration(tmp_path):
    paths = []
    for service_id in SERVICE_IDS:
        decl = tr(service_id).decl
        assert decl is not None
        p = tmp_path / f"{service_id}.toml"
        p.write_text(emit_toml(decl), encoding="utf-8")
        paths.append(str(p))
    assert cli.main(["validate", *paths]) == 0


@pytest.mark.parametrize("service_id", STATIC_IDS)
def test_static_manifests_produce_no_service(service_id: str):
    t = tr(service_id)
    assert t.kind == "static"
    assert t.decl is None
    assert t.registry is None
    assert t.mount["kind"] == "static"
    assert t.mount["port_name"] is None
    assert t.mount["static_root"] == service_id
    assert t.mount["build"]  # deploy.install carried verbatim for T3.4


# ------------------------------------------------------- individual mappings


def test_exec_becomes_argv_with_a_bare_entry_point():
    decl = tr("files").decl
    assert decl is not None
    assert decl.start.argv[0] == "uvicorn"
    assert "${PORT_main}" in decl.start.argv
    assert decl.start.workdir == "repo/apps/files"


def test_var_lib_paths_are_rewritten_to_the_service_data_dir():
    decl = tr("kvservice").decl
    assert decl is not None
    root = f"{SERVICES_DIR.as_posix()}/kvservice/root"
    assert decl.env["KV_DB_PATH"] == f"{root}/data/kvservice.db"
    assert decl.env["SVC_M2M_PUBLIC_KEY_PATH"] == f"{root}/etc/jwt-rs256.pub"


def test_injected_env_is_complete_and_urls_come_from_the_context():
    decl = tr("commentservice").decl
    assert decl is not None
    env = decl.env
    assert env["SVC_NAME"] == "commentservice"
    assert env["SVC_AUDIENCE"] == "commentservice"
    assert env["PORT"] == "${PORT_main}"
    assert env["GIT_COMMIT"] == SHA
    assert env["SVC_CAPABILITIES"] == "comments"
    assert env["SVC_OWNER"] == "lishuyu"
    assert env["SVC_HEALTH_PATH"] == "/health"
    assert env["REGISTRY_URL"] == "http://127.0.0.1:19100"
    # The manifest pins production's loopback auth port (8001); the replica's wins.
    assert env["AUTH_URL"] == "http://127.0.0.1:19101"
    assert decl.secrets == ("SVC_SECRET",)


def test_display_name_becomes_the_declaration_name_and_svc_display_name():
    decl = tr("files").decl
    assert decl is not None
    assert decl.name == "Files Service"
    assert decl.env["SVC_DISPLAY_NAME"] == "Files Service"
    bare = tr("kvservice").decl
    assert bare is not None
    assert bare.name == ""
    assert "SVC_DISPLAY_NAME" not in bare.env


def test_extra_secret_names_are_appended():
    t = translate(manifest("turingtest"), ctx(extra_secret_names=("BOT_LLM_API_KEY",)))
    assert t.decl is not None
    assert t.decl.secrets == ("SVC_SECRET", "BOT_LLM_API_KEY")


def test_memory_max_honours_the_floor_and_the_default():
    # kvservice asks for 100M, below the 120M floor (Q8).
    low = tr("kvservice").decl
    assert low is not None
    assert low.limits.memory_max == "120M"
    # files asks for 200M and keeps it.
    keep = tr("files").decl
    assert keep is not None
    assert keep.limits.memory_max == "200M"
    # llmgateway declares none and gets the default.
    default = tr("llmgateway").decl
    assert default is not None
    assert default.limits.memory_max == "150M"
    assert default.limits.cpu_max == "40%"
    assert default.limits.pids_max == 64


def test_health_and_stop_and_restart_mapping():
    decl = tr("resume").decl
    assert decl is not None
    assert (decl.health.kind, decl.health.port, decl.health.path) == ("http", "main", "/health")
    assert decl.health.start_period_s == 120.0
    assert (decl.stop.signal, decl.stop.timeout_s) == ("SIGTERM", 10.0)
    assert decl.restart.policy == "on-failure"
    assert decl.restart.backoff_s == 5.0
    assert decl.runtime.kind == "uv" and decl.runtime.sync and decl.runtime.python == "3.12"


@pytest.mark.parametrize(
    ("yaml_restart", "policy"),
    [("no", "never"), ("on-failure", "on-failure"), ("always", "always")],
)
def test_restart_policy_map(yaml_restart: str, policy: str):
    text = manifest("resume").replace("restart: on-failure", f'restart: "{yaml_restart}"')
    t = translate(text, ctx())
    assert t.decl is not None
    assert t.decl.restart.policy == policy


def test_manual_restart_is_a_flag_not_a_restart_policy():
    text = manifest("resume").replace("owner: lishuyu", "owner: lishuyu\nmanual_restart: true")
    t = translate(text, ctx())
    assert t.flags["manual_restart"] is True
    assert t.decl is not None
    assert t.decl.restart.policy == "on-failure"
    assert tr("resume").flags["manual_restart"] is False


def test_acl_and_registry_sidecar_shape():
    t = tr("mailbox")
    assert t.registry is not None
    assert t.registry["acl"] == [
        {"action": "read", "principal": "admin", "effect": "allow"},
        {"action": "write", "principal": "admin", "effect": "allow"},
    ]
    assert t.registry["capabilities"] == ["mailbox", "email", "inbox"]
    assert t.mount == {
        "version": 1,
        "id": "mailbox",
        "kind": "service",
        "gateway": "mail.lishuyu.app",
        "path": None,
        "subdomain": "mail",
        "port_name": "main",
        "static_root": None,
        "build": [],
        "headers": {},
    }


def test_acl_effect_defaults_to_allow():
    text = manifest("resume").replace(
        "  - {action: read,  principal: anon,    effect: allow}",
        "  - {action: read,  principal: anon}",
    )
    t = translate(text, ctx())
    assert t.registry is not None
    assert t.registry["acl"][0] == {"action": "read", "principal": "anon", "effect": "allow"}


# ------------------------------------------------------------- the loopback gate


@pytest.mark.parametrize(
    "url",
    [
        "https://registry.lishuyu.app",
        "http://registry.lishuyu.app",
        "http://localhost:19100",
        "http://127.0.0.1",
        "http://127.0.0.1:19100/",
        "http://10.0.0.1:19100",
    ],
)
def test_non_loopback_registry_url_is_refused(url: str):
    with pytest.raises(TranslateError) as exc:
        ctx(registry_url=url)
    assert "ctx.registry_url" in str(exc.value)
    assert "loopback" in str(exc.value)


def test_non_loopback_auth_url_is_refused():
    with pytest.raises(TranslateError) as exc:
        ctx(auth_url="https://auth.lishuyu.app")
    assert "ctx.auth_url" in str(exc.value)


def test_context_rejects_a_non_sha():
    with pytest.raises(TranslateError) as exc:
        ctx(sha="main")
    assert "ctx.sha" in str(exc.value)


# ------------------------------------------------------------- rejection table


def _edit(service_id: str, old: str, new: str) -> str:
    text = manifest(service_id)
    assert old in text, old
    return text.replace(old, new, 1)


REJECTIONS: list[tuple[str, str, str, str, str]] = [
    # (label, base manifest, old text, new text, expected field path in the message)
    ("unknown top-level key", "resume", "name: resume", "name: resume\nfoo: bar", "unknown keys"),
    (
        "unknown process key",
        "resume",
        "  restart: on-failure",
        "  restart: on-failure\n  nice: 5",
        "process",
    ),
    (
        "unknown deploy key",
        "resume",
        "  target_dir: /srv/resume",
        "  target_dir: /srv/resume\n  hooks: []",
        "deploy",
    ),
    (
        "unknown mount key",
        "resume",
        "  path: /resume",
        "  path: /resume\n  tls: true",
        "mount",
    ),
    (
        "unknown acl key",
        "resume",
        "  - {action: read,  principal: anon,    effect: allow}",
        "  - {action: read,  principal: anon,    effect: allow, ttl: 5}",
        "acl[0]",
    ),
    (
        "unknown registry key",
        "resume",
        "  capabilities: [resume]",
        "  capabilities: [resume]\n  weight: 1",
        "registry",
    ),
    ("wrong schema_version", "resume", "schema_version: 1", "schema_version: 2", "schema_version"),
    ("bad kind", "resume", "schema_version: 1", "schema_version: 1\nkind: cronjob", "kind"),
    (
        "install command we do not recognise",
        "resume",
        "    - cd apps/resume && /usr/local/bin/uv sync",
        "    - cd apps/resume && pip install -r requirements.txt",
        "deploy.install[0]",
    ),
    (
        "two install commands",
        "resume",
        "    - cd apps/resume && /usr/local/bin/uv sync",
        "    - cd apps/resume && /usr/local/bin/uv sync\n    - echo done",
        "deploy.install",
    ),
    (
        "install dir disagrees with working_dir",
        "resume",
        "    - cd apps/resume && /usr/local/bin/uv sync",
        "    - cd apps/other && /usr/local/bin/uv sync",
        "deploy.install[0]",
    ),
    (
        "exec entry point outside a venv",
        "resume",
        "  exec: /srv/resume/apps/resume/.venv/bin/uvicorn",
        "  exec: /srv/resume/apps/resume/run.sh",
        "process.exec",
    ),
    (
        "unknown substitution in exec",
        "resume",
        "--port ${PORT}",
        "--port ${PORT} --root ${SVC_ROOT}",
        "process.exec",
    ),
    (
        "working_dir outside /srv/<name>",
        "resume",
        "  working_dir: /srv/resume/apps/resume",
        "  working_dir: /srv/elsewhere/apps/resume",
        "process.working_dir",
    ),
    (
        "target_dir outside /srv/<name>",
        "resume",
        "  target_dir: /srv/resume",
        "  target_dir: /srv/resume-old",
        "deploy.target_dir",
    ),
    (
        "another service's state dir",
        "kvservice",
        "    KV_DB_PATH: /var/lib/kvservice/kvservice.db",
        "    KV_DB_PATH: /var/lib/logservice/kvservice.db",
        "process.environment.KV_DB_PATH",
    ),
    (
        "env name reserved by the harness",
        "resume",
        "    SVC_ROOT_PATH: /resume",
        '    SVC_ROOT_PATH: /resume\n    PORT_MAIN: "1"',
        "process.environment.PORT_MAIN",
    ),
    (
        "env the harness injects",
        "resume",
        "    SVC_ROOT_PATH: /resume",
        "    SVC_ROOT_PATH: /resume\n    SVC_NAME: other",
        "process.environment.SVC_NAME",
    ),
    (
        "boolean env value",
        "resume",
        "    SVC_ROOT_PATH: /resume",
        "    SVC_ROOT_PATH: /resume\n    DEBUG: true",
        "process.environment.DEBUG",
    ),
    (
        "path and subdomain together",
        "resume",
        "  path: /resume",
        "  path: /resume\n  subdomain: resume",
        "mount",
    ),
    (
        "subdomain that does not match the gateway",
        "mailbox",
        "  subdomain: mail",
        "  subdomain: post",
        "mount.subdomain",
    ),
    ("no mount port", "resume", "  port: 9201", "  gateway_note: x", "mount"),
    (
        "sub-tree staging for a service",
        "resume",
        "    branch: main",
        "    branch: main\n    path: apps/resume",
        "deploy.source.path",
    ),
    (
        "non-git source",
        "resume",
        "    type: git",
        "    type: rsync",
        "deploy.source.type",
    ),
    ("id ams cannot hold", "resume", "name: resume", "name: Resume", "name"),
    (
        "acl principal outside the registry regex",
        "resume",
        "principal: anon,    effect: allow}",
        "principal: robot,    effect: allow}",
        "acl[0].principal",
    ),
    (
        "static manifest with a process",
        "llm-web",
        "mount:",
        "process:\n  exec: /x/.venv/bin/uvicorn\n  working_dir: /srv/llm-web\n\nmount:",
        "process",
    ),
]


@pytest.mark.parametrize(
    ("label", "base", "old", "new", "path"),
    REJECTIONS,
    ids=[r[0].replace(" ", "-") for r in REJECTIONS],
)
def test_unsupported_constructs_raise_naming_the_field(
    label: str, base: str, old: str, new: str, path: str
):
    with pytest.raises(TranslateError) as exc:
        translate(_edit(base, old, new), ctx())
    assert path in str(exc.value), f"{label}: {exc.value}"


def test_a_manifest_that_is_not_a_mapping_is_refused():
    with pytest.raises(TranslateError):
        translate("- a\n- b\n", ctx())


def test_layer_zero_and_the_gateway_do_not_depend_on_the_registry(tmp_path):
    """The gate must not be able to deadlock the fleet it protects.

    ``registry`` is the root, ``auth`` starts fine without it (its only registry
    use is the M2M email client, which builds an httpx client and makes no call
    at startup -- ``components/auth/src/auth/email_client.py``), and Caddy is
    declared by the gateway module and proxies whatever is up. A dependency on
    ``registry`` in any of the three would either be a cycle or would keep the
    gateway down for the whole of Layer 0's start.
    """
    from ams.platform import bootstrap, gateway
    from ams.state import StateDir

    state = StateDir(tmp_path / "state")
    for service_id, decl in bootstrap.layer0_declarations(state).items():
        assert decl.depends_on == (), service_id
    caddy = loads(gateway.caddy_declaration(state, tmp_path / "store"))
    assert caddy.depends_on == ()
