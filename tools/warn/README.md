# warn

Hand one warning to the operator from any program on any host. `warn` delivers it to `ams intake`
(see `docs/watch.md`), which appends it to the AMS escalation journal; the operator that reads the
journal (DSH's ga-watch and its agent) checks it and tells the owner only if there is something to
do. One file, Python standard library only (3.8+), so a host needs nothing installed.

```sh
warn --source n8 --severity error "qwen38-llama.service failed"
journalctl -u foo -n 40 | warn --source phm.foo --severity warn "foo restarted 3 times" --text -
warn --curl        # print the curl one-liner for hosts without Python
```

| Option | Meaning |
|---|---|
| `title` | One line (300 characters). The operator signs `(severity, title, source)`: keep the cause stable here and put changing numbers in `--text` |
| `--source` | Who warns: a host or program, dotted lowercase words (`n8`, `phm.nginx`); kind becomes `warn.<source>.<severity>` |
| `--severity` | `info`, `warn` (default), `error`, `crit` |
| `--key` | Dedupe key; default `<source>-<sha1(title)[:10]>-<UTC yyyymmddhh>`, so one text dedupes within the hour |
| `--subject`, `--url`, `--text` | Optional; `--text -` reads stdin (8000 characters, untrusted data) |
| `--strict` | Exit 1 when not delivered, 2 without token or endpoint; the default exit code is 0 so a hook never breaks what it watches |
| `--print` | Show the event JSON without sending |

**Token**: `~/.config/warn/token`, mode 0600, the value of the intake's `INTAKE_TOKEN` secret.
Write it through a pipe, never into a chat or a command line.
**Endpoint**: `$WARN_URL`, else `~/.config/warn/url` (one line, `http://<intake host>:18090/escalate`).
There is no built-in default. Timeout 5 s (`$WARN_TIMEOUT`).
**Failure**: a timeout, 5xx, 429 or network error saves the event under
`~/.local/state/warn/spool/` and the next run replays it first (at most 20 per run, 100 kept,
dropped after 7 days); a 4xx (bad token, bad field) is not retried. Every step is logged to
`~/.local/state/warn/warn.log` (256 KB, one rotation), never the token.

```sh
curl -fsS -m 5 -H "Authorization: Bearer $(cat ~/.config/warn/token)" -H 'Content-Type: application/json' \
  -d '{"kind":"warn.n8.error","key":"n8-llama-20261031","subject":"n8","title":"llama failed"}' \
  "$(cat ~/.config/warn/url)"
```

Also here: `systemd/` (`warn@.service` + `warn-unit-failed`, for `OnFailure=warn@%n.service` on Linux
hosts; a drop-in `warn@.service.d/host.conf` with `Environment=WARN_HOST=<name>` names the host) and
`docker_health_watch.py` (follows `docker events`, warns when a container turns unhealthy; run it
as a scheduled task / service on the Docker host).

Tests: `python3 -m pytest tools/warn` (starts a fake endpoint on a local port).
