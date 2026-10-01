# Pool-merge facts — `api` monorepo

Read-only fact-finding for the "run many FastAPI services in one Python
process" design decision. No proposal here, only verified facts (with
method/confidence noted). Repo: `~/Codes/api`,
branch `ams-platform`, HEAD `93709211bf37` (2026-09-02 22:16:42 -0400),
working tree clean at time of survey.

Method: static reading (`grep`/`cat` of source, `service.yaml`, `pyproject.toml`).
**No `uv sync`/resolution was actually run** — dependency-compatibility claims
below are from reading version specifiers only, not a verified resolve. Flagged
inline as such.

## 0. Inventory correction

The brief listed 16 services + SDK. The repo actually has these Python
FastAPI apps (found via `find */service.yaml` + `src/` scan):

- Layer-0 (no `service.yaml` — bootstrapped directly, not deployer-managed):
  `components/registry`, `components/auth`
- Deployer-managed (`service.yaml` present), matching the brief's 16:
  `services/{kvservice,logservice,messageservice,notificationservice,
  emailservice,commentservice,wechatservice,llmgateway,oss,secretsservice}`,
  `apps/{timeservice,pages,mailbox,locationservice,files,turingtest,
  llmpricing,displayservice,resume}` — that's 17, not 14; `oss`,
  `secretsservice`, `displayservice`, `resume` were in the brief's "maybe"
  list and are confirmed real Python services with `service.yaml`.
- Extra apps present but **not** in the brief and **not** covered below:
  `apps/iwatchpet`, `apps/test-service` (both have `main.py` using the SDK
  the same way; no `service.yaml` found for either — likely dev-only/unlisted).
  `apps/files-web`, `apps/llm-web` are `kind: static` (bun-built static
  sites, no Python process, no ASGI app) — out of scope for pooling.
- `components/sdk` — the shared library, not a service.
- `components/deployer` — a separate Python package in this repo: the
  *legacy* systemd+Caddy deployer that reads `service.yaml` today. Its
  `Manifest` dataclass (`components/deployer/src/deployer/manifest.py:94-109`)
  is the authoritative schema — see §3.

So: **19 Python FastAPI processes today** (2 Layer-0 + 17 deployer-managed),
all counted below.

## 1. Package names — collision check

Verified via `find <svc>/src -maxdepth 1 -mindepth 1 -type d`: every service
has exactly one top-level importable package under `src/`, and **all 19 names
are distinct** — no collisions:

`registry, auth, sdk, kvservice, logservice, messageservice,
notificationservice, emailservice, commentservice, wechatservice,
llmgateway, oss, secretsservice, timeservice, pages, mailboxsvc,
locationservice, files, turingtest, llmpricing, displayservice, resume`

Note: `apps/mailbox`'s package is `mailboxsvc`, not `mailbox` — deliberately
renamed to avoid shadowing the stdlib `mailbox` module (comment in its
`service.yaml`: `# Import package is mailboxsvc (stdlib owns mailbox)`).
High confidence — exhaustive `find`, not sampled.

## 2. Per-service table

ASGI app object, startup command, health path, DB/identity env vars. `factory?`
= Y means `build_app()` returns a fresh `FastAPI()` instance; N means a
module-level `app = FastAPI(...)` object built at import time.

