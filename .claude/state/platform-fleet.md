# Layer-1 fleet bring-up + resource report — racknerd, 2026-09-02/03 (T4.1)

Twenty ams services and two static sites run under the live
`ams-harness.service`, brought up through the real sync loop on a 60 s systemd
timer, from the local bare mirror `/home/harness/store/upstream/api.git` at ref
`ams-platform`. Everything below is copied from a real run. Nothing is
reconstructed and nothing is extrapolated.

Confidence marks: **[verified]** = observed directly, with n. **[weak]** = one
observation, plausible alternatives remain. Every number here is **n=1** unless
it says otherwise — one box, one bring-up, no long soak.

Host: racknerd, 1 vCPU, 1967 MiB RAM, 1023 MiB swap, Ubuntu 24.04.
Gateway `http://127.0.0.1:20180`, loopback only, plain HTTP (D21).
Artefacts: `deploy/ams-harness.service` (now `--policy platform`),
`deploy/ams-platform-sync.{service,timer}`, `src/ams/platform/{policy,bootstrap}.py`,
DECISIONS **D28**.

---

## 1. What runs

`memory.current` and `memory.peak` are the service cgroup's, in bytes, read from
`/sys/fs/cgroup/system.slice/ams-harness.service/svc-<id>/` at one instant after
the fleet had settled. `peak` is cumulative since the cgroup was created, so it
includes each service's start-up burst. "gw" is the HTTP status of `GET /health`
through Caddy (`Host:` header for a subdomain mount) [all verified].

### Layer 0 + demos

| id | port | uid | memory.current | memory.peak | memory.max | healthy |
| --- | --- | --- | --- | --- | --- | --- |
| registry | 20100 | 105120 | 74 584 064 (71.1 M) | 397 852 672 (379.4 M) | 734 003 200 | yes |
| auth | 20101 | 106144 | 54 112 256 (51.6 M) | 126 631 936 (120.8 M) | 209 715 200 | yes |
| caddy | 20180 | 107168 | 12 640 256 (12.1 M) | 14 278 656 | 125 829 120 | yes |
| hello | 20000 | 100000 | 9 850 880 | 10 145 792 | 67 108 864 | yes |
| pyhello | 20001 | 101024 | 11 284 480 | 14 082 048 | 67 108 864 | yes |

### Tier 1 — the 10 `services/*` + timeservice (9 of 11 up)

| id | port | uid | memory.current | memory.peak | memory.max | healthy | registered | gw |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| commentservice | 20004 | 108192 | 49 385 472 (47.1 M) | 49 930 240 | 157 286 400 | yes | healthy 14 s | 200 |
| emailservice | 20005 | 109216 | 47 833 088 (45.6 M) | 48 193 536 | 125 829 120 | yes | healthy 14 s | 200 |
| kvservice | 20002 | 102048 | 48 431 104 (46.2 M) | 48 873 472 | 125 829 120 | yes | healthy 14 s | 200 |
| llmgateway | 20006 | 110240 | 49 192 960 (46.9 M) | 49 598 464 | 157 286 400 | yes | healthy 15 s | 200 |
| logservice | 20007 | 111264 | 50 118 656 (47.8 M) | 50 388 992 | 209 715 200 | yes | healthy 16 s | 200 |
| messageservice | 20008 | 112288 | 48 357 376 (46.1 M) | 48 697 344 | 125 829 120 | yes | healthy 13 s | 200 |
| notificationservice | 20009 | 113312 | 48 611 328 (46.4 M) | 49 762 304 | 125 829 120 | yes | healthy 16 s | 200 |
| timeservice | 20003 | 103072 | 58 675 200 (56.0 M) | 81 551 360 | 157 286 400 | yes | healthy 17 s | 200 |
| wechatservice | 20012 | 116384 | 48 848 896 (46.6 M) | 49 221 632 | 157 286 400 | yes | healthy 13 s | 200 |
| **oss** | 20010 | 114336 | — | — | 209 715 200 | **no** | identity, never seen | 502 |
| **secretsservice** | 20011 | 115360 | — | — | 125 829 120 | **no** | identity, never seen | 502 |

### Tier 2 — the remaining apps (6 of 8 up) + both static sites

