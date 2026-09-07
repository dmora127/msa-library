"""FastAPI application factory."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

from app.api import routes_jwks
from app.api.router import api_router
from app.core.config import get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging
from app.services.deps import get_namespace_registry, get_token_issuer
from app.services.token_issuer import JwksProvider


def create_app() -> FastAPI:
    configure_logging()
    # Fail fast: TOKEN_ISSUER_IMPL=pelican/inprocess cannot mint without a valid
    # namespace registry. Loading here (not lazily on the first upload) turns a
    # bad file into a container that never becomes healthy instead of a 503
    # storm to OSPool jobs. Cached, so get_token_issuer() below reuses it.
    get_namespace_registry()
    app = FastAPI(title="af3-cache", version="0.1.0")
    register_exception_handlers(app)
    # CORS is owned HERE, not at the edge: the Caddyfile deliberately sets no
    # Access-Control-* headers, because Caddy's `header` directive REPLACES the
    # upstream value and would silently widen this allowlist back to "*".
    # Only the OSG web front-ends need cross-origin access, and only to the
    # unauthenticated reads (/v1/stats, /v1/query). Content-Type is the only
    # header they send — /v1/uploads/* and /v1/admin/* are credentialed and are
    # not browser-facing, so X-API-Key/Authorization stay off this list; a
    # preflight that asks for them is refused. OSPool jobs send no Origin
    # header at all, so none of this touches them.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "https://osg-htc.org",
            "https://www.osg-htc.org",
            "https://opensciencegrid.org",
            "https://www.opensciencegrid.org",
        ],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type"],
    )
    # Prometheus instrumentation. Added AFTER CORSMiddleware, and Starlette's
    # add_middleware PREPENDS, so this ends up OUTERMOST -- it therefore times
    # the complete request and also sees preflights CORS rejects (those never
    # reach the router, so they are recorded as "<unmatched>").
    #
    # When disabled the middleware is not added to the stack at all and the
    # endpoint is not mounted -- not "present but inert".
    if get_settings().metrics_enabled:
        from prometheus_client import (
            CONTENT_TYPE_LATEST,
            REGISTRY,
            generate_latest,
        )

        from app.core.metrics import DbPoolCollector
        from app.core.middleware import MetricsMiddleware

        app.add_middleware(MetricsMiddleware)
        REGISTRY.register(DbPoolCollector())

        # include_in_schema=False: not part of the public contract, and it keeps
        # /metrics out of /openapi.json and /docs.
        @app.get("/metrics", include_in_schema=False)
        async def metrics() -> Response:
            return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)

    app.include_router(api_router)
    # Conditional: only publish JWKS when we are the issuer (self-signing).
    if isinstance(get_token_issuer(), JwksProvider):
        app.include_router(routes_jwks.router)
    return app


app = create_app()
