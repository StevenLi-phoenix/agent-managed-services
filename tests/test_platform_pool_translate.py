"""Golden-file tests for the pool translation (PLAN-pool T3).

A pool is N manifests translated into **one** ``ServiceDecl`` plus a
``pool.json`` the runner reads. The properties that matter and are asserted
here:

* the emitted ``service.toml`` matches ``golden/platform/pool/pool-core.toml``
  byte for byte, and round trips through ``ams.schema.loads``;
* a pooled member's *registry* sidecar is byte-identical to its standalone one
  (Alternative B's strongest property: the registry never learns about pools);
* a pooled member's *mount* sidecar differs from its standalone one by exactly
  the two additive keys ``port_owner``/``port_name``, and a standalone member's
  sidecar grows no new keys at all;
* every cross-manifest rule of PLAN-pool §3.2 raises with its stated message;
* ``pool.json`` carries secret *names*, never secret values.
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
import re

import pytest

from ams.platform.translate import (
    IDENTITY_ENV,
    POOL_ADMIN_PORT_NAME,
    POOL_ID_PREFIX,
    POOL_UVICORN_PIN,
    PoolMember,
    PoolTranslation,
    TranslateContext,
    TranslateError,
    Translation,
    build_pool,
    emit_toml,
    mangle_member,
    pool_member,
    pool_port_name,
    translate,
)
from ams.schema import PORT_NAME_RE, loads

GOLDEN = pathlib.Path(__file__).parent / "golden" / "platform"
POOL_GOLDEN = GOLDEN / "pool"
MANIFESTS = GOLDEN / "manifests"

# Same frozen constants as tests/test_platform_translate.py, so the pooled
# sidecars can be diffed against the existing standalone goldens.
SHA = "0123456789abcdef0123456789abcdef01234567"
SERVICES_DIR = pathlib.Path("/home/harness/state/services")

MEMBER_DIRS = {"kvservice": "services/kvservice", "timeservice": "apps/timeservice"}


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


def member(service_id: str, **over: object) -> PoolMember:
    """Translate one manifest as a member of pool ``core`` and extract it."""
    over.setdefault("pool", "core")
    t = translate(manifest(service_id), ctx(**over))
    return pool_member(t, MEMBER_DIRS[service_id])


def core(**over: object) -> PoolTranslation:
    members = [member("kvservice", **over), member("timeservice", **over)]
    return build_pool("core", members, ctx(pool="core", **over))


# --------------------------------------------------------------------- mangling


def test_mangle_member_upcases_and_replaces_hyphens():
    assert mangle_member("kvservice") == "KVSERVICE"
    assert mangle_member("notification-svc") == "NOTIFICATION_SVC"


def test_pool_port_name_is_the_id_verbatim_when_it_is_a_legal_port_name():
    assert pool_port_name("kvservice") == "kvservice"
    assert pool_port_name("notificationservice") == "notificationservice"
    assert PORT_NAME_RE.match(pool_port_name("notificationservice"))


def test_pool_port_name_lowercase_mangles_a_hyphenated_id():
    assert pool_port_name("notification-svc") == "notification_svc"
    assert PORT_NAME_RE.match(pool_port_name("notification-svc"))


# --------------------------------------------------------------------- context


def test_translate_context_pool_defaults():
    c = ctx()
    assert c.pool is None
    assert c.pool_memory_base == "150M"
    assert c.pool_memory_per_member == "30M"
    assert c.pool_pids_base == 48
    assert c.pool_pids_per_member == 16
    assert c.pool_cpu_max == "100%"
    assert c.pool_start_period_s == 240.0
    assert c.pool_runner_name == "pool_runner.py"
    assert c.pool_uvicorn_pin == POOL_UVICORN_PIN


def test_translate_context_pool_id():
    assert ctx(pool="core").pool_id == "pool-core"
    assert ctx().pool_id is None


def test_translate_context_rejects_a_bad_pool_name():
    with pytest.raises(TranslateError, match="ctx.pool"):
        ctx(pool="Core")


# --------------------------------------------------------------------- extraction


def test_pool_member_parses_a_factory_app():
    m = member("kvservice")
    assert m.id == "kvservice"
    assert m.app == "kvservice.main:build_app"
    assert m.factory is True
    assert m.runtime_rel == "services/kvservice"
    assert m.secret_names == ("SVC_SECRET",)


def test_a_module_level_app_member_yields_factory_false():
    t = translate(manifest("resume"), ctx(pool="core"))
    m = pool_member(t, "apps/resume")
    assert m.app == "resume.main:app"
    assert m.factory is False


def test_pool_member_splits_identity_from_shared_env():
    m = member("kvservice")
    assert m.identity_env == {
        "SVC_NAME": "kvservice",
        "SVC_AUDIENCE": "kvservice",
        "SVC_CAPABILITIES": "keyvalue",
        "SVC_HEALTH_PATH": "/health",
        "SVC_OWNER": "steven",
        "SVC_ROOT_PATH": "/kv",
    }
    assert set(m.identity_env) <= IDENTITY_ENV
    assert not set(m.shared_env) & IDENTITY_ENV
    # PORT is an identity key but the runner sets it from ``port_env``.
    assert "PORT" not in m.identity_env and "PORT" not in m.shared_env


def test_pool_member_shared_env_carries_the_mandatory_urls():
    # T0: SVC_AUDIENCE is a KeyError in load_from_env when missing, and an
    # unset REGISTRY_URL/AUTH_URL points a member at the wrong registry.
    for service_id in MEMBER_DIRS:
        m = member(service_id)
        assert m.identity_env["SVC_AUDIENCE"] == service_id
        assert m.shared_env["REGISTRY_URL"] == "http://127.0.0.1:19100"
        assert m.shared_env["AUTH_URL"] == "http://127.0.0.1:19101"


def test_pool_member_rejects_a_manifest_dir_that_is_not_the_uv_project_dir():
    t = translate(manifest("kvservice"), ctx(pool="core"))
    with pytest.raises(TranslateError, match="does not match the uv project directory"):
        pool_member(t, "apps/kvservice")


def test_pool_member_rejects_a_static_translation():
    t = translate((MANIFESTS / "files-web.yaml").read_text(encoding="utf-8"), ctx())
    with pytest.raises(TranslateError, match="only kind: service can join a pool"):
        pool_member(t, "web/files")


def test_pool_is_not_valid_for_kind_static():
    text = (MANIFESTS / "files-web.yaml").read_text(encoding="utf-8")
    with pytest.raises(TranslateError, match="pool is not valid for kind: static"):
        translate(text, ctx(pool="core"))


# --------------------------------------------------------------------- paths


def test_a_pooled_member_lives_under_the_pool_root():
    m = member("kvservice")
    root = "/home/harness/state/services/pool-core/root"
    assert m.shared_env["SVC_M2M_PUBLIC_KEY_PATH"] == f"{root}/etc/jwt-rs256.pub"


def test_data_paths_are_rewritten_under_data_member():
    m = member("kvservice")
    root = "/home/harness/state/services/pool-core/root"
    assert m.shared_env["KV_DB_PATH"] == f"{root}/data/kvservice/kvservice.db"


def test_standalone_data_paths_are_unchanged():
    t = translate(manifest("kvservice"), ctx())
    assert t.decl is not None
    assert (
        t.decl.env["KV_DB_PATH"]
        == "/home/harness/state/services/kvservice/root/data/kvservice.db"
    )


def test_a_pooled_member_still_refuses_another_services_state_dir():
    text = manifest("kvservice").replace(
        "/var/lib/kvservice/kvservice.db", "/var/lib/logservice/kv.db"
    )
    with pytest.raises(TranslateError, match="points at another service's state dir"):
        translate(text, ctx(pool="core"))


# --------------------------------------------------------------------- sidecars


def test_a_pooled_members_registry_sidecar_is_byte_identical():
    for service_id in MEMBER_DIRS:
        pooled = translate(manifest(service_id), ctx(pool="core"))
        golden = (GOLDEN / f"{service_id}.registry.json").read_bytes()
        emitted = (json.dumps(dict(pooled.registry or {}), indent=2) + "\n").encode()
        assert emitted == golden, service_id


def test_a_pooled_members_mount_sidecar_matches_the_pool_golden():
    for service_id in MEMBER_DIRS:
        pooled = translate(manifest(service_id), ctx(pool="core"))
        golden = (POOL_GOLDEN / f"{service_id}.mount.json").read_bytes()
        emitted = (json.dumps(dict(pooled.mount), indent=2) + "\n").encode()
        assert emitted == golden, service_id


def test_a_pooled_mount_differs_from_the_standalone_one_only_by_port_owner():
    for service_id in MEMBER_DIRS:
        standalone = dict(translate(manifest(service_id), ctx()).mount)
        pooled = dict(translate(manifest(service_id), ctx(pool="core")).mount)
        assert pooled.pop("port_owner") == "pool-core"
        assert pooled["port_name"] == service_id
        standalone.pop("port_name")
        assert {k: v for k, v in pooled.items() if k != "port_name"} == standalone


def test_a_standalone_mount_grows_no_new_keys():
    """The additive keys must be *absent*, not null, or every golden changes."""
    for service_id in MEMBER_DIRS:
        mount = translate(manifest(service_id), ctx()).mount
        assert "port_owner" not in mount
        golden = (GOLDEN / f"{service_id}.mount.json").read_bytes()
        assert (json.dumps(dict(mount), indent=2) + "\n").encode() == golden


# --------------------------------------------------------------------- the decl


def test_pool_toml_matches_the_golden():
    got = emit_toml(core().decl)
    assert got == (POOL_GOLDEN / "pool-core.toml").read_text(encoding="utf-8")


def test_pool_decl_round_trips_through_the_schema():
    decl = core().decl
    assert loads(emit_toml(decl)) == decl


def test_pool_decl_shape():
    p = core()
    assert p.id == "pool-core"
    assert p.decl.id == "pool-core"
    assert p.decl.name == "pool core"
    assert p.decl.depends_on == ("registry",)
    assert p.decl.secrets == ("SVC_SECRET__KVSERVICE", "SVC_SECRET__TIMESERVICE")
    assert p.decl.start.workdir == "repo"
    assert p.decl.start.argv == (
        "python",
        "/home/harness/state/services/pool-core/root/pool_runner.py",
    )
    assert p.decl.restart.policy == "always"
    assert p.decl.restart.backoff_s == 10.0
    assert p.decl.logging.format == "level-prefix"
    assert p.decl.health.kind == "http"
    assert p.decl.health.port == POOL_ADMIN_PORT_NAME
    assert p.decl.health.path == "/_pool/health"
    assert p.decl.health.start_period_s == 240.0
    assert POOL_ID_PREFIX == "pool-"


def test_pool_ports_are_the_admin_port_plus_one_per_member():
    assert dict(core().decl.ports) == {"pool": 0, "kvservice": 0, "timeservice": 0}


def test_pool_packages_pin_uvicorn_first_then_the_editable_members():
    assert core().decl.runtime.packages == (
        POOL_UVICORN_PIN,
        "-e",
        "services/kvservice",
        "-e",
        "apps/timeservice",
    )
    assert core().decl.runtime.sync is False
    assert core().decl.runtime.kind == "uv"
    assert core().decl.runtime.python == "3.12"


def test_pool_limits_scale_with_the_member_count():
    p = core()
    assert p.decl.limits.memory_max == "210M"  # 150M + 2 x 30M
    assert p.decl.limits.pids_max == 80  # 48 + 2 x 16
    assert p.decl.limits.cpu_max == "100%"


def test_pool_limits_honour_the_context_knobs():
    p = build_pool(
        "core",
        [member("kvservice"), member("timeservice")],
        ctx(pool="core", pool_memory_base="1G", pool_memory_per_member="512M"),
    )
    assert p.decl.limits.memory_max == "2G"


def test_pool_env_is_the_pool_keys_plus_the_union_of_non_identity_member_env():
    env = dict(core().decl.env)
    assert env["GIT_COMMIT"] == SHA
    assert env["POOL_ID"] == "core"
    assert env["POOL_PORT_ADMIN"] == "${PORT_pool}"
    assert env["POOL_PORT_KVSERVICE"] == "${PORT_kvservice}"
    assert env["POOL_PORT_TIMESERVICE"] == "${PORT_timeservice}"
    # union of the members' non-identity keys
    assert env["REGISTRY_URL"] == "http://127.0.0.1:19100"
    assert env["AUTH_URL"] == "http://127.0.0.1:19101"
    assert env["KV_DB_PATH"].endswith("/data/kvservice/kvservice.db")


def test_no_identity_key_leaks_into_the_pool_env():
    p = core()
    env = dict(p.decl.env)
    assert not set(env) & IDENTITY_ENV
    for m in p.members:
        for key in m.identity_env:
            assert key in IDENTITY_ENV
            assert key not in env
        for key in m.shared_env:
            assert env[key] == m.shared_env[key]


# --------------------------------------------------------------------- pool.json


def test_pool_json_matches_the_golden():
    got = json.dumps(dict(core().pool_json), indent=2) + "\n"
    assert got == (POOL_GOLDEN / "pool-core.pool.json").read_text(encoding="utf-8")


def test_pool_json_member_shape():
    doc = core().pool_json
    assert doc["version"] == 1
    assert doc["pool"] == "core"
    assert doc["sha"] == SHA
    kv = doc["members"][0]
    assert kv["id"] == "kvservice"
    assert kv["app"] == "kvservice.main:build_app"
    assert kv["factory"] is True
    assert kv["port_name"] == "kvservice"
    assert kv["port_env"] == "POOL_PORT_KVSERVICE"
    assert kv["health_path"] == "/health"
    assert kv["secret_env"] == {"SVC_SECRET": "SVC_SECRET__KVSERVICE"}
    assert "PORT" not in kv["env"]
    assert kv["env"]["SVC_NAME"] == "kvservice"
    assert kv["env"]["SVC_ROOT_PATH"] == "/kv"


def test_every_member_gets_its_own_ams_data_dir():
    """The pool process's AMS_DATA_DIR is the shared parent; a member reading it
    would see its neighbours' state, so the runner swaps in a per-member value."""
    doc = core().pool_json
    root = "/home/harness/state/services/pool-core/root"
    values = [m["env"]["AMS_DATA_DIR"] for m in doc["members"]]
    assert values == [f"{root}/data/kvservice", f"{root}/data/timeservice"]
    assert len(set(values)) == len(values)
    for m in doc["members"]:
        assert m["env"]["AMS_DATA_DIR"] == f"{root}/data/{m['id']}"