| id | port | uid | memory.current | memory.peak | memory.max | healthy | registered | gw |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| files | 20014 | 118432 | 60 268 544 (57.5 M) | 85 614 592 (81.6 M) | 209 715 200 | yes | healthy 20 s | 200 `/files` |
| locationservice | 20016 | 120480 | 59 211 776 (56.5 M) | 84 500 480 (80.6 M) | 209 715 200 | yes | healthy 9 s | 200 `Host: location` |
| mailbox | 20017 | 121504 | 65 798 144 (62.8 M) | 85 876 736 (81.9 M) | 209 715 200 | yes | healthy 13 s | 200 `Host: mail` |
| pages | 20018 | 122528 | 64 786 432 (61.8 M) | 86 216 704 (82.2 M) | 209 715 200 | yes | healthy 17 s | 200 `Host: pages` |
| turingtest | 20019 | 123552 | 69 611 520 (66.4 M) | 83 628 032 (79.8 M) | 262 144 000 | yes | healthy 21 s | 200 `Host: turning-test` |
| llmpricing | 20020 | 124576 | 67 330 048 (64.2 M) | 81 178 624 (77.4 M) | 157 286 400 | yes | healthy 24 s | 200 `/llm-live-pricing` |
| **resume** | 20013 | 117408 | — | — | 209 715 200 | **no** | no identity | 502 |
| **displayservice** | 20015 | 119456 | — | — | 209 715 200 | **no** | identity, never seen | 502 |
| files-web | (static) | 125600 | — no process — | | | n/a | n/a | 200 `Host: file`, 2818 B |
| llm-web | (static) | 126624 | — no process — | | | n/a | n/a | 200 `Host: llm`, 20697 B |

`last_seen` ages of 9–24 s against a 30 s SDK heartbeat mean the heartbeat
threads are live against the replica registry, not merely that registration
succeeded [verified, 15 services]. The registry reports **19** service rows: 15
heartbeating, plus identities for `oss`, `displayservice` and `secretsservice`
that the sync created before their processes failed, and none for `resume`,
which never got that far.

**This is the first live exercise of the subdomain and static gateway paths.**
platform-layer0.md §9 recorded both as golden-file-only, because neither pilot
manifest used them. Five subdomain mounts and two `file_server` static mounts
now answer 200 through the real Caddy [verified].

## 2. The resource verdict

**20 ams services + 2 static sites fit in 2 GB, with ~650 MB to spare. The
answer to Q8's "14 processes" is that 20 fit, and the honest limit was never
reached — the fleet ran out of *working services*, not memory.**

| measurement | value |
| --- | --- |
| harness cgroup `memory.current`, all 20 services | 1 097 969 664 (1047 MiB) |
| `free -m` available, settled | 650 MiB |
| `free -m` used / buff-cache | 1317 / 678 MiB |
| swap used | 148 MiB of 1023 |
| store use (`df`, XFS reflink loop) | 1.6 G of 6.0 G |
| uid blocks carved | 27 of 64 (`state/uidmap.json`) |
| ports allocated | 20000–20020 plus fixed 20100/20101/20180 |

The 300 MB floor this task was told to stop at was **never hit**: the soak added
all eight tier-2 apps and both static sites and still ended at 650 MB available.
Every service that could start, did.

Per-service steady state, measured rather than assumed:

| class | steady `memory.current` | n |
| --- | --- | --- |
| `services/*` (tier 1) | 45.6 – 47.8 MiB | 9 |
| `apps/*` (tier 2) | 56.5 – 66.4 MiB | 6 |
| registry | 71.1 MiB (burst 379.4 MiB) | 1 |
| auth | 51.6 MiB (peak 120.8 MiB) | 1 |
| caddy | 12.1 MiB | 1 |

Q8's planning constant was "~60 MB RSS per uvicorn, n=1 from the pilot". That
holds up: tier-1 services land ~46 MiB and tier-2 apps ~60 MiB, so the estimate
was right to within the spread. What Q8 did **not** predict is the registry's
379 MiB registration burst (§3), and that, not the per-service figure, is what
nearly killed the bring-up.

`memory.peak` for tier 2 runs 20–25 MiB above steady state (e.g. `pages`
86.2 M peak vs 61.8 M current), so a cap must be sized on the *start-up* number.
The 120 MiB floor and 150 MiB default (D-Q8) are comfortable for both classes;
nothing was clipped.

