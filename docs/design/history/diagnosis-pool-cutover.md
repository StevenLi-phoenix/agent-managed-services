# diagnosis-pool-cutover — `ams platform pool adopt core` fails before it moves anything

Written 2026-09-03 during T10's live cutover on racknerd. The live cutover is
**blocked**. The fleet is undamaged: nothing was stopped, moved, copied or
unlinked. Full session log in `docs/design/history/pool-migration.md`.

## The failure signal

As `harness`, with the deployed ams at `acd9dfe`:

```
$ ams platform pool adopt core
ERROR PermissionError: [Errno 13] Permission denied: '/home/harness/store/state/services/pool-core/root/data/commentservice'
```

An unhandled `PermissionError`, not a `PoolAdoptError` — the command has no
diagnostic of its own for this, which is itself part of the finding.
`ams platform pool plan core` on the same host succeeds and prints the full
15-member plan.

## Root cause

`_list_dir` in `src/ams/platform/pool.py:321` guards `iterdir()` against
`OSError` (line 337–341) and falls back to the admin namespace, but its first
statement is an **unguarded** probe:

```python
335:    if not directory.is_dir():
336:        return (), True
```

`pathlib.Path.is_dir()` swallows only `ENOENT`, `ENOTDIR`, `EBADF` and `ELOOP`
(`pathlib._ignore_error`). **`EACCES` is re-raised.** Stat-ing
`<pool-root>/data/<member>` needs execute permission on `<pool-root>/data`,
and the harness does not have it:

```
$ ls -lan /home/harness/store/state/services/pool-core/root/
drwxr-x--- 2 127648 127648    6 Sep  3 08:06 data
$ id harness
uid=1000(harness) gid=1000(harness) groups=1000(harness)
```

`<root>/data` is created by `ensure_service_root` at mode 0750
(`src/ams/userns.py:436–437`) and then chowned to the service's uid inside the
admin namespace, so it lands as `127648:127648 0750` — the pool's uid block
base + `INNER_UID`. uid 1000 is neither owner nor group, so it has no `x`.

Reproduced directly on the box with the deployed interpreter (Python 3.12.3):

```
is_dir RAISED PermissionError [Errno 13] Permission denied: '.../pool-core/root/data/commentservice'
parent iterdir RAISED PermissionError [Errno 13] Permission denied: '.../pool-core/root/data'
```

n=1 host, but this is a deterministic permission fact, not a timing signal:
the same call raises on every invocation, and it will raise on **every** first
adoption of **every** pool, because a freshly staged pool root always has a
0750 `data/` owned by the pool's uid before `_ensure_pool_dirs` runs.

## Why the call site reaches it

`adopt()` (`src/ams/platform/pool.py:480`) runs `_check_collisions(run)`
(line 607) *before* `_ensure_pool_dirs(run)` (line 641) — correctly, since the
collision check is what makes the move safe. `_check_collisions` calls
`_list_dir(member.target_data, …)` at line 624–626 for the first member whose
`needs_adopt` is true (`commentservice`, alphabetically first). At that moment
`<pool-root>/data` exists (created by `ensure_service_root` during the sync
tick that staged the pool) but `<pool-root>/data/<member>` does not, and the
harness cannot stat inside it.

`plan()` never hits this because its two `_list_dir` calls (lines 411 and 414)
target the member's own legacy `data/` and its staging dir, both of which the
harness reaches through paths it can traverse.

## Hypothesised fix (NOT applied — `src/` is out of T10's scope)

Guard the probe the same way the `iterdir()` below it is guarded, so an
unreadable directory falls through to the admin-namespace `find` instead of
raising. Roughly, at `src/ams/platform/pool.py:335`:

```python
    try:
        if not directory.is_dir():
            return (), True
    except OSError as e:
        log.debug("%s: cannot stat %s from the harness (%s); using the admin ns",
                  service_id, directory, e)
    else:
        try:
            return tuple(sorted(p.name for p in directory.iterdir())), True
        except OSError as e:
            ...
```

