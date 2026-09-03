# spike-pool — T0 result (racknerd, 2026-09-03)

Verdict: **the shape works**, with two corrections to the plan's runner contract.

## Setup
Scratch venv `/home/harness/store/scratch-pool` (uv, reflink cache, python 3.12.14,
uvicorn 0.52.4), 15 Layer-1 projects + sdk installed editable together
(resolved, no conflicts). Scripts archived in `.claude/state/evidence/`:
`pool_probe.py` (Mount variant, alternative A), `pool_spike2.py` (N-ports
variant, alternative B), `envsurvey.py` (AST survey of env reads).

## Result (`pool_spike2.py`, n=3 runs, identical)
6 members (kvservice, timeservice, llmpricing, logservice, messageservice,
commentservice), 6 `uvicorn.Server`s on ports 21000–21005 in one event loop:
every `/health` 200, every `/openapi.json` carries its own title, RSS 66 MiB
(ru_maxrss), 3–4 threads, SIGTERM → all stopped in 0.63–0.73 s, 0 failed.
Earlier Mount variant (`pool_probe.py`, 4 members, one port): also 200 on
health/openapi/docs, 78 MiB.

## Findings that change the plan
1. **`load_from_env()` is NOT one-shot at `build_app()` time.** AST survey over
   all 19 services + sdk (n=19, static, `envsurvey.py`): `timeservice`,
   `llmpricing`, `resume`, `displayservice`, `test-service` call
   `load_from_env()` again inside their **lifespan**. Env-swap must therefore
   wrap `build_app()` AND lifespan startup AND lifespan shutdown per member.
   Verified: uvicorn `Server` splits cleanly into `config.load()` +
   `lifespan = config.lifespan_class(config)` + `await startup()` (runs the
   lifespan) + `main_loop()` + `shutdown()` — the spike runs `startup()`
   sequentially under each member's identity env and the `main_loop()`s
   concurrently.
2. **Request-time env reads exist but use service-specific names**
   (`MESSAGE_INGEST_TOKEN`, `NOTIFY_EMAIL_*`, `LOCATION_*`, `MAILBOX_*`,
   `DEEPSEEK_API_KEY`, `RESEND_API_KEY`, `BOT_LLM_*`…). So the process env can
   hold the **union** of all members' non-identity keys. Translate must reject a
   pool where two members set the same non-identity key to different values.
   The identity keys (`SVC_NAME`, `SVC_AUDIENCE`, `SVC_SECRET`, `SVC_ROOT_PATH`,
   `SVC_ENDPOINT`, `SVC_CAPABILITIES`, `SVC_HEALTH_PATH`, `SVC_DISPLAY_NAME`,
   `SVC_OWNER`, `SVC_LOCATION`, `PORT`, `AMS_DATA_DIR`) are swapped per phase.
   The one request-time read of an identity key: `SVC_ENDPOINT` in
   `sdk/ui.py:266 login_redirect` and `mailbox/main.py:249 _sso_redirect`
   (login return-to). In a pool it resolves to "" — needs a small api-side fix
   (derive from the request) or those two members stay standalone.
3. **uvicorn startup failure calls `sys.exit(3)`** inside the task; in one loop
   that `SystemExit` propagates out of `asyncio.run` and kills every member.
   The runner must catch `SystemExit` per member at startup and treat it as
   "member failed" (plan §4.3), not let it escape.
4. Spike artefacts, not findings: `pages` failed startup only because
   `SVC_DEV=1` disables M2M and its lifespan calls oss; a synchronous
   `urlopen` on the loop thread blocked the servers (use a thread).

## Addendum — one event loop and blocking I/O (audit, 2026-09-03, n=19 static)

Concern raised in plan §10 risk 7: in one loop, a member doing synchronous I/O
inside an `async def` handler stalls every member. `evidence/asyncaudit.py`
counted route handlers per service (decorator heuristic) and looked for
blocking libraries inside `async def` bodies:

| pooled member | async handlers | sync handlers | blocking text in async bodies |
|---|---|---|---|
| kvservice, logservice, commentservice, messageservice, secretsservice, pages, timeservice | 0 | 3–11 | sqlite3 only in lifespan init |
| notificationservice, emailservice, locationservice | 1 | 2–8 | sqlite3/smtplib (emailservice) |
| turingtest | 2 | 3 | – |
| mailbox | 5 | 7 | – |
| llmpricing, wechatservice | 6 | 1–2 | – |
| resume | 13 | 6 | – |
| standalone: llmgateway 11 async (`requests`), displayservice 4 (`subprocess`), files 0/27 | | | |

Reading: sync `def` handlers run on Starlette's shared anyio threadpool (default
40 threads) and never block the loop; the loop-blocking risk is confined to the
few `async def` handlers, and the heuristic found no blocking library calls in
those bodies for the pooled members (emailservice's smtplib hit is its lifespan/
provider init — verify in T10). What DOES couple: one 40-thread pool shared by
15 members instead of 15 × 40. Decision: keep the one-loop runner; fallback if
T10 shows cross-member latency coupling = one thread + one loop per member
(same memory, sequential startup still needed for the env swap). Weak signal,
not a Fact: the heuristic is textual, n=19.

## Fleet dry check — `build_pool("core")` over the 15 real manifests (2026-09-03, local)

api branch `ams-platform` @ `c8b1fff3` (overlays `pool = "core"` added to the
15 members; `f88ebe60` T11 SDK fix). Result: 15 members, 16 ports (`pool` +
one per member), 20 mangled secrets, `limits = 600M / 100% / pids 288`,
34 unioned non-identity env keys with NO conflicts, `resume` detected as a
module-level app (`factory=false`), `schema.loads(emit_toml(decl)) == decl`.

Ordering constraint for T10: do NOT push `ams-platform` to
`<store>/upstream/api.git` before the new ams is deployed — the live overlay
reader rejects the unknown `pool` key and the 60 s sync timer would mark all 15
members failed.

## Remote Linux suite after T1–T9 (2026-09-03, `remote-test.sh t10-live tests/linux`)
117 passed / 5 failed in 341 s. The 4 pool-runner live tests failed (member
ports never bound, `Connection refused`) — T2 diagnosing. The 5th,
`test_cgroup.py::test_pids_max_caps_a_fork_storm`, failed in the full run and in
the module run (with T2's remote provisioning running concurrently) but passed
alone (n=3 total) — load-sensitive timing, same open signal as before, not a
pools regression.
