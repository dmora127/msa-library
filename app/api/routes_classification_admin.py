"""/v1/admin/classification — NR BLAST reclassification export + job tracking.

Admin-key gated. Driven by host cron on the VM (no scheduler in-process, and
the container has no ssh): helpers/reclassify_export.sh downloads pending.fa,
scp's it to the BLAST box, runs `reclass-submit` there and POSTs the job id
here; helpers/reclassify_poll.sh asks GET /jobs?active=true and, only when a
job is QUEUED/RUNNING, runs `reclass-status` remotely and PUTs the JSON back.
Results themselves arrive per sequence via the existing POST /v1/admin/reclassify.

Streaming note: pending.fa streams straight from the request-scoped session.
That is safe on the pinned FastAPI (0.136.1): `fastapi/routing.py` sends the
response INSIDE the request AsyncExitStack, so `get_session`'s commit/close
runs after the body has been sent (yield dependencies default to request
scope). tests/test_classification_export.py::test_streams_in_multiple_chunks
fails loudly if a future FastAPI moves teardown before the response again.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.security import require_admin_key
from app.db.session import get_session
from app.schemas.admin import ReclassJobCreate, ReclassJobOut, ReclassRemoteStatus
from app.services import reclass_jobs
from app.services.classification_export import count_pending, iter_pending_fasta

router = APIRouter(
    prefix="/v1/admin/classification",
    tags=["admin"],
    dependencies=[Depends(require_admin_key)],
)


@router.get(
    "/pending.fa",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/x-fasta": {}}, "description": "FASTA"}},
)
async def export_pending_fasta(
    min_length: int | None = Query(
        default=None, ge=1,
        description="Skip sequences shorter than this (aa). Default: RECLASS_EXPORT_MIN_LENGTH.",
    ),
    max_length: int | None = Query(
        default=None, ge=1, description="Skip sequences longer than this (aa)."
    ),
    limit: int | None = Query(default=None, ge=1, description="Cap the number of records."),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    """Every protein sequence with an ACTIVE cache entry still marked
    'Pending Classification' in both species and query_name, as FASTA with
    headers `>{sequence_id}|{seq_hash}` (what POST /v1/admin/reclassify needs),
    ordered by sequence id. X-Sequence-Count is an advisory pre-count."""
    effective_min = min_length if min_length is not None else settings.reclass_export_min_length
    n = await count_pending(session, min_length=effective_min, max_length=max_length, limit=limit)
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    headers = {
        "Content-Disposition": f'attachment; filename="pending-{ts}.fa"',
        "X-Sequence-Count": str(n),
        "X-Min-Length": str(effective_min),
        "Cache-Control": "no-store",
    }
    if max_length is not None:
        headers["X-Max-Length"] = str(max_length)
    return StreamingResponse(
        iter_pending_fasta(session, min_length=effective_min, max_length=max_length, limit=limit),
        media_type="text/x-fasta",
        headers=headers,
    )


@router.post("/jobs", response_model=ReclassJobOut, status_code=201)
async def create_reclass_job(
    body: ReclassJobCreate,
    session: AsyncSession = Depends(get_session),
) -> ReclassJobOut:
    """Record a job the helper just submitted remotely (status QUEUED)."""
    row = await reclass_jobs.create_job(
        session,
        job_id=body.job_id,
        input_name=body.input_name,
        sequence_count=body.sequence_count,
        residue_count=body.residue_count,
        input_bytes=body.input_bytes,
    )
    return ReclassJobOut(**row)


@router.get("/jobs", response_model=list[ReclassJobOut])
async def list_reclass_jobs(
    active: bool = Query(default=False, description="Only QUEUED/RUNNING jobs."),
    limit: int = Query(default=50, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
) -> list[ReclassJobOut]:
    rows = await reclass_jobs.list_jobs(session, active_only=active, limit=limit)
    return [ReclassJobOut(**r) for r in rows]


@router.get("/jobs/{job_id}", response_model=ReclassJobOut)
async def get_reclass_job(
    job_id: str,
    session: AsyncSession = Depends(get_session),
) -> ReclassJobOut:
    return ReclassJobOut(**await reclass_jobs.get_job(session, job_id))


@router.put("/jobs/{job_id}/status", response_model=ReclassJobOut)
async def update_reclass_job_status(
    job_id: str,
    body: ReclassRemoteStatus,
    session: AsyncSession = Depends(get_session),
) -> ReclassJobOut:
    """Store one `reclass-status` observation verbatim (plus typed columns).
    A terminal status (DONE/FAILED/KILLED/UNKNOWN) removes the job from
    GET /jobs?active=true, which stops the poller."""
    row = await reclass_jobs.apply_remote_status(
        session,
        job_id,
        status=body.status,
        submitted_at=body.submitted_at,
        started_at=body.started_at,
        finished_at=body.finished_at,
        exit_code=body.exit_code,
        posted_so_far=body.posted_so_far,
        summary=body.summary,
        error=body.error,
        raw=body.model_dump(mode="json"),
    )
    return ReclassJobOut(**row)