def test_ams_data_dir_is_an_identity_key_and_never_in_the_pool_env():
    assert "AMS_DATA_DIR" in IDENTITY_ENV
    assert "AMS_DATA_DIR" not in core().decl.env


def test_pool_json_maps_every_declared_secret_by_name():
    members = [
        member("kvservice", extra_secret_names=("DEEPSEEK_API_KEY",)),
        member("timeservice"),
    ]
    p = build_pool("core", members, ctx(pool="core"))
    assert p.pool_json["members"][0]["secret_env"] == {
        "SVC_SECRET": "SVC_SECRET__KVSERVICE",
        "DEEPSEEK_API_KEY": "DEEPSEEK_API_KEY__KVSERVICE",
    }
    assert p.decl.secrets == (
        "SVC_SECRET__KVSERVICE",
        "DEEPSEEK_API_KEY__KVSERVICE",
        "SVC_SECRET__TIMESERVICE",
    )


def test_pool_json_records_the_mangled_port_name_for_a_hyphenated_id():
    m = dataclasses.replace(member("kvservice"), id="notification-svc")
    p = build_pool("core", [m, member("timeservice")], ctx(pool="core"))
    entry = p.pool_json["members"][0]
    assert entry["port_name"] == "notification_svc"
    assert entry["port_env"] == "POOL_PORT_NOTIFICATION_SVC"
    assert "notification_svc" in p.decl.ports
    assert p.decl.env["POOL_PORT_NOTIFICATION_SVC"] == "${PORT_notification_svc}"