## 3. What the bring-up found

Six findings, all with a live signal. Full reasoning and rejected alternatives
in DECISIONS **D28**.

1. **The registry OOM-killed itself twice and took the fleet with it.** At its
   200M cap (`anon-rss:202988kB`) and again at 320M (`anon-rss:325416kB`), inside
   its own cgroup, while the tier-1 fleet registered at once. Every Layer-1
   service registers inside its FastAPI lifespan, so `Connection refused` became
   `Application startup failed` for all 11, five attempts each, and the whole
   tier parked in `failed`. Memory tracking the cap that closely looks like a
   leak, so it was **measured instead of guessed**: cap raised to 700M, fleet
   restarted to force the burst, `memory.current` sampled every 10 s for
   5 minutes.

   | t (s) | registry `memory.current` | registry `memory.peak` |
   | --- | --- | --- |
   | 0 | 60 010 496 | 120 721 408 |
   | 20 | 60 018 688 | 120 721 408 |
   | 30 | 63 590 400 | **397 852 672** |
   | 60 | 63 946 752 | 397 852 672 |
   | 180 | 64 397 312 | 397 852 672 |
   | 300 | 66 039 808 | 397 852 672 |

   **Burst, not leak** [verified]: peak fixed at 379.4 MiB during the
   registration storm at t≈30 s, current fell back to ~64 MB and crept 6 MB over
   the next 4.5 minutes. Cap is now 700M (`_REGISTRY_MEMORY_MAX`); the process
   demonstrably sits at 64–78 MB, so the headroom costs nothing while unused.

   **And the burst is very nearly flat in fleet size** — measured three times at
   the 700M cap, so the "do not extrapolate" caveat this report first carried is
   now replaced by evidence:

   | registering services | registry `memory.peak` | OOM? |
   | --- | --- | --- |
   | 9 | 397 852 672 (379.4 MiB) | no |
   | 20 | 403 202 048 (384.5 MiB) | no |
   | 20 (repeat) | 403 992 576 (385.3 MiB) | no |

   Going from 9 to 20 simultaneous registrations cost **1.5%** more peak memory,
   so the burst is dominated by fixed start-up cost (FastAPI init plus the MCP
   session manager), not by per-registration allocation [verified, n=3]. 512M
   would in fact have sufficed; 700M is kept because the margin is free and the
   two kills that motivated it were expensive.

