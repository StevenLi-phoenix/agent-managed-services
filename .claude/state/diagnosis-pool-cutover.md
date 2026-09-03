# diagnosis-pool-cutover — `ams platform pool adopt core` fails before it moves anything

Written 2026-09-03 during T10's live cutover on racknerd. The live cutover is
**blocked**. The fleet is undamaged: nothing was stopped, moved, copied or
unlinked. Full session log in `.claude/state/pool-migration.md`.

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
