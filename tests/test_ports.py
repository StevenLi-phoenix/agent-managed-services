import json
import socket
from pathlib import Path

import pytest

from ams.ports import PortAllocator, PortConflict, is_port_free
from ams.state import StateCorrupt


@pytest.fixture
def occupied_socket():
    """A real listening socket on 127.0.0.1 to occupy a port for the duration of a test."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    yield port
    s.close()


def test_is_port_free_true_and_false(occupied_socket: int):
    assert is_port_free(occupied_socket) is False
    # something almost certainly free right after closing an ephemeral bind
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    free_port = s.getsockname()[1]
    s.close()
    assert is_port_free(free_port) is True


def test_auto_allocation_skips_assigned_and_bound_ports(tmp_path: Path, occupied_socket: int):
    lo = occupied_socket  # narrow custom range starting at a port we know is bound
    alloc = PortAllocator(tmp_path / "ports.json", range=(lo, lo + 5))
    result = alloc.allocate("svc-a", {"main": 0})
    assert result["main"] != occupied_socket
    assert lo <= result["main"] <= lo + 5

    # a second service must not get the same port
    result2 = alloc.allocate("svc-b", {"main": 0})
    assert result2["main"] != result["main"]
    assert result2["main"] != occupied_socket


def test_fixed_conflict_between_two_services_raises(tmp_path: Path):
    alloc = PortAllocator(tmp_path / "ports.json", bind_check=False)
    alloc.allocate("svc-a", {"main": 9000})
    with pytest.raises(PortConflict):
        alloc.allocate("svc-b", {"main": 9000})


def test_same_service_refixed_request_is_idempotent(tmp_path: Path):
    alloc = PortAllocator(tmp_path / "ports.json", bind_check=False)
    r1 = alloc.allocate("svc-a", {"main": 9000})
    r2 = alloc.allocate("svc-a", {"main": 9000})
    assert r1 == r2 == {"main": 9000}


def test_same_service_reauto_request_is_idempotent(tmp_path: Path):
    alloc = PortAllocator(tmp_path / "ports.json", range=(20000, 20010))
    r1 = alloc.allocate("svc-a", {"main": 0})
    r2 = alloc.allocate("svc-a", {"main": 0})
    assert r1 == r2


def test_persistence_round_trip(tmp_path: Path):
    state_path = tmp_path / "ports.json"
    alloc1 = PortAllocator(state_path, bind_check=False)
    r1 = alloc1.allocate("svc-a", {"main": 9000, "admin": 9001})

    alloc2 = PortAllocator(state_path, bind_check=False)
    assert alloc2.get("svc-a") == r1


def test_changing_fixed_request_reassigns(tmp_path: Path):
    alloc = PortAllocator(tmp_path / "ports.json", bind_check=False)
    alloc.allocate("svc-a", {"main": 9000})
    result = alloc.allocate("svc-a", {"main": 9001})
    assert result == {"main": 9001}
    # old port is free again for another service
    alloc.allocate("svc-b", {"main": 9000})


def test_dropping_name_removes_it(tmp_path: Path):
    alloc = PortAllocator(tmp_path / "ports.json", bind_check=False)
    alloc.allocate("svc-a", {"main": 9000, "admin": 9001})
    result = alloc.allocate("svc-a", {"main": 9000})
    assert result == {"main": 9000}
    assert alloc.get("svc-a") == {"main": 9000}
    # admin's old port is free again for another service
    alloc.allocate("svc-b", {"other": 9001})


def test_out_of_range_raises_value_error(tmp_path: Path):
    alloc = PortAllocator(tmp_path / "ports.json", bind_check=False)
    with pytest.raises(ValueError):
        alloc.allocate("svc-a", {"main": 80})
    with pytest.raises(ValueError):
        alloc.allocate("svc-a", {"main": 70000})


def test_bind_check_false_allows_currently_bound_port(tmp_path: Path, occupied_socket: int):
    alloc = PortAllocator(tmp_path / "ports.json", bind_check=False)
    result = alloc.allocate("svc-a", {"main": occupied_socket})
    assert result == {"main": occupied_socket}


def test_bind_check_true_rejects_currently_bound_fixed_port(tmp_path: Path, occupied_socket: int):
    alloc = PortAllocator(tmp_path / "ports.json", bind_check=True)
    with pytest.raises(PortConflict):
        alloc.allocate("svc-a", {"main": occupied_socket})


def test_release(tmp_path: Path):
    alloc = PortAllocator(tmp_path / "ports.json", bind_check=False)
    alloc.allocate("svc-a", {"main": 9000})
    alloc.release("svc-a")
    assert alloc.get("svc-a") == {}
    alloc.release("svc-a")  # idempotent, must not raise


def test_assignments(tmp_path: Path):
    alloc = PortAllocator(tmp_path / "ports.json", bind_check=False)
    alloc.allocate("svc-a", {"main": 9000})
    alloc.allocate("svc-b", {"main": 9001})
    assert dict(alloc.assignments()) == {"svc-a": {"main": 9000}, "svc-b": {"main": 9001}}


# --------------------------------------------------------------------------- corruption


def test_is_port_free_checks_wildcard_too():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", 0))
    s.listen(1)
    port = s.getsockname()[1]
    try:
        assert is_port_free(port) is False
    finally:
        s.close()


def test_load_corrupt_json_raises_state_corrupt(tmp_path: Path):
    state_path = tmp_path / "ports.json"
    state_path.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(StateCorrupt, match="corrupt JSON"):
        PortAllocator(state_path, bind_check=False)


def test_load_wrong_top_level_type_raises_state_corrupt(tmp_path: Path):
    state_path = tmp_path / "ports.json"
    state_path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    with pytest.raises(StateCorrupt):
        PortAllocator(state_path, bind_check=False)


def test_load_non_integer_port_raises_state_corrupt(tmp_path: Path):
    state_path = tmp_path / "ports.json"
    state_path.write_text(
        json.dumps({"version": 1, "ports": {"svc-a": {"main": "9000"}}}), encoding="utf-8"
    )
    with pytest.raises(StateCorrupt, match="not an integer"):
        PortAllocator(state_path, bind_check=False)


def test_load_negative_port_raises_state_corrupt(tmp_path: Path):
    state_path = tmp_path / "ports.json"
    state_path.write_text(
        json.dumps({"version": 1, "ports": {"svc-a": {"main": -1}}}), encoding="utf-8"
    )
    with pytest.raises(StateCorrupt, match=r"outside \["):
        PortAllocator(state_path, bind_check=False)


def test_load_out_of_range_port_raises_state_corrupt(tmp_path: Path):
    state_path = tmp_path / "ports.json"
    state_path.write_text(
        json.dumps({"version": 1, "ports": {"svc-a": {"main": 80}}}), encoding="utf-8"
    )
    with pytest.raises(StateCorrupt, match=r"outside \["):
        PortAllocator(state_path, bind_check=False)
