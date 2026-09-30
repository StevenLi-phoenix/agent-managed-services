# Manifest translation

> **Legacy manifest mode (api v2.0.0), deprecated in ams 1.1.0.** For the Cordis-based api core, see [platform-core.md](platform-core.md).

`ams.platform.translate.translate(text, ctx)` turns one api `service.yaml`
**manifest** into an ams `service.toml` **declaration** plus the two sidecars in
[platform-sidecars.md](platform-sidecars.md). It parses the manifest with
`ams.platform.yamlsubset` (stdlib only, no PyYAML) and **rejects anything it does
not recognise**: a mis-translated manifest deploys the wrong thing silently, a
`TranslateError` naming the field path does not.

All 21 manifests in the api repo translate today: 19 services and 2 static
sites. `tests/golden/platform/` holds a byte-exact golden for every one of them,
alongside a committed copy of each manifest (the `api/` clone is gitignored, so
the copies are what makes the suite runnable on the Linux host).

## Context

```python
TranslateContext(
    sha="…",                              # GIT_COMMIT for the service
    services_dir=Path("<state>/services"),  # service root = <services_dir>/<id>/root
    registry_url="http://127.0.0.1:19100",
    auth_url="http://127.0.0.1:19101",
    extra_secret_names=(),                # names from service.ams.toml (T3.4)
    default_memory_max="150M", memory_floor="120M",
    cpu_max="40%", pids_max=64, start_period_s=120.0,
)
```

`services_dir` rather than a per-service root: the service id lives in the
manifest, so the root cannot be known before parsing.

`registry_url` and `auth_url` **must** be `http://127.0.0.1:<port>`. The
constructor raises otherwise. This is the whole of PLAN-allin risk 5: the sync
loop generates a `SVC_SECRET` and creates a registry identity, and a wrong URL
would write those into production.

## Mapping

| manifest | declaration | notes |
| --- | --- | --- |
| `name` | `id` | must also match ams `^[a-z][a-z0-9-]{0,31}$`; longest real id is 19 chars |
| `display_name` | `name`, `env.SVC_DISPLAY_NAME` | omitted entirely when absent |
| `owner` | `env.SVC_OWNER` | omitted when absent |
| `audience` | `env.SVC_AUDIENCE` | required for `kind: service` |
| `manual_restart` | `Translation.flags["manual_restart"]` | a sidecar flag, never `restart.policy` |
| `deploy.source.type` | — | must be `git` |
| `deploy.source.path` | — | `kind: static` only; a service needs the whole monorepo for its relative SDK path dependency |
| `deploy.target_dir` | — | must equal `/srv/<name>`; ams stages the tree at `<root>/repo` |
| `deploy.install` | `[runtime]` | see below |
| `deploy.user` | — | required by the api schema, discarded: ams maps uid blocks itself |
| `process.exec` | `start.argv` | `shlex.split`; see below |
| `process.working_dir` | `start.workdir` | `/srv/<name>/<rel>` → `repo/<rel>` |
| `process.environment` | `[env]` | plus injected vars; see below |
| `process.restart` | `restart.policy` | `no`→`never`, `on-failure`→`on-failure`, `always`→`always` |
| `process.restart_sec` | `restart.backoff_s` | default 10 |
| `process.memory_max` | `limits.memory_max` | floored at 120M, default 150M (Q8) |
| — | `limits.cpu_max`, `limits.pids_max` | `40%` and `64` from the context |
| — | `[health]` | `kind="http"`, `port="main"`, `path=registry.health_path`, `start_period_s=120` |
| — | `[stop]` | `SIGTERM`, 10 s |
| — | `[ports]` | `main = 0` — ams allocates; `mount.port` is discarded |
| — | `secrets` | `["SVC_SECRET", *ctx.extra_secret_names]` |
| `mount.*` | `mount.json` | never enters the declaration (D7) |
| `acl`, `registry.*` | `registry.json` | never enters the declaration (D7) |

### `process.exec`

