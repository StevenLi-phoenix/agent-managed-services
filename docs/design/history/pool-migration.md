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

The script that produced `docs/design/history/evidence/pool-before-2026-09-03.txt`
was not archived, so its exact definitions of `python_processes` and the two
memory sums cannot be reproduced. Rather than compare an "after" number to a
method I cannot restate, a fresh **before-recheck** was taken with a script
that is archived: `/tmp/ams-measure.sh` on the box, copied to
`docs/design/history/evidence/ams-measure.sh`. Definitions:

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
git clone --mirror ~/Codes/api $MIRROR/api.git
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
`chmod o+x` workaround are in **`docs/design/history/diagnosis-pool-cutover.md`**.
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

---

## Step 5, second attempt — `_list_dir` fixed, adopt fails one step later (08:22–08:27 UTC)

`e873270` deployed via `scripts/deploy-racknerd.sh` at ~08:22 by the owning
task. Verified on the box: `_list_dir` now returns a `Listing` and its
docstring names this cutover as the reason. Fleet recovery after the restart,
polled every 30 s: 5 → 5 → 7 → 14 → **20 healthy at 150 s**, not healthy =
`displayservice`, `oss`, `resume`, `secretsservice`. Same shape as the 08:00
restart.

`ams platform pool plan core` — unchanged, 15 members, 65 planned actions
(30 database moves, 20 secret copies, 15 declaration removals).

### The first defect is fixed

`ams platform pool adopt core` now walks past the pool root's 0750 `data/`
and reports, for all 15 members:

```
INFO ams.platform.pool: pool-core: /home/harness/store/state/services/pool-core/root/data/commentservice does not exist (find rc=1); it holds nothing
```

Absent is distinguished from unlistable, exactly as intended. `_ensure_pool_dirs`
then created all 15 `<pool-root>/data/<member>` directories.

### The second defect

```
ERROR SpawnError: mv -n -t /home/harness/store/state/platform/adopt/commentservice -- .../commentservice.db .../commentservice.db-shm .../commentservice.db-wal exited 1: mv: cannot stat '.../commentservice.db-shm': No such file or directory
mv: cannot stat '.../commentservice.db-wal': No such file or directory
```

`adopt()` computes the plan once, before the member loop, while every member is
**running** — so a WAL-mode SQLite database contributes three names
(`.db`, `.db-shm`, `.db-wal`). Inside the loop `_stop_member` stops the member,
SQLite checkpoints on clean shutdown and **deletes `-shm` and `-wal`**, and
`_move_member_data` then moves the pre-stop list:

```
src/ams/platform/pool.py:783   sources = [str(member.legacy_data / name) for name in member.data_entries]
src/ams/platform/pool.py:784   run.run_admin_fn(["mv", "-n", "-t", str(staging), "--", *sources], block).check()
```

Confirmed on the box at the moment of failure: `kvservice` and `logservice`
(running) each hold `.db` + `.db-shm` + `.db-wal`; `commentservice` (just
stopped) held none. Deterministic, not a race — all 10 members with a database
will fail their own `mv`, one per invocation.

Full analysis, three ranked fix candidates, and the rejected operator
workaround are in **`docs/design/history/diagnosis-pool-cutover.md`**. Nothing under
`src/` touched, nothing patched on the box.

### Blast radius: one service, recovered, no data lost

`mv` is not atomic across its arguments: `commentservice.db` was moved into the
staging directory before `mv` hit the missing names. The abort left
`commentservice` stopped with `desired=down`, an empty `data/`, and its
database parked in `<state>/platform/adopt/commentservice/`. The two-hop
staging design is what kept the data.

Recovered as root, no code involved:

```
mv -n /home/harness/store/state/platform/adopt/commentservice/commentservice.db \
      /home/harness/store/state/services/commentservice/root/data/commentservice.db
rmdir /home/harness/store/state/platform/adopt/commentservice
ams ctl start commentservice
```

The parked file was already `108192:108192`, 57344 bytes, mtime 02:06 —
byte-size and mtime identical to the copy inside
`/var/lib/ams/pre-pool-data-20260903080517.tgz`, so the snapshot was not
needed. `commentservice` `/health` on `127.0.0.1:20004` → 200.

`ams ctl status` after recovery: **20 running + healthy**, not healthy =
`displayservice`, `oss`, `resume`, `secretsservice` — identical to step 1.

