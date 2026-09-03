# pool-migration — T10 live cutover of `pool-core` on racknerd

Append-only running log. Every command's outcome is recorded as it happened,
including failures. ams HEAD at the start: `acd9dfe`. api `ams-platform` @
`c8b1fff3`. Operator: T10 agent, 2026-09-03.

---

## Step 1 — reconcile the roster (2026-09-03 07:54–07:56 UTC)

`ams platform status` and `ams ctl status` as `harness`, env
`AMS_STATE_DIR=/home/harness/store/state AMS_STORE_DIR=/home/harness/store`,
interpreter `/home/harness/venv/bin/python3`, cwd `/home/harness/ams`.

24 declarations in the harness, 20 running, 4 failed.

### The 15 intended `pool-core` members (from `grep 'pool = "core"' api/**/service.ams.toml`)

| member | platform stage | ctl status | port |
| --- | --- | --- | --- |
| commentservice | healthy | running | 20004 |
| emailservice | healthy | running | 20005 |
| kvservice | healthy | running | 20002 |
| llmpricing | healthy | running | 20020 |
| locationservice | healthy | running | 20016 |
| logservice | healthy | running | 20007 |
| mailbox | healthy | running | 20017 |
| messageservice | healthy | running | 20008 |
| notificationservice | healthy | running | 20009 |
| pages | healthy | running | 20018 |
| **resume** | **failed** [escalated] | failed | 20013 |
| **secretsservice** | **failed** [escalated] | failed | 20011 |
| timeservice | healthy | running | 20003 |
| turingtest | healthy | running | 20019 |
| wechatservice | healthy | running | 20012 |

13 of 15 healthy. `resume` and `secretsservice` were already failed before the
migration for known reasons (`resume`: health never 200 within 90 s;
`secretsservice`: no `SECRETS_MASTER_KEY` in any manifest or overlay, T4.1).
**They are EXPECTED to stay failed inside the pool.** That is not a regression
and the pool must not be judged on them.

Deviation from the runbook noted honestly: runbook step 1 says "confirm
`secretsservice` starts standalone" before pooling it. It does **not** start
standalone today, and no credential exists to make it. It is pooled anyway
because the overlay commit `c8b1fff3` already carries its `pool = "core"` line
and the pool is one process — dropping it would mean editing the api branch.
Consequence: one member is known-dead inside the pool from the first tick.

### Non-members, for contrast

| service | stage | note |
| --- | --- | --- |
| displayservice | failed [escalated] | not a member; stays standalone, stays failed |
| oss | failed [escalated] | not a member (R2 placeholder creds); stays standalone |
| files, llmgateway | healthy | not members by design (trust domain) |
| files-web, llm-web | declared | static mounts, terminal at `declared` |
| registry, auth, caddy, hello, pyhello | running | Layer 0 / smoke, never pool |

### `--only` list on the sync timer

`deploy/ams-platform-sync.service` names 9 services:
commentservice, emailservice, kvservice, llmgateway, logservice,
messageservice, notificationservice, wechatservice, timeservice. Eight of
those are pool members. `sync._select_pools` (`src/ams/platform/sync.py:1121`)
selects the **whole** pool when any one member is named — "a pool is one
process, so there is no declaration that deploys half of it" — and
`_phase_translate` parses every discovered manifest before selection, so the
seven members absent from the list (llmpricing, locationservice, mailbox,
pages, resume, secretsservice, turingtest) still flow through as
`_member_item`s. **No edit to the unit is required.** To be confirmed against
`sync --dry-run` in step 4 before relying on it.

## Step 1b — "before" measurement, re-taken with a reproducible script

The script that produced `.claude/state/evidence/pool-before-2026-09-03.txt`
was not archived, so its exact definitions of `python_processes` and the two
memory sums cannot be reproduced. Rather than compare an "after" number to a
method I cannot restate, a fresh **before-recheck** was taken with a script
that is archived: `/tmp/ams-measure.sh` on the box, copied to
`.claude/state/evidence/ams-measure.sh`. Definitions:

- `layer1_sum_MiB` — sum of `memory.current` over
  `/sys/fs/cgroup/system.slice/ams-harness.service/svc-*` **excluding**
  `svc-registry svc-auth svc-caddy svc-hello svc-pyhello`.
- `all_services_MiB` — the same sum over every `svc-*`.
- `python_processes` — interpreters running out of a service root
  (`store/state/services/*/bin/python`) plus the harness itself.
