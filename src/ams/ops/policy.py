"""Which fix, if any, an incident earns -- deterministic rules, no model.

The decision layer of AMS is rules (``ams.decision``); the judgement of whether the owner has to
hear about something lives with the operator that reads the escalation journal. This module only
answers "is there a safe, closed-list action that is the usual cure for this kind of incident?".
Whether it may run is :class:`ams.ops.actions.Executor`'s business: allow-list, freeze, cooldown,
daily and hourly caps, and the Kaorou guards all apply to what is chosen here.
"""

from __future__ import annotations

from ams.ops.config import ServiceConfig
from ams.ops.gate import Incident

#: Actions tried, in order, for an incident of a kind. A kind not listed has no cure: a log line,
#: a restart storm (another restart is not the fix) and a failed interval job are escalated as
#: they are.
CURES: dict[str, tuple[str, ...]] = {
    # soft_restart_kaorou first: it drains the daemon and refuses while a burn runs, which is the
    # only restart the Kaorou daemon is allowed (kickstart on it is refused by the Executor).
    "job_down": ("soft_restart_kaorou", "kickstart"),
    "health": ("soft_restart_kaorou", "kickstart"),
    "tailscale": ("advertise_tailscale",),
}


def choose(inc: Incident, svc: ServiceConfig | None) -> str | None:
    """The first cure for ``inc`` that ``svc`` lists among its actions, else ``None``."""
    if svc is None:
        return None
    for name in CURES.get(inc.kind, ()):
        if name in svc.actions:
            return name
    return None
