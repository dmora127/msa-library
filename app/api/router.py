"""Top-level router aggregation.

Routers are added per build phase. JWKS is mounted conditionally by the app
factory (only when the active TokenIssuer publishes a key set).
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api import (
    routes_admin,
    routes_health,
    routes_ingestion,
    routes_query,
    routes_stats,
)

api_router = APIRouter()
api_router.include_router(routes_health.router)
api_router.include_router(routes_stats.router)  # public, no auth
api_router.include_router(routes_query.router)
api_router.include_router(routes_ingestion.router)
api_router.include_router(routes_admin.router)
