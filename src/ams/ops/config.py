"""``ams watch`` configuration: one TOML file, validated up front (a typo fails at load)."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ACTION_NAMES = frozenset({"kickstart", "soft_restart_kaorou", "clean_tmp", "advertise_tailscale"})
EXPECTS = frozenset({"running", "interval", "observe"})
LOG_FORMATS = frozenset({"auto", "level-prefix", "json", "plain"})
_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
_DOMAIN_RE = re.compile(r"^(gui/\d+|system)/[A-Za-z0-9._-]+$")


class ConfigError(ValueError):
    """The configuration is wrong; the message names the key."""


def _expand(p: str) -> Path:
    return Path(p).expanduser()


@dataclass(frozen=True)
class ForwardConfig:
    """Where incidents go: the harness's ``ams intake`` (it writes the escalation journal)."""

    url: str = "http://127.0.0.1:18090/escalate"
    token_file: Path = Path("~/.config/ams-watch/intake-token").expanduser()
    origin: str = "mini"  # an incident of service X is escalated as origin "<origin>.<X>"
    timeout_s: float = 10.0


@dataclass(frozen=True)
class GateConfig:
    new_window_s: float = 12 * 3600  # silent this long => a recurrence counts as new again
    flap_window_s: float = 3600
    flap_count: int = 5  # occurrences inside flap_window_s that make a cause "flapping"
    flap_cooldown_s: float = 6 * 3600  # at most one flap report per cause per this period
    job_repeat_s: float = 1800  # a job that stays down is re-reported at most this often
    flap_runs: int = 3  # launchd restarts inside flap_runs_window_s => restart storm
    flap_runs_window_s: float = 600
    health_failures: int = 3  # consecutive failed probes before a health incident


@dataclass(frozen=True)
class ActionLimits:
    cooldown_s: float = 1800
    max_per_target_per_day: int = 3
    max_per_hour: int = 4
    timeout_s: float = 60


@dataclass(frozen=True)
class KaorouConfig:
    api: str = "http://127.0.0.1:8096"
    env_script: Path = Path(
        "~/Codes/translate_upload_yt_bilibili/service/kaorou-env.sh"
    ).expanduser()


@dataclass(frozen=True)
class CleanTmpConfig:
    dirs: tuple[Path, ...] = ()
    min_age_hours: float = 24


@dataclass(frozen=True)
class TailscaleConfig:
    binary: Path = Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale")
    services: tuple[str, ...] = ()


@dataclass(frozen=True)
class ServiceConfig:
    id: str
    launchd: str | None = None  # "gui/501/<label>" or "system/<label>"
    expect: str = "observe"
    logs: tuple[Path, ...] = ()
    log_format: str = "auto"
    ignore: tuple[re.Pattern[str], ...] = ()
    escalate: tuple[re.Pattern[str], ...] = ()  # lines that are ERROR whatever their text says
    health_url: str | None = None
    health_socket: Path | None = None
    health_accept: tuple[int, ...] = ()  # extra HTTP codes that count as alive (e.g. 401)
    actions: tuple[str, ...] = ()
    description: str = ""

    @property
    def label(self) -> str | None:
        return self.launchd.split("/", 2)[2] if self.launchd else None

    @property
    def domain(self) -> str | None:
        return self.launchd.rsplit("/", 1)[0] if self.launchd else None


@dataclass(frozen=True)
class Config:
    state_dir: Path
    config_dir: Path
    tick_s: float
    mode: str
    forward: ForwardConfig
    gate: GateConfig
    limits: ActionLimits
    kaorou: KaorouConfig
    clean_tmp: CleanTmpConfig
    tailscale: TailscaleConfig
    services: tuple[ServiceConfig, ...] = field(default_factory=tuple)

    def service(self, sid: str) -> ServiceConfig | None:
        return next((s for s in self.services if s.id == sid), None)


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    val = raw.get(name, {})
    if not isinstance(val, dict):
        raise ConfigError(f"{name}: must be a table")
    return val


def _only(table: dict[str, Any], allowed: set[str], where: str) -> None:
    extra = set(table) - allowed
    if extra:
        raise ConfigError(f"{where}: unknown key(s) {sorted(extra)}")


def _fields(cls: type, table: dict[str, Any], where: str, paths: tuple[str, ...] = ()) -> Any:
    _only(table, set(cls.__dataclass_fields__), where)
    kwargs = {k: (_expand(v) if k in paths else v) for k, v in table.items()}
    try:
        return cls(**kwargs)
    except TypeError as e:  # pragma: no cover - defensive
        raise ConfigError(f"{where}: {e}") from e


def _regexes(patterns: Any, where: str) -> tuple[re.Pattern[str], ...]:
    out = []
    for pat in patterns:
        try:
            out.append(re.compile(pat))
        except re.error as e:
            raise ConfigError(f"{where}: bad regex {pat!r}: {e}") from e
    return tuple(out)