| service | app entrypoint | factory? | import-time work | command (`process.exec`) | health |
|---|---|---|---|---|---|
| registry | `registry.main:app` | **N** (module-level, `main.py:177`) | yes — see §2a | not in `service.yaml` (Layer-0, no deploy/process/mount block at all) | `/health` (assumed, not verified — no service.yaml to confirm registry path) |
| auth | `auth.main:app` | **N** (module-level, `main.py:269`) | yes — see §2a | Layer-0, same as above | not verified |
| resume | `resume.main:app` | **N** (module-level, `main.py:669`) | yes — module-level `DB_PATH = Path(os.environ.get("RESUME_DB_PATH", ...))` at `main.py:121`, `_write_lock = threading.Lock()` at `main.py:195` | `uvicorn resume.main:app --host 127.0.0.1 --port ${PORT}` | `/health` |
| kvservice | `kvservice.main:build_app` | Y | no | `uvicorn kvservice.main:build_app --factory ...` | `/health` |
| logservice | `logservice.main:build_app` | Y | no | same pattern | `/health` |
| messageservice | `messageservice.main:build_app` | Y | no | same pattern | `/health` |
| notificationservice | `notificationservice.main:build_app` | Y | no | same pattern | `/health` |
| emailservice | `emailservice.main:build_app` | Y | no | same pattern | `/health` |
| commentservice | `commentservice.main:build_app` | Y | no | same pattern | `/health` |
| wechatservice | `wechatservice.main:build_app` | Y | no | same pattern | `/health` |
| llmgateway | `llmgateway.main:build_app` | Y | no | same pattern | `/health` |
| oss | `oss.main:build_app` | Y | no | same pattern | `/health` |
| secretsservice | `secretsservice.main:build_app` | Y | no | same pattern | `/health` |
| timeservice | `timeservice.main:build_app` | Y | no | same pattern | `/health` |
| pages | `pages.main:build_app` | Y | no | same pattern | `/health` |
| mailbox | `mailboxsvc.main:build_app` | Y | no | same pattern | `/health` |
| locationservice | `locationservice.main:build_app` | Y | no | same pattern | `/health` |
| files | `files.main:build_app` | Y | no | same pattern | `/health` |
| turingtest | `turingtest.main:build_app` | Y | no | same pattern | `/health` |
| llmpricing | `llmpricing.main:build_app` | Y (extra optional args `cache=`, `fx=`) | no | same pattern | `/health` |
| displayservice | `displayservice.main:build_app` | Y | no | same pattern | `/health` |

All 17 deployer-managed services confirm `health_path: /health` in their
`service.yaml` `registry:` block, and the code has a literal `@app.get("/health")`
route in every one (grepped, n=17/17). registry/auth have no `service.yaml` to
read a `health_path` from; not independently verified they expose `/health` —
flagging as unverified, not assumed.