The existing admin-ns fallback already returns the right answer for this path:
`find <dir> -mindepth 1 -maxdepth 1 -print0` on a directory that does not exist
exits non-zero, so `result.ok` is false and `_list_dir` returns
`((), False)` — which `_check_collisions` turns into the
`"cannot list … refusing to move data into a directory whose contents are
unknown"` `PoolAdoptError`. **That would be wrong too**: a not-yet-created
`data/<member>` is genuinely empty, not unknown. So the fix probably has to
distinguish "absent" from "unlistable" inside the admin ns as well — e.g. run
the `find` against `<pool-root>/data` and treat a missing child as empty, or
have `_check_collisions` call `_ensure_pool_dirs` first and then list. Deciding
between those is the owning task's call, not T10's.

Rejected workaround, deliberately not applied on the box: `chmod o+x`
on `<pool-root>/data`. It does unblock the adoption (only the traverse bit is
missing) and `StateDir.ensure` already treats 0750 as a floor so a deliberate
`o+x` survives. It was rejected because it hides a defect that every future
pool would hit, and because T10's brief reserves code-shaped fixes for the
owning task. If an operator needs the migration *today*, this is the one-line
unblock — but the code fix should land first.

## Verified blast radius: none

`_check_collisions` is documented to refuse "before the first move", and it
did. Checked on the box after the failure:

- all 15 member `service.toml` declarations still present;
- `commentservice` and `kvservice` legacy `root/data` still hold their
  `.db`, `.db-shm`, `.db-wal`;
- `<state>/secrets/pool-core` does not exist (no secret was copied);
- no `.ams-pool-staging` directory anywhere;
- `ams ctl status`: 20 running + healthy, the same 4 not healthy as before the
  migration (`displayservice`, `oss`, `resume`, `secretsservice`).

## Host state left behind

- The mirror `<store>/upstream/api.git` **carries the overlays**, head
  `ams-platform = c8b1fff30428…`. Deliberately left there: the ordering
  constraint is satisfied and re-pushing costs nothing.
- **`ams-platform-sync.timer` is STOPPED.** With the overlays live and the
  adoption blocked, every 60 s tick re-stages and re-provisions the pool
  (~16 s CPU) and re-trips the guard. Restarting it is safe but noisy;
  `systemctl start ams-platform-sync.timer` when the fix lands.
- `llmgateway` moved to `c8b1fff3` during the 08:06 tick (the SDK changed) and
  is healthy. Not reverted.
- Pre-migration data snapshot: `/var/lib/ams/pre-pool-data-20260903080517.tgz`
  (422 KiB, 24 `data/` dirs, 16 SQLite files). Unused — nothing moved.

---

# Second failure — the move list is captured before the member is stopped

2026-09-03 08:25 UTC, after `e873270` (the `_list_dir` fix) was deployed. The
first defect is **fixed and confirmed fixed**: adoption now walks past the
0750 pool `data/` and reports, for all 15 members,
`… /data/<member> does not exist (find rc=1); it holds nothing`. It then dies
one step later.

## The failure signal

```
$ ams platform pool adopt core
ERROR SpawnError: mv -n -t /home/harness/store/state/platform/adopt/commentservice -- /home/harness/store/state/services/commentservice/root/data/commentservice.db /home/harness/store/state/services/commentservice/root/data/commentservice.db-shm /home/harness/store/state/services/commentservice/root/data/commentservice.db-wal exited 1: mv: cannot stat '.../commentservice.db-shm': No such file or directory
mv: cannot stat '.../commentservice.db-wal': No such file or directory
```

## Root cause

`adopt()` computes the plan **once**, before the member loop, while every
member is still **running**. A running SQLite database in WAL mode has three
files on disk — `x.db`, `x.db-shm`, `x.db-wal` — so `MemberPlan.data_entries`
records three names. Then, inside the loop, `_stop_member` stops the member;
SQLite's clean shutdown checkpoints the WAL and **deletes `-shm` and `-wal`**.
`_move_member_data` then moves the list it captured before the stop:

```
src/ams/platform/pool.py:783   sources = [str(member.legacy_data / name) for name in member.data_entries]
src/ams/platform/pool.py:784   run.run_admin_fn(["mv", "-n", "-t", str(staging), "--", *sources], block).check()
```

Two of the three names no longer exist. GNU `mv` moves what it can, reports the
rest, and exits 1; `.check()` raises `SpawnError` and the adoption aborts.

