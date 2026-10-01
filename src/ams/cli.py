"""``ams`` command line entry point.

Four subcommands:

- ``ams validate <service.toml>...`` — parse + validate declarations.
- ``ams run`` — load every declaration in the state dir and supervise it.
- ``ams provision [<id>...]`` — build the declared runtime for a service.
- ``ams ctl <op> [<id>]`` — talk to a running harness over its control socket
  (``ams.control``): ``ping``, ``status``, ``reload``, ``start|stop|restart|kill``.
- ``ams check-host`` — host prerequisite report (delegates to ``ams.hostcheck``).

``ams run`` writes two streams deliberately: **stderr** is the harness log
(human/agent readable, ``logging``), **stdout** is the escalation stream (one
JSON object per line). An embedding agent reads stdout as events and stderr as
diagnostics.

The assembly of state dir + allocators + spawner + supervisor lives in one
function, :func:`build_supervisor`, so ``tests/linux/test_e2e.py`` exercises the
same code path ``run`` does rather than a lookalike.

Imports of the isolation / allocator modules are lazy on purpose: ``validate``
and ``--no-isolation`` must work on a machine where the Linux-only pieces are
absent or unimportable.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import pwd
import stat
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ams.schema import DeclError, ServiceDecl, load

if TYPE_CHECKING:  # pragma: no cover - typing only, these are Linux-only imports
    from ams.decision import DecisionPolicy, Escalation
    from ams.events import Event
    from ams.ports import PortAllocator
    from ams.spawn import Spawner
    from ams.state import StateDir
    from ams.supervisor import Supervisor
    from ams.uidmap import UidAllocator, UidBlock

log = logging.getLogger("ams.cli")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_UNAVAILABLE = 2

# How often ``run`` logs a one-line-per-service summary.
SUMMARY_INTERVAL_S = 60.0

# Ceiling for the graceful-shutdown phase. deploy/ams-harness.service sets
# TimeoutStopSec=50; the supervisor spends up to (this + ~1.5s) before it
# force-kills, so a declaration with a huge stop.timeout_s must be clamped
# rather than letting systemd SIGKILL the harness mid-shutdown. 45 s covers
# core mode's stop.timeout_s = 40 (upstream core.service TimeoutStopSec=40:
# every plugin generation drains before the process exits).
SHUTDOWN_BUDGET_S = 45.0

# `ams ctl` operations. Duplicated from ams.control.OPS rather than imported at
# module import time so that building the parser (and `ams validate`) never
# needs the control module; the client checks the two agree.
CTL_OPS: tuple[str, ...] = ("ping", "status", "reload", "start", "stop", "restart", "kill")
CTL_TIMEOUT_S = 10.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ams", description="agent managed services supervisor")
    sub = parser.add_subparsers(dest="command", required=True)

    p_validate = sub.add_parser("validate", help="validate service declaration files")
    p_validate.add_argument("paths", nargs="+", metavar="service.toml")

    p_run = sub.add_parser("run", help="supervise every service in the state dir")
    p_run.add_argument("--state-dir", type=Path, default=None, help="overrides $AMS_STATE_DIR")
    p_run.add_argument(
        "--no-isolation",
        action="store_true",
        help="use PlainSpawner (no userns/cgroup); development only",
    )
    p_run.add_argument(
        "--provision",
        action="store_true",
        help="build each service's declared runtime before starting it",
    )
    p_run.add_argument("--escalate", choices=("jsonl", "null"), default="jsonl")
    p_run.add_argument(
        "--policy",
        choices=("default", "platform"),
        default="default",
        help="decision policy: 'platform' adds cause dedupe, the post-sync health "
        "gate and the Caddy/registry rules (ams.platform.policy)",
    )
    p_run.add_argument("--log-level", default="INFO")

    p_prov = sub.add_parser("provision", help="build declared runtimes, then exit")
    p_prov.add_argument("ids", nargs="*", metavar="service-id", help="default: every service")
    p_prov.add_argument("--state-dir", type=Path, default=None, help="overrides $AMS_STATE_DIR")
    p_prov.add_argument("--log-level", default="INFO")

    p_ctl = sub.add_parser("ctl", help="talk to a running harness over its control socket")
    p_ctl.add_argument("op", choices=CTL_OPS, help="operation to perform")
    p_ctl.add_argument("id", nargs="?", default=None, metavar="service-id")
    p_ctl.add_argument("--state-dir", type=Path, default=None, help="overrides $AMS_STATE_DIR")
    p_ctl.add_argument(
        "--timeout", type=float, default=CTL_TIMEOUT_S, help="seconds to wait for a reply"
    )

    sub.add_parser("check-host", help="report host prerequisites")

    p_esc = sub.add_parser(
        "escalations", help="read the escalation journal (<state>/logs/escalations.jsonl)"
    )
    p_esc.add_argument("--state-dir", type=Path, default=None, help="overrides $AMS_STATE_DIR")
    p_esc.add_argument("-n", type=int, default=50, help="newest N records (0 = all)")
    p_esc.add_argument("--service", default=None, help="only this service id")
    p_esc.add_argument("--since", default=None, help="only records at or after this UTC ISO time")
    p_esc.add_argument("--json", action="store_true", help="raw JSON lines (untrusted content)")

    # `ams secret set|rm|list|check`. Argument wiring and handlers live in
    # ams.secrets so that every line that can touch a value sits in one module.
    from ams.secrets import add_subparser as _add_secret_subparser

    _add_secret_subparser(sub)

    # `ams platform sync|status|bootstrap`. Same pattern as `secret`: the
    # replica platform's arguments and handlers live in ams.platform.cli so the
    # supervisor core never imports the translation/gateway/registry layer.
    from ams.platform.cli import add_subparser as _add_platform_subparser

    _add_platform_subparser(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "validate":
        return cmd_validate(args.paths)
    if args.command == "secret":
        from ams.secrets import cmd_secret

        return cmd_secret(args)
    if args.command == "run":
        return cmd_run(args)
    if args.command == "provision":
        return cmd_provision(args)
    if args.command == "ctl":
        return cmd_ctl(args)
    if args.command == "platform":
        from ams.platform.cli import cmd_platform

        return cmd_platform(args)
    if args.command == "check-host":
        return cmd_check_host()
    if args.command == "escalations":
        return cmd_escalations(args)
    return EXIT_ERROR  # pragma: no cover - argparse enforces the choices


# ------------------------------------------------------------------ escalations


def cmd_escalations(args: argparse.Namespace) -> int:
    """Print the escalation journal, oldest first, newest ``-n`` records."""
    import json

    from ams.escalations import format_record, journal_path, read_records

    path = journal_path(_state_dir(args.state_dir))
    records = read_records(path)
    if args.service:
        records = [r for r in records if r.get("service_id") == args.service]
    if args.since:
        since = args.since if args.since.endswith("Z") else args.since + "Z"
        records = [r for r in records if str(r.get("ts", "")) >= since]
    if args.n > 0:
        records = records[-args.n :]
    if not records:
        print(f"no escalations in {path}", file=sys.stderr)
        return EXIT_OK
    for rec in records:
        print(json.dumps(rec, default=str) if args.json else format_record(rec))
    return EXIT_OK


# --------------------------------------------------------------------- validate


def cmd_validate(paths: list[str]) -> int:
    failed = False
    for raw in paths:
        path = Path(raw)
        try:
            decl: ServiceDecl = load(path)
        except (DeclError, OSError) as e:
            print(f"ERROR {path}: {e}")
            failed = True
        else:
            print(f"OK {decl.id}")
    return EXIT_ERROR if failed else EXIT_OK


# -------------------------------------------------------------------- assembly


class Unavailable(RuntimeError):
    """A prerequisite for supervising with isolation is missing.

    Carries a message that names the fix; ``cmd_run`` prints it and exits 2
    rather than silently degrading to an unisolated supervisor.
    """


@dataclass(frozen=True)
class Assembly:
    """Everything ``ams run`` needs, wired together and ready to start."""

    state: StateDir
    supervisor: Supervisor
    spawner: Spawner
    ports: PortAllocator
    declarations: dict[str, ServiceDecl]
    uids: UidAllocator | None = None
    isolated: bool = True
    registered: list[str] = field(default_factory=list)
    # The runtime store, kept so a hot reload can register a new declaration
    # through the same _register() path startup uses (ams.reload).
    store: Any = None
    # service id -> sha256 of its service.toml as last loaded. Change detection
    # for `ams ctl reload` / SIGHUP; seeded by ams.reload.seed_hashes.
    hashes: dict[str, str] = field(default_factory=dict)
    # Ids whose declaration disappeared: stopped, awaiting a clean removal from
    # the service table once the process is actually gone (ams.reload).
    pending_removals: set[str] = field(default_factory=set)

    def start_all(self) -> None:
        for service_id in self.registered:
            self.supervisor.start(service_id)

    def block_for(self, service_id: str) -> UidBlock | None:
        return None if self.uids is None else self.uids.allocate(service_id)


def harness_user() -> str:
    """The unprivileged user the harness runs as; owns the /etc/subuid range.

    Resolved from the real uid, not from ``$USER``/``$LOGNAME``: those are unset
    under some ``systemd-run`` invocations and are not authoritative anyway, and
    the name is used to look up the subuid range that every service's identity
    is carved from. Getting it wrong means either no isolated spawner at all or,
    worse, someone else's range.
    """
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except KeyError:  # pragma: no cover - uid with no passwd entry
        fallback = os.environ.get("USER") or os.environ.get("LOGNAME") or "harness"
        log.warning("uid %d has no passwd entry; falling back to %r", os.getuid(), fallback)
        return fallback


def uid_allocator(state: StateDir) -> UidAllocator:
    """The subuid block allocator. Needs a subuid range; needs no cgroup.

    Separate from :func:`_make_spawner` because provisioning needs the service's
    identity but not a delegated cgroup, so ``ams provision`` runs from an
    ordinary shell while ``ams run`` does not.
    """
    from ams.uidmap import UidAllocator

    return UidAllocator.from_host(harness_user(), state.uidmap_state)


def _make_spawner(state: StateDir, *, isolation: bool) -> tuple[Spawner, UidAllocator | None]:
    """PlainSpawner, or the Linux isolated spawner. Never falls back silently."""
    if not isolation:
        from ams.spawn import PlainSpawner

        log.warning(
            "running WITHOUT isolation (--no-isolation): services run as %s", harness_user()
        )
        return PlainSpawner(), None
    from ams.isolated import make_isolated_spawner

    allocator = uid_allocator(state)
    return make_isolated_spawner(allocator.allocate), allocator


def _load_runtime_layer(state: StateDir) -> tuple[Any, Any]:
    """``(store, extra_env_for)`` from ``ams.runtime``, or ``(None, None)``.

    The runtime provisioning layer is developed independently; the supervisor
    must start services with ``runtime.kind = "none"`` even when it is absent.
    """
    try:
        from ams import runtime
    except ImportError as e:
        log.warning("runtime provisioning unavailable (%s); only runtime.kind='none' will work", e)
        return None, None
    try:
        store = runtime.RuntimeStore.from_env()
        return store, runtime.make_extra_env_for(state, store)
    except Exception as e:
        log.warning("runtime layer present but unusable (%s); continuing without it", e)
        return None, None


def build_supervisor(
    state: StateDir,
    *,
    isolation: bool = True,
    escalation: Escalation | None = None,
    provision: bool = False,
    reset_window_min_s: float | None = None,
    policy: DecisionPolicy | None = None,
) -> Assembly:
    """Wire the state dir, allocators, spawner and supervisor into one loop.

    This is what ``ams run`` executes; the Linux end-to-end test drives the same
    function so the tested path and the shipped path cannot drift apart.

    Provisioning, when asked for, happens here -- before any service is started
    and therefore before the loop exists. It must never move into the loop:
    ``runtime.provision`` shells out to uv/pnpm/bun and blocks for seconds to
    minutes, and the supervisor is single-threaded, so a provision inside it
    would stall log reads, health checks and reaping for *every* service.
    ``extra_env_for`` is safe to call from ``start()`` only because
    ``runtime.make_extra_env_for`` returns a pure lookup that never provisions.

    Raises :class:`Unavailable` when isolation was asked for and the host cannot
    provide it (no delegated cgroup, no subuid range, corrupt allocator state).
    """
    from ams.cgroup import CgroupUnavailable
    from ams.ports import PortAllocator
    from ams.supervisor import Supervisor, set_child_subreaper_if_possible

    set_child_subreaper_if_possible()
    try:
        state.ensure()
    except OSError as e:
        # Almost always ownership: the state dir must belong to the harness
        # user. A traceback out of a systemd unit is useless to the agent.
        raise Unavailable(
            f"cannot prepare the state dir {state.root}: {e}\n"
            f"It and everything under it must be owned by the harness user "
            f"({harness_user()}): chown -R {harness_user()} {state.root}"
        ) from e
    log.info("state dir: %s", state.root)

    # Both allocators load persisted JSON and both can refuse to start: a
    # corrupt or truncated file raises ams.state.StateCorrupt (a RuntimeError),
    # a changed block_size raises ValueError. Neither may reach the operator as
    # a traceback out of a systemd unit, and neither may be papered over --
    # re-carving on top of unreadable state would hand out uids or ports that
    # are already in use. So they are constructed here, inside the guard.
    try:
        spawner, uids = _make_spawner(state, isolation=isolation)
        ports = PortAllocator(state.ports_state)
    except (CgroupUnavailable, OSError, RuntimeError, ValueError) as e:
        raise Unavailable(
            f"cannot start supervision: {type(e).__name__}: {e}\n"
            f"If this names a file under {state.runtime_state_dir}, that allocator "
            "state is unreadable: inspect and repair it (or delete it, accepting "
            "that services get new uid blocks and ports).\n"
            "Otherwise run 'ams check-host' to see what is missing, or pass "
            "--no-isolation to supervise without a user namespace or cgroup "
            "(development only)."
        ) from e

    # Loaded with or without isolation: the lookup is pure (no provisioning at
    # start), and a plain spawn needs it just as much -- a managed-node service
    # (core mode) would otherwise start with PATH=/usr/local/bin:/usr/bin:/bin
    # and die on a missing `node`. Provisioning itself stays isolation-only
    # (_register only provisions with a uid block).
    store, runtime_env_for = _load_runtime_layer(state)
    # Declared secrets (D16) ride the same per-start lookup as runtime
    # activation and win over it; they are injected even without isolation,
    # because a service's need for its credential does not depend on how it is
    # sandboxed. A declared secret with no stored value raises MissingSecret
    # here at start time -> spawn failure -> escalation, by design.
    from ams.secrets import make_extra_env_for as _extra_env_with_secrets
    from ams.secrets import warn_missing_secrets

    extra_env_for = _extra_env_with_secrets(state, runtime_env_for)

    kwargs: dict[str, Any] = {"escalation": escalation, "extra_env_for": extra_env_for}
    if policy is not None:
        kwargs["policy"] = policy
    if reset_window_min_s is not None:
        kwargs["reset_window_min_s"] = reset_window_min_s
    sup = Supervisor(spawner, **kwargs)

    declarations = state.load_declarations()
    if not declarations:
        log.warning("no service declarations under %s", state.services_dir)
    # A heads-up, not a refusal: the other services must still come up.
    # Belt and braces. warn_missing_secrets already swallows a per-service
    # OSError internally (an unreadable store directory is logged and skipped),
    # so this outer guard exists for everything that is NOT that: a bug in the
    # diagnostic itself, a corrupt state layout, anything unforeseen. A boot
    # that dies because an *advisory* warning failed is strictly worse than a
    # boot with no warning -- the services that can start would never start,
    # and the one that cannot would have failed at its own start anyway with a
    # message naming the secret. This is the same shape as the bug where an env
    # hook raised outside Supervisor.start's guard and took down every service
    # ordered after it.
    try:
        warn_missing_secrets(state, declarations)
    except Exception as e:
        log.warning(
            "could not check declared secrets against the store (%s: %s); "
            "continuing. A service whose secret is unset fails at its own start.",
            type(e).__name__,
            e,
        )

    asm = Assembly(
        state=state,
        supervisor=sup,
        spawner=spawner,
        ports=ports,
        declarations=declarations,
        uids=uids,
        isolated=isolation,
        store=store,
    )
    for service_id, decl in declarations.items():
        if _register(asm, service_id, decl, store=store, provision=provision):
            asm.registered.append(service_id)
    from ams.reload import seed_hashes

    seed_hashes(asm)
    return asm


def _register(
    asm: Assembly,
    service_id: str,
    decl: ServiceDecl,
    *,
    store: Any,
    provision: bool,
) -> bool:
    """Allocate ports + uid block, prepare the root, register. False = skip it."""
    root = asm.state.service_root(service_id)
    try:
        allocated = asm.ports.allocate(service_id, decl.ports)
    except Exception as e:
        log.error("skipping %s: port allocation failed: %s", service_id, e)
        return False
    block = None
    if asm.isolated:
        from ams.userns import ensure_service_root

        block = asm.block_for(service_id)
        if block is None:  # pragma: no cover - isolated implies an allocator
            log.error("skipping %s: isolation requested but no uid allocator", service_id)
            return False
        try:
            # Idempotent, and the only way the root gets the service's ownership:
            # once chowned to the block the harness cannot write it any more.
            ensure_service_root(root, block)
            _ensure_traversable(asm.state, root)
        except Exception as e:
            log.error("skipping %s: could not prepare %s: %s", service_id, root, e)
            return False
    if provision and store is not None and block is not None:
        if not _provision_one(asm.state, store, decl, root, block):
            return False
    asm.supervisor.add(decl, root, allocated)
    return True


def _ensure_traversable(state: StateDir, service_root: Path) -> None:
    """Let the service uid resolve its own workdir by path.

    The child inherits its cwd, which needs no traversal rights at all, but any
    program that then resolves that cwd *by path* does: ``python -m http.server``
    calls ``os.getcwd()`` and stats the result, and every directory above it
    must be +x for the service uid. ``StateDir.ensure`` makes the harness-owned
    directories 0750 and the service is neither their owner nor in their group,
    so a bare declaration would 404 on every request (observed on racknerd
    before this existed).

    Only ``o+x`` is added, never ``o+r``: these directories stay unlistable, so
    a service cannot enumerate its siblings. It can still traverse to a sibling
    root whose name it guesses; closing that needs the per-service directory to
    be group-owned by the service's mapped gid, which is a change to the state
    layout rather than to the CLI. Recorded in PROGRESS.md.
    """
    for d in (state.root, state.services_dir, service_root.parent):
        try:
            mode = stat.S_IMODE(d.stat().st_mode)
        except OSError as e:
            log.warning("cannot stat %s: %s", d, e)
            continue
        if mode & stat.S_IXOTH:
            continue
        try:
            d.chmod(mode | stat.S_IXOTH)
        except OSError as e:
            log.warning("could not make %s traversable by service uids: %s", d, e)
        else:
            log.debug("made %s traversable by service uids (0%o)", d, mode | stat.S_IXOTH)


def _provision_one(
    state: StateDir, store: Any, decl: ServiceDecl, root: Path, block: UidBlock
) -> bool:
    from ams import runtime

    log_path = state.logs_dir / f"{decl.id}-provision.log"
    try:
        env = runtime.provision(decl, root, store, block, log_path=log_path)
    except Exception as e:
        log.error("provisioning %s failed: %s: %s", decl.id, type(e).__name__, e)
        return False
    log.info("provisioned %s (%s): %s (log: %s)", decl.id, decl.runtime.kind, env, log_path)
    return True


# -------------------------------------------------------------------- reporting


class PeriodicSummary:
    """One INFO line per service every ``interval_s``. Cheap agent observability."""

    def __init__(
        self,
        supervisor: Supervisor,
        spawner: Spawner,
        interval_s: float = SUMMARY_INTERVAL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.supervisor = supervisor
        self.spawner = spawner
        self.interval_s = interval_s
        self._clock = clock
        self._next = clock() + interval_s

    def __call__(self, _events: list[Event]) -> None:
        now = self._clock()
        if now < self._next:
            return
        self._next = now + self.interval_s
        self.log_now()

    def log_now(self) -> None:
        info = self.supervisor.status()
        if not info:
            log.info("summary: no services registered")
            return
        for service_id, row in info.items():
            log.info("summary %s", self._describe(service_id, row))

    def _describe(self, service_id: str, row: dict[str, Any]) -> str:
        uptime = row["uptime_s"]
        parts = [
            service_id,
            f"status={row['status']}",
            f"pid={row['pid']}",
            f"uptime={uptime:.0f}s" if uptime is not None else "uptime=-",
            f"healthy={row['healthy']}",
            f"failures={row['consecutive_failures']}",
        ]
        if row.get("waiting_for"):
            parts.append("waiting_for=" + ",".join(row["waiting_for"]))
        if row["ports"]:
            parts.append("ports=" + ",".join(f"{n}:{p}" for n, p in sorted(row["ports"].items())))
        mem = self._memory_current(service_id)
        if mem is not None:
            parts.append(f"memory.current={mem}")
        return " ".join(parts)

    def _memory_current(self, service_id: str) -> int | None:
        st = self.supervisor.services.get(service_id)
        stats = getattr(self.spawner, "stats", None)
        if st is None or st.spawned is None or stats is None:
            return None
        try:
            return stats(st.spawned).get("memory.current")
        except OSError as e:  # a cgroup removed under us must not break reporting
            log.debug("stats for %s unavailable: %s", service_id, e)
            return None


def shutdown_timeout_for(declarations: dict[str, ServiceDecl]) -> float:
    """Graceful-stop budget, clamped so systemd's TimeoutStopSec cannot fire."""
    longest = max((d.stop.timeout_s for d in declarations.values()), default=5.0)
    if longest > SHUTDOWN_BUDGET_S:
        log.warning(
            "longest stop.timeout_s is %.1fs, over the %.1fs shutdown budget; "
            "clamping so systemd's TimeoutStopSec=50 does not SIGKILL the harness "
            "mid-shutdown. Lower stop.timeout_s, or raise TimeoutStopSec in the unit.",
            longest,
            SHUTDOWN_BUDGET_S,
        )
        return SHUTDOWN_BUDGET_S
    return longest


