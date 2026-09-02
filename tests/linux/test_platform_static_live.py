"""Real static-site publishing against the target host (T3.4).

Everything `tests/test_platform_static.py` cannot check because it fakes
`run_admin`: that a real build actually runs inside the admin ns (the
``bun``/``find``/``cp``/``rm`` argv this module builds really execs, not just
"looks right"), and that the published tree is readable by a genuinely
*different*, mapped uid -- not merely by the harness that wrote it. The second
point is the one a portable test structurally cannot make: on macOS there is
no user namespace to prove anything against, and even on Linux a same-uid read
would not distinguish "harness-owned, 0644" from "still owned by whoever built
it".

``llm-web``'s no-build path always runs (pure `cp -a --reflink=auto`, no
external tool). ``files-web``'s bun build only runs when bun is actually
installed at ``/home/harness/.bun/bin/bun`` (CLAUDE.md's noted path); this
Linux host may or may not have it, and skipping beats a flaky network install
here -- `tests/test_platform_static.py::test_publish_files_web_runs_build_steps_via_admin_ns`
already pins the exact argv bun would receive.

Run with:
    scripts/remote-test.sh ams-static tests/linux/test_platform_static_live.py \\
        tests/test_platform_static.py
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from ams.platform.static import publish_static
from ams.runtime import RuntimeStore
from ams.state import StateDir
from ams.uidmap import UidBlock
from ams.userns import run_admin

pytestmark = [pytest.mark.linux, pytest.mark.timeout(300)]

# First block of the harness' real /etc/subuid range (as in the other live
# tests). Never chowned to here -- a static publish is never chowned to any
# block (see `publish_static`'s docstring) -- so this block is used only to
# read the result back as a genuinely different, mapped uid.
BLOCK = UidBlock(100_000, 100_000, 1024)

#: The reflink store (D8/D13): `_copy_reflink` needs the checkout and the
#: published tree on the same filesystem, and bun/pnpm's caches live here too.
STORE_ROOT = Path("/home/harness/store")
#: A private scratch tree under the store, distinct from the harness' own
#: `AMS_STATE_DIR` (`/home/harness/state/<subdir>`, set by `remote-test.sh`)
#: so this drill never touches real service state.
BASE = STORE_ROOT / "state" / "plat-static-test"

SHA_NOBUILD = "1111111111111111111111111111111111111a"
SHA_BUN = "2222222222222222222222222222222222222b"
BUN_BIN = Path("/home/harness/.bun/bin/bun")


@pytest.fixture(scope="module", autouse=True)
def _clean() -> Iterator[None]:
    shutil.rmtree(BASE, ignore_errors=True)
    yield
    shutil.rmtree(BASE, ignore_errors=True)


def _state() -> StateDir:
    return StateDir(BASE / "state")


def _store() -> RuntimeStore:
    store = RuntimeStore(STORE_ROOT, bun_install=Path("/home/harness/.bun"))
    store.ensure()
    return store


def _cat_as_mapped_uid(path: Path) -> str:
    """Read ``path`` as inner uid 1000 -- a real mapped identity, never the
    harness -- proving the file is world-readable (D4), not merely readable
    by whichever uid happened to write it."""
    result = run_admin(
        ["setpriv", "--reuid", "1000", "--regid", "1000", "--clear-groups", "cat", str(path)],
        BLOCK,
        timeout_s=30.0,
    )
    result.check()
    return result.stdout.decode("utf-8")


def test_publish_no_build_llm_web_shaped_site() -> None:
    checkout = BASE / "checkout" / SHA_NOBUILD
    app_dir = checkout / "apps" / "llm-web"
    app_dir.mkdir(parents=True)
    (app_dir / "index.html").write_text("<h1>llm</h1>\n", encoding="utf-8")
    (app_dir / "app.js").write_text("console.log(1);\n", encoding="utf-8")

    mount = {"id": "llm-web", "kind": "static", "build": []}
    target = publish_static(mount, checkout, _state(), _store(), None)

    assert target == _state().root / "platform" / "static" / "llm-web"
    assert _cat_as_mapped_uid(target / "index.html") == "<h1>llm</h1>\n"
    assert (target / ".ams-sha").read_text(encoding="utf-8").strip() == SHA_NOBUILD
    # no scratch residue after a successful publish
    assert not (target.parent / ".build" / f"llm-web-{SHA_NOBUILD}").exists()


def test_republish_same_sha_is_a_readonly_noop() -> None:
    checkout = BASE / "checkout" / SHA_NOBUILD  # reuses the previous test's tree
    mount = {"id": "llm-web", "kind": "static", "build": []}
    before = (_state().root / "platform" / "static" / "llm-web" / "index.html").stat().st_mtime
    target = publish_static(mount, checkout, _state(), _store(), None)
    after = (target / "index.html").stat().st_mtime
    assert before == after  # untouched: the marker matched, nothing was rebuilt


@pytest.mark.skipif(not BUN_BIN.is_file(), reason=f"bun not installed at {BUN_BIN}")
def test_publish_files_web_shaped_site_with_real_bun_build() -> None:
    checkout = BASE / "checkout" / SHA_BUN
    app_dir = checkout / "apps" / "files-web"
    app_dir.mkdir(parents=True)
    (app_dir / "package.json").write_text(
        '{"name": "files-web", "private": true}\n', encoding="utf-8"
    )
    (app_dir / "index.ts").write_text('console.log("built-ok");\n', encoding="utf-8")

    mount = {
        "id": "files-web",
        "kind": "static",
        "build": [
            "bun install",
            "bun build index.ts --outdir dist",
            "find . -mindepth 1 -maxdepth 1 ! -name dist -exec rm -rf {} +",
            "cp -a dist/. .",
            "rm -rf dist",
        ],
    }
    target = publish_static(mount, checkout, _state(), _store(), BLOCK)

    assert target == _state().root / "platform" / "static" / "files-web"
    built = _cat_as_mapped_uid(target / "index.js")
    assert "built-ok" in built
    # the prune step really ran: sources and node_modules did not survive
    assert not (target / "package.json").exists()
    assert not (target / "node_modules").exists()
    assert not (target / "index.ts").exists()
