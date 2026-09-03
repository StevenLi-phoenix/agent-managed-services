import pytest

from ams import schema
from ams.schema import DeclError, ServiceDecl, StartSpec, expand_ports, loads, port_refs

MINIMAL = """
id = "demo"
[start]
argv = ["sleep", "1"]
"""

FULL = """
id = "web-app"
name = "Web App"
[start]
argv = ["python", "-m", "http.server", "${PORT_main}"]
workdir = "app"
[env]
DATABASE_URL = "sqlite:///db.sqlite"
ADMIN_PORT = "${PORT_admin}"
[ports]
main = 0
admin = 8081
[runtime]
kind = "venv"
python = "3.12"
requirements = "requirements.txt"
packages = ["uvicorn"]
[health]
kind = "http"
port = "main"
path = "/healthz"
interval_s = 5
timeout_s = 2
start_period_s = 10
[stop]
signal = "SIGINT"
timeout_s = 3
[limits]
memory_max = "256M"
cpu_max = "50%"
pids_max = 64
[restart]
policy = "always"
max_retries = 3
backoff_s = 2
backoff_max_s = 30
"""


def test_minimal_defaults():
    d = loads(MINIMAL)
    assert d.id == "demo"
    assert d.start.argv == ("sleep", "1")
    assert d.start.workdir == "."
    assert d.runtime.kind == "none"
    assert d.health.kind == "none"
    assert d.stop.signal == "SIGTERM" and d.stop.signum == 15
    assert d.limits.memory_max_bytes is None and d.limits.cpu_max_value is None
    assert d.restart.policy == "on-failure"
    assert dict(d.env) == {} and dict(d.ports) == {}


def test_full_round_trip():
    d = loads(FULL)
    assert d.name == "Web App"
    assert d.start.workdir == "app"
    assert dict(d.ports) == {"main": 0, "admin": 8081}
    assert d.runtime.packages == ("uvicorn",)
    assert d.health.kind == "http" and d.health.port == "main"
    assert d.stop.signum == 2
    assert d.limits.memory_max_bytes == 256 * 1024 * 1024
    assert d.limits.cpu_max_value == "50000 100000"
    assert d.limits.pids_max == 64
    assert d.restart.policy == "always" and d.restart.max_retries == 3
    assert port_refs(d) == {"main", "admin"}


def test_frozen_and_immutable_mappings():
    d = loads(FULL)
    with pytest.raises(AttributeError):
        d.id = "x"  # type: ignore[misc]
    with pytest.raises(TypeError):
        d.env["X"] = "y"  # type: ignore[index]


