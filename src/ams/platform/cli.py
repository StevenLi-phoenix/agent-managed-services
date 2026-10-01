"""``ams platform`` -- the operator front door for core mode.

``ams platform core ...`` hosts the Cordis-based api core as one ams service:
``config import``, ``bootstrap``, ``sync`` (what ``deploy/ams-core-sync.timer``
runs every 60 s; running it by hand is the same code path), ``status``,
``release --rollback`` and ``ship``. See ``docs/platform-core.md``.

Streams follow the harness convention: **stdout** is the JSON-lines escalation
channel an agent parses, **stderr** is the human log. ``status`` is the one
exception -- it is a report for a person, so it prints a table on stdout.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ams.state import StateCorrupt, StateDir

log = logging.getLogger("ams.platform.cli")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_UNAVAILABLE = 2


def add_subparser(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Wire ``ams platform ...`` into an existing ``ams`` subparser action."""
    parser = subparsers.add_parser("platform", help="core mode: host the api core (Cordis)")
    ops = parser.add_subparsers(dest="platform_command", required=True)
    add_core_subparser(ops)
    return parser


def cmd_platform(args: argparse.Namespace) -> int:
    handlers = {"core": cmd_core}
    return handlers[args.platform_command](args)


def _state_dir(explicit: Path | None) -> StateDir:
    return StateDir(explicit) if explicit else StateDir.from_env()


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
    parser = ops.add_parser("core", help="host the Cordis-based api core as one service")
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


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
