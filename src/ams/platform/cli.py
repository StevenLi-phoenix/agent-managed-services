"""``ams platform`` — the operator front door for the replica platform.

Five verbs, deliberately thin:

- ``ams platform sync`` — one tick of :mod:`ams.platform.sync`. This is what
  ``deploy/ams-platform-sync.timer`` runs every 60 s; running it by hand is the
  same code path, which is the point.
- ``ams platform status`` — pretty-print ``<state>/platform/state.json``. Read
  only, and safe with no harness running.
- ``ams platform bootstrap`` — Layer-0 keypair, secrets and declarations
  (delegates to :func:`ams.platform.bootstrap.main`).
- ``ams platform rollback`` — re-deploy one service at an earlier commit
  (delegates to :mod:`ams.platform.rollback`, which owns its own flags).
- ``ams platform pool {plan,adopt}`` — move N services' data, secrets and
  declarations into one pool root (delegates to :mod:`ams.platform.pool`).
- ``ams platform core ...`` — core mode (ams 1.1.0, PLAN-core): host the
  Cordis-based api core as one service. ``config import``, ``bootstrap``,
  ``sync`` (what ``deploy/ams-core-sync.timer`` runs), ``status``, ``release
  --rollback``, ``ship``. Everything above it is the **deprecated** manifest mode,
  kept for api v2.0.0.

Streams follow the harness convention: **stdout** is the JSON-lines escalation
channel an agent parses, **stderr** is the human log. ``status`` is the one
exception — it is a report for a person, so it prints a table on stdout.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ams.platform import sync as sync_mod
from ams.platform.pool import add_subparser as add_pool
from ams.platform.pool import cmd_pool
from ams.platform.rollback import add_subparser as add_rollback
from ams.platform.rollback import cmd_rollback
from ams.platform.sync import (
    EXIT_ERROR,
    EXIT_OK,
    STATE_VERSION,
    SyncConfig,
    SyncError,
    SyncReport,
)
from ams.state import StateCorrupt, StateDir, read_json_checked

log = logging.getLogger("ams.platform.cli")

EXIT_UNAVAILABLE = 2

DEFAULT_REPO_URL = "https://github.com/StevenLi-phoenix/api"


def add_subparser(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Wire ``ams platform ...`` into an existing ``ams`` subparser action."""
    parser = subparsers.add_parser(
        "platform",
        help="replica platform: sync from the api repo, inspect state, bootstrap Layer 0",
    )
    ops = parser.add_subparsers(dest="platform_command", required=True)

    p_sync = ops.add_parser("sync", help="one sync tick: fetch, translate, declare, reload, gate")
    p_sync.add_argument("--repo", default=DEFAULT_REPO_URL, metavar="URL", help="source repository")
    p_sync.add_argument("--ref", default="main", help="branch to follow (default: main)")
    p_sync.add_argument(
        "--registry-url",
        default=sync_mod.DEFAULT_REGISTRY_URL,
        help=f"loopback only (default: {sync_mod.DEFAULT_REGISTRY_URL})",
    )
    p_sync.add_argument(
        "--auth-url",
        default=sync_mod.DEFAULT_AUTH_URL,
        help=f"loopback only (default: {sync_mod.DEFAULT_AUTH_URL})",
    )
    p_sync.add_argument(
        "--only",
        action="append",
        metavar="ID",
        help="sync only this service id or manifest directory (repeatable)",
    )
    p_sync.add_argument(
        "--skip", action="append", metavar="ID", help="skip this service id (repeatable)"
    )
    p_sync.add_argument(
        "--dry-run",
        action="store_true",
        help="print the plan; writes nothing under the state dir and calls nothing",
    )
    p_sync.add_argument("--state-dir", type=Path, default=None, help="overrides $AMS_STATE_DIR")
    p_sync.add_argument("--store-dir", type=Path, default=None, help="overrides $AMS_STORE_DIR")
    p_sync.add_argument("--log-level", default="INFO")

    p_status = ops.add_parser("status", help="print the platform sync state")
    p_status.add_argument("--state-dir", type=Path, default=None, help="overrides $AMS_STATE_DIR")
    p_status.add_argument("ids", nargs="*", metavar="service-id", help="default: every service")

    p_boot = ops.add_parser(
        "bootstrap", help="generate the Layer-0 keypair, secrets and declarations (idempotent)"
    )
    p_boot.add_argument("--state", metavar="DIR", help="state dir (default: $AMS_STATE_DIR)")
    p_boot.add_argument("--store", metavar="DIR", help="store dir (default: $AMS_STORE_DIR)")
    p_boot.add_argument("--examples", metavar="DIR", help="also render example declarations")
    p_boot.add_argument("-v", "--verbose", action="store_true")

    add_rollback(ops)
    add_pool(ops)
    add_core_subparser(ops)
    return parser


