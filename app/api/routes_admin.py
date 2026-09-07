"""GET/PUT /v1/admin/config — runtime-adjustable operational settings.

Admin-key gated. Never returns or accepts API keys. Updates take effect on the
next request (settings are read per-request from the small app_settings table;
v0.1 keeps this uncached for correctness — hot-adjustability is the
requirement, caching is a future optimization).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import require_admin_key
from app.db.session import get_session
from app.schemas.admin import (
    AdminConfig,
    AdminConfigUpdate,
    AdminIngestRequest,
    NamespaceStatus,
    ReclassifyRequest,
    ReclassifyResponse,
    SourceInfo,
    SourceUpdate,
)
from app.schemas.ingestion import UploadCompleteResponse
from app.services.deps import (
    get_capacity_gate,
    get_ingestion_service,
    get_maintenance_service,
    get_settings_service,
)
from app.services.ingestion_service import IngestionService
from app.services.maintenance_service import MaintenanceService
from app.services.namespace_capacity import NamespaceCapacityGate
from app.services.settings_service import SettingsService

router = APIRouter(
    prefix="/v1/admin", tags=["admin"], dependencies=[Depends(require_admin_key)]
)


async def _current(svc: SettingsService) -> AdminConfig:
    eff = await svc.effective()
    return AdminConfig(
        max_upload_size_bytes=eff.max_upload_size_bytes,
        token_ttl_seconds=eff.token_ttl_seconds,
        osdf_namespace_template=eff.osdf_namespace_template,
        reap_grace_hours=eff.reap_grace_hours,
    )


@router.get("/config", response_model=AdminConfig)
async def get_config(
    svc: SettingsService = Depends(get_settings_service),
) -> AdminConfig:
    return await _current(svc)


@router.put("/config", response_model=AdminConfig)
async def update_config(
    body: AdminConfigUpdate,
    svc: SettingsService = Depends(get_settings_service),
) -> AdminConfig:
    if body.max_upload_size_bytes is not None:
        await svc.set(
            "max_upload_size_bytes", str(body.max_upload_size_bytes), "admin"
        )
    if body.token_ttl_seconds is not None:
        await svc.set("token_ttl_seconds", str(body.token_ttl_seconds), "admin")
    if body.osdf_namespace_template is not None:
        await svc.set(
            "osdf_namespace_template", body.osdf_namespace_template, "admin"
        )
    if body.reap_grace_hours is not None:
        await svc.set("reap_grace_hours", str(body.reap_grace_hours), "admin")
    return await _current(svc)


@router.get("/namespaces", response_model=list[NamespaceStatus])
async def list_namespaces(
    session: AsyncSession = Depends(get_session),
    gate: NamespaceCapacityGate = Depends(get_capacity_gate),
) -> list[NamespaceStatus]:
    """Every configured OSDF namespace with its usage, capacity, headroom and
    whether the capacity gate currently admits new uploads. Bypasses the gate's
    60 s cache so an operator sees the live number."""
    snap = await gate.snapshot(session, fresh=True)
    return [NamespaceStatus(**vars(u)) for _, u in sorted(snap.items())]


@router.get("/sources", response_model=list[SourceInfo])
async def list_sources(
    svc: SettingsService = Depends(get_settings_service),
) -> list[SourceInfo]:
    return [
        SourceInfo(
            name=s.name, enabled=s.enabled,
            idle_ttl_days=s.idle_ttl_days, notes=s.notes,
        )
        for s in await svc.list_sources()
    ]


@router.put("/sources/{name}", response_model=SourceInfo)
async def update_source(
    name: str,
    body: SourceUpdate,
    svc: SettingsService = Depends(get_settings_service),
) -> SourceInfo:
    """Update a source: enabled flag, idle_ttl_days, and/or notes.

    Only fields PRESENT in the request body are written, so idle_ttl_days can be
    set to null ("never cull") distinctly from being omitted. Previously this
    endpoint accepted only `enabled` and silently discarded everything else while
    returning 200 with an unchanged body -- which read exactly like success.
    """
    supplied = body.model_dump(exclude_unset=True)
    # `name` is accepted only so a GET response can be round-tripped; it is not
    # a rename and must agree with the path.
    body_name = supplied.pop("name", None)
    if body_name is not None and body_name != name:
        raise HTTPException(
            status_code=400,
            detail=(
                f"body name {body_name!r} does not match path {name!r}; "
                "this endpoint cannot rename a source"
            ),
        )
    if not supplied:
        raise HTTPException(
            status_code=400,
            detail="no updatable fields supplied (enabled, idle_ttl_days, notes)",
        )
    if not await svc.update_source(name, supplied):
        raise HTTPException(status_code=404, detail=f"unknown source: {name}")
    row = next(s for s in await svc.list_sources() if s.name == name)
    return SourceInfo(
        name=row.name, enabled=row.enabled,
        idle_ttl_days=row.idle_ttl_days, notes=row.notes,
    )


@router.post("/maintenance/run")
async def run_maintenance(
    svc: MaintenanceService = Depends(get_maintenance_service),
) -> dict[str, int]:
    """Idle-sweep then reap. Intended to be hit by a cron/scheduler."""
    res = await svc.run()
    return {"marked_stale": res.marked_stale, "reaped": res.reaped}


@router.post("/maintenance/reclassify")
async def run_reclassify(
    limit: int = 100,
    after_id: int = 0,
    svc: MaintenanceService = Depends(get_maintenance_service),
) -> dict:
    """Re-run the Classifier on entries still marked 'Unclassified' with
    id > after_id. Cron-friendly (single batch, ignore the cursor); for a
    one-time backlog drain, loop calling this with after_id := last_id until
    exhausted=true (deploy/jetstream2/reclassify_drain.sh does this).
    """
    res = await svc.reclassify_unclassified(limit=limit, after_id=after_id)
    return {
        "scanned": res.scanned,
        "updated": res.updated,
        "last_id": res.last_id,
        "exhausted": res.exhausted,
        "limit": limit,
        "after_id": after_id,
    }


@router.post("/reclassify", response_model=ReclassifyResponse)
async def reclassify_from_blast(
    body: ReclassifyRequest,
    svc: MaintenanceService = Depends(get_maintenance_service),
) -> ReclassifyResponse:
    """Single-item classification callback from the external BLAST pipeline.

    NOT the batch sweep — that is POST /v1/admin/maintenance/reclassify, which
    re-runs the local Classifier over 'Unclassified' rows. This endpoint takes
    one externally computed (species, query_name) result and applies it to ALL
    msa_cache_entries for the sequence that still carry 'Pending Classification'
    in both fields (the sentinel the job-upload path writes; see
    IngestionService._resolve_classification). sequence_hash must match the
    stored seq_hash (409 on mismatch — guards against stale/transposed ids).
    Idempotent: a repeat call returns 200 with updated=0.
    """
    updated = await svc.apply_external_classification(
        body.sequence_id, body.sequence_hash, body.species, body.query_name
    )
    return ReclassifyResponse(sequence_id=body.sequence_id, updated=updated)


@router.post("/ingest", response_model=UploadCompleteResponse)
async def admin_ingest(
    body: AdminIngestRequest,
    svc: IngestionService = Depends(get_ingestion_service),
) -> UploadCompleteResponse:
    """Bulk-seed an a3m the caller has on (or is about to put on) the origin.

    Bypasses the community-only upload gate (admin asserts the source). Uses
    body.osdf_uri when given; otherwise mints one from osdf_namespace_template
    against the new msa_ingestions.id — the same id-derived path the job upload
    path produces — and returns it. Runs the same verify+publish pipeline as a
    normal upload-complete — in pelican mode, fetches the object and checks
    sha/size/format + that the a3m's query hashes to the declared sequence; in
    dryrun mode, logs the would-be `pelican object get` and publishes trusting
    the declared metadata. Returns the lifecycle outcome (available / rejected).
    """
    return await svc.admin_ingest(body)
