"""The write-only secret store (D16): storage, injection, CLI.

The load-bearing property under test is negative: a stored value must never
appear in stdout, stderr, a log record, or a repr. Several tests below assert
exactly that, because everything else about this module is easy to fix and that
one mistake is not (a leaked credential has to be rotated).
"""

from __future__ import annotations

import io
import logging
import os
import stat
from pathlib import Path

import pytest

from ams.cli import main
from ams.schema import from_dict
from ams.secrets import (
    MissingSecret,
    SecretError,
    SecretStore,
    make_extra_env_for,
    store_for,
    warn_missing_secrets,
)
from ams.state import StateDir

VALUE = "hunter2-e0f5c1a9"


@pytest.fixture
def store(tmp_path: Path) -> SecretStore:
    return SecretStore(tmp_path)


def _decl(service_id: str = "svc", **extra: object):
    data: dict[str, object] = {"id": service_id, "start": {"argv": ["/bin/echo", "hi"]}}
    data.update(extra)
    return from_dict(data)


def _state_with(tmp_path: Path, service_id: str, toml_text: str) -> StateDir:
    service_dir = tmp_path / "services" / service_id
    service_dir.mkdir(parents=True, exist_ok=True)
    (service_dir / "service.toml").write_text(toml_text, encoding="utf-8")
    return StateDir(tmp_path)


# ------------------------------------------------------------------- storage


def test_round_trip(store: SecretStore) -> None:
    store.set("svc", "SVC_SECRET", VALUE.encode())
    assert store.load("svc", ["SVC_SECRET"]) == {"SVC_SECRET": VALUE}
    assert store.names("svc") == ["SVC_SECRET"]


def test_the_store_lives_under_secrets_in_the_state_dir(tmp_path: Path) -> None:
    state = StateDir(tmp_path)
    assert store_for(state).dir == tmp_path / "secrets"
    assert store_for(state).path("svc", "K") == tmp_path / "secrets" / "svc" / "K"


def test_directories_are_0700_and_files_0600(store: SecretStore) -> None:
    path = store.set("svc", "K", b"v")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(store.service_dir("svc")).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(store.dir).st_mode) == 0o700


def test_modes_survive_a_permissive_umask(store: SecretStore) -> None:
    """mkdir/open honour the umask; the store must force its modes anyway."""
    old = os.umask(0o000)
    try:
        path = store.set("svc", "K", b"v")
    finally:
        os.umask(old)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(store.service_dir("svc")).st_mode) == 0o700


def test_overwrite_is_atomic_and_leaves_no_temp_file(store: SecretStore) -> None:
    store.set("svc", "K", b"first")
    store.set("svc", "K", b"second")
    assert store.load("svc", ["K"]) == {"K": "second"}
    assert [p.name for p in store.service_dir("svc").iterdir()] == ["K"]


