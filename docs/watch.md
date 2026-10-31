# External escalations and the host watcher

The supervisor core watches the children it spawned. Two additions cover everything else on a
host and on other hosts, and both end in the same place -- the escalation journal -- so the one
operator that reads it (`ams escalations`, DSH's ga-watch) sees every source and applies one gate.

```
other hosts, other users, any program ─ warn / curl ─►  ams intake  (HTTP, bearer token)  ┐
launchd jobs, probes, logs, Tailscale ── ams watch ──►  ams intake                       ├─► <state>/logs/escalations.jsonl ─► operator
a program on the harness host ───────── ams escalate ───────────────────────────────────┘
```

## `ams escalate` -- the local way in

```sh
ams escalate --source n8 --severity error "disk is full" --text - < details.txt
```

Appends one record (source `escalate`) to the journal of `--state-dir` / `$AMS_STATE_DIR`. Exit 0
written, 1 journal unwritable, 2 bad arguments. `--severity` is `info|warn|error|crit` (default
`warn`); `--source` is dotted lowercase words (`[a-z0-9_-]`).

## `ams intake` -- the way in for everything else

`ams intake serve --state-dir … --bind 0.0.0.0 --port 18090` is an HTTP receiver, run as an ordinary
declared service of the harness (same user as the journal, supervised, health-probed). Token:
`$INTAKE_TOKEN`, set with `ams secret set intake INTAKE_TOKEN`, or `--token-file`.

| Request | Answer |
|---|---|
| `GET /healthz` | 200, no auth (the supervisor's probe) |
| `POST /escalate` + `Authorization: Bearer <token>` + JSON | 202 `{"outcome":"accepted"\|"duplicate"}`; 400 bad field; 401; 413 above 64 KiB; 503 no token or journal unwritable |

The JSON is the `warn` CLI's payload: `kind` = `warn.<origin>.<severity>` (or the fields `origin`
and `severity`), `title` (required, 300 chars), `key`, `subject`, `text` (8000), `url`,
`occurredAt` (epoch ms). A `key` seen in the last hour is a `duplicate` and is not written twice
(the sender's retry or spool replay). The token is compared in constant time and never appears in a
response or a log.

### The record

The journal record has the supervisor's own escalation shape, with `kind` `External`,
`service_id` = origin, `reason` = `external <severity>`, and `event.text` = one JSON log line
`{"level": <severity>, "msg": <title>, "src": <origin>, "error": <text>}`. That is the shape a JSON
log line of a supervised service has, so a watcher that signs `(level, msg, src)` (ga-watch)
treats a repeat of the same title from the same origin as one cause: reported when new, again
after a long silence, or when it flaps. Put the stable cause in `title` and the changing details in
`text`. Every field is untrusted: it is clipped, never interpreted, and sits inside one JSON string.

## `tools/warn` -- the sender for other hosts

`tools/warn/warn.py` is the client: a single-file, stdlib-only (Python 3.8+) CLI with a local spool
for failed deliveries, a curl one-liner, a `warn@.service` template (`OnFailure=warn@%n.service`)
and a Docker health watcher. Token and endpoint live in `~/.config/warn/` on each host. See
`tools/warn/README.md`.

## `ams watch` -- the host watcher

What mini-ops did, minus the model. It runs as the owner of the things it watches (a user's
launchd domain and logs are not reachable from the harness user), so it hands incidents to
`ams intake` over loopback, through a durable outbox in its state file.

Per tick, for each `[[service]]` of `watch.toml` (`examples/watch/watch.toml`):

1. **Observe**: `launchctl print` of the declared job (any domain), an HTTP GET or unix-socket
   connect, new lines of the declared logs (classified by AMS's `LogLine`, a stated level beats the
   words in the message), Tailscale Services the node should advertise.
2. **Gate**: log causes are signed (ids, numbers, timestamps collapsed) and reported when new, back
   after 12 h, or flapping (5 in 1 h, at most once per 6 h); a job is *down* after two ticks, a
   *storm* after 3 restarts in 10 minutes; a probe fails after 3 in a row. The first tick only
   records a baseline.
3. **Fix**, by rule (`ams.ops.policy`): a job that is down or unhealthy gets the first of
   `soft_restart_kaorou`, `kickstart` the service lists; a missing Tailscale Service gets
   `advertise_tailscale`; nothing else is fixed automatically (log lines, restart storms, failed
   interval jobs are escalated as they are). `Executor` decides whether it runs: allow-list per
   service, freeze, a 30-minute cooldown per action and service, 3 a day per service, 4 an hour
   overall, `dry-run`; **the Kaorou daemon is never kickstarted** (`soft_restart_kaorou` drains it
   and refuses while any ffmpeg process or running job exists or the API is unreachable).
4. **Escalate** every incident -- with the action taken and whether it worked -- to the intake:
   origin `<origin>.<service>`, title = the stable cause, text = headline, up to three evidence
   lines (redacted), the action line, and "已自动处理，无需操作" or "需要人看一眼".

`ams watch run|status|check|freeze|test`; `freeze add <service|*> 30m "reason"` stops actions and
mutes escalation for planned maintenance (`--keep-alerts` only blocks actions).

No model is called anywhere. Whether the owner has to hear about an escalation is the operator's
call; the rules above only decide what is safe to do first.
