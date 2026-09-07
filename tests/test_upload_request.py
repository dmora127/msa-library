"""Upload-request: auth, happy path, oversize-before-token, source gate,
idempotency, reject-unless-stale, db_version identity, schema validation."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.core.hashing import sequence_hash
from tests.conftest import NS_ISSUER

KEY = {"X-API-Key": "test-key"}
NS_TEMPLATE = "osdf:///{namespace}/community-contributed/{seq_hash}/{source_slug}/{ingestion_id}.a3m"
SHA = "a" * 64
DV = "uniref-2026-01"


async def _count(db_session) -> int:
    r = await db_session.execute(text("select count(*) from public.msa_ingestions"))
    return int(r.scalar_one())


async def test_request_requires_api_key(client):
    r = await client.post("/v1/uploads/request", json={"sequence": "MK"})
    assert r.status_code == 401


async def test_request_happy_path(client, db_session):
    body = {
        "sequence": "mkt ayiakqr",
        "db_version": DV,
        "declared_size_bytes": 500,
        "declared_sha256": SHA,
    }
    r = await client.post("/v1/uploads/request", json=body, headers=KEY)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["write_token"].startswith("stub.")
    assert j["token_type"] == "Bearer"
    assert j["max_upload_size_bytes"] == 104857600
    h = sequence_hash("MKTAYIAKQR")
    # immutable, unique object at the flat community-contributed namespace
    assert j["osdf_uri"] == (
        f"osdf:///af3-msa-library/community-contributed/{j['ingestion_id']}.a3m"
    )
    row = (
        await db_session.execute(
            text(
                "select status, seq_hash, source, db_version, declared_sha256, "
                "token_ref from public.msa_ingestions where id = :id"
            ),
            {"id": j["ingestion_id"]},
        )
    ).one()
    assert row.status == "pending"
    assert row.seq_hash == h
    assert row.source == "Community Contributed - Cached"
    assert row.db_version == DV
    assert row.declared_sha256 == SHA
    assert row.token_ref  # bound, but the JWT itself is never stored


async def test_request_oversize_rejected_before_token(client, db_session):
    body = {
        "sequence": "MKTAYIAK",
        "db_version": DV,
        "declared_size_bytes": 999_999_999_999,
        "declared_sha256": SHA,
    }
    r = await client.post("/v1/uploads/request", json=body, headers=KEY)
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "upload_too_large"
    assert await _count(db_session) == 0  # no pending row, no token minted


async def test_request_disabled_source_rejected(client, db_session):
    body = {
        "sequence": "MKTAYIAK",
        "source": "GDM",  # jobs may only contribute to the community source
        "db_version": DV,
        "declared_size_bytes": 100,
        "declared_sha256": SHA,
    }
    r = await client.post("/v1/uploads/request", json=body, headers=KEY)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "source_not_enabled"
    assert await _count(db_session) == 0


async def test_request_idempotent_reuses_inflight_row(client, db_session):
    body = {
        "sequence": "QRSTVWY", "db_version": DV,
        "declared_size_bytes": 10, "declared_sha256": SHA,
    }
    r1 = await client.post("/v1/uploads/request", json=body, headers=KEY)
    r2 = await client.post("/v1/uploads/request", json=body, headers=KEY)
    assert r1.json()["ingestion_id"] == r2.json()["ingestion_id"]
    assert r1.json()["osdf_uri"] == r2.json()["osdf_uri"]  # same object
    assert await _count(db_session) == 1
    assert r1.json()["write_token"] != r2.json()["write_token"]  # token refreshed


async def test_request_distinct_db_versions_are_separate_ingestions(client, db_session):
    base = {"sequence": "MKDBVER", "declared_size_bytes": 10, "declared_sha256": SHA}
    a = await client.post(
        "/v1/uploads/request", json={**base, "db_version": "v1"}, headers=KEY
    )
    b = await client.post(
        "/v1/uploads/request", json={**base, "db_version": "v2"}, headers=KEY
    )
    assert a.json()["ingestion_id"] != b.json()["ingestion_id"]
    assert await _count(db_session) == 2  # different db_version = different upload


async def test_request_rejected_when_live_entry_exists(client, db_session):
    # an active, non-idle cache entry for this (seq,source,db_version)
    sid = (
        await db_session.execute(
            text(
                "insert into public.msa_sequences (seq_hash, sequence, seq_length) "
                "values (:h,:s,:l) returning id"
            ),
            {"h": sequence_hash("MKLIVECACHE"), "s": "MKLIVECACHE", "l": 11},
        )
    ).scalar_one()
    await db_session.execute(
        text(
            "insert into public.msa_cache_entries "
            "(sequence_id, source, db_version, osdf_uri) "
            "values (:sid,'Community Contributed - Cached',:dv,'osdf:///live.a3m')"
        ),
        {"sid": sid, "dv": DV},
    )
    r = await client.post(
        "/v1/uploads/request",
        json={"sequence": "MKLIVECACHE", "db_version": DV,
              "declared_size_bytes": 10, "declared_sha256": SHA},
        headers=KEY,
    )
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "already_cached"
    assert err["osdf_uri"] == "osdf:///live.a3m"  # job uses the cache instead
    assert await _count(db_session) == 0  # no new ingestion


@pytest.mark.parametrize(
    "payload",
    [
        {"db_version": DV, "declared_size_bytes": 1, "declared_sha256": SHA},
        {"sequence": "MK", "seq_hash": "a" * 64, "db_version": DV,
         "declared_size_bytes": 1, "declared_sha256": SHA},
        {"seq_hash": "a" * 64, "db_version": DV, "declared_size_bytes": 1,
         "declared_sha256": SHA},  # seq_hash without seq_length
        {"sequence": "MK", "db_version": DV, "declared_size_bytes": 1,
         "declared_sha256": "nothex"},
        {"sequence": "MK", "db_version": DV, "declared_size_bytes": 0,
         "declared_sha256": SHA},  # gt=0
        {"sequence": "MK", "declared_size_bytes": 1,
         "declared_sha256": SHA},  # missing db_version
    ],
)
async def test_request_validation(client, payload):
    r = await client.post("/v1/uploads/request", json=payload, headers=KEY)
    assert r.status_code == 422


# --- namespace registry + capacity gate ---------------------------------------


async def _set_template(db_session, template: str) -> None:
    from app.core.config import get_settings
    from app.services.settings_service import SettingsService

    await SettingsService(db_session, get_settings()).set(
        "osdf_namespace_template", template, "test"
    )


async def _seed_usage(db_session, ns: str, declared: int) -> None:
    import uuid

    await db_session.execute(
        text(
            """
            insert into public.msa_ingestions
              (status, seq_hash, seq_length, source, db_version,
               declared_size_bytes, declared_sha256, osdf_uri, namespace,
               token_ref, token_expires_at)
            values ('pending', :h, 5, 'Community Contributed - Cached', 'v1',
                    :d, :sha, :u, :ns, :ref, now())
            """
        ),
        {"h": uuid.uuid4().hex * 2, "d": declared, "sha": "d" * 64,
         "u": f"osdf:///{ns}/community-contributed/{uuid.uuid4()}.a3m",
         "ns": ns, "ref": uuid.uuid4().hex},
    )


def _req(seq: str, **over) -> dict:
    body = {"sequence": seq, "db_version": DV, "declared_size_bytes": 10,
            "declared_sha256": SHA}
    body.update(over)
    return body


async def test_request_populates_namespace_column(client, db_session):
    # default registry in tests is empty and the default template hardcodes
    # the store -> the column is derived from the URI only when the registry
    # knows the namespace; here it does not, so NULL.
    r = await client.post("/v1/uploads/request", json=_req("MKNSCOL"), headers=KEY)
    ns = (
        await db_session.execute(
            text("select namespace from public.msa_ingestions where id = :id"),
            {"id": r.json()["ingestion_id"]},
        )
    ).scalar_one()
    assert ns is None


async def test_request_picks_only_enabled_namespaces(client, db_session, registry_override):
    registry_override([
        {"namespace": "af3-msa-library", "issuer": NS_ISSUER, "capacity_tb": 1},
        {"namespace": "drained", "issuer": NS_ISSUER, "capacity_tb": 1, "enabled": False},
    ])
    await _set_template(db_session, NS_TEMPLATE)
    seen = set()
    for i in range(20):
        r = await client.post("/v1/uploads/request", json=_req(f"MKPICK{i:02d}"), headers=KEY)
        assert r.status_code == 200, r.text
        uri = r.json()["osdf_uri"]
        seen.add(uri.split("/")[3])
        ns = (
            await db_session.execute(
                text("select namespace from public.msa_ingestions where id = :id"),
                {"id": r.json()["ingestion_id"]},
            )
        ).scalar_one()
        assert ns == "af3-msa-library"
    assert seen == {"af3-msa-library"}


async def test_request_skips_full_namespace(client, db_session, registry_override):
    registry_override([
        {"namespace": "af3-msa-library", "issuer": NS_ISSUER, "capacity_tb": 1},
        {"namespace": "west", "issuer": NS_ISSUER, "capacity_tb": 1},
    ])
    await _set_template(db_session, NS_TEMPLATE)
    await _seed_usage(db_session, "af3-msa-library", 950_000_000_000)  # 5% left
    for i in range(10):
        r = await client.post("/v1/uploads/request", json=_req(f"MKFULL{i:02d}"), headers=KEY)
        assert r.status_code == 200, r.text
        assert r.json()["osdf_uri"].startswith("osdf:///west/")


async def test_request_all_full_is_507(client, db_session, registry_override):
    registry_override([
        {"namespace": "af3-msa-library", "issuer": NS_ISSUER, "capacity_tb": 1},
    ])
    await _set_template(db_session, NS_TEMPLATE)
    await _seed_usage(db_session, "af3-msa-library", 950_000_000_000)
    before = await _count(db_session)
    r = await client.post("/v1/uploads/request", json=_req("MKALLFULL"), headers=KEY)
    assert r.status_code == 507, r.text
    err = r.json()["error"]
    assert err["code"] == "namespace_full"
    assert err["namespaces"]["af3-msa-library"]["used_bytes"] == 950_000_000_000
    assert err["namespaces"]["af3-msa-library"]["headroom_pct"] == 5.0
    assert await _count(db_session) == before  # no row, no token


async def test_request_hardcoded_template_is_gated_too(client, db_session, registry_override):
    # default template hardcodes af3-msa-library: the gate keys on the URI's
    # namespace, so a full store still refuses even without {namespace}
    registry_override([
        {"namespace": "af3-msa-library", "issuer": NS_ISSUER, "capacity_tb": 1},
    ])
    await _seed_usage(db_session, "af3-msa-library", 950_000_000_000)
    r = await client.post("/v1/uploads/request", json=_req("MKHARDFULL"), headers=KEY)
    assert r.status_code == 507
    assert r.json()["error"]["code"] == "namespace_full"


async def test_rerequest_in_full_namespace_still_succeeds(client, db_session, registry_override):
    registry_override([
        {"namespace": "af3-msa-library", "issuer": NS_ISSUER, "capacity_tb": 1},
    ], ttl_seconds=0)  # no snapshot cache: usage seeded mid-test must be seen
    r1 = await client.post("/v1/uploads/request", json=_req("MKREREQ"), headers=KEY)
    assert r1.status_code == 200
    await _seed_usage(db_session, "af3-msa-library", 950_000_000_000)
    # fresh sequence -> refused ...
    r_new = await client.post("/v1/uploads/request", json=_req("MKREREQ2"), headers=KEY)
    assert r_new.status_code == 507
    # ... but the existing pending row re-mints (its bytes are already counted)
    r2 = await client.post("/v1/uploads/request", json=_req("MKREREQ"), headers=KEY)
    assert r2.status_code == 200, r2.text
    assert r2.json()["ingestion_id"] == r1.json()["ingestion_id"]
    assert r2.json()["osdf_uri"] == r1.json()["osdf_uri"]


async def test_request_template_needs_namespace_but_none_configured_503(client, db_session):
    # empty registry (default in tests) + a {namespace} template
    await _set_template(db_session, NS_TEMPLATE)
    r = await client.post("/v1/uploads/request", json=_req("MKNONS"), headers=KEY)
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "token_issuance_failed"