def _service(raw: dict[str, Any]) -> ServiceConfig:
    sid = raw.get("id")
    if not isinstance(sid, str) or not _ID_RE.match(sid):
        raise ConfigError(f"service.id: {sid!r} must match {_ID_RE.pattern}")
    where = f"service[{sid}]"
    _only(
        raw,
        {
            "id",
            "launchd",
            "expect",
            "logs",
            "log_format",
            "ignore",
            "escalate",
            "health_url",
            "health_socket",
            "health_accept",
            "actions",
            "description",
        },
        where,
    )
    launchd = raw.get("launchd")
    if launchd is not None and not _DOMAIN_RE.match(launchd):
        raise ConfigError(
            f"{where}.launchd: {launchd!r} must look like gui/501/<label> or system/<label>"
        )
    expect = raw.get("expect", "observe")
    if expect not in EXPECTS:
        raise ConfigError(f"{where}.expect: {expect!r} not in {sorted(EXPECTS)}")
    if expect != "observe" and not launchd:
        raise ConfigError(f"{where}.expect={expect} needs launchd")
    fmt = raw.get("log_format", "auto")
    if fmt not in LOG_FORMATS:
        raise ConfigError(f"{where}.log_format: {fmt!r} not in {sorted(LOG_FORMATS)}")
    actions = tuple(raw.get("actions", ()))
    bad = [a for a in actions if a not in ACTION_NAMES]
    if bad:
        raise ConfigError(f"{where}.actions: unknown {bad}; known {sorted(ACTION_NAMES)}")
    if launchd and launchd.startswith("system/") and "kickstart" in actions:
        raise ConfigError(f"{where}: kickstart on a system-domain job needs root; not supported")
    ignore = _regexes(raw.get("ignore", ()), f"{where}.ignore")
    escalate = _regexes(raw.get("escalate", ()), f"{where}.escalate")
    sock = raw.get("health_socket")
    return ServiceConfig(
        id=sid,
        launchd=launchd,
        expect=expect,
        logs=tuple(_expand(p) for p in raw.get("logs", ())),
        log_format=fmt,
        ignore=ignore,
        escalate=escalate,
        health_url=raw.get("health_url"),
        health_socket=_expand(sock) if sock else None,
        health_accept=tuple(int(c) for c in raw.get("health_accept", ())),
        actions=actions,
        description=raw.get("description", ""),
    )


def parse(raw: dict[str, Any], *, config_dir: Path | None = None) -> Config:
    top = {"ops", "forward", "gate", "actions", "kaorou", "service"}
    _only(raw, top, "config")
    ops = _section(raw, "ops")
    _only(ops, {"state_dir", "tick_s", "mode"}, "ops")
    mode = ops.get("mode", "live")
    if mode not in ("live", "dry-run"):
        raise ConfigError(f"ops.mode: {mode!r} must be live or dry-run")
    acts = _section(raw, "actions")
    _only(
        acts,
        {
            "cooldown_s",
            "max_per_target_per_day",
            "max_per_hour",
            "timeout_s",
            "clean_tmp",
            "advertise_tailscale",
        },
        "actions",
    )
    limits = _fields(
        ActionLimits,
        {k: v for k, v in acts.items() if k in ActionLimits.__dataclass_fields__},
        "actions",
    )
    clean = _section(acts, "clean_tmp")
    _only(clean, {"dirs", "min_age_hours"}, "actions.clean_tmp")
    ts = _section(acts, "advertise_tailscale")
    _only(ts, {"binary", "services"}, "actions.advertise_tailscale")
    cfg = Config(
        state_dir=_expand(ops.get("state_dir", "~/.local/state/ams-watch")),
        config_dir=config_dir or _expand("~/.config/ams-watch"),
        tick_s=float(ops.get("tick_s", 30)),
        mode=mode,
        forward=_fields(ForwardConfig, _section(raw, "forward"), "forward", ("token_file",)),
        gate=_fields(GateConfig, _section(raw, "gate"), "gate"),
        limits=limits,
        kaorou=_fields(KaorouConfig, _section(raw, "kaorou"), "kaorou", ("env_script",)),
        clean_tmp=CleanTmpConfig(
            dirs=tuple(_expand(d) for d in clean.get("dirs", ())),
            min_age_hours=float(clean.get("min_age_hours", 24)),
        ),
        tailscale=TailscaleConfig(
            binary=_expand(
                ts.get("binary", "/Applications/Tailscale.app/Contents/MacOS/Tailscale")
            ),
            services=tuple(ts.get("services", ())),
        ),
        services=tuple(_service(s) for s in raw.get("service", ())),
    )
    ids = [s.id for s in cfg.services]
    if len(set(ids)) != len(ids):
        raise ConfigError("service.id: duplicate ids")
    for s in cfg.services:
        if "advertise_tailscale" in s.actions and not cfg.tailscale.services:
            raise ConfigError(
                f"service[{s.id}]: advertise_tailscale needs actions.advertise_tailscale.services"
            )
        if "clean_tmp" in s.actions and not cfg.clean_tmp.dirs:
            raise ConfigError(f"service[{s.id}]: clean_tmp needs actions.clean_tmp.dirs")
    if cfg.tick_s < 1:
        raise ConfigError("ops.tick_s must be >= 1")
    return cfg


def load(path: Path) -> Config:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise ConfigError(f"config not found: {path}") from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from e
    return parse(raw, config_dir=path.parent)