@pytest.mark.parametrize(
    "toml, needle",
    [
        ('id = "Bad_ID"\n[start]\nargv=["x"]', "id:"),
        ('id = "ok"\n', "start:"),
        ('id = "ok"\n[start]\nargv=[]', "start.argv"),
        ('id = "ok"\n[start]\nargv=["x"]\nworkdir="../escape"', "start.workdir"),
        ('id = "ok"\n[start]\nargv=["x"]\nbogus=1', "unknown keys"),
        ('id = "ok"\nbogus=1\n[start]\nargv=["x"]', "unknown top-level"),
        ('id = "ok"\n[start]\nargv=["x"]\n[ports]\nmain=80', "ports.main"),
        ('id = "ok"\n[start]\nargv=["x"]\n[ports]\nmain=70000', "ports.main"),
        ('id = "ok"\n[start]\nargv=["x"]\n[ports]\n"Bad Name"=0', "ports."),
        ('id = "ok"\n[start]\nargv=["x", "${PORT_main}"]', "PORT_main"),
        ('id = "ok"\n[start]\nargv=["x"]\n[env]\nP="${PORT_nope}"', "PORT_nope"),
        ('id = "ok"\n[start]\nargv=["x"]\n[env]\n"1BAD"="v"', "env.1BAD"),
        ('id = "ok"\n[start]\nargv=["x"]\n[env]\nPATH="/x"', "env.PATH"),
        ('id = "ok"\n[start]\nargv=["x"]\n[env]\nAMS_SERVICE_ID="y"', "env.AMS_SERVICE_ID"),
        ('id = "ok"\n[start]\nargv=["x"]\n[ports]\nmain=0\n[env]\nPORT_main="1"', "env.PORT_main"),
        ('id = "ok"\n[start]\nargv=["x"]\n[runtime]\nkind="docker"', "runtime.kind"),
        ('id = "ok"\n[start]\nargv=["x"]\n[runtime]\npackages=["a"]', "runtime"),
        ('id = "ok"\n[start]\nargv=["x"]\n[runtime]\nkind="nix"\npackages=["a"]', "runtime"),
        (
            'id = "ok"\n[start]\nargv=["x"]\n[runtime]\nkind="venv"\npython="three"',
            "runtime.python",
        ),
        ('id = "ok"\n[start]\nargv=["x"]\n[runtime]\nkind="bun"\npython="3.12"', "runtime.python"),
        ('id = "ok"\n[start]\nargv=["x"]\n[runtime]\nkind="uv"\nnode="22"', "runtime.node"),
        ('id = "ok"\n[start]\nargv=["x"]\n[runtime]\nkind="pnpm"\nnode="v22"', "runtime.node"),
        (
            'id = "ok"\n[start]\nargv=["x"]\n[runtime]\nkind="pnpm"\nrequirements="r.txt"',
            "runtime.requirements",
        ),
        ('id = "ok"\n[start]\nargv=["x"]\n[runtime]\nkind="venv"\nsync=true', "runtime.sync"),
        (
            'id = "ok"\n[start]\nargv=["x"]\n[runtime]\nkind="uv"\nsync=true\nrequirements="r"',
            "runtime.sync",
        ),
        ('id = "ok"\n[start]\nargv=["x"]\n[runtime]\nkind="uv"\nsync="yes"', "runtime.sync"),
        ('id = "ok"\n[start]\nargv=["x"]\n[health]\nkind="tcp"', "health.port"),
        (
            'id = "ok"\n[start]\nargv=["x"]\n[health]\nkind="tcp"\nport="main"',
            "health.port",
        ),
        ('id = "ok"\n[start]\nargv=["x"]\n[health]\nkind="log"', "health.pattern"),
        (
            'id = "ok"\n[start]\nargv=["x"]\n[health]\nkind="log"\npattern="("',
            "health.pattern",
        ),
        (
            'id = "ok"\n[start]\nargv=["x"]\n[health]\nkind="none"\ninterval_s=-1',
            "health.interval_s",
        ),
        ('id = "ok"\n[start]\nargv=["x"]\n[stop]\nsignal="SIGNOPE"', "stop.signal"),
        ('id = "ok"\n[start]\nargv=["x"]\n[stop]\ntimeout_s=0', "stop.timeout_s"),
        ('id = "ok"\n[start]\nargv=["x"]\n[limits]\nmemory_max="lots"', "limits.memory_max"),
        ('id = "ok"\n[start]\nargv=["x"]\n[limits]\ncpu_max="0.5"', "limits.cpu_max"),
        ('id = "ok"\n[start]\nargv=["x"]\n[limits]\npids_max=0', "limits.pids_max"),
        ('id = "ok"\n[start]\nargv=["x"]\n[limits]\npids_max="64"', "limits.pids_max"),
        ('id = "ok"\n[start]\nargv=["x"]\n[restart]\npolicy="sometimes"', "restart.policy"),
        ('id = "ok"\n[start]\nargv=["x"]\n[restart]\nbackoff_s=10\nbackoff_max_s=1', "restart"),
        ('id = "ok"\n[start]\nargv="not a list"', "start.argv"),
        ("id = = broken", "toml:"),
    ],
)
def test_invalid_declarations(toml, needle):
    with pytest.raises(DeclError) as ei:
        loads(toml)
    assert needle in str(ei.value)


def test_secrets_names_only():
    d = loads('id="s"\nsecrets=["SVC_SECRET","API_KEY"]\n[start]\nargv=["x"]\n[env]\nMODE="prod"')
    assert d.secrets == ("SVC_SECRET", "API_KEY")
    assert loads(MINIMAL).secrets == ()
    for bad, needle in [
        ('secrets=["bad name"]', "secrets"),
        ('secrets=["PATH"]', "reserved"),
        ('secrets=["AMS_X"]', "reserved"),
        ('secrets=["A","A"]', "twice"),
        ('secrets=["A"]\n[env]\nA="v"', "also set in env"),
        ('secrets="A"', "list"),
    ]:
        with pytest.raises(DeclError) as ei:
            loads('id="s"\n' + bad + '\n[start]\nargv=["x"]')
        assert needle in str(ei.value)


