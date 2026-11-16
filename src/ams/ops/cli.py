"""``ams watch run|status|check|freeze|test``.

Importing this module is cheap on purpose (``ams.cli`` builds the parser with it on every start);
everything that needs the platform layer is imported inside the handlers.
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import sys
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("ams.ops")
DEFAULT_CONFIG = "~/.config/ams-watch/watch.toml"
_DURATION = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text: str) -> float:
    try:
        return float(text[:-1]) * _DURATION[text[-1]]
    except (KeyError, ValueError, IndexError):
        raise argparse.ArgumentTypeError(f"{text!r}: use e.g. 90s, 30m, 2h, 1d") from None


def add_subparser(sub: Any) -> None:
    p = sub.add_parser("watch", help="watch launchd jobs, probes and logs; fix; escalate")
    p.add_argument(
        "-c",
        "--config",
        type=Path,
        default=None,
        help=f"default: $AMS_WATCH_CONFIG or {DEFAULT_CONFIG}",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    ws = p.add_subparsers(dest="watch_command", required=True)
    r = ws.add_parser("run", help="run the loop")
    r.add_argument("--once", action="store_true", help="one tick, then exit")
    s = ws.add_parser("status")
    s.add_argument("--json", action="store_true")
    f = ws.add_parser(
        "freeze", help="planned maintenance: no actions (and no escalation) for a service"
    )
    fs = f.add_subparsers(dest="op", required=True)
    fa = fs.add_parser("add")
    fa.add_argument("service", help="service id, or * for all")
    fa.add_argument("duration", type=parse_duration)
    fa.add_argument("reason")
    fa.add_argument("--keep-alerts", action="store_true", help="still escalate; only block actions")
    fr = fs.add_parser("rm")
    fr.add_argument("service")
    fs.add_parser("list")
    t = ws.add_parser("test", help="send one info escalation through the intake")
    t.add_argument("text", nargs="?")
    ws.add_parser("check", help="validate the config and the credential file")
    j = ws.add_parser(
        "journal",
        help="the operator's gate on the escalation journal: a line per new/back/flapping cause",
    )
    j.add_argument(
        "--journal-config", type=Path, required=True, help="rules, state file, health list"
    )
    j.add_argument("--state-dir", type=Path, default=None, help="overrides $AMS_STATE_DIR")


def _load(args: argparse.Namespace):  # noqa: ANN202
    from ams.ops.config import ConfigError, load

    path = args.config or Path(os.environ.get("AMS_WATCH_CONFIG", DEFAULT_CONFIG)).expanduser()
    try:
        return load(path)
    except ConfigError as e:
        print(f"ams watch: {e}", file=sys.stderr)
        return None


def _setup_logging(state_dir: Path, verbose: bool, to_file: bool) -> None:
    handlers: list[logging.Handler] = []
    if verbose or not to_file or sys.stderr.isatty():
        handlers.append(logging.StreamHandler(sys.stderr))
    if to_file:
        d = state_dir / "logs"
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                d / "ams-watch.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
            )
        )
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        handlers=handlers,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )


def _build(cfg):  # noqa: ANN001, ANN202
    from ams.escalations import EscalationJournal
    from ams.ops.engine import Engine
    from ams.ops.forward import Forwarder
    from ams.ops.store import Store

    cfg.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    store = Store(cfg.state_dir / "state.json")
    journal = EscalationJournal(cfg.state_dir / "logs" / "incidents.jsonl", "ams-watch")
    return Engine(cfg, store, Forwarder(cfg.forward, store.data), journal=journal), store


def _lock(cfg):  # noqa: ANN001, ANN202
    import fcntl

    fh = open(cfg.state_dir / "lock", "w")  # noqa: SIM115 - held for the process lifetime
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit("ams watch: another instance holds the state lock")
    return fh


def _run(cfg, args) -> int:  # noqa: ANN001
    import signal

    engine, store = _build(cfg)
    _lock_fh = _lock(cfg)  # noqa: F841
    stop = {"now": False}
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.__setitem__("now", True))
    log.info(
        "ams watch started: %d services, tick %.0fs, mode=%s",
        len(cfg.services),
        cfg.tick_s,
        cfg.mode,
    )
    while not stop["now"]:
        t0 = time.monotonic()
        try:
            rep = engine.tick()
            if rep.incidents:
                log.info(
                    "tick: incidents=%d escalated=%d muted=%d actions=%d",
                    rep.incidents,
                    rep.escalated,
                    rep.muted,
                    len(rep.actions),
                )
        except Exception:  # the loop must outlive any single bad tick
            log.exception("tick failed")
        if args.once:
            break
        slept = 0.0
        while not stop["now"] and slept < max(0.0, cfg.tick_s - (time.monotonic() - t0)):
            time.sleep(0.5)
            slept += 0.5
    store.save()
    log.info("ams watch stopped")
    return 0


def _status(cfg, args) -> int:  # noqa: ANN001
    from ams.ops import freeze as freeze_mod
    from ams.ops.store import Store

    d = Store(cfg.state_dir / "state.json").data
    st = d.get("status", {})
    age = time.time() - st.get("ts", 0)
    print(f"last tick: {age:.0f}s ago   outbox={st.get('outbox', 0)}   mode={cfg.mode}")
    for sid, info in st.get("services", {}).items():
        job, hl = info.get("job"), info.get("health")
        bits = []
        if job is not None:
            bits.append(
                "not loaded"
                if not job["loaded"]
                else f"{job['state']} pid={job['pid']} runs={job['runs']}"
            )
        if hl is not None:
            bits.append(f"health={'ok' if hl['ok'] else hl['detail']}")
        print(f"  {sid:<18} {'  '.join(bits)}")
    for fz in freeze_mod.load(cfg.config_dir / "freeze.json").values():
        left = int(fz.until - time.time())
        print(
            f"  FROZEN {fz.service} for {left // 60} more min "
            f"({fz.reason}; {'muted' if fz.mute else 'actions off'})"
        )
    for h in d["actions"][-5:]:
        when = time.strftime("%m-%d %H:%M", time.localtime(h["ts"]))
        print(f"  action {when} {h['target']} {h['action']} -> {h['outcome']}")
    if args.json:
        print(json.dumps(st, indent=1, default=str))
    return 0


def _freeze(cfg, args) -> int:  # noqa: ANN001
    from ams.ops import freeze as freeze_mod

    path = cfg.config_dir / "freeze.json"
    if args.op == "add":
        fz = freeze_mod.add(
            path, args.service, args.duration, args.reason, mute=not args.keep_alerts
        )
        until = time.strftime("%H:%M", time.localtime(fz.until))
        print(f"frozen {fz.service} until {until} ({fz.reason})")
    elif args.op == "rm":
        print("removed" if freeze_mod.remove(path, args.service) else "not frozen")
    else:
        for fz in freeze_mod.load(path).values():
            until = time.strftime("%F %T", time.localtime(fz.until))
            print(f"{fz.service}\t{until}\t{'mute' if fz.mute else 'no-actions'}\t{fz.reason}")
    return 0


def _agent_running(cfg) -> bool:  # noqa: ANN001
    import fcntl

    cfg.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(cfg.state_dir / "lock", "a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(fh, fcntl.LOCK_UN)
        return False


def _test(cfg, args) -> int:  # noqa: ANN001
    from ams.ops.forward import Forwarder
    from ams.ops.store import Store

    if _agent_running(cfg):
        print("the loop is running and owns the state file; stop it or use `ams escalate`")
        return 1
    store = Store(cfg.state_dir / "state.json")
    fw = Forwarder(cfg.forward, store.data)
    fw.enqueue(
        f"ops-test-{time.strftime('%Y%m%d%H%M%S')}",
        {
            "origin": f"{cfg.forward.origin}.watch-test",
            "severity": "info",
            "title": "ams watch test",
            "text": args.text or "ams watch test escalation: the path to the journal works.",
        },
    )
    delivered, failed = fw.flush()
    store.save()
    print(f"delivered={delivered} failed={failed} queued={len(store.data['outbox'])}")
    return 0 if delivered else 1


def _check(cfg, args) -> int:  # noqa: ANN001
    print(f"ok: {len(cfg.services)} services, mode={cfg.mode}, state={cfg.state_dir}")
    for s in cfg.services:
        print(
            f"  {s.id:<18} {s.launchd or '-':<48} expect={s.expect:<9} "
            f"actions={','.join(s.actions) or '-'}"
        )
    tf = cfg.forward.token_file
    print(f"  intake token: {'present' if tf.is_file() else 'MISSING'} ({tf})")
    print(f"  intake url:   {cfg.forward.url}")
    return 0


def _journal(args: argparse.Namespace) -> int:
    from ams.ops import journal
    from ams.state import StateDir

    try:
        cfg = journal.load_config(args.journal_config)
    except (OSError, ValueError) as e:
        print(f"ams watch journal: {e}", file=sys.stderr)
        return 2
    state = StateDir(args.state_dir) if args.state_dir else StateDir.from_env()
    logging.basicConfig(
        level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s"
    )
    return journal.run(cfg, state.root)


def cmd_watch(args: argparse.Namespace) -> int:
    if args.watch_command == "journal":
        return _journal(args)
    cfg = _load(args)
    if cfg is None:
        return 2
    _setup_logging(cfg.state_dir, args.verbose, to_file=args.watch_command == "run")
    handlers = {
        "run": _run,
        "status": _status,
        "freeze": _freeze,
        "test": _test,
        "check": _check,
    }
    return handlers[args.watch_command](cfg, args)
