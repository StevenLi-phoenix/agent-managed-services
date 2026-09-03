"""Tests for ams.platform.registryclient against a stdlib http.server fake."""

from __future__ import annotations

import json
import logging
import socket
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from ams.platform.registryclient import (
    RETRY_DELAYS,
    RegistryClient,
    RegistryError,
    from_sidecar,
)

ADMIN_TOKEN = "top-secret-admin-token-do-not-log"  # noqa: S105 - test fixture value
SERVICE_SECRET = "top-secret-service-secret-do-not-log"  # noqa: S105


# --------------------------------------------------------------- fake server


class _ScriptedRegistry(HTTPServer):
    """Records every request and replays a scripted list of responses in order.

    ``script`` is a list of ``(status, json_body)``, popped one per request.
    Once exhausted, ``default_response`` is served for any further request.
    """

    def __init__(self, script: list[tuple[int, dict]] | None = None) -> None:
        super().__init__(("127.0.0.1", 0), _ScriptedHandler)
        self.script: list[tuple[int, dict]] = list(script or [])
        self.default_response: tuple[int, dict] = (200, {"ok": True})
        self.requests: list[dict] = []
        self.lock = threading.Lock()


class _ScriptedHandler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # keep test output clean
        pass

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw_body = self.rfile.read(length) if length else b""
        server: _ScriptedRegistry = self.server  # type: ignore[assignment]
        with server.lock:
            server.requests.append(
                {
                    "method": self.command,
                    "path": self.path,
                    "headers": {k: v for k, v in self.headers.items()},
                    "body": raw_body,
                }
            )
            status, payload = server.script.pop(0) if server.script else server.default_response
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = _handle
    do_POST = _handle


def _start(script: list[tuple[int, dict]] | None = None):
    server = _ScriptedRegistry(script)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    return server, thread, base_url


def _stop(server: HTTPServer, thread: threading.Thread) -> None:
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


@pytest.fixture
def registry():
    server, thread, base_url = _start()
    try:
        yield server, base_url
    finally:
        _stop(server, thread)


class FakeClock:
    """Deterministic (sleep, now) pair: sleep() advances the same clock now() reads."""

    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds

    def now(self) -> float:
        return self.t


def recording_sleep() -> tuple[Callable[[float], None], list[float]]:
    calls: list[float] = []
    return calls.append, calls


# ---------------------------------------------------------------- base_url


def test_non_loopback_base_url_rejected():
    with pytest.raises(RegistryError, match="loopback"):
        RegistryClient("http://example.com:8080", ADMIN_TOKEN)


def test_https_scheme_rejected_even_on_loopback():
    with pytest.raises(RegistryError, match="loopback"):
        RegistryClient("https://127.0.0.1:8080", ADMIN_TOKEN)


def test_localhost_and_127_both_accepted(registry):
    _server, base_url = registry
    port = base_url.rsplit(":", 1)[1]
    RegistryClient(f"http://127.0.0.1:{port}", ADMIN_TOKEN)
    RegistryClient(f"http://localhost:{port}", ADMIN_TOKEN)


# ------------------------------------------------------------ create_identity


def test_create_identity_201_created_true():
    server, thread, base_url = _start([(201, {"id": "files"})])
    try:
        sleep_fn, sleeps = recording_sleep()
        client = RegistryClient(base_url, ADMIN_TOKEN, sleep_fn=sleep_fn)
        result = client.create_identity(
            "files",
            SERVICE_SECRET,
            audience="files",
            display_name="Files Service",
            owner="steven",
            capabilities=["filestorage"],
            health_path="/health",
        )
        assert result.created is True
        assert sleeps == []
        assert len(server.requests) == 1
        req = server.requests[0]
        assert req["method"] == "POST"
        assert req["path"] == "/api/services"
        assert req["headers"]["X-Service-Secret"] == SERVICE_SECRET
        assert req["headers"]["X-Admin-Token"] == ADMIN_TOKEN
        body = json.loads(req["body"])
        assert body == {
            "id": "files",
            "audience": "files",
            "display_name": "Files Service",
            "owner": "steven",
            "capabilities": ["filestorage"],
        }
    finally:
        _stop(server, thread)


def test_create_identity_409_is_created_false_no_retry():
    server, thread, base_url = _start([(409, {"detail": "already exists"})])
    try:
        sleep_fn, sleeps = recording_sleep()
        client = RegistryClient(base_url, ADMIN_TOKEN, sleep_fn=sleep_fn)
        result = client.create_identity("files", SERVICE_SECRET, audience="files")
        assert result.created is False
        assert len(server.requests) == 1
        assert sleeps == []
    finally:
        _stop(server, thread)