Split with `shlex.split`. The entry point
`/srv/<n>/<rel>/.venv/bin/<tool>` becomes bare `<tool>` — `runtime_env`
prepends the provisioned venv's `bin` to `PATH`. Any other absolute path under
`/srv/` raises: that location does not exist under ams and guessing a rewrite is
exactly what this translator refuses to do. An absolute path outside `/srv/`
(say `/usr/bin/node`) is kept verbatim.

`${PORT}` becomes `${PORT_main}`. Any other `${…}`, or a leftover `$`, raises:
argv is never handed to a shell (D1), so nothing else would expand.

### `deploy.install`

Exactly one command, matching exactly

```
cd <rel> && uv sync
cd <rel> && /usr/local/bin/uv sync
```

and `<rel>` must agree with `process.working_dir`. It becomes
`runtime = {kind = "uv", python = "3.12", sync = true}` with
`start.workdir = "repo/<rel>"`. Every other install command raises — D1 forbids
running shell strings, so an unrecognised one has to be modelled deliberately.

### `process.environment`

Values pass through unchanged except for one rewrite: anything under
`/var/lib/<name>/` becomes `<root>/data/…`, where `<root>` is
`<services_dir>/<id>/root`. That was the pilot's finding #5 — every stateful
service (which is all of them) points at a `/var/lib` path a mapped uid cannot
write. A value under `/var/lib/` belonging to a *different* service raises.

The absolute path is baked in rather than a `${AMS_DATA_DIR}`-style token,
because the declaration schema expands `${PORT_*}` and nothing else.

On top of the manifest's own variables the translator injects:

| variable | value |
| --- | --- |
| `SVC_NAME` | the id |
| `SVC_AUDIENCE` | `audience` |
| `PORT` | `${PORT_main}` (plain `PORT` is not reserved; the `PORT_` *prefix* is) |
| `GIT_COMMIT` | `ctx.sha` |
| `SVC_CAPABILITIES` | `registry.capabilities`, comma-joined (omitted when empty) |
| `SVC_DISPLAY_NAME` | `display_name` (omitted when absent) |
| `SVC_OWNER` | `owner` (omitted when absent) |
| `SVC_HEALTH_PATH` | `registry.health_path` |
| `SVC_M2M_PUBLIC_KEY_PATH` | `<root>/etc/jwt-rs256.pub` — `/etc/auth/jwt-rs256.pub` is unreachable for a mapped uid (Q4) |
| `REGISTRY_URL` | `ctx.registry_url` |
| `AUTH_URL` | `ctx.auth_url` |

A manifest that sets one of these raises, **except** `REGISTRY_URL` and
`AUTH_URL`: two manifests pin production's loopback auth port (`8001`) and the
replica allocates its own, so the context value wins there. A manifest env name
reserved by ams (`PATH`, `HOME`, `AMS_*`, `PORT_*`, …) also raises.

## `kind: static`

`files-web` and `llm-web` produce **no declaration and no registry record** —
`Translation.decl` and `Translation.registry` are `None`. Only `mount.json` is
written, carrying `static_root` (`<id>`, under `<state>/platform/static/`) and
`build` (the `deploy.install` list verbatim, for T3.4 to run). `process`,
`audience`, `acl`, `registry`, `deploy.user` and `mount.port` are all forbidden.

## Not supported

Anything below raises rather than being guessed at.

**YAML** (`YamlSubsetError`, naming the line): anchors, aliases, tags, merge
keys, multi-document streams, block scalars (`|`, `>`), tab indentation,
duplicate mapping keys, ambiguous numerics (`0755`, `1_000`, `.inf`), and the
YAML 1.1 boolean words `yes`/`no`/`on`/`off`/`y`/`n`. That last one matters:
PyYAML resolves a bare `no` to `False`, so a manifest writing `restart: no` —
legal per the deployer's own JSON schema — already reaches the deployer as a
boolean and fails its enum check. Write `restart: "no"`.