- `fleet_service_procs` — lines in every `svc-*/cgroup.procs` (top-level
  process per service).
- latency — `curl -w %{time_total}` on `/time/health` through Caddy at
  `127.0.0.1:20180` with `Host: api.lishuyu.app`, **seconds**.

Before-recheck, n=3, 60 s apart, fleet idle, 07:57–07:59 UTC:

| sample | python_processes | fleet_service_procs | layer1_sum MiB | all_services MiB | free avail MiB | load | /time/health s | http |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 18 | 20 | 712 | 866 | 653 | 0.69 | 0.003022 | 200 |
| 2 | 18 | 20 | 712 | 867 | 657 | 0.28 | 0.002161 | 200 |
| 3 | 18 | 20 | 712 | 867 | 656 | 0.21 | 0.002939 | 200 |

The archived file's numbers (layer1_sum 751–786, all_services 929–976,
python_processes 19, n=3) are **higher by ~40–100 MiB**. Both were taken on
the same fleet three hours apart with nothing restarted in between, so the gap
is method plus reclaim drift, not a real change. **The before/after delta in
this document is computed against the recheck row (712 / 866), not against the
archived row.** n=3 for both; single host, no repetition across reboots.

---

## Step 2 — deploy the new ams (2026-09-03 08:00 UTC)

`scripts/deploy-racknerd.sh` from the repo at `acd9dfe`. rsync + unit install +
`systemctl restart ams-harness.service`. Elapsed 8.0 s. `systemctl is-active`
→ `active`.

The restart brought the whole fleet down and back up. `depends_on` ordering is
visible in the journal: every Layer-1 service logged
`… waiting for registry` before starting.

Fleet recovery, polled every 30 s with `ams ctl status`:

| t after restart | running + healthy | not running |
| --- | --- | --- |
| 30 s | 1 | 23 |
| 60 s | 5 | 19 |
| 90 s | 9 | 15 |
| 120 s | 10 | 14 |
| 150 s | **20** | displayservice, oss, resume, secretsservice |
| 180–240 s | 20 | (same 4) |

**Cold start to fleet-steady: between 120 s and 150 s** (30 s polling
granularity, n=1). The post-deploy set is identical to the pre-deploy set —
same 20 running, same 4 failed. No regression from the deploy itself.

`ams platform status` after the restart is byte-identical to step 1's: no
record moved stage, `timeservice` still carries its `last healthy:
561722a7c496` note.

### Sync ticks are no-ops

`journalctl -u ams-platform-sync` at 08:04:28 UTC:

```
INFO ams.platform.sources: api: ams-platform -> 93709211bf377e4f56dc983e1c70108fe84fd42c
INFO ams.platform.sync: ams-platform -> 93709211bf37: 21 manifest(s)
INFO ams.platform.sync: sync done: sha=93709211bf37 services=9 unchanged=9 failed=0 reloaded=False gateway_changed=0
INFO ams.platform.cli: sha=93709211bf37 services=9 unchanged=9 failed=0 reloaded=False gateway_changed=0
```

`unchanged=9 failed=0 reloaded=False gateway_changed=0` on the tick after the
new code was live: the new ams is a no-op against the un-overlaid mirror,
which is exactly the ordering constraint being satisfied. The mirror head for
`ams-platform` is `93709211bf37` — the commit **before** `f88ebe60` (T11 SDK
fix) and `c8b1fff3` (the overlays), confirming nothing pool-related has
reached the box yet.

## Step 3 — backup (2026-09-03 08:05 UTC)