| check | result |
| --- | --- |
| 15 member `service.toml` declarations | all present |
| `commentservice/root/data/commentservice.db` | restored, 57344 bytes |
| `<state>/platform/adopt/` | empty |
| `<state>/secrets/pool-core` | does not exist |
| `<pool-root>/data/<member>` × 15 | created, empty, correctly owned — left in place |

## Steps 6–9 — still not reached

The pool was never declared and never started. No pool process, no pool cgroup,
no `/_pool/health`, no member routed through a pool port. The "after" column of
the measurement table in `docs/platform-pools.md` remains unmeasured and is
marked blocked, not pending.

## Host state left behind (unchanged from the first attempt except where noted)

| item | state |
| --- | --- |
| ams code on `/home/harness/ams` | `e873270`, deployed, harness active |
| `<store>/upstream/api.git` `ams-platform` | `c8b1fff30428…` — overlays live |
| `ams-platform-sync.timer` | **STOPPED** (still) |
| `ams-harness.service` | active, 20 running + healthy |
| `commentservice` | stopped and restarted during the failed adopt; healthy |
| pool root | staged, provisioned venv, 15 empty `data/<member>` dirs, no declaration, never started |
| data snapshot | `/var/lib/ams/pre-pool-data-20260903080517.tgz`, still unused |

---

## Step 5, third attempt — ADOPTED (2026-09-03 08:41–08:43 UTC)

`436d86b` deployed at ~08:41. Fleet back to 20 running + healthy at **125 s**
after the restart (25 s polling), same 4 not healthy as always. `pool plan core`
unchanged: 15 members, 65 planned actions.

`ams platform pool adopt core` — **succeeded**:

```
pool-core: adopted 15 member(s); moved=10 secrets=20 declarations removed=15
```

The action list runs `pool-data`, then per member
`stop → data-out → data → secrets → undeclare` (the five members with no
database skip the two data steps). No error, no partial state.

### Verification of the move

`<pool-root>/data/<member>/`, listed as root:

| member | contents |
| --- | --- |
| commentservice, emailservice, kvservice, locationservice, logservice, mailbox, messageservice, notificationservice, pages, turingtest | `<member>.db` |
| llmpricing, resume, secretsservice, timeservice, wechatservice | *(empty — they never had one)* |

The `-shm`/`-wal` sidecars are correctly **absent**: SQLite checkpointed them
away on the clean stop, which is exactly the fact the previous attempt tripped
over.

`<state>/secrets/pool-core/` — 20 files, **names only**, no value read or
printed:

```
BARK_DEVICE_KEY__NOTIFICATIONSERVICE   RESEND_API_KEY__EMAILSERVICE
DEEPSEEK_API_KEY__COMMENTSERVICE       WECHAT_MP_APPID__WECHATSERVICE
WECHAT_MP_APPSECRET__WECHATSERVICE
SVC_SECRET__{COMMENTSERVICE, EMAILSERVICE, KVSERVICE, LLMPRICING,
             LOCATIONSERVICE, LOGSERVICE, MAILBOX, MESSAGESERVICE,
             NOTIFICATIONSERVICE, PAGES, RESUME, SECRETSSERVICE,
             TIMESERVICE, TURINGTEST, WECHATSERVICE}
```

All 15 member `service.toml` declarations removed; the legacy roots are left in
place, as the runbook requires.

## Step 6 — sync, declare, start (08:42:48 → 08:50:01, 7 min 13 s)

One `ams platform sync` by hand with the timer's own arguments minus `--only`.

```
INFO ams.platform.sync: sync done: sha=c8b1fff30428 services=22 unchanged=1 failed=4 reloaded=True gateway_changed=16
```

16 gateway sites re-rendered onto the pool's ports. Registry identities all
returned **409 = success**; ACL upserts 200.

### The pool process

```
08:43:37 INFO ams.supervisor: registered service pool-core (ports={commentservice:20021 … wechatservice:20036, pool:20031})
08:43:37 INFO ams.cgroup: limits for pool-core: {'memory.max': '629145600', 'pids.max': '288', 'cpu.max': '100000 100000'}
08:43:37 INFO ams.isolated: spawned pool-core pid=2129805 cgroup=…/svc-pool-core block=127648+1024
```

**Cold start: 21 s.** Spawn at 08:43:37, the last member (`wechatservice`,
port 20036) and the admin server (20031) both serving at 08:43:58. Well under
`start_period_s = 240`. Members came up sequentially at roughly one per
0.5–1 s, in the order the runner swaps identity env.

`ams ctl status` now lists **10 declarations** where there were 24:
`auth, caddy, displayservice, files, hello, llmgateway, oss, pool-core,
pyhello, registry`.