**Manifest** (`TranslateError`, naming the field path): any unknown key at any
level; `schema_version` other than 1; a `kind` other than `service`/`static`; an
install command outside the one recognised form; an install directory that
disagrees with `working_dir`; a `target_dir` or `working_dir` outside
`/srv/<name>`; an `exec` entry point that is neither a `.venv/bin/<tool>` nor an
absolute path outside `/srv/`; a substitution other than `${PORT}`; a
`/var/lib/` path belonging to another service; a reserved or
harness-injected env name; a boolean env value (YAML, JSON and systemd disagree
on the spelling — quote it); both or neither of `mount.path` and
`mount.subdomain`; a `subdomain` that is not the first label of `gateway`; a
missing `mount.port` on a service; `deploy.source.path` on a service; an ACL
principal outside the registry's regex.

## Emitting

`emit_toml(decl)` renders the declaration with a fixed table order and env/port
keys sorted, so the goldens are byte-stable. `schema.loads(emit_toml(d)) == d`
holds for every service, asserted per manifest in
`tests/test_platform_translate.py`.

## `service.ams.toml`: extra secret names (T3.4)

`TranslateContext.extra_secret_names` exists so a manifest can pull in a
secret it has no field for, but `translate()` never populates it -- the
manifest format has no such key and adding one would blur the same line D7
already draws (a manifest describes the service, not the harness's secret
store). Instead, `ams.platform.static.load_ams_overlay(manifest_dir)` reads an
optional `service.ams.toml` file beside `service.yaml`:

```toml
secrets = ["DEEPSEEK_API_KEY"]
[env]
FEATURE_X = "on"
```

`secrets` is a list of **names only** -- exactly what `extra_secret_names`
wants, one call site turns straight into the other: `ams.platform.
static.overlay_secret_names(manifest_dir)` returns `list[str]`, and the sync
loop (T3.1) passes it as `TranslateContext(..., extra_secret_names=tuple(...))`
before calling `translate()`. Values are never in this file; they go into the
SecretStore with `ams secret set <id> <NAME>` (D16). `[env]` is for the rarer
case of a non-secret value the manifest cannot express either (a tunable, a
feature flag); T3.1 folds it into the declaration's `[env]` itself, after
`translate()` returns -- `load_ams_overlay` only parses and validates it.

Both `secrets` entries and `env` keys are validated against the same
uppercase-only pattern `TranslateContext.__post_init__` itself enforces on
`extra_secret_names` (not the looser, mixed-case `ams.schema.ENV_NAME_RE`):
validating against the looser pattern here would let an overlay pass
`load_ams_overlay` only to fail later inside `translate()` with a less
specific error naming `ctx.extra_secret_names` instead of the overlay file.
Reserved names (`ams.schema.RESERVED_ENV`/`RESERVED_ENV_PREFIXES`) are
rejected here too, for the same reason. `docs/platform-sidecars.md` is
unaffected: these names still end up in the declaration's `secrets = [...]`
field exactly like `SVC_SECRET` (D16), never in a sidecar.

See `DECISIONS.md` for which of the 21 manifests actually need one, and why.

## `pool`: N manifests translated into one declaration

`load_ams_overlay` also reads an optional `pool = "<name>"` key from
`service.ams.toml` (`Overlay.pool`, `static.overlay_pool`). A manifest naming
a pool is still translated individually — `translate()` is called once per
manifest with `TranslateContext.pool` set to the unprefixed name, which
redirects every absolute path (`root_for`, and `_rewrite_data_path`'s new
`data_subdir` parameter) into the pool's root instead of the manifest's own
— but the sync loop never writes that manifest's `service.toml`. Instead it
collects every `PoolMember` extracted from those individual translations
(`translate.pool_member`) and calls `translate.build_pool(pool, members,
ctx)` once, which is what actually produces the pool's `ServiceDecl` and its
`pool.json` sidecar (the runner's own config, distinct from `mount.json`/
`registry.json`). `mangle_member` turns a hyphenated member id into the env
suffix its `POOL_PORT_<SUFFIX>` and `SVC_SECRET__<SUFFIX>` names use, and
`build_pool` raises if two members' ids mangle alike. Full contract,
including the identity-env split that makes one process env safe for N
members: `docs/platform-pools.md`.
