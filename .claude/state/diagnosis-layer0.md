# diagnosis-layer0 — the first live bring-up failed at `health-layer0`

2026-09-02, racknerd, T3.2. **Not** PLAN-allin risk 4 ("registry/auth may not
start rootless with data outside `/var/lib`"): neither process was ever
executed, so nothing about rootless operation was tested by this failure. The
harness refused to register them.

## Symptom

`python -m ams.platform.layer0` completed 13 of 17 stages and stopped:

```
"failed_stage": "health-layer0",
"health": {"auth": false, "caddy": true, "registry": false},
"reload": {"added": ["caddy"], "changed": ["kvservice", "timeservice"],
           "errors": {"auth":     "registration failed (ports, uid block or service root); see log",
                      "registry": "registration failed (ports, uid block or service root); see log"}}
```

From the journal (`ams.cli`, one per service):

```
ERROR ams.cli: skipping registry: could not prepare /home/harness/store/state/services/registry/root:
  chown -R 1000:1000 /home/harness/store/state/services/registry/root exited 1:
  chown: cannot read directory '.../registry/root/data': Permission denied
```

preceded by several thousand `chown: changing ownership of '.../repo/...': Operation
not permitted` lines. Caddy, whose root did not exist yet, was added fine.

## Root cause: the harness's uid allocator is a long-lived in-memory object

`ams.cli.build_supervisor` constructs one `UidAllocator` at harness start and
keeps it for the process lifetime. `UidAllocator.allocate` answers from
`self._blocks`, which was read from `<state>/state/uidmap.json` **once, at that
moment**, and `_save()` writes the whole in-memory map back.

The bring-up runs as a separate process. It allocated, from the same file:

| id | allocated by `layer0` (18:09:0x) | re-allocated by the harness on reload (18:09:15) |
| --- | --- | --- |
| registry | 105120 | **107168** |
| auth | 106144 | **105120** |
| caddy | 107168 | **106144** |

`layer0` then staged the repo, placed the JWT keys and provisioned the venv with
its numbers, so every file under `services/registry/root` is owned by 105120.
`ams ctl reload` reached `ams.cli._register` → `ensure_service_root(root, block)`
with the harness's *different* block (107168). The owner check
(`owner != block.uid_start`) therefore fired, and the recursive `chown` ran
inside an admin namespace that maps 107168…108191 — which has no authority over
files owned by 105120. Hence `EPERM` on every file and `EACCES` on the 0750
`data` directory. `_register` returned False and the service was skipped.

Two secondary facts confirm it: the harness's write also clobbered `layer0`'s
map on disk (the allocator persists its whole view), and `caddy` — the one id
whose root did not exist yet — registered without error because `created=True`
made the chown act on a fresh empty directory.

## What was wrong in the plan, not just in the code

The T3.2 brief says: *"use `UidAllocator.from_host(user, state.uidmap_state).allocate(id)`
(same allocator + state file the harness uses; allocation is idempotent and
persisted, so the harness will find the same block)"*. The second half is false
while the harness is running. Idempotence holds **per allocator instance**; it
does not hold across processes, because the running harness never re-reads the
file. Every design that pre-allocates from a second process hits this —
including T3.1's sync loop, which is specified as `ams provision` (a separate
process that allocates from disk) followed by `ams ctl reload` (the harness
allocating from a stale cache). This was going to fail there too.

## Hypotheses considered

1. **Registry/auth cannot run rootless with `data` outside `/var/lib`** (risk 4).
   Rejected: no process was started. The failure is entirely in registration.
2. **The 0750 `data` mode is too strict for the admin namespace.** Rejected: the
   same mode is created and chowned successfully by `bootstrap.ensure_service_dirs`
   and by `runtime.provision`, both from the bring-up process, with the matching
   block. The mode is not the variable; the block is.
3. **A race between two agents writing the live state dir.** Rejected: the two
   writers are `layer0` and the harness, one after the other, and the sequence is
   visible in the timestamps.
4. **Stale allocator cache.** Confirmed by the table above, read from
   `uidmap.json` before and after the reload.

## Fix applied

`UidAllocator.allocate` re-reads its persisted state before carving a block for
an id it does not know:

```python
existing = self._blocks.get(service_id)
if existing is not None:
    return existing
self._load()                      # another process may have carved it since
existing = self._blocks.get(service_id)
if existing is not None:
    return existing
```

Warm path is untouched (a known id still costs a dict lookup and no I/O); the
re-read happens only on the miss that is about to allocate anyway. It also feeds
`_lowest_free_index` the blocks other processes carved, so the harness can no
longer hand out a block that is already in use on disk.

This is in `src/ams/uidmap.py`, which T3.2's scope does not name. It is changed
anyway because the alternative fixes are all worse: restarting the unit on every
Layer-0 change defeats D17's whole point, and having `layer0` guess the harness's
numbers would only move the race. Recorded in DECISIONS D24.

It is a *narrowing*, not a general cure: two processes can still interleave
`allocate` → `_save`. The single-writer invariant (the harness owns its
allocators) still deserves a real answer — an allocation op on the control
socket — and that is named as Phase-B work in DECISIONS D24.

## Live state repaired

`uidmap.json` was left holding the harness's numbers while the trees on disk
hold `layer0`'s. Repair, with the unit stopped so the harness could not write
over it again:

1. `systemctl stop ams-harness`
2. rewrite `uidmap.json` with the mapping the on-disk trees actually have
   (registry 105120, auth 106144, caddy 107168);
3. `rm -rf services/caddy/root` — created empty under the wrong block, and Caddy
   keeps nothing there (its binary is in the store, its config in
   `<state>/gateway`);
4. `systemctl start ams-harness`, then re-run the bring-up.
