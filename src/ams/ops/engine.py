"""The tick of ``ams watch``: observe -> gate -> (freeze | fix) -> escalate.

What mini-ops did with a model (suppress or act) is split: the safe cure for an incident is chosen
by rules (:mod:`ams.ops.policy`) and guarded by :class:`ams.ops.actions.Executor`; whether the
owner has to hear about it is the operator's call (DSH reads the escalation journal through
ga-watch), so every incident is escalated, together with what was done about it.
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ams.escalations import EscalationJournal
from ams.events import Severity
from ams.ops import actions as actions_mod
from ams.ops import freeze as freeze_mod
from ams.ops import policy
from ams.ops.actions import ActionRequest, ActionResult, Executor
from ams.ops.config import Config, ServiceConfig
from ams.ops.forward import Forwarder, render
from ams.ops.gate import HealthGate, Incident, JobGate, LogGate
from ams.ops.observe import health, launchd, logs
from ams.ops.observe.launchd import JobState
from ams.ops.runner import Runner
from ams.ops.runner import run as default_run
from ams.ops.store import Store
from ams.ops.text import clip, sanitize, signature

log = logging.getLogger("ams.ops.engine")

TAILSCALE_EVERY_S = 300
EVIDENCE_LINES = 3
TRACE_LINES = 14  # a Traceback needs its tail to be diagnosable
_TRACE = ("Traceback", "ERROR: Exception")


@dataclass
class Env:
    """Everything the engine touches outside itself, so tests can replace it."""

    runner: Runner = default_run
    read_job: Callable[[str, Runner], JobState] = launchd.read_job
    http_probe: Callable[..., health.Probe] = health.http
    socket_probe: Callable[[Path], health.Probe] = health.unix_socket
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.time


@dataclass
class TickReport:
    incidents: int = 0
    muted: int = 0
    escalated: int = 0
    actions: list[ActionResult] = field(default_factory=list)


class Engine:
    def __init__(
        self,
        cfg: Config,
        store: Store,
        forwarder: Forwarder,
        env: Env | None = None,
        journal: EscalationJournal | None = None,
    ) -> None:
        self.cfg, self.store, self.forwarder = cfg, store, forwarder
        self.journal = journal
        self.env = env or Env()
        d = store.data
        self.logs = LogGate(d["sigs"], cfg.gate)
        self.jobs = JobGate(d["jobs"], cfg.gate)
        self.health = HealthGate(d["health"], cfg.gate)
        self.executor = Executor(cfg, d["actions"], runner=self.env.runner, sleep=self.env.sleep)
        self._ticks = 0
        self._ts_last = 0.0

    # ---- observation --------------------------------------------------------------------
    def _log_incidents(self, svc: ServiceConfig, now: float, seed: bool) -> list[Incident]:
        out: list[Incident] = []
        seen: set[str] = set()
        for path in svc.logs:
            entry = self.store.data["offsets"].setdefault(str(path), {})
            lines, skipped = logs.read_new(path, entry, seed=seed)
            if skipped:
                log.warning("%s: skipped %d bytes of a log flood", path.name, skipped)
            hits = list(logs.noteworthy(svc, lines))
            for ln, sev in hits:
                sig = signature(svc.id, ln)
                if seed:
                    self.logs.seed(svc.id, sig, ln, now)
                    continue
                opened = self.logs.observe(svc.id, sig, ln, now)
                if opened is None or sig in seen:
                    continue
                seen.add(sig)
                idx = lines.index(ln)
                span = TRACE_LINES if ln.lstrip().startswith(_TRACE) else EVIDENCE_LINES
                ctx = tuple(lines[idx : idx + span])
                out.append(
                    Incident(
                        "log",
                        svc.id,
                        sig,
                        clip(sanitize(ln, 160), 160),
                        opened[0],
                        evidence=ctx,
                        count=opened[1],
                        facts={"severity": Severity(sev).name},
                        ts=now,
                    )
                )
        return out

    def _observe(self, now: float, seed: bool) -> tuple[list[Incident], dict[str, dict]]:
        incidents: list[Incident] = []
        status: dict[str, dict] = {}
        for svc in self.cfg.services:
            info: dict[str, Any] = {}
            if svc.launchd:
                job = self.env.read_job(svc.launchd, self.env.runner)
                info["job"] = dataclasses.asdict(job)
                if not seed:
                    incidents += self.jobs.check(svc, job, now)
            if svc.health_url or svc.health_socket:
                pr = (
                    self.env.http_probe(svc.health_url, accept=svc.health_accept)
                    if svc.health_url
                    else self.env.socket_probe(svc.health_socket)
                )  # type: ignore[arg-type]
                info["health"] = {"ok": pr.ok, "detail": pr.detail}
                if not seed:
                    inc = self.health.check(svc, pr.ok, pr.detail, now)
                    if inc:
                        incidents.append(inc)
            if svc.logs:
                incidents += self._log_incidents(svc, now, seed)
            if "advertise_tailscale" in svc.actions and not seed:
                inc = self._tailscale(svc, now)
                if inc:
                    incidents.append(inc)
            status[svc.id] = info
        return incidents, status

    def _tailscale(self, svc: ServiceConfig, now: float) -> Incident | None:
        if now - self._ts_last < TAILSCALE_EVERY_S:
            return None
        self._ts_last = now
        missing = actions_mod.missing_services(self.cfg, self.env.runner)
        st = self.store.data["tailscale"]
        if not missing:
            st.pop("last_report", None)
            return None
        last = st.get("last_report")
        if last is not None and now - last < self.cfg.gate.job_repeat_s:
            return None
        st["last_report"] = now
        return Incident(
            "tailscale",
            svc.id,
            f"{svc.id}|tailscale",
            f"Tailscale Service 未被本机 advertise：{', '.join(missing)}",
            "MISSING",
            facts={"missing": missing},
            ts=now,
        )

    # ---- decisions ----------------------------------------------------------------------
    def _journal(self, decision: str, inc: Incident, **extra: Any) -> None:
        """One JSON line per decision -- the audit trail (and what an operator session reads)."""
        if self.journal is not None:
            self.journal.append(
                {
                    "decision": decision,
                    "service": inc.service,
                    "kind": inc.kind,
                    "reason": inc.reason,
                    "signature": inc.signature,
                    "headline": inc.headline,
                    "count": inc.count,
                    **extra,
                }
            )

    def handle(
        self,
        inc: Incident,
        freezes: dict[str, freeze_mod.Freeze],
        now: float,
        report: TickReport,
    ) -> None:
        fz = freeze_mod.lookup(freezes, inc.service)
        if fz is not None and fz.mute:
            self._journal("muted", inc, freeze=fz.reason)
            report.muted += 1
            return
        result: ActionResult | None = None
        name = policy.choose(inc, self.cfg.service(inc.service))
        if name:
            result = self.executor.execute(ActionRequest(name, inc.service, {}), freezes, now)
            report.actions.append(result)
        self._journal("escalate", inc, action=result.line() if result else None)
        digest = hashlib.sha1(inc.signature.encode()).hexdigest()[:10]  # noqa: S324 - not security
        key = f"ops-{digest}-{time.strftime('%Y%m%d%H%M', time.localtime(now))}"
        if self.forwarder.enqueue(key, render(self.cfg.forward.origin, inc, result)):
            report.escalated += 1

    # ---- the loop body ------------------------------------------------------------------
    def tick(self) -> TickReport:
        now = self.env.clock()
        report = TickReport()
        data = self.store.data
        seed = not data["baselined"]
        freezes = freeze_mod.load(self.cfg.config_dir / "freeze.json", now)
        incidents, status = self._observe(now, seed)
        if seed:
            data["baselined"] = True
            log.info("baseline seeded: %d causes", len(data["sigs"]))
        for inc in incidents:
            report.incidents += 1
            self.handle(inc, freezes, now, report)
        self.forwarder.flush()
        self._ticks += 1
        if self._ticks % 120 == 0:
            self.logs.prune(now)
        data["status"] = {
            "ts": now,
            "services": status,
            "outbox": len(data["outbox"]),
            "freeze": sorted(freezes),
        }
        self.store.save()
        return report
