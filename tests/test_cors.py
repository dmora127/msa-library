"""CORS — the OSG web front-ends call the public reads cross-origin.

CORS is owned by the app (app/main.py), NOT by the Caddyfile: Caddy's `header`
directive replaces the upstream value, so an edge block would silently widen
this allowlist. These tests pin the allowlist so that stays true.

No DB: preflights are answered by the middleware before routing, and the real
requests here hit /openapi.json.
"""

from __future__ import annotations

import httpx
import pytest

from app.main import app

ALLOWED = "https://osg-htc.org"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    )


async def _preflight(origin: str, headers: str = "Content-Type") -> httpx.Response:
    async with _client() as c:
        return await c.options(
            "/v1/query",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": headers,
            },
        )


@pytest.mark.parametrize(
    "origin",
    [
        "https://osg-htc.org",
        "https://www.osg-htc.org",
        "https://opensciencegrid.org",
        "https://www.opensciencegrid.org",
    ],
)
async def test_preflight_allows_each_osg_origin(origin):
    r = await _preflight(origin)
    assert r.status_code == 200
    # echoed back exactly — never "*", which would also cover every other site
    assert r.headers["access-control-allow-origin"] == origin


@pytest.mark.parametrize(
    "origin",
    [
        "https://evil.example.com",
        "http://osg-htc.org",              # scheme is part of the origin
        "https://osg-htc.org.evil.com",    # suffix trick, not a prefix match
    ],
)
async def test_preflight_refuses_other_origins(origin):
    r = await _preflight(origin)
    assert r.status_code == 400
    assert "access-control-allow-origin" not in r.headers


async def test_preflight_refuses_credentialed_headers():
    """/v1/uploads/* and /v1/admin/* are not browser-facing; keep it that way."""
    r = await _preflight(ALLOWED, headers="Content-Type, X-API-Key")
    assert r.status_code == 400


async def test_no_origin_means_no_cors_headers():
    """OSPool jobs send no Origin — their responses must be byte-identical."""
    async with _client() as c:
        r = await c.get("/openapi.json")
    assert r.status_code == 200
    assert not [h for h in r.headers if h.startswith("access-control")]


async def test_allow_credentials_is_off():
    """Allow-Origin is echoed per-origin, but no cookies/credentials ride along."""
    async with _client() as c:
        r = await c.get("/openapi.json", headers={"Origin": ALLOWED})
    assert r.headers["access-control-allow-origin"] == ALLOWED
    assert "access-control-allow-credentials" not in r.headers