def test_pool_json_holds_no_secret_values():
    """Only the name mapping, ever -- a value here would land in a 0640 sidecar."""
    members = [
        member("kvservice", extra_secret_names=("DEEPSEEK_API_KEY",)),
        member("timeservice"),
    ]
    p = build_pool("core", members, ctx(pool="core"))
    for m, entry in zip(p.members, p.pool_json["members"], strict=True):
        decl = m.translation.decl
        assert decl is not None
        # Nothing is invented: every value came from the member's own env plus
        # the derived AMS_DATA_DIR, and a secret never enters either (the
        # schema forbids the env overlap and reserves the AMS_ prefix).
        root = "/home/harness/state/services/pool-core/root"
        expected = {k: v for k, v in decl.env.items() if k != "PORT"}
        expected["AMS_DATA_DIR"] = f"{root}/data/{m.id}"
        assert entry["env"] == expected
        assert not set(entry["env"]) & set(decl.secrets)
        suffix = mangle_member(entry["id"])
        assert entry["secret_env"] == {n: f"{n}__{suffix}" for n in decl.secrets}
    # Belt and braces: a generated SVC_SECRET is a long opaque token, and no
    # value that is not an absolute path or a URL may have that shape.
    for entry in p.pool_json["members"]:
        for value in entry["env"].values():
            if value.startswith(("/", "http://", "https://")) or value == SHA:
                continue
            assert not re.search(r"[A-Za-z0-9+/=_-]{24,}", value), value


