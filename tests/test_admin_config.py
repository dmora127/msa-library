"""Admin config + source management: auth, defaults, hot-update, no key
leakage, source enable/disable, validation."""

from __future__ import annotations

import pytest

ADMIN = {"X-API-Key": "admin-test-key"}
USER = {"X-API-Key": "test-key"}
SHA = "a" * 64
COMMUNITY = "Community Contributed - Cached"
EXPECTED_FIELDS = {
    "max_upload_size_bytes",
    "token_ttl_seconds",
    "osdf_namespace_template",
    "reap_grace_hours",
}


async def test_admin_requires_admin_key(client):
    assert (await client.get("/v1/admin/config")).status_code == 401
    # a valid *user* key is not an *admin* key
    assert (await client.get("/v1/admin/config", headers=USER)).status_code == 401
    assert (await client.get("/v1/admin/config", headers=ADMIN)).status_code == 200


async def test_get_defaults_and_no_key_leakage(client):
    r = await client.get("/v1/admin/config", headers=ADMIN)
    body = r.json()
    assert set(body) == EXPECTED_FIELDS  # nothing else, esp. no api keys
    assert body["max_upload_size_bytes"] == 104857600
    assert body["token_ttl_seconds"] == 1200
    assert body["reap_grace_hours"] == 12
    assert "{ingestion_id}" in body["osdf_namespace_template"]


async def test_put_lowers_limit_and_is_honored_by_upload_request(client, db_session):
    r = await client.put(
        "/v1/admin/config", json={"max_upload_size_bytes": 1000}, headers=ADMIN
    )
    assert r.status_code == 200 and r.json()["max_upload_size_bytes"] == 1000

    # the new limit takes effect immediately on the next upload-request
    over = await client.post(
        "/v1/uploads/request",
        json={"sequence": "MKTAYIAK", "db_version": "v1",
              "declared_size_bytes": 5000, "declared_sha256": SHA},
        headers=USER,
    )
    assert over.status_code == 413
    ok = await client.post(
        "/v1/uploads/request",
        json={"sequence": "MKTAYIAK", "db_version": "v1",
              "declared_size_bytes": 900, "declared_sha256": SHA},
        headers=USER,
    )
    assert ok.status_code == 200


async def test_list_sources_reflects_msa_sources(client):
    r = await client.get("/v1/admin/sources", headers=ADMIN)
    assert r.status_code == 200
    by_name = {s["name"]: s for s in r.json()}
    assert by_name[COMMUNITY]["enabled"] is True
    assert by_name[COMMUNITY]["idle_ttl_days"] == 90
    assert by_name["GDM"]["enabled"] is True and by_name["GDM"]["idle_ttl_days"] is None


async def test_disable_source_hides_from_query_and_blocks_upload(client, db_session):
    from sqlalchemy import text

    from app.core.hashing import sequence_hash

    # seed a community hit so we can watch it disappear from query
    sid = (
        await db_session.execute(
            text(
                "insert into public.msa_sequences (seq_hash, sequence, seq_length) "
                "values (:h,:s,:l) returning id"
            ),
            {"h": sequence_hash("MKDISABLESRC"), "s": "MKDISABLESRC", "l": 12},
        )
    ).scalar_one()
    await db_session.execute(
        text(
            "insert into public.msa_cache_entries "
            "(sequence_id, source, db_version, osdf_uri) "
            "values (:sid,:src,'v1','osdf:///x.a3m')"
        ),
        {"sid": sid, "src": COMMUNITY},
    )

    q1 = await client.post("/v1/query", json={"sequence": "MKDISABLESRC"}, headers=USER)
    assert q1.json()["results"][0]["found"] is True

    # disable the community source
    upd = await client.put(
        f"/v1/admin/sources/{COMMUNITY}", json={"enabled": False}, headers=ADMIN
    )
    assert upd.status_code == 200 and upd.json()["enabled"] is False

    # query no longer returns it; upload is refused
    q2 = await client.post("/v1/query", json={"sequence": "MKDISABLESRC"}, headers=USER)
    assert q2.json()["results"][0]["found"] is False
    up = await client.post(
        "/v1/uploads/request",
        json={"sequence": "MKNEW", "db_version": "v1",
              "declared_size_bytes": 10, "declared_sha256": SHA},
        headers=USER,
    )
    assert up.status_code == 422
    assert up.json()["error"]["code"] == "source_not_enabled"


async def test_update_unknown_source_404(client):
    r = await client.put(
        "/v1/admin/sources/Nope", json={"enabled": False}, headers=ADMIN
    )
    assert r.status_code == 404


@pytest.mark.parametrize(
    "payload",
    [
        {},  # nothing to update
        {"token_ttl_seconds": 0},  # gt=0
        {"max_upload_size_bytes": -1},
        {"reap_grace_hours": 0},
        {"osdf_namespace_template": "osdf:///no/placeholders.a3m"},
    ],
)
async def test_put_validation(client, payload):
    r = await client.put("/v1/admin/config", json=payload, headers=ADMIN)
    assert r.status_code == 422
