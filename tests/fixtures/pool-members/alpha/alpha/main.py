"""A minimal ASGI service for the pool runner live test.

Deliberately not FastAPI: ``pool_runner.py`` only needs an ASGI callable, and
keeping this fixture dependency-free keeps provisioning fast. It records
``SVC_NAME`` both at build time (``build_app()``) and again inside the
lifespan startup event -- the exact shape T0's AST survey found in five real
services (``.claude/state/spike-pool.md`` finding 1) and the property the
pool runner's per-phase identity-env swap exists to get right.
"""

from __future__ import annotations

import json
import os
from typing import Any


def build_app() -> Any:
    build_name = os.environ["SVC_NAME"]
    state: dict[str, str | None] = {"lifespan_name": None}

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    state["lifespan_name"] = os.environ["SVC_NAME"]
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
            return
        if scope["type"] != "http":
            return
        if scope["method"] == "GET" and scope["path"] == "/health":
            status = 200
            body = json.dumps({"svc": build_name, "lifespan": state["lifespan_name"]}).encode()
        else:
            status = 404
            body = b'{"detail": "not found"}'
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body})

    return app
