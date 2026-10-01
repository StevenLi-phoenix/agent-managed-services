"""PORT_NAME_RE: a port name may be as long as a service id (32 chars), so a
port named after a service always validates. Every shipped example loads."""

from pathlib import Path

import pytest

from ams.schema import DeclError, expand_ports, loads

EXAMPLES = sorted((Path(__file__).parents[1] / "examples").rglob("service.toml"))


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


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.parent.name)
def test_example_declarations_load(path: Path):
    loads(path.read_text())


def test_there_are_examples_to_load():
    assert EXAMPLES, "examples/**/service.toml went missing"
