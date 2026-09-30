"""Managed Node toolchain (PLAN-core §4.1): download, verify, install, and use it.

Everything here is portable. The download is a fake ``fetch`` serving a tiny
``.tar.gz`` shaped like a nodejs.org release (``node-v<ver>-<tag>/bin/node``)
plus a matching ``SHASUMS256.txt``; its ``bin/npm`` is a shell script that does
what ``npm install -g --prefix <dir> pnpm@<ver>`` would leave behind. The
admin-namespace half of ``provision_tree`` is covered by recording ``_admin``;
the real namespace run lives in ``tests/linux``.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import stat
import sys
import tarfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from ams import runtime as rt_mod
from ams import schema
from ams.runtime import (
    NodeToolchain,
    ProvisionError,
    RuntimeStore,
    ensure_node_toolchain,
    managed_toolchain,
    node_archive_name,
    node_platform_tag,
    node_toolchain,
    provision,
    provision_tree,
    provisioning_env,
    runtime_env,
)
from ams.uidmap import UidBlock
from ams.userns import AdminResult

NODE = "24.20.0"
PNPM = "11.19.0"
TAG = "linux-x64"
DIST = f"https://nodejs.org/dist/v{NODE}/"
ARCHIVE = f"node-v{NODE}-{TAG}.tar.gz"

FAKE_NODE = "#!/bin/sh\necho v24.20.0\n"
# Records its argv and PATH into the prefix, then leaves a runnable bin/pnpm
# exactly where npm's global install into --prefix would put the shim.
FAKE_NPM = """#!/bin/sh
prefix=""
all="$*"
while [ $# -gt 0 ]; do
  case "$1" in
    --prefix) prefix="$2"; shift ;;
  esac
  shift
