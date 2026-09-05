"""reclass_jobs: records of external NR BLAST reclassification jobs.

The host helper reclassify_export.sh creates a row right after
`reclass-submit` returns a job id; reclassify_poll.sh stores every
`reclass-status` JSON here. Rows are the durable record (the remote box
forgets jobs after 15 days). ACTIVE_STATES is what the poller asks for; a row
leaves that set on its first terminal status and is never polled again.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ReclassJobExists, ReclassJobNotFound

STATES = ("QUEUED", "RUNNING", "DONE", "FAILED", "KILLED", "UNKNOWN")
ACTIVE_STATES = ("QUEUED", "RUNNING")

_COLS = (
    "job_id, input_name, sequence_count, residue_count, input_bytes, status, "
    "remote_submitted_at, remote_started_at, remote_finished_at, exit_code, "
    "posted_so_far, summary, last_status, last_error, poll_count, last_polled_at, "
    "created_at, updated_at"
)


def _row(mapping: Any) -> dict:
    d = dict(mapping)
    # asyncpg hands jsonb back decoded; be tolerant if a driver returns text.
    for k in ("summary", "last_status"):
        if isinstance(d.get(k), str):
            d[k] = json.loads(d[k])
    return d


async def create_job(
    session: AsyncSession,
    *,
    job_id: str,
    input_name: str,
    sequence_count: int,
    residue_count: int | None = None,
    input_bytes: int | None = None,
) -> dict:
    existing = await session.execute(
        text("select 1 from public.reclass_jobs where job_id = :id"), {"id": job_id}
    )
    if existing.first() is not None:
        raise ReclassJobExists(f"job {job_id} already recorded")
    row = await session.execute(
        text(
            "insert into public.reclass_jobs "
            "(job_id, input_name, sequence_count, residue_count, input_bytes) "
            "values (:id, :name, :n, :res, :bytes) "
            f"returning {_COLS}"
        ),
        {"id": job_id, "name": input_name, "n": sequence_count,
         "res": residue_count, "bytes": input_bytes},
    )
    return _row(row.one()._mapping)


async def list_jobs(session: AsyncSession, *, active_only: bool, limit: int) -> list[dict]:
    where = "where status = any(:active)" if active_only else ""
    rows = await session.execute(
        text(
            f"select {_COLS} from public.reclass_jobs {where} "
            "order by created_at desc, job_id desc limit :limit"
        ),
        {"active": list(ACTIVE_STATES), "limit": limit},
    )
    return [_row(r._mapping) for r in rows]


async def get_job(session: AsyncSession, job_id: str) -> dict:
    row = (
        await session.execute(
            text(f"select {_COLS} from public.reclass_jobs where job_id = :id"), {"id": job_id}
        )
    ).one_or_none()
    if row is None:
        raise ReclassJobNotFound(f"no reclass job {job_id}")
    return _row(row._mapping)


async def apply_remote_status(
    session: AsyncSession,
    job_id: str,
    *,
    status: str,
    submitted_at: datetime | None,
    started_at: datetime | None,
    finished_at: datetime | None,
    exit_code: int | None,
    posted_so_far: int | None,
    summary: dict | None,
    error: str | None,
    raw: dict,
) -> dict:
    """Store one `reclass-status` observation. `status` must already be one of
    STATES (the schema validates). Remote timestamps overwrite ours when
    present; the full payload is kept in last_status; poll_count increments."""
    stmt = text(
        "update public.reclass_jobs set "
        "  status = :status, "
        "  remote_submitted_at = coalesce(:submitted_at, remote_submitted_at), "
        "  remote_started_at = coalesce(:started_at, remote_started_at), "
        "  remote_finished_at = coalesce(:finished_at, remote_finished_at), "
        "  exit_code = coalesce(:exit_code, exit_code), "
        "  posted_so_far = coalesce(:posted_so_far, posted_so_far), "
        "  summary = coalesce(:summary, summary), "
        "  last_status = :raw, "
        "  last_error = :error, "
        "  poll_count = poll_count + 1, "
        "  last_polled_at = now(), "
        "  updated_at = now() "
        f"where job_id = :id returning {_COLS}"
    ).bindparams(bindparam("summary", type_=JSONB), bindparam("raw", type_=JSONB))
    row = (
        await session.execute(
            stmt,
            {
                "id": job_id,
                "status": status,
                "submitted_at": submitted_at,
                "started_at": started_at,
                "finished_at": finished_at,
                "exit_code": exit_code,
                "posted_so_far": posted_so_far,
                "summary": summary,
                "raw": raw,
                "error": error,
            },
        )
    ).one_or_none()
    if row is None:
        raise ReclassJobNotFound(f"no reclass job {job_id}")
    return _row(row._mapping)