**2a. registry/auth import-time work** (both are module-level `app`, so
*everything* in `main.py` before line 177/269 runs at import time, in
addition to Python-level `FastAPI()` construction):
- `registry/main.py:112-121` and `auth/main.py:174-194`: read ~10 env vars
  each (DB path, JWT key paths, issuer, admin token, auth URL) — these reads
  happen inside a `_lifespan` async contextmanager function body (deferred
  until app startup, not at raw import), **except** `auth/main.py:268`:
  `_session_secret = os.environ.get("AUTH_SESSION_SECRET") or
  secrets.token_urlsafe(32)` — this one is a genuine **module-level**
  statement, so importing `auth.main` either reads `AUTH_SESSION_SECRET`
  from the process env or **mints a random secret at import time** (once
  per process, not per app-instance, since there's only one `app` object).
- Both wire `SessionMiddleware`/similar off that secret before returning
  `app` (auth line ~274). This is the one clear case where "just import
  the module twice for two logical mounts" cannot work — there is only ever
  one `app` object per process for registry and for auth, and one shared
  random secret for auth if `AUTH_SESSION_SECRET` is unset.

## 3. `service.yaml` schema (authoritative — from the deployer's own dataclasses)

Source: `components/deployer/src/deployer/manifest.py:35-109`. Complete set
of top-level/nested keys the schema accepts, confirmed against `n=17`
example files (all in `apps/*/service.yaml`, `services/*/service.yaml`):

```
schema_version, name, deploy{source{type,repo,branch,path}, target_dir, user, install},
process{exec, working_dir, environment, restart, restart_sec, memory_max},
mount{gateway, port, path, subdomain}, kind (service|static), audience,
display_name, owner, manual_restart, acl[{action,principal,effect}],
registry{capabilities, health_path}
```

**No `tags`, `group`, `pool`, or any multi-app-hosting field exists in the
schema.** Confirmed by reading the dataclass field lists exhaustively (not
sampled) — `Manifest` at `manifest.py:94-109` has exactly the 12 fields listed
above and no others.

Repo-wide grep for any existing notion of multi-app hosting (`Mount(`,
`app.mount(`, `Starlette(routes=`, `pool`, `tag`, `SERVICE_TAG`) turned up
**zero** relevant hits outside:
- `registry/main.py:83`: `app.mount("/mcp", MCPAuthGuard(mcp.streamable_http_app()))`
  — registry mounts its own internal MCP sub-app under itself; this is the
  only place in the whole repo that already proves `sdk.fastapi.setup_sdk()` +
  `app.mount()` coexist and work (registry uses both).
- `deployer/manifest.py:355`: a `Mount(...)` call — that's the deployer's own
  `Mount` **dataclass** (the `mount:` YAML section), unrelated to Starlette.

So: **there is no existing pool/tag/group concept anywhere in this repo** —
confirmed, not inferred.

`mount.port` is registered in the Registry and used by the (separate, Caddy
+ systemd) legacy deployer to build reverse-proxy targets; `${PORT}` in
`process.exec` is a systemd-level template variable
(`components/deployer/src/deployer/appliers/systemd.py:50`), **not** read by
any Python code via `os.environ["PORT"]`/`os.getenv("PORT")` — confirmed
zero hits for `"PORT"` in any service's Python source (only in
deployer's own tests/systemd applier). Uvicorn receives the port purely as
an `--port` CLI arg baked into the command line at deploy time.

## 4. Environment variables — per-service identity vs shared

**Per-process identity, read once via `sdk.config.load_from_env()`
(`components/sdk/src/sdk/config.py:40-64`)** — every one of the 17
deployer-managed services + `resume` calls `load_from_env()` (or, for
`turingtest`, `sdk_load_from_env()` — same function, aliased) exactly once,
inside `build_app()`/module scope, to build a **frozen** `SvcConfig`
dataclass (`config.py:21-38`, `@dataclass(frozen=True, slots=True)`):

| env var | required? | meaning | per-service? |
|---|---|---|---|
| `SVC_NAME` | required (`KeyError` if missing) | registry id | **yes, unique per service** |
| `SVC_AUDIENCE` | required | JWT audience | **yes, unique per service** |
| `SVC_SECRET` | required | shared secret for `/register`+heartbeat | **yes, unique per service** |
| `SVC_ROOT_PATH` | optional | mount prefix (for `_check_route_prefixes` warning, and for the subset that pass it into `FastAPI(root_path=...)` — see below) | **yes, unique per service** |
| `SVC_ENDPOINT` | optional | explicit registered URL (subdomain-mounted services) | **yes, unique per service** |
| `SVC_DEV` | optional | disables all Registry network calls | could be shared (all-or-nothing) but is set per-service today |
| `SVC_HEARTBEAT_INTERVAL`, `SVC_CAPABILITIES`, `SVC_LOCATION`, `SVC_DISPLAY_NAME`, `SVC_OWNER`, `SVC_HEALTH_PATH`, `SVC_PUBLIC_BASE` | optional | metadata | per-service (small variation) |
| `SVC_M2M_PUBLIC_KEY_PATH` | optional | path to auth's RS256 public key | **shared** — every service that sets it points at the same file (`/etc/auth/jwt-rs256.pub`, per comment at `services/logservice/main.py:374`); not a per-service identity value despite living in `SvcConfig` |
| `REGISTRY_URL`, `AUTH_URL` | optional | endpoints | shared across all services (same defaults) |
| `<PREFIX>_DB_PATH` (e.g. `KV_DB_PATH`, `LOG_DB_PATH`, `MESSAGE_DB_PATH`, `EMAIL_DB_PATH`, `COMMENT_DB_PATH`, `LLMGW_DB_PATH`, `OSS_DB_PATH`, `SECRETS_DB_PATH`+`SECRETS_MASTER_KEY`, `FILES_DB_PATH`, `PAGES_DB_PATH`, `MAILBOX_DB_PATH`, `LOCATION_DB_PATH`, `TURINGTEST_DB_PATH`, `DISPLAY_DB_PATH`, `RESUME_DB_PATH`) | most required or defaulted | sqlite file path, distinct filenames per service | **yes, distinct paths — no collisions observed (n=15, all under distinct `/var/lib/<service>/` dirs)** |

**Load-bearing constraint for pooling**: `os.environ` is process-global.
`load_from_env()` reads it exactly **once**, synchronously, at
`build_app()`-call time (confirmed: no service re-reads `SVC_*` env after
construction; the resulting `SvcConfig` is `frozen=True`). This means 16+
services cannot coexist with 16 different `SVC_NAME`/`SVC_SECRET`/
`SVC_ROOT_PATH` simultaneously present in one process's env — **but** because
the read happens once per `build_app()` invocation, a pool launcher that
calls `build_app()` for each service *sequentially*, temporarily mutating
`os.environ` (or monkeypatching `os.environ` per call) before each call and
restoring after, would work with the SDK as written today — no SDK code
change required for that specific pattern. This is an architecture option,
not a recommendation (left to the design decision).

`root_path=os.environ.get("SVC_ROOT_PATH", "")` is passed directly into
`FastAPI(...)` (not via `SvcConfig`) in a subset of services — confirmed at:
`llmgateway/main.py:234`, `pages/main.py:280`, `mailboxsvc/main.py:334`,
`locationservice/main.py:260`, `llmpricing/main.py:257`, `resume/main.py:674`.
The rest (`kvservice, logservice, messageservice, notificationservice,
emailservice, commentservice, wechatservice, oss, secretsservice,
turingtest, timeservice, files, displayservice`) do **not** set FastAPI's
`root_path` even though several of them also set `SVC_ROOT_PATH` in their
`service.yaml` environment block — for those, `SVC_ROOT_PATH` is consumed
only by `sdk.fastapi._check_route_prefixes` (a startup warning check, not
routing behavior) since Caddy's `handle_path` already strips the prefix
before the request reaches the app. Confirmed by grep, n=19 main.py files,
not sampled. **Whether `FastAPI(root_path=...)` and `app.mount("/prefix",
sub_app)` compose correctly together was not tested here** — flagging as
open, not verified (registry's own internal `app.mount("/mcp", ...)` doesn't
exercise this because registry itself has no `root_path` set).

## 5. `pyproject.toml` dependencies

All 21 `pyproject.toml` files read (19 services + sdk + deployer not
counted). Every service declares:

```
fastapi>=0.110
uvicorn[standard]>=0.27
sdk  (uv path dependency: "../../components/sdk", editable=true;
      registry/auth use "../sdk")
```

**No version-constraint conflicts found** across any service (no
`pydantic<2` anywhere; `notificationservice` and `emailservice` are the only
two with an explicit `pydantic` constraint at all — both `pydantic[email]>=2.0`
/ `pydantic>=2.0`, compatible). No `sqlalchemy` dependency exists anywhere in
the repo (grepped) — every DB-backed service uses stdlib `sqlite3` directly
against its own `*_DB_PATH` file, so there is no ORM-version axis to
conflict on. This is a **static reading of specifiers only — `uv sync`/lock
resolution across all 19 into one venv was not actually run**, so
"resolvable into one venv" is a plausible-but-unverified inference (n=21
files read, 0 conflicts found in specifiers; confidence: high for "no stated
conflicts", not verified for "actually resolves").

Per-service extra/native deps (beyond the three shared above):

| service | extra deps |
|---|---|
| registry | `pyjwt[crypto]>=2.8`, `argon2-cffi>=23.1`, `httpx>=0.27`, `mcp[cli]>=1.9.0` |
| auth | `pyjwt[crypto]>=2.8`, `argon2-cffi>=23.1`, `httpx>=0.27`, `authlib>=1.3`, `itsdangerous>=2.2`, `webauthn>=2.0` |
| notificationservice | `httpx>=0.27`, `pydantic>=2.0` |
| emailservice | `httpx>=0.27`, `aiosmtplib>=3.0`, `pydantic[email]>=2.0` |
| wechatservice | `httpx>=0.27` |
| llmgateway | `httpx>=0.27`, `python-multipart>=0.0.9` |
| pages | `python-multipart>=0.0.9` |
| mailbox | `pydantic[email]>=2.0`, `httpx>=0.27` |
| locationservice | `python-multipart>=0.0.9` |
| turingtest | `httpx>=0.27` (runtime, not dev-only — comment explains bot.py calls DeepSeek) |
| llmpricing | `httpx>=0.27` |
| oss | `boto3>=1.34` (native-ish, pulls botocore) |
| displayservice | `pillow>=10.4` (native, C extension), `httpx>=0.27`, `paho-mqtt>=2.0` |
| resume | `email-validator>=2.0`, `httpx>=0.27` |
| kvservice, logservice, messageservice, commentservice, secretsservice, timeservice, files | fastapi/uvicorn/sdk only, no extras |

`sdk`'s own deps (`components/sdk/pyproject.toml`): `httpx>=0.27`,
`pyjwt[crypto]>=2.8`, `beautifulsoup4>=4.12`, `markdownify>=0.13`; `fastapi`
is an **optional extra** (`[project.optional-dependencies] fastapi =
["fastapi>=0.110"]`), separate from the `dev` extra.

**uv workspace**: root `pyproject.toml` explicitly documents there is
**no workspace** (`# Root config — dev tooling only. Each component is its
own Python package with its own pyproject.toml and .venv. There is no
workspace project here.`, confirmed no `[tool.uv.workspace]` table anywhere).
Every one of the 21 packages has its **own `uv.lock`** (confirmed present for
all 21, n=21/21) and its own venv, `sdk` pulled in as an editable path
dependency (not a workspace member).

## 6. Process-ownership assumptions (threads, signals, mounts, websockets, lifespan)

Grepped `threading.`, `Thread(`, `signal.signal`, `sys.exit`,
`asyncio.create_task`, `BackgroundTasks`, `@asynccontextmanager`,
`StaticFiles` across all 21 `main.py` files (n=21, exhaustive not sampled):

- **All 19 deployer-managed + resume/registry/auth use `@asynccontextmanager`
  lifespan** with `asyncio.create_task(...)` for their background loops (GC
  sweeps in kvservice/logservice/oss/files, moderation loop in commentservice,
  MCP worker in registry/auth, display refresh in displayservice). None use
  raw `threading.Thread` for app-level background work — the only
  `threading.*` usage outside the SDK is plain `threading.Lock()` mutexes
  (`pages/main.py:186`, `files/main.py:203,1642`, `resume/main.py:195`) for
  in-process critical sections — harmless to share a process, in fact
  *more* correct in a pooled single process than across separate processes.
- **Zero `signal.signal(...)` calls** and **zero `sys.exit(...)` calls** in
  any service's `main.py` — nothing installs its own signal handlers or
  assumes it can terminate the whole process.
- **Zero `StaticFiles` mounts** in any of the 21 `main.py` files (static
  sites are handled entirely outside Python, by the `kind: static` Caddy
  path — `files-web`, `llm-web`).
- **Websockets**: `files` (`/p2p/ws`, `main.py:604`) and `turingtest`
  (`/ws`, `main.py:230`) each define one websocket route; both explicitly
  route around the SDK's HTTP-only auth middleware (comment at both sites:
  `@app.middleware("http")` doesn't run for websocket scope, so each does
  its own origin check) — no process-global assumption, self-contained per
  app.
- **The SDK's own background threads** (daemon, per `Registry`/`M2MClient`/
  `CLSHandler` instance — see §7) are *per-instance*, not per-process
  singletons, so N service instances in one process would just mean N
  independent heartbeat/M2M-refresh/CLS-forward daemon threads. Not
  inherently broken, but N× the thread count of today for the same box.

