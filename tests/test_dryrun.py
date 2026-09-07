"""Simulation mode: dryrun token issuer + OSDF client log the pelican
commands they WOULD run and never execute; complete_upload publishes trusting
declared metadata."""

from __future__ import annotations

import logging

import pytest
from sqlalchemy import text

from app.core.config import Settings
from app.core.errors import OsdfUnavailable
from app.core.hashing import sequence_hash
from app.core.namespaces import NamespaceConfigError
from app.services.osdf_client import DryRunOsdfClient, build_osdf_client
from app.services.token_issuer import (
    JwksProvider,
    LoggingDryRunTokenIssuer,
    build_token_issuer,
)
from tests.conftest import NS_ISSUER

KEY = {"X-API-Key": "test-key"}
SHA = "a" * 64


async def test_dryrun_token_issuer_logs_command_and_returns_synthetic(
    caplog, namespaces_yaml
):
    issuer = LoggingDryRunTokenIssuer(
        Settings(token_issuer_impl="dryrun", pelican_namespaces_file=str(namespaces_yaml()))
    )
    uri = "osdf:///af3-msa-library/community-contributed/iid.a3m"
    with caplog.at_level(logging.INFO, logger="af3cache.tokens"):
        tok = await issuer.issue(subject="job", object_uri=uri, ttl_seconds=1200)
    assert tok.token.startswith("SIMULATED-TOKEN.")
    assert tok.token_ref and tok.claim_jti and tok.token_ref != tok.claim_jti
    assert not isinstance(issuer, JwksProvider)
    key = namespaces_yaml.key("af3-msa-library")
    assert any(
        f"pelican token create {uri} --write --issuer {NS_ISSUER} -k {key} "
        "--scope-path /community-contributed/iid.a3m --lifetime 1200"
        in r.message
        for r in caplog.records
    )


async def test_dryrun_without_file_uses_placeholders(caplog, tmp_path):
    # the "start on dryrun before the credential exists" workflow: no file, no raise
    issuer = LoggingDryRunTokenIssuer(
        Settings(token_issuer_impl="dryrun",
                 pelican_namespaces_file=str(tmp_path / "absent.yaml"))
    )
    with caplog.at_level(logging.INFO, logger="af3cache.tokens"):
        tok = await issuer.issue(
            subject="job", object_uri="osdf:///af3-msa-library/x.a3m", ttl_seconds=5
        )
    assert tok.token.startswith("SIMULATED-TOKEN.")
    msg = next(r.message for r in caplog.records if "would run" in r.message)
    assert "--scope-path <SCOPE_PATH>" in msg and "<PRIVATE_KEY>" in msg


def test_dryrun_invalid_file_raises(tmp_path):
    bad = tmp_path / "ns.yaml"
    bad.write_text("namespaces: [")
    with pytest.raises(NamespaceConfigError):
        LoggingDryRunTokenIssuer(
            Settings(token_issuer_impl="dryrun", pelican_namespaces_file=str(bad))
        )


def test_dryrun_dispatch():
    assert isinstance(
        build_token_issuer(Settings(token_issuer_impl="dryrun")),
        LoggingDryRunTokenIssuer,
    )
    c = build_osdf_client("dryrun")
    assert isinstance(c, DryRunOsdfClient) and c.is_simulated is True


async def test_dryrun_osdf_delete_logs_and_fetch_raises(caplog):
    c = DryRunOsdfClient()
    with caplog.at_level(logging.INFO, logger="af3cache.osdf"):
        await c.delete("pelican://osgaf3_store1/x/iid.a3m")
    assert any(
        "would run: pelican object rm pelican://osgaf3_store1/x/iid.a3m"
        in r.message
        for r in caplog.records
    )
    with pytest.raises(OsdfUnavailable):
        await c.fetch("pelican://x", max_bytes=10)


@pytest.fixture
def dryrun_osdf():
    from app.main import app
    from app.services.deps import get_osdf_client

    c = DryRunOsdfClient()
    app.dependency_overrides[get_osdf_client] = lambda: c
    yield c
    app.dependency_overrides.pop(get_osdf_client, None)


async def test_simulate_complete_publishes_and_is_queryable(
    client, db_session, dryrun_osdf
):
    req = await client.post(
        "/v1/uploads/request",
        json={"sequence": "MKSIMSEQ", "db_version": "v1",
              "declared_size_bytes": 321, "declared_sha256": SHA},
        headers=KEY,
    )
    iid = req.json()["ingestion_id"]
    r = await client.post(f"/v1/uploads/{iid}/complete", headers=KEY)
    assert r.json()["status"] == "available"

    row = (
        await db_session.execute(
            text(
                "select status, observed_sha256, observed_size_bytes, "
                "cache_entry_id from public.msa_ingestions where id=:i"
            ),
            {"i": iid},
        )
    ).one()
    assert row.status == "available"
    assert row.observed_sha256 == SHA  # declared trusted (simulation)
    assert row.observed_size_bytes == 321
    assert row.cache_entry_id is not None

    q = await client.post("/v1/query", json={"sequence": "MKSIMSEQ"}, headers=KEY)
    hit = q.json()["results"][0]["hits"][0]
    assert hit["a3m_sha256"] == SHA and hit["db_version"] == "v1"
    assert hit["n_sequences"] is None  # unknown without a real a3m


async def test_simulate_seq_hash_only_left_uploaded_not_published(
    client, db_session, dryrun_osdf
):
    h = sequence_hash("MKNOTEXTSEQ")
    req = await client.post(
        "/v1/uploads/request",
        json={"seq_hash": h, "seq_length": 11, "db_version": "v1",
              "declared_size_bytes": 10, "declared_sha256": SHA},
        headers=KEY,
    )
    iid = req.json()["ingestion_id"]
    r = await client.post(f"/v1/uploads/{iid}/complete", headers=KEY)
    assert r.json()["status"] == "uploaded"  # can't publish w/o sequence text
    n = (
        await db_session.execute(
            text("select count(*) from public.msa_cache_entries")
        )
    ).scalar_one()
    assert n == 0
