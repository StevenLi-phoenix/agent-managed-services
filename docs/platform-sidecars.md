# Platform sidecars

An api `service.yaml` manifest carries three kinds of information. Only one of
them belongs in an ams declaration:

| information | destination | owner |
| --- | --- | --- |
| how to run the process | `service.toml` (`ServiceDecl`) | the supervisor |
| where it is mounted on the gateway | `mount.json` | `ams.platform.gateway` (T2.2) |
| what the registry should know about it | `registry.json` | `ams.platform.registryclient` (T2.3) |
| where the sync loop got to | `platform-state.json` | `ams.platform.sync` (T3.1) |

A supervisor that knows about HTTP mounts and ACL rules stops being a
supervisor (D7), so mounts and ACLs never enter `ServiceDecl`. They are written
beside it as **sidecars**: small, versioned JSON documents produced by
`ams.platform.translate` and consumed by the gateway renderer, the registry
client and the sync loop.

Every sidecar carries `"version": 1`. A consumer that reads a version it does
not know must fail loudly rather than guess. All three shapes are written
atomically with sorted keys, but through two writers: `mount.json` and
`registry.json` go through `sync._write_json_if_changed` (sorted keys, trailing
newline, no write at all when the bytes are unchanged), `platform-state.json`
through `ams.state.write_json_atomic` (sorted keys, no trailing newline). The
examples below and the committed goldens list `version` first; an on-disk file
does not — sorted keys put it last — so nothing may depend on key order.

## Locations

```
<state>/platform/mounts/<id>.json      mount.json
<state>/platform/registry/<id>.json    registry.json
<state>/platform/state.json            platform-state.json  (one file, all services)
<state>/platform/static/<id>/          static site document roots (kind=static)
```

`<state>` is `AMS_STATE_DIR`. `<id>` is the ams service id, i.e. the manifest's
`name`.

## `mount.json`

One per manifest, including `kind: static` manifests (which produce no ams
service at all). This is the only input the gateway renderer needs.

```json
{
  "version": 1,
  "id": "files",
  "kind": "service",
  "gateway": "api.lishuyu.app",
  "path": "/files",
  "subdomain": null,
  "port_name": "main",
  "static_root": null,
  "build": [],
  "headers": {}
}
```

| key | type | meaning |
| --- | --- | --- |
| `version` | int | always `1` |
| `id` | string | ams service id; matches the declaration's `id` |
| `kind` | `"service"` \| `"static"` | `static` sites have no process and no registry record |
| `gateway` | string | hostname the site is reachable under, verbatim from the manifest |
| `path` | string \| null | path mount under `gateway` (`handle_path <path>/*`); mutually exclusive with `subdomain` |
| `subdomain` | string \| null | subdomain mount; the renderer emits a whole site block for `gateway` |
| `port_name` | string \| null | name of the port in the declaration's `ports` table to reverse-proxy to. Always `"main"` for `kind: service`, `null` for `kind: static`. **The number is not here** — ams allocates it at runtime, so the renderer resolves `port_name` against the live allocation, never against the manifest's production port. |
| `static_root` | string \| null | `kind: static` only: document root, relative to `<state>/platform/static/` (always `<id>`) |
| `build` | list of strings | `kind: static` only: `deploy.install` verbatim. Informational for the renderer; T3.4 owns running it. Empty for `kind: service`. |
| `headers` | object | per-mount response-header **overrides**, keyed by header name. Empty means "template defaults only". The translator never writes into it; it exists so an operator can pin one header without forking the renderer. The renderer's own `default_csp` / `csp_map` / `admin_cors_map` tables stay in `gateway.py` as data (Q3) — they are not manifest-derived and are not duplicated here. |

Exactly one of `path` and `subdomain` is non-null for `kind: service`. A static
site may have either (`files-web` and `llm-web` both use `subdomain`).

## `registry.json`

One per `kind: service` manifest. `kind: static` produces none — there is no
registry record to attach rules to.

```json
{
  "version": 1,
  "id": "files",
  "audience": "files",
  "display_name": "Files Service",
  "owner": "steven",
  "capabilities": ["filestorage"],
  "health_path": "/health",
  "acl": [
    {"action": "read", "principal": "anon", "effect": "allow"},
    {"action": "write", "principal": "anon", "effect": "allow"}
  ]
}
```

| key | type | meaning |
| --- | --- | --- |
| `version` | int | always `1` |
| `id` | string | ams service id; the registry's `services.id` |
| `audience` | string | JWT `aud` claim; required, since `kind: static` never gets here |
| `display_name` | string \| null | `null` when the manifest omits `display_name` |
| `owner` | string \| null | `null` when the manifest omits `owner` |
| `capabilities` | list of strings | in manifest order, not sorted — `discover/{cap}` order is cosmetic but stable output matters for the goldens |
| `health_path` | string | defaults to `/health`; the same value is injected as `SVC_HEALTH_PATH` and used as the declaration's `[health].path` |
| `acl` | list of objects | `{action, principal, effect}` in manifest order. `effect` is always present (default `"allow"` filled in by the translator), so the client never has to know the default. |