## 7. SDK (`components/sdk`) — global-state and logging audit

**Registry heartbeat** (`sdk/registry.py`): `Registry.start()` spawns one
daemon `threading.Thread` named `f"sdk-heartbeat-{self._config.name}"`
(`registry.py:143-148`) per `Registry` **instance** — not a module
singleton. `Registry.__init__` (`registry.py:38-70`) stores no class-level/
module-level mutable state; every field is `self.`-scoped. Confirmed no
`global` statement anywhere in `registry.py`. **Verdict: two `Registry`
instances in one process do not interfere with each other** — each owns its
own thread, `httpx.Client`, and status lock.

**`sdk.fastapi.setup_sdk`** (`fastapi.py:67-223`):
- `_request_ctx: contextvars.ContextVar[Request]` (`fastapi.py:62-64`) is
  **module-level** but is a `ContextVar` — safe under concurrent requests
  from multiple apps in one process because `ContextVar` is
  task/coroutine-scoped, not a shared mutable. Not a collision risk.
- `logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s:
  %(message)s", stream=sys.stderr)` at `fastapi.py:103`, gated by
  `if not logging.getLogger().handlers` — **additive/idempotent**: only the
  *first* service's `setup_sdk()` call in a process actually configures the
  root logger; every subsequent call is a no-op (verified test:
  `components/sdk/tests/test_fastapi_logging.py:39-50`, "Pre-existing
  handler/format wins — setup_sdk must not have called basicConfig"). This
  is exactly the commit named in the brief
  (`3ce73d51 sdk: configure root logger with level tokens when
  unconfigured (ams log classification)`).
- **The format string `"%(levelname)s %(name)s: %(message)s"` does NOT
  include the service name/tag** — `%(name)s` is the Python **logger**
  name (e.g. `sdk.registry`, `uvicorn.access`, `kvservice.main`), not the
  platform service id. So a pooled process's stderr output would **not**
  self-identify which mounted service emitted a given line unless each
  service's own loggers happen to be named distinctly (they are, by module
  path — `kvservice.*` vs `logservice.*` — so in practice lines ARE
  attributable via logger name, just not via an explicit service tag field).
  `ams`'s own `[logging] format` severity-heuristic parser (per this repo's
  `events.py`) would see the same single, undifferentiated stderr stream
  from a pooled process either way.