The R2 backup unit is not installed on this host and has no credentials, so
`systemctl start ams-platform-backup` (the runbook's step 2) is not available.
A local tarball was taken instead, as root:

```
cd /home/harness/store/state/services && ls -d */root/data > /tmp/databackuplist.txt
tar -C /home/harness/store/state/services --ignore-failed-read \
    -czf /var/lib/ams/pre-pool-data-20260903080517.tgz -T /tmp/databackuplist.txt
```

| field | value |
| --- | --- |
| path | `/var/lib/ams/pre-pool-data-20260903080517.tgz` |
| size | 431 644 bytes (422 KiB) |
| entries | 72 |
| `data/` dirs captured | 24 (every declared service) |
| SQLite files captured | 16 |

SQLite files in the snapshot: `auth`, `commentservice`, `emailservice`,
`files`, `kvservice`, `llmgateway`, `locationservice`, `logservice`,
`mailbox`, `messageservice`, `notificationservice`, `oss`, `pages`,
`registry` (+ `registry-runtime`), `turingtest`.

Pool members with **no** db of their own — `llmpricing`, `resume`,
`secretsservice`, `timeservice`, `wechatservice` — have their (empty or
non-SQLite) `data/` captured all the same.

**This tarball is the undo for the data move in step 5.** Restoring is
per-member: extract `<member>/root/data` back over
`/home/harness/store/state/services/<member>/root/data`.

---

## Step 4 — push the overlays, watch the guard trip (2026-09-03 08:06 UTC)

The api clone has no mirror remote configured (`git remote -v` shows only
`origin` → github.com, which the box cannot reach). The established transport
is `scripts/platform-bootstrap.sh`'s: `git clone --mirror` to a temp dir, then
`rsync -az --delete` into `<store>/upstream/api.git`. That was reused verbatim.

```
git clone --mirror /Users/lishuyu/Codes/AgentManangedServices/api $MIRROR/api.git
rsync -az --delete "$MIRROR/api.git/" racknerd:/home/harness/store/upstream/api.git/
ssh racknerd "chown -R harness:harness /home/harness/store/upstream/api.git"
```

Mirror head after the push, read back on the box as `harness`:
`refs/heads/ams-platform = c8b1fff30428170c1aca4bf6a3165cfbd66494c6`. Ordering
constraint satisfied — the new ams went out at 08:00, the overlays at 08:06.

### The 08:06:30 tick

`--only` question settled by the live log, no unit edit needed:

```
INFO ams.platform.sync: pool core: 8 of 15 members named; a pool is one process, so all of it is selected
```

The tick then staged the pool root, placed the JWT public key, created the
venv and ran one `uv pip install` for all 15 projects:

```
INFO ams.userns: created service root /home/harness/store/state/services/pool-core/root
INFO ams.platform.bootstrap: placed jwt-rs256.pub for pool-core (mode 444)
INFO ams.runtime: pool-core: uv venv: uv venv -q --python 3.12 .../pool-core/root/.venv -> rc=0 in 0.1s
INFO ams.runtime: pool-core: uv pip install: uv pip install -q --python .../pool-core/root/.venv/bin/python uvicorn[standard]==0.52.4 -e apps/llmpricing -e apps/locationservice -e apps/mailbox -e apps/pages -e apps/resume -e apps/timeservice -e apps/turingtest -e services/commentservice -e services/emailservice -e services/kvservice -e services/logservice -e services/messageservice -e services/notificationservice -e services/secretsservice -e services/wechatservice -> rc=0 in 15.0s
```

**One `uv pip install`, 15 editable projects, rc=0 in 15.0 s** with the warm
reflink cache — an order of magnitude under the "minutes" the runbook budgets.

### The adoption guard, escalated once, verbatim

```
{"kind": "PlatformSync", "service_id": "pool-core", "action": "escalate", "reason": "declare: member(s) ['commentservice', 'emailservice', 'kvservice', 'locationservice', 'logservice', 'mailbox', 'messageservice', 'notificationservice', 'pages', 'turingtest'] still have data in their pre-pool services/<id>/root/data; run `ams platform pool adopt core` to move it into the pool root -- sync never moves data", "event": {"service_id": "pool-core", "stage": "declare", "error": "declare: member(s) [...] still have data in their pre-pool services/<id>/root/data; run `ams platform pool adopt core` to move it into the pool root -- sync never moves data", "sha": "c8b1fff30428170c1aca4bf6a3165cfbd66494c6", "prev_sha": null}}
```

Exactly one `action: escalate` line for the whole tick. The other 14 members
logged `not declared -- its pool failed (…)` at INFO, not as escalations. The
guard names 10 members — the 10 whose `root/data` is non-empty; `llmpricing`,
`resume`, `secretsservice`, `timeservice`, `wechatservice` have nothing to
move, as the step-3 snapshot independently showed.

Tick outcome: `sha=c8b1fff30428 services=17 unchanged=0 failed=16
reloaded=True gateway_changed=0`, unit exit 1. **The 15 members kept running
throughout** — the guard stops the pool from being declared, it does not touch
the standing member declarations.

Collateral, and expected: `llmgateway` is not a pool member but its SHA moved
(the SDK changed in `f88ebe60`), so this tick restaged, reprovisioned,
restarted and re-registered it. `create identity 'llmgateway' -> status=409`
(= success, the row exists), ACL upserts 200/200, health 200 after 9 s of
`Connection refused`. No regression.

### The timer was stopped here

`systemctl stop ams-platform-sync.timer`. Reason: the 60 s timer would
re-enter a tick while `pool adopt` is stopping members through the control
socket and moving their data, and neither the adopt nor the sync is written to
expect the other. This is an operator action, not a code change, and the timer
is restarted in step 6.

### `ams platform sync --dry-run`, pool section

Run as `harness` with the unit's own arguments minus `--only` (so the plan
covers the whole fleet). Verbatim, one line per member, reformatted only by
dropping the duplicated `event` object:

