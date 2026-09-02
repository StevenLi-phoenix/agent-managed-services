"""``ams platform`` — the operator front door for the replica platform.

Three verbs, deliberately thin:

- ``ams platform sync`` — one tick of :mod:`ams.platform.sync`. This is what
  ``deploy/ams-platform-sync.timer`` runs every 60 s; running it by hand is the
  same code path, which is the point.
- ``ams platform status`` — pretty-print ``<state>/platform/state.json``. Read
  only, and safe with no harness running.
- ``ams platform bootstrap`` — Layer-0 keypair, secrets and declarations
  (delegates to :func:`ams.platform.bootstrap.main`).

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
    return parser


def cmd_platform(args: argparse.Namespace) -> int:
    handlers = {"sync": _cmd_sync, "status": _cmd_status, "bootstrap": _cmd_bootstrap}
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


def format_status(services: dict[str, Any], wanted: Sequence[str]) -> list[str]:
    """One aligned row per service, plus the error lines underneath.

    Separate from :func:`_cmd_status` so a test can assert the rendering without
    capturing stdout, and so an agent can reuse it.
    """
    rows = [(sid, services[sid]) for sid in wanted if sid in services]
    if not rows:
        return ["no services"]
    width = max(len(sid) for sid, _ in rows)
    lines: list[str] = []
    for sid, rec in rows:
        sha = str(rec.get("sha") or "-")[:12]
        flags = []
        if rec.get("manual_restart"):
            flags.append("manual_restart")
        if rec.get("escalated"):
            flags.append("escalated")
        lines.append(
            f"{sid:<{width}}  {str(rec.get('stage')):<11}  {sha:<12}  "
            f"since={rec.get('stage_since') or '-'}" + (f"  [{', '.join(flags)}]" if flags else "")
        )
        if rec.get("error"):
            lines.append(f"{'':<{width}}  error: {rec['error']}")
        if rec.get("prev_sha"):
            lines.append(f"{'':<{width}}  last healthy: {str(rec['prev_sha'])[:12]}")
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
