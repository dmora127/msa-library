"""/v1/admin/similarity: auth, flag gating, status, builds through HTTP."""

from __future__ import annotations

import asyncio

import pytest

from app.services.deps import get_similarity_runtime
from app.services.similarity.service import SimilarityRuntime
from tests.similarity_fakes import fake_mmseqs, make_config
from tests.test_query import _seed_entry, _seed_sequence

ADMIN = {"X-API-Key": "admin-test-key"}
USER = {"X-API-Key": "test-key"}


@pytest.fixture
def runtime_factory(tmp_path):
    from app.main import app

    def _make(**overrides) -> SimilarityRuntime:
        binary, _ = fake_mmseqs(tmp_path, **overrides.pop("fake", {}))
        rt = SimilarityRuntime(make_config(binary, tmp_path, **overrides))
        app.dependency_overrides[get_similarity_runtime] = lambda: rt
        return rt

    yield _make
    app.dependency_overrides.pop(get_similarity_runtime, None)


async def _seed_active(db_session):
    h, sid = await _seed_sequence(db_session, "MKTAYIAKQR")
    await _seed_entry(db_session, sid, "OSG-Generated", "osdf:///a.a3m")


@pytest.mark.parametrize("path,method", [
    ("/v1/admin/similarity/status", "get"),
    ("/v1/admin/similarity/nightly-build", "post"),
    ("/v1/admin/similarity/delta-refresh", "post"),
])
async def test_requires_admin_key(client, runtime_factory, path, method):
    runtime_factory()
    assert (await getattr(client, method)(path)).status_code == 401
    assert (await getattr(client, method)(path, headers=USER)).status_code == 401


async def test_build_endpoints_409_while_flags_false(client, runtime_factory):
    runtime_factory()  # defaults: both builders disabled
    for path in ("/v1/admin/similarity/nightly-build", "/v1/admin/similarity/delta-refresh"):
        r = await client.post(path, headers=ADMIN)
        assert r.status_code == 409, r.text
        assert r.json()["error"]["code"] == "similarity_build_disabled"


async def test_status_before_and_after_a_nightly(client, db_session, runtime_factory):
    await _seed_active(db_session)
    rt = runtime_factory(nightly_build_enabled=True, delta_build_enabled=True)
    r = await client.get("/v1/admin/similarity/status", headers=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["db_mode"] == "ephemeral" and body["search_enabled"] is True
    assert body["nightly"] is None and body["delta"] is None
    assert body["mmseqs_version"] == "18-8cc5c" and body["build_in_progress"] is False
    assert body["thresholds"]["min_percent_identity"] == 90.0
    assert body["thresholds"]["max_hits"] == 5

    r = await client.post("/v1/admin/similarity/nightly-build", headers=ADMIN)
    assert r.status_code == 200, r.text
    built = r.json()
    assert built["manifest"]["build_type"] == "nightly"
    assert built["manifest"]["sequence_count"] == 1 and built["retired"] == []
    assert built["manifest"]["generation_path"].startswith(str(rt.cfg.db_root))

    r = await client.post("/v1/admin/similarity/delta-refresh", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["manifest"]["build_type"] == "delta"
    assert r.json()["manifest"]["base_nightly_generation"] == built["manifest"]["generation"]

    r = await client.get("/v1/admin/similarity/status", headers=ADMIN)
    st = r.json()
    assert st["nightly"]["generation"] == built["manifest"]["generation"]
    assert st["delta"]["sequence_count"] == 0


async def test_build_failure_is_500_and_leaves_no_generation(client, db_session, runtime_factory):
    await _seed_active(db_session)
    rt = runtime_factory(nightly_build_enabled=True, fake={"fail_step": "createdb"})
    r = await client.post("/v1/admin/similarity/nightly-build", headers=ADMIN)
    assert r.status_code == 500, r.text
    assert r.json()["error"]["code"] == "similarity_build_failed"
    assert not (rt.cfg.db_root / "nightly-buildDB").exists()


async def test_concurrent_build_is_409_in_progress(client, db_session, runtime_factory):
    await _seed_active(db_session)
    rt = runtime_factory(nightly_build_enabled=True)
    # hold the lock as a running build would
    await rt.build_lock.acquire()
    try:
        r = await client.post("/v1/admin/similarity/nightly-build", headers=ADMIN)
        assert r.status_code == 409 and r.json()["error"]["code"] == "similarity_build_in_progress"
        r = await client.get("/v1/admin/similarity/status", headers=ADMIN)
        assert r.json()["build_in_progress"] is True
    finally:
        rt.build_lock.release()
    await asyncio.sleep(0)
