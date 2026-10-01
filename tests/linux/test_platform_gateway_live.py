"""Linux-only checks that the rendered gateway config is real Caddy config.

The portable tests pin the *bytes* (tests/test_platform_gateway_core.py). Only Caddy
itself can say whether those bytes are a config it accepts, and that is the
failure mode a golden file cannot catch: a header block that is byte-perfect and
syntactically invalid still takes the whole gateway down at restart.

Run via ``scripts/linux-test.sh ams-gw tests/linux/test_platform_gateway_live.py``.
Skipped unless the pinned binary from ``deploy/install-host.sh`` is present.
"""

from __future__ import annotations

import http.client
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

from ams.platform import gateway
from ams.platform.gateway import CoreSite, GatewayConfig, render_core
from ams.state import StateDir

pytestmark = pytest.mark.linux

GOLDEN_DIR = Path(__file__).parents[1] / "golden" / "gateway" / "core"
SCENARIOS = ["basic", "empty", "logdir"]
STORE = Path(os.environ.get("AMS_STORE_DIR", "/home/harness/store"))
CADDY = STORE / "bin" / "caddy"

needs_caddy = pytest.mark.skipif(
    not CADDY.is_file(), reason=f"no pinned caddy binary at {CADDY} (deploy/install-host.sh)"
)


def validate(config: Path) -> subprocess.CompletedProcess[str]:
    # --adapter is required even for a file literally named "Caddyfile": without
    # it a rename would silently switch Caddy into JSON-config mode.
    return subprocess.run(
        [str(CADDY), "validate", "--adapter", "caddyfile", "--config", str(config)],
        capture_output=True,
        text=True,
        timeout=60,
    )


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@needs_caddy
@pytest.mark.parametrize("scenario", SCENARIOS)
def test_golden_config_is_accepted_by_caddy(scenario: str, tmp_path: Path) -> None:
    """Every golden render must survive `caddy validate` exit 0."""
    work = tmp_path / scenario
    shutil.copytree(GOLDEN_DIR / scenario, work)
    # The goldens name machine-independent stand-ins; the log directory is the
    # one that must exist, because Caddy opens the writer while provisioning.
    (tmp_path / "srv-logs").mkdir()
    for path in work.rglob("*"):
        if path.is_file():
            path.write_text(path.read_text().replace("/srv/ams/logs", str(tmp_path / "srv-logs")))
    result = validate(work / "Caddyfile")
    assert result.returncode == 0, f"caddy validate failed:\n{result.stderr}"


@needs_caddy
@pytest.mark.parametrize("scenario", SCENARIOS)
def test_rendered_config_is_already_in_caddy_fmt_canonical_form(
    scenario: str, tmp_path: Path
) -> None:
    """`caddy validate` only *warns* about non-canonical formatting, so nothing
    else would catch the drift. Keeping the renderer's output identical to
    `caddy fmt` means an operator running `caddy fmt --overwrite` on the live
    config cannot make it differ from what the next render produces."""
    work = tmp_path / scenario
    shutil.copytree(GOLDEN_DIR / scenario, work)
    for path in sorted(work.rglob("*")):
        if not path.is_file():
            continue
        result = subprocess.run(
            [str(CADDY), "fmt", str(path)], capture_output=True, text=True, timeout=60
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == path.read_text(), f"{scenario}/{path.name} is not caddy-fmt clean"


@needs_caddy
def test_a_freshly_written_config_validates(tmp_path: Path) -> None:
    """`render_core()` + `write()` end to end, not a checked-in file."""
    state = StateDir(tmp_path / "state")
    sites = [CoreSite("api.lishuyu.app", 18080), CoreSite("pages.shuyuli.com", 18081)]
    cfg = GatewayConfig(listen_port=free_port())
    changed = gateway.write(state, render_core(sites, cfg))
    assert changed
    result = validate(gateway.caddyfile_path(state))
    assert result.returncode == 0, f"caddy validate failed:\n{result.stderr}"


@needs_caddy
def test_running_caddy_answers_the_harness_health_probe(tmp_path: Path) -> None:
    """The health path in `caddy_declaration()` has to be a route Caddy actually
    serves, or the harness marks a working gateway unhealthy forever; and a
    site must actually reach its loopback port."""
    state = StateDir(tmp_path / "state")
    port = free_port()
    backend = free_port()
    cfg = GatewayConfig(listen_port=port)
    gateway.write(state, render_core([CoreSite("api.example.test", backend)], cfg))
    origin = subprocess.Popen(
        ["python3", "-m", "http.server", str(backend), "--bind", "127.0.0.1"],
        cwd=tmp_path,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    env = dict(os.environ, XDG_CONFIG_HOME=str(tmp_path / "cfg"), XDG_DATA_HOME=str(tmp_path / "d"))
    proc = subprocess.Popen(
        [
            str(CADDY),
            "run",
            "--config",
            str(gateway.caddyfile_path(state)),
            "--adapter",
            "caddyfile",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    try:
        status = None
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                pytest.fail(f"caddy exited early ({proc.returncode}):\n{proc.communicate()[0]}")
            try:
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                conn.request("GET", gateway.HEALTH_PATH)
                resp = conn.getresponse()
                status, body = resp.status, resp.read().decode()
                conn.close()
                break
            except OSError:
                time.sleep(0.2)
        assert status == 200, f"health probe never returned 200 (got {status})"
        assert body == "ok"

        # The 404 catch-all, so a request for an unknown host is distinguishable
        # from a dead gateway.
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request("GET", "/nope")
        resp = conn.getresponse()
        assert resp.status == 404
        assert b'"error":"not_found"' in resp.read()
        conn.close()

        # ... and so is every Host that has no [[site]] (not Caddy's empty 200).
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request("GET", "/", headers={"Host": "unknown.example.test"})
        resp = conn.getresponse()
        assert resp.status == 404
        assert b'"error":"not_found"' in resp.read()
        conn.close()

        # A [[site]] host is reverse-proxied to its loopback port.
        proxied = None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            conn.request("GET", "/", headers={"Host": "api.example.test"})
            resp = conn.getresponse()
            proxied = resp.status
            resp.read()
            conn.close()
            if proxied == 200:
                break
            time.sleep(0.2)  # the origin may still be binding
        assert proxied == 200, f"site api.example.test was not proxied (got {proxied})"
    finally:
        origin.terminate()
        origin.wait(timeout=10)
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            proc.kill()
            proc.wait(timeout=10)
