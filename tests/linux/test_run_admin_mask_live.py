"""``run_admin``'s mount-namespace mask, for real (issue #1).

The portable tests assert the mask is *forwarded*; this one asserts it actually
hides. Control case first: unmasked, inner root reads what the harness uid can
(the whole point of the admin map) -- which is exactly the access the mask has
to contain for untrusted builds and provisioning tools.

Run with ``scripts/remote-test.sh ams-secfix tests/linux/test_run_admin_mask_live.py``.
"""

from __future__ import annotations

import os
from pathlib import Path

import linuxhost
import pytest

from ams.userns import run_admin

pytestmark = pytest.mark.linux

BLOCK = linuxhost.block(0)


def test_mask_hides_harness_private_files(tmp_path: Path) -> None:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    token = secrets / "TOKEN"
    token.write_text("top-secret\n", encoding="utf-8")
    os.chmod(secrets, 0o700)
    os.chmod(token, 0o600)
    workdir = tmp_path / "build"
    workdir.mkdir()

    # Control: unmasked, inner root reads the harness uid's 0600 file.
    run_admin(["cp", str(token), str(workdir / "leak")], BLOCK, timeout_s=30.0).check()
    assert (workdir / "leak").read_text(encoding="utf-8") == "top-secret\n"

    # Masked: the same copy finds nothing -- the source stat fails inside the
    # private mount ns, cp exits non-zero, and nothing lands in the workdir.
    result = run_admin(
        ["cp", str(token), str(workdir / "leak2")],
        BLOCK,
        timeout_s=30.0,
        mask=[secrets],
    )
    assert not result.ok
    assert not (workdir / "leak2").exists()

    # The mask lives and dies with the child: the parent still sees the file.
    assert token.read_text(encoding="utf-8") == "top-secret\n"


def test_mask_skips_absent_paths_and_never_propagates(tmp_path: Path) -> None:
    workdir = tmp_path / "build"
    workdir.mkdir()
    (workdir / "seen").write_text("visible\n", encoding="utf-8")

    # An absent path in the mask is skipped, the run succeeds, and the parent
    # namespace is untouched (no mount leaks back into the harness).
    run_admin(
        ["touch", str(workdir / "after")],
        BLOCK,
        timeout_s=30.0,
        mask=[tmp_path / "does-not-exist"],
    ).check()
    assert (workdir / "after").exists()
    assert (workdir / "seen").read_text(encoding="utf-8") == "visible\n"
