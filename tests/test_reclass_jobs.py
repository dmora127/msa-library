"""/v1/admin/classification/jobs — tracking of remote NR BLAST jobs.

The PUT payloads below are the verbatim shapes `reclass-status` prints on the
BLAST box for each state (as supplied by the operator), so the mapping into
reclass_jobs columns is pinned against the real contract.
"""

from __future__ import annotations

import pytest

from tests.test_reclassify_external import ADMIN, USER

JOBS = "/v1/admin/classification/jobs"

QUEUED = {
    "job": "20260905T071245Z-64839f", "source": "nightly_2026-09-05.fa", "bytes": 7400,
    "records": 40, "submitted_at": "2026-09-05T07:12:45+00:00", "status": "QUEUED",
    "input_retained": True, "log_tail": [],
}
RUNNING = {
    "job": "20260905T071354Z-3880bf", "source": "live_batch.fa", "bytes": 7400, "records": 40,
    "submitted_at": "2026-09-05T07:13:54+00:00", "status": "RUNNING",
    "started_at": "2026-09-05T07:13:54+00:00",
    "results": "/media/volume/mmseq2-data/runs/20260905T071354Z-3880bf/results.tsv",
    "posted_so_far": 0, "input_retained": True,
    "log_tail": ["[2026-09-05T07:13:54+00:00] stage 1: exact match",
                 "  [exact] scanning NR with 12 workers"],
}
DONE = {
    "job": "20260905T071245Z-64839f", "source": "nightly_2026-09-05.fa", "bytes": 7400,
    "records": 40, "submitted_at": "2026-09-05T07:12:45+00:00", "status": "DONE",
    "started_at": "2026-09-05T07:13:44+00:00", "finished_at": "2026-09-05T07:13:46+00:00",
    "exit_code": 0,
    "results": "/media/volume/mmseq2-data/runs/20260905T071245Z-64839f/results.tsv.gz",
    "posted_so_far": 40, "input_retained": False,
    "summary": {"rows": 40, "status_200": 40},
    "log_tail": ["[2026-09-05T07:13:46+00:00] DONE. results -> .../results.tsv"],
}
FAILED = {
    "job": "20260905T071348Z-4fe841", "source": "other_batch.fa", "bytes": 7888, "records": 40,
    "submitted_at": "2026-09-05T07:13:48+00:00", "status": "FAILED",
    "started_at": "2026-09-05T07:13:48+00:00", "finished_at": "2026-09-05T07:13:49+00:00",
    "exit_code": 4, "results": "/media/volume/mmseq2-data/runs/x/results.tsv",
    "posted_so_far": 0, "input_retained": True, "summary": {"rows": 0},
    "log_tail": ["ERROR: .../ex_all does not match the current .../input.fa",
                 "  Unset REUSE_EXACT to recompute stage 1 from scratch."],
}
KILLED = {**RUNNING, "status": "KILLED", "finished_at": "2026-09-05T07:14:04+00:00",
          "exit_code": 143}
UNKNOWN = {"job": "20260905T999999Z-deadbe", "status": "UNKNOWN", "error": "no such job"}


async def _create(client, job_id: str, **kw):
    body = {"job_id": job_id, "input_name": f"pending-{job_id}.fa", "sequence_count": 40,
            "input_bytes": 7400}
    body.update(kw)
    r = await client.post(JOBS, json=body, headers=ADMIN)
    assert r.status_code == 201, r.text
    return r.json()


async def test_requires_admin_key(client):
    for method, path, kw in (
        ("post", JOBS, {"json": {"job_id": "j", "input_name": "f", "sequence_count": 1}}),
        ("get", JOBS, {}),
        ("get", f"{JOBS}/j", {}),
        ("put", f"{JOBS}/j/status", {"json": QUEUED}),
    ):
        assert (await getattr(client, method)(path, **kw)).status_code == 401
        assert (await getattr(client, method)(path, headers=USER, **kw)).status_code == 401


async def test_create_and_duplicate(client):
    row = await _create(client, "20260905T071245Z-64839f", residue_count=5000)
    assert row["status"] == "QUEUED" and row["poll_count"] == 0
    assert row["sequence_count"] == 40 and row["residue_count"] == 5000
    assert row["input_name"] == "pending-20260905T071245Z-64839f.fa"
    assert row["summary"] is None and row["last_status"] is None and row["created_at"]
    r = await client.post(JOBS, json={"job_id": "20260905T071245Z-64839f",
                                      "input_name": "x.fa", "sequence_count": 1}, headers=ADMIN)
    assert r.status_code == 409 and r.json()["error"]["code"] == "reclass_job_exists"
    r = await client.post(JOBS, json={"job_id": "x", "input_name": "x.fa",
                                      "sequence_count": 1, "bogus": 1}, headers=ADMIN)
    assert r.status_code == 422


