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


@dataclass
class DefaultPolicy:
    """Rule-based stub.

    - Log lines at or above ``escalate_at`` are escalated, the rest are logged.
    - Service exits follow the declaration's restart policy (RESTART or STOP).
      The supervisor escalates terminal states (retry exhaustion, stop after a
      crash) itself, so the agent always sees them regardless of the policy.
    - Everything else is logged.
    """

    escalate_at: Severity = Severity.WARNING

    def decide(self, event: Event, ctx: ServiceContext) -> Decision:
        if isinstance(event, ServiceExited):
            return self._decide_exit(event, ctx)
        if isinstance(event, LogLine):
            if event.severity >= self.escalate_at:
                return Decision(Action.ESCALATE, f"{event.severity.name} on {event.stream}")
            return Decision(Action.LOG)
        if severity_of(event) >= self.escalate_at:
            return Decision(Action.ESCALATE, type(event).__name__)
        return Decision(Action.LOG)

    @staticmethod
    def _decide_exit(event: ServiceExited, ctx: ServiceContext) -> Decision:
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
