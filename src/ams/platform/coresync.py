"""One core-mode tick: fetch, stage, release core, ship changed plugins, gate, record.

The moving half of core mode (PLAN-core §4.3); the static half is
:mod:`ams.platform.core`. Like the legacy :mod:`ams.platform.sync` this is **a
process, not a loop**: ``pnpm install``, ``node scripts/build.mjs`` and the
artifact builds block for seconds to minutes and the supervisor is single-threaded
(D17), so a 60 s timer (``deploy/ams-core-sync.timer``) runs one tick and it talks
to the running harness over the control socket and to core through corectl.

What a tick does, in order::

    1  fetch the mirror at cfg.ref                          -> head
    2  head != staged_sha: stage releases/<head>, pnpm install (no build)
    3  release needed (first release, or head touched cfg.core_paths):
         build, place the config bundle, stop core, flip current, declare + start,
         health gate; a failed gate flips back to the previous release
    4  plan: core_plan.mjs over the roster in the head tree -> content keys
    5  ship set: roster plugins whose content key differs from ams's last attempt
    6  upload + deploy each (corectl); outcome != ok is recorded as a failure
    7  poll status until each deploy leaves probation: live, or failed
    8  apply cfg.privileges where they differ (then restart that plugin)
    9  render + write the Caddy front; restart caddy when it changed
   10  write core.json iff something changed

Properties the timer depends on:

- **A content key that failed is never retried.** Core rejected it or reverted it
  once; deploying the same bytes every 60 s is a retry storm, not a fix. A new
  commit with *new content* retries. The same holds for a release: a sha whose
  release failed is not released again (``release_failed_sha``).
- **Manual drift is reported, not overridden.** An operator's ``corectl deploy``
  or ``revert`` stays until the plugin's content changes upstream.
- **A tick that changes nothing writes nothing** -- no record, no declaration, no
  reload, no deploy. Steady state is one ``git fetch`` and one ``corectl status``.
- **The exit code reflects this tick only** (the rule of 593daae): a failure
  carried in the record does not fail every later tick.
- **Escalate once.** Every failure is one JSON line on the escalation stream,
  deduplicated across ticks by normalized cause (``policy.cause_key``); a cause
  not seen again in a tick is forgotten, so a recurrence escalates again.

Everything with a side effect goes through :class:`Hooks`, so the portable tests
drive a whole tick with fakes and plain mode (``isolation=False``) runs the real
thing on macOS with no user namespace.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import logging
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any, Protocol

from ams.platform import core as core_mod
from ams.platform.core import CORE_ID, CoreConfig, CoreLayout
from ams.platform.corectl import CoreControl, CoreControlError, Runner
from ams.platform.policy import cause_key
from ams.runtime import RuntimeStore
from ams.schema import RuntimeSpec
from ams.state import StateCorrupt, StateDir
from ams.uidmap import UidBlock

log = logging.getLogger("ams.platform.coresync")

EXIT_OK = 0
EXIT_ERROR = 1

RECORD_VERSION = 1
ESCALATION_KIND = "CoreSync"
LOCK_NAME = "core.lock"
CORE_PLAN_ASSET = "core_plan.mjs"
PROVISION_LOG = "core-provision.log"

#: upstream core-ship polls every 3 s.
POLL_INTERVAL_S = 3.0
GATE_INTERVAL_S = 2.0
STOP_POLL_S = 1.0
STOP_WAIT_S = core_mod.STOP_TIMEOUT_S + 20.0
PLAN_TIMEOUT_S = 900.0
PROBE_TIMEOUT_S = 3.0
BASE_PATH = ("/usr/local/bin", "/usr/bin", "/bin")
#: Canonical checkouts kept in the store (``SourceMirror.gc``).
MIRROR_KEEP = 3

# Escalation kinds -- the ``event.kind`` of a record.
K_FETCH = "core_fetch_failed"
K_STAGE = "core_stage_failed"
K_RELEASE = "core_release_failed"
K_DOWN = "core_down"
K_UNREACHABLE = "core_unreachable"
K_PLAN = "core_plan_failed"
K_BUILD = "core_plugin_build_failed"
K_REJECTED = "core_plugin_rejected"
K_NOT_LIVE = "core_plugin_not_live"
K_SHIP = "core_ship_error"
K_DRIFT = "core_plugin_drift"
K_PRIVILEGES = "core_privileges_failed"
K_GATEWAY = "core_gateway_failed"
K_ERROR = "core_sync_error"

LIVE = "live"
FAILED = "failed"
PROBATION = "probation"
#: Rejected for a reason about core's *state*, not the artifact's bytes: retried
#: once that state changes (``blocked_on``), never every tick.
BLOCKED = "blocked"

#: corectl error codes that refuse the artifact bytes themselves (upload/parse,
#: upstream ``artifact.ts``/``ops.ts``). A verdict on the content key, recorded like
#: a rejected deploy; any other ``CoreControlError`` is transport and retried.
ARTIFACT_REFUSALS = frozenset(
    {
        "invalid_manifest",
        "invalid_artifact",
        "artifact_too_large",
        "manifest_mismatch",
        "plugin_mismatch",
    }
)
#: Transition reasons (upstream ``manager.ts``) that describe what core currently
#: has installed -- a missing or failed provider, a CAS race, a dependent still
#: holding a service -- rather than anything wrong with the artifact.
STATE_REJECTIONS = frozenset(
    {
        "dependency_unavailable",
        "dependency_failed",
        "dependency_major_mismatch",
        "generation_conflict",
        "service_in_use",
        "major_in_use",
        "unknown_plugin",
    }
)
#: ``observed.reason`` of a plugin whose desired *and* lastKnownGood artifacts core
#: cannot read at boot (upstream ``boot.ts``) -- e.g. core.sqlite restored without
#: ``artifacts/``. Only a fresh upload brings it back.
UNREADABLE = "artifact_unreadable"


class CoreSyncError(RuntimeError):
    """A tick could not run at all (bad record, lock held, no previous release)."""


class CorePlanError(RuntimeError):
    """core_plan.mjs could not run (as opposed to one plugin failing to build)."""


class SupervisorError(RuntimeError):
    """The harness answered a control request with ``ok: false``."""


# --------------------------------------------------------------------------- plan


@dataclass(frozen=True)
class PluginPlan:
    """One line of core_plan.mjs output."""

    plugin_id: str
    dir: str = ""
    artifact_id: str = ""
    content_key: str = ""
    path: str = ""
    commit: str | None = None
    dirty: bool = False
    error: str | None = None


_PLAN_REQUIRED = ("artifactId", "contentKey", "path")


def parse_plan_output(text: str) -> list[PluginPlan]:
    """Every JSON-object line that names a ``pluginId``; anything else is ignored.

    A success line missing a required field becomes an error plan rather than a
    half-filled one: shipping ``path=""`` would upload nothing.
    """
    plans: list[PluginPlan] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        pid = obj.get("pluginId") if isinstance(obj, dict) else None
        if not isinstance(pid, str) or not pid:
            continue
        if obj.get("error") is not None:
            plans.append(PluginPlan(plugin_id=pid, error=str(obj["error"])[:2000]))
            continue
        missing = [k for k in _PLAN_REQUIRED if not isinstance(obj.get(k), str) or not obj[k]]
        if missing:
            plans.append(
                PluginPlan(plugin_id=pid, error=f"incomplete plan line: missing {missing}")
            )
            continue
        plans.append(
            PluginPlan(
                plugin_id=pid,
                dir=str(obj.get("dir") or ""),
                artifact_id=obj["artifactId"],
                content_key=obj["contentKey"],
                path=obj["path"],
                commit=obj.get("commit") if isinstance(obj.get("commit"), str) else None,
                dirty=bool(obj.get("dirty")),
            )
        )
    return plans


def core_plan_source() -> Path:
    """The planner asset shipped with ams -- package *data*, never imported."""
    return Path(__file__).resolve().parent / "assets" / CORE_PLAN_ASSET


class Planner(Protocol):
    def __call__(
        self,
        tree: Path,
        out_dir: Path,
        ids: Sequence[str],
        *,
        runner: Runner,
        env: Mapping[str, str],
    ) -> list[PluginPlan]: ...


def _admin(argv: Sequence[str], block: UidBlock) -> None:
    from ams.userns import run_admin

    run_admin(list(argv), block).check()


def make_planner(state: StateDir, layout: CoreLayout, block: UidBlock | None) -> Planner:
    """The real planner: place core_plan.mjs under ``<root>/build`` and run it.

    The asset is copied into the service root because in isolated mode the planner
    runs as the service uid, which cannot read the harness's ams checkout (the
    same reason the pool runner is placed, PLAN-pool §4.1).

    In isolated mode the placement runs **as the service** (the ``runner``), never
    as inner root: ``build/`` is service-owned and core keeps running while a tick
    plans, so a compromised core can plant ``build/core_plan.mjs`` or
    ``build/artifacts`` as a symlink to a harness file. A following ``cp``/``chown``
    as inner root (the harness uid, with CAP_CHOWN) would overwrite that file and
    hand it to the service. As the service, a planted link only reaches what the
    service could write anyway. The bytes travel through a 0644 staging file in the
    harness-owned ``<state>/services/core/`` (traversable by the service, not
    writable by it).
    """

    def place(runner: Runner, argv: Sequence[str], env: Mapping[str, str], what: str) -> None:
        result = runner(list(argv), env=env, cwd=None, timeout_s=60.0)
        if not result.ok:
            tail = result.stderr.decode(errors="replace").strip()[-500:]
            raise CorePlanError(f"{what} failed (rc={result.returncode}): {tail or '(no stderr)'}")

    def plan(
        tree: Path,
        out_dir: Path,
        ids: Sequence[str],
        *,
        runner: Runner,
        env: Mapping[str, str],
    ) -> list[PluginPlan]:
        asset = layout.build / CORE_PLAN_ASSET
        content = core_plan_source().read_bytes()
        if block is None:
            out_dir.mkdir(parents=True, exist_ok=True)
            if not asset.is_file() or asset.read_bytes() != content:
                asset.write_bytes(content)
        else:
            tmp = state.service_dir(CORE_ID) / f".{CORE_PLAN_ASSET}.tmp{os.getpid()}"
            tmp.write_bytes(content)
            try:
                os.chmod(tmp, 0o644)
                place(runner, ["mkdir", "-p", str(out_dir)], env, "mkdir of the artifacts dir")
                place(runner, ["cp", str(tmp), str(asset)], env, f"placing {CORE_PLAN_ASSET}")
            finally:
                tmp.unlink(missing_ok=True)
        argv = ["node", str(asset), str(tree), str(out_dir), *ids]
        started = time.monotonic()
        result = runner(argv, env=env, cwd=str(tree), timeout_s=PLAN_TIMEOUT_S)
        # Complete lines count even when node died afterwards: a crash in one
        # plugin's build must not throw away the others' plans (the ids with no
        # line become build errors in _Tick.plan). No line at all is a failed run.
        plans = parse_plan_output(result.stdout.decode(errors="replace"))
        if not result.ok:
            tail = "\n".join(result.stderr.decode(errors="replace").strip().splitlines()[-15:])
            if not plans:
                raise CorePlanError(
                    f"core_plan.mjs exited {result.returncode}: {tail or '(no stderr)'}"
                )
            log.warning(
                "core_plan.mjs exited %d after %d plan line(s); keeping them: %s",
                result.returncode,
                len(plans),
                tail or "(no stderr)",
            )
        log.info(
            "planned %d plugin(s) in %.1fs (%d error(s))",
            len(plans),
            time.monotonic() - started,
            sum(1 for p in plans if p.error),
        )
        return plans

    return plan


# --------------------------------------------------------------------------- supervisor


class Supervisor(Protocol):
    def reload(self) -> dict[str, Any]: ...
    def start(self, service_id: str) -> dict[str, Any]: ...
    def stop(self, service_id: str) -> dict[str, Any]: ...
    def restart(self, service_id: str) -> dict[str, Any]: ...
    def status(self) -> dict[str, Any]: ...


class SocketSupervisor:
    """The running harness, over ``<state>/control.sock`` (same ops as ``ams ctl``)."""

    def __init__(self, state: StateDir, timeout_s: float = 60.0) -> None:
        self.state = state
        self.timeout_s = timeout_s

    def _call(self, op: str, service_id: str | None = None) -> dict[str, Any]:
        from ams.platform import sync as sync_mod

        if op == "reload":
            response = sync_mod.ctl_reload(self.state, timeout_s=self.timeout_s)
        elif op == "restart" and service_id is not None:
            response = sync_mod.ctl_restart(self.state, service_id, timeout_s=self.timeout_s)
        else:
            from ams.control import control_socket_path
            from ams.control import request as control_request

            response = control_request(
                control_socket_path(self.state), op, service_id, timeout_s=self.timeout_s
            )
        if not response.get("ok"):
            raise SupervisorError(
                f"ctl {op} {service_id or ''}: {response.get('error') or response}"
            )
        log.info("ctl %s %s -> ok", op, service_id or "")
        return response

    def reload(self) -> dict[str, Any]:
        return self._call("reload")

    def start(self, service_id: str) -> dict[str, Any]:
        return self._call("start", service_id)

    def stop(self, service_id: str) -> dict[str, Any]:
        return self._call("stop", service_id)

    def restart(self, service_id: str) -> dict[str, Any]:
        return self._call("restart", service_id)

    def status(self) -> dict[str, Any]:
        return self._call("status")


# --------------------------------------------------------------------------- hooks


def http_probe(port: int, path: str) -> int | None:
    """``GET http://127.0.0.1:<port><path>`` -> status code, ``None`` = nothing answered."""
    url = f"http://127.0.0.1:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=PROBE_TIMEOUT_S) as resp:  # noqa: S310 - loopback
            return int(resp.status)
    except urllib.error.HTTPError as e:
        return int(e.code)
    except (urllib.error.URLError, OSError, ValueError):
        return None