# --------------------------------------------------------------------- §3.2 errors


def test_non_injective_mangling_raises():
    a = dataclasses.replace(member("kvservice"), id="notification-svc")
    b = dataclasses.replace(member("timeservice"), id="notification_svc")
    with pytest.raises(TranslateError) as e:
        build_pool("core", [a, b], ctx(pool="core"))
    assert str(e.value) == (
        "pool 'core': member ids ['notification-svc', 'notification_svc'] mangle to "
        "the same env suffix NOTIFICATION_SVC"
    )


def test_a_member_named_pool_collides_with_the_admin_port():
    m = dataclasses.replace(member("kvservice"), id="pool")
    with pytest.raises(TranslateError) as e:
        build_pool("core", [m, member("timeservice")], ctx(pool="core"))
    assert str(e.value) == (
        "pool 'core': member id 'pool' collides with the reserved admin port name"
    )


def test_a_pool_of_one_is_refused():
    with pytest.raises(TranslateError) as e:
        build_pool("core", [member("kvservice")], ctx(pool="core"))
    assert str(e.value) == (
        "pool 'core': only one member ('kvservice') — refusing to create a pool of one"
    )


def test_a_pool_of_none_is_refused():
    with pytest.raises(TranslateError, match="pool 'core': no members"):
        build_pool("core", [], ctx(pool="core"))