def shutdown_grace(asm: Assembly) -> Callable[[], float]:
    """The graceful-stop budget as of *shutdown*, not of harness start.

    ``reload`` keeps ``asm.declarations`` current, so a service added after start
    -- core, declared by ``ams platform core bootstrap`` on a running harness,
    with a 40 s drain -- gets its stop timeout honoured on the next
    ``systemctl stop``, still clamped to ``SHUTDOWN_BUDGET_S``.
    """
    return lambda: shutdown_timeout_for(asm.declarations)


# -------------------------------------------------------------------------- run


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
        force=True,
    )


def _state_dir(explicit: Path | None) -> StateDir:
    """An explicit --state-dir must beat $AMS_STATE_DIR, which from_env prefers."""
    from ams.state import StateDir

    return StateDir(explicit) if explicit else StateDir.from_env()


def cmd_run(args: argparse.Namespace) -> int:
    _configure_logging(args.log_level)
    try:
        from ams.decision import JsonLinesEscalation, NullEscalation

        state = _state_dir(args.state_dir)
    except ImportError as e:
        print(f"ams run needs ams.state/ams.decision: {e}", file=sys.stderr)
        return EXIT_UNAVAILABLE

    from ams.escalations import EscalationJournal, journal_path

    escalation = (
        JsonLinesEscalation(journal=EscalationJournal(journal_path(state), source="harness"))
        if args.escalate == "jsonl"
        else NullEscalation()
    )
    # Lazy, like every other optional layer here: `ams run` on a host without the
    # platform package must still supervise.
    platform_policy = None
    if getattr(args, "policy", "default") == "platform":
        from ams.platform.policy import make_policy

        platform_policy = make_policy(state, escalation=escalation)
        log.info("platform policy: cause dedupe, post-sync health gate, caddy/registry rules")
    try:
        asm = build_supervisor(
            state,
            isolation=not args.no_isolation,
            escalation=escalation,
            provision=args.provision,
            policy=platform_policy,
        )
    except Unavailable as e:
        print(str(e), file=sys.stderr)
        return EXIT_UNAVAILABLE

    from ams.control import ControlServer, control_socket_path
    from ams.reload import drain_pending_removals, reload

    asm.start_all()
    summary = PeriodicSummary(asm.supervisor, asm.spawner)
    summary.log_now()

    def on_reload() -> dict[str, Any]:
        return reload(asm)

    def on_iteration(events: list[Event]) -> None:
        # Removals are two-phase: reload() stops the service, this finishes the
        # drop once the process is actually gone (see ams.reload).
        drain_pending_removals(asm)
        if platform_policy is not None:
            # The policy's tick: close expired dedupe windows and run the
            # post-sync health gate. It emits through `escalation` itself.
            platform_policy.flush()
        summary(events)

    server = ControlServer(control_socket_path(state), asm.supervisor, reload_fn=on_reload)
    try:
        server.open()
    except OSError as e:
        # A harness that cannot offer a control channel is still a working
        # supervisor; refusing to start would be a worse failure than losing
        # `ams ctl`. SIGHUP reload keeps working either way.
        log.error("control socket unavailable (%s); 'ams ctl' will not work", e)
    try:
        asm.supervisor.run_forever(
            on_iteration=on_iteration,
            on_reload=on_reload,
            shutdown_timeout_s=shutdown_grace(asm),
        )
    finally:
        server.close()
    log.info("stopped")
    return EXIT_OK