### Members

`ams platform status` (nested under `pool-core healthy c8b1fff30428 (15)`):
13 healthy, 2 failed.

| healthy (13) | failed (2) |
| --- | --- |
| commentservice, emailservice, kvservice, llmpricing, locationservice, logservice, mailbox, messageservice, notificationservice, pages, timeservice, turingtest, wechatservice | resume, secretsservice |

**Identical to the pre-migration set.** No member that worked standalone
stopped working in the pool.

`/_pool/health` on 127.0.0.1:20031, verbatim:

```json
{
  "pool": "core",
  "ok": ["commentservice","emailservice","kvservice","llmpricing","locationservice",
         "logservice","mailbox","messageservice","notificationservice","pages",
         "timeservice","turingtest","wechatservice"],
  "failed": {"secretsservice": "build failed", "resume": "startup failed"}
}
```

The two failures name their real causes in the runner log, and both are the
pre-existing ones:

```
ERROR [secretsservice] pool: build failed: KeyError: 'SECRETS_MASTER_KEY'
ERROR [resume] pool: startup failed: SystemExit: 3
  PermissionError: [Errno 13] Permission denied: '/var/lib/resume'
```

**This is the live confirmation of spike finding #3.** `resume`'s uvicorn
startup called `sys.exit(3)` inside the shared event loop; the runner caught
the `SystemExit` per member and the other 14 members were unaffected. Before
the runner had that guard, one member's `SystemExit` would have taken the whole
process down.

### Pool resource use

| metric | measured | declared limit | source of the limit |
| --- | --- | --- | --- |
| `memory.current` | 130 MiB (133–134 at rest, n=3) | 600 MiB | `150M + 30M × 15` |
| `memory.peak` | 130 MiB | — | |
| `pids.current` | 27 | 288 | `48 + 16 × 15` |
| thread count of pid 2129805 | 27 | — | |
| RSS | 127 MiB | — | |

Both formulas are **far** more generous than the measurement needs: memory
landed at 22 % of the limit and pids at 9 %. T0's 66 MiB at N=6 without
`SVC_DEV` extrapolated to 85–100 MiB at N=15; the real figure with heartbeat,
M2M-refresh and CLS-forwarder threads running is 130 MiB, so T0 was an
under-estimate by ~30 %, in the direction the runbook predicted. 27 threads for
15 members is roughly 1.8 per member, not the 3 the `pids_per_member = 16`
constant budgets for. Recorded here rather than acted on: the doc's instruction
is to re-derive only when the measurement lands *above* the formula.

### Escalations since the reload — 14 from `pool-core`, classified

| # | text | verdict |
| --- | --- | --- |
| 1 | `ERROR [secretsservice] pool: build failed: KeyError: 'SECRETS_MASTER_KEY'` | **genuine**, pre-existing |
| 4 | `ERROR [resume] pool: startup failed: SystemExit: 3` + `PermissionError: /var/lib/resume` + 2 uvicorn traceback lines | **genuine**, pre-existing |
| 1 | `WARNING [pool] sdk.fastapi: route '/comments/{comment_id}' starts with mount prefix '/comments'` | **genuine**, pre-existing api warning |
| 4 | `WARNING [mailbox]` / `WARNING [pages] … failed to ensure oss bucket at startup` + 2 `httpx 401 … /oss/…` | **genuine**, caused by `oss` being down (not a pool member) |
| 3 | `INFO [pool] uvicorn.error: {Waiting for application startup, Application startup complete, Uvicorn running on …}` | **MISCLASSIFIED — new** |
| 1 | `pool-core: stuck at stage=declared for 300s after a sync` | **false alarm — new** |

Nothing new and genuine. Two new *noise* sources, both worth fixing:

**(a) `[logging] format = "level-prefix"` does not match the runner's own
format.** The declaration sets it, but `_LEVEL_PREFIX_RE` in
`src/ams/events.py:64` is `^(?P<level>[A-Za-z]+)[ \t]+(?P<name>[^\s:]+):` —
`LEVEL name:`. The runner emits `LEVEL [tag] name:`, e.g.
`INFO [pool] uvicorn.error: …`. The bracketed tag breaks the match, so
`_from_level_prefix` returns `None` for **every** line the pool prints and
everything falls through to the text heuristic — which then matches `\bERROR\b`
inside the *logger name* `uvicorn.error` and escalates an INFO banner as ERROR.
Verified locally: `_from_level_prefix` returns `None` for all four sample runner
lines. Consequence today is three spurious escalations per pool start; the
deeper cost is that the level-prefix contract the pool was given is inert.

