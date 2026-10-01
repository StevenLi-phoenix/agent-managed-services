# Mock deploy: the api platform on a fresh 1 GB DigitalOcean droplet (2026-09-03)

A rehearsal of Phase B's *deploy* half on a disposable box, using nothing but
the repo's own scripts. Production (`phm`, droplet `platform`, 2 GB) was not
touched. Instruction: "mock deploy to phm with new DO machine … DO 实例用 ram
1GB 版本的".

| | |
|---|---|
| Droplet | `platform-mock`, id 597528369, 203.0.113.10, `s-1vcpu-1gb` (961 MiB), 25 GB, nyc1, Ubuntu 24.04, tag `ams-mock`, ssh alias `phm-mock` |
| Cost | $6/month while it exists — delete with `doctl compute droplet delete 597528369` |
| Added by hand | 1 GiB swapfile (safety net; swap *use* is a measurement, see below), NodeSource node 22 (install-host only warns) |

## Sequence actually run

1. `doctl compute droplet create … --size s-1vcpu-1gb --image ubuntu-24-04-x64`.
2. rsync repo → `/root/ams-src`; `bash deploy/install-host.sh` (root).
3. `AMS_HOST=phm-mock scripts/deploy-racknerd.sh` → `/home/harness/ams`, unit installed, `ams check-host` all OK except the interactive-session cgroup line (expected).
4. `AMS_HOST=phm-mock scripts/platform-bootstrap.sh --no-deploy --ref ams-platform --no-layer1` → registry 200, auth tcp open, caddy `/ams-health` 200.
5. Placeholder secrets (`replica-placeholder`, same eight as racknerd but under the pool's mangled names: `pool-core/DEEPSEEK_API_KEY__COMMENTSERVICE` …, `oss/R2_*`); `ams-platform-sync.{service,timer}` installed verbatim from `deploy/`; first tick in the foreground.

## Gaps found in the scripts (each fixed in this commit)

1. **`install-host.sh` aborted at the pnpm step** on the minimal 24.04 cloud image:
   the standalone pnpm binary needs `libatomic.so.1`, which racknerd happened to
   have. `set -e` then skipped Caddy, the state dir and the unit. Fix: `libatomic1`
   in the apt line, plus an `apt-get update` before it (fresh images have stale lists).
2. **`layer0.py` assumed the pilot services exist.** Stage `stop-layer1` does
   `ctl stop kvservice` and the harness answers `unknown service` on a fresh host.
   Worse, since pools, a standalone kvservice/timeservice declared by the
   bring-up would collide with `pool-core` on the first sync tick. Fix:
   `--no-layer1` (Layer 0 only; the sync timer owns the fleet), passed through by
   `platform-bootstrap.sh --no-layer1`. Tests: `test_no_layer1_runs_every_stage_and_touches_no_layer1_service`,
   `test_cli_no_layer1_passes_an_empty_pilot_set`.
3. **Not fixed, documented:** node 22 and the sync/backup timer units are still
   hand steps (`install-host.sh` warns about node; `deploy-racknerd.sh` installs
   only `ams-harness.service`).
4. **`sync._phase_finish` opened the pool's health gate before its members had
   registry identities.** Fresh host, pool item ahead of its members in `work`:
   every member's startup 404ed on `POST /api/services/register`, the runner
   exited "no members started", and the pool was recorded `failed` after the
   90 s gate — *then* the tick created `llmpricing`'s identity. With the D29
   hold that record would not have been re-gated for 900 s although the next
   restart would have succeeded. Fix: register everything first, then gate
   (two passes; D30). Test: `test_every_identity_is_created_before_any_health_gate_opens`
   (red on the old code, green with the fix).

## Result of the fresh re-run (state wiped, all four fixes in, warm uv cache)

| time (UTC) | event |
|---|---|
| 15:45:11 | `ams-harness` restarted on empty state |
| 15:45:19 | registry, auth, caddy started (Layer 0 bring-up 19 s end to end incl. `git archive` + `uv sync` from a warm cache) |
| 15:45:48 | first sync tick starts `pool-core` (15 members) and `llmgateway`, Caddy restarted for 17 routes |
| 15:46:11 | `pool-core` healthy — **23 s** from exec to `/_pool/health` 200, one attempt, no crash |
| 15:49:11 | tick ends: `services=17 unchanged=0 failed=2 reloaded=True gateway_changed=17`; the 3 min is two 90 s gates on the known-dead members |
| 15:50 | second tick: `unchanged=17 failed=2 reloaded=False`, 1 s |

- **Healthy:** registry, auth, caddy, llmgateway, pool-core with 13/15 members.
  **Failed, as on racknerd and for the same reasons:** `resume`
  (`PermissionError: /var/lib/resume`) and `secretsservice` (no
  `SECRETS_MASTER_KEY`).
- **Routes:** 15/15 probed paths answer 200 through Caddy with
  `Host: api.lishuyu.app` (`/kv/health /time/now /log /message /notification
  /email /comment /wechat /location /llmpricing /pages /mailbox /turingtest
  /llmgateway /registry`).
- **Escalations in the fresh run (since 15:45):** 19, all start-up one-shots:
  `llmgateway` crashed once with the register 404 (standalone services still
  start at reload and register afterwards — the D30 open item; attempt 2 came
  up 16 s later), `secretsservice`/`resume` as above, `mailbox` failing to
  reach `oss` (placeholder R2 creds), one SDK route-prefix warning, and one
  `sdk.acl: acl refresh failed 401` (also a start-up one-shot on racknerd:
  108 lines in 12 h, all at the 08:41 cutover restart).
- **Cold-cache run (run 1, before the wipe):** Layer 0 provisioned in ~2.5 min
  (`uv` downloading into an empty store), fleet provisioning ~50 s more;
  swap peaked at 42 MB during `uv sync` and never grew afterwards.
- **Host memory with the fleet idle (`free -m`, 1 vCPU / 961 MiB):** used
  ~595–640 MiB, available ~320–366 MiB, swap 39 MiB flat, load < 0.1 after
  settling. Per-cgroup n=3 table below.
- **Not verified here:** the cross-pool M2M token path (verified on racknerd
  with the same commit, `pool-migration.md`), backups (no R2 credentials on
  the mock either), and anything on port 80/443 or a real hostname — the mock
  is loopback-only like the replica.

### Measurement, fleet idle (n=3, 60 s apart, `evidence/mock-do-1gb-2026-09-03.txt`)

| | mock (1 GB DO) | racknerd after pools (2 GB), same method |
|---|---|---|
| Python processes (services + harness) | 4 | 5 |
| service cgroups | 5 (registry, auth, caddy, llmgateway, pool-core) | 8 (+ displayservice, files, oss — not in the mock's `--only` set / not started) |
| Layer-1 cgroup sum | 186–187 MiB | 275 MiB |
| all services cgroup sum | 350–352 MiB | 464 MiB |
| `pool-core` memory.current / pids | 130–132 MiB / 27 | 101–134 MiB / 26–27 |
| `free` available | 357–366 MiB | — |
| swap used | 39 MiB, flat (all from `uv sync`) | n/a |
| `/time/health` via Caddy | 0.8–1.4 ms | ~5 ms (n=20, different method) |

The fleet the mock runs is smaller than racknerd's by three standalone
services (`displayservice`, `files`, `oss`), which is exactly the sync unit's
`--only` list; those three would add roughly 3 × 75–90 MiB on this host and
are the reason the difference is not a 1 GB-vs-2 GB effect. **Verdict, n=1
box:** the pooled fleet fits a 1 GB droplet with ~350 MiB to spare at idle;
the only swap use was provisioning, and a full 21-service fleet would need
either those three pooled too or a measurement before committing to 1 GB
for production.