- **CLS forwarding does carry the service name**: `setup_sdk(...,
  forward_logs=True, log_source=...)` → `sdk.cls.setup_cls_logging(source=
  log_source or registry.config.name, ...)` (`fastapi.py:150`) stamps every
  forwarded log entry's `"source"` field with the service's `SVC_NAME`
  (`cls.py:389`, `_record_to_entry`). So the **CLS/logservice path already
  has per-service attribution built in**; only the raw stderr/`ams`-visible
  path lacks an explicit tag.
- No other global mutable state found in `sdk/`: grepped every remaining
  module (`acl.py`, `auth_client.py`, `m2m_client.py`, `m2m_verifier.py`,
  `deps.py`, `decorators.py`, `errors.py`, `principal.py`, `_bearer.py`,
  `message_bus_client.py`, `notify_client.py`, `agent.py`, `ui.py`) for
  module-level assignments outside `logger = logging.getLogger(__name__)`
  and frozen constant tuples/ints — none found (n=13 files, exhaustive).
  Every client (`ACLClient`, `AuthClient`, `M2MClient`, `M2MVerifier`) is
  constructed per-service inside `build_app()` and stored on that app's own
  `app.state`, not module-level.

## 8. `service.yaml` field inventory across all 17 example files (verbatim keys seen)