def cmd_platform(args: argparse.Namespace) -> int:
    handlers = {
        "sync": _cmd_sync,
        "status": _cmd_status,
        "bootstrap": _cmd_bootstrap,
        "rollback": cmd_rollback,
        "pool": cmd_pool,
        "core": cmd_core,
    }
    return handlers[args.platform_command](args)


# ------------------------------------------------------------------------- sync


def _state_dir(explicit: Path | None) -> StateDir:
    return StateDir(explicit) if explicit else StateDir.from_env()


def _cmd_sync(args: argparse.Namespace) -> int:
    from ams.runtime import RuntimeStore

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    state = _state_dir(args.state_dir)
    store = RuntimeStore(args.store_dir) if args.store_dir else RuntimeStore.from_env()
    try:
        cfg = SyncConfig(
            repo_url=args.repo,
            ref=args.ref,
            registry_url=args.registry_url,
            auth_url=args.auth_url,
            include=tuple(args.only) if args.only else None,
            exclude=tuple(args.skip or ()),
            dry_run=bool(args.dry_run),
        )
    except SyncError as e:
        print(f"ERROR {e}", file=sys.stderr)
        return EXIT_ERROR
    try:
        report = sync_mod.sync(state, store, cfg)
    except (SyncError, StateCorrupt) as e:
        print(f"ERROR {e}", file=sys.stderr)
        return EXIT_ERROR
    log.info("%s", report.summary())
    return report.exit_code


# ----------------------------------------------------------------------- status


