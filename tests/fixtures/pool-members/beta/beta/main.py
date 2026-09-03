"""Same shape as ``alpha.main`` (see that module's docstring), plus two ways
to break on purpose so the live test can exercise §4.5 of PLAN-pool.md.

``BETA_BREAK`` (read at build time, and again during lifespan startup so a
build-time-only reader would not see the change):

- ``"1"``       -- ``build_app()`` raises a plain exception. This is the
  "deliberately broken import/build" case: the runner must skip this member
  and keep serving the rest.
- ``"lifespan"`` -- ``build_app()`` succeeds, but the *lifespan startup*
  handler raises. Uvicorn turns a failed lifespan startup into
  ``sys.exit(3)`` inside its own task (T0 finding 3, spike-pool.md); this is
  the regression case for "the runner must catch ``SystemExit``, not just
  ``Exception``, or one member's bad lifespan takes down the whole pool."
- unset/anything else -- behaves exactly like ``alpha.main``.
"""

from __future__ import annotations

import json
import os
from typing import Any


def build_app() -> Any:
    if os.environ.get("BETA_BREAK") == "1":
        raise RuntimeError("beta forced to fail at build time (BETA_BREAK=1)")

    build_name = os.environ["SVC_NAME"]
    state: dict[str, str | None] = {"lifespan_name": None}

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    if os.environ.get("BETA_BREAK") == "lifespan":
                        raise RuntimeError(
                            "beta forced to fail at lifespan startup (BETA_BREAK=lifespan)"
                        )
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