def test_two_members_disagreeing_on_a_non_identity_key_raise():
    a = member("kvservice")
    b = member("timeservice")
    a = dataclasses.replace(
        a, id="emailservice", shared_env={**a.shared_env, "RESEND_FROM_ADDR": "a@x"}
    )
    b = dataclasses.replace(
        b,
        id="notificationservice",
        shared_env={**b.shared_env, "RESEND_FROM_ADDR": "b@x"},
    )
    with pytest.raises(TranslateError) as e:
        build_pool("core", [a, b], ctx(pool="core"))
    assert str(e.value) == (
        "pool 'core': members 'emailservice' and 'notificationservice' both set "
        "RESEND_FROM_ADDR to different values ('a@x' vs 'b@x'); a pool shares one "
        "process env for non-identity keys, so one of them must change or leave "
        "the pool"
    )


def test_two_members_agreeing_on_a_non_identity_key_are_fine():
    a = member("kvservice")
    b = member("timeservice")
    a = dataclasses.replace(a, shared_env={**a.shared_env, "RESEND_FROM_ADDR": "a@x"})
    b = dataclasses.replace(b, shared_env={**b.shared_env, "RESEND_FROM_ADDR": "a@x"})
    p = build_pool("core", [a, b], ctx(pool="core"))
    assert p.decl.env["RESEND_FROM_ADDR"] == "a@x"


def test_members_may_disagree_on_identity_keys():
    p = core()
    roots = {m.id: m.identity_env["SVC_ROOT_PATH"] for m in p.members}
    assert roots == {"kvservice": "/kv", "timeservice": "/time"}


def test_a_static_member_is_refused_by_build_pool():
    t = translate((MANIFESTS / "files-web.yaml").read_text(encoding="utf-8"), ctx())
    bad = PoolMember(
        id="files-web",
        translation=t,
        runtime_rel="web/files",
        app="x:app",
        factory=False,
        identity_env={},
        shared_env={},
        secret_names=(),
    )
    with pytest.raises(TranslateError, match="only kind: service can join a pool"):
        build_pool("core", [bad, member("kvservice")], ctx(pool="core"))


def test_build_pool_rejects_a_bad_pool_name():
    with pytest.raises(TranslateError, match="does not match"):
        build_pool("Core", [member("kvservice"), member("timeservice")], ctx(pool="core"))


# --------------------------------------------------------------------- goldens


def test_member_manifest_copies_match_the_shared_fixtures():
    for service_id in MEMBER_DIRS:
        assert (POOL_GOLDEN / f"{service_id}.yaml").read_bytes() == (
            MANIFESTS / f"{service_id}.yaml"
        ).read_bytes()


def test_translation_is_still_a_translation():
    t = translate(manifest("kvservice"), ctx(pool="core"))
    assert isinstance(t, Translation)
    assert t.kind == "service"