**(b) the post-sync health gate fires while the gate is still running.** The
policy escalated `pool-core: stuck at stage=declared for 300s` at ~08:48 even
though the pool had been healthy since 08:43:58. The sync walks its members'
health gates sequentially and two of them burn 90 s each, so the run outlives
the policy's 300 s window. Same line fired for 15 other services in the same
tick.

Neither was patched. `src/` is out of T10's scope.

## Step 7 — external verification through Caddy

`caddy fmt` round-trip on the rendered config: **identical**, byte for byte
(`caddy fmt <state>/gateway/Caddyfile | cmp -s` against the file). 21 site files
rendered; the pool members' `reverse_proxy` lines point at the pool's ports
(kvservice → 20023, timeservice → 20034, pages → 20030, mailbox → 20027).

### A correction to method, recorded because it changes the numbers

The first pass sent `Host: api.lishuyu.app`. The entry site block is literally
`http://127.0.0.1:20180 { … }`, so that Host matches **no** site and Caddy
answers an empty 200. Every path returned "200" including `oss`, which is down.
That result was worthless and is discarded. The correct probe sends no Host
override (curl then sends `Host: 127.0.0.1:20180`, which matches the entry
site). Subdomain members do need their own Host.

**The same flaw is in the latency row of `ams-measure.sh`, so the "before" and
"after" `time_health_via_caddy_s` numbers measure Caddy's empty-200 handler,
not timeservice.** They are internally comparable and externally meaningless;
a corrected latency measurement is below.

### Path-mounted members, `http://127.0.0.1:20180<path>/health`

| path | code | body |
| --- | --- | --- |
| /comments /email /kv /logs /message /notify | 200 | `{"status":"ok"}` |
| /llm-live-pricing /time /wechat | 200 | `{"ok":true}` |
| /resume | **502** | *(expected — member failed)* |
| /secrets | **502** | *(expected — member failed)* |

### Subdomain members

| Host | code | body |
| --- | --- | --- |
| location.lishuyu.app | 200 | `{"ok":true}` |
| mail.lishuyu.app | 200 | `{"ok":true}` |
| pages.shuyuli.com | 200 | `{"ok":true}` |
| turning-test.lishuyu.app | 200 | `{"ok":true}` |

### Non-pool controls, and the harness probe

`/files/health` 200 `{"status":"ok"}`, `/llm/health` 200 `{"ok":true}`,
`/oss/health` 502 (down, not a member), `/ams-health` 200 `ok`.

**13 of 15 members serve 200 through the gateway with a real body. The two
502s are the two members that were already failed before the migration.**

### M2M across the pool boundary — the shared key path

`timeservice` (pool member) mints a token and calls `kvservice` (pool member)
through Caddy. Both read the **one** `<pool-root>/etc/jwt-rs256.pub`. The secret
was read inside the script on the box and never printed; the token is not
reproduced here.

```
POST /api/m2m/token -> 200
  header : {'alg': 'RS256', 'typ': 'JWT'}
  claims : {'sub': 'service:timeservice', 'aud': 'kvservice',
            'iss': 'http://127.0.0.1:20101', 'iat': 1788425587,
            'exp': 1788511987, 'typ': 'm2m'}
  sig len: 256 bytes
PUT  /kv/pool-t10  no token  -> 401
PUT  /kv/pool-t10  M2M token -> 204
GET  /kv/pool-t10  no token  -> 401
GET  /kv/pool-t10  M2M token -> 200 {"key":"pool-t10","value":"from-timeservice-in-pool",…}
GET  /kv/pool-t10  tampered  -> 401
```

Signer and verifier agree inside one process on a key neither reads from the
other's declaration, and a six-character change to the signature is rejected —
so "any bearer token works" is ruled out. n=1, the same shape as
`platform-layer0.md` §4.

## Step 8 — the "after" measurement

Same script, same definitions, n=3, 60 s apart, fleet idle, 08:53–08:55 UTC.

| sample | python_procs | fleet_procs | layer1_sum MiB | all_services MiB | free avail MiB | load | pool mem MiB | pool pids | pool threads |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 5 | 8 | 275 | 464 | 1146 | 0.27 | 133 | 27 | 27 |
| 2 | 5 | 8 | 275 | 465 | 1146 | 0.21 | 134 | 27 | 27 |
| 3 | 5 | 8 | 275 | 465 | 1147 | 0.12 | 134 | 27 | 27 |