def test_a_failed_write_leaves_no_temp_file(store: SecretStore, monkeypatch) -> None:
    store.set("svc", "K", b"first")

    def boom(*_a: object, **_kw: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        store.set("svc", "K", b"second")
    assert [p.name for p in store.service_dir("svc").iterdir()] == ["K"]
    assert store.load("svc", ["K"]) == {"K": "first"}  # the old value survived


def test_exactly_one_trailing_newline_is_stripped(store: SecretStore) -> None:
    store.set("svc", "A", b"value\n")  # `echo secret | ams secret set ...`
    store.set("svc", "B", b"value\n\n")  # a value that really ends in a newline
    store.set("svc", "C", b"value\r\n")  # CRLF counts as one newline
    store.set("svc", "D", b"value")
    got = store.load("svc", ["A", "B", "C", "D"])
    assert got == {"A": "value", "B": "value\n", "C": "value", "D": "value"}


def test_an_empty_value_is_rejected(store: SecretStore) -> None:
    for raw in (b"", b"\n"):
        with pytest.raises(SecretError, match="empty"):
            store.set("svc", "K", raw)


def test_a_nul_byte_is_rejected(store: SecretStore) -> None:
    with pytest.raises(SecretError, match="NUL"):
        store.set("svc", "K", b"va\0lue")
    assert store.names("svc") == []


def test_multiline_values_survive(store: SecretStore) -> None:
    pem = b"-----BEGIN KEY-----\nabc\ndef\n-----END KEY-----\n"
    store.set("svc", "KEY", pem)
    assert store.load("svc", ["KEY"])["KEY"] == pem.decode().rstrip("\n")


def test_invalid_service_ids_and_names_are_rejected(store: SecretStore) -> None:
    for bad_id in ("../escape", "UPPER", "", "a/b", "9lives"):
        with pytest.raises(SecretError, match="service id"):
            store.set(bad_id, "K", b"v")
    for bad_name in ("../escape", "with-dash", "", "9K", "a b"):
        with pytest.raises(SecretError, match="secret name"):
            store.set("svc", bad_name, b"v")


def test_reserved_names_are_rejected(store: SecretStore) -> None:
    for name in ("PATH", "HOME", "AMS_SERVICE_ID", "PORT_main", "UV_CACHE_DIR"):
        with pytest.raises(SecretError, match="reserved"):
            store.set("svc", name, b"v")


def test_a_missing_secret_names_the_service_and_the_secret(store: SecretStore) -> None:
    store.set("svc", "PRESENT", b"v")
    with pytest.raises(MissingSecret) as excinfo:
        store.load("svc", ["PRESENT", "ABSENT"])
    message = str(excinfo.value)
    assert "svc" in message and "ABSENT" in message


def test_names_ignores_temp_and_unnameable_files(store: SecretStore) -> None:
    store.set("svc", "K", b"v")
    (store.service_dir("svc") / ".K.tmp999").write_text("x")
    (store.service_dir("svc") / "not-a-name").write_text("x")
    assert store.names("svc") == ["K"]
    assert store.names("unknown") == []


def test_remove_and_remove_all(store: SecretStore) -> None:
    store.set("svc", "A", b"1")
    store.set("svc", "B", b"2")
    assert store.remove("svc", "A") is True
    assert store.remove("svc", "A") is False
    assert store.names("svc") == ["B"]
    assert store.remove_all("svc") == 1
    assert store.names("svc") == []
    assert not store.service_dir("svc").exists()
    assert store.remove_all("svc") == 0  # idempotent


def test_missing_lists_only_the_unset_names(store: SecretStore) -> None:
    store.set("svc", "A", b"1")
    assert store.missing("svc", ["A", "B", "C"]) == ["B", "C"]
    assert store.missing("svc", ["A"]) == []


def test_repr_never_contains_a_value(store: SecretStore) -> None:
    store.set("svc", "K", VALUE.encode())
    assert VALUE not in repr(store)
    assert VALUE not in str(store)
    assert "secrets" in repr(store)


def test_storing_logs_the_name_but_never_the_value(store: SecretStore, caplog) -> None:
    with caplog.at_level(logging.DEBUG):
        store.set("svc", "SVC_SECRET", VALUE.encode())
        store.load("svc", ["SVC_SECRET"])
        store.remove("svc", "SVC_SECRET")
    assert "SVC_SECRET" in caplog.text
    assert VALUE not in caplog.text


# ----------------------------------------------------------------- injection


def test_extra_env_for_adds_the_declared_secret(tmp_path: Path) -> None:
    state = StateDir(tmp_path)
    store_for(state).set("svc", "K", VALUE.encode())
    env, path_prepend = make_extra_env_for(state)(_decl(secrets=["K"]))
    assert env == {"K": VALUE}
    assert path_prepend == ()


def test_extra_env_for_raises_when_the_secret_is_absent(tmp_path: Path) -> None:
    lookup = make_extra_env_for(StateDir(tmp_path))
    with pytest.raises(MissingSecret, match="K"):
        lookup(_decl(secrets=["K"]))


def test_secrets_win_over_runtime_extras_and_keep_the_path_prepend(tmp_path: Path) -> None:
    state = StateDir(tmp_path)
    store_for(state).set("svc", "K", b"from-store")
    runtime = lambda _decl: ({"K": "from-runtime", "VIRTUAL_ENV": "/v"}, ("/v/bin",))  # noqa: E731
    env, path_prepend = make_extra_env_for(state, runtime)(_decl(secrets=["K"]))
    assert env == {"K": "from-store", "VIRTUAL_ENV": "/v"}
    assert path_prepend == ("/v/bin",)


def test_a_declaration_without_secrets_is_untouched(tmp_path: Path) -> None:
    runtime = lambda _decl: ({"VIRTUAL_ENV": "/v"}, ("/v/bin",))  # noqa: E731
    assert make_extra_env_for(StateDir(tmp_path), runtime)(_decl()) == (
        {"VIRTUAL_ENV": "/v"},
        ("/v/bin",),
    )


def test_the_injected_value_reaches_the_service_environment(tmp_path: Path) -> None:
    """SpawnRequest.env is where extra_env actually becomes the child's env."""
    from ams.spawn import SpawnRequest

    state = StateDir(tmp_path)
    store_for(state).set("svc", "K", VALUE.encode())
    decl = _decl(secrets=["K"])
    extra, prepend = make_extra_env_for(state)(decl)
    req = SpawnRequest(decl=decl, root=tmp_path, ports={}, extra_env=extra, path_prepend=prepend)
    env = req.env()
    assert env["K"] == VALUE
    assert env["PATH"].endswith("/usr/local/bin:/usr/bin:/bin")  # reserved names still win


def test_warn_missing_secrets_names_every_gap(tmp_path: Path, caplog) -> None:
    state = StateDir(tmp_path)
    store_for(state).set("two", "B", b"v")
    declarations = {
        "one": _decl("one", secrets=["A"]),
        "two": _decl("two", secrets=["B"]),
        "three": _decl("three"),
    }
    with caplog.at_level(logging.WARNING):
        assert warn_missing_secrets(state, declarations) == ["one"]
    assert "one declares secrets with no stored value: A" in caplog.text
    assert "three" not in caplog.text


def test_build_supervisor_injects_secrets(tmp_path: Path) -> None:
    from ams.cli import build_supervisor

    toml = 'id = "svc"\nsecrets = ["K"]\n[start]\nargv = ["/bin/echo", "hi"]\n'
    state = _state_with(tmp_path, "svc", toml)
    store_for(state).set("svc", "K", VALUE.encode())
    asm = build_supervisor(state, isolation=False)
    assert asm.registered == ["svc"]
    extra, _ = asm.supervisor._extra_env_for(asm.declarations["svc"])
    assert extra["K"] == VALUE


def test_build_supervisor_warns_but_starts_when_a_secret_is_missing(tmp_path: Path, caplog) -> None:
    from ams.cli import build_supervisor

    toml = 'id = "svc"\nsecrets = ["K"]\n[start]\nargv = ["/bin/echo", "hi"]\n'
    state = _state_with(tmp_path, "svc", toml)
    with caplog.at_level(logging.WARNING):
        asm = build_supervisor(state, isolation=False)
    assert asm.registered == ["svc"]  # the harness still comes up
    assert "svc declares secrets with no stored value: K" in caplog.text
    with pytest.raises(MissingSecret):  # ... and the start is what fails
        asm.supervisor._extra_env_for(asm.declarations["svc"])


# ----------------------------------------------------------------------- cli


def _stdin(monkeypatch, text: str) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(text))


