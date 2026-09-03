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

Streams follow the harness convention: **stdout** is the JSON-lines escalation
channel an agent parses, **stderr** is the human log. ``status`` is the one
exception — it is a report for a person, so it prints a table on stdout.
"""

from __future__ import annotations

import argparse
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
    return parser


def cmd_platform(args: argparse.Namespace) -> int:
    handlers = {
        "sync": _cmd_sync,
        "status": _cmd_status,
        "bootstrap": _cmd_bootstrap,
        "rollback": cmd_rollback,
        "pool": cmd_pool,
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
