"""Portable tests for the Layer-0 bootstrap.

``openssl`` and ``run_admin`` are both replaced with recorders: the point of
these tests is the *decisions* (what is generated once, what is never
overwritten, what argv touches a service root, what never reaches an output
stream), and none of that needs a real keypair or a real user namespace. The
Linux counterpart ``tests/linux/test_platform_bootstrap_live.py`` covers the
half that only a namespace can prove -- real ownership and real modes.
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path

import pytest

from ams import cli
from ams.platform import bootstrap as bs
from ams.runtime import RuntimeStore
from ams.schema import loads as decl_loads
from ams.secrets import store_for
from ams.state import StateDir
from ams.uidmap import UidBlock
from ams.userns import AdminResult

BLOCK = UidBlock(100_000, 100_000)

# A throwaway RSA keypair. Content is irrelevant (nothing here verifies a
# signature) but it must be stable so the "second run leaves identical bytes"
# assertion is about the bootstrap, not about openssl's randomness.
FAKE_PRIVATE = b"-----BEGIN PRIVATE KEY-----\nnot-a-real-key\n-----END PRIVATE KEY-----\n"
FAKE_PUBLIC = b"-----BEGIN PUBLIC KEY-----\nnot-a-real-key\n-----END PUBLIC KEY-----\n"


# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def openssl(monkeypatch):
    """Record openssl argv and write plausible files instead of running it."""
    calls: list[list[str]] = []

    def fake(argv, *, timeout_s=60.0):
        argv = list(argv)
        calls.append(argv)
        out = Path(argv[argv.index("-out") + 1])
        out.write_bytes(FAKE_PUBLIC if "-pubout" in argv else FAKE_PRIVATE)

    monkeypatch.setattr(bs, "_run_openssl", fake)
    return calls


@pytest.fixture
def admin(monkeypatch):
    """Record ``run_admin`` argv and actually perform the mkdir/cp/chmod parts.

    ``chown`` is the one thing a portable test cannot do, so it is recorded and
    skipped; ownership is asserted on Linux instead.
    """
    calls: list[list[str]] = []

    def fake(argv, block, **kwargs):
        argv = list(argv)
        calls.append(argv)
        if argv[0] == "mkdir":
            mode = int(argv[argv.index("-m") + 1], 8) if "-m" in argv else 0o755
            target = Path(argv[-1])
            target.mkdir(parents=True, exist_ok=True)
            os.chmod(target, mode)
        elif argv[0] == "cp":
            Path(argv[2]).write_bytes(Path(argv[1]).read_bytes())
        elif argv[0] == "chmod":
            os.chmod(Path(argv[2]), int(argv[1], 8))
        return AdminResult(tuple(argv), 0, b"", b"")

    monkeypatch.setattr(bs, "run_admin", fake)
    return calls


@pytest.fixture
def state(tmp_path) -> StateDir:
    return StateDir(tmp_path / "state")


@pytest.fixture
def store(tmp_path) -> RuntimeStore:
    return RuntimeStore(tmp_path / "store")


def _tree_hashes(root: Path) -> dict[str, str]:
    """sha256 of every regular file under ``root``, keyed by relative path."""
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


# --------------------------------------------------------------------------- keypair


def test_keypair_argv_is_a_list_and_never_a_shell_string(openssl, store):
    bs.ensure_keypair(store)

    genpkey, pubout = openssl
    # Both write into a temp dir inside <store>/platform and are moved into
    # place, so an interrupted run leaves no truncated key behind.
    assert genpkey[:7] == [
        "openssl",
        "genpkey",
        "-algorithm",
        "RSA",
        "-pkeyopt",
        "rsa_keygen_bits:2048",
        "-out",
    ]
    assert Path(genpkey[7]).name == "jwt-rs256.pem"
    assert pubout[:2] == ["openssl", "rsa"] and pubout[4] == "-pubout"
    assert pubout[2] == "-in" and pubout[3] == genpkey[7]
    assert pubout[5] == "-out" and Path(pubout[6]).name == "jwt-rs256.pub"
    for path in (genpkey[7], pubout[6]):
        assert Path(path).parent.parent == bs.platform_dir(store)
    # Every element is a separate argv entry: no spaces, no shell metacharacters
    # that would only matter if a shell were involved.
    for argv in openssl:
        assert all(" " not in a and ";" not in a and "|" not in a for a in argv)


def test_keypair_modes_and_directory_are_harness_only(openssl, store):
    bs.ensure_keypair(store)

    assert bs.jwt_private_path(store).stat().st_mode & 0o7777 == 0o600
    assert bs.jwt_public_path(store).stat().st_mode & 0o7777 == 0o644
    assert bs.platform_dir(store).stat().st_mode & 0o7777 == 0o700


def test_keypair_is_generated_once(openssl, store):
    assert bs.ensure_keypair(store) == ["key:private", "key:public"]
    before = bs.jwt_private_path(store).read_bytes()

    assert bs.ensure_keypair(store) == []
    assert len(openssl) == 2  # not four: openssl was not invoked a second time
    assert bs.jwt_private_path(store).read_bytes() == before


def test_half_a_keypair_is_regenerated_not_accepted(openssl, store):
    bs.ensure_keypair(store)
    bs.jwt_public_path(store).unlink()

    assert bs.ensure_keypair(store) == ["key:private", "key:public"]
    assert len(openssl) == 4
    assert bs.jwt_public_path(store).is_file()


def test_openssl_failure_becomes_a_bootstrap_error(monkeypatch, store):
    def boom(argv, *, timeout_s=60.0):
        raise bs.BootstrapError("openssl rsa exited 1: unable to load key")

    monkeypatch.setattr(bs, "_run_openssl", boom)
    with pytest.raises(bs.BootstrapError):
        bs.ensure_keypair(store)
    # The temp dir is cleaned up, so a retry does not see a stale private key.
    assert not bs.jwt_private_path(store).exists()


# --------------------------------------------------------------------------- secrets


def test_secrets_are_generated_for_every_declared_name(state):
    store = store_for(state)
    made = bs.ensure_secrets(
        store, {bs.REGISTRY_ID: bs.REGISTRY_SECRETS, bs.AUTH_ID: bs.AUTH_SECRETS}
    )

    assert set(made) == {
        "secret:registry/REGISTRY_ADMIN_TOKEN",
        "secret:auth/AUTH_SESSION_SECRET",
        "secret:auth/AUTH_PAT_VERIFY_TOKEN",
        "secret:auth/AUTH_M2M_SECRET",
    }
    assert store.names(bs.AUTH_ID) == sorted(bs.AUTH_SECRETS)
    assert store.missing(bs.REGISTRY_ID, bs.REGISTRY_SECRETS) == []
    # 32 bytes of entropy rendered as hex, matching `openssl rand -hex 32`.
    assert len(store.load(bs.REGISTRY_ID, ["REGISTRY_ADMIN_TOKEN"])["REGISTRY_ADMIN_TOKEN"]) == 64


def test_an_existing_secret_is_never_overwritten(state):
    store = store_for(state)
    store.set(bs.AUTH_ID, "AUTH_SESSION_SECRET", b"kept-across-restarts")

    made = bs.ensure_secrets(store, {bs.AUTH_ID: bs.AUTH_SECRETS})

    assert "secret:auth/AUTH_SESSION_SECRET" not in made
    assert store.load(bs.AUTH_ID, ["AUTH_SESSION_SECRET"]) == {
        "AUTH_SESSION_SECRET": "kept-across-restarts"
    }


def test_every_secret_value_stays_out_of_stdout_stderr_and_logs(
    state, store, openssl, capsys, caplog
):
    caplog.set_level(logging.DEBUG)
    bs.bootstrap(state, store)

    values = set()
    for sid, names in ((bs.REGISTRY_ID, bs.REGISTRY_SECRETS), (bs.AUTH_ID, bs.AUTH_SECRETS)):
        values.update(store_for(state).load(sid, names).values())
    assert len(values) == 4  # four distinct values, not one reused

    captured = capsys.readouterr()
    haystack = "\n".join(
        [captured.out, captured.err, caplog.text, *(r.getMessage() for r in caplog.records)]
    )
    assert haystack  # the test would pass vacuously against an empty haystack
    for value in values:
        assert value not in haystack
    # Nor into the declarations the run just wrote.
    for sid in (bs.REGISTRY_ID, bs.AUTH_ID):
        text = state.service_decl_path(sid).read_text()
        for value in values:
            assert value not in text


# --------------------------------------------------------------------------- declarations


@pytest.mark.parametrize("service_id", [bs.REGISTRY_ID, bs.AUTH_ID])
def test_declaration_validates_through_the_cli(state, store, openssl, service_id, capsys):
    bs.bootstrap(state, store)
    path = state.service_decl_path(service_id)

    assert cli.main(["validate", str(path)]) == 0
    assert f"OK {service_id}" in capsys.readouterr().out


@pytest.mark.parametrize("service_id", [bs.REGISTRY_ID, bs.AUTH_ID])
def test_declaration_round_trips_including_the_logging_table(state, service_id):
    decl = bs.layer0_declarations(state)[service_id]
    text = bs.render_declaration(decl, what="test")

    reloaded = decl_loads(text)
    assert reloaded == decl
    # The spliced-in table is the one emit_toml does not write itself.
    assert reloaded.logging.format == "level-prefix"
    assert "\n[logging]\nformat = " in text


def test_layer0_ports_are_fixed_and_cross_reference_each_other(state):
    registry = bs.layer0_declarations(state)[bs.REGISTRY_ID]
    auth = bs.layer0_declarations(state)[bs.AUTH_ID]

    assert registry.ports["main"] == 20100
    assert auth.ports["main"] == 20101
    # A declaration can only expand its OWN ${PORT_x}, so the cross references
    # have to be literals -- which is the entire reason the ports are fixed.
    assert registry.env["REGISTRY_AUTH_URL"] == "http://127.0.0.1:20101"
    assert auth.env["AUTH_REGISTRY_URL"] == "http://127.0.0.1:20100"
    # Signer and verifier agree: both issuers are the replica's auth address,
    # which is also what translate.py injects as AUTH_URL for Layer-1.
    assert registry.env["REGISTRY_JWT_ISSUER"] == auth.env["AUTH_JWT_ISSUER"]
    assert auth.env["AUTH_JWT_ISSUER"] == "http://127.0.0.1:20101"


def test_argv_uses_the_module_level_app_not_a_factory(state):
    decls = bs.layer0_declarations(state)
    assert decls[bs.REGISTRY_ID].start.argv[:2] == ("uvicorn", "registry.main:app")
    assert decls[bs.AUTH_ID].start.argv[:2] == ("uvicorn", "auth.main:app")
    for decl in decls.values():
        assert "--factory" not in decl.start.argv
        assert decl.start.argv[-1] == "${PORT_main}"
        assert decl.runtime.kind == "uv" and decl.runtime.sync is True


def test_auth_health_is_tcp_because_auth_has_no_health_route(state):
    decls = bs.layer0_declarations(state)
    assert decls[bs.AUTH_ID].health.kind == "tcp"
    assert decls[bs.REGISTRY_ID].health.kind == "http"
    assert decls[bs.REGISTRY_ID].health.path == "/health"
    for decl in decls.values():
        assert decl.health.start_period_s == 120.0
        assert decl.limits.memory_max == "200M"
        assert decl.limits.pids_max == 64


def test_no_github_oauth_placeholders_are_invented(state):
    """``_build_oauth_providers`` skips GitHub unless BOTH creds are set, so the
    app starts without them -- and a placeholder would render a login button
    leading to a GitHub error page."""
    auth = bs.layer0_declarations(state)[bs.AUTH_ID]
    assert not any("GITHUB" in name for name in auth.secrets)
    assert not any("GITHUB" in name for name in auth.env)


def test_env_paths_all_live_under_the_service_root(state):
    for service_id, decl in bs.layer0_declarations(state).items():
        root = str(state.service_root(service_id))
        for name, value in decl.env.items():
            if value.startswith("/"):
                assert value.startswith(root + "/"), f"{service_id}.{name} escapes the root"
        assert decl.env[f"{service_id.upper()}_JWT_PRIVATE_KEY_PATH"].endswith("/etc/jwt-rs256.pem")


def test_a_drifted_declaration_is_repaired_and_reported(state, store, openssl):
    bs.bootstrap(state, store)
    path = state.service_decl_path(bs.REGISTRY_ID)
    path.write_text(path.read_text().replace('memory_max = "200M"', 'memory_max = "1G"'))

    result = bs.bootstrap(state, store)

    assert result.updated == ("decl:registry",)
    assert result.created == ()
    assert 'memory_max = "200M"' in path.read_text()


# --------------------------------------------------------------------------- idempotence


def test_second_run_creates_nothing_and_changes_no_bytes(state, store, openssl, tmp_path):
    examples = tmp_path / "examples"
    first = bs.bootstrap(state, store, examples_dir=examples)
    assert first.changed
    before = {**_tree_hashes(state.root), **_tree_hashes(store.root), **_tree_hashes(examples)}

    second = bs.bootstrap(state, store, examples_dir=examples)

    assert second.created == ()
    assert second.updated == ()
    assert not second.changed
    assert sorted(second.existing) == sorted(first.created)
    assert {
        **_tree_hashes(state.root),
        **_tree_hashes(store.root),
        **_tree_hashes(examples),
    } == before


def test_bootstrap_does_not_touch_service_roots(state, store, openssl, admin):
    """No uid block exists at bootstrap time, so there is nothing to place a key
    into; roots are the sync loop's job (T3.1)."""
    bs.bootstrap(state, store)

    assert admin == []
    assert not state.service_root(bs.REGISTRY_ID).exists()


