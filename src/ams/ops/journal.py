"""``ams watch journal``: the operator's gate on the escalation journal.

DSH runs a Monitor over ``escalations.jsonl``: it signs each cause, prints one line when a cause is
new, back after a long silence, or flapping, and wakes the agent on that line. That Monitor
(ga-watch) was written for one service; this is the same thing with the service-specific parts as
data: ignore rules in a TOML file, scoped to a service, and the services whose health is polled.

* The gate is :class:`ams.ops.gate.LogGate` -- the one the host watcher uses (mini-ops' copy and
  ga-watch's were the same algorithm). Windows are ga-watch's: new again after 12 h of silence,
  flapping at 5 in an hour, at most one flap line per 6 h.
* The state file is ga-watch's format (``{"sig": {...}, "bootstrapped": ...}``), so swapping the
  Monitor's script keeps what it already knew, and swapping back works too.
* With the global-awareness rules the output is line-for-line ga-watch's
  (``tests/ops/test_ops_journal.py`` replays the frozen original against this).

Records are untrusted; they are only parsed, signed and clipped. Nothing here runs a record's text.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ams.ops.config import GateConfig
from ams.ops.gate import LogGate

log = logging.getLogger("ams.ops.journal")

#: ga-watch's windows (also GateConfig's defaults; stated here because they are the contract).
GATE = GateConfig(
    new_window_s=12 * 3600, flap_window_s=3600, flap_count=5, flap_cooldown_s=6 * 3600
)
HEALTH_REPEAT_S = 1800
TAGS = {"NEW": "新问题", "BACK": "旧问题重现", "FLAP": "反复出现"}


# ------------------------------------------------------------------------------------- rules


@dataclass(frozen=True)
class IgnoreRule:
    service: str | None = None
    level: str | None = None
    msg: tuple[str, ...] = ()
    msg_prefix: tuple[str, ...] = ()
    msg_contains: tuple[str, ...] = ()


@dataclass(frozen=True)
class KnownMissing:
    """A failure the owner already knows about: ``msg`` from a source whose name starts with
    ``src_prefix`` and whose error text contains one of ``error_contains``."""

    msg: str
    src_prefix: tuple[str, ...]
    error_contains: tuple[str, ...]
    service: str | None = None


@dataclass(frozen=True)
class Rules:
    ignore_raw_prefixes: tuple[str, ...] = ()
    ignore: tuple[IgnoreRule, ...] = ()
    known_missing: tuple[KnownMissing, ...] = ()


def _strs(table: dict[str, Any], key: str, where: str) -> tuple[str, ...]:
    val = table.get(key, ())
    if not isinstance(val, (list, tuple)) or not all(isinstance(v, str) for v in val):
        raise ValueError(f"{where}.{key}: a list of strings")
    return tuple(val)


def _only(table: dict[str, Any], allowed: set[str], where: str) -> None:
    extra = set(table) - allowed
    if extra:
        raise ValueError(f"{where}: unknown key(s) {sorted(extra)}")


def parse_rules(raw: dict[str, Any]) -> Rules:
    _only(raw, {"ignore_raw_prefixes", "rule", "known_missing"}, "rules")
    ignore = []
    for i, r in enumerate(raw.get("rule", ())):
        where = f"rule[{i}]"
        _only(r, {"service", "level", "msg", "msg_prefix", "msg_contains"}, where)
        ignore.append(
            IgnoreRule(
                service=r.get("service"),
                level=r.get("level"),
                msg=_strs(r, "msg", where),
                msg_prefix=_strs(r, "msg_prefix", where),
                msg_contains=_strs(r, "msg_contains", where),
            )
        )
    missing = []
    for i, r in enumerate(raw.get("known_missing", ())):
        where = f"known_missing[{i}]"
        _only(r, {"service", "msg", "src_prefix", "error_contains"}, where)
        if not isinstance(r.get("msg"), str):
            raise ValueError(f"{where}.msg: a string")
        missing.append(
            KnownMissing(
                msg=r["msg"],
                src_prefix=_strs(r, "src_prefix", where),
                error_contains=_strs(r, "error_contains", where),
                service=r.get("service"),
            )
        )
    return Rules(_strs(raw, "ignore_raw_prefixes", "rules"), tuple(ignore), tuple(missing))


@dataclass(frozen=True)
class JournalConfig:
    path: Path | None
    state: Path
    rules: Rules
    health_services: tuple[str, ...] = ()
    health_interval_s: float = 300.0
    extra: dict[str, Any] = field(default_factory=dict)


def load_config(path: Path) -> JournalConfig:
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    journal, health = raw.pop("journal", {}), raw.pop("health", {})
    _only(journal, {"path", "state"}, "journal")
    _only(health, {"interval_s", "services"}, "health")
    if "state" not in journal:
        raise ValueError("journal.state is required")
    return JournalConfig(
        path=Path(journal["path"]).expanduser() if "path" in journal else None,
        state=Path(journal["state"]).expanduser(),
        rules=parse_rules(raw),
        health_services=_strs(health, "services", "health"),
        health_interval_s=float(health.get("interval_s", 300)),
    )


# ---------------------------------------------------------------------------------- watcher


def _ignored(rules: Rules, service: str | None, level: Any, msg: str) -> bool:
    for r in rules.ignore:
        if r.service is not None and r.service != service:
            continue
        if r.level is not None and r.level != level:
            continue
        if msg in r.msg or (r.msg_prefix and msg.startswith(r.msg_prefix)):
            return True
        if any(s in msg for s in r.msg_contains):
            return True
    return False


def _known_missing(
    rules: Rules, service: str | None, obj: dict[str, Any], msg: str, src: str
) -> bool:
    for k in rules.known_missing:
        if k.service is not None and k.service != service:
            continue
        if msg == k.msg and src.startswith(k.src_prefix):
            err = str(obj.get("error", ""))
            if any(s in err for s in k.error_contains):
                return True
    return False


Signature = tuple[Any, ...]


def classify(
    rules: Rules, service: str | None, obj: dict[str, Any], raw: str
) -> tuple[Signature, Any, str] | None:
    """``(signature, level, detail)`` of a record's text, or ``None`` when a rule drops it."""
    if rules.ignore_raw_prefixes and raw.startswith(rules.ignore_raw_prefixes):
        return None
    if not raw.startswith("{"):
        return ("raw", raw[:120]), "unknown", raw[:300]
    level = obj.get("level")
    msg = obj.get("msg", "")
    src = obj.get("src") or ""
    if not isinstance(msg, str):
        msg = str(msg)
    if _ignored(rules, service, level, msg) or _known_missing(rules, service, obj, msg, src):
        return None
    err = obj.get("error")
    if isinstance(err, dict):
        err = err.get("message", "")
    detail = f"{msg}" + (f" src={src}" if src else "") + (f" err={str(err)[:220]}" if err else "")
    if "cron" in obj:
        detail += f" cron={obj['cron']}"
    return (level, msg, src), level, detail


