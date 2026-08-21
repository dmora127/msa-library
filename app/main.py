"""FastAPI application factory."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import routes_jwks
from app.api.router import api_router
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging
from app.services.deps import get_token_issuer
from app.services.token_issuer import JwksProvider


def create_app() -> FastAPI:
    configure_logging()
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
    app.include_router(api_router)
    # Conditional: only publish JWKS when we are the issuer (self-signing).
    if isinstance(get_token_issuer(), JwksProvider):
        app.include_router(routes_jwks.router)
    return app


app = create_app()