```
pool-core   would run: translate, stage, provision, declare, reload, health
            (pool of 15: commentservice, emailservice, kvservice, llmpricing,
             locationservice, logservice, mailbox, messageservice,
             notificationservice, pages, resume, secretsservice, timeservice,
             turingtest, wechatservice)
llmpricing            in pool core: mount-gains-port_owner=pool-core, declaration-unlinked, registry-unchanged, register, health
locationservice       in pool core: mount-gains-port_owner=pool-core, declaration-unlinked, registry-unchanged, register, health
mailbox               in pool core: mount-gains-port_owner=pool-core, declaration-unlinked, registry-unchanged, register, health
pages                 in pool core: mount-gains-port_owner=pool-core, declaration-unlinked, registry-unchanged, register, health
resume                in pool core: mount-gains-port_owner=pool-core, declaration-unlinked, registry-unchanged, register, health
timeservice           in pool core: mount-gains-port_owner=pool-core, declaration-unlinked, registry-unchanged, register, health
turingtest            in pool core: mount-gains-port_owner=pool-core, declaration-unlinked, registry-unchanged, register, health
commentservice        in pool core: mount-gains-port_owner=pool-core, declaration-unlinked, registry-unchanged, register, health
emailservice          in pool core: mount-gains-port_owner=pool-core, declaration-unlinked, registry-unchanged, register, health
kvservice             in pool core: mount-gains-port_owner=pool-core, declaration-unlinked, registry-unchanged, register, health
logservice            in pool core: mount-gains-port_owner=pool-core, declaration-unlinked, registry-unchanged, register, health
messageservice        in pool core: mount-gains-port_owner=pool-core, declaration-unlinked, registry-unchanged, register, health
notificationservice   in pool core: mount-gains-port_owner=pool-core, declaration-unlinked, registry-unchanged, register, health
secretsservice        in pool core: mount-gains-port_owner=pool-core, declaration-unlinked, registry-unchanged, register, health
wechatservice         in pool core: mount-gains-port_owner=pool-core, declaration-unlinked, registry-unchanged, register, health
```

Summary line: `sha=c8b1fff30428 services=22 unchanged=22 failed=18
reloaded=False gateway_changed=0`.

This matches the runbook's step-4 expectation exactly: **one** new service
`pool-core`, **15** members losing their declarations, **15** mount sidecars
gaining `port_owner`, and **zero** registry sidecar changes
(`registry-unchanged` on every member).

---

## Step 5 — `pool plan` succeeded, `pool adopt` FAILED (2026-09-03 08:12 UTC)

### `ams platform pool plan core` — clean

Header line:

```
pool-core: 15 member(s), root /home/harness/store/state/services/pool-core/root (running: auth, caddy, commentservice, emailservice, files, hello, kvservice, llmgateway, llmpricing, locationservice, logservice, mailbox, messageservice, notificationservice, pages, pyhello, registry, timeservice, turingtest, wechatservice)
```

Then, per member: 30 database file moves (10 members × `.db`, `.db-shm`,
`.db-wal`), 20 secret copies under mangled names, 15 declaration removals.
Sample, verbatim:

```
  commentservice:
    move .../services/commentservice/root/data/commentservice.db -> .../services/pool-core/root/data/commentservice/commentservice.db
    move .../commentservice.db-shm -> .../pool-core/root/data/commentservice/commentservice.db-shm
    move .../commentservice.db-wal -> .../pool-core/root/data/commentservice/commentservice.db-wal
    copy secret DEEPSEEK_API_KEY -> DEEPSEEK_API_KEY__COMMENTSERVICE
    copy secret SVC_SECRET -> SVC_SECRET__COMMENTSERVICE
    remove declaration commentservice/service.toml
  ...
  wechatservice:
    copy secret SVC_SECRET -> SVC_SECRET__WECHATSERVICE
    copy secret WECHAT_MP_APPID -> WECHAT_MP_APPID__WECHATSERVICE
    copy secret WECHAT_MP_APPSECRET -> WECHAT_MP_APPSECRET__WECHATSERVICE
    remove declaration wechatservice/service.toml
```

