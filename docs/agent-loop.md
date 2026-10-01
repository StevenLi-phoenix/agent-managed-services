# Where the agent is

ams is "agent managed": an agent declares services, reads what goes wrong and
fixes it. This page says exactly which part is code and which part is the
agent, and why the line is drawn there.

## Two layers

```
service stdout/stderr, exits, health
        │
        ▼
DecisionPolicy  (deterministic, in the harness loop)      ams.decision / ams.platform.policy
   ├─ SUPPRESS  noise: self-probe access lines, Node's --trace-warnings hint,
   │            known Caddy start-up warnings, duplicates inside a dedupe window
   ├─ LOG       INFO lines
   ├─ RESTART / STOP   exits, per the declaration's restart policy
   └─ ESCALATE  everything rules cannot settle: WARNING+ lines, retries exhausted,
                a crash loop after a sync, a failed release, a rejected plugin,
                a failed backup
        │
        ▼
escalation records  ── stdout (JSON lines; journald under systemd)
                    └─ <state>/logs/escalations.jsonl   ← `ams escalations`
        │
        ▼
the agent  (an operator session: Claude Code or a human, started by a person)
   reads records, `ams ctl status`, `ams platform core status`, service logs;
   fixes by editing a declaration / core.toml / the upstream repo, setting a
   secret, `ams ctl reload|restart`, `ams platform core release --rollback`
```

**The decision layer is code.** `DefaultPolicy` and `PlatformPolicy` are plain
rules with tests. They run inside the single-threaded supervisor loop, in
microseconds, with no network and no model. They decide *whether a human-grade
judgement is needed*, not what the fix is.

**The agent is not inside the harness process.** It is an operator session
that a person starts (for example `claude` in a terminal on the host, or over
ssh), which consumes escalations and acts through the same CLI a human uses.
Nothing in ams calls a language model.

## Why there is no LLM deciding suppress-or-fix

1. **Log text is attacker-controlled input.** Every line a service prints is
   written by that service's code and its dependencies. A model deciding on it
   in-loop is a prompt-injection channel from any compromised package to the
   process that holds the harness uid, the secrets store and the uid map. The
   trust boundary is documented in `ams.decision`: event text is data, never
   instructions.
2. **The loop must be fast and total.** The supervisor is one thread
   (`docs/event-loop.md`); a model call blocks for seconds and fails in ways a
   supervisor may not. A crash must be restarted in the same iteration whether
   or not an API is reachable.
3. **Suppression is the dangerous verdict.** The only thing an in-loop model
   could add over the rules is deciding to *drop* something. A wrongly
   suppressed error is invisible by construction; a wrongly escalated one costs
   a glance. The rules are conservative on purpose: they suppress only exact,
   anchored, tested patterns.
4. **Determinism is testable.** Every suppression rule has a test and a
   DECISIONS entry; "the model thought it was noise" has neither.

The `DecisionPolicy` protocol is still the extension point. A policy can only
return an `Action` (suppress, log, restart, stop, escalate); it cannot run
commands. If someone does plug a model in there, it should be allowed to
escalate more, never to suppress more than the rules do.

## The agent's loop

The records the agent reads:

```console
$ ams escalations -n 5
2026-10-01T09:12:03Z  harness    caddy         LogLine         warning line | {"level":"warn","msg":"..."}
2026-10-01T09:14:40Z  core-sync  core          CoreSync        core_plugin_rejected: artifact refused | ...
2026-10-01T09:20:11Z  harness    web           ServiceExited   restart.policy=on-failure gave up after 5 retries
$ ams escalations --service core --json     # raw records, one JSON object per line
$ ams escalations --since 2026-10-01T09:00:00
```

`--json` prints records as stored. Treat them as untrusted (they carry service
output verbatim); the default human format strips terminal control characters.

A typical session, in order:

1. `ams escalations -n 50` — what the rules could not settle, oldest first.
2. `ams ctl status` — what is running, healthy, waiting, failed.
   `ams platform core status` in core mode — release, plugins, drift.
3. Read the cause: the record's `event` (exit code, health detail, log text,
   core's refusal code), then the service's own logs.
4. Fix at the source, never in the harness's state:
   - a wrong declaration → edit `services/<id>/service.toml`, `ams ctl reload`;
   - a missing secret → `ams secret set <id> NAME` (value on stdin), `ams ctl restart <id>`;
   - bad code → fix upstream; the next core-sync tick ships it (a failed content
     key is never retried as is, so a fix *is* a new key);
   - a bad core release → `ams platform core release --rollback`.
5. Verify: `ams ctl status`, and that no new record for the same cause appears.

Each record names its `source` (`harness`, `core-sync`, `backup`), `kind`, the
`service_id`, the policy's `reason` and the full `event`. Duplicates of one
cause are collapsed before they are written (`ams.platform.policy` in the
harness; `core.json`'s `escalated` map across core-sync ticks), so the journal
is a list of distinct problems, not a log.

## Journal mechanics

- Path: `<state>/logs/escalations.jsonl`, mode 0600, harness-owned.
- Writers: `ams run` (`JsonLinesEscalation`), `ams platform core sync|ship|release`,
  the backup timer (failures only). One `write(2)` per record on an `O_APPEND`
  fd, so concurrent writers never interleave inside a line.
- Rotation: past 8 MiB the file becomes `escalations.jsonl.1` (one generation
  kept); `ams escalations` reads both.
- A journal that cannot be written (disk full, permissions) logs a warning and
  never blocks the escalation itself, which still reaches stdout.
