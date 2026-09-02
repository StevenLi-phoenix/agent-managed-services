from pathlib import Path

import pytest

from ams.schema import DeclError
from ams.state import StateCorrupt, StateDir, read_json_checked

MINIMAL = """
id = "demo"
[start]
argv = ["sleep", "1"]
"""


def test_layout_paths(tmp_path: Path):
    sd = StateDir(tmp_path)
    assert sd.services_dir == tmp_path / "services"
    assert sd.service_dir("web") == tmp_path / "services" / "web"
    assert sd.service_decl_path("web") == tmp_path / "services" / "web" / "service.toml"
    assert sd.service_root("web") == tmp_path / "services" / "web" / "root"
    assert sd.service_runtime_dir("web") == tmp_path / "services" / "web" / "runtime"
    assert sd.runtime_state_dir == tmp_path / "state"
    assert sd.uidmap_state == tmp_path / "state" / "uidmap.json"
    assert sd.ports_state == tmp_path / "state" / "ports.json"
    assert sd.logs_dir == tmp_path / "logs"


def test_ensure_creates_dirs_not_service_roots(tmp_path: Path):
    sd = StateDir(tmp_path)
    sd.ensure()
    assert sd.services_dir.is_dir()
    assert sd.runtime_state_dir.is_dir()
    assert sd.logs_dir.is_dir()
    assert not sd.service_root("web").exists()
    # mode forced regardless of umask
    assert (sd.services_dir.stat().st_mode & 0o777) == 0o750


def test_ensure_idempotent(tmp_path: Path):
    sd = StateDir(tmp_path)
    sd.ensure()
    sd.ensure()  # must not raise
    assert sd.services_dir.is_dir()


def _write_decl(sd: StateDir, service_id: str, text: str) -> None:
    d = sd.service_dir(service_id)
    d.mkdir(parents=True)
    sd.service_decl_path(service_id).write_text(text, encoding="utf-8")


def test_list_service_ids_skips_bad_ids(tmp_path: Path):
    sd = StateDir(tmp_path)
    sd.ensure()
    _write_decl(sd, "web-app", MINIMAL.replace('"demo"', '"web-app"'))
    _write_decl(sd, "Bad_ID", MINIMAL)  # invalid id: uppercase/underscore
    # a dir with no service.toml is ignored entirely
    (sd.services_dir / "not-a-service").mkdir()
    # a stray file (not a dir) is ignored
    (sd.services_dir / "stray.txt").write_text("x", encoding="utf-8")

    ids = sd.list_service_ids()
    assert ids == ["web-app"]


def test_list_service_ids_empty_when_missing(tmp_path: Path):
    sd = StateDir(tmp_path / "nope")
    assert sd.list_service_ids() == []


def test_load_declarations_skips_invalid_returns_valid(tmp_path: Path):
    sd = StateDir(tmp_path)
    sd.ensure()
    _write_decl(sd, "good", MINIMAL.replace('"demo"', '"good"'))
    _write_decl(sd, "bad", 'id = "bad"\n[start]\nargv = []\n')  # empty argv -> DeclError

    decls = sd.load_declarations()
    assert set(decls) == {"good"}
    assert decls["good"].id == "good"


def test_load_declaration_raises_for_invalid(tmp_path: Path):
    sd = StateDir(tmp_path)
    sd.ensure()
    _write_decl(sd, "bad", 'id = "bad"\n[start]\nargv = []\n')
    with pytest.raises(DeclError):
        sd.load_declaration("bad")


def test_load_declaration_raises_for_missing(tmp_path: Path):
    sd = StateDir(tmp_path)
    sd.ensure()
    with pytest.raises(FileNotFoundError):
        sd.load_declaration("nope")


def test_from_env_uses_env_var(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("AMS_STATE_DIR", str(tmp_path / "envdir"))
    sd = StateDir.from_env(default=tmp_path / "default")
    assert sd.root == tmp_path / "envdir"


def test_from_env_uses_default_when_no_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.delenv("AMS_STATE_DIR", raising=False)
    sd = StateDir.from_env(default=tmp_path / "default")
    assert sd.root == tmp_path / "default"


def test_from_env_uses_home_when_no_env_no_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("AMS_STATE_DIR", raising=False)
    sd = StateDir.from_env()
    assert sd.root == Path.home() / "ams-state"


def test_read_json_checked_round_trip(tmp_path: Path):
    p = tmp_path / "x.json"
    p.write_text('{"a": 1}', encoding="utf-8")
    assert read_json_checked(p) == {"a": 1}


def test_read_json_checked_raises_state_corrupt_on_bad_json(tmp_path: Path):
    p = tmp_path / "x.json"
    p.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(StateCorrupt, match="corrupt JSON"):
        read_json_checked(p)


def test_ensure_preserves_a_deliberate_o_x_on_services(tmp_path: Path):
    """`ams.cli._ensure_traversable` adds o+x to services/ so a service uid can
    resolve its own workdir by path. `ensure()` runs again from every entry
    point -- including `ams.platform.bootstrap` while services are running --
    and must not take it back: the next spawn of every running service then
    fails with PermissionError on its own interpreter. Observed live on
    racknerd; see `.claude/state/diagnosis-layer0.md`.
    """
    import stat as _stat

    sd = StateDir(tmp_path)
    sd.ensure()
    assert (sd.services_dir.stat().st_mode & 0o777) == 0o750

    sd.services_dir.chmod(0o751)
    sd.ensure()
    mode = sd.services_dir.stat().st_mode & 0o777
    assert mode == 0o751
    # ...and never o+r: the directory stays unlistable by a service uid.
    assert not mode & _stat.S_IROTH