def test_create_identity_retries_502_then_succeeds():
    server, thread, base_url = _start([(502, {}), (502, {}), (201, {"id": "files"})])
    try:
        sleep_fn, sleeps = recording_sleep()
        client = RegistryClient(base_url, ADMIN_TOKEN, sleep_fn=sleep_fn)
        result = client.create_identity("files", SERVICE_SECRET, audience="files")
        assert result.created is True
        assert len(server.requests) == 3
        assert sleeps == [RETRY_DELAYS[0], RETRY_DELAYS[1]]
    finally:
        _stop(server, thread)


def test_create_identity_403_raises_immediately_no_retry():
    server, thread, base_url = _start([(403, {"detail": "bad admin token"})])
    try:
        sleep_fn, sleeps = recording_sleep()
        client = RegistryClient(base_url, ADMIN_TOKEN, sleep_fn=sleep_fn)
        with pytest.raises(RegistryError, match="403"):
            client.create_identity("files", SERVICE_SECRET, audience="files")
        assert len(server.requests) == 1
        assert sleeps == []
    finally:
        _stop(server, thread)


def test_create_identity_exhausted_backoff_names_last_status():
    server, thread, base_url = _start()
    server.default_response = (503, {"detail": "restarting"})
    try:
        sleep_fn, sleeps = recording_sleep()
        client = RegistryClient(base_url, ADMIN_TOKEN, sleep_fn=sleep_fn)
        with pytest.raises(RegistryError, match="503") as exc_info:
            client.create_identity("files", SERVICE_SECRET, audience="files")
        assert "after" in str(exc_info.value)
        assert len(server.requests) == len(RETRY_DELAYS) + 1
        assert sleeps == list(RETRY_DELAYS)
    finally:
        _stop(server, thread)


def test_create_identity_network_error_treated_like_503():
    # Bind a port and close it immediately: nothing is listening there, so
    # every connection attempt raises a transport error (ECONNREFUSED).
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    sleep_fn, sleeps = recording_sleep()
    client = RegistryClient(
        f"http://127.0.0.1:{port}", ADMIN_TOKEN, sleep_fn=sleep_fn, timeout_s=1.0
    )
    with pytest.raises(RegistryError, match="transport error"):
        client.create_identity("files", SERVICE_SECRET, audience="files")
    assert sleeps == list(RETRY_DELAYS)


def test_create_identity_rejects_invalid_health_path():
    server, thread, base_url = _start()
    try:
        client = RegistryClient(base_url, ADMIN_TOKEN)
        with pytest.raises(RegistryError, match="health_path"):
            client.create_identity("files", SERVICE_SECRET, audience="files", health_path="health")
        assert server.requests == []
    finally:
        _stop(server, thread)


def test_create_identity_rejects_invalid_service_id():
    server, thread, base_url = _start()
    try:
        client = RegistryClient(base_url, ADMIN_TOKEN)
        with pytest.raises(RegistryError, match="service_id"):
            client.create_identity("Not-Valid!", SERVICE_SECRET, audience="files")
        assert server.requests == []
    finally:
        _stop(server, thread)


# ------------------------------------------------------------------- acl


def test_upsert_acl_body_matches_deployer_shape_one_post_per_rule(registry):
    server, base_url = registry
    client = RegistryClient(base_url, ADMIN_TOKEN)
    client.upsert_acl(
        "files",
        [
            {"action": "read", "principal": "anon"},
            {"action": "write", "principal": "anon", "effect": "deny"},
        ],
    )
    assert len(server.requests) == 2
    first, second = server.requests
    assert first["path"] == "/api/acl"
    assert first["method"] == "POST"
    assert "X-Service-Secret" not in first["headers"]
    assert first["headers"]["X-Admin-Token"] == ADMIN_TOKEN
    assert json.loads(first["body"]) == {
        "service_id": "files",
        "action": "read",
        "principal": "anon",
        "effect": "allow",
    }
    assert json.loads(second["body"]) == {
        "service_id": "files",
        "action": "write",
        "principal": "anon",
        "effect": "deny",
    }


def test_upsert_acl_retries_and_fails_like_create_identity():
    server, thread, base_url = _start([(502, {}), (200, {"ok": True})])
    try:
        sleep_fn, sleeps = recording_sleep()
        client = RegistryClient(base_url, ADMIN_TOKEN, sleep_fn=sleep_fn)
        client.upsert_acl("files", [{"action": "read", "principal": "anon"}])
        assert len(server.requests) == 2
        assert sleeps == [RETRY_DELAYS[0]]
    finally:
        _stop(server, thread)


