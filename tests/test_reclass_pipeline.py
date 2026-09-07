"""API-driven NR-BLAST hand-off: POST /jobs/submit, /jobs/poll, /jobs/{id}/poll.

ssh and scp are replaced by shell scripts that log their argv, copy uploads
into a directory, and answer `reclass-submit` / `reclass-status` the way the
box does (incl. UNKNOWN with exit 2). They run through the real
asyncio.create_subprocess_exec path.
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.deps import get_reclass_runtime
from app.services.reclass_pipeline import ReclassRuntime
from app.services.reclass_remote import RemoteReclass
from tests.test_classification_export import _pending, _seq
from tests.test_reclass_jobs import DONE, RUNNING
from tests.test_reclassify_external import ADMIN, USER

JOBS = "/v1/admin/classification/jobs"
JOB_ID = "20260905T120000Z-abc123"

_SSH = """#!/usr/bin/env bash
printf 'ssh %s\\n' "$*" >> __LOG__
cmd="${@: -1}"
case "$cmd" in
  *reclass-submit*) __SUBMIT__ ;;
  *reclass-status*) job="${cmd##* }"
     __STATUS__ ;;
  *) echo "unexpected remote command: $cmd" >&2; exit 3 ;;
esac
"""
_SCP = """#!/usr/bin/env bash
printf 'scp %s\\n' "$*" >> __LOG__
__SCP__
"""


def fake_remote(
    tmp_path: Path,
    *,
    job_id: str = JOB_ID,
    statuses: dict[str, dict] | None = None,
    fail: str | None = None,
    submit_output: str | None = None,
) -> SimpleNamespace:
    log = tmp_path / "remote.log"
    uploads = tmp_path / "uploads"
    uploads.mkdir(exist_ok=True)
    status_dir = tmp_path / "status"
    status_dir.mkdir(exist_ok=True)
    for jid, payload in (statuses or {}).items():
        (status_dir / f"{jid}.json").write_text(json.dumps(payload))
    if fail == "submit":
        submit = 'echo "fake submit failure" >&2; exit 1'
    else:
        out = submit_output if submit_output is not None else job_id
        submit = f"printf '%s\\n' {shlex.quote(out)}"
    if fail == "status":
        status = 'echo "ssh: connect to host box port 22: Connection refused" >&2; exit 255'
    else:
        status = (
            f'if [ -f "{status_dir}/$job.json" ]; then cat "{status_dir}/$job.json"; '
            'else printf \'{"job": "%s", "status": "UNKNOWN", "error": "no such job"}\\n\' "$job"; '
            "exit 2; fi"
        )
    if fail == "scp":
        scp = 'echo "fake scp failure" >&2; exit 1'
    else:
        scp = f'src="${{@: -2:1}}"; cp "$src" "{uploads}/"'
    ssh_p = tmp_path / "ssh"
    ssh_script = _SSH.replace("__LOG__", str(log)).replace("__SUBMIT__", submit)
    ssh_p.write_text(ssh_script.replace("__STATUS__", status))
    scp_p = tmp_path / "scp"
    scp_p.write_text(_SCP.replace("__LOG__", str(log)).replace("__SCP__", scp))
    ssh_p.chmod(0o755)
    scp_p.chmod(0o755)
    return SimpleNamespace(
        ssh=str(ssh_p), scp=str(scp_p), log=log, uploads=uploads, status_dir=status_dir
    )


def _log_lines(f) -> list[str]:
    return f.log.read_text().splitlines() if f.log.exists() else []


@pytest.fixture
def remote(tmp_path):
    from app.main import app

    def _make(**kw) -> SimpleNamespace:
        f = fake_remote(tmp_path, **kw)
        key = tmp_path / "reclass_ed25519"
        key.write_text("not-a-real-key\n")
        kh = tmp_path / "known_hosts"
        kh.write_text("")
        rr = RemoteReclass(
            remote="exouser@box", data_dir="/media/volume/mmseq2-data",
            submit_cmd="/home/exouser/.local/bin/reclass-submit",
            status_cmd="/home/exouser/.local/bin/reclass-status",
            ssh_binary=f.ssh, scp_binary=f.scp, key_file=str(key), known_hosts_file=str(kh),
            ssh_timeout=10, scp_timeout=10,
        )
        (tmp_path / "tmp").mkdir(exist_ok=True)
        f.rt = ReclassRuntime(rr, tmp_root=tmp_path / "tmp")
        app.dependency_overrides[get_reclass_runtime] = lambda: f.rt
        return f

    yield _make
    app.dependency_overrides.pop(get_reclass_runtime, None)


async def test_requires_admin_key(client, remote):
    remote()
    for path in (f"{JOBS}/submit", f"{JOBS}/poll", f"{JOBS}/x/poll"):
        assert (await client.post(path)).status_code == 401
        assert (await client.post(path, headers=USER)).status_code == 401


async def test_submit_end_to_end(client, db_session, remote):
    f = remote()
    seeded = sorted([(await _pending(db_session, _seq(40, i))) for i in (3, 1, 2)])
    r = await client.post(f"{JOBS}/submit", headers=ADMIN)
    assert r.status_code == 201, r.text
    j = r.json()
    assert j["job_id"] == JOB_ID and j["status"] == "QUEUED"
    assert j["sequence_count"] == 3 and j["residue_count"] == 120 and j["input_bytes"] > 0
    assert re.fullmatch(r"pending-\d{8}T\d{6}Z\.fa", j["input_name"])

    # the uploaded FASTA is exactly the export, in id order
    uploaded = f.uploads / j["input_name"]
    lines = uploaded.read_text().splitlines()
    assert lines[0::2] == [f">{sid}|{h}" for sid, h in seeded]
    assert len(uploaded.read_bytes()) == j["input_bytes"]

    scp_line = next(ln for ln in _log_lines(f) if ln.startswith("scp "))
    ssh_line = next(ln for ln in _log_lines(f) if ln.startswith("ssh "))
    for needle in ("-o BatchMode=yes", "-o IdentitiesOnly=yes", "-o StrictHostKeyChecking=yes",
                   "UserKnownHostsFile=", "reclass_ed25519"):
        assert needle in scp_line and needle in ssh_line, needle
    assert scp_line.endswith(f"exouser@box:/media/volume/mmseq2-data/inbox/{j['input_name']}")
    assert ssh_line.endswith(
        f"exouser@box /home/exouser/.local/bin/reclass-submit inbox/{j['input_name']}"
    )

    # recorded, active, and the staging dir is gone
    active = (await client.get(JOBS, params={"active": "true"}, headers=ADMIN)).json()
    assert [x["job_id"] for x in active] == [JOB_ID]
    assert list((f.rt.tmp_root).glob("reclass_export_*")) == []


async def test_submit_body_controls_the_export(client, db_session, remote):
    f = remote()
    short_id, _ = await _pending(db_session, _seq(20, 10))
    ids = sorted([(await _pending(db_session, _seq(40, i)))[0] for i in (11, 12, 13)])
    r = await client.post(f"{JOBS}/submit", json={"limit": 2, "min_length": 1}, headers=ADMIN)
    assert r.status_code == 201, r.text
    assert r.json()["sequence_count"] == 2
    up = next(f.uploads.iterdir()).read_text().splitlines()
    assert [int(h[1:].split("|")[0]) for h in up[0::2]] == sorted([short_id, *ids])[:2]
    r = await client.post(f"{JOBS}/submit", json={"bogus": 1}, headers=ADMIN)
    assert r.status_code == 422


async def test_submit_nothing_pending_is_409_without_touching_the_box(client, remote):
    f = remote()
    r = await client.post(f"{JOBS}/submit", headers=ADMIN)
    assert r.status_code == 409 and r.json()["error"]["code"] == "reclass_nothing_pending"
    assert _log_lines(f) == []
    assert (await client.get(JOBS, headers=ADMIN)).json() == []


@pytest.mark.parametrize("fail,expect_scp", [("scp", True), ("submit", True)])
async def test_submit_remote_failure_is_503_and_records_nothing(
    client, db_session, remote, fail, expect_scp
):
    f = remote(fail=fail)
    await _pending(db_session, _seq(40, 20))
    r = await client.post(f"{JOBS}/submit", headers=ADMIN)
    assert r.status_code == 503, r.text
    assert r.json()["error"]["code"] == "reclass_remote_unavailable"
    assert fail in r.json()["error"]["message"]
    assert (await client.get(JOBS, headers=ADMIN)).json() == []
    assert any(ln.startswith("scp ") for ln in _log_lines(f)) is expect_scp
    assert list(f.rt.tmp_root.glob("reclass_export_*")) == []


async def test_submit_garbage_job_id_is_503(client, db_session, remote):
    remote(submit_output="ERROR: inbox not writable")
    await _pending(db_session, _seq(40, 21))
    r = await client.post(f"{JOBS}/submit", headers=ADMIN)
    assert r.status_code == 503 and "bad_job_id" in r.json()["error"]["message"]
    assert (await client.get(JOBS, headers=ADMIN)).json() == []


async def test_submit_in_progress_is_409(client, db_session, remote):
    f = remote()
    await _pending(db_session, _seq(40, 22))
    await f.rt.submit_lock.acquire()
    try:
        r = await client.post(f"{JOBS}/submit", headers=ADMIN)
        assert r.status_code == 409 and r.json()["error"]["code"] == "reclass_submit_in_progress"
    finally:
        f.rt.submit_lock.release()
    assert _log_lines(f) == []


async def _record(client, job_id: str):
    r = await client.post(JOBS, json={"job_id": job_id, "input_name": f"{job_id}.fa",
                                      "sequence_count": 40}, headers=ADMIN)
    assert r.status_code == 201, r.text


async def test_poll_all_and_one(client, remote):
    f = remote(statuses={"j-run": {**RUNNING, "job": "j-run", "posted_so_far": 7},
                         "j-done": {**DONE, "job": "j-done"}})
    for jid in ("j-run", "j-done", "j-gone"):
        await _record(client, jid)

    r = await client.post(f"{JOBS}/poll", headers=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["failed"] == []
    got = {p["job"]["job_id"]: (p["previous_status"], p["job"]["status"]) for p in body["polled"]}
    assert got == {"j-run": ("QUEUED", "RUNNING"), "j-done": ("QUEUED", "DONE"),
                   "j-gone": ("QUEUED", "UNKNOWN")}
    run = next(p["job"] for p in body["polled"] if p["job"]["job_id"] == "j-run")
    assert run["posted_so_far"] == 7 and run["poll_count"] == 1
    gone = next(p["job"] for p in body["polled"] if p["job"]["job_id"] == "j-gone")
    assert gone["last_error"] == "no such job"
    assert sum(1 for ln in _log_lines(f) if "reclass-status" in ln) == 3

    # second round: only the still-active job is polled; nothing else touches ssh
    r = await client.post(f"{JOBS}/poll", headers=ADMIN)
    assert [p["job"]["job_id"] for p in r.json()["polled"]] == ["j-run"]
    assert sum(1 for ln in _log_lines(f) if "reclass-status" in ln) == 4

    # single-job poll works on any status; unknown id is 404
    r = await client.post(f"{JOBS}/j-done/poll", headers=ADMIN)
    assert r.status_code == 200 and r.json()["previous_status"] == "DONE"
    assert (await client.post(f"{JOBS}/nope/poll", headers=ADMIN)).status_code == 404
    # nothing active after everything terminal -> no ssh at all
    (f.status_dir / "j-run.json").write_text(json.dumps({**DONE, "job": "j-run"}))
    await client.post(f"{JOBS}/poll", headers=ADMIN)
    n = sum(1 for ln in _log_lines(f) if "reclass-status" in ln)
    r = await client.post(f"{JOBS}/poll", headers=ADMIN)
    assert r.json() == {"polled": [], "failed": []}
    assert sum(1 for ln in _log_lines(f) if "reclass-status" in ln) == n


async def test_poll_remote_failure(client, remote):
    remote(fail="status")
    await _record(client, "j-x")
    r = await client.post(f"{JOBS}/j-x/poll", headers=ADMIN)
    assert r.status_code == 503 and r.json()["error"]["code"] == "reclass_remote_unavailable"
    r = await client.post(f"{JOBS}/poll", headers=ADMIN)
    assert r.status_code == 200
    assert r.json()["polled"] == [] and r.json()["failed"][0]["job_id"] == "j-x"
    j = (await client.get(f"{JOBS}/j-x", headers=ADMIN)).json()
    assert j["status"] == "QUEUED" and j["poll_count"] == 0  # untouched


async def test_poll_unparseable_status_is_503(client, remote):
    remote(statuses={"j-bad": {"job": "j-bad", "status": "EXPLODED"}})
    await _record(client, "j-bad")
    r = await client.post(f"{JOBS}/j-bad/poll", headers=ADMIN)
    assert r.status_code == 503 and "not understood" in r.json()["error"]["message"]