Per-cgroup at rest: `pool-core` 130, `files` 80, `registry` 76, `llmgateway` 60,
`auth` 49, `caddy` 34, `hello` 9, `pyhello` 9.

### Before → after, n=3 each

| metric | before | after | delta | target | met |
| --- | --- | --- | --- | --- | --- |
| Layer-1 cgroup sum | 712 MiB | 275 MiB | **−437 (−61 %)** | ≤ 300 | yes |
| all-services cgroup total | 866–867 MiB | 464–465 MiB | **−402 (−46 %)** | ≤ 600 | yes |
| `free -m` available | 653–657 MiB | 1146–1147 MiB | **+491** | ≥ 1200 | **no**, 1146 |
| python processes | 18 | 5 | −13 | — | — |
| pool `memory.current` | n/a | 133–134 MiB | — | ≤ 150 | yes |
| pool thread count | n/a | 27 | — | < 288 | yes |
| cold start to all members serving | 120–150 s (n=1) | **21 s** (n=1) | −100 s | < 240 | yes |

`free -m available` misses its 1200 MiB target by 54 MiB. Every other target is
met, most with wide margin.

### Corrected latency, taken at the same moment

`curl -w %{time_total}`, entry site, no Host override, n=20 each, sorted, p50:

| endpoint | p50 | min | max |
| --- | --- | --- | --- |
| `/time/health` (pool member) | 5.39 ms | 4.20 | 35.15 |
| `/kv/health` (pool member) | 5.46 ms | 4.08 | 12.43 |
| `/llm/health` (**standalone control**, not pooled) | 5.82 ms | 4.80 | 40.05 |

No valid "before" number exists for this row (see the method correction above),
so `llmgateway` — a non-pooled service measured in the same minute on the same
host — is used as the control instead. Pool members are **not slower** than the
standalone control; the 0.4 ms gap is inside the noise of a 20-sample run.
Weak evidence against the shared-event-loop coupling risk in spike §addendum:
one idle-fleet run, n=20, no concurrent load. **Do not read this as "no
coupling under load" — that was not tested.**

## Step 6b — the sync timer, restarted

`systemctl start ams-platform-sync.timer` at 08:56. First tick:

```
INFO ams.platform.sync: pool core: 8 of 15 members named; a pool is one process, so all of it is selected
INFO ams.platform.sync: resume still failing (…); already escalated
INFO ams.platform.sync: secretsservice still failing (…); already escalated
INFO ams.platform.sync: sync done: sha=c8b1fff30428 services=17 unchanged=15 failed=2 reloaded=True gateway_changed=0
```

15 unchanged, 2 failed and **deduped** (`already escalated`, no new escalation),
gateway unchanged. That is the steady state.

**New operational cost, worth recording.** A tick now takes **3 min 2 s**
(08:55:56 → 08:58:58) because `resume` and `secretsservice` each burn a 90 s
health gate, and the 60 s timer therefore runs ticks back to back. The unit's
own comment explains that `oss` and `secretsservice` were left out of `--only`
for exactly this reason — but a pooled member **cannot** be excluded that way:
naming any member selects the whole pool. The escape hatch the comment relies
on no longer exists for these two. Options for the owning task: give a known-
dead member a shorter health deadline, let a pool declare a member as expected-
failed, or fix the two services. Not acted on here.

## Step 9 — regressions

**None.** Nothing that returned 200 before the migration returns anything else
now.

| before | after |
| --- | --- |
| 13 members healthy | the same 13 members healthy |
| resume, secretsservice failed | the same two failed, same causes |
| displayservice, oss failed (non-members) | unchanged |
| files, llmgateway healthy (non-members) | unchanged |

New escalations are the three misclassified `INFO [pool] uvicorn.error` lines
and one premature post-sync health-gate warning — noise, not failures, both
analysed above.

Legacy member roots are **left in place**, as the runbook requires. The
pre-migration snapshot `/var/lib/ams/pre-pool-data-20260903080517.tgz` is
retained and was never needed.

## Final host state

| item | state |
| --- | --- |
| ams on `/home/harness/ams` | `436d86b` |
| `<store>/upstream/api.git` `ams-platform` | `c8b1fff30428…` |
| `ams-harness.service` | active; 10 declarations, `pool-core` healthy |
| `ams-platform-sync.timer` | **active** (restarted); ticks ~3 min, no-op |
| `pool-core` | pid 2129805, 16 ports 20021–20036, 130 MiB, 27 threads |
| legacy member roots | present, untouched |
| snapshot | `/var/lib/ams/pre-pool-data-20260903080517.tgz`, unused |