The five members with no database (`llmpricing`, `resume`, `secretsservice`,
`timeservice`, `wechatservice`) show only secret copies and the declaration
removal, matching the guard's 10-member list and the step-3 snapshot.

### `ams platform pool adopt core` — blocked

```
$ ams platform pool adopt core
ERROR PermissionError: [Errno 13] Permission denied: '/home/harness/store/state/services/pool-core/root/data/commentservice'
```

An unhandled `PermissionError`, not a `PoolAdoptError`. **Root cause:
`src/ams/platform/pool.py:335`** — `_list_dir` guards its `iterdir()` against
`OSError` and falls back to the admin namespace, but the `directory.is_dir()`
probe one line above is unguarded, and `pathlib` re-raises `EACCES` from
`is_dir()` (it ignores only `ENOENT`, `ENOTDIR`, `EBADF`, `ELOOP`). Stat-ing
`<pool-root>/data/<member>` needs `x` on `<pool-root>/data`, which is
`drwxr-x--- 127648:127648` — the pool's uid, not `harness` (uid 1000).
Reproduced directly on the box with the deployed interpreter.

Full analysis, the reproduction, the hypothesised fix and the rejected
`chmod o+x` workaround are in **`.claude/state/diagnosis-pool-cutover.md`**.
Per T10's scope rules, nothing under `src/` was touched and nothing was patched
on the box.

### Blast radius: none

`_check_collisions` runs before the first move and refused there. Verified on
the box after the failure:

| check | result |
| --- | --- |
| 15 member `service.toml` declarations | all present |
| `commentservice` / `kvservice` legacy `root/data` | `.db`, `.db-shm`, `.db-wal` all in place |
| `<state>/secrets/pool-core` | does not exist |
| `.ams-pool-staging` anywhere | none |
| `ams ctl status` | 20 running + healthy; not healthy = displayservice, oss, resume, secretsservice |

That is the identical set to step 1. **No regression, no data loss, nothing to
restore from the step-3 tarball.**

## Steps 6–9 — not reached

The pool was never declared, so there is no pool process, no pool cgroup, no
`/_pool/health`, and no per-member routing through the pool's ports. Steps 6
(sync to healthy), 7 (external verification through Caddy), 8 (the "after"
measurement) and 9 (regression summary) are **not done** and are not reported
as partial results.

The "after" column of the measurement table in `docs/platform-pools.md` is
therefore still unmeasured. What T10 *did* measure — the before-recheck row and
the standalone fleet's cold start — has been written into the **before**
column; the "after" cells are left as `*(T10)*`.

## Host state left behind

| item | state |
| --- | --- |
| ams code on `/home/harness/ams` | `acd9dfe`, deployed, harness running |
| `<store>/upstream/api.git` `ams-platform` | `c8b1fff30428…` — **overlays are live in the mirror** |
| `ams-platform-sync.timer` | **STOPPED** — see below |
| `ams-harness.service` | active, 20 services running + healthy |
| `llmgateway` | moved to `c8b1fff3` (SDK change), healthy, not reverted |
| data snapshot | `/var/lib/ams/pre-pool-data-20260903080517.tgz`, unused |
| pool root `<state>/services/pool-core/root` | staged + provisioned venv, **no declaration**, never started |

**The timer is stopped on purpose.** With the overlays in the mirror and the
adoption blocked, every 60 s tick re-stages the pool root, re-runs the 15-project
`uv pip install` (~16 s CPU) and re-trips the guard. Leaving it running would
be a permanent failing-tick loop on a 1 vCPU box. Restart it with
`systemctl start ams-platform-sync.timer` once the `_list_dir` fix is deployed;
the fleet is fully supervised meanwhile, only deployment is frozen.

**Rollback was deliberately NOT performed.** The brief's rollback path (revert
the overlay commit in the mirror, restore data per member, reload) exists to
undo damage, and there is none: no data moved, no declaration removed, every
member still serving. Reverting the mirror would only churn `llmgateway` back
onto the older SDK. The overlays are left in place so the cutover can be
retried the moment the fix lands.
