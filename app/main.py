"""FastAPI application factory."""

from __future__ import annotations

from fastapi import FastAPI

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
    app.include_router(api_router)
    # Conditional: only publish JWKS when we are the issuer (self-signing).
    if isinstance(get_token_issuer(), JwksProvider):
        app.include_router(routes_jwks.router)
    return app


app = create_app()
