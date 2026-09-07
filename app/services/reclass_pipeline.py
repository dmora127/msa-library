"""API-driven NR-BLAST hand-off: submit a pending-classification job, poll it.

submit_pending_job: export -> scp to the box's inbox -> `reclass-submit` ->
record the job (QUEUED). poll_job / poll_active_jobs: `reclass-status` ->
store the JSON. Both are what the cron helpers now call over HTTPS, and what an
operator calls for an on-demand run.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import (
    ReclassNothingPending,
    ReclassRemoteUnavailable,
    ReclassSubmitInProgress,
)
from app.schemas.admin import ReclassRemoteStatus
from app.services import reclass_jobs
from app.services.classification_export import write_pending_fasta
from app.services.reclass_remote import RemoteReclass, RemoteReclassError

log = logging.getLogger("af3cache.reclass")


class ReclassRuntime:
    """Process singleton (lru_cache'd in deps): the remote client and the
    submit lock (one export/upload/submit at a time per process)."""

    def __init__(self, remote: RemoteReclass, *, tmp_root: Path | None = None):
        self.remote = remote
        self.tmp_root = tmp_root
        self.submit_lock = asyncio.Lock()


def build_reclass_runtime(settings: Settings) -> ReclassRuntime:
    remote = RemoteReclass(
        remote=settings.reclass_remote,
        data_dir=settings.reclass_remote_data_dir.rstrip("/"),
        submit_cmd=settings.reclass_remote_submit,
        status_cmd=settings.reclass_remote_status,
        ssh_binary=settings.reclass_ssh_binary,
        scp_binary=settings.reclass_scp_binary,
        key_file=settings.reclass_ssh_key_file,
        known_hosts_file=settings.reclass_known_hosts_file,
        ssh_timeout=settings.reclass_ssh_timeout_seconds,
        scp_timeout=settings.reclass_scp_timeout_seconds,
    )
    tmp_root = Path(settings.reclass_tmp_root) if settings.reclass_tmp_root else None
    return ReclassRuntime(remote, tmp_root=tmp_root)


@dataclass(frozen=True)
class PolledJob:
    previous_status: str
    job: dict


@dataclass(frozen=True)
class PollFailure:
    job_id: str
    error: str


async def submit_pending_job(
    session: AsyncSession,
    rt: ReclassRuntime,
    *,
    min_length: int,
    max_length: int | None = None,
    limit: int | None = None,
) -> dict:
    if rt.submit_lock.locked():
        raise ReclassSubmitInProgress("another reclassification submit is in progress")
    async with rt.submit_lock:
        tmp = await asyncio.to_thread(
            tempfile.TemporaryDirectory,
            prefix="reclass_export_",
            dir=str(rt.tmp_root) if rt.tmp_root else None,
        )
        try:
            name = f"pending-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.fa"
            path = Path(tmp.name) / name
            stats = await write_pending_fasta(
                session, path, min_length=min_length, max_length=max_length, limit=limit
            )
            if stats.sequence_count == 0:
                raise ReclassNothingPending(
                    "no pending sequences match "
                    f"(min_length={min_length}, max_length={max_length}, limit={limit})"
                )
            try:
                await rt.remote.upload(path, name)
                job_id = await rt.remote.submit(name)
            except RemoteReclassError as e:
                log.warning("reclass submit: %s", e)
                raise ReclassRemoteUnavailable(
                    f"reclass {e.step} failed ({e.reason}); nothing recorded"
                ) from e
            row = await reclass_jobs.create_job(
                session,
                job_id=job_id,
                input_name=name,
                sequence_count=stats.sequence_count,
                residue_count=stats.residue_count,
                input_bytes=stats.bytes,
            )
            log.info(
                "reclass submit: job=%s sequences=%d residues=%d bytes=%d min_length=%d "
                "max_length=%s limit=%s",
                job_id, stats.sequence_count, stats.residue_count, stats.bytes,
                min_length, max_length, limit,
            )
            return row
        finally:
            await asyncio.to_thread(tmp.cleanup)


async def poll_job(session: AsyncSession, rt: ReclassRuntime, job_id: str) -> PolledJob:
    before = await reclass_jobs.get_job(session, job_id)  # 404 if unknown
    try:
        payload = await rt.remote.status(job_id)
    except RemoteReclassError as e:
        log.warning("reclass poll %s: %s", job_id, e)
        raise ReclassRemoteUnavailable(f"reclass status failed ({e.reason})") from e
    try:
        st = ReclassRemoteStatus(**payload)
    except ValidationError as e:
        raise ReclassRemoteUnavailable(
            f"reclass status payload not understood: {str(e).splitlines()[0][:120]}"
        ) from e
    row = await reclass_jobs.apply_remote_status(
        session,
        job_id,
        status=st.status,
        submitted_at=st.submitted_at,
        started_at=st.started_at,
        finished_at=st.finished_at,
        exit_code=st.exit_code,
        posted_so_far=st.posted_so_far,
        summary=st.summary,
        error=st.error,
        raw=st.model_dump(mode="json"),
    )
    if row["status"] != before["status"]:
        log.info("reclass poll: job=%s %s -> %s", job_id, before["status"], row["status"])
    return PolledJob(previous_status=before["status"], job=row)


async def poll_active_jobs(
    session: AsyncSession, rt: ReclassRuntime, *, limit: int = 500
) -> tuple[list[PolledJob], list[PollFailure]]:
    """Poll every QUEUED/RUNNING job. One remote failure does not stop the
    others; failures are returned alongside the successes."""
    rows = await reclass_jobs.list_jobs(session, active_only=True, limit=limit)
    polled: list[PolledJob] = []
    failed: list[PollFailure] = []
    for r in rows:
        try:
            polled.append(await poll_job(session, rt, r["job_id"]))
        except ReclassRemoteUnavailable as e:
            failed.append(PollFailure(job_id=r["job_id"], error=e.message))
    return polled, failed
