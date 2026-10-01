"""Live Layer-0 bootstrap: real ``openssl``, real user namespace, real ownership.

The portable suite proves the *decisions* against recorders. This proves the two
things a recorder cannot: that ``openssl`` produces a usable RS256 pair with the
modes we claim, and that a key placed through ``run_admin`` genuinely ends up
owned by the service's host uid and genuinely unreadable by the harness.

Runs only via ``scripts/remote-test.sh ams-boot`` (D6). It writes under
``/home/harness/store/state/`` -- a sibling of the live ``services/`` directory,
never inside it -- and removes everything it made.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import linuxhost
import pytest

from ams.platform import bootstrap as bs
from ams.runtime import RuntimeStore
from ams.state import StateDir
from ams.userns import remove_service_root

pytestmark = pytest.mark.linux

#: First block of the harness' real subuid/subgid range (tests/linux/linuxhost.py).
BLOCK = linuxhost.block(0)
SERVICE_ID = "plat-boot"
LIVE_ROOT = Path(os.environ.get("AMS_LIVE_TEST_ROOT", "/home/harness/store/state"))


@pytest.fixture
def base(tmp_path_factory):
    """A scratch state+store tree beside the live state dir, removed afterwards.

    Not ``tmp_path``: the service root must sit on the same filesystem the real
    harness uses, and the uid block must be one this host actually delegates.
    """
    if not LIVE_ROOT.is_dir():
        pytest.skip(f"{LIVE_ROOT} does not exist on this host")
    root = LIVE_ROOT / f"plat-boot-test-{os.getpid()}"
    root.mkdir(parents=True)
    state = StateDir(root / "state")
    state.ensure()
    store = RuntimeStore(root / "store")
    try:
        yield state, store
    finally:
        service_root = state.service_root(SERVICE_ID)
        if service_root.exists():
            remove_service_root(service_root, BLOCK)
        shutil.rmtree(root, ignore_errors=True)
        assert not root.exists(), f"failed to clean up {root}"


def test_openssl_generates_a_usable_rs256_pair(base):
    _, store = base

    assert bs.ensure_keypair(store) == ["key:private", "key:public"]

    priv, pub = bs.jwt_private_path(store), bs.jwt_public_path(store)
    assert priv.stat().st_mode & 0o7777 == 0o600
    assert pub.stat().st_mode & 0o7777 == 0o644
    assert bs.platform_dir(store).stat().st_mode & 0o7777 == 0o700
    assert priv.read_bytes().startswith(b"-----BEGIN PRIVATE KEY-----")
    assert pub.read_bytes().startswith(b"-----BEGIN PUBLIC KEY-----")
    # The pair must actually match: openssl derives the modulus from each side
    # and the two digests agree only for a real pair.
    mods = [
        subprocess.run(
            [
                "openssl",
                "rsa",
                "-in",
                str(p),
                *(["-pubin"] if p is pub else []),
                "-noout",
                "-modulus",
            ],
            capture_output=True,
            check=True,
        ).stdout
        for p in (priv, pub)
    ]
    assert mods[0] == mods[1]
    assert b"Modulus=" in mods[0]
    # 2048-bit modulus = 512 hex characters.
    assert len(mods[0].strip().split(b"=", 1)[1]) == 512


def test_service_dirs_are_owned_by_the_block(base):
    state, _ = base

    assert bs.ensure_service_dirs(state, SERVICE_ID, BLOCK) is True

    root = state.service_root(SERVICE_ID)
    data, etc = root / "data", root / "etc"
    for path in (root, data, etc):
        assert path.is_dir()
        assert path.stat().st_uid == BLOCK.uid_start, f"{path} is uid {path.stat().st_uid}"
        assert path.stat().st_gid == BLOCK.gid_start
    assert data.stat().st_mode & 0o7777 == 0o750
    assert etc.stat().st_mode & 0o7777 == 0o755

    # Warm path: everything already exists and is owned correctly.
    assert bs.ensure_service_dirs(state, SERVICE_ID, BLOCK) is False


def test_public_key_lands_service_owned_and_world_readable(base):
    state, store = base
    bs.ensure_keypair(store)

    assert bs.place_jwt_key(state, SERVICE_ID, BLOCK, store=store) is True

    dst = state.service_root(SERVICE_ID) / "etc" / "jwt-rs256.pub"
    st = dst.stat()
    assert st.st_uid == BLOCK.uid_start
    assert st.st_gid == BLOCK.gid_start
    assert st.st_mode & 0o7777 == 0o444
    # 0444 means the harness can still read it; only the private key is closed.
    assert dst.read_bytes() == bs.jwt_public_path(store).read_bytes()


def test_private_key_lands_0400_and_the_harness_cannot_read_it(base):
    state, store = base
    bs.ensure_keypair(store)

    assert bs.place_jwt_key(state, SERVICE_ID, BLOCK, store=store, private=True) is True

    dst = state.service_root(SERVICE_ID) / "etc" / "jwt-rs256.pem"
    st = dst.stat()
    assert st.st_uid == BLOCK.uid_start
    assert st.st_mode & 0o7777 == 0o400
    # Owner-only, and the owner is a uid the harness does not hold: this is the
    # whole point of a per-service copy instead of production's shared group.
    assert os.getuid() != BLOCK.uid_start
    with pytest.raises(PermissionError):
        dst.read_bytes()


def test_second_placement_is_a_no_op(base):
    state, store = base
    bs.ensure_keypair(store)
    bs.place_jwt_key(state, SERVICE_ID, BLOCK, store=store)
    dst = state.service_root(SERVICE_ID) / "etc" / "jwt-rs256.pub"
    before = dst.stat()

    assert bs.place_jwt_key(state, SERVICE_ID, BLOCK, store=store) is False

    after = dst.stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


def test_full_bootstrap_then_key_placement(base):
    """The order T3.2 will use: bootstrap, then place keys once a block exists."""
    state, store = base

    result = bs.bootstrap(state, store)
    assert result.changed
    assert not bs.bootstrap(state, store).changed

    # Layer-0 signers get the private key too.
    for private in (False, True):
        assert bs.place_jwt_key(state, SERVICE_ID, BLOCK, store=store, private=private) is True

    etc = state.service_root(SERVICE_ID) / "etc"
    assert sorted(p.name for p in etc.iterdir()) == ["jwt-rs256.pem", "jwt-rs256.pub"]
    assert all(p.stat().st_uid == BLOCK.uid_start for p in etc.iterdir())
    # The declarations the run wrote still validate as written.
    for service_id in (bs.REGISTRY_ID, bs.AUTH_ID):
        assert state.load_declaration(service_id).id == service_id
