"""PORT_NAME_RE widening: pool member ids can be up to 32 chars (SERVICE_ID_RE),
so a port name derived from a member id must also allow up to 32 chars.

See .claude/state/PLAN-pool.md §3.3 and §5.1.
"""

from pathlib import Path

import pytest

from ams.schema import DeclError, expand_ports, loads

GOLDEN_DIR = Path(__file__).parent / "golden" / "platform"
GOLDEN_TOMLS = sorted(GOLDEN_DIR.glob("*.toml"))


def _decl(port_name: str) -> str:
    return f'id = "ok"\n[start]\nargv = ["x"]\n[ports]\n{port_name} = 0\n'


def test_32_char_port_name_validates():
    name = "a" + "b" * 31
    assert len(name) == 32
    d = loads(_decl(name))
    assert name in d.ports


def test_33_char_port_name_rejected():
    name = "a" + "b" * 32
    assert len(name) == 33
    with pytest.raises(DeclError, match="ports."):
        loads(_decl(name))


def test_port_name_with_hyphen_rejected():
    with pytest.raises(DeclError, match="ports."):
        loads(_decl("bad-name"))


def test_32_char_port_name_expands_in_argv():
    name = "a" + "b" * 31
    text = f'id = "ok"\n[start]\nargv = ["x", "${{PORT_{name}}}"]\n[ports]\n{name} = 0\n'
    d = loads(text)
    expanded = expand_ports(d.start.argv[1], {name: 8123})
    assert expanded == "8123"


@pytest.mark.parametrize("path", GOLDEN_TOMLS, ids=lambda p: p.name)
def test_golden_platform_declarations_still_load(path: Path):
    loads(path.read_text())
