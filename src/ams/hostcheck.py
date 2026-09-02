"""Pre-flight checks: is this host able to run isolated services at all?

Every check here corresponds to a failure we actually hit while bringing the
target box up, and every failing check carries the fix in its detail string so
the agent loop (or a human) can act on it without rediscovering the cause.

Run standalone::

    python -m ams.hostcheck
"""

from __future__ import annotations

import os
import platform
import pwd
import shutil
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from ams.cgroup import SYSFS_CGROUP, CgroupRoot, CgroupUnavailable

SUBUID = Path("/etc/subuid")
SUBGID = Path("/etc/subgid")
APPARMOR_RESTRICT = Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns")
APPARMOR_ATTR = (Path("/proc/self/attr/apparmor/current"), Path("/proc/self/attr/current"))
MAX_USER_NS = Path("/proc/sys/user/max_user_namespaces")
USERNS_CLONE = Path("/proc/sys/kernel/unprivileged_userns_clone")
HARNESS_PROFILE = "ams-harness"


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def parse_subid(text: str, user: str) -> list[tuple[int, int]]:
    """Parse ``name:start:count`` lines for one user.

    Deliberately a local parser rather than an import: ``ams.uidmap`` owns the
    allocation policy, this only needs to know whether a range exists.
    """
    ranges: list[tuple[int, int]] = []
    for line in text.splitlines():
        parts = line.strip().split(":")
        if len(parts) != 3 or parts[0] != user:
            continue
        try:
            ranges.append((int(parts[1]), int(parts[2])))
        except ValueError:
            continue
    return ranges


# --------------------------------------------------------------------------- checks


def _check_kernel() -> CheckResult:
    ok = sys.platform.startswith("linux")
    return CheckResult(
        "kernel",
        ok,
        f"{platform.system()} {platform.release()}"
        + ("" if ok else " — the isolated spawner is Linux only"),
    )


def _check_subid(path: Path, user: str, label: str) -> CheckResult:
    text = _read(path)
    if text is None:
        return CheckResult(label, False, f"{path} unreadable; install the `uidmap` package")
    ranges = parse_subid(text, user)
    if not ranges:
        return CheckResult(
            label,
            False,
            f"no range for {user!r} in {path}; add one, e.g. "
            f"`usermod --add-sub-uids 100000-165535 {user}`",
        )
    return CheckResult(label, True, f"{user}: " + ", ".join(f"{s}:{c}" for s, c in ranges))


def _check_setuid_helper(name: str) -> CheckResult:
    found = shutil.which(name)
    if found is None:
        return CheckResult(name, False, f"{name} not on PATH; `apt install uidmap`")
    mode = os.stat(found).st_mode
    if not mode & stat.S_ISUID:
        return CheckResult(
            name,
            False,
            f"{found} is not setuid root ({stat.filemode(mode)}); "
            f"`chmod u+s {found}` — without it only a single-uid map is possible",
        )
    return CheckResult(name, True, f"{found} ({stat.filemode(mode)})")


def _check_cgroup_v2(sysfs: Path) -> CheckResult:
    controllers = _read(sysfs / "cgroup.controllers")
    if controllers is None:
        return CheckResult(
            "cgroup-v2",
            False,
            f"{sysfs}/cgroup.controllers missing; host is not on the cgroup v2 unified hierarchy",
        )
    return CheckResult("cgroup-v2", True, f"controllers: {' '.join(controllers.split())}")


def _check_delegation() -> CheckResult:
    try:
        root = CgroupRoot.discover()
    except CgroupUnavailable as e:
        return CheckResult("cgroup-delegated", False, str(e))
    return CheckResult(
        "cgroup-delegated",
        True,
        f"{root.path} (enabled: {' '.join(root.enabled_controllers()) or 'none yet'})",
    )


def _apparmor_profile() -> str:
    for path in APPARMOR_ATTR:
        text = _read(path)
        if text:
            return text.strip()
    return ""


