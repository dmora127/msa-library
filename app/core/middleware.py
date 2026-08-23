"""ASGI middleware.

Pure ASGI (`async def __call__(scope, receive, send)`) rather than Starlette's
BaseHTTPMiddleware, for three concrete reasons:

  1. `scope` is a plain dict shared down the whole stack, so after the router has
     run we can read `scope["route"]` -- the matched route TEMPLATE. That is what
     keeps metric cardinality bounded.
  2. Wrapping `send` lets us observe the real status code, including responses
     produced by exception handlers.
  3. It never touches the response body. The classic middleware bug is buffering
     the body to measure it, which breaks streaming and inflates memory on large
     responses. Here body messages pass straight through.
"""

from __future__ import annotations

import logging
import time

from app.core.metrics import record_request

log = logging.getLogger("af3cache.metrics")

# Requests that never matched a route (404s, and anything CORSMiddleware rejects
# before routing) all collapse to this single label value. Without it, internet
# background noise against a public IP -- /wp-login.php, /.env, /.git/config --
# would mint a new permanent time series per unique junk URL: unbounded,
# attacker-controlled cardinality.
UNMATCHED = "<unmatched>"


def _route_label(scope) -> str:  # noqa: ANN001
    """Return a BOUNDED label for the matched route.

    FastAPI's APIRoute puts itself in scope["route"], whose .path is the template
    ("/v1/uploads/{ingestion_id}/complete") rather than the concrete path -- that
    is the case that matters, since ingestion ids are unbounded.

    But FastAPI's own built-in endpoints (/openapi.json, /docs, /redoc,
    /docs/oauth2-redirect) are plain Starlette Routes, which set "endpoint" in
    scope but NOT "route". Without the fallback below they all collapse into
    "<unmatched>" alongside genuine 404s, which hides doc scraping in with
    internet noise.

    The `not scope.get("path_params")` guard is what keeps that fallback safe: a
    match that consumed no path parameters means the raw path IS its own template,
    so it cannot be unbounded. Anything that DID consume path params but somehow
    lacks a route object falls through to "<unmatched>" rather than risking
    per-request cardinality.
    """
    route = scope.get("route")
    template = getattr(route, "path", None)
    if template:
        return template
    if scope.get("endpoint") is not None and not scope.get("path_params"):
        return scope.get("path") or UNMATCHED
    return UNMATCHED


class MetricsMiddleware:
    """Record method/route/status/duration for every HTTP request."""

    def __init__(self, app):  # noqa: ANN001 - ASGI app callable
        self.app = app

    async def __call__(self, scope, receive, send):  # noqa: ANN001
        # lifespan and websocket scopes pass through untouched.
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start = time.perf_counter()
        # If the app raises before sending anything, no http.response.start ever
        # arrives. Defaulting to 500 records it as the error it is, rather than
        # dropping it silently.
        status = 500

        async def send_wrapper(message):  # noqa: ANN001
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            # `finally` (not `except`) so this runs on both success and failure,
            # and does NOT swallow: unhandled exceptions still propagate to
            # Starlette's ServerErrorMiddleware exactly as before.
            try:
                template = _route_label(scope)
                record_request(
                    scope.get("method", "UNKNOWN"),
                    template,
                    status,
                    time.perf_counter() - start,
                )
            except Exception:  # noqa: BLE001 - metrics must never break a request
                log.warning("metrics: middleware record failed", exc_info=True)
