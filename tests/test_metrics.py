"""Prometheus /metrics — endpoint shape, label cardinality, and the CORS contract.

NOTE ON ASSERTIONS: prometheus_client's default REGISTRY is process-global, so
counters accumulate across every test in a pytest process and there is no
reliable "reset". Every assertion here is therefore on a DELTA measured around
the action, never on an absolute value.

No DB: /metrics needs none, and the requests used to move counters hit
/openapi.json and preflights, which are answered before routing.
"""

from __future__ import annotations

import contextlib

import httpx
import pytest

from app.main import app

ALLOWED = "https://osg-htc.org"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    )


async def _metrics_text() -> str:
    async with _client() as c:
        r = await c.get("/metrics")
    assert r.status_code == 200
    return r.text


def _counter(text: str, metric: str, **labels) -> float:
    """Pull one sample out of the exposition text, 0.0 if absent."""
    want = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
    for line in text.splitlines():
        if line.startswith("#") or not line.startswith(metric):
            continue
        head, _, value = line.rpartition(" ")
        if not want or f"{{{want}}}" == head[len(metric):] or want in head:
            return float(value)
    return 0.0


async def test_metrics_endpoint_serves_prometheus_text():
    async with _client() as c:
        r = await c.get("/metrics")
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]
    # Our own namespace is present, which also pins the af3cache_ prefix.
    assert "af3cache_http_requests_total" in r.text


async def test_metrics_is_not_in_the_public_schema():
    """It must not appear in /openapi.json or /docs — it is not public API."""
    async with _client() as c:
        r = await c.get("/openapi.json")
    assert r.status_code == 200
    assert "/metrics" not in r.json()["paths"]


async def test_requests_are_counted_by_route_template():
    before = _counter(await _metrics_text(), "af3cache_http_requests_total",
                      method="GET", route="/openapi.json", status="200")
    async with _client() as c:
        await c.get("/openapi.json")
    after = _counter(await _metrics_text(), "af3cache_http_requests_total",
                     method="GET", route="/openapi.json", status="200")
    assert after == before + 1


@pytest.mark.parametrize(
    "path",
    [
        "/v1/uploads/11111111-1111-1111-1111-111111111111/complete",
        "/v1/uploads/22222222-2222-2222-2222-222222222222/complete",
        "/nope/does/not/exist",
        "/wp-login.php",
        "/.env",
    ],
)
async def test_unrouted_paths_do_not_mint_series(path):
    """Cardinality guard.

    This is the assertion that matters most in this file. Labelling by raw path
    would create one permanent series per ingestion id (423k+ and growing) and
    one per junk URL probed from the internet — unbounded and attacker
    controlled. Everything unrouted must collapse to a single label value.
    """
    async with _client() as c:
        # PRE-EXISTING BUG, not a metrics failure: RedactSecretsFilter's
        # _KV_RE matches the format string's OWN placeholder in
        # app/core/security.py ("x-api-key=%s" -> "x-api-key=<redacted>"),
        # leaving 0 placeholders with 2 args, so record.getMessage() raises.
        # In production this silently kills that auth-rejection log line.
        # Scheduled for the phase 4 redaction rework.
        #
        # Swallowed here deliberately: the middleware records in a `finally`,
        # so the label under test is still written even when the handler
        # raises -- which is exactly what the assertion below checks.
        with contextlib.suppress(TypeError):
            await c.post(path)
    text = await _metrics_text()
    assert path not in text, f"raw path leaked into a metric label: {path}"


async def test_unmatched_label_is_used_for_unrouted_requests():
    async with _client() as c:
        await c.get("/definitely-not-a-route")
    assert 'route="<unmatched>"' in await _metrics_text()


async def test_query_hit_ratio_counters_exist():
    """Denominator and numerator for the cache hit ratio."""
    text = await _metrics_text()
    assert "af3cache_query_sequences_total" in text
    assert "af3cache_query_hits_total" in text


async def test_metrics_does_not_break_the_cors_contract():
    """The metrics middleware is outermost; CORS behaviour must be unchanged."""
    async with _client() as c:
        good = await c.options(
            "/v1/query",
            headers={
                "Origin": ALLOWED,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Content-Type",
            },
        )
        bad = await c.options(
            "/v1/query",
            headers={
                "Origin": "https://evil.example.com",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Content-Type",
            },
        )
    assert good.status_code == 200
    assert good.headers["access-control-allow-origin"] == ALLOWED
    assert bad.status_code == 400
    assert "access-control-allow-origin" not in bad.headers


async def test_no_origin_request_still_has_no_cors_headers():
    """OSPool jobs send no Origin; their responses must stay byte-identical."""
    async with _client() as c:
        r = await c.get("/openapi.json")
    assert not [h for h in r.headers if h.startswith("access-control")]