done
[ -n "$prefix" ] || { echo "no --prefix" >&2; exit 2; }
mkdir -p "$prefix/bin" "$prefix/lib/node_modules/pnpm/bin"
printf '%s\\n' "$all" > "$prefix/npm-argv"
printf '%s\\n' "$PATH" > "$prefix/npm-path"
printf '%s\\n' "${npm_config_ignore_scripts:-unset}" > "$prefix/npm-ignore-scripts"
printf '#!/bin/sh\\necho 11.19.0\\n' > "$prefix/lib/node_modules/pnpm/bin/pnpm.cjs"
chmod 755 "$prefix/lib/node_modules/pnpm/bin/pnpm.cjs"
ln -s ../lib/node_modules/pnpm/bin/pnpm.cjs "$prefix/bin/pnpm"
"""
FAILING_NPM = "#!/bin/sh\necho 'npm ERR! 404 pnpm@11.19.0 not found' >&2\nexit 1\n"


def make_tarball(
    top: str = f"node-v{NODE}-{TAG}",
    *,
    npm: str = FAKE_NPM,
    extra: tuple[tuple[str, bytes], ...] = (),
    with_node: bool = True,
) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:

        def add_dir(name: str) -> None:
            info = tarfile.TarInfo(name)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            tar.addfile(info)

        def add_file(name: str, data: bytes, mode: int = 0o755) -> None:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = mode
            tar.addfile(info, io.BytesIO(data))

        add_dir(top)
        add_dir(f"{top}/bin")
        if with_node:
            add_file(f"{top}/bin/node", FAKE_NODE.encode())
        add_file(f"{top}/bin/npm", npm.encode())
        add_file(f"{top}/README.md", b"node\n", 0o644)
        link = tarfile.TarInfo(f"{top}/bin/corepack")  # a relative in-tree link, like upstream
        link.type = tarfile.SYMTYPE
        link.linkname = "node"
        tar.addfile(link)
        for name, data in extra:
            add_file(name, data, 0o644)
    return buf.getvalue()


def shasums(archive: bytes, name: str = ARCHIVE) -> bytes:
    digest = hashlib.sha256(archive).hexdigest()
    return (
        f"{'0' * 64}  node-v{NODE}-win-x64.zip\n"
        f"{digest}  {name}\n"
        f"{'1' * 64}  node-v{NODE}-darwin-arm64.tar.gz\n"
    ).encode()


class FakeFetch:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.calls: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.calls.append(url)
        if url not in self.files:
            raise ProvisionError(f"404 {url}")
        return self.files[url]


def good_fetch(archive: bytes | None = None) -> FakeFetch:
    archive = make_tarball() if archive is None else archive
    return FakeFetch({DIST + "SHASUMS256.txt": shasums(archive), DIST + ARCHIVE: archive})


def no_fetch(url: str) -> bytes:
    raise AssertionError(f"must not download anything, asked for {url}")


def leftovers(d: Path) -> list[str]:
    return sorted(p.name for p in d.iterdir()) if d.is_dir() else []


@pytest.fixture
def store(tmp_path: Path) -> RuntimeStore:
    return RuntimeStore(tmp_path / "store", bun_install=tmp_path / "bun")


# --------------------------------------------------------------------------- tags & names


@pytest.mark.parametrize(
    ("platform", "machine", "tag"),
    [
        ("linux", "x86_64", "linux-x64"),
        ("linux", "amd64", "linux-x64"),
        ("linux", "aarch64", "linux-arm64"),
        ("linux", "arm64", "linux-arm64"),
        ("darwin", "arm64", "darwin-arm64"),
        ("darwin", "x86_64", "darwin-x64"),
    ],
)
def test_platform_tag_mapping(platform: str, machine: str, tag: str) -> None:
    assert node_platform_tag(platform, machine) == tag


@pytest.mark.parametrize(("platform", "machine"), [("win32", "AMD64"), ("linux", "riscv64")])
def test_unsupported_platforms_are_refused(platform: str, machine: str) -> None:
    with pytest.raises(ProvisionError, match="no managed node"):
        node_platform_tag(platform, machine)


def test_platform_tag_defaults_to_this_host() -> None:
    assert node_platform_tag().split("-")[0] in ("linux", "darwin")


def test_archive_name_is_the_tar_gz_every_supported_tag_publishes() -> None:
    # nodejs.org publishes .tar.gz AND .tar.xz for all four tags; .tar.gz needs
    # only zlib, which every CPython build has (lzma is an optional module).
    assert node_archive_name(NODE, "linux-arm64") == "node-v24.20.0-linux-arm64.tar.gz"
    assert node_archive_name(NODE, "darwin-x64") == "node-v24.20.0-darwin-x64.tar.gz"


# --------------------------------------------------------------------------- pure paths


def test_node_toolchain_paths_live_in_the_store(store: RuntimeStore) -> None:
    assert store.node_dir == store.root / "node"
    assert store.pnpm_dir == store.root / "pnpm"
    tc = node_toolchain(store, NODE, PNPM)
    assert tc == NodeToolchain(
        NODE, PNPM, store.root / "node" / "v24.20.0", store.root / "pnpm" / "11.19.0"
    )
    # pnpm's bin first: its shim is what a bare `pnpm` must resolve to.
    assert tc.bin_dirs == (
        str(store.root / "pnpm" / "11.19.0" / "bin"),
        str(store.root / "node" / "v24.20.0" / "bin"),
    )
    assert node_toolchain(store, NODE).bin_dirs == (str(store.root / "node" / "v24.20.0" / "bin"),)


def test_managed_toolchain_follows_the_spec(store: RuntimeStore) -> None:
    assert managed_toolchain(schema.RuntimeSpec(kind="pnpm", node=NODE, pnpm=PNPM), store) == (
        node_toolchain(store, NODE, PNPM)
    )
    assert managed_toolchain(schema.RuntimeSpec(kind="pnpm", node="22"), store) is None
    assert managed_toolchain(schema.RuntimeSpec(kind="bun", node=NODE), store) is None
    assert managed_toolchain(schema.RuntimeSpec(kind="uv"), store) is None


# --------------------------------------------------------------------------- ensure


def test_ensure_downloads_verifies_and_installs(store: RuntimeStore) -> None:
    fetch = good_fetch()
    tc = ensure_node_toolchain(store, NODE, fetch=fetch, platform_tag=TAG)
    assert fetch.calls == [DIST + "SHASUMS256.txt", DIST + ARCHIVE]
    assert tc == node_toolchain(store, NODE)
    node = tc.node_dir / "bin" / "node"
    assert node.is_file() and os.access(node, os.X_OK)
    assert (tc.node_dir / "bin" / "corepack").is_symlink()
    # world-readable: the services run it under their own uids.
    assert stat.S_IMODE(os.stat(tc.node_dir).st_mode) == 0o755
    assert leftovers(store.node_dir) == ["v24.20.0"]  # no temp dirs left behind


def test_ensure_is_idempotent(store: RuntimeStore) -> None:
    ensure_node_toolchain(store, NODE, fetch=good_fetch(), platform_tag=TAG)
    tc = ensure_node_toolchain(store, NODE, fetch=no_fetch, platform_tag=TAG)
    assert tc == node_toolchain(store, NODE)


def test_checksum_mismatch_leaves_nothing_in_place(store: RuntimeStore) -> None:
    archive = make_tarball()
    fetch = FakeFetch(
        {DIST + "SHASUMS256.txt": shasums(b"something else"), DIST + ARCHIVE: archive}
    )
    with pytest.raises(ProvisionError, match="sha256 mismatch"):
        ensure_node_toolchain(store, NODE, fetch=fetch, platform_tag=TAG)
    assert not (store.node_dir / "v24.20.0").exists()
    assert leftovers(store.node_dir) == []


def test_archive_missing_from_shasums_is_refused(store: RuntimeStore) -> None:
    archive = make_tarball()
    fetch = FakeFetch(
        {
            DIST + "SHASUMS256.txt": shasums(archive, name="node-v24.20.0-linux-s390x.tar.gz"),
            DIST + ARCHIVE: archive,
        }
    )
    with pytest.raises(ProvisionError, match="not listed in SHASUMS256"):
        ensure_node_toolchain(store, NODE, fetch=fetch, platform_tag=TAG)
    assert fetch.calls == [DIST + "SHASUMS256.txt"]  # never downloaded the archive
    assert leftovers(store.node_dir) == []


@pytest.mark.parametrize(
    "archive_factory",
    [
        lambda: make_tarball(extra=((f"node-v{NODE}-{TAG}/../../escape", b"x"),)),
        lambda: make_tarball(extra=(("elsewhere/file", b"x"),)),
        lambda: make_tarball(with_node=False),
        lambda: b"not a tarball at all",
    ],
    ids=["dotdot", "outside-top-dir", "no-bin-node", "garbage"],
)
def test_bad_archives_leave_nothing_in_place(
    store: RuntimeStore, tmp_path: Path, archive_factory: Callable[[], bytes]
) -> None:
    with pytest.raises(ProvisionError):
        ensure_node_toolchain(store, NODE, fetch=good_fetch(archive_factory()), platform_tag=TAG)
    assert leftovers(store.node_dir) == []
    assert not (tmp_path / "escape").exists()


@pytest.mark.parametrize("bad", ["24", "24.20", "v24.20.0", "24.20.0; rm -rf /"])
def test_ensure_requires_exact_versions(store: RuntimeStore, bad: str) -> None:
    with pytest.raises(ProvisionError, match="exact"):
        ensure_node_toolchain(store, bad, fetch=no_fetch, platform_tag=TAG)
    with pytest.raises(ProvisionError, match="exact"):
        ensure_node_toolchain(store, NODE, bad, fetch=no_fetch, platform_tag=TAG)


def test_fetch_failure_becomes_a_provision_error(store: RuntimeStore) -> None:
    def broken(url: str) -> bytes:
        raise OSError("connection reset")

    with pytest.raises(ProvisionError, match="connection reset"):
        ensure_node_toolchain(store, NODE, fetch=broken, platform_tag=TAG)
    assert leftovers(store.node_dir) == []


def test_ensure_installs_pnpm_with_the_managed_npm(store: RuntimeStore) -> None:
    tc = ensure_node_toolchain(store, NODE, PNPM, fetch=good_fetch(), platform_tag=TAG)
    assert tc == node_toolchain(store, NODE, PNPM)
    assert tc.pnpm_dir is not None
    shim = tc.pnpm_dir / "bin" / "pnpm"
    assert shim.exists() and os.access(shim, os.X_OK)
    argv = (tc.pnpm_dir / "npm-argv").read_text().split()
    assert argv[:3] == ["install", "-g", "--prefix"]
    assert argv[-1] == "pnpm@11.19.0"
    # the managed node must be what npm (and pnpm's `#!/usr/bin/env node`) finds.
    path = (tc.pnpm_dir / "npm-path").read_text().strip().split(":")
    assert path[0] == str(tc.node_dir / "bin")
    # pnpm has no install scripts; npm must not run any it would be handed.
    assert (tc.pnpm_dir / "npm-ignore-scripts").read_text().strip() == "true"
    assert stat.S_IMODE(os.stat(tc.pnpm_dir).st_mode) == 0o755
    assert leftovers(store.pnpm_dir) == ["11.19.0"]


def test_pnpm_install_is_idempotent_too(store: RuntimeStore) -> None:
    ensure_node_toolchain(store, NODE, PNPM, fetch=good_fetch(), platform_tag=TAG)
    mtime = os.stat(store.pnpm_dir / PNPM / "npm-argv").st_mtime_ns
    ensure_node_toolchain(store, NODE, PNPM, fetch=no_fetch, platform_tag=TAG)
    assert os.stat(store.pnpm_dir / PNPM / "npm-argv").st_mtime_ns == mtime


def test_pnpm_install_failure_leaves_no_pnpm_dir(store: RuntimeStore) -> None:
    fetch = good_fetch(make_tarball(npm=FAILING_NPM))
    with pytest.raises(ProvisionError, match="404 pnpm@11.19.0"):
        ensure_node_toolchain(store, NODE, PNPM, fetch=fetch, platform_tag=TAG)
    assert leftovers(store.pnpm_dir) == []
    # the node half is still good and is reused next time.
    assert (store.node_dir / "v24.20.0" / "bin" / "node").is_file()


# --------------------------------------------------------------------------- env


def pnpm_decl(workdir: str = "current", **runtime: Any) -> schema.ServiceDecl:
    return schema.from_dict(
        {
            "id": "core",
            "start": {"argv": ["node", "dist/core/main.js"], "workdir": workdir},
            "runtime": {"kind": "pnpm", **runtime},
        }
    )


ROOT = Path("/state/services/core/root")
STORE = RuntimeStore(Path("/srv/store"), bun_install=Path("/opt/bun"))


def test_runtime_env_puts_the_managed_toolchain_ahead_of_the_host() -> None:
    env = runtime_env(pnpm_decl(node=NODE, pnpm=PNPM), ROOT, STORE)
    assert env.path_prepend == (
        "/state/services/core/root/current/node_modules/.bin",
        "/srv/store/pnpm/11.19.0/bin",
        "/srv/store/node/v24.20.0/bin",
    )
    assert env.extra_env == {
        "PNPM_HOME": "/srv/store/pnpm-home",
        "npm_config_store_dir": "/srv/store/pnpm-store",
    }


def test_runtime_env_managed_node_with_host_pnpm_keeps_pnpm_home() -> None:
    env = runtime_env(pnpm_decl(node=NODE), ROOT, STORE)
    assert env.path_prepend == (
        "/state/services/core/root/current/node_modules/.bin",
        "/srv/store/node/v24.20.0/bin",
        "/srv/store/pnpm-home/bin",
    )


def test_runtime_env_legacy_node_is_unchanged() -> None:
    env = runtime_env(pnpm_decl(node="22"), ROOT, STORE)
    assert env.path_prepend == (
        "/state/services/core/root/current/node_modules/.bin",
        "/srv/store/pnpm-home/bin",
    )


def test_provisioning_env_prepends_the_toolchain_for_a_managed_spec() -> None:
    base = provisioning_env(STORE)
    managed = provisioning_env(STORE, schema.RuntimeSpec(kind="pnpm", node=NODE, pnpm=PNPM))
    assert managed["PATH"].split(":") == [
        "/srv/store/pnpm/11.19.0/bin",
        "/srv/store/node/v24.20.0/bin",
        *base["PATH"].split(":"),
    ]
    assert {k: v for k, v in managed.items() if k != "PATH"} == {
        k: v for k, v in base.items() if k != "PATH"
    }
    assert provisioning_env(STORE, schema.RuntimeSpec(kind="pnpm", node="22")) == base


# --------------------------------------------------------------------------- provision()


class AdminRecorder:
    """Stands in for ``runtime._admin``: records the shown argv and the tool env."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail_on = fail_on

    def __call__(self, argv: Any, block: UidBlock, **kw: Any) -> AdminResult:
        argv = list(argv)
        shown = list(kw.get("display") or argv)
        env: dict[str, str] = {}
        if argv[:2] == ["env", "-i"]:
            rest = argv[4:]
            while rest and "=" in rest[0] and not rest[0].startswith("/"):
                k, _, v = rest.pop(0).partition("=")
                env[k] = v
        self.calls.append({"argv": argv, "shown": shown, "env": env, "what": kw.get("what")})
        if self.fail_on and self.fail_on in " ".join(shown):
            raise ProvisionError(f"{kw.get('what')} failed (rc=1): boom")
        return AdminResult(tuple(argv), 0, b"", b"")