The registry client (T2.3) turns this into one `POST /api/services` and one
`POST /api/acl` per rule. The service **secret** is never in this file: it lives
in the SecretStore under the service id (D16).

## `platform-state.json`

One file for the whole fleet, rewritten atomically by the sync loop.

```json
{
  "version": 1,
  "services": {
    "files": {
      "sha": "9f1c0b2e4a6d8f0011223344556677889900aabb",
      "prev_sha": "1122334455667788990011223344556677889900",
      "deployed_sha": "9f1c0b2e4a6d8f0011223344556677889900aabb",
      "stage": "healthy",
      "rolled_back_from": null,
      "error": null,
      "manual_restart": false,
      "escalated": false,
      "updated_at": "2026-09-02T13:04:07Z",
      "stage_since": "2026-09-02T13:03:12Z"
    }
  }
}
```

| key | type | meaning |
| --- | --- | --- |
| `version` | int | always `1` |
| `services` | object | ams service id → state record |

State record:

| key | type | meaning |
| --- | --- | --- |
| `sha` | string \| null | the commit this service is currently being driven to |
| `prev_sha` | string \| null | the last sha that reached `healthy`. A post-sync health failure escalates with **both** shas (T3.3), so the operator sees what changed. |
| `deployed_sha` | string \| null | the commit whose tree is actually staged at `<root>/repo`. Additive to version 1 (D26): a service the commit range did not touch is re-translated at *this* sha, so its declaration is byte-identical and nothing restarts. Falls back to the on-disk `.ams-sha` marker when absent. |
| `rolled_back_from` | string \| null | set by `ams platform rollback` to the commit the service was moved *away* from; `null` otherwise. Additive to version 1 (D27/T4.3). It is **not** a pin: the next sync tick drives the service back to the branch head. |
| `stage` | enum | see below |
| `error` | string \| null | one-line reason the service is at `failed`; `null` otherwise |
| `manual_restart` | bool | from the manifest. `true` = the sync loop applies everything but does not restart the service (self-deploy of core services). |
| `escalated` | bool | an escalation has already been emitted for the current `(sha, stage, error)`. Cleared on any stage change. Guarantees "a repeated translate failure escalates once, not per tick" (T3.3). |
| `health_failed_at` | string | UTC ISO-8601 `...Z`; when the health gate last **failed** for this service. Written only on a failure and omitted by `as_json` while unset; read only while the record is `failed` — it is the clock the 900 s retry hold (`failed_health_retry_s`) runs on, so a failed service that nothing moved re-probes every 15 min instead of every tick. |
| `updated_at` | string | UTC ISO-8601 `...Z`, second precision; touched on every write |
| `stage_since` | string | UTC ISO-8601 `...Z`; when the service entered the current `stage`. The health gate's "failing for N minutes" is measured from here. |

### `stage`

```
fetched → translated → provisioned → declared → reloaded → registered → healthy
                                                                    ↘ failed
```

| stage | reached when |
| --- | --- |
| `fetched` | the sha is materialised in the store and staged into `<root>/repo` |
| `translated` | `service.toml`, `mount.json` and `registry.json` are rendered (in memory) without error |
| `provisioned` | `uv sync` has completed for this sha |
| `declared` | `service.toml` and both sidecars are written to disk |
| `reloaded` | `ams ctl reload` has been accepted and the service is running the new declaration |
| `registered` | the registry identity exists and the ACL rules are upserted |
| `healthy` | the ams health probe has reported the service up at least once since `reloaded` |
| `failed` | any transition raised; `error` says which, `stage_since` says when |

`failed` is terminal for the tick, not for the service: the next sync retries
from the first stage whose inputs changed. A service at `failed` never blocks
another service — every service has its own record.

## Pools: two additive `mount.json` keys, one additive `pool.json`, two additive `platform-state.json` fields

A pooled member's `mount.json` gains two OPTIONAL keys, both absent by
default so an unpooled service's sidecar is byte-identical to before pools
existed:

| key | type | meaning |
| --- | --- | --- |
| `port_owner` | string \| null | the service id whose port allocation actually holds this mount's port — a pool id. Absent/null means "my own id", today's behaviour for every non-pooled service. |

`port_name` is unchanged in shape, but a pooled member's value is its own id
(mangled if it contains a hyphen — `translate.pool_port_name`) rather than
the constant `"main"`, since N members share the one declaration's `ports`
table and each needs a name of its own.

A pool's own `<root>/pool.json` is a **new** sidecar, version 1, read only by
`pool_runner.py` (never by `sync.py`'s callers besides the declare phase that
writes it, and by `backup.discover` to label a database by member). Its
shape, and the runner's env-swap contract, are documented in full in
`docs/platform-pools.md` rather than duplicated here.

`platform-state.json`'s per-service record gains two additive fields,
omitted by `as_json` when unset (same precedent as `deployed_sha`, D26):

| key | type | meaning |
| --- | --- | --- |
| `pool` | string \| null | on a *member* record: the pool it runs inside, unprefixed (`"core"`) |
| `pool_members` | list of strings | on a *pool* record: the member ids sharing this process, sorted |

`ams platform status` reads `pool_members` to sort pools first with their
members indented underneath, and adds a `POOL` column only when at least one
record carries either key.