def test_examples_render_against_the_target_host_path(state, store, openssl, tmp_path):
    examples = tmp_path / "examples"
    bs.bootstrap(state, store, examples_dir=examples)

    text = (examples / "registry" / "service.toml").read_text()
    assert "/home/harness/store/state/services/registry/root/data/registry.db" in text
    assert str(tmp_path / "state") not in text


def test_checked_in_examples_match_the_generator():
    """The files under ``examples/platform/layer0/`` are generated, and a stale
    copy is worse than none: it is read as documentation."""
    repo = Path(__file__).resolve().parent.parent
    example_state = StateDir(bs.EXAMPLE_STATE_ROOT)
    for service_id, decl in bs.layer0_declarations(example_state).items():
        path = repo / "examples" / "platform" / "layer0" / service_id / "service.toml"
        assert path.is_file(), f"{path} is missing"
        expected = bs.render_declaration(decl, what=bs._WHAT[service_id])
        assert path.read_text(encoding="utf-8") == expected


# --------------------------------------------------------------------------- key placement


def test_place_public_key_argv_sequence(state, store, openssl, admin):
    bs.ensure_keypair(store)
    admin.clear()

    assert bs.place_jwt_key(state, "kvservice", BLOCK, store=store) is True

    etc = str(state.service_root("kvservice") / "etc")
    assert admin == [
        ["mkdir", "-m", "755", "-p", etc],
        ["cp", str(bs.jwt_public_path(store)), f"{etc}/jwt-rs256.pub"],
        ["chown", "-R", "1000:1000", etc],
        ["chmod", "0444", f"{etc}/jwt-rs256.pub"],
    ]
    for argv in admin:
        assert all(isinstance(a, str) and " " not in a for a in argv)
    assert Path(etc, "jwt-rs256.pub").read_bytes() == FAKE_PUBLIC


