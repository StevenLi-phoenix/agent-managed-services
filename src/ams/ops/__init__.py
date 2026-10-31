"""``ams watch``: the host-level watcher that used to be mini-ops, minus the model.

Where the supervisor core watches the children it spawned, this layer watches what something else
owns: launchd jobs (any domain), health endpoints, unix sockets, log files, Tailscale Services. It
gates what it sees (new / back / flapping causes, job down, restart storm, failing probes), runs a
short closed list of guarded fixes chosen by rules, and escalates every incident -- with what was
done about it -- into the harness's escalation journal through ``ams intake``. Judging whether the
owner has to hear about it is the operator's job (DSH's ga-watch reads the journal).
"""