def default_toolchain(store: RuntimeStore, cfg: CoreConfig) -> str:
    """PATH for everything run in a core tree: managed pnpm + node first."""
    from ams.runtime import ensure_node_toolchain

    tc = ensure_node_toolchain(store, cfg.node, cfg.pnpm)
    return ":".join((*tc.bin_dirs, *BASE_PATH))


def default_gateway(state: StateDir, cfg: CoreConfig) -> list[str]:
    """Render + write the Caddy front from ``[[site]]``; the relative paths that changed."""
    from ams.platform import gateway as gateway_mod

    sites = [gateway_mod.CoreSite(host=s.host, port=s.port) for s in cfg.sites]
    gcfg = gateway_mod.GatewayConfig(
        listen_port=cfg.caddy_port, static_root=gateway_mod.static_root(state)
    )
    changed = gateway_mod.write(state, gateway_mod.render_core(sites, gcfg))
    root = gateway_mod.gateway_dir(state)
    return [p.relative_to(root).as_posix() if p.is_relative_to(root) else p.name for p in changed]


def default_block(state: StateDir) -> UidBlock:
    from ams.platform import sync as sync_mod

    return sync_mod.uid_allocator(state).allocate(CORE_ID)


def default_mirror(cfg: CoreConfig, store: RuntimeStore) -> Any:
    from ams.platform.sources import SourceMirror

    return SourceMirror(store.root, cfg.mirror, url=cfg.url)


def default_runner(block: UidBlock | None) -> Runner:
    from ams.platform import corectl

    return corectl.plain_runner() if block is None else corectl.isolated_runner(block)


def default_provision(tree: Path, spec: RuntimeSpec, **kwargs: Any) -> None:
    from ams.runtime import provision_tree

    provision_tree(tree, spec, **kwargs)


@dataclass(frozen=True)
class Hooks:
    """Every side effect of a tick. Defaults (and ``None``) are the real thing."""

    mirror_factory: Callable[[CoreConfig, RuntimeStore], Any] = default_mirror
    #: ``None`` = :class:`CoreControl` over the tick's runner.
    control_factory: Callable[[Path, Runner, str], Any] | None = None
    supervisor_factory: Callable[[StateDir], Supervisor] = SocketSupervisor
    provision: Callable[..., None] = default_provision
    #: ``None`` = :func:`make_planner`.
    planner_factory: Callable[[CoreLayout, UidBlock | None], Planner] | None = None
    http_probe: Callable[[int, str], int | None] = http_probe
    gateway: Callable[[StateDir, CoreConfig], list[str]] = default_gateway
    toolchain: Callable[[RuntimeStore, CoreConfig], str] = default_toolchain
    runner_factory: Callable[[UidBlock | None], Runner] = default_runner
    block_factory: Callable[[StateDir], UidBlock] = default_block


# --------------------------------------------------------------------------- record