@pytest.fixture
def own_block() -> UidBlock:
    """A block whose host uid is ours, so provision's final owner check passes."""
    return UidBlock(os.getuid(), os.getgid(), 1024)


def test_provision_managed_spec_ensures_toolchain_then_installs_then_builds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store: RuntimeStore, own_block: UidBlock
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    ensured: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        rt_mod,
        "ensure_node_toolchain",
        lambda s, n, p=None, **kw: (ensured.append((s, n, p)), node_toolchain(s, n, p))[1],
    )
    admin = AdminRecorder()
    monkeypatch.setattr(rt_mod, "_admin", admin)
    root = tmp_path / "root"
    (root / "current").mkdir(parents=True)
    (root / "current" / "package.json").write_text("{}")
    d = pnpm_decl(node=NODE, pnpm=PNPM, build=["node", "scripts/build.mjs"])

    provision(d, root, store, own_block)

    assert ensured == [(store, NODE, PNPM)]
    shown = [c["shown"] for c in admin.calls]
    assert shown[0][:2] == ["pnpm", "install"]
    assert shown[1] == ["node", "scripts/build.mjs"]
    assert shown[2][:2] == ["chown", "-R"]
    for call in admin.calls[:2]:
        assert call["argv"][2:4] == ["-C", str(root / "current")]
        assert call["env"]["PATH"].split(":")[:2] == list(
            node_toolchain(store, NODE, PNPM).bin_dirs
        )


