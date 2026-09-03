# Pool probe — racknerd, 2026-09-03 (n=1 each, import/serve only, no traffic)

Scratch venv `/home/harness/store/scratch-pool` (uv, reflink cache), 15 Layer-1
projects + sdk installed editable together: resolved with no conflict.

| measurement | RSS (ru_maxrss) |
|---|---|
| `import sys` | 10 MiB |
| `import fastapi` | 44 MiB |
| `import fastapi,uvicorn,pydantic,httpx` | 46 MiB |
| 1 app imported (timeservice) | 53 MiB |
| 5 apps imported | 50 MiB |
| 14 Layer-1 apps imported | 59 MiB |
| 4 apps built via env-swap `build_app()`, mounted, served by one uvicorn (`pool_probe.py`) | 78 MiB |

Current fleet for comparison (cgroup memory.current): 16 Python processes at
44–74 MiB each, fleet total 1105 MiB, box has 1967 MiB.

`pool_probe.py` findings: `/<name>/health`, `/<name>/openapi.json`
(`servers=[{"url":"/<name>"}]`), `/<name>/docs` all 200 for kvservice,
timeservice, llmpricing, pages — regardless of whether the member passes
`root_path` into FastAPI (Starlette `Mount` sets `scope["root_path"]`).
`sdk.config.load_from_env()` requires `SVC_NAME`, `SVC_AUDIENCE`, `SVC_SECRET`.
With `SVC_DEV=1` the SDK still ran an ACL refresh against the default
`REGISTRY_URL` (401) — the pool env must carry the Layer-0 URLs like today.