Evidence that the deletion is the stop, not something else — taken on the box
at the moment of the failure:

| directory | contents |
| --- | --- |
| `kvservice/root/data` (running) | `kvservice.db`, `kvservice.db-shm`, `kvservice.db-wal` |
| `logservice/root/data` (running) | `logservice.db`, `logservice.db-shm`, `logservice.db-wal` |
| `commentservice/root/data` (just stopped) | *(empty — its `.db` had already been moved)* |

This is deterministic, not a race that sometimes loses: **every** WAL-mode
member with a live `-wal` at plan time will fail its own `mv`. 10 of the 15
`pool-core` members hold a database, so this blocks the adoption 10 times over,
one member per invocation.

## Partial state it leaves behind, and why a retry limps

`mv` is not atomic across its argument list. `commentservice.db` **was** moved
into `<state>/platform/adopt/commentservice/` before `mv` hit the missing
names, so the failure leaves: the member stopped with `desired=down`, its
`data/` empty, and its database parked in the staging directory. No data is
lost — the two-hop staging design is what saves it — but the service is down
and an operator who does not read the staging directory would think it was.

`_move_member_data` re-lists the staging directory afterwards
(`src/ams/platform/pool.py:796`) and completes the second hop on a later run,
so re-running `adopt` does make progress: the already-stopped member has an
empty `data/`, its `mv` is skipped, and its parked database lands in the pool
root. But the *next* member is still running, so the next run fails on *its*
`mv`. Ten runs, ten stopped services, is not a migration.

## Hypothesised fixes (NOT applied)

Three candidates, in the order I would rank them:

1. **Re-list after the stop.** `_move_member_data` recomputes the member's
   `data/` contents itself instead of trusting `member.data_entries` from
   plan time. This is the only one that fixes the underlying staleness: the
   plan is a *forecast* taken against a running fleet, and everything the
   loop does after `_stop_member` acts on a directory that has since changed.
   `_check_collisions` should keep using the plan (it must refuse *before*
   anything stops), but the move should not.
2. **Tolerate vanished sources.** Filter `sources` to paths that still exist
   immediately before the `mv`. Narrower, and it silently accepts any other
   disappearance too.
3. **Move the directory, not the files.** Sidesteps name-by-name staleness but
   changes the collision semantics `_check_collisions` is built on.

Do not "fix" this by dropping `-shm`/`-wal` from the plan. They are legitimately
present when the service is running, and a plan that hides them would be lying
about what adoption touches.

## Operator workaround, deliberately NOT taken

`ams ctl stop` on all 15 members first, *then* `pool plan` + `pool adopt`. With
every member already down, SQLite has checkpointed, the `-shm`/`-wal` files are
gone before the plan is computed, and the plan matches the disk. It uses only
the documented CLI and needs no code change.

It was not taken because it would produce a green cutover from a procedure the
shipped `pool adopt` does not itself perform — `adopt` stops the members on its
own, precisely so an operator does not have to, and a migration that only works
when the fleet is hand-stopped first should not be recorded as "adoption
works". T10's brief also reserves this call for the owning task. If the team
wants the "after" measurement before the fix lands, this is the one-command
path to it and I can run it on request.

## Recovery performed on the box

The abort left `commentservice` down with its database in the staging
directory. Restored as root, no code involved:

```
mv -n /home/harness/store/state/platform/adopt/commentservice/commentservice.db \
      /home/harness/store/state/services/commentservice/root/data/commentservice.db
rmdir /home/harness/store/state/platform/adopt/commentservice
ams ctl start commentservice
```

The file was already owned `108192:108192` (the member's uid), 57344 bytes,
mtime 02:06 — byte-size and mtime identical to the copy in
`/var/lib/ams/pre-pool-data-20260903080517.tgz`, so the snapshot was not
needed. `commentservice` is running and healthy again; `/health` on
`127.0.0.1:20004` returns 200. `ams ctl status`: **20 running + healthy**, not
healthy = `displayservice`, `oss`, `resume`, `secretsservice` — the same four
as before the migration began.

The 15 empty `<pool-root>/data/<member>` directories created by
`_ensure_pool_dirs` were left in place: correctly owned, empty, and idempotent
on the next run.