2. **No start ordering meant a harness restart was a fleet outage — and
   `depends_on` fixed it, live.** Observed twice before the fix: every Layer-1
   service started alongside the registry, hit `Connection refused`, exited 3,
   and exhausted its retries. After the harness picked up T4.5's `depends_on =
   ["registry"]` the whole fleet restarts cleanly [verified, **n=3** harness
   restarts, the last two with all 24 declarations]:

   | restart | Layer 0 + demos start | registry healthy | dependents start | dependents on attempt 1 |
   | --- | --- | --- | --- | --- |
   | 02:06:39 | 02:06:39–40 | 02:06:44 | 02:06:45+ | 9 of 9 |
   | 02:41:23 | 02:41:23–24 | 02:41:45 | 02:41:45+ | 17 of 17 |
   | 02:50:40 | 02:50:40–41 | 02:50:51 | 02:50:52+ | 19 of 19 |

   Not one dependent has crash-looped on a missing registry since. 19 of the 24
   declarations carry `depends_on = ["registry"]`; the five that do not are
   `registry`, `auth`, `caddy`, `hello` and `pyhello`, which is correct.
   This report's earlier failures are the evidence for why the feature was
   needed, and these three restarts are the evidence that it works.

3. **Two Caddy start-up warnings still escalated; both are now suppressed.**
   `admin endpoint disabled` on every start and `exiting; byeee!!` on every stop
   — 2 of the 4 that platform-layer0.md §7 classified as noise. `_TLS_MSG_RE`
   only caught the two that say "requires TLS". `_DELIBERATE_MSG_RE` now catches
   these, matched on the message rather than the logger so a real
   `{"level":"error","logger":"admin"}` still escalates (pinned by a test).
   Verified live: **zero** Caddy escalations from the harness pid running the
   fixed policy, against one per start before.

4. **`service_policies` needs no manual step.** The question T3.2 left open is
   answered: the registry has no write API, but migrations `003`–`009` seed the
   table and `ams platform bootstrap` points `REGISTRY_MIGRATIONS_DIR` at the
   staged tree, so the replica already carries all eight upstream rows with
   `schema_version` at 10 [verified by reading `registry.db`]. The only
   hand-written row is `('timeservice','kvservice')` — a pair no migration
   declares, invented for T3.2's M2M proof. Nothing was scripted; nothing needed
   to be.

5. **A code deploy between two ticks can make every declaration unloadable.**
   The translator began emitting `depends_on` while the *running* harness still
   had the old `schema.py` imported, so its reload rejected all nine freshly
   written declarations (`unknown top-level keys ['depends_on']`, `errors=9`) and
   restarted nothing. Services kept running on their in-memory declarations, so
   nothing broke — but a harness restart in that window would have failed to
   start nine services. Nothing detects this today. n=1.

6. **The registry's health probe flaps under a full-fleet start.** During the
   20-service restart the supervisor logged
   `registry health=FAIL http 127.0.0.1:20100/health TimeoutError: timed out`
   twice (02:43:33, 02:44:24), recovering within 35 s and 14 s. On one vCPU, 20
   uvicorn processes importing while 20 SDK clients register is enough contention
   that the registry cannot answer a 200 within the probe timeout. It never
   restarted — a health failure alone does not — and the fleet came up fine, but
   this is the contention signal to watch if the fleet grows or if anything is
   ever made to restart on a failed probe. n=2 flaps in one restart.

### The timer: no-op and single-service redeploy [verified]

`ams-platform-sync.timer` is `enabled --now`, firing every 60 s.

- **Second consecutive tick is a clean no-op**:
  `sync done: sha=013b4d074914 services=9 unchanged=9 failed=0 reloaded=False
  gateway_changed=0`, and a full `ctl status` pid list before and after two ticks
  is **byte-identical** — nothing restarted.
- **A commit inside one service's directory redeploys that service and nothing
  else** (D26). One line added to `api/apps/timeservice/README.md`, pushed to the
  mirror; the next tick reported
  `sha=93709211bf37 services=9 unchanged=8 failed=0 reloaded=True`, the harness
  logged `reload: timeservice changed; restarting` and `+0 ~1 -0 =15 errors=0`,
  and the pid diff was exactly one line:

  ```
  15c15
  < timeservice=2071809
  ---
  > timeservice=2074489
  ```

  The eight untouched services logged `nothing under services/<id>/ or
  shared//components/sdk/ changed since 013b4d074914; staying at it` and were not
  re-staged, re-provisioned or re-probed.
- **The same property holds for a declaration change that is not a code change.**
  Syncing `oss` and `secretsservice` once so their declarations would pick up
  `depends_on` produced `reload: oss changed; restarting`,
  `reload: secretsservice changed; restarting` and
  **`+0 ~2 -0 =22 errors=0`** — exactly the two services whose declaration
  changed, twenty-two untouched, no errors.

### Why the four dead services are dead

Two distinct causes, neither of them resources:

| id | cause | fixable here? |
| --- | --- | --- |
| oss | `botocore SSLError` for `https://replica-placeholder.r2.cloudflarestorage.com` — `ensure_bucket()` runs unconditionally in the lifespan | no: needs real R2 credentials |
| secretsservice | `KeyError: 'SECRETS_MASTER_KEY'` — required by `build_app`, declared by no manifest and no `service.ams.toml` | no: needs a one-line overlay in `api/` (T3.4's file) |
| resume | `PermissionError: '/var/lib/resume'` | no: translation gap, see below |
| displayservice | same as `resume` | no: same |

`resume` and `displayservice` read their DB path from a **code** default of
`/var/lib/<name>/`, which their manifests never set, so `translate`'s
`/var/lib/<n>/ → <root>/data/` rewrite (the pilot's finding #5) has no env value
to rewrite and the mapped uid cannot create the directory. The five tier-2
services whose manifest *does* set the path (`files`, `locationservice`,
`mailbox`, `pages`, `turingtest`) are unaffected — which is why 6 of 8 came up.
The fix is either an upstream manifest line or a translator that injects
`<SVC>_DB_PATH` when the manifest omits it; `translate.py` was out of this task's
scope and the second option guesses a variable name, so neither was taken.

### Escalations

The platform policy behaved as designed, including the parts that had never run
live. Counted from the current harness pid:

| class | what it was | verdict |
| --- | --- | --- |
| `post-sync health gate: failed > 300s` × 4 | oss, secretsservice, resume, displayservice | **real** — T3.3's gate, first live firing |
| `crash loop after sync` × 3 | resume, displayservice, timeservice | **real**, carries the sha pair |
| `N occurrences in one window` (many) | D24's dedupe summaries | **real** — the deduper working |
| `caddy | ERROR on stderr` × 18 | 502 access lines for the four dead services | **real**, caused by the four above |
| `post-sync health gate: declared > 300s` × 2 | files-web, llm-web | **false positive** — see below |
| caddy WARNING | none | suppressed by the D28 fix |

**One defect found and deliberately not fixed.** A `kind: static` mount's
terminal stage *is* `declared` — there is no process to health-gate — so
`_health_gate` escalates every correctly-published static site once per sha. The
fix is to read `kind` from `<state>/platform/mounts/<id>.json` (the state record
carries no `kind`) and skip static mounts. That is new I/O in `policy.py` rather
than a constant, and `policy.py` is T3.3's module; this task's scope allowed
constants only. Recorded here and in D28 so it is not rediscovered.

## 4. Manual steps taken

Every one of these is an operator act, not something the loop did:

1. `git config --global --add safe.directory /home/harness/store/upstream/api.git`
   on racknerd, so root could push into the harness-owned mirror; then
   `chown -R harness:harness` after each push.
2. Pushed `api/` branch `ams-platform` into the mirror (three times: the overlay
   commit, then two README commits). No history was rewritten and nothing was
   pushed to GitHub.
3. Set eight placeholder secrets to the literal `replica-placeholder`:
   `commentservice/DEEPSEEK_API_KEY`, `wechatservice/WECHAT_MP_APP{ID,SECRET}`,
   `notificationservice/BARK_DEVICE_KEY`, `emailservice/RESEND_API_KEY`,
   `oss/R2_{ACCOUNT_ID,ACCESS_KEY_ID,SECRET_ACCESS_KEY}`. **Four of those five
   services run with fake credentials and every provider call they make will
   fail.** That is expected and is not a platform defect.
4. Re-ran `ams platform bootstrap` twice to push the registry cap change into its
   declaration, each followed by `ams ctl reload` (which restarted registry only).
5. Restarted the 11 tier-1 services **one at a time**, waiting for each to go
   healthy, to recover from the OOM cascade. Eight came up in 10 s each.
6. Installed and enabled `ams-platform-sync.{service,timer}` into
   `/etc/systemd/system`.

Nothing else was hand-edited. No service's data dir was touched.

## 5. Unverified / open

- **n=1 for every number here.** One box, one bring-up. The longest continuous
  observation of a settled fleet is about five minutes.
- **The registry burst is now n=3 and flat (379.4 / 384.5 / 385.3 MiB at 9 / 20
  / 20 registrations), but all three are on one box with one CPU.** The flatness
  is good evidence that the cost is fixed rather than per-registration; it is not
  evidence about a different host, a warmer or colder page cache, or a registry
  serving real traffic while the fleet starts.
- **`memory.peak` is cumulative and page-cache-contaminated.** The tier-2 peaks
  (~80 MiB) include first-touch page cache charged to whoever faulted a page in
  first, exactly as the pilot found. They are an upper bound on the start-up
  burst, not a measurement of the working set.
- **No load was applied.** Every service is idle apart from its own heartbeat.
  These are idle-fleet numbers; a fleet serving traffic is unmeasured (n=0).
- **`oss`, `secretsservice`, `resume`, `displayservice` are down and stay down.**
  `oss` and `secretsservice` are also excluded from the timer's `--only` list, so
  their `failed` records will not be retried until someone edits the unit.
- **The static health-gate false positive is unfixed** (§3).
- **`endpoint` in the registry still points at production** — unchanged from
  platform-layer0.md §3, and now true for 19 rows instead of 2.
- **Auth is still only TCP-probed and no user token has ever been minted**
  (n=0), unchanged from platform-layer0.md §9.
- **`ams rm <id>` still does not exist.** Four dead services hold four uid
  blocks, four ports, four service roots and three registry identities, and there
  is no path that reclaims any of it.