def test_place_private_key_uses_0400(state, store, openssl, admin):
    bs.ensure_keypair(store)
    admin.clear()

    bs.place_jwt_key(state, bs.AUTH_ID, BLOCK, store=store, private=True)

    dst = str(state.service_root(bs.AUTH_ID) / "etc" / "jwt-rs256.pem")
    assert admin[1] == ["cp", str(bs.jwt_private_path(store)), dst]
    assert admin[3] == ["chmod", "0400", dst]
    assert Path(dst).stat().st_mode & 0o7777 == 0o400


def test_place_jwt_key_without_a_keypair_raises(state, store):
    with pytest.raises(bs.BootstrapError, match="run ensure_keypair first"):
        bs.place_jwt_key(state, bs.AUTH_ID, BLOCK, store=store)


def test_place_jwt_key_warm_path_forks_nothing(state, store, openssl, admin, monkeypatch):
    """The sync loop calls this for every service on every tick; an already
    correct key must not cost four namespace forks."""
    bs.ensure_keypair(store)
    bs.place_jwt_key(state, "kvservice", BLOCK, store=store)
    dst = state.service_root("kvservice") / "etc" / "jwt-rs256.pub"
    # The recorder cannot chown, so fake the ownership the real admin ns sets.
    real_stat = os.stat

    def fake_stat(path, *a, **kw):
        st = real_stat(path, *a, **kw)
        return os.stat_result(
            (
                st.st_mode,
                st.st_ino,
                st.st_dev,
                st.st_nlink,
                BLOCK.uid_start,
                BLOCK.gid_start,
                st.st_size,
                0,
                0,
                0,
            )
        )

    monkeypatch.setattr(
        bs.os, "stat", lambda p, *a, **k: fake_stat(p) if Path(p) == dst else real_stat(p)
    )
    admin.clear()

    assert bs.place_jwt_key(state, "kvservice", BLOCK, store=store) is False
    assert admin == []