async def test_get_and_404(client):
    await _create(client, "j1")
    assert (await client.get(f"{JOBS}/j1", headers=ADMIN)).json()["job_id"] == "j1"
    r = await client.get(f"{JOBS}/nope", headers=ADMIN)
    assert r.status_code == 404 and r.json()["error"]["code"] == "reclass_job_not_found"
    r = await client.put(f"{JOBS}/nope/status", json=QUEUED, headers=ADMIN)
    assert r.status_code == 404


@pytest.mark.parametrize("payload,expect", [
    (QUEUED, dict(status="QUEUED", exit_code=None, posted_so_far=None, summary=None,
                  last_error=None, started="", finished="")),
    (RUNNING, dict(status="RUNNING", exit_code=None, posted_so_far=0, summary=None,
                   last_error=None, started="2026-09-05T07:13:54", finished="")),
    (DONE, dict(status="DONE", exit_code=0, posted_so_far=40,
                summary={"rows": 40, "status_200": 40}, last_error=None,
                started="2026-09-05T07:13:44", finished="2026-09-05T07:13:46")),
    (FAILED, dict(status="FAILED", exit_code=4, posted_so_far=0, summary={"rows": 0},
                  last_error=None, started="2026-09-05T07:13:48", finished="2026-09-05T07:13:49")),
    (KILLED, dict(status="KILLED", exit_code=143, posted_so_far=0, summary=None,
                  last_error=None, started="2026-09-05T07:13:54", finished="2026-09-05T07:14:04")),
    (UNKNOWN, dict(status="UNKNOWN", exit_code=None, posted_so_far=None, summary=None,
                   last_error="no such job", started="", finished="")),
])
async def test_status_update_maps_every_remote_shape(client, payload, expect):
    await _create(client, "j-shape")
    r = await client.put(f"{JOBS}/j-shape/status", json=payload, headers=ADMIN)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["status"] == expect["status"]
    assert j["exit_code"] == expect["exit_code"]
    assert j["posted_so_far"] == expect["posted_so_far"]
    assert j["summary"] == expect["summary"]
    assert j["last_error"] == expect["last_error"]
    assert (j["remote_started_at"] or "").startswith(expect["started"])
    assert (j["remote_finished_at"] or "").startswith(expect["finished"])
    if payload.get("submitted_at"):
        assert j["remote_submitted_at"].startswith("2026-09-05T07:1")
        assert j["remote_submitted_at"].endswith("+00:00") or j["remote_submitted_at"].endswith("Z")
    # the whole remote payload is kept verbatim (incl. keys we don't type)
    assert j["last_status"]["status"] == expect["status"]
    for k in ("log_tail", "input_retained", "results", "records"):
        if k in payload:
            assert j["last_status"][k] == payload[k]
    assert j["poll_count"] == 1 and j["last_polled_at"]


async def test_polling_lifecycle_and_active_list(client):
    await _create(client, "old-done")
    await client.put(f"{JOBS}/old-done/status", json=DONE, headers=ADMIN)
    await _create(client, "j-a")
    await _create(client, "j-b")
    active = (await client.get(JOBS, params={"active": "true"}, headers=ADMIN)).json()
    assert [j["job_id"] for j in active] == ["j-b", "j-a"]  # newest first, DONE excluded
    everything = (await client.get(JOBS, headers=ADMIN)).json()
    assert {j["job_id"] for j in everything} == {"old-done", "j-a", "j-b"}

    # queued -> running -> running -> done: counters and coalescing
    await client.put(f"{JOBS}/j-a/status", json=QUEUED, headers=ADMIN)
    r = await client.put(f"{JOBS}/j-a/status", json={**RUNNING, "posted_so_far": 12},
                         headers=ADMIN)
    assert r.json()["posted_so_far"] == 12 and r.json()["poll_count"] == 2
    r = await client.put(f"{JOBS}/j-a/status", json=DONE, headers=ADMIN)
    j = r.json()
    assert j["status"] == "DONE" and j["poll_count"] == 3 and j["posted_so_far"] == 40
    assert j["remote_started_at"].startswith("2026-09-05T07:13:44")  # remote value wins
    active = (await client.get(JOBS, params={"active": "true"}, headers=ADMIN)).json()
    assert [x["job_id"] for x in active] == ["j-b"]
    # status is case/space-normalised; junk is rejected
    r = await client.put(f"{JOBS}/j-b/status", json={"status": " running "}, headers=ADMIN)
    assert r.status_code == 200 and r.json()["status"] == "RUNNING"
    r = await client.put(f"{JOBS}/j-b/status", json={"status": "EXPLODED"}, headers=ADMIN)
    assert r.status_code == 422
    r = await client.put(f"{JOBS}/j-b/status", json={"job": "x"}, headers=ADMIN)
    assert r.status_code == 422