No `tags`, `group`, or `pool` field exists anywhere (see §3 for the
authoritative schema — this is the observed-usage cross-check, n=17/17
files, all keys below are a subset of the schema in §3, nothing extra):

`schema_version, kind, name, display_name, audience, owner, deploy.source.{type,repo,branch,path}, deploy.install, deploy.target_dir, deploy.user, process.exec, process.working_dir, process.environment.<KEY>, process.restart, process.restart_sec, process.memory_max, mount.gateway, mount.subdomain, mount.path, mount.port, acl[].{action,principal,effect}, registry.capabilities, registry.health_path`

Mount pattern breakdown (n=17): 10 use `mount.path` on the shared
`api.lishuyu.app` gateway (path-prefix mounts: files, resume, timeservice,
llmpricing, kvservice, llmgateway, commentservice, emailservice, oss,
secretsservice, logservice, messageservice, notificationservice, wechatservice
— that's actually 14, correcting: path-mounted = files, resume, timeservice,
llmpricing, kvservice, llmgateway, commentservice, emailservice, oss,
secretsservice, logservice, messageservice, notificationservice,
wechatservice = **14**); 6 use `mount.subdomain` on their own hostname
(subdomain-mounted, no path prefix: displayservice, locationservice,
mailbox, pages, turingtest = **5**, plus `files-web`/`llm-web` static, not
counted here since not Python services). 14+5 = 19 which is more than 17 —
recount: the 17 Python deployer-managed services split as **14 path-mounted
+ 3 subdomain-mounted with a port** (displayservice, locationservice,
mailbox) **+ 2 kind:service subdomain w/o a distinguishing extra field**
(pages, turingtest) — all 5 subdomain-mounted ones still declare
`mount.port` (confirmed present in all 5), so `mount.port` is universal
across every `kind: service` entry regardless of path- or subdomain-mount
style; only `kind: static` entries omit `port`.

---

## Confidence summary

- **High confidence, exhaustive (n = all files in category)**: package-name
  uniqueness (§1), app-entrypoint pattern per service (§2), schema field
  list (§3), env-var identity classification (§4), dependency specifiers
  and uv.lock/no-workspace facts (§5), thread/signal/mount/websocket audit
  (§6), SDK global-state audit (§7).
- **Explicitly unverified, flagged inline**: whether the 21 packages'
  dependencies actually resolve together in one venv (only specifiers were
  read, no `uv sync` run); whether registry's health path is literally
  `/health` (no `service.yaml` for Layer-0 services to confirm against);
  whether `FastAPI(root_path=...)` composes correctly with `app.mount()` for
  a sub-app (no test exercises this combination in the repo today).
- **Do not treat as settled without re-verification**: the sequential
  env-swap approach to reusing `load_from_env()` unmodified for pooling
  (§4) is a *plausible mechanism*, not something exercised by any existing
  test in this repo.
