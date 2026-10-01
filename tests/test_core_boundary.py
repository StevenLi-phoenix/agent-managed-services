"""The supervisor core (``ams`` minus ``ams.platform``) stands on its own.

``ams.platform`` is the runtime for one specific application (the api core);
everything else -- schema, supervisor, isolation, secrets, control socket,
escalation journal, CLI -- is a general rootless supervisor. These tests are
the guarantee behind that claim: no core module imports the platform at module
level, and with ``ams.platform`` made unimportable the CLI still validates and
supervises.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
CORE_MODULES = sorted(
    p.stem for p in (SRC / "ams").glob("*.py") if p.stem not in ("__init__", "__main__")
)
EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "hello" / "service.toml"


def _platform_imports(tree: ast.AST) -> list[tuple[int, bool]]:
    """(line, at_module_level) for every import of ams.platform in ``tree``."""
    found: list[tuple[int, bool]] = []
    top_level = {id(n) for n in getattr(tree, "body", [])}
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        if any(n == "ams.platform" or n.startswith("ams.platform.") for n in names):
            found.append((node.lineno, id(node) in top_level))
    return found


def test_no_core_module_imports_the_platform_at_module_level() -> None:
    offenders = []
    for stem in CORE_MODULES:
        path = SRC / "ams" / f"{stem}.py"
        for line, top in _platform_imports(ast.parse(path.read_text(encoding="utf-8"))):
            if top:
                offenders.append(f"{path.name}:{line}")
    assert offenders == [], f"core modules import ams.platform at import time: {offenders}"


def test_only_the_cli_reaches_into_the_platform_at_all() -> None:
    users = sorted(
        stem
        for stem in CORE_MODULES
        if _platform_imports(ast.parse((SRC / "ams" / f"{stem}.py").read_text("utf-8")))
    )
    assert users == ["cli"], users


def _run_without_platform(code: str) -> subprocess.CompletedProcess[str]:
    """Run ``code`` in a fresh interpreter where ``import ams.platform`` fails,
    exactly as it would with the platform/ directory deleted."""
    blocker = (
        "import sys, importlib.abc\n"
        "class _NoPlatform(importlib.abc.MetaPathFinder):\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name == 'ams.platform' or name.startswith('ams.platform.'):\n"
        "            raise ModuleNotFoundError(f'No module named {name!r}', name=name)\n"
        "        return None\n"
        "sys.meta_path.insert(0, _NoPlatform())\n"
    )
    return subprocess.run(
        [sys.executable, "-c", blocker + code],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PYTHONPATH": str(SRC), "PATH": "/usr/bin:/bin"},
        check=False,
    )


def test_every_core_module_imports_without_the_platform() -> None:
    mods = json.dumps([f"ams.{m}" for m in CORE_MODULES])
    res = _run_without_platform(
        f"import importlib\nfor m in {mods}:\n    importlib.import_module(m)\n"
        "assert not [m for m in sys.modules if m.startswith('ams.platform')]\n"
        "print('ok')\n"
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "ok"


def test_the_cli_works_without_the_platform() -> None:
    res = _run_without_platform(
        "from ams.cli import build_parser, main\n"
        "p = build_parser()\n"
        "choices = p._subparsers._group_actions[0].choices\n"
        "assert 'platform' not in choices and 'run' in choices, sorted(choices)\n"
        f"sys.exit(main(['validate', {str(EXAMPLE)!r}]))\n"
    )
    assert res.returncode == 0, res.stderr


def test_policy_platform_without_the_platform_is_a_clear_error(tmp_path: Path) -> None:
    res = _run_without_platform(
        "from ams.cli import main\n"
        f"sys.exit(main(['run', '--no-isolation', '--policy', 'platform', "
        f"'--state-dir', {str(tmp_path / 'state')!r}]))\n"
    )
    assert res.returncode == 2, (res.returncode, res.stderr)
    assert "ams.platform is not installed" in res.stderr
