"""The suppress-or-fix decision point.

This is the explicit interface between the supervisor core and the agent
loop. The supervisor turns everything it observes into an ``Event`` and asks a
``DecisionPolicy`` what to do. The core ships only a rule-based stub
(``DefaultPolicy``); the agent plugs in its own policy and an ``Escalation``
sink that receives what the policy decided to surface.

Trust boundary: the event text originates from the supervised process (and
therefore from whoever can influence its output). A policy must treat it as
data. Nothing here ever executes text from an event.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import IO, Protocol, runtime_checkable

from ams.events import Event, LogLine, ServiceExited, Severity, severity_of
from ams.schema import ServiceDecl

log = logging.getLogger("ams.decision")


class Action(Enum):
    SUPPRESS = "suppress"  # drop silently
    LOG = "log"  # keep in the harness log, do not surface
    ESCALATE = "escalate"  # surface to the agent loop for a possible fix
    RESTART = "restart"  # restart the service now
    STOP = "stop"  # stop the service and do not restart it


@dataclass(frozen=True)
class Decision:
    action: Action
    reason: str = ""


@dataclass(frozen=True)
class ServiceContext:
    """What a policy may know about the service when deciding."""

    decl: ServiceDecl
    attempt: int = 1  # current start attempt number
    consecutive_failures: int = 0
    running: bool = False


@runtime_checkable
class DecisionPolicy(Protocol):
    def decide(self, event: Event, ctx: ServiceContext) -> Decision: ...


@runtime_checkable
class Escalation(Protocol):
    """Where escalated events go. The agent loop implements this."""

    def escalate(self, event: Event, decision: Decision, ctx: ServiceContext) -> None: ...


# One HTTP access-log line, in the two shapes the services we supervise emit:
#   uvicorn:     INFO:     127.0.0.1:54312 - "GET /health HTTP/1.1" 200 OK
#   http.server: 127.0.0.1 - - [02/Sep/2026 10:11:12] "GET /health HTTP/1.1" 200 -
# Anchored on the loopback address so a request from anywhere else never matches.
_LOOPBACK_ACCESS_RE = re.compile(
    r"\b127\.0\.0\.1(?::\d+)?\s+-\s+(?:-\s+)?(?:\[[^\]]*\]\s+)?"
    r'"(?P<method>GET|HEAD)\s+(?P<path>\S+)\s+HTTP/\d(?:\.\d)?"\s+(?P<status>\d{3})'
)


def is_self_probe(event: LogLine, ctx: ServiceContext) -> bool:
    """True for an access-log line produced by the harness's own health probe.

    Narrow on purpose: the request must come from loopback, use GET/HEAD, hit
    exactly the declared ``health.path`` and have answered 2xx/3xx. A 500 on the
    health path is the single most interesting line the service can emit and is
    never suppressed.

    This is noise control, not a security boundary: the log text is written by
    the service, so a service could forge a line to have it dropped -- but a
    service that wants to hide output can simply not print it.
    """
    health = ctx.decl.health
    if health.kind != "http":
        return False
    m = _LOOPBACK_ACCESS_RE.search(event.text)
    if m is None:
        return False
    if m.group("path").split("?", 1)[0] != health.path:
        return False
    return 200 <= int(m.group("status")) < 400


@dataclass
class DefaultPolicy:
    """Rule-based stub.

    - Log lines at or above ``escalate_at`` are escalated, the rest are logged.
    - The harness's own health-probe access lines are dropped entirely
      (``suppress_self_probes``): the probe interval otherwise makes every HTTP
      service a steady log source that says nothing the ``HealthChanged`` events
      do not already say.
    - Service exits follow the declaration's restart policy (RESTART or STOP),
      except an ``expected`` one -- an exit the harness asked for is a fact to
      record, not a decision to make. The supervisor escalates terminal states
      (retry exhaustion, stop after a crash) itself, so the agent always sees
      them regardless of the policy.
    - Everything else is logged.
    """

    escalate_at: Severity = Severity.WARNING
    suppress_self_probes: bool = True

    def decide(self, event: Event, ctx: ServiceContext) -> Decision:
        if isinstance(event, ServiceExited):
            return self._decide_exit(event, ctx)
        if isinstance(event, LogLine):
            if self.suppress_self_probes and is_self_probe(event, ctx):
                return Decision(Action.SUPPRESS, "harness health probe access log")
            if event.severity >= self.escalate_at:
                return Decision(Action.ESCALATE, f"{event.severity.name} on {event.stream}")
            return Decision(Action.LOG)
        if severity_of(event) >= self.escalate_at:
            return Decision(Action.ESCALATE, type(event).__name__)
        return Decision(Action.LOG)

    @staticmethod
    def _decide_exit(event: ServiceExited, ctx: ServiceContext) -> Decision:
        if event.expected:
            # stop / kill / shutdown / the stop half of a restart. The
            # supervisor already knows what happens next (``desired`` and
            # ``restart_pending``), and it ignores a policy RESTART for a
            # service an operator stopped anyway. Returning LOG keeps a routine
            # reload out of the escalation channel for policies that escalate
            # exits, without touching the unexpected-exit path below.
            return Decision(Action.LOG, "expected exit (harness-initiated)")
        policy = ctx.decl.restart
        if policy.policy == "never":
            return Decision(Action.STOP, "restart.policy=never")
        if event.ok and policy.policy == "on-failure":
            return Decision(Action.STOP, "clean exit with restart.policy=on-failure")
        if ctx.consecutive_failures >= policy.max_retries:
            return Decision(
                Action.STOP, f"{ctx.consecutive_failures} consecutive failures >= max_retries"
            )
        return Decision(Action.RESTART, "restart.policy=" + policy.policy)


@dataclass
class JsonLinesEscalation:
    """Stub sink: one JSON object per escalated event on a stream (default stdout).

    The agent loop reads these lines. Intended for ``ams run`` until the agent
    provides a real implementation.
    """

    stream: IO[str] = field(default_factory=lambda: sys.stdout)

    def escalate(self, event: Event, decision: Decision, ctx: ServiceContext) -> None:
        record = {
            "kind": type(event).__name__,
            "service_id": getattr(event, "service_id", None),
            "action": decision.action.value,
            "reason": decision.reason,
            "event": asdict(event),
            "attempt": ctx.attempt,
            "consecutive_failures": ctx.consecutive_failures,
        }
        self.stream.write(json.dumps(record, default=str) + "\n")
        self.stream.flush()


class NullEscalation:
    def escalate(self, event: Event, decision: Decision, ctx: ServiceContext) -> None:
        log.info("escalated %s: %s", type(event).__name__, decision.reason)