def test_provision_legacy_node_keeps_the_warning_and_never_downloads(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    store: RuntimeStore,
    own_block: UidBlock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(rt_mod, "ensure_node_toolchain", lambda *a, **k: pytest.fail("download"))
    admin = AdminRecorder()
    monkeypatch.setattr(rt_mod, "_admin", admin)
    root = tmp_path / "root"
    root.mkdir()
    (root / "package.json").write_text("{}")
    with caplog.at_level(logging.WARNING, logger="ams.runtime"):
        provision(pnpm_decl(workdir=".", node="22"), root, store, own_block)
    assert "is not honoured" in caplog.text
    assert [c["shown"][:2] for c in admin.calls] == [["pnpm", "install"], ["chown", "-R"]]


# --------------------------------------------------------------------------- provision_tree


FAKE_TOOL = """#!/bin/sh
{{
  echo "$(basename "$0") $*"
  echo "cwd=$(pwd)"
  echo "commit=$CORE_SOURCE_COMMIT"
  echo "path=$PATH"
  echo "import=$npm_config_package_import_method"
}} >> "{record}"
{extra}
"""


def install_tool(bin_dir: Path, name: str, record: Path, extra: str = "") -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    tool = bin_dir / name
    tool.write_text(FAKE_TOOL.format(record=record, extra=extra))
    tool.chmod(0o755)


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    t = tmp_path / "releases" / "abc123"
    t.mkdir(parents=True)
    (t / "package.json").write_text('{"name": "x", "private": true}')
    (t / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
    return t


def test_provision_tree_refuses_non_pnpm_specs(store: RuntimeStore, tree: Path) -> None:
    for spec in (schema.RuntimeSpec(kind="uv"), schema.RuntimeSpec(kind="bun")):
        with pytest.raises(ProvisionError, match="kind=pnpm"):
            provision_tree(tree, spec, block=None, store=store)


def test_provision_tree_plain_uses_host_pnpm_frozen_lockfile_and_builds(
    store: RuntimeStore, tree: Path, tmp_path: Path
) -> None:
    record = tmp_path / "record.txt"
    install_tool(store.pnpm_home / "bin", "pnpm", record)
    install_tool(store.pnpm_home / "bin", "node", record)
    spec = schema.RuntimeSpec(kind="pnpm", node="22", build=("node", "scripts/build.mjs"))
    log_path = tmp_path / "logs" / "provision.log"

    provision_tree(
        tree,
        spec,
        block=None,
        store=store,
        env={"CORE_SOURCE_COMMIT": "a" * 40},
        log_path=log_path,
    )

    lines = record.read_text().splitlines()
    assert lines[0] == "pnpm install --frozen-lockfile"
    assert f"cwd={tree}" in lines
    assert f"commit={'a' * 40}" in lines
    assert lines[5] == "node scripts/build.mjs"
    # plain mode (dev, macOS, --no-isolation): the tree and the store may be on
    # different filesystems, so a hard `clone` would fail; reflink when possible.
    assert "import=clone-or-copy" in lines
    text = log_path.read_text()
    assert "pnpm install --frozen-lockfile" in text and "node scripts/build.mjs" in text


def test_provision_tree_managed_toolchain_is_first_on_path(
    monkeypatch: pytest.MonkeyPatch, store: RuntimeStore, tree: Path, tmp_path: Path
) -> None:
    record = tmp_path / "record.txt"
    tc = node_toolchain(store, NODE, PNPM)
    assert tc.pnpm_dir is not None
    install_tool(tc.pnpm_dir / "bin", "pnpm", record)
    install_tool(tc.node_dir / "bin", "node", record)
    ensured: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        rt_mod,
        "ensure_node_toolchain",
        lambda s, n, p=None, **kw: (ensured.append((n, p)), node_toolchain(s, n, p))[1],
    )
    spec = schema.RuntimeSpec(kind="pnpm", node=NODE, pnpm=PNPM, build=("node", "b.mjs"))

    provision_tree(tree, spec, block=None, store=store)

    assert ensured == [(NODE, PNPM)]
    lines = record.read_text().splitlines()
    path_lines = [ln for ln in lines if ln.startswith("path=")]
    assert len(path_lines) == 2
    for ln in path_lines:
        assert ln.removeprefix("path=").split(":")[:2] == list(tc.bin_dirs)


def test_provision_tree_run_build_false_only_installs(
    store: RuntimeStore, tree: Path, tmp_path: Path
) -> None:
    record = tmp_path / "record.txt"
    install_tool(store.pnpm_home / "bin", "pnpm", record)
    spec = schema.RuntimeSpec(kind="pnpm", build=("node", "never.mjs"))
    provision_tree(tree, spec, block=None, store=store, run_build=False)
    assert [ln for ln in record.read_text().splitlines() if " " in ln and "=" not in ln] == [
        "pnpm install --frozen-lockfile"
    ]


def test_provision_tree_without_lockfile_resolves_and_warns(
    store: RuntimeStore, tree: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (tree / "pnpm-lock.yaml").unlink()
    record = tmp_path / "record.txt"
    install_tool(store.pnpm_home / "bin", "pnpm", record)
    with caplog.at_level(logging.WARNING, logger="ams.runtime"):
        provision_tree(tree, schema.RuntimeSpec(kind="pnpm"), block=None, store=store)
    assert record.read_text().splitlines()[0] == "pnpm install"
    assert "pnpm-lock.yaml" in caplog.text


def test_provision_tree_failure_carries_the_stderr_tail(
    store: RuntimeStore, tree: Path, tmp_path: Path
) -> None:
    record = tmp_path / "record.txt"
    install_tool(store.pnpm_home / "bin", "pnpm", record)
    install_tool(
        store.pnpm_home / "bin",
        "node",
        record,
        extra="echo 'error TS2304: cannot find name' >&2\nexit 3",
    )
    spec = schema.RuntimeSpec(kind="pnpm", build=("node", "scripts/build.mjs"))
    with pytest.raises(ProvisionError, match=r"(?s)rc=3.*TS2304"):
        provision_tree(tree, spec, block=None, store=store)


def test_provision_tree_missing_tool_is_a_clean_error(store: RuntimeStore, tree: Path) -> None:
    spec = schema.RuntimeSpec(kind="pnpm")
    with pytest.raises(ProvisionError, match="'pnpm' not found"):
        provision_tree(
            tree, spec, block=None, store=store, env={"PATH": str(tree / "nothing-here")}
        )


def test_provision_tree_times_out(store: RuntimeStore, tree: Path, tmp_path: Path) -> None:
    record = tmp_path / "record.txt"
    install_tool(store.pnpm_home / "bin", "pnpm", record, extra="sleep 30")
    with pytest.raises(ProvisionError, match="timed out"):
        provision_tree(
            tree, schema.RuntimeSpec(kind="pnpm"), block=None, store=store, timeout_s=0.5
        )


@pytest.mark.parametrize("missing", ["tree", "package.json"])
def test_provision_tree_needs_a_package_tree(store: RuntimeStore, tree: Path, missing: str) -> None:
    target = tree if missing == "tree" else tree / "package.json"
    if missing == "tree":
        for p in tree.iterdir():
            p.unlink()
        tree.rmdir()
    else:
        target.unlink()
    with pytest.raises(ProvisionError, match=missing):
        provision_tree(tree, schema.RuntimeSpec(kind="pnpm"), block=None, store=store)


def test_provision_tree_isolated_refuses_off_linux(
    monkeypatch: pytest.MonkeyPatch, store: RuntimeStore, tree: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    with pytest.raises(ProvisionError, match="needs Linux"):
        provision_tree(
            tree, schema.RuntimeSpec(kind="pnpm"), block=UidBlock(100_000, 100_000), store=store
        )


def test_provision_tree_isolated_runs_as_inner_root_then_chowns(
    monkeypatch: pytest.MonkeyPatch, store: RuntimeStore, tree: Path, own_block: UidBlock
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(
        rt_mod, "ensure_node_toolchain", lambda s, n, p=None, **kw: node_toolchain(s, n, p)
    )
    admin = AdminRecorder()
    monkeypatch.setattr(rt_mod, "_admin", admin)
    spec = schema.RuntimeSpec(kind="pnpm", node=NODE, pnpm=PNPM, build=("node", "b.mjs"))

    provision_tree(tree, spec, block=own_block, store=store, env={"CORE_SOURCE_COMMIT": "f" * 40})

    assert [c["shown"] for c in admin.calls] == [
        ["pnpm", "install", "--frozen-lockfile"],
        ["node", "b.mjs"],
        ["chown", "-R", "1000:1000", str(tree)],
    ]
    install = admin.calls[0]
    assert install["argv"][:4] == ["env", "-i", "-C", str(tree)]
    assert install["env"]["CORE_SOURCE_COMMIT"] == "f" * 40
    assert install["env"]["PATH"].split(":")[:2] == list(node_toolchain(store, NODE, PNPM).bin_dirs)
    # the admin path keeps the reflink-only import method (one XFS store, D8).
    assert install["env"]["npm_config_package_import_method"] == "clone"


def test_provision_tree_isolated_failure_stops_before_build(
    monkeypatch: pytest.MonkeyPatch, store: RuntimeStore, tree: Path, own_block: UidBlock
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    admin = AdminRecorder(fail_on="pnpm install")
    monkeypatch.setattr(rt_mod, "_admin", admin)
    spec = schema.RuntimeSpec(kind="pnpm", build=("node", "b.mjs"))
    with pytest.raises(ProvisionError, match="boom"):
        provision_tree(tree, spec, block=own_block, store=store)
    assert [c["shown"][0] for c in admin.calls] == ["pnpm"]