def _stdin_binary(monkeypatch, data: bytes) -> None:
    class _Stdin:
        buffer = io.BytesIO(data)

        def isatty(self) -> bool:
            return False

    monkeypatch.setattr("sys.stdin", _Stdin())


def test_cli_set_reads_stdin_and_prints_nothing(tmp_path: Path, monkeypatch, capsys) -> None:
    _stdin(monkeypatch, VALUE + "\n")
    rc = main(["secret", "set", "svc", "SVC_SECRET", "--state-dir", str(tmp_path)])
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert VALUE not in captured.err
    assert SecretStore(tmp_path).load("svc", ["SVC_SECRET"]) == {"SVC_SECRET": VALUE}


def test_cli_set_reads_binary_stdin(tmp_path: Path, monkeypatch) -> None:
    _stdin_binary(monkeypatch, VALUE.encode() + b"\n")
    assert main(["secret", "set", "svc", "K", "--state-dir", str(tmp_path)]) == 0
    assert SecretStore(tmp_path).load("svc", ["K"]) == {"K": VALUE}


def test_cli_set_from_file(tmp_path: Path, capsys) -> None:
    source = tmp_path / "value.txt"
    source.write_text(VALUE + "\n")
    rc = main(
        ["secret", "set", "svc", "K", "--from-file", str(source), "--state-dir", str(tmp_path)]
    )
    assert rc == 0
    assert capsys.readouterr().out == ""
    assert SecretStore(tmp_path).load("svc", ["K"]) == {"K": VALUE}