def _cmd_status(args: argparse.Namespace) -> int:
    state = _state_dir(args.state_dir)
    path = sync_mod.state_path(state)
    if not path.exists():
        print(f"no platform state at {path}; run 'ams platform sync' first", file=sys.stderr)
        return EXIT_UNAVAILABLE
    try:
        data = read_json_checked(path)
    except StateCorrupt as e:
        print(f"ERROR {e}", file=sys.stderr)
        return EXIT_ERROR
    if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
        found = data.get("version") if isinstance(data, dict) else None
        print(f"ERROR {path}: unsupported version {found!r}", file=sys.stderr)
        return EXIT_ERROR
    services: dict[str, Any] = data.get("services") or {}
    wanted = args.ids or sorted(services)
    missing = [sid for sid in wanted if sid not in services]
    for line in format_status(services, wanted):
        print(line)
    if missing:
        print(f"unknown service(s): {', '.join(missing)}", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_OK if all(services[sid].get("stage") != "failed" for sid in wanted) else EXIT_ERROR


MEMBER_INDENT = "  "


def _pool_cell(rec: Any) -> str | None:
    """The POOL column for one record: ``(N)`` on a pool, its name on a member.

    ``None`` means "this row has nothing to do with a pool", and a fleet where
    every row answers ``None`` is rendered without the column at all -- so a
    platform with no pools keeps byte-for-byte the output it had before pools
    existed (PLAN-pool §3.4).
    """
    if rec.get("pool_members"):
        return f"({len(rec['pool_members'])})"
    pool = rec.get("pool")
    return str(pool) if pool else None


def _ordered(
    rows: Sequence[tuple[str, Any]], services: dict[str, Any]
) -> list[tuple[str, Any, bool]]:
    """Pools first, each followed by its own members; everything else after.

    A pool and its members are one process and one deploy unit, so reading them
    apart -- which a flat alphabetical listing does -- is what the indent exists
    to prevent.
    """
    by_id = dict(rows)
    out: list[tuple[str, Any, bool]] = []
    placed: set[str] = set()
    for pool_id in sorted(sid for sid, rec in rows if rec.get("pool_members")):
        out.append((pool_id, by_id[pool_id], False))
        placed.add(pool_id)
        for member in sorted(services[pool_id].get("pool_members") or []):
            if member in by_id and member not in placed:
                out.append((member, by_id[member], True))
                placed.add(member)
    out += [(sid, rec, False) for sid, rec in rows if sid not in placed]
    return out


def format_status(services: dict[str, Any], wanted: Sequence[str]) -> list[str]:
    """One aligned row per service, plus the error lines underneath.

    Separate from :func:`_cmd_status` so a test can assert the rendering without
    capturing stdout, and so an agent can reuse it.
    """
    rows = [(sid, services[sid]) for sid in wanted if sid in services]
    if not rows:
        return ["no services"]
    ordered = _ordered(rows, services)
    cells = {sid: _pool_cell(rec) for sid, rec, _ in ordered}
    has_pools = any(cell is not None for cell in cells.values())
    width = max(len(MEMBER_INDENT) * indented + len(sid) for sid, _, indented in ordered)
    pool_width = max((len(cell or "-") for cell in cells.values()), default=1)
    rendered = {sid for sid, _, _ in ordered}

    lines: list[str] = []
    for sid, rec, indented in ordered:
        sha = str(rec.get("sha") or "-")[:12]
        flags = []
        if rec.get("manual_restart"):
            flags.append("manual_restart")
        if rec.get("escalated"):
            flags.append("escalated")
        label = (MEMBER_INDENT if indented else "") + sid
        columns = [f"{label:<{width}}", f"{str(rec.get('stage')):<11}", f"{sha:<12}"]
        if has_pools:
            columns.append(f"{cells[sid] or '-':<{pool_width}}")
        columns.append(f"since={rec.get('stage_since') or '-'}")
        lines.append("  ".join(columns) + (f"  [{', '.join(flags)}]" if flags else ""))
        if rec.get("error"):
            lines.append(f"{'':<{width}}  error: {rec['error']}")
        if rec.get("prev_sha"):
            lines.append(f"{'':<{width}}  last healthy: {str(rec['prev_sha'])[:12]}")
        # `ams platform status <member>` is the common way to ask about one
        # service, and on its own a member row says nothing about the process
        # that actually runs it. One line, only when the pool is not on screen.
        pool = rec.get("pool")
        if pool and not rec.get("pool_members"):
            pool_id = sync_mod.pool_id_for(str(pool))
            if pool_id not in rendered:
                stage = str((services.get(pool_id) or {}).get("stage") or "unknown")
                lines.append(f"{'':<{width}}  pool: {pool_id} ({stage})")
    return lines


# -------------------------------------------------------------------- bootstrap


def _cmd_bootstrap(args: argparse.Namespace) -> int:
    from ams.platform.bootstrap import main as bootstrap_main

    argv: list[str] = []
    if args.state:
        argv += ["--state", args.state]
    if args.store:
        argv += ["--store", args.store]
    if args.examples:
        argv += ["--examples", args.examples]
    if args.verbose:
        argv.append("--verbose")
    return int(bootstrap_main(argv))


# ------------------------------------------------------------------------- core


def _core_common(p: argparse.ArgumentParser, *, isolation: bool = False) -> None:
    p.add_argument("--state-dir", type=Path, default=None, help="overrides $AMS_STATE_DIR")
    p.add_argument("--store-dir", type=Path, default=None, help="overrides $AMS_STORE_DIR")
    p.add_argument(
        "--config", type=Path, default=None, help="core.toml (default: <state>/platform/core.toml)"
    )
    p.add_argument("--log-level", default="INFO")
    if isolation:
        p.add_argument(
            "--no-isolation",
            action="store_true",
            help="plain mode: no user namespace (dev / macOS; pairs with 'ams run --no-isolation')",
        )


def add_core_subparser(ops: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """``ams platform core ...`` (PLAN-core §4.3)."""
    parser = ops.add_parser("core", help="core mode: host the Cordis-based api core (1.1.0)")
    verbs = parser.add_subparsers(dest="core_command", required=True)

    p_config = verbs.add_parser("config", help="the core config bundle")
    config_ops = p_config.add_subparsers(dest="config_command", required=True)
    p_import = config_ops.add_parser(
        "import", help="validate plugins.json (+ jwt keys, fonts) into the harness master copy"
    )
    p_import.add_argument("plugins_json", type=Path)
    p_import.add_argument(
        "--rebase",
        action="append",
        default=[],
        metavar="OLD=NEW",
        help="rewrite string values starting with OLD; NEW is a path or @root/@data/@etc "
        "(repeatable, e.g. --rebase /var/lib/core=@data --rebase /etc/core=@etc)",
    )
    p_import.add_argument(
        "--jwt-dir", type=Path, default=None, help="directory with jwt.pem + jwt.pub"
    )
    p_import.add_argument("--fonts", type=Path, default=None, help="fonts directory to copy")
    _core_common(p_import)

    _core_common(
        verbs.add_parser("bootstrap", help="declare caddy, first core release, install the roster"),
        isolation=True,
    )
    _core_common(
        verbs.add_parser("sync", help="one tick: fetch, stage, release, ship, gate"), isolation=True
    )
    p_status = verbs.add_parser("status", help="release, previous, and every plugin's state")
    p_status.add_argument("--json", action="store_true", help="machine-readable output")
    p_status.add_argument("--offline", action="store_true", help="the record only; do not ask core")
    _core_common(p_status, isolation=True)
    p_release = verbs.add_parser("release", help="core releases")
    p_release.add_argument(
        "--rollback", action="store_true", help="flip current back to the previous release"
    )
    _core_common(p_release, isolation=True)
    p_ship = verbs.add_parser("ship", help="ship roster plugins from the staged tree now")
    p_ship.add_argument("ids", nargs="+", metavar="plugin-id")
    p_ship.add_argument(
        "--force",
        action="store_true",
        help="ship even an unchanged or previously failed content key",
    )
    _core_common(p_ship, isolation=True)
    return parser


def _core_setup(args: argparse.Namespace) -> tuple[StateDir, Any, Any]:
    """Logging, state, store and the validated config -- or CoreConfigError."""
    from ams.platform import core as core_mod
    from ams.runtime import RuntimeStore

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    state = _state_dir(args.state_dir)
    store = RuntimeStore(args.store_dir) if args.store_dir else RuntimeStore.from_env()
    cfg = core_mod.load_config(args.config or core_mod.config_path(state), state=state)
    return state, store, cfg


def cmd_core(args: argparse.Namespace) -> int:
    from ams.platform.core import CoreConfigError

    handlers = {
        "config": _core_config,
        "bootstrap": _core_bootstrap,
        "sync": _core_sync,
        "status": _core_status,
        "release": _core_release,
        "ship": _core_ship,
    }
    if args.core_command == "release" and not args.rollback:
        print(
            "ERROR 'ams platform core release' needs --rollback (releases happen in sync)",
            file=sys.stderr,
        )
        return EXIT_ERROR
    try:
        return handlers[args.core_command](args)
    except CoreConfigError as e:
        print(f"ERROR {e}", file=sys.stderr)
        return EXIT_ERROR


def _core_config(args: argparse.Namespace) -> int:
    from ams.platform import core as core_mod

    pairs: list[tuple[str, str]] = []
    for item in args.rebase:
        old, sep, new = item.partition("=")
        if not sep or not old or not new:
            print(f"ERROR --rebase {item!r}: expected OLD=NEW", file=sys.stderr)
            return EXIT_ERROR
        pairs.append((old, new))
    state, _store, cfg = _core_setup(args)
    names = core_mod.import_bundle(
        state, args.plugins_json, rebase=pairs, jwt_dir=args.jwt_dir, fonts_dir=args.fonts, cfg=cfg
    )
    # Names only: the bundle holds secrets (PLAN-core §5).
    for name in names:
        print(name)
    log.info("imported %d file(s); the next sync places them into the core root", len(names))
    return EXIT_OK


def _core_report(report: Any) -> int:
    log.info("%s", report.summary())
    return int(report.exit_code)


def _core_bootstrap(args: argparse.Namespace) -> int:
    from ams.platform import coresync

    state, store, cfg = _core_setup(args)
    return _core_report(coresync.bootstrap(state, store, cfg, isolation=not args.no_isolation))


def _core_sync(args: argparse.Namespace) -> int:
    from ams.platform import coresync

    state, store, cfg = _core_setup(args)
    return _core_report(coresync.tick(state, store, cfg, isolation=not args.no_isolation))


def _core_release(args: argparse.Namespace) -> int:
    from ams.platform import coresync

    state, store, cfg = _core_setup(args)
    return _core_report(
        coresync.rollback_release(state, store, cfg, isolation=not args.no_isolation)
    )


def _core_ship(args: argparse.Namespace) -> int:
    from ams.platform import coresync

    state, store, cfg = _core_setup(args)
    return _core_report(
        coresync.ship(
            state, store, cfg, list(args.ids), force=args.force, isolation=not args.no_isolation
        )
    )


def _core_status(args: argparse.Namespace) -> int:
    from ams.platform import core as core_mod
    from ams.platform import coresync

    state, store, cfg = _core_setup(args)
    path = core_mod.record_path(state)
    if not path.exists():
        print(f"no core record at {path}; run 'ams platform core bootstrap' first", file=sys.stderr)
        return EXIT_UNAVAILABLE
    try:
        record = coresync.CoreRecord.load(path).data
    except StateCorrupt as e:
        print(f"ERROR {e}", file=sys.stderr)
        return EXIT_ERROR
    live = None
    if not args.offline:
        live = coresync.live_status(state, store, cfg, record, isolation=not args.no_isolation)
    view = coresync.status_view(record, live, cfg.plugins, queried=not args.offline)
    if args.json:
        print(json.dumps(view, indent=2, sort_keys=True))
    else:
        for line in format_core_status(view):
            print(line)
    return EXIT_OK


def format_core_status(view: dict[str, Any]) -> list[str]:
    """Human rendering of :func:`ams.platform.coresync.status_view` (names and ids only)."""

    def short(value: Any) -> str:
        return str(value)[:12] if value else "-"

    release, previous = short(view.get("release_sha")), short(view.get("previous_release_sha"))
    lines = [
        f"release   {release}  previous {previous}"
        f"  staged {short(view.get('staged_sha'))}  core {view.get('core')}",
    ]
    if view.get("release_failed_sha"):
        lines.append(
            f"held      {short(view['release_failed_sha'])} (release failed or rolled back)"
        )
    rows = view.get("plugins") or []
    if not rows:
        return [*lines, "no plugins"]
    width = max(len(r["plugin"]) for r in rows)
    lines.append(
        f"{'PLUGIN':<{width}}  {'PHASE':<9}  {'LIVE':<9}  {'ARTIFACT':<12}  {'COMMIT':<12}  AMS"
    )
    for r in rows:
        ams = r.get("ams") or {}
        last = (
            f"{ams.get('outcome')} {short(ams.get('artifact'))} @{short(ams.get('sha'))}"
            if ams
            else "-"
        )
        flags = [
            f
            for f, on in (("drift", r.get("drift")), ("not in roster", not r.get("in_roster")))
            if on
        ]
        phase, running = str(r.get("phase") or "-"), str(r.get("live") or "-")
        lines.append(
            f"{r['plugin']:<{width}}  {phase:<9}  {running:<9}  "
            f"{short(r.get('artifact')):<12}  {short(r.get('commit')):<12}  {last}"
            + (f"  [{', '.join(flags)}]" if flags else "")
        )
        if ams and ams.get("reason"):
            lines.append(f"{'':<{width}}  ams: {ams['reason']}")
        if r.get("reason"):
            lines.append(f"{'':<{width}}  core: {r['reason']}")
    return lines


# ------------------------------------------------------------------ standalone


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - thin wrapper
    """``python -m ams.platform.cli`` for a build where ``ams`` is not on PATH."""
    parser = argparse.ArgumentParser(prog="ams platform")
    sub = parser.add_subparsers(dest="command", required=True)
    add_subparser(sub)
    args = parser.parse_args(list(argv) if argv is not None else None)
    return cmd_platform(args)


def report_lines(report: SyncReport) -> list[str]:  # pragma: no cover - debugging aid
    """Human summary of a report, for an operator running sync by hand."""
    rows = (f"{o.id:<20} {o.stage:<11} {', '.join(o.actions) or '-'}" for o in report.services)
    return [report.summary(), *rows]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
