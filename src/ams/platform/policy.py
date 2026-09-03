"""Platform decision policy: dedupe by cause, gate on post-sync health, tame Caddy.

`DefaultPolicy` is deliberately memoryless -- it judges one event against one
`ServiceContext` and never looks at what it said a second ago. That is the right
core behaviour, and it is the wrong *fleet* behaviour: 21 services behind one
gateway produce the same cause hundreds of times (a heartbeat against a registry
that has not started, an access log line per request, a crash loop restarting on
a 1 s backoff), and an escalation channel that repeats one cause 300 times is
indistinguishable from no escalation channel at all.

`PlatformPolicy` wraps `DefaultPolicy` -- it always delegates first and then
refines -- and adds four things the platform needs:

1. **Dedupe by cause.** A *cause key* is ``(service_id, kind, normalized text)``
   where the normalizer strips timestamps, pids, ports, hex ids and numbers of
   three digits or more, so two lines that differ only in "which request" or
   "which pid" collapse to one cause. A cause escalates once per ``window_s``;
   later occurrences become ``LOG`` with ``reason="deduped (n=k)"``, and when the
   window closes a single summary escalation says "repeated k times in window".
2. **A post-sync health gate.** `<state>/platform/state.json`
   (`docs/platform-sidecars.md`, written by `ams.platform.sync`, T3.1) says which
   sha each service is being driven to and when it entered its current stage. A
   service that has not reached ``healthy`` within ``health_grace_s`` of that
   stage escalates once, carrying **both** ``sha`` and ``prev_sha`` and the
   suggested action "rollback to prev_sha". The rollback itself is T4.3; this
   module only recommends, it never acts.
3. **Caddy.** Caddy is not an SDK service: it writes one JSON object per line
   (`[logging] format = "json"`, D21/D19). Access lines below 500 are noise,
   500s are the interesting ones (deduped per path+status), and TLS warnings are
   expected in Phase A, which is plain HTTP by decision (D21).
4. **Registry heartbeat noise.** Every SDK service heartbeats to the registry.
   Until the registry answers, every one of them logs a connection-refused error
   per interval -- an accurate report of a condition nobody needs told 20 times.

Trust boundary, unchanged from `ams.decision`: log text comes from the supervised
process. This module only *matches* and *counts* it. The single `json.loads` here
is guarded, reads four fixed keys and executes nothing (same rule as
`ams.events._from_json`).

The supervisor never imports this module; `ams run --policy platform` selects it.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ams.decision import (
    Action,
    Decision,
    DecisionPolicy,
    DefaultPolicy,
    Escalation,
    ServiceContext,
)
from ams.events import Event, HealthChanged, LogLine, ServiceExited, Severity
from ams.schema import DeclError, ServiceDecl, StartSpec
from ams.state import StateDir

log = logging.getLogger("ams.platform.policy")

__all__ = [
    "CADDY_SERVICE_ID",
    "DEFAULT_HEALTH_GRACE_S",
    "DEFAULT_WINDOW_S",
    "REGISTRY_SERVICE_ID",
    "DedupeSummary",
    "DedupeVerdict",
    "DedupingEscalation",
    "EscalationDeduper",
    "EscalationRecord",
    "PlatformPolicy",
    "cause_key",
    "cause_key_for_event",
    "make_policy",
    "normalize_cause_text",
    "platform_state_path",
]

# 10 minutes: long enough that a 10 s health probe or a 1 s restart backoff
# cannot refill the channel, short enough that a condition still true after it
# gets said again rather than going quiet forever. See DECISIONS D24.
DEFAULT_WINDOW_S = 600.0
# 5 minutes after a sync stage began. `start_period_s` for the fleet is 120 s
# (PLAN-allin Q8), so this leaves room for a slow first start plus one restart
# before the gate calls it a failure.
DEFAULT_HEALTH_GRACE_S = 300.0

CADDY_SERVICE_ID = "caddy"
REGISTRY_SERVICE_ID = "registry"

# Cause keys are compared as whole strings; a very long line (a traceback body,
# a 4 KiB JSON blob) is truncated so the key stays bounded. Two causes sharing
# this much prefix are the same cause for escalation purposes.
MAX_CAUSE_TEXT = 240


# ---------------------------------------------------------------- normalization
# Ordered: the specific shapes first, the generic "any long number" last, so a
# timestamp is never partially eaten by the number rule.
_NORM_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    # ISO-8601, with or without fraction and offset: 2026-09-02T13:04:07.123Z
    (
        re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?"),
        "<ts>",
    ),
    # http.server / CLF: [02/Sep/2026 10:11:12] or [02/Sep/2026:10:11:12 +0000]
    (re.compile(r"\[\d{1,2}/[A-Za-z]{3}/\d{4}[ :]\d{2}:\d{2}:\d{2}[^\]]*\]"), "<ts>"),
    # A bare wall clock left over from a log prefix.
    (re.compile(r"\b\d{1,2}:\d{2}:\d{2}(?:[.,]\d+)?\b"), "<ts>"),
    # Unix epoch, seconds with a fraction (Caddy's own "ts" field when unformatted).
    (re.compile(r"\b1[0-9]{9}\.[0-9]+\b"), "<ts>"),
    # An address, optionally with a port: 127.0.0.1:54312
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?"), "<addr>"),
    # pid=1234 / pid 1234 / PID: 1234
    (re.compile(r"\b[Pp][Ii][Dd]\s*[=: ]\s*\d+"), "pid=<n>"),
    # A full-length id: a git sha (40), a uuid without dashes (32), a sha256
    # (64). Matched without requiring a letter, because "the sha that happens to
    # be all digits" must still collapse against one that is not -- otherwise
    # one commit in ten million escalates twice.
    (re.compile(r"\b[0-9a-fA-F]{32,64}\b"), "<hex>"),
    # A shorter hex id (an abbreviated sha, a request id) -- here the letter is
    # required, so a plain 7-digit decimal falls through to the number rule
    # below and gets one placeholder rather than two depending on its digits.
    (re.compile(r"\b(?=[0-9a-fA-F]*[a-fA-F])[0-9a-fA-F]{7,}\b"), "<hex>"),
    # Anything else numeric and long enough to be an identity rather than a
    # quantity: a port, a byte count, a line number. Two digits are left alone
    # ("5xx", "3 retries") because collapsing them loses more than it gains.
    (re.compile(r"\b\d{3,}\b"), "<n>"),
)

_WS_RE = re.compile(r"\s+")


def normalize_cause_text(text: str) -> str:
    """Collapse the parts of a log line that vary per occurrence.

    Two lines that differ only in their timestamp, pid, port, hex id or any
    number of three digits or more normalize to the same string, which is what
    makes them one *cause* rather than two events.

    Known over-collapse, accepted: an all-hex-letter word of seven characters or
    more (``defaced``) becomes ``<hex>``. It costs nothing -- both occurrences
    still escalate once and the summary quotes the normalized form, so an
    operator sees exactly what was collapsed.
    """
    out = text
    for pattern, replacement in _NORM_RULES:
        out = pattern.sub(replacement, out)
    out = _WS_RE.sub(" ", out).strip()
    return out[:MAX_CAUSE_TEXT]


def cause_key(service_id: str, kind: str, text: str) -> str:
    """The identity of a cause: ``service|kind|normalized text``.

    ``kind`` separates event classes that could otherwise normalize to the same
    string (an exit and a log line both reduced to ``<n>``), and the service id
    keeps one noisy service from silencing the same symptom in another.
    """
    return f"{service_id or '-'}|{kind}|{normalize_cause_text(text)}"


def _event_text(event: Event) -> str:
    """The part of an event that identifies *what happened*, not *when*.

    Deliberately excludes anything that changes on every occurrence of the same
    cause: `uptime_s`, `attempt`, `pid`, `ts`. A service that crashes at 0.4 s
    and again at 0.6 s has one cause, not two.
    """
    if isinstance(event, LogLine):
        return event.text
    if isinstance(event, ServiceExited):
        return f"exit code={event.exit_code} signal={event.signal} expected={event.expected}"
    if isinstance(event, HealthChanged):
        return f"healthy={event.healthy} {event.detail}"
    return type(event).__name__


def cause_key_for_event(event: Event, kind: str | None = None) -> str:
    service_id = getattr(event, "service_id", "") or "-"
    return cause_key(service_id, kind or type(event).__name__, _event_text(event))


# --------------------------------------------------------------------- deduper


@dataclass(frozen=True)
class DedupeVerdict:
    first: bool  # True = this occurrence opens a window and should escalate
    count: int  # occurrences in the current window, including this one


@dataclass(frozen=True)
class DedupeSummary:
    """What a closed window is worth saying: one line, not k lines."""

    key: str
    count: int
    first_ts: float
    last_ts: float

    @property
    def service_id(self) -> str:
        return self.key.split("|", 1)[0]

    @property
    def kind(self) -> str:
        parts = self.key.split("|", 2)
        return parts[1] if len(parts) > 2 else "?"

    @property
    def text(self) -> str:
        parts = self.key.split("|", 2)
        cause = parts[2] if len(parts) > 2 else self.key
        return f"repeated {self.count} times in window: {cause}"


@dataclass
class _Window:
    start: float
    last: float
    count: int


class EscalationDeduper:
    """Counts occurrences of a cause key inside a rolling window.

    Standalone on purpose: `PlatformPolicy` uses it for supervisor events, and
    `ams.platform.sync` (T3.1) can wrap it around its own JSONL sink with
    `DedupingEscalation` -- its `PlatformSync` records never pass through the
    supervisor, so its repeated translate/provision failures need the same
    treatment from a different direction.

    Time is always passed in, never read: the policy owns the clock so tests can
    inject one.
    """

    def __init__(self, window_s: float = DEFAULT_WINDOW_S) -> None:
        if window_s <= 0:
            raise ValueError(f"window_s must be positive, got {window_s}")
        self.window_s = float(window_s)
        self._windows: dict[str, _Window] = {}
        self._pending: list[DedupeSummary] = []

    def __len__(self) -> int:
        return len(self._windows)

    def observe(self, key: str, now: float) -> DedupeVerdict:
        """Record one occurrence of ``key``. ``first`` means "escalate this one".

        A window whose age has passed ``window_s`` is rolled over here rather
        than waiting for `expire`, so a caller that never ticks still gets
        correct re-escalation; the summary for the window just closed is queued
        for the next `expire`.
        """
        window = self._windows.get(key)
        if window is None or now - window.start >= self.window_s:
            if window is not None and window.count > 1:
                self._pending.append(DedupeSummary(key, window.count, window.start, window.last))
            self._windows[key] = _Window(start=now, last=now, count=1)
            return DedupeVerdict(first=True, count=1)
        window.count += 1
        window.last = now
        return DedupeVerdict(first=False, count=window.count)

    def expire(self, now: float) -> list[DedupeSummary]:
        """Close every window older than ``window_s`` and return what to say.

        A window with a single occurrence produces no summary -- it was already
        escalated in full and "repeated 1 times" is not news. Closing the window
        also re-arms the cause: its next occurrence escalates again.
        """
        out = self._pending
        self._pending = []
        for key, window in list(self._windows.items()):
            if now - window.start >= self.window_s:
                del self._windows[key]
                if window.count > 1:
                    out.append(DedupeSummary(key, window.count, window.start, window.last))
        return out

    def reset(self) -> None:
        self._windows.clear()
        self._pending.clear()


@dataclass(frozen=True)
class EscalationRecord:
    """One thing to hand an `Escalation` sink: the triple its protocol wants."""

    event: Event
    decision: Decision
    ctx: ServiceContext


@dataclass
class DedupingEscalation:
    """An `Escalation` that drops repeats of a cause and summarizes on `flush`.

    For callers that produce escalations *outside* the supervisor loop -- the
    sync loop's `PlatformSync` records, which arrive as JSONL on stdout and never
    reach a `DecisionPolicy`. Wrap the real sink, keep escalating, call `flush`
    once per tick.
    """

    inner: Escalation
    deduper: EscalationDeduper = field(default_factory=EscalationDeduper)
    clock: Callable[[], float] = time.time
    kind: str | None = None  # override the event-class kind in the cause key

    _seen: dict[str, EscalationRecord] = field(default_factory=dict, init=False, repr=False)

    def escalate(self, event: Event, decision: Decision, ctx: ServiceContext) -> None:
        now = self.clock()
        key = cause_key_for_event(event, self.kind)
        verdict = self.deduper.observe(key, now)
        if verdict.first:
            self._seen[key] = EscalationRecord(event, decision, ctx)
            self.inner.escalate(event, decision, ctx)
        else:
            log.debug("deduped (n=%d) %s", verdict.count, key)

    def flush(self, now: float | None = None) -> list[EscalationRecord]:
        """Emit one summary per closed window. Returns what was emitted."""
        moment = self.clock() if now is None else now
        out: list[EscalationRecord] = []
        for summary in self.deduper.expire(moment):
            seed = self._seen.pop(summary.key, None)
            ctx = seed.ctx if seed is not None else _placeholder_ctx(summary.service_id)
            record = EscalationRecord(
                LogLine(summary.service_id, "stderr", summary.text, Severity.WARNING, ts=moment),
                Decision(Action.ESCALATE, f"{summary.count} occurrences in one window"),
                ctx,
            )
            out.append(record)
            _safe_escalate(self.inner, record)
        return out


def _safe_escalate(sink: Escalation, record: EscalationRecord) -> None:
    try:
        sink.escalate(record.event, record.decision, record.ctx)
    except Exception as e:  # a broken sink must not take down the harness
        log.exception("escalation sink raised: %s", e)


# ------------------------------------------------------------------ placeholder


_PLACEHOLDER_ARGV = ("<platform-policy>",)


def _placeholder_ctx(service_id: str) -> ServiceContext:
    """A context for a service the policy has never seen an event from.

    The health gate reads `state.json`, which can name a service that has not
    started yet (or one whose declaration failed to load), and the `Escalation`
    protocol wants a `ServiceContext` regardless. Same shape as the supervisor's
    `_ORPHAN_CTX`.
    """
    try:
        decl = ServiceDecl(id=service_id, start=StartSpec(argv=_PLACEHOLDER_ARGV))
    except DeclError:
        # An id out of `SERVICE_ID_RE` cannot come from our own sync loop, but
        # the file is on disk and could have been edited. Do not raise inside a
        # decision path over it.
        decl = ServiceDecl(id="unknown", start=StartSpec(argv=_PLACEHOLDER_ARGV))
    return ServiceContext(decl)


# ------------------------------------------------------------------ caddy rules

# Caddy's access logger. Matched exactly, not by prefix: `http.log.error` is a
# different logger and must keep its default treatment.
CADDY_ACCESS_LOGGER = "http.log.access"
_TLS_MSG_RE = re.compile(r"\b(tls|certificates?|acme)\b", re.IGNORECASE)
# Caddy's other two start-up/shutdown WARNINGs, both statements of fact about a
# configuration the operator chose. Added at T4.1 after the live run: with only
# `_TLS_MSG_RE` in place, `admin off` (D21/Q3) escalated on every Caddy start
# and the SIGTERM line on every stop — 2 of the 4 warnings
# `.claude/state/platform-layer0.md` §7 classified as noise. Matched on the
# message, not the logger, because `admin` is also the logger of real admin-API
# errors we do want to hear about.
_DELIBERATE_MSG_RE = re.compile(
    r"(admin endpoint disabled|exiting; byeee)",
    re.IGNORECASE,
)


def _guarded_json(text: str) -> dict[str, Any] | None:
    """`json.loads` on untrusted text, or None. Executes nothing, guesses nothing."""
    stripped = text.lstrip()
    if not stripped.startswith("{"):
        return None
    try:
        obj = json.loads(stripped)
    except (ValueError, RecursionError):
        return None
    return obj if isinstance(obj, dict) else None


def _caddy_status(obj: dict[str, Any]) -> int | None:
    status = obj.get("status")
    # bool is an int subclass; `"status": true` is not a status.
    if isinstance(status, bool) or not isinstance(status, int):
        return None
    return status


def _caddy_path(obj: dict[str, Any]) -> str:
    request = obj.get("request")
    if isinstance(request, dict):
        uri = request.get("uri")
        if isinstance(uri, str):
            return uri.split("?", 1)[0][:MAX_CAUSE_TEXT] or "/"
    return "-"


# ------------------------------------------------------- registry heartbeat noise

_HEARTBEAT_RE = re.compile(
    r"\b(heartbeat|heartbeats|acl[-_ ]?refresh|refresh[-_ ]?acl|service[-_ ]?registration)\b",
    re.IGNORECASE,
)
_REFUSED_RE = re.compile(
    r"(connection\s+refused|econnrefused|errno\s+111|"
    r"max\s+retries\s+exceeded|failed\s+to\s+establish\s+a\s+new\s+connection|"
    r"connection\s+aborted|cannot\s+connect)",
    re.IGNORECASE,
)


# --------------------------------------------------------------- platform state


def platform_state_path(state: StateDir) -> Path:
    """`<state>/platform/state.json` -- the sync loop's fleet record (T3.1)."""
    return state.root / "platform" / "state.json"