# -------------------------------------------------------------------------- ctl


def cmd_ctl(args: argparse.Namespace) -> int:
    """Client half of the control channel: one request, one JSON line out.

    Three exit codes, deliberately distinct: 0 the harness did it, 1 the harness
    answered and refused (unknown id, a service that will not start), 2 there is
    no harness to ask. An agent scripting against this needs to tell "the thing
    is broken" from "the thing is not running".
    """
    from ams.control import OPS, ControlError, control_socket_path
    from ams.control import request as control_request

    assert set(CTL_OPS) == set(OPS), "ams.cli.CTL_OPS drifted from ams.control.OPS"
    state = _state_dir(args.state_dir)
    path = control_socket_path(state)
    try:
        response = control_request(path, args.op, args.id, timeout_s=args.timeout)
    except ControlError as e:
        print(str(e), file=sys.stderr)
        return EXIT_UNAVAILABLE
    print(json.dumps(response, indent=2, sort_keys=True))
    return EXIT_OK if response.get("ok") else EXIT_ERROR


# -------------------------------------------------------------------- provision


def cmd_provision(args: argparse.Namespace) -> int:
    _configure_logging(args.log_level)
    try:
        from ams import runtime  # noqa: F401 - presence check only
    except ImportError as e:
        print(
            f"ams provision needs ams.runtime: {e}\n"
            "The runtime provisioning layer is not installed in this build.",
            file=sys.stderr,
        )
        return EXIT_UNAVAILABLE

    state = _state_dir(args.state_dir)
    try:
        state.ensure()
        # Deliberately not _make_spawner: provisioning needs each service's uid
        # block and the admin namespace, but no cgroup. Going through the
        # spawner would make `ams provision` require a delegated cgroup and so
        # be unrunnable from an ordinary shell -- which is exactly where an
        # operator or agent invokes it, next to a harness that is already up.
        uids = uid_allocator(state)
    except (OSError, RuntimeError, ValueError) as e:
        print(
            f"cannot provision: {type(e).__name__}: {e}\n"
            "Run 'ams check-host' to see what is missing.",
            file=sys.stderr,
        )
        return EXIT_UNAVAILABLE
    store, _ = _load_runtime_layer(state)
    if store is None:
        print("runtime store unusable; see the log above", file=sys.stderr)
        return EXIT_UNAVAILABLE

    declarations = state.load_declarations()
    wanted = args.ids or sorted(declarations)
    missing = [sid for sid in wanted if sid not in declarations]
    if missing:
        print(f"unknown service(s): {', '.join(missing)}", file=sys.stderr)
        return EXIT_ERROR

    from ams.userns import ensure_service_root

    failed: list[str] = []
    for service_id in wanted:
        decl = declarations[service_id]
        root = state.service_root(service_id)
        block = uids.allocate(service_id)
        ensure_service_root(root, block)
        _ensure_traversable(state, root)
        if _provision_one(state, store, decl, root, block):
            print(f"OK {service_id} ({decl.runtime.kind})")
        else:
            failed.append(service_id)
    return EXIT_ERROR if failed else EXIT_OK


# ------------------------------------------------------------------ check-host


def cmd_check_host() -> int:
    try:
        from ams.hostcheck import main as hostcheck_main
    except ImportError:
        print("hostcheck unavailable", file=sys.stderr)
        return EXIT_UNAVAILABLE
    # Explicit []: hostcheck.main(None) falls back to sys.argv[1:], which here is
    # ["check-host"] and would be read as the username to check subuid ranges for.
    return int(hostcheck_main([]))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