def _check_apparmor() -> CheckResult:
    restrict = (_read(APPARMOR_RESTRICT) or "").strip()
    if restrict != "1":
        return CheckResult(
            "apparmor-userns",
            True,
            f"unrestricted (apparmor_restrict_unprivileged_userns={restrict or 'absent'})",
        )
    profile = _apparmor_profile()
    if HARNESS_PROFILE in profile:
        return CheckResult("apparmor-userns", True, f"restricted, running under {profile}")
    return CheckResult(
        "apparmor-userns",
        False,
        f"apparmor_restrict_unprivileged_userns=1 and this process is {profile or 'unconfined'}, "
        f"not {HARNESS_PROFILE}: setresuid/chown inside the namespace will fail with EPERM even "
        "after the map is written. Install deploy/apparmor/ams-harness to /etc/apparmor.d/, run "
        "`apparmor_parser -r /etc/apparmor.d/ams-harness`, and start the harness from the "
        "interpreter the profile is attached to (/home/harness/venv/bin/python3)",
    )


def _check_userns_sysctl() -> CheckResult:
    max_ns = (_read(MAX_USER_NS) or "").strip()
    clone = (_read(USERNS_CLONE) or "").strip()
    if max_ns and max_ns.isdigit() and int(max_ns) <= 0:
        return CheckResult(
            "userns-sysctl",
            False,
            "user.max_user_namespaces=0; `sysctl -w user.max_user_namespaces=15000`",
        )
    if clone == "0":
        return CheckResult(
            "userns-sysctl",
            False,
            "kernel.unprivileged_userns_clone=0; `sysctl -w kernel.unprivileged_userns_clone=1`",
        )
    detail = f"max_user_namespaces={max_ns or 'absent'}"
    if clone:
        detail += f", unprivileged_userns_clone={clone}"
    return CheckResult("userns-sysctl", True, detail)


def blocked_ancestors(*dirs: Path) -> list[Path]:
    """Existing directories on the way to ``dirs`` that lack o+x, outermost first.

    Non-existent paths contribute nothing: the harness creates them itself and
    they inherit a traversable mode.
    """
    blocked: set[Path] = set()
    for d in dirs:
        for p in [d, *d.parents]:
            if p.exists() and not (os.stat(p).st_mode & stat.S_IXOTH):
                blocked.add(p)
    return sorted(blocked, key=lambda p: (len(p.parts), str(p)))


def _check_state_traversal(*dirs: Path) -> CheckResult:
    """Service uids are outside the harness' groups: every ancestor needs o+x.

    Without this a service can inherit a cwd but cannot open anything by
    absolute path: not its own root, and not a per-service environment handed
    to it from the store. The harness home is the one that bites, because
    ``useradd`` creates it 0750. 0711 is enough and leaks nothing: it grants
    traversal only, and the files inside stay unreadable.
    """
    blocked = blocked_ancestors(*dirs)
    listed = ", ".join(str(d) for d in dirs)
    if not blocked:
        return CheckResult("state-traversal", True, f"{listed} reachable by service uids")
    worst = blocked[0]  # outermost: fixing it may be enough to unblock the rest
    return CheckResult(
        "state-traversal",
        False,
        f"{worst} is {stat.filemode(os.stat(worst).st_mode)}: a service uid cannot traverse it, so "
        f"absolute paths under {listed} fail inside the namespace. Fix: `chmod 0711 {worst}` "
        "(adds traversal only; files inside stay unreadable)",
    )


def check_host(
    user: str | None = None,
    state_dir: Path | None = None,
    store_dir: Path | None = None,
) -> list[CheckResult]:
    """Run every pre-flight check. Order is roughly cheapest-first."""
    if user is None:
        user = pwd.getpwuid(os.getuid()).pw_name
    if state_dir is None:
        state_dir = Path(os.environ.get("AMS_STATE_DIR", str(Path.home() / "state")))
    if store_dir is None:
        store_dir = Path(os.environ.get("AMS_STORE_DIR", str(Path.home() / "store")))
    return [
        _check_kernel(),
        _check_subid(SUBUID, user, "subuid"),
        _check_subid(SUBGID, user, "subgid"),
        _check_setuid_helper("newuidmap"),
        _check_setuid_helper("newgidmap"),
        _check_cgroup_v2(SYSFS_CGROUP),
        _check_delegation(),
        _check_apparmor(),
        _check_userns_sysctl(),
        _check_state_traversal(Path(state_dir), Path(store_dir)),
    ]


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    user = args[0] if args else None
    results = check_host(user)
    width = max(len(r.name) for r in results)
    for r in results:
        print(f"{'OK  ' if r.ok else 'FAIL'}  {r.name.ljust(width)}  {r.detail}")
    failed = [r.name for r in results if not r.ok]
    if failed:
        print(f"\n{len(failed)} check(s) failed: {', '.join(failed)}")
        return 1
    print(f"\nall {len(results)} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