def test_uv_sync_mode():
    d = loads(
        'id="s"\n[start]\nargv=["uvicorn"]\nworkdir="repo/svc"\n[runtime]\nkind="uv"\nsync=true'
    )
    assert d.runtime.sync and d.runtime.is_python
    assert loads(FULL).runtime.sync is False


def test_node_runtimes():
    d = loads(
        'id="n"\n[start]\nargv=["node","main.js"]\n[runtime]\nkind="pnpm"\nnode="22"\npackages=["express@4"]'
    )
    assert d.runtime.is_node and not d.runtime.is_python
    assert d.runtime.node == "22" and d.runtime.packages == ("express@4",)
    b = loads('id="b"\n[start]\nargv=["bun","main.ts"]\n[runtime]\nkind="bun"')
    assert b.runtime.is_node and b.runtime.node is None
    assert loads(FULL).runtime.is_python


def test_parse_size_and_percent():
    assert schema.parse_size("4096") == 4096
    assert schema.parse_size("1k") == 1024
    assert schema.parse_size("2G") == 2 << 30
    assert schema.parse_percent("150%") == 150.0
    assert (
        ServiceDecl(
            "c", StartSpec(("x",)), limits=schema.LimitsSpec(cpu_max="150%")
        ).limits.cpu_max_value
        == "150000 100000"
    )
    with pytest.raises(ValueError):
        schema.parse_size("1.5G")
    with pytest.raises(ValueError):
        schema.parse_percent("0%")


def test_expand_ports():
    url = expand_ports("http://127.0.0.1:${PORT_main}/x", {"main": 8000})
    assert url == "http://127.0.0.1:8000/x"
    assert expand_ports("plain", {}) == "plain"
    with pytest.raises(KeyError):
        expand_ports("${PORT_other}", {"main": 1})


def test_load_from_file(tmp_path):
    p = tmp_path / "service.toml"
    p.write_text(MINIMAL)
    assert schema.load(p).id == "demo"


def test_logging_format_defaults_to_auto_and_accepts_the_four_hints():
    assert loads(MINIMAL).logging.format == "auto"
    for fmt in ("auto", "level-prefix", "json", "plain"):
        d = loads(MINIMAL + f'[logging]\nformat = "{fmt}"\n')
        assert d.logging.format == fmt


def test_logging_format_rejects_unknown_values_and_keys():
    with pytest.raises(DeclError) as bad_format:
        loads(MINIMAL + '[logging]\nformat = "syslog"\n')
    assert "logging.format" in str(bad_format.value)
    with pytest.raises(DeclError) as bad_key:
        loads(MINIMAL + "[logging]\nlevel = 3\n")
    assert "logging" in str(bad_key.value)


# ------------------------------------------------------------------ depends_on


def with_depends_on(value: str) -> str:
    """MINIMAL with a top-level ``depends_on`` -- above [start], or TOML nests it."""
    return f'id = "demo"\ndepends_on = {value}\n[start]\nargv = ["sleep", "1"]\n'


def test_depends_on_defaults_to_empty_and_parses_a_list_of_ids():
    assert loads(MINIMAL).depends_on == ()
    assert loads(with_depends_on('["registry", "auth"]')).depends_on == ("registry", "auth")


def test_depends_on_preserves_declaration_order():
    """Not sorted: the order is what a reader (and `waiting_for`) sees."""
    assert loads(with_depends_on('["zzz", "aaa"]')).depends_on == ("zzz", "aaa")


@pytest.mark.parametrize(
    "value,needle",
    [
        ('["Registry"]', "must match"),
        ('["has space"]', "must match"),
        ('[""]', "must match"),
        ('["demo"]', "cannot depend on itself"),
        ('["registry", "registry"]', "listed twice"),
    ],
)
def test_depends_on_rejects_bad_entries(value: str, needle: str):
    with pytest.raises(DeclError) as e:
        loads(with_depends_on(value))
    assert "depends_on" in str(e.value)
    assert needle in str(e.value)


def test_depends_on_must_be_a_list_of_strings():
    for value in ('"registry"', "[1]", "{ a = 1 }"):
        with pytest.raises(DeclError) as e:
            loads(with_depends_on(value))
        assert "depends_on" in str(e.value)


def test_depends_on_is_validated_on_direct_construction_too():
    """The dataclass is the boundary, not `loads` -- bootstrap builds decls directly."""
    with pytest.raises(DeclError):
        ServiceDecl(id="a", start=StartSpec(argv=("x",)), depends_on=("a",))