def test_cli_set_hints_on_a_tty_and_still_reads(tmp_path: Path, monkeypatch, capsys) -> None:
    class _Tty:
        buffer = io.BytesIO(VALUE.encode())

        def isatty(self) -> bool:
            return True

    monkeypatch.setattr("sys.stdin", _Tty())
    assert main(["secret", "set", "svc", "K", "--state-dir", str(tmp_path)]) == 0
    err = capsys.readouterr().err
    assert "stdin" in err and VALUE not in err


def test_cli_set_rejects_a_bad_name_without_writing(tmp_path: Path, monkeypatch, capsys) -> None:
    _stdin(monkeypatch, VALUE)
    rc = main(["secret", "set", "svc", "PATH", "--state-dir", str(tmp_path)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "reserved" in err and VALUE not in err
    assert SecretStore(tmp_path).names("svc") == []


def test_cli_set_reports_an_empty_value(tmp_path: Path, monkeypatch, capsys) -> None:
    _stdin(monkeypatch, "\n")
    assert main(["secret", "set", "svc", "K", "--state-dir", str(tmp_path)]) == 1
    assert "empty" in capsys.readouterr().err


def test_cli_list_prints_names_only(tmp_path: Path, capsys) -> None:
    store = SecretStore(tmp_path)
    store.set("svc", "B", VALUE.encode())
    store.set("svc", "A", VALUE.encode())
    assert main(["secret", "list", "svc", "--state-dir", str(tmp_path)]) == 0
    captured = capsys.readouterr()
    assert captured.out.split() == ["A", "B"]
    assert VALUE not in captured.out and VALUE not in captured.err


def test_cli_rm(tmp_path: Path, capsys) -> None:
    SecretStore(tmp_path).set("svc", "K", b"v")
    assert main(["secret", "rm", "svc", "K", "--state-dir", str(tmp_path)]) == 0
    assert SecretStore(tmp_path).names("svc") == []
    assert main(["secret", "rm", "svc", "K", "--state-dir", str(tmp_path)]) == 1
    assert "no stored value" in capsys.readouterr().err


def test_cli_check_exits_1_and_lists_the_missing_names(tmp_path: Path, capsys) -> None:
    toml = 'id = "svc"\nsecrets = ["A", "B"]\n[start]\nargv = ["/bin/echo"]\n'
    _state_with(tmp_path, "svc", toml)
    SecretStore(tmp_path).set("svc", "A", b"v")
    assert main(["secret", "check", "svc", "--state-dir", str(tmp_path)]) == 1
    assert capsys.readouterr().out.strip() == "MISSING svc: B"

    SecretStore(tmp_path).set("svc", "B", b"v")
    assert main(["secret", "check", "svc", "--state-dir", str(tmp_path)]) == 0
    assert capsys.readouterr().out.strip() == "OK svc (2 secret(s) set)"


def test_cli_check_on_an_unknown_service_fails(tmp_path: Path, capsys) -> None:
    assert main(["secret", "check", "ghost", "--state-dir", str(tmp_path)]) == 1
    assert "ghost" in capsys.readouterr().err


def test_cli_uses_the_state_dir_env_var(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AMS_STATE_DIR", str(tmp_path))
    _stdin(monkeypatch, VALUE)
    assert main(["secret", "set", "svc", "K"]) == 0
    assert SecretStore(tmp_path).names("svc") == ["K"]


def test_no_command_output_anywhere_contains_the_value(
    tmp_path: Path, monkeypatch, capsys, caplog
) -> None:
    """One sweep over every subcommand: nothing prints or logs a value."""
    toml = 'id = "svc"\nsecrets = ["K"]\n[start]\nargv = ["/bin/echo"]\n'
    _state_with(tmp_path, "svc", toml)
    state_args = ["--state-dir", str(tmp_path), "--log-level", "DEBUG"]
    with caplog.at_level(logging.DEBUG):
        _stdin(monkeypatch, VALUE)
        assert main(["secret", "set", "svc", "K", *state_args]) == 0
        assert main(["secret", "list", "svc", *state_args]) == 0
        assert main(["secret", "check", "svc", *state_args]) == 0
        assert main(["secret", "rm", "svc", "K", *state_args]) == 0
    captured = capsys.readouterr()
    assert VALUE not in captured.out
    assert VALUE not in captured.err
    assert VALUE not in caplog.text


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory permissions")
def test_an_unreadable_store_is_reported_not_fatal(tmp_path: Path, caplog) -> None:
    """An advisory diagnostic must never be able to abort startup or a reload.

    The same shape as the bug where an env hook raised outside
    Supervisor.start's guard: one broken service took down every service after
    it. Here a store directory nobody can read is logged and skipped.
    """
    state = StateDir(tmp_path)
    store = store_for(state)
    store.set("broken", "A", b"v")
    store.set("fine", "B", b"v")
    store.remove("fine", "B")  # 'fine' is genuinely missing its secret
    store.service_dir("broken").chmod(0o000)
    declarations = {
        "broken": _decl("broken", secrets=["A"]),
        "fine": _decl("fine", secrets=["B"]),
    }
    try:
        with caplog.at_level(logging.DEBUG):
            warned = warn_missing_secrets(state, declarations)
    finally:
        store.service_dir("broken").chmod(0o700)
    # The unreadable one is reported and skipped ...
    assert "cannot inspect the secret store for broken" in caplog.text
    # ... and it does not suppress the warning for the service behind it.
    assert warned == ["fine"]
    assert "fine declares secrets with no stored value: B" in caplog.text


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory permissions")
def test_build_supervisor_survives_an_unreadable_store(tmp_path: Path, caplog) -> None:
    """`ams run` must still come up; the affected service fails at its own start."""
    from ams.cli import build_supervisor

    toml = 'id = "svc"\nsecrets = ["K"]\n[start]\nargv = ["/bin/echo", "hi"]\n'
    state = _state_with(tmp_path, "svc", toml)
    store = store_for(state)
    store.set("svc", "K", b"v")
    store.service_dir("svc").chmod(0o000)
    try:
        with caplog.at_level(logging.ERROR):
            asm = build_supervisor(state, isolation=False)
        assert asm.registered == ["svc"]
        assert "cannot inspect the secret store for svc" in caplog.text
        # The start is where it fails, and only for this service.
        with pytest.raises((MissingSecret, PermissionError)):
            asm.supervisor._extra_env_for(asm.declarations["svc"])
    finally:
        store.service_dir("svc").chmod(0o700)


def test_build_supervisor_survives_a_raising_warn_hook(tmp_path: Path, caplog, monkeypatch) -> None:
    """The outer guard: even a broken diagnostic must not abort `ams run`.

    Distinct from test_build_supervisor_survives_an_unreadable_store, which
    exercises the guard *inside* warn_missing_secrets. This one makes the
    function itself raise -- standing in for a bug in the diagnostic or a
    corrupt state layout -- and asserts the boot still produces a supervisor
    with the service registered.
    """
    import ams.secrets as secrets_module
    from ams.cli import build_supervisor

    toml = 'id = "svc"\nsecrets = ["K"]\n[start]\nargv = ["/bin/echo", "hi"]\n'
    state = _state_with(tmp_path, "svc", toml)
    store_for(state).set("svc", "K", VALUE.encode())

    def boom(*_a: object, **_kw: object) -> list[str]:
        raise OSError("state dir vanished mid-check")

    monkeypatch.setattr(secrets_module, "warn_missing_secrets", boom)
    with caplog.at_level(logging.WARNING):
        asm = build_supervisor(state, isolation=False)
    assert asm.registered == ["svc"]  # the harness came up anyway
    assert "could not check declared secrets against the store" in caplog.text
    assert "OSError" in caplog.text
    # The secret itself is still delivered: only the advisory check was broken.
    extra, _ = asm.supervisor._extra_env_for(asm.declarations["svc"])
    assert extra["K"] == VALUE
    assert VALUE not in caplog.text
