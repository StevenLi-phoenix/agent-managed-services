"""Deterministic gates that decide *whether a human-grade judgement is needed* -- before any LLM.

The log gate is the "signature + NEW / BACK / FLAP" design the DSH operator session already runs
for global-awareness (``ga-watch``): a cause is reported when it is new, when it comes back after
a long silence, or when it flaps -- and at most once per cooldown while it flaps. Everything else
is counted, not reported. Job and health gates are edge-triggered with a repeat interval.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ams.ops.config import GateConfig, ServiceConfig
from ams.ops.observe.launchd import JobState

HIST_CAP = 200


def _due(st: dict[str, Any], key: str, now: float, interval: float) -> bool:
    """True when ``key`` was never set or ``interval`` has passed since it was."""
    last = st.get(key)
    return last is None or now - last >= interval


@dataclass(frozen=True)
class Incident:
    kind: str  # log | job_down | job_flap | job_exit | health | tailscale
    service: str
    signature: str
    headline: str
    reason: str  # NEW | BACK | FLAP | DOWN | STORM | EXIT | HEALTH | MISSING
    evidence: tuple[str, ...] = ()
    count: int = 1
    facts: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    @property
    def is_log(self) -> bool:
        return self.kind == "log"


class LogGate:
    """Per-signature novelty tracking, persisted in ``store.data['sigs']``."""

    def __init__(self, sigs: dict[str, Any], cfg: GateConfig) -> None:
        self.sigs, self.cfg = sigs, cfg

    def seed(self, service: str, sig: str, sample: str, now: float) -> None:
        """Baseline pass: remember the cause without reporting it."""
        e = self.sigs.setdefault(
            sig, {"first": now, "last_seen": now, "hist": [], "last_emit": 0.0, "service": service}
        )
        e.update(last_seen=now, base=True, sample=sample[:300], hist=[now])

    def observe(self, service: str, sig: str, sample: str, now: float) -> tuple[str, int] | None:
        """Count one occurrence. Returns ``(reason, count_in_window)`` when it should be opened."""
        e = self.sigs.get(sig)
        if e is None:
            self.sigs[sig] = {
                "first": now,
                "last_seen": now,
                "hist": [now],
                "last_emit": now,
                "service": service,
                "sample": sample[:300],
                "base": False,
            }
            return "NEW", 1
        gap = now - e["last_seen"]
        hist = [h for h in e["hist"] if now - h < self.cfg.flap_window_s][-HIST_CAP:]
        hist.append(now)
        e.update(hist=hist, last_seen=now, sample=sample[:300])
        if gap > self.cfg.new_window_s:
            e["last_emit"] = now
            return "BACK", len(hist)
        if len(hist) >= self.cfg.flap_count and now - e["last_emit"] > self.cfg.flap_cooldown_s:
            e["last_emit"] = now
            return "FLAP", len(hist)
        return None

    def prune(self, now: float, keep_s: float = 14 * 86400) -> int:
        stale = [k for k, v in self.sigs.items() if now - v["last_seen"] > keep_s]
        for k in stale:
            del self.sigs[k]
        return len(stale)


class JobGate:
    """Edge-triggered launchd incidents: down, restart storm, non-zero exit of an interval job."""

    def __init__(self, jobs: dict[str, Any], cfg: GateConfig) -> None:
        self.jobs, self.cfg = jobs, cfg

    def check(self, svc: ServiceConfig, job: JobState, now: float) -> list[Incident]:
        if svc.expect == "observe" or svc.launchd is None:
            return []
        st = self.jobs.setdefault(svc.id, {})
        out: list[Incident] = []
        facts = {
            "loaded": job.loaded,
            "state": job.state,
            "pid": job.pid,
            "runs": job.runs,
            "last_exit": job.last_exit,
            "last_signal": job.last_signal,
            "detail": job.detail,
        }
        if svc.expect == "running":
            if not job.running:
                st.setdefault("down_since", now)
                st["down_ticks"] = st.get("down_ticks", 0) + 1
                due = _due(st, "last_report", now, self.cfg.job_repeat_s)
                if st["down_ticks"] >= 2 and due:  # one tick of grace for a restart in progress
                    st["last_report"] = now
                    why = "未加载" if not job.loaded else f"状态 {job.state or '?'}"
                    out.append(
                        Incident(
                            "job_down",
                            svc.id,
                            f"{svc.id}|job_down",
                            f"{svc.id} 没有在运行（{why}）",
                            "DOWN",
                            facts=facts | {"down_for_s": int(now - st["down_since"])},
                        )
                    )
            else:
                for k in ("down_since", "down_ticks", "last_report"):
                    st.pop(k, None)
            if job.runs is not None:
                hist = [
                    (t, r)
                    for t, r in st.get("runs_hist", [])
                    if now - t <= self.cfg.flap_runs_window_s
                ]
                hist.append((now, job.runs))
                st["runs_hist"] = hist
                grew = job.runs - min(r for _, r in hist)
                if grew >= self.cfg.flap_runs and _due(
                    st, "storm_report", now, self.cfg.job_repeat_s
                ):
                    st["storm_report"] = now
                    out.append(
                        Incident(
                            "job_flap",
                            svc.id,
                            f"{svc.id}|job_flap",
                            f"{svc.id} 在 {int(self.cfg.flap_runs_window_s / 60)} 分钟内重启了 {grew} 次",  # noqa: E501
                            "STORM",
                            facts=facts | {"restarts": grew},
                        )
                    )
        elif svc.expect == "interval":
            key = (job.last_exit, job.runs)
            if job.last_exit not in (None, 0) and st.get("reported_exit") != list(key):
                st["reported_exit"] = list(key)
                out.append(
                    Incident(
                        "job_exit",
                        svc.id,
                        f"{svc.id}|job_exit",
                        f"{svc.id} 上一次运行退出码 {job.last_exit}",
                        "EXIT",
                        facts=facts,
                    )
                )
            elif job.last_exit in (None, 0):
                st.pop("reported_exit", None)
        st["runs"], st["pid"] = job.runs, job.pid
        return out


class HealthGate:
    def __init__(self, health: dict[str, Any], cfg: GateConfig) -> None:
        self.health, self.cfg = health, cfg

    def check(self, svc: ServiceConfig, ok: bool, detail: str, now: float) -> Incident | None:
        st = self.health.setdefault(svc.id, {"fails": 0})
        if ok:
            st.update(fails=0)
            st.pop("last_report", None)
            return None
        st["fails"] += 1
        if st["fails"] >= self.cfg.health_failures and _due(
            st, "last_report", now, self.cfg.job_repeat_s
        ):
            st["last_report"] = now
            return Incident(
                "health",
                svc.id,
                f"{svc.id}|health",
                f"{svc.id} 健康检查连续失败（{detail}）",
                "HEALTH",
                facts={"detail": detail, "consecutive_failures": st["fails"]},
            )
        return None