def test_ensure_service_dirs_creates_data_and_etc(state, admin):
    assert bs.ensure_service_dirs(state, "kvservice", BLOCK) is True

    root = state.service_root("kvservice")
    assert [a[0] for a in admin] == ["mkdir", "mkdir", "chown"]
    assert admin[0][-1] == str(root / "data")
    assert admin[0][2] == "750"
    assert admin[1][-1] == str(root / "etc")
    assert admin[2] == ["chown", "-R", "1000:1000", str(root)]
    assert (root / "data").is_dir() and (root / "etc").is_dir()


# --------------------------------------------------------------------------- cli


def test_main_exits_zero_and_prints_no_values(monkeypatch, tmp_path, openssl, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    state_dir = tmp_path / "state"

    rc = bs.main(["--state", str(state_dir), "--store", str(tmp_path / "store")])

    assert rc == 0
    values = set(store_for(StateDir(state_dir)).load(bs.AUTH_ID, bs.AUTH_SECRETS).values())
    captured = capsys.readouterr()
    for value in values:
        assert value not in captured.out + captured.err + caplog.text


def test_main_reports_a_bootstrap_failure_as_exit_one(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(
        bs, "ensure_keypair", lambda store: (_ for _ in ()).throw(bs.BootstrapError("no openssl"))
    )
    rc = bs.main(["--state", str(tmp_path / "state"), "--store", str(tmp_path / "store")])
    assert rc == 1


def test_loopback_url_is_the_only_shape_the_replica_emits():
    assert bs.loopback_url(20100) == "http://127.0.0.1:20100"
    with pytest.raises(bs.BootstrapError):
        bs.loopback_url("20100")  # type: ignore[arg-type]