def _stamp(now_s: float) -> str:
    return datetime.fromtimestamp(now_s, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _empty_record() -> dict[str, Any]:
    return {
        "version": RECORD_VERSION,
        "staged_sha": None,
        "release_sha": None,
        "previous_release_sha": None,
        "release_failed_sha": None,
        "stage_failed_sha": None,
        "planned_sha": None,
        "planned_roster": [],
        #: plugins whose last ship failed in transport: re-planned alone next tick
        "ship_retry": [],
        #: pid -> live generationId that a failed post-grant restart left running
        "privilege_restart": {},
        "plugins": {},
        "build_failures": {},
        "escalated": {},
    }


class CoreRecord:
    """``<state>/platform/core.json``. Change-gated: :meth:`flush` writes only a diff."""

    def __init__(self, path: Path, data: dict[str, Any], snapshot: str | None) -> None:
        self.path = path
        self.data = data
        self._snapshot = snapshot

    @classmethod
    def load(cls, path: Path) -> CoreRecord:
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return cls(path, _empty_record(), None)
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise StateCorrupt(f"{path}: not valid JSON ({e.msg} at line {e.lineno})") from None
        if not isinstance(data, dict) or data.get("version") != RECORD_VERSION:
            found = data.get("version") if isinstance(data, dict) else None
            raise StateCorrupt(f"{path}: unsupported version {found!r}")
        merged = {**_empty_record(), **data}
        return cls(path, merged, cls._dump(merged))

    @staticmethod
    def _dump(data: Mapping[str, Any]) -> str:
        return json.dumps(dict(data), indent=2, sort_keys=True) + "\n"

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.data[key] = value

    @property
    def plugins(self) -> dict[str, dict[str, Any]]:
        return self.data["plugins"]

    def flush(self) -> bool:
        text = self._dump(self.data)
        if text == self._snapshot:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.tmp{os.getpid()}")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, self.path)
        self._snapshot = text
        log.info("wrote %s", self.path)
        return True


@contextlib.contextmanager
def locked(state: StateDir) -> Iterator[None]:
    """One core operation at a time (timer tick, ``ship``, ``release --rollback``).

    ``flock`` on ``<state>/platform/core.lock``, non-blocking: a second run reports
    and exits instead of queueing behind a tick that may take minutes. No thread.
    """
    path = state.root / core_mod.PLATFORM_DIRNAME / LOCK_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CoreSyncError(f"another core operation is running ({path} is locked)") from None
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# --------------------------------------------------------------------------- report


@dataclass(frozen=True)
class CoreReport:
    sha: str | None = None
    released: bool = False
    release_ok: bool | None = None
    rolled_back: bool = False
    shipped: tuple[str, ...] = ()
    live: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    pending: tuple[str, ...] = ()
    privileges_changed: tuple[str, ...] = ()
    gateway_changed: tuple[str, ...] = ()
    drift: tuple[str, ...] = ()
    escalations: tuple[Mapping[str, Any], ...] = ()
    record_written: bool = False
    error: str | None = None

    @property
    def exit_code(self) -> int:
        """Non-zero iff *this* tick failed something (593daae)."""
        if self.error or self.failed or self.release_ok is False:
            return EXIT_ERROR
        return EXIT_OK

    def summary(self) -> str:
        parts = [f"core {str(self.sha or '-')[:12]}"]
        if self.released:
            parts.append(f"release={'ok' if self.release_ok else 'FAILED'}")
        if self.rolled_back:
            parts.append("rolled back")
        for name in ("shipped", "live", "failed", "pending", "drift", "privileges_changed"):
            value = getattr(self, name)
            if value:
                parts.append(f"{name}={','.join(value)}")
        if self.gateway_changed:
            parts.append(f"gateway changed ({len(self.gateway_changed)})")
        if self.escalations:
            parts.append(f"{len(self.escalations)} escalation(s)")
        parts.append("record written" if self.record_written else "no change")
        if self.error:
            parts.append(f"error: {self.error}")
        return "; ".join(parts)


# --------------------------------------------------------------------------- the tick


@dataclass
class _Outcome:
    shipped: list[str] = field(default_factory=list)
    live: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    drift: list[str] = field(default_factory=list)
    privileges: list[str] = field(default_factory=list)
    gateway: list[str] = field(default_factory=list)
    released: bool = False
    release_ok: bool | None = None
    rolled_back: bool = False
    error: str | None = None


def _plugin_status(status: Mapping[str, Any] | None, plugin_id: str) -> dict[str, Any] | None:
    for p in (status or {}).get("plugins") or []:
        if isinstance(p, dict) and p.get("pluginId") == plugin_id:
            return p
    return None


def _installed(p: Mapping[str, Any] | None) -> bool:
    return bool(p and isinstance(p.get("desired"), dict))


def _active(p: Mapping[str, Any] | None) -> bool:
    """The plugin has a live generation that is serving."""
    live = (p or {}).get("live")
    return isinstance(live, dict) and live.get("phase") == "active"


def _unreadable(p: Mapping[str, Any] | None) -> bool:
    observed = (p or {}).get("observed") or {}
    return observed.get("phase") == "failed" and str(observed.get("reason") or "").startswith(
        UNREADABLE
    )


def _reason_code(reason: Any) -> str:
    """``dependency_unavailable: clock`` -> ``dependency_unavailable``."""
    return str(reason or "").split(":", 1)[0].strip()


def core_fingerprint(status: Mapping[str, Any] | None) -> str:
    """A short digest of what core runs: every plugin's live artifact and phase.

    A ``blocked`` verdict holds while this stays the same, and is retried once
    when it changes (a provider went live, a dependent was removed ...).
    """
    items = sorted(
        f"{p.get('pluginId')}={(p.get('live') or {}).get('artifactId')}"
        f"/{(p.get('observed') or {}).get('phase')}"
        for p in (status or {}).get("plugins") or []
        if isinstance(p, dict)
    )
    return hashlib.sha256("\n".join(items).encode("utf-8")).hexdigest()[:16]


def _touches(paths: Sequence[str], prefixes: Sequence[str]) -> list[str]:
    """Paths matching ``core_paths``: an entry ending in ``/`` is a prefix, else exact."""
    hits = []
    for path in paths:
        for want in prefixes:
            if (want.endswith("/") and path.startswith(want)) or path == want:
                hits.append(path)
                break
    return hits


class _Tick:
    def __init__(
        self,
        state: StateDir,
        store: RuntimeStore,
        cfg: CoreConfig,
        *,
        isolation: bool,
        stream: IO[str],
        now: Callable[[], float],
        sleep: Callable[[float], None],
        hooks: Hooks,
    ) -> None:
        self.state = state
        self.store = store
        self.cfg = cfg
        self.isolation = isolation
        self.stream = stream
        self.now = now
        self.sleep = sleep
        self.hooks = hooks
        self.layout = CoreLayout.from_state(state)
        self.record = CoreRecord.load(core_mod.record_path(state))
        self.escalations: list[dict[str, Any]] = []
        self._observed: set[str] = set()
        self.out = _Outcome()
        self.block: UidBlock | None = None
        self._runner: Runner | None = None
        self._path_env: str | None = None
        self._sup: Supervisor | None = None
        self.head: str | None = None
        self._bundle: str | None = None
        #: Set when the whole tick ran. Only then is "not seen this tick" evidence
        #: that a cause cleared -- a tick that stopped at the fetch never looked.
        self.completed = False

    # ------------------------------------------------------------- plumbing

    @property
    def sup(self) -> Supervisor:
        if self._sup is None:
            self._sup = self.hooks.supervisor_factory(self.state)
        return self._sup

    @property
    def runner(self) -> Runner:
        if self._runner is None:
            self._runner = self.hooks.runner_factory(self.block)
        return self._runner

    @property
    def path_env(self) -> str:
        if self._path_env is None:
            self._path_env = self.hooks.toolchain(self.store, self.cfg)
        return self._path_env

    def control(self, sha: str) -> Any:
        """corectl from the tree of ``sha`` (the one the running core was built from)."""
        tree = self.layout.release_dir(sha)
        if self.hooks.control_factory is None:
            return CoreControl(self.runner, tree, self.layout.socket, self.path_env)
        return self.hooks.control_factory(tree, self.runner, self.path_env)

    def spec(self) -> RuntimeSpec:
        from ams.schema import loads

        return loads(core_mod.core_declaration(self.cfg, self.layout)).runtime

    def provision_log(self) -> Path:
        return self.state.logs_dir / PROVISION_LOG

    def bundle_digest(self) -> str:
        """The config bundle every verdict of this tick is judged under ("" = none)."""
        if self._bundle is None:
            self._bundle = core_mod.bundle_digest(self.state) or ""
        return self._bundle

    # ----------------------------------------------------------- escalation

    def escalate(
        self, kind: str, cause: str, *, plugin: str | None = None, sha: str | None = None
    ) -> None:
        key = cause_key(plugin or CORE_ID, kind, cause)
        self._observed.add(key)
        sha = sha if sha is not None else self.head
        escalated: dict[str, Any] = self.record["escalated"]
        if key in escalated:
            log.info(
                "%s%s still %s; already escalated", CORE_ID, f"/{plugin}" if plugin else "", kind
            )
            return
        escalated[key] = {"kind": kind, "since": _stamp(self.now())}
        record = {
            "kind": ESCALATION_KIND,
            "service_id": CORE_ID,
            "action": "escalate",
            "reason": f"{kind}: {cause}",
            "event": {
                "kind": kind,
                "service": CORE_ID,
                "plugin": plugin,
                "cause": cause,
                "sha": sha,
            },
        }
        self.escalations.append(record)
        self.stream.write(json.dumps(record, default=str) + "\n")
        self.stream.flush()
        log.error("escalating %s%s %s: %s", CORE_ID, f"/{plugin}" if plugin else "", kind, cause)

    def forget_unobserved(self) -> None:
        escalated: dict[str, Any] = self.record["escalated"]
        for key in [k for k in escalated if k not in self._observed]:
            log.info("cause cleared: %s", key)
            del escalated[key]

    def report(self) -> CoreReport:
        o = self.out
        return CoreReport(
            sha=self.head,
            released=o.released,
            release_ok=o.release_ok,
            rolled_back=o.rolled_back,
            shipped=tuple(o.shipped),
            live=tuple(o.live),
            failed=tuple(dict.fromkeys(o.failed)),
            pending=tuple(o.pending),
            privileges_changed=tuple(o.privileges),
            gateway_changed=tuple(o.gateway),
            drift=tuple(o.drift),
            escalations=tuple(self.escalations),
            error=o.error,
        )

    # ------------------------------------------------------------- layout

    def prepare(self) -> None:
        self.state.ensure()
        (self.state.root / core_mod.PLATFORM_DIRNAME).mkdir(parents=True, exist_ok=True)
        if self.isolation:
            from ams.userns import ensure_service_root

            self.block = self.hooks.block_factory(self.state)
            ensure_service_root(self.layout.root, self.block)
        core_mod.ensure_layout(self.layout, self.block)

    # --------------------------------------------------------------- stage

    def stage(self, head: str) -> bool:
        """``True`` when ``head`` is staged and installed.

        A sha whose stage failed is held like a failed release: re-running a
        ``pnpm install`` that fails the same way every 60 s fixes nothing, and a
        new commit tries again. The caller keeps running the rest of the tick
        against the running release either way.
        """
        if head == self.record["staged_sha"]:
            return True
        if head == self.record["stage_failed_sha"]:
            log.info("staging %s failed before; not retried at this sha", head[:12])
            return False
        mirror = self.mirror
        tree = self.layout.release_dir(head)
        try:
            if self.block is None:
                mirror.stage_plain(head, tree)
            else:
                mirror.stage(head, self.layout.root, self.block, dest=f"releases/{head}")
            problems = core_mod.check_tree_pins(tree, self.cfg.node, self.cfg.pnpm)
            if problems:
                raise core_mod.CoreConfigError("; ".join(problems))
            self.hooks.provision(
                tree,
                self.spec(),
                block=self.block,
                store=self.store,
                run_build=False,
                log_path=self.provision_log(),
                env={"CORE_SOURCE_COMMIT": head},
            )
        except Exception as e:  # noqa: BLE001 - SourceError/ProvisionError/SpawnError alike
            self.escalate(K_STAGE, f"{type(e).__name__}: {e}")
            self.out.error = f"stage {head[:12]} failed"
            self.record["stage_failed_sha"] = head
            return False
        self.record["staged_sha"] = head
        self.record["stage_failed_sha"] = None
        log.info("staged + installed %s", head[:12])
        with contextlib.suppress(Exception):
            mirror.gc(keep=MIRROR_KEEP)
        # Every staged tree carries a full node_modules; the one staged before
        # this is garbage now unless it is the release or its rollback target.
        self.gc_releases()
        return True

    # ------------------------------------------------------------- release

    def release_needed(self, head: str) -> bool:
        release = self.record["release_sha"]
        if release is None:
            log.info("no release yet: releasing %s", head[:12])
            return True
        if head == release:
            return False
        if head == self.record["release_failed_sha"]:
            log.info("release of %s failed or was rolled back; not retried at this sha", head[:12])
            return False
        try:
            paths = self.mirror.changed_paths(release, head)
        except Exception as e:  # noqa: BLE001 - SourceError: not knowing is not "nothing"
            log.warning(
                "cannot diff %s..%s (%s); treating core as changed", release[:12], head[:12], e
            )
            return True
        hits = _touches(paths, self.cfg.core_paths)
        if hits:
            log.info("%s touches core paths (%s); releasing", head[:12], ", ".join(hits[:5]))
            return True
        log.info("%s leaves core paths alone; core stays at %s", head[:12], release[:12])
        return False

    def core_state(self) -> str | None:
        services = self.sup.status().get("services") or {}
        svc = services.get(CORE_ID)
        return str(svc.get("status")) if isinstance(svc, dict) else None

    def stop_core(self) -> bool:
        state = self.core_state()
        if state in (None, "stopped", "failed"):
            return True
        self.sup.stop(CORE_ID)
        deadline = self.now() + STOP_WAIT_S
        while self.now() < deadline:
            state = self.core_state()
            if state in (None, "stopped", "failed"):
                log.info("core stopped")
                return True
            self.sleep(STOP_POLL_S)
        return False

    def declare(self) -> bool:
        """Write the declaration; ``True`` when it changed on disk."""
        from ams.platform.sync import _write_if_changed

        text = core_mod.core_declaration(self.cfg, self.layout)
        changed = _write_if_changed(self.state.service_decl_path(CORE_ID), text)
        if changed:
            log.info("core declaration written")
        return changed

    def start_core(self) -> None:
        """Declaration changed -> reload (which adds or restarts it); else start.

        A reload leaves a service that ``ctl stop`` stopped *down* -- the
        operator's intent wins (``ams.reload``) -- and a release stops core
        before it flips ``current``. So after a reload core is started explicitly
        when it is still stopped; not unconditionally, because ``start`` refuses
        a live process (a first declaration is already started by the reload).
        """
        if self.declare():
            self.sup.reload()
            if self.core_state() in ("stopped", "failed"):
                log.info("core declaration changed while core was stopped; starting it")
                self.sup.start(CORE_ID)
            return
        try:
            self.sup.start(CORE_ID)
        except SupervisorError as e:
            if "unknown service" not in str(e):
                raise
            self.sup.reload()

    def active_plugins(self, sha: str | None) -> frozenset[str]:
        """Plugins serving (``live.phase`` active) on the core built from ``sha``."""
        if sha is None:
            return frozenset()
        try:
            status = self.control(sha).status()
        except CoreControlError as e:
            log.warning("cannot list live plugins before the switch: %s", e)
            return frozenset()
        return frozenset(
            str(p["pluginId"])
            for p in status.get("plugins") or []
            if isinstance(p, dict) and isinstance(p.get("pluginId"), str) and _active(p)
        )

    def gate(
        self, sha: str, was_healthy: bool, active_before: frozenset[str] = frozenset()
    ) -> tuple[bool, str]:
        """Core answers on its socket, then HTTP: never worse than before the stop.

        ``/health`` is the ``health`` plugin behind the ``gateway`` plugin, not core
        itself, so a fresh core (no plugins) cannot pass an HTTP gate. The rule is
        therefore "no regression": ``/health`` must be 200 again if it was 200
        before the stop; otherwise every plugin that was serving before the stop
        (``active_before``) must be serving again -- one plugin already red must
        not turn the gate into "the port answers"; with nothing serving before,
        the gateway port must answer HTTP at all if the gateway plugin is
        installed (and readable); otherwise the control socket answering is it.
        """
        ctl = self.control(sha)
        deadline = self.now() + self.cfg.health_timeout_s
        answered = False
        last = "control socket not answering"
        port = self.cfg.gateway_port
        while True:
            status: Mapping[str, Any] | None = None
            try:
                status = ctl.status()
                answered = True
            except CoreControlError as e:
                if not answered:
                    last = f"control socket not answering: {e}"
            if status is not None:
                missing = sorted(
                    pid for pid in active_before if not _active(_plugin_status(status, pid))
                )
                if was_healthy:
                    code = self.hooks.http_probe(port, core_mod.HEALTH_PATH)
                    if code == 200:
                        return True, "/health 200"
                    last = f"/health answered {code}" if code else "gateway not answering"
                elif missing:
                    last = f"serving before the switch, not serving now: {', '.join(missing)}"
                elif active_before:
                    return True, f"all {len(active_before)} plugin(s) serving before serve again"
                else:
                    gw = _plugin_status(status, "gateway")
                    if not (_installed(gw) and gw["desired"].get("enabled", True)):
                        return True, "control socket answers (no gateway installed)"
                    if _unreadable(gw):
                        # Restored core.sqlite without artifacts/: nothing can bind
                        # the port until ship reinstalls the gateway -- which only
                        # runs once this gate has passed.
                        return True, "control socket answers (gateway artifact unreadable)"
                    code = self.hooks.http_probe(port, core_mod.HEALTH_PATH)
                    if code is not None:
                        return True, f"gateway answers ({code})"
                    last = "gateway plugin installed but port not answering"
            if self.harness_gave_up():
                # `failed` is terminal: restart policy exhausted, the harness will not
                # start core again. Waiting out health_timeout_s would only extend the
                # outage before the flip back (seen in the local e2e: ~90 s vs ~15 s).
                return False, f"{last}; the harness gave up on core (status failed)"
            if self.now() >= deadline:
                return False, last
            self.sleep(GATE_INTERVAL_S)

    def harness_gave_up(self) -> bool:
        """``True`` when the harness reports core ``failed`` (it will not restart it).

        A harness that cannot be asked is not evidence either way: ``False``.
        """
        try:
            state = self.core_state()
        except Exception as e:  # noqa: BLE001 - ControlError/SupervisorError alike
            log.debug("gate: cannot ask the harness about core: %s", e)
            return False
        if state == "failed":
            log.warning("gate: the harness reports core failed; not waiting any longer")
            return True
        return False

    def _switch(
        self, sha: str, was_healthy: bool, active_before: frozenset[str] = frozenset()
    ) -> tuple[bool, str]:
        if not self.stop_core():
            return False, "core did not stop"
        core_mod.flip_current(self.layout, sha, self.block)
        self.start_core()
        return self.gate(sha, was_healthy, active_before)

    def release(self, head: str) -> bool:
        try:
            self.core_state()
        except Exception as e:  # noqa: BLE001 - ControlError/SupervisorError
            # Not a verdict on this sha: retried next tick, once the harness is up.
            self.escalate(K_RELEASE, f"harness control socket: {type(e).__name__}: {e}")
            self.out.error = "harness not reachable"
            return False
        self.out.released = True
        tree = self.layout.release_dir(head)
        prev = self.record["release_sha"]
        try:
            self.hooks.provision(
                tree,
                self.spec(),
                block=self.block,
                store=self.store,
                run_build=True,
                log_path=self.provision_log(),
                env={"CORE_SOURCE_COMMIT": head},
            )
            core_mod.place_bundle(self.state, self.layout, self.block)
        except Exception as e:  # noqa: BLE001 - ProvisionError/SpawnError/OSError alike
            return self._release_failed(head, f"build: {type(e).__name__}: {e}", flipped=False)
        was_healthy = (
            prev is not None
            and self.hooks.http_probe(self.cfg.gateway_port, core_mod.HEALTH_PATH) == 200
        )
        active_before = self.active_plugins(prev)
        log.info(
            "releasing %s (previous %s, /health before: %s, %d plugin(s) serving)",
            head[:12],
            str(prev)[:12],
            "200" if was_healthy else "not 200",
            len(active_before),
        )
        try:
            ok, detail = self._switch(head, was_healthy, active_before)
        except Exception as e:  # noqa: BLE001 - supervisor/transport errors
            return self._release_failed(
                head,
                f"{type(e).__name__}: {e}",
                flipped=True,
                was_healthy=was_healthy,
                active_before=active_before,
            )
        if not ok:
            return self._release_failed(
                head,
                f"health gate: {detail}",
                flipped=True,
                was_healthy=was_healthy,
                active_before=active_before,
            )
        self.record["previous_release_sha"] = prev
        self.record["release_sha"] = head
        self.record["release_failed_sha"] = None
        self.out.release_ok = True
        log.info("released %s (%s)", head[:12], detail)
        self.gc_releases()
        return True

    def _release_failed(
        self,
        head: str,
        detail: str,
        *,
        flipped: bool,
        was_healthy: bool = False,
        active_before: frozenset[str] = frozenset(),
    ) -> bool:
        self.out.release_ok = False
        prev = self.record["release_sha"]
        if prev is not None:
            # Held: a sha that failed next to a working release is not retried.
            # A first release is: nothing runs yet, so a retry costs one start,
            # and a fresh host that hit an environment problem heals by itself.
            self.record["release_failed_sha"] = head
        self.escalate(K_RELEASE, f"release {head[:12]}: {detail}")
        if not flipped:
            return False
        if prev is None:
            self.escalate(K_DOWN, "the first release failed; there is no release to flip back to")
            return False
        try:
            ok, back = self._switch(prev, was_healthy, active_before)
        except Exception as e:  # noqa: BLE001
            ok, back = False, f"{type(e).__name__}: {e}"
        self.out.rolled_back = True
        if ok:
            log.warning("flipped back to %s (%s)", prev[:12], back)
        else:
            self.escalate(K_DOWN, f"flip back to {prev[:12]} failed too: {back}")
        return False

    def gc_releases(self) -> None:
        """Remove release trees nothing points at: keep release, previous, staged."""
        keep = {self.record[k] for k in ("release_sha", "previous_release_sha", "staged_sha")}
        self.gc_dir(self.layout.releases, keep)

    def gc_dir(self, parent: Path, keep: set[str | None]) -> None:
        """Delete ``<parent>/<sha>`` entries not in ``keep``. Best effort, logged."""
        try:
            if self.block is None:
                names = [p.name for p in parent.iterdir()] if parent.is_dir() else []
            else:
                from ams.userns import run_admin

                # A 0750 service-owned directory: only listable from the admin ns.
                res = run_admin(
                    ["find", str(parent), "-mindepth", "1", "-maxdepth", "1", "-printf", "%f\\n"],
                    self.block,
                )
                names = res.stdout.decode(errors="replace").split() if res.ok else []
        except Exception as e:  # noqa: BLE001 - gc is best effort
            log.warning("gc: cannot list %s: %s", parent, e)
            return
        for name in names:
            if name in keep or not core_mod.SHA_RE.match(name):
                continue
            path = parent / name
            log.info("gc: removing %s", path)
            try:
                if self.block is None:
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    _admin(["rm", "-rf", str(path)], self.block)
            except Exception as e:  # noqa: BLE001 - gc is best effort
                log.warning("gc of %s failed: %s", path, e)

    # ------------------------------------------------------------ status

    def core_status(self) -> dict[str, Any] | None:
        release = self.record["release_sha"]
        if release is None:
            return None
        try:
            return self.control(release).status()
        except CoreControlError as e:
            self.escalate(K_UNREACHABLE, f"corectl status: {e}")
            self.out.error = "core unreachable"
            return None

    def resolve_pending(self, status: Mapping[str, Any]) -> None:
        for pid, rec in self.record.plugins.items():
            if rec.get("outcome") != PROBATION:
                continue
            verdict = self._verdict(_plugin_status(status, pid), rec.get("artifact_id", ""))
            if verdict is None:
                self.out.pending.append(pid)
            else:
                self._settle(pid, rec, verdict)

    def check_drift(self, status: Mapping[str, Any]) -> None:
        for pid in self.cfg.plugins:
            rec = self.record.plugins.get(pid)
            if not rec or rec.get("outcome") != LIVE:
                continue
            p = _plugin_status(status, pid)
            if not _installed(p):
                continue  # missing: re-shipped by the plan below
            desired = p["desired"]
            if desired.get("artifactId") != rec.get("artifact_id"):
                self.out.drift.append(pid)
                self.escalate(
                    K_DRIFT,
                    f"core wants artifact {str(desired.get('artifactId'))[:12]} but ams last "
                    f"shipped {str(rec.get('artifact_id'))[:12]} (manual deploy or revert?); "
                    "left alone until the plugin's content changes",
                    plugin=pid,
                )
            elif desired.get("enabled") is False:
                self.out.drift.append(pid)
                self.escalate(K_DRIFT, "disabled in core by hand; left alone", plugin=pid)

    # -------------------------------------------------------------- plan

    def retry_reason(self, rec: Mapping[str, Any], status: Mapping[str, Any] | None) -> str | None:
        """Why a content key that did not go live may be tried once more, or ``None``.

        A verdict holds for the bytes *under the conditions it was reached in*:
        the core release, the config bundle, and -- for a ``blocked`` rejection
        about core's state -- what core was running. A change to any of them is a
        new experiment, tried once; an unchanged one is never retried (no storm).
        Records written before these fields existed are never retried.
        """
        outcome = rec.get("outcome")
        if outcome not in (FAILED, BLOCKED):
            return None
        release = self.record["release_sha"]
        if "release" in rec and rec["release"] != release:
            return f"core release changed ({_short(rec['release'])} -> {_short(release)})"
        if "bundle" in rec and rec["bundle"] != self.bundle_digest():
            return "config bundle changed"
        if outcome == BLOCKED and rec.get("blocked_on") != core_fingerprint(status):
            return "core's installed plugins changed"
        return None

    def plan_ids(self, head: str, status: Mapping[str, Any]) -> list[str]:
        """Which roster plugins to build this tick; ``[]`` = none.

        The whole roster on a new head or roster; otherwise only the plugins that
        need another look: a transport failure last tick, a plugin recorded live
        that core no longer has or cannot read, and a failed or blocked verdict
        whose conditions changed (:meth:`retry_reason`).
        """
        if head != self.record["planned_sha"]:
            return list(self.cfg.plugins)
        if list(self.cfg.plugins) != list(self.record["planned_roster"]):
            log.info("roster changed; re-planning")
            return list(self.cfg.plugins)
        retry = set(self.record["ship_retry"])
        ids: list[str] = []
        for pid in self.cfg.plugins:
            rec = self.record.plugins.get(pid) or {}
            p = _plugin_status(status, pid)
            if pid in retry:
                why: str | None = "its last ship failed in transport"
            elif rec.get("outcome") == LIVE and not _installed(p):
                why = "recorded live but not installed in core"
            elif _installed(p) and _unreadable(p):
                why = "core cannot read its artifact"
            else:
                why = self.retry_reason(rec, status)
            if why:
                log.info("%s: re-planning at %s: %s", pid, head[:12], why)
                ids.append(pid)
        return ids

    def plan(self, head: str, ids: Sequence[str]) -> list[PluginPlan] | None:
        factory = self.hooks.planner_factory
        planner = (
            factory(self.layout, self.block)
            if factory is not None
            else make_planner(self.state, self.layout, self.block)
        )
        env = {
            "PATH": self.path_env,
            "LANG": "C.UTF-8",
            "HOME": str(self.layout.root),
            "CORE_SOURCE_COMMIT": head,
        }
        try:
            plans = planner(
                self.layout.release_dir(head),
                self.layout.artifacts_dir(head),
                list(ids),
                runner=self.runner,
                env=env,
            )
        except Exception as e:  # noqa: BLE001 - CorePlanError, runner/spawn errors
            self.escalate(K_PLAN, f"{type(e).__name__}: {e}")
            self.out.error = "plan failed"
            return None
        by_id = {p.plugin_id: p for p in plans}
        missing = [pid for pid in ids if pid not in by_id]
        for pid in missing:
            by_id[pid] = PluginPlan(plugin_id=pid, error="core_plan.mjs printed no line for it")
        return [by_id[pid] for pid in ids]

    # -------------------------------------------------------------- ship

    def ship_set(
        self,
        head: str,
        plans: Sequence[PluginPlan],
        status: Mapping[str, Any] | None,
        *,
        force: bool = False,
    ) -> list[PluginPlan]:
        out: list[PluginPlan] = []
        failures: dict[str, str] = self.record["build_failures"]
        for plan in plans:
            pid = plan.plugin_id
            if plan.error:
                self.out.failed.append(pid)
                if failures.get(pid) != head:
                    failures[pid] = head
                self.escalate(K_BUILD, f"build at {head[:12]}: {plan.error}", plugin=pid)
                continue
            failures.pop(pid, None)
            rec = self.record.plugins.get(pid)
            if force or not rec or rec.get("content_key") != plan.content_key:
                out.append(plan)
                continue
            p = _plugin_status(status, pid)
            if _installed(p) and _unreadable(p):
                log.warning("%s: core cannot read its artifact; reinstalling", pid)
                out.append(plan)
                continue
            if rec.get("outcome") in (FAILED, BLOCKED):
                why = self.retry_reason(rec, status)
                if why:
                    log.info(
                        "%s: content %s was %s, but %s; trying once more",
                        pid,
                        plan.content_key[:12],
                        rec.get("outcome"),
                        why,
                    )
                    out.append(plan)
                    continue
                log.info(
                    "%s: content %s already %s; not retried",
                    pid,
                    plan.content_key[:12],
                    rec.get("outcome"),
                )
                continue
            if rec.get("outcome") == LIVE and not _installed(p):
                log.warning("%s: live in the record but not installed in core; reinstalling", pid)
                out.append(plan)
                continue
            log.debug("%s: content unchanged (%s)", pid, plan.content_key[:12])
        return out

    def _entry(self, plan: PluginPlan, artifact_id: str, head: str) -> dict[str, Any]:
        """The record entry of one attempt, with the conditions it was judged under."""
        return {
            "content_key": plan.content_key,
            "artifact_id": artifact_id,
            "sha": head,
            "release": self.record["release_sha"],
            "bundle": self.bundle_digest(),
            "at": _stamp(self.now()),
        }

    def ship(
        self,
        ctl: Any,
        head: str,
        plans: Sequence[PluginPlan],
        status: Mapping[str, Any] | None = None,
    ) -> bool:
        """Upload + deploy each, then wait out probation. ``True`` = no transport failure.

        Every outcome that is about the plugin is recorded under its content key:
        a deploy that went ok (probation), a rejected or failed one, and core
        refusing the artifact bytes at upload (``ARTIFACT_REFUSALS``). A rejection
        about core's state (``STATE_REJECTIONS``) is recorded ``blocked`` with the
        :func:`core_fingerprint` of ``status``. Only a transport failure is left
        unrecorded -- listed in ``ship_retry`` so the next tick re-plans just it.
        """
        fingerprint = core_fingerprint(status)
        retry = set(self.record["ship_retry"])
        transport: list[str] = []
        deployed: dict[str, str] = {}
        for plan in plans:
            pid = plan.plugin_id
            retry.discard(pid)
            try:
                artifact_id = ctl.upload(Path(plan.path))
                if artifact_id != plan.artifact_id:
                    log.warning(
                        "%s: core stored %s, core_plan built %s",
                        pid,
                        artifact_id[:12],
                        plan.artifact_id[:12],
                    )
                transition = ctl.deploy(artifact_id)
            except CoreControlError as e:
                if e.code in ARTIFACT_REFUSALS:
                    reason = f"core refused the artifact: {e}"[:1000]
                    self.record.plugins[pid] = {
                        **self._entry(plan, plan.artifact_id, head),
                        "outcome": FAILED,
                        "reason": reason,
                    }
                    self.out.failed.append(pid)
                    self.escalate(K_REJECTED, reason, plugin=pid)
                    continue
                self._transport_failed(pid, e, transport)
                continue
            except (OSError, ValueError) as e:
                self._transport_failed(pid, e, transport)
                continue
            self.out.shipped.append(pid)
            outcome = transition.get("outcome")
            entry = self._entry(plan, str(transition.get("toArtifact") or artifact_id), head)
            if outcome == "ok":
                self.record.plugins[pid] = {**entry, "outcome": PROBATION, "reason": None}
                deployed[pid] = entry["artifact_id"]
                log.info("%s: deployed %s; in probation", pid, entry["artifact_id"][:12])
                self.grant_now(ctl, pid)
                continue
            reason = f"deploy {outcome}: {transition.get('reason')}"
            if _reason_code(transition.get("reason")) in STATE_REJECTIONS:
                self.record.plugins[pid] = {
                    **entry,
                    "outcome": BLOCKED,
                    "reason": reason,
                    "blocked_on": fingerprint,
                }
                log.warning("%s: %s; retried once core's installed plugins change", pid, reason)
            else:
                self.record.plugins[pid] = {**entry, "outcome": FAILED, "reason": reason}
            self.out.failed.append(pid)
            self.escalate(K_REJECTED, reason, plugin=pid)
        self.record["ship_retry"] = sorted(retry | set(transport))
        shipped_any = bool(deployed)
        self.wait_probation(ctl, deployed)
        if shipped_any:
            self.collect_garbage(ctl)
        return not transport

    def _transport_failed(self, pid: str, e: Exception, transport: list[str]) -> None:
        self.escalate(K_SHIP, f"ship: {type(e).__name__}: {e}", plugin=pid)
        self.out.failed.append(pid)
        transport.append(pid)

    def collect_garbage(self, ctl: Any) -> None:
        """``corectl gc`` after a ship: artifacts nothing references any more.

        Best effort. Without it every content change adds an artifact for good,
        and upstream ``corectl deploy`` reads and hashes the whole store first.
        """
        try:
            ctl.gc()
        except (CoreControlError, ValueError) as e:
            log.warning("corectl gc failed (ignored): %s", e)

    def wait_probation(self, ctl: Any, deployed: dict[str, str]) -> None:
        started = self.now()
        while deployed and self.now() - started < self.cfg.probation_timeout_s:
            self.sleep(POLL_INTERVAL_S)
            try:
                status = ctl.status()
            except CoreControlError as e:
                log.warning("status during probation failed: %s", e)
                continue
            for pid in list(deployed):
                verdict = self._verdict(_plugin_status(status, pid), deployed[pid], ctl=ctl)
                if verdict is None:
                    continue
                del deployed[pid]
                self._settle(pid, self.record.plugins[pid], verdict)
        for pid in deployed:
            log.warning(
                "%s: probation still running after %.0fs", pid, self.cfg.probation_timeout_s
            )
            self.out.pending.append(pid)

    def _verdict(
        self, p: Mapping[str, Any] | None, artifact_id: str, *, ctl: Any = None
    ) -> tuple[bool, str] | None:
        """``None`` = still in probation; else (live?, why)."""
        observed = (p or {}).get("observed") or {}
        phase = observed.get("phase")
        if phase == PROBATION:
            return None
        if (
            phase == "active"
            and observed.get("artifactId") == artifact_id
            and not observed.get("reason")
        ):
            return True, "active"
        return False, (
            f"phase {phase}, artifact {str(observed.get('artifactId'))[:12]}"
            f"{', reason: ' + str(observed['reason']) if observed.get('reason') else ''}"
        )

    def _settle(self, pid: str, rec: dict[str, Any], verdict: tuple[bool, str]) -> None:
        live, why = verdict
        if live:
            rec.update(outcome=LIVE, reason=None)
            self.out.live.append(pid)
            log.info("%s: live on %s", pid, str(rec.get("artifact_id"))[:12])
            return
        evidence = self._evidence(pid)
        reason = f"not live after deploy: {why}{evidence}"
        rec.update(outcome=FAILED, reason=reason[:1000])
        self.out.failed.append(pid)
        self.escalate(K_NOT_LIVE, reason, plugin=pid, sha=rec.get("sha"))

    def _evidence(self, pid: str) -> str:
        release = self.record["release_sha"]
        if release is None:
            return ""
        try:
            ctl = self.control(release)
            trans = ctl.transitions(pid, limit=3)
            fails = ctl.failures(pid, limit=3)
        except (CoreControlError, ValueError) as e:
            return f"; (no evidence: {e})"
        t = "; ".join(
            f"{x.get('kind')} {x.get('outcome')}" + (f": {x['reason']}" if x.get("reason") else "")
            for x in trans
        )
        f = "; ".join(f"{x.get('class')}: {x.get('message')}" for x in fails)
        return f"; transitions: {t or '-'}; failures: {f or '-'}"

    # -------------------------------------------------------- privileges

    def _restart_for_privileges(self, ctl: Any, pid: str, p: Mapping[str, Any] | None) -> bool:
        """Restart ``pid`` so a new generation holds its privileges. ``True`` = ok.

        corectl returns a failed or rejected restart as a *result*: the old
        generation -- started without the privileges, so without an ``ops`` port
        (upstream ``Manager.host``) -- keeps running. That is escalated, and the
        generation it left running is remembered so a later tick restarts again
        while it is still the live one.
        """
        before = ((p or {}).get("live") or {}).get("generationId")
        transition = ctl.restart(pid)
        outcome = transition.get("outcome") if isinstance(transition, dict) else None
        pending: dict[str, Any] = self.record["privilege_restart"]
        if outcome != "ok":
            pending[pid] = before
            self.escalate(
                K_PRIVILEGES,
                f"restart after granting privileges: {outcome}: "
                f"{(transition or {}).get('reason')}; the running generation lacks them",
                plugin=pid,
            )
            return False
        pending.pop(pid, None)
        return True

    def grant_now(self, ctl: Any, pid: str) -> None:
        """Right after a deploy: give a privileged plugin its privileges *before*
        probation judges it.

        core only accepts privileges for a plugin it knows (``unknown_plugin``
        otherwise), so a first install always starts without them -- and ``health``
        without ``ops.read`` answers every ``/health`` probe (the harness's own, every
        10 s) with a 503 fault, fails probation and is left with no revert target.
        Seen in the first local end-to-end run. The restart makes the generation
        that holds them, with a fresh probation.
        """
        wanted = self.cfg.privileges.get(pid)
        if wanted is None:
            return
        try:
            p = _plugin_status(ctl.status(), pid)
            if not _installed(p) or sorted(p["desired"].get("privileges") or []) == sorted(wanted):
                return
            ctl.privileges(pid, list(wanted))
            if not self._restart_for_privileges(ctl, pid, p):
                return
        except (CoreControlError, ValueError) as e:
            self.escalate(K_PRIVILEGES, f"{type(e).__name__}: {e}", plugin=pid)
            return
        self.out.privileges.append(pid)
        log.info("%s: privileges %s granted before probation", pid, ",".join(wanted))

    def apply_privileges(self, ctl: Any) -> None:
        if not self.cfg.privileges:
            return
        try:
            status = ctl.status()
        except CoreControlError as e:
            log.warning("privileges skipped: %s", e)
            return
        pending: dict[str, Any] = self.record["privilege_restart"]
        for pid in [k for k in pending if k not in self.cfg.privileges]:
            del pending[pid]
        for pid, wanted in self.cfg.privileges.items():
            p = _plugin_status(status, pid)
            if not _installed(p):
                log.info("privileges for %s deferred: not installed in core", pid)
                continue
            current = sorted(p["desired"].get("privileges") or [])
            if current == sorted(wanted):
                if pid not in pending:
                    continue
                live_gen = (p.get("live") or {}).get("generationId")
                if not p.get("live") or live_gen != pending[pid]:
                    # A newer generation started since (with the privileges).
                    log.info("%s: a new generation holds its privileges", pid)
                    del pending[pid]
                    continue
                log.info("%s: generation %s predates its privileges; restarting", pid, live_gen)
                try:
                    if self._restart_for_privileges(ctl, pid, p):
                        self.out.privileges.append(pid)
                except (CoreControlError, ValueError) as e:
                    self.escalate(K_PRIVILEGES, f"{type(e).__name__}: {e}", plugin=pid)
                continue
            try:
                ctl.privileges(pid, list(wanted))
                # Privileges bind at generation start: a running plugin needs a new one.
                if p.get("live") and not self._restart_for_privileges(ctl, pid, p):
                    continue
            except (CoreControlError, ValueError) as e:
                self.escalate(K_PRIVILEGES, f"{type(e).__name__}: {e}", plugin=pid)
                continue
            self.out.privileges.append(pid)

    # ----------------------------------------------------------- gateway

    def gateway(self) -> None:
        if not self.cfg.sites:
            return
        try:
            changed = self.hooks.gateway(self.state, self.cfg)
        except Exception as e:  # noqa: BLE001 - GatewayError/OSError
            self.escalate(K_GATEWAY, f"{type(e).__name__}: {e}")
            return
        self.out.gateway = list(changed)
        if not changed:
            return
        from ams.platform.gateway import CADDY_SERVICE_ID

        if not self.state.service_decl_path(CADDY_SERVICE_ID).is_file():
            log.info("gateway config changed; caddy is not declared (ams platform core bootstrap)")
            return
        try:
            self.sup.restart(CADDY_SERVICE_ID)
        except Exception as e:  # noqa: BLE001
            self.escalate(K_GATEWAY, f"restart caddy: {type(e).__name__}: {e}")

    # ---------------------------------------------------------- mirror

    @property
    def mirror(self) -> Any:
        if not hasattr(self, "_mirror"):
            self._mirror = self.hooks.mirror_factory(self.cfg, self.store)
        return self._mirror


def _resolve_hooks(
    hooks: Hooks | None,
    control_factory: Callable[[Path, Runner, str], Any] | None,
    mirror_factory: Callable[[CoreConfig, RuntimeStore], Any] | None,
) -> Hooks:
    h = hooks or Hooks()
    if control_factory is not None:
        h = replace(h, control_factory=control_factory)
    if mirror_factory is not None:
        h = replace(h, mirror_factory=mirror_factory)
    return h


def _run(
    state: StateDir,
    store: RuntimeStore,
    cfg: CoreConfig,
    body: Callable[[_Tick], None],
    *,
    isolation: bool,
    escalation: IO[str] | None,
    now: Callable[[], float],
    sleep: Callable[[float], None],
    hooks: Hooks,
    what: str,
) -> CoreReport:
    """Lock, load, run ``body``, forget cleared causes, flush. Never raises."""
    stream = escalation if escalation is not None else sys.stdout
    try:
        with locked(state):
            try:
                t = _Tick(
                    state,
                    store,
                    cfg,
                    isolation=isolation,
                    stream=stream,
                    now=now,
                    sleep=sleep,
                    hooks=hooks,
                )
            except StateCorrupt as e:
                log.error("%s: %s", what, e)
                return CoreReport(error=str(e))
            try:
                body(t)
            except CoreSyncError as e:
                t.out.error = str(e)
            except Exception as e:  # noqa: BLE001 - one tick must never crash the timer
                log.exception("%s crashed", what)
                t.escalate(K_ERROR, f"{what}: {type(e).__name__}: {e}")
                t.out.error = f"{type(e).__name__}: {e}"
            if t.completed:
                t.forget_unobserved()
            written = t.record.flush()
            report = replace(t.report(), record_written=written)
            log.info("%s done: %s", what, report.summary())
            return report
    except CoreSyncError as e:
        log.error("%s: %s", what, e)
        return CoreReport(error=str(e))


def _tick_body(t: _Tick) -> None:
    t.prepare()
    try:
        head = t.mirror.fetch(t.cfg.ref)
    except Exception as e:  # noqa: BLE001 - SourceError/OSError
        t.escalate(K_FETCH, f"{type(e).__name__}: {e}")
        t.out.error = "fetch failed"
        return
    t.head = head
    log.info("%s -> %s", t.cfg.ref, head[:12])

    # Plugins are built from the head tree and judged by the running core, so
    # they only ship while that core is head's core (or head leaves core alone).
    ship_ok = True
    if not t.stage(head):
        log.warning("%s is not staged; running the tick against the current release", head[:12])
        ship_ok = False
    elif t.release_needed(head):
        if not t.release(head):
            log.warning("release failed; plugins are not shipped this tick")
            t.gateway()
            return
    elif head == t.record["release_failed_sha"] and head != t.record["release_sha"]:
        log.info(
            "core at %s is held; plugins built from it are not shipped onto core %s",
            head[:12],
            str(t.record["release_sha"])[:12],
        )
        ship_ok = False
    if not t.out.released and t.record["release_sha"] is not None:
        if t.declare():
            # A config change (memory_max, log level, ports) with no new core code.
            log.info("core declaration changed without a release; reloading")
            t.sup.reload()
        if core_mod.place_bundle(t.state, t.layout, t.block):
            # The secrets plugin re-reads plugins.json on mtime; a plugin picks up
            # its new section with its next generation (upstream docs/core/plugins.md).
            log.warning(
                "config bundle changed: 'corectl restart <plugin>' for the affected plugins"
            )

    status = t.core_status()
    if status is None:
        t.gateway()
        return
    t.resolve_pending(status)
    t.check_drift(status)

    ctl = t.control(t.record["release_sha"])
    ids = t.plan_ids(head, status) if ship_ok else []
    if ids:
        plans = t.plan(head, ids)
        if plans is not None:
            # Planned ids leave ship_retry; ship() puts transport failures back.
            t.record["ship_retry"] = sorted(set(t.record["ship_retry"]) - set(ids))
            t.ship(ctl, head, t.ship_set(head, plans, status), status)
            # Every outcome that is about a plugin is recorded by now, so head
            # is planned even when some failed: a failure is not re-planned
            # every tick, only a transport failure (ship_retry) is re-shipped.
            t.record["planned_sha"] = head
            t.record["planned_roster"] = list(t.cfg.plugins)
    t.apply_privileges(ctl)
    t.gateway()
    t.gc_dir(t.layout.build / "artifacts", {head, t.record["planned_sha"]})
    t.completed = True


def tick(
    state: StateDir,
    store: RuntimeStore,
    cfg: CoreConfig,
    *,
    isolation: bool = True,
    escalation: IO[str] | None = None,
    now: Callable[[], float] = time.time,
    control_factory: Callable[[Path, Runner, str], Any] | None = None,
    mirror_factory: Callable[[CoreConfig, RuntimeStore], Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    hooks: Hooks | None = None,
) -> CoreReport:
    """One core-mode tick (the module docstring's ten steps). Never raises."""
    return _run(
        state,
        store,
        cfg,
        _tick_body,
        isolation=isolation,
        escalation=escalation,
        now=now,
        sleep=sleep,
        hooks=_resolve_hooks(hooks, control_factory, mirror_factory),
        what="core tick",
    )


def rollback_release(
    state: StateDir,
    store: RuntimeStore,
    cfg: CoreConfig,
    *,
    isolation: bool = True,
    escalation: IO[str] | None = None,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    hooks: Hooks | None = None,
) -> CoreReport:
    """Flip ``current`` back to ``previous_release_sha``, gated like a release.

    The sha rolled back *from* becomes ``release_failed_sha``, so the timer does not
    release it again on its next tick; a new commit releases normally.
    """

    def body(t: _Tick) -> None:
        t.prepare()
        prev, cur = t.record["previous_release_sha"], t.record["release_sha"]
        t.head = cur
        if not prev or not cur:
            raise CoreSyncError("no previous release to roll back to")
        t.out.released = True
        was_healthy = t.hooks.http_probe(cfg.gateway_port, core_mod.HEALTH_PATH) == 200
        active_before = t.active_plugins(cur)
        ok, detail = t._switch(prev, was_healthy, active_before)
        if not ok:
            t.escalate(K_RELEASE, f"rollback to {prev[:12]}: {detail}")
            back_ok, back = t._switch(cur, was_healthy, active_before)
            if not back_ok:
                t.escalate(K_DOWN, f"flip forward to {cur[:12]} failed too: {back}")
            t.out.release_ok = False
            return
        t.record["release_sha"] = prev
        t.record["previous_release_sha"] = cur
        t.record["release_failed_sha"] = cur
        t.out.release_ok = True
        t.out.rolled_back = True
        log.info("rolled back %s -> %s (%s)", cur[:12], prev[:12], detail)

    return _run(
        state,
        store,
        cfg,
        body,
        isolation=isolation,
        escalation=escalation,
        now=now,
        sleep=sleep,
        hooks=hooks or Hooks(),
        what="core rollback",
    )


def ship(
    state: StateDir,
    store: RuntimeStore,
    cfg: CoreConfig,
    plugin_ids: Sequence[str],
    *,
    force: bool = False,
    isolation: bool = True,
    escalation: IO[str] | None = None,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    hooks: Hooks | None = None,
) -> CoreReport:
    """Ship roster plugins from the staged tree now; ``force`` ignores the content-key record."""

    def body(t: _Tick) -> None:
        unknown = [pid for pid in plugin_ids if pid not in cfg.plugins]
        if unknown:
            raise CoreSyncError(f"not in the roster (core.plugins): {', '.join(unknown)}")
        t.prepare()
        head, release = t.record["staged_sha"], t.record["release_sha"]
        t.head = head
        if not head or not release:
            raise CoreSyncError("nothing staged or released yet; run 'ams platform core sync'")
        status = t.core_status()
        if status is None:
            return
        ordered = [pid for pid in cfg.plugins if pid in plugin_ids]
        plans = t.plan(head, ordered)
        if plans is None:
            return
        t.ship(t.control(release), head, t.ship_set(head, plans, status, force=force), status)

    return _run(
        state,
        store,
        cfg,
        body,
        isolation=isolation,
        escalation=escalation,
        now=now,
        sleep=sleep,
        hooks=hooks or Hooks(),
        what="core ship",
    )


def bootstrap(
    state: StateDir,
    store: RuntimeStore,
    cfg: CoreConfig,
    *,
    isolation: bool = True,
    escalation: IO[str] | None = None,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    hooks: Hooks | None = None,
) -> CoreReport:
    """Declare the Caddy front (fixed ``caddy_port``) and run the first tick.

    The gateway files are written *before* Caddy is declared, so its first start
    already finds a Caddyfile. Core itself is declared by the tick's first release
    (after ``current`` exists, so the reload that starts it finds its workdir).
    """
    h = hooks or Hooks()
    if cfg.sites:
        from ams.platform import layer0
        from ams.platform.gateway import CADDY_SERVICE_ID
        from ams.platform.sync import _write_if_changed

        state.ensure()
        try:
            h.gateway(state, cfg)
        except Exception as e:  # noqa: BLE001
            log.error("bootstrap: gateway render failed: %s", e)
            return CoreReport(error=f"gateway: {type(e).__name__}: {e}")
        caddy_bin = store.root / "bin" / "caddy"
        if not caddy_bin.is_file():
            log.warning(
                "bootstrap: %s is missing; caddy will not start (deploy/install-host.sh)", caddy_bin
            )
        text = layer0._caddy_declaration_text(state, store, cfg.caddy_port)
        if _write_if_changed(state.service_decl_path(CADDY_SERVICE_ID), text):
            log.info("bootstrap: caddy declared on %d", cfg.caddy_port)
            try:
                h.supervisor_factory(state).reload()
            except Exception as e:  # noqa: BLE001 - harness not running is reported, not fatal
                log.warning("bootstrap: reload after declaring caddy failed: %s", e)
    return tick(
        state,
        store,
        cfg,
        isolation=isolation,
        escalation=escalation,
        now=now,
        sleep=sleep,
        hooks=h,
    )


# --------------------------------------------------------------------------- status


def live_status(
    state: StateDir,
    store: RuntimeStore,
    cfg: CoreConfig,
    record: Mapping[str, Any],
    *,
    isolation: bool = True,
    hooks: Hooks | None = None,
) -> dict[str, Any] | None:
    """``corectl status`` from the released tree, or ``None`` when that is not possible.

    Read only and side-effect free: no lock, no toolchain download (the managed
    node must already exist), no record write. For ``ams platform core status``.
    """
    from ams.runtime import node_toolchain

    release = record.get("release_sha")
    if not release:
        return None
    tc = node_toolchain(store, cfg.node, cfg.pnpm)
    if not tc.node_bin.is_file():
        log.info("managed node %s not installed yet; core status not queried", cfg.node)
        return None
    h = hooks or Hooks()
    block = h.block_factory(state) if isolation else None
    runner = h.runner_factory(block)
    path_env = ":".join((*tc.bin_dirs, *BASE_PATH))
    tree = CoreLayout.from_state(state).release_dir(release)
    ctl = (
        CoreControl(runner, tree, CoreLayout.from_state(state).socket, path_env)
        if h.control_factory is None
        else h.control_factory(tree, runner, path_env)
    )
    try:
        return ctl.status()
    except CoreControlError as e:
        log.warning("core status: %s", e)
        return None


def _short(value: Any, n: int = 12) -> str | None:
    return str(value)[:n] if value else None


def status_view(
    record: Mapping[str, Any],
    live: Mapping[str, Any] | None,
    roster: Sequence[str],
    *,
    queried: bool = True,
) -> dict[str, Any]:
    """One JSON-able view of the record joined with core's own status.

    Rows: the roster in order, then anything else core or the record knows
    about (a plugin installed by hand is still shown). ``drift`` is "ams last
    shipped X live, core now wants Y".
    """
    rec_plugins: Mapping[str, Any] = record.get("plugins") or {}
    live_plugins = {
        p["pluginId"]: p
        for p in (live or {}).get("plugins") or []
        if isinstance(p, dict) and isinstance(p.get("pluginId"), str)
    }
    ids = list(dict.fromkeys([*roster, *sorted(live_plugins), *sorted(rec_plugins)]))
    rows = []
    for pid in ids:
        p = live_plugins.get(pid) or {}
        desired = p.get("desired") or {}
        observed = p.get("observed") or {}
        running = p.get("live") or {}
        rec = rec_plugins.get(pid)
        ams = (
            {
                "outcome": rec.get("outcome"),
                "artifact": _short(rec.get("artifact_id")),
                "content_key": _short(rec.get("content_key")),
                "sha": _short(rec.get("sha")),
                "reason": rec.get("reason"),
                "at": rec.get("at"),
            }
            if isinstance(rec, dict)
            else None
        )
        drift = bool(
            live is not None
            and isinstance(rec, dict)
            and rec.get("outcome") == LIVE
            and desired
            and desired.get("artifactId") != rec.get("artifact_id")
        )
        rows.append(
            {
                "plugin": pid,
                "in_roster": pid in roster,
                "phase": observed.get("phase"),
                "live": running.get("phase"),
                "artifact": _short(desired.get("artifactId")),
                "commit": _short(running.get("commit")),
                "privileges": list(desired.get("privileges") or []),
                "reason": observed.get("reason"),
                "ams": ams,
                "drift": drift,
            }
        )
    return {
        "release_sha": record.get("release_sha"),
        "previous_release_sha": record.get("previous_release_sha"),
        "staged_sha": record.get("staged_sha"),
        "release_failed_sha": record.get("release_failed_sha"),
        "planned_sha": record.get("planned_sha"),
        "core": "reachable" if live is not None else ("unreachable" if queried else "not queried"),
        "plugins": rows,
    }