def test_upsert_acl_403_raises_immediately():
    server, thread, base_url = _start([(403, {"detail": "nope"})])
    try:
        client = RegistryClient(base_url, ADMIN_TOKEN)
        with pytest.raises(RegistryError, match="403"):
            client.upsert_acl("files", [{"action": "read", "principal": "anon"}])
    finally:
        _stop(server, thread)


# --------------------------------------------------------------- wait_healthy


def test_wait_healthy_true_on_first_200(registry):
    _server, base_url = registry
    client = RegistryClient(base_url, ADMIN_TOKEN)
    assert client.wait_healthy(f"{base_url}/health", deadline_s=5.0, interval_s=0.01) is True


def test_wait_healthy_false_after_deadline():
    server, thread, base_url = _start()
    server.default_response = (503, {"status": "starting"})
    try:
        clock = FakeClock()
        client = RegistryClient(base_url, ADMIN_TOKEN, sleep_fn=clock.sleep, now_fn=clock.now)
        assert client.wait_healthy(f"{base_url}/health", deadline_s=3.0, interval_s=1.0) is False
        assert clock.t >= 3.0
    finally:
        _stop(server, thread)


def test_wait_healthy_transport_error_counts_as_not_yet():
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    clock = FakeClock()
    client = RegistryClient(
        f"http://127.0.0.1:{port}",
        ADMIN_TOKEN,
        sleep_fn=clock.sleep,
        now_fn=clock.now,
        timeout_s=1.0,
    )
    url = f"http://127.0.0.1:{port}/health"
    assert client.wait_healthy(url, deadline_s=2.0, interval_s=1.0) is False


# ------------------------------------------------------------------ secrecy


def test_secret_and_admin_token_never_appear_in_logs_repr_or_exceptions(caplog):
    server, thread, base_url = _start([(201, {}), (403, {"detail": "bad"})])
    try:
        caplog.set_level(logging.DEBUG, logger="ams.platform.registryclient")
        client = RegistryClient(base_url, ADMIN_TOKEN)

        assert ADMIN_TOKEN not in repr(client)
        assert ADMIN_TOKEN not in str(client)

        client.create_identity("files", SERVICE_SECRET, audience="files")
        with pytest.raises(RegistryError) as exc_info:
            client.create_identity("files", SERVICE_SECRET, audience="files")

        for record in caplog.records:
            text = record.getMessage()
            assert ADMIN_TOKEN not in text
            assert SERVICE_SECRET not in text
        assert ADMIN_TOKEN not in str(exc_info.value)
        assert SERVICE_SECRET not in str(exc_info.value)
    finally:
        _stop(server, thread)


# ---------------------------------------------------------------- from_sidecar


def test_from_sidecar_maps_the_documented_shape():
    reg = {
        "version": 1,
        "id": "files",
        "audience": "files",
        "display_name": "Files Service",
        "owner": "steven",
        "capabilities": ["filestorage"],
        "health_path": "/health",
        "acl": [
            {"action": "read", "principal": "anon", "effect": "allow"},
            {"action": "write", "principal": "anon", "effect": "allow"},
        ],
    }
    service_id, kwargs, rules = from_sidecar(reg)
    assert service_id == "files"
    assert kwargs == {
        "audience": "files",
        "display_name": "Files Service",
        "owner": "steven",
        "capabilities": ["filestorage"],
        "health_path": "/health",
    }
    assert rules == reg["acl"]


def test_from_sidecar_fills_defaults_for_optional_fields():
    reg = {"version": 1, "id": "svc", "audience": "svc"}
    service_id, kwargs, rules = from_sidecar(reg)
    assert service_id == "svc"
    assert kwargs["display_name"] is None
    assert kwargs["owner"] is None
    assert kwargs["capabilities"] == []
    assert kwargs["health_path"] == "/health"
    assert rules == []


def test_from_sidecar_rejects_unknown_version():
    with pytest.raises(RegistryError, match="version"):
        from_sidecar({"version": 2, "id": "svc", "audience": "svc"})


def test_from_sidecar_output_feeds_create_identity_and_upsert_acl(registry):
    _server, base_url = registry
    reg = {
        "version": 1,
        "id": "files",
        "audience": "files",
        "capabilities": ["filestorage"],
        "acl": [{"action": "read", "principal": "anon"}],
    }
    service_id, kwargs, rules = from_sidecar(reg)
    client = RegistryClient(base_url, ADMIN_TOKEN)
    result = client.create_identity(service_id, SERVICE_SECRET, **kwargs)
    assert result.created is True
    client.upsert_acl(service_id, rules)