class JournalWatcher:
    def __init__(
        self,
        rules: Rules,
        state_path: Path,
        emit: Callable[[str], None],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.rules, self.state_path, self._emit, self.clock = rules, Path(state_path), emit, clock
        self.lock = threading.Lock()
        self.state = self._load()
        self.gate = LogGate(self.state["sig"], GATE, hist_cap=None)
        self.quiet = False
        self._health: dict[str, dict[str, Any]] = {}

    # ---- state ----------------------------------------------------------------------------
    def _load(self) -> dict[str, Any]:
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {"sig": {}}
        if not isinstance(state, dict):
            state = {"sig": {}}
        state.setdefault("sig", {})
        state.setdefault("bootstrapped", False)
        return state

    def save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_name(self.state_path.name + ".tmp")
        tmp.write_text(json.dumps(self.state), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def emit(self, text: str) -> None:
        if not self.quiet:
            self._emit(text)

    # ---- records --------------------------------------------------------------------------
    def handle(self, line: str) -> None:
        raw = line.strip()
        if not raw:
            return
        try:
            rec = json.loads(raw)
            event = rec.get("event", {})
            text = event.get("text", "")
            reason = rec.get("reason", "")
            service = rec.get("service_id")
        except (ValueError, AttributeError):
            return
        if not isinstance(text, str):
            return
        try:
            obj = json.loads(text) if text.lstrip().startswith("{") else {}
        except ValueError:
            obj = {}
        if not isinstance(obj, dict):
            obj = {}
        verdict = classify(self.rules, service if isinstance(service, str) else None, obj, text)
        if verdict is None:
            return
        sig, level, detail = verdict
        with self.lock:
            opened = self.gate.observe(
                str(service or "-"), "|".join(map(str, sig)), detail, self.clock()
            )
            self.save()
        if opened is None:
            return
        kind, count = opened
        extra = f" 近1h出现{count}次" if kind == "FLAP" else ""
        self.emit(f"[{TAGS[kind]}] level={level} {detail} (AMS: {reason}){extra}")

    def bootstrap(self, journal: Path) -> int:
        """Learn what is already in the journal without printing it. Returns the size read."""
        data = b""
        self.quiet = True
        try:
            data = Path(journal).read_bytes()
            for line in data.decode("utf-8", "replace").splitlines():
                self.handle(line)
        finally:
            self.quiet = False
        now = self.clock()
        with self.lock:
            for entry in self.state["sig"].values():
                entry["last_emit"] = 0
                entry["base"] = True
                entry["hist"] = [entry.get("last_seen", now)]
                entry["count"] = 1
            self.state["bootstrapped"] = True
            self.save()
        return len(data)

    # ---- service health -------------------------------------------------------------------
    def check_service(
        self, service: str, status: dict[str, Any] | None, error: str | None = None
    ) -> None:
        """One poll of ``ams ctl status <service>``: report down and restarts, not steady state."""
        st = self._health.setdefault(service, {"down_since": None, "last_emit": 0.0, "seen": False})
        now = self.clock()
        if status is None:
            if now - st["last_emit"] < HEALTH_REPEAT_S:
                return
            st["last_emit"] = now
            self.emit(f"[服务查询失败] {service} ams ctl status 出错: {error}")
            return
        running = status.get("status") == "running"
        healthy = bool(status.get("healthy"))
        uptime = status.get("uptime_s") or 0
        fails = status.get("consecutive_failures")
        if running and healthy:
            st["down_since"] = None
            if uptime < 300 and st["seen"]:
                if now - st["last_emit"] >= HEALTH_REPEAT_S:
                    st["last_emit"] = now
                    self.emit(
                        f"[服务刚重启] {service} 已恢复，uptime {int(uptime)}s，连续失败 {fails}"
                    )
            st["seen"] = True
            return
        if st["down_since"] is None:
            st["down_since"] = now
        if st["last_emit"] and now - st["last_emit"] < HEALTH_REPEAT_S:
            return
        st["last_emit"] = now
        self.emit(
            f"[服务异常] {service} status={status.get('status')} healthy={healthy} "
            f"consecutive_failures={fails} 已持续 {int(now - st['down_since'])}s"
        )


# ------------------------------------------------------------------------------------ loops


def tail_loop(w: JournalWatcher, journal: Path, stop: Callable[[], bool] = lambda: False) -> None:
    offset = w.bootstrap(journal)
    buf = ""
    while not stop():
        time.sleep(1.0)
        try:
            size = journal.stat().st_size
            if size < offset:  # rotated / truncated
                offset, buf = 0, ""
            if size == offset:
                continue
            with open(journal, "rb") as f:
                f.seek(offset)
                chunk = f.read(size - offset)
                offset = f.tell()
            buf += chunk.decode("utf-8", "replace")
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                w.handle(line)
        except FileNotFoundError:
            continue
        except Exception:  # the watcher outlives any one bad record
            log.exception("journal read failed")


def ctl_status(service: str, state_dir: Path) -> tuple[dict[str, Any] | None, str | None]:
    env = dict(os.environ, AMS_STATE_DIR=str(state_dir))
    try:
        out = subprocess.run(
            [sys.executable, "-m", "ams", "ctl", "status", service],
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        ).stdout
        return json.loads(out)["services"][service], None
    except Exception as e:  # any failure is a report, not a crash
        return None, f"{type(e).__name__}: {e}"


def health_loop(
    w: JournalWatcher, services: tuple[str, ...], state_dir: Path, interval_s: float
) -> None:
    while True:
        time.sleep(interval_s)
        for sid in services:
            st, err = ctl_status(sid, state_dir)
            with w.lock:
                w.check_service(sid, st, err)


def run(cfg: JournalConfig, state_dir: Path) -> int:
    journal = cfg.path or state_dir / "logs" / "escalations.jsonl"

    def emit(text: str) -> None:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()

    w = JournalWatcher(cfg.rules, cfg.state, emit)
    if cfg.health_services:
        threading.Thread(
            target=health_loop,
            args=(w, cfg.health_services, state_dir, cfg.health_interval_s),
            daemon=True,
        ).start()
    log.info("watching %s (state %s, health of %s)", journal, cfg.state, cfg.health_services)
    tail_loop(w, journal)
    return 0