def _mount_sidecar_path(state: StateDir, service_id: str) -> Path:
    """`<state>/platform/mounts/<id>.json` (`docs/platform-sidecars.md`)."""
    return state.root / "platform" / "mounts" / f"{service_id}.json"


def _is_static_mount(state: StateDir, service_id: str) -> bool:
    """Whether the mount sidecar for ``service_id`` declares ``kind: static``.

    A ``kind: static`` mount has no process and no registry record, so
    ``declared`` is its terminal stage (`docs/platform-sidecars.md`,
    `sync._phase_declare`) -- reaching it is success, not a sync stuck partway.
    Absent or malformed sidecar -> False (fail-safe): the caller then falls back
    to treating the record as an ordinary service, which is the behaviour that
    existed before this check (DECISIONS D28 open item).
    """
    try:
        data = json.loads(_mount_sidecar_path(state, service_id).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return False
    return isinstance(data, dict) and data.get("kind") == "static"


def _pool_suffix(record: dict[str, Any]) -> str:
    """`" (pool of N: a, b, c)"`, or `""` when the record names no pool members.

    A pool's `ServiceRecord` carries `pool_members` (PLAN-pool §5.7, written by
    `ams.platform.sync` -- not this module's job to write, only to read
    defensively). Reading it here turns "one process crashed" into "N services
    are down", which is the whole point of surfacing it in the crash-loop
    escalation rather than leaving an operator to go look up the pool roster.
    """
    members = record.get("pool_members")
    if not isinstance(members, list):
        return ""
    names = [m for m in members if isinstance(m, str)]
    if not names:
        return ""
    return f" (pool of {len(names)}: {', '.join(names)})"


def _parse_iso_z(value: Any) -> float | None:
    """`2026-09-02T13:04:07Z` -> epoch seconds, or None for anything else.

    The sidecar contract says UTC ISO-8601 with a `Z`, second precision. A value
    that is not that is a corrupt record, and a corrupt record must not make the
    gate fire (or not fire) on a guess -- it returns None and the service is
    skipped, which the caller logs.
    """
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.timestamp()


# --------------------------------------------------------------------- policy


@dataclass(frozen=True)
class _Refined:
    decision: Decision
    key: str | None = None  # cause key override; None = derive from the event


@dataclass
class PlatformPolicy:
    """`DefaultPolicy` plus fleet memory. Delegates first, then refines.

    Every event goes through `base` before this class looks at it, so every
    `DefaultPolicy` behaviour survives verbatim: self-probe suppression (D19),
    `expected` exits logged rather than restarted, exits following the
    declaration's restart policy. This class only ever turns an `ESCALATE` into a
    `LOG` (dedupe, heartbeat noise) or a `SUPPRESS`/`ESCALATE` for Caddy's own
    JSON, and it never changes a `RESTART` or a `STOP` -- those drive supervisor
    behaviour, and a policy that quietly stopped restarting a service to reduce
    log volume would be a much worse bug than the noise it fixed.

    Escalations this class *adds* (a crash loop after a sync, a health-gate
    failure, a window summary) cannot be returned as a `Decision`: one event
    yields one decision, and the crash-loop case must still return the `RESTART`
    the supervisor needs. They are queued and drained by `flush`.

    `flush(now)` is the tick: it runs the health gate, closes expired dedupe
    windows and emits everything queued. `ams run --policy platform` wires it
    into `Supervisor.run_forever(on_iteration=...)`, which runs after every
    select() iteration -- call it yourself if you drive the supervisor directly.
    """

    state: StateDir | None = None
    base: DecisionPolicy = field(default_factory=DefaultPolicy)
    window_s: float = DEFAULT_WINDOW_S
    health_grace_s: float = DEFAULT_HEALTH_GRACE_S
    caddy_service_id: str = CADDY_SERVICE_ID
    registry_service_id: str = REGISTRY_SERVICE_ID
    clock: Callable[[], float] = time.time
    escalation: Escalation | None = None

    _deduper: EscalationDeduper = field(init=False, repr=False)
    _pending: list[EscalationRecord] = field(default_factory=list, init=False, repr=False)
    _last_ctx: dict[str, ServiceContext] = field(default_factory=dict, init=False, repr=False)
    _registry_ready_at: float | None = field(default=None, init=False, repr=False)
    # (service_id, sha, stage) already reported by the health gate. Not the
    # deduper: the gate says "escalate ONCE", and a condition keyed on the sha
    # re-arms when the sync loop moves the service, not when a clock runs out.
    _gated: set[tuple[str, str, str]] = field(default_factory=set, init=False, repr=False)
    _state_cache: dict[str, Any] = field(default_factory=dict, init=False, repr=False)
    _state_cache_key: tuple[int, int] | None = field(default=None, init=False, repr=False)
    _state_warned: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        self._deduper = EscalationDeduper(self.window_s)

    # ------------------------------------------------------------- DecisionPolicy

    def decide(self, event: Event, ctx: ServiceContext) -> Decision:
        now = self.clock()
        service_id = getattr(event, "service_id", "") or ""
        if service_id:
            self._last_ctx[service_id] = ctx
        self._note_registry_ready(event, now)

        base = self.base.decide(event, ctx)
        refined = self._refine(event, ctx, base, now)
        if refined.decision.action is not Action.ESCALATE:
            return refined.decision

        key = refined.key or cause_key_for_event(event)
        verdict = self._deduper.observe(key, now)
        if verdict.first:
            return refined.decision
        return Decision(Action.LOG, f"deduped (n={verdict.count}): {refined.decision.reason}")

    # ------------------------------------------------------------------- the tick

    def flush(self, now: float | None = None) -> list[EscalationRecord]:
        """Run the health gate, close expired windows, emit everything queued.

        Returns the records emitted. When `escalation` is set they have already
        been handed to it; the return value is for callers that supervise the
        policy directly (and for tests, which is how every rule here is checked).
        """
        moment = self.clock() if now is None else now
        self._health_gate(moment)
        for summary in self._deduper.expire(moment):
            self._queue(
                summary.service_id,
                summary.text,
                Severity.WARNING,
                f"{summary.count} occurrences in one window",
                moment,
            )
        out = self._pending
        self._pending = []
        if self.escalation is not None:
            for record in out:
                _safe_escalate(self.escalation, record)
        return out

    # --------------------------------------------------------------------- rules

    def _refine(self, event: Event, ctx: ServiceContext, base: Decision, now: float) -> _Refined:
        if isinstance(event, ServiceExited):
            self._maybe_crash_loop(event, ctx, now)
            return _Refined(base)
        if not isinstance(event, LogLine):
            return _Refined(base)
        if base.action is Action.SUPPRESS:
            # DefaultPolicy already dropped it (a self-probe access line, D19).
            # Nothing here re-surfaces what the core decided to drop.
            return _Refined(base)
        if event.service_id == self.caddy_service_id:
            caddy = self._caddy(event)
            if caddy is not None:
                return caddy
        if base.action is Action.ESCALATE and self._is_early_heartbeat(event):
            return _Refined(
                Decision(
                    Action.LOG,
                    "registry has not reported healthy yet; heartbeat failure is expected",
                )
            )
        return _Refined(base)

    def _caddy(self, event: LogLine) -> _Refined | None:
        """Caddy's JSON log. None = nothing to say, keep the delegated decision."""
        obj = _guarded_json(event.text)
        if obj is None:
            # Not JSON: a startup banner, a panic trace, a line from a Caddy that
            # has not configured its logger yet. Falls through to DefaultPolicy,
            # which classifies it by severity like any other service's output.
            return None
        if obj.get("logger") == CADDY_ACCESS_LOGGER:
            status = _caddy_status(obj)
            if status is None:
                return None
            if status < 500:
                return _Refined(Decision(Action.SUPPRESS, f"caddy access log ({status})"))
            path = _caddy_path(obj)
            # The status goes in the *kind*, not the text: the normalizer turns
            # any three-digit number into a placeholder, so a 502 and a 503 on
            # one path would otherwise be one cause. Only the path is
            # normalized, which is what we want -- /files/1234567 collapses.
            return _Refined(
                Decision(Action.ESCALATE, f"caddy {status} on {path}"),
                cause_key(event.service_id, f"caddy-access-{status}", path),
            )
        level = obj.get("level")
        is_warn = (isinstance(level, str) and level.strip().lower() in ("warn", "warning")) or (
            level is None and event.severity == Severity.WARNING
        )
        # Both the logger name and the message are checked: Caddy's TLS warnings
        # name the subsystem in the logger (`tls`, `tls.issuance`) and do not
        # always repeat it in the message ("stapling OCSP: no OCSP server").
        subject = " ".join(
            part for part in (obj.get("logger"), obj.get("msg")) if isinstance(part, str)
        )
        if is_warn and _TLS_MSG_RE.search(subject):
            # Phase A is plain HTTP by decision (D21): `auto_https off`, no
            # certificates to get. Caddy still warns about what it is not doing.
            return _Refined(
                Decision(Action.SUPPRESS, "TLS/certificate warning; Phase A is plain HTTP (D21)")
            )
        if is_warn and _DELIBERATE_MSG_RE.search(subject):
            return _Refined(
                Decision(Action.SUPPRESS, "caddy start/stop warning about a deliberate setting")
            )
        return None

    def _is_early_heartbeat(self, event: LogLine) -> bool:
        if self._registry_ready_at is not None:
            return False
        if event.service_id == self.registry_service_id:
            return False
        return bool(_HEARTBEAT_RE.search(event.text) and _REFUSED_RE.search(event.text))

    def _note_registry_ready(self, event: Event, now: float) -> None:
        """Learn when the registry came up, from the event stream itself.

        A policy is handed the context of the service the event belongs to and
        nothing else, so there is no way to ask "is the registry up?". The one
        registry fact that passes through here is its own `HealthChanged`, so
        that is what the heartbeat rule keys on. Latched: a registry that flaps
        later does not re-open the suppression window, because by then the other
        services' heartbeat failures are real news.
        """
        if self._registry_ready_at is not None:
            return
        if isinstance(event, HealthChanged) and event.healthy:
            if event.service_id == self.registry_service_id:
                self._registry_ready_at = now
                log.info("registry reported healthy; heartbeat noise suppression off")

    # ---------------------------------------------------------------- health gate

    def _maybe_crash_loop(self, event: ServiceExited, ctx: ServiceContext, now: float) -> None:
        """A service crash-looping right after we moved it to a new sha.

        Extra escalation, never a returned decision: the delegated `RESTART` has
        to survive. The value it adds over the supervisor's own retry-exhaustion
        escalation is the sha pair -- "this started when you moved it from X to
        Y" is the sentence that makes the fix obvious.
        """
        if event.expected or ctx.consecutive_failures < 2:
            return
        record = self._service_record(event.service_id)
        if record is None:
            return
        synced_at = _parse_iso_z(record.get("updated_at"))
        if synced_at is None or now - synced_at > self.health_grace_s:
            return
        sha, prev = record.get("sha"), record.get("prev_sha")
        key = cause_key(event.service_id, "crash-loop-after-sync", f"{sha} -> {prev}")
        if not self._deduper.observe(key, now).first:
            return
        self._queue(
            event.service_id,
            f"{event.service_id}: {ctx.consecutive_failures} consecutive failures within "
            f"{int(self.health_grace_s)}s of a sync (sha={sha}, prev_sha={prev}); "
            f"suggested action: rollback to prev_sha{_pool_suffix(record)}",
            Severity.ERROR,
            "crash loop after sync",
            now,
            ctx=ctx,
        )

    def _health_gate(self, now: float) -> None:
        """Escalate once per service that a sync left short of `healthy`.

        Measured from `stage_since`, not `updated_at`: `updated_at` is touched on
        every write (`docs/platform-sidecars.md`), so a service the sync loop
        retries every 60 s would have a fresh `updated_at` forever and never trip
        a gate keyed on it. `updated_at` is the fallback for a record written
        before `stage_since` existed.
        """
        services = self._state_document().get("services")
        if not isinstance(services, dict):
            return
        for service_id, record in services.items():
            if not isinstance(service_id, str) or not isinstance(record, dict):
                continue
            stage = record.get("stage")
            if not isinstance(stage, str):
                continue
            sha = record.get("sha")
            marker = (service_id, str(sha), stage)
            if stage == "healthy":
                # Reaching healthy re-arms the gate for the next sync of this sha.
                self._gated.discard(marker)
                continue
            if (
                stage == "declared"
                and self.state is not None
                and _is_static_mount(self.state, service_id)
            ):
                # A static mount's terminal stage is `declared`, not `healthy`
                # (see `_is_static_mount`) -- it is done, not stuck.
                self._gated.discard(marker)
                continue
            since = _parse_iso_z(record.get("stage_since")) or _parse_iso_z(
                record.get("updated_at")
            )
            if since is None or now - since <= self.health_grace_s:
                continue
            if marker in self._gated:
                continue
            self._gated.add(marker)
            prev = record.get("prev_sha")
            error = record.get("error")
            detail = f"; error: {error}" if isinstance(error, str) and error else ""
            self._queue(
                service_id,
                f"{service_id}: stuck at stage={stage} for {int(now - since)}s after a sync "
                f"(sha={sha}, prev_sha={prev}){detail}; "
                f"suggested action: rollback to prev_sha",
                Severity.ERROR,
                f"post-sync health gate: {stage} > {int(self.health_grace_s)}s",
                now,
            )

    def _service_record(self, service_id: str) -> dict[str, Any] | None:
        services = self._state_document().get("services")
        if not isinstance(services, dict):
            return None
        record = services.get(service_id)
        return record if isinstance(record, dict) else None

    def _state_document(self) -> dict[str, Any]:
        """`state.json`, cached on (mtime, size). Absent or unreadable = empty.

        Read on the supervisor's thread, so it must never raise and never block
        on anything but one `stat` in the common case. Absence is the normal
        state before the first sync (T3.1 writes this file), not an error.
        """
        if self.state is None:
            return {}
        path = platform_state_path(self.state)
        try:
            info = path.stat()
        except OSError:
            self._state_cache, self._state_cache_key = {}, None
            return {}
        key = (info.st_mtime_ns, info.st_size)
        if key == self._state_cache_key:
            return self._state_cache
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            self._warn_state_once("cannot read %s: %s", path, e)
            self._state_cache, self._state_cache_key = {}, key
            return {}
        if not isinstance(doc, dict):
            self._warn_state_once("%s is not a JSON object; ignoring", path)
            doc = {}
        elif doc.get("version") != 1:
            # The sidecar contract says a consumer that meets a version it does
            # not know must fail loudly rather than guess. Loudly, here, is a log
            # line and an empty document: raising out of a decision path would
            # take down supervision of every service to complain about a file
            # nothing had asked for yet.
            self._warn_state_once(
                "%s has unsupported version %r; ignoring", path, doc.get("version")
            )
            doc = {}
        else:
            self._state_warned = False
        self._state_cache, self._state_cache_key = doc, key
        return doc

    def _warn_state_once(self, fmt: str, *args: Any) -> None:
        if not self._state_warned:
            self._state_warned = True
            log.error(fmt, *args)

    # -------------------------------------------------------------------- queueing

    def _queue(
        self,
        service_id: str,
        text: str,
        severity: Severity,
        reason: str,
        now: float,
        ctx: ServiceContext | None = None,
    ) -> None:
        context = ctx or self._last_ctx.get(service_id) or _placeholder_ctx(service_id)
        self._pending.append(
            EscalationRecord(
                LogLine(service_id, "stderr", text, severity, ts=now),
                Decision(Action.ESCALATE, reason),
                context,
            )
        )

    # ------------------------------------------------------------------ inspection

    @property
    def registry_ready_at(self) -> float | None:
        return self._registry_ready_at

    @property
    def pending(self) -> Iterable[EscalationRecord]:
        return tuple(self._pending)


def make_policy(state: StateDir, **kwargs: Any) -> PlatformPolicy:
    """Build the platform policy for a state dir. What `ams run --policy platform` calls."""
    return PlatformPolicy(state=state, **kwargs)
