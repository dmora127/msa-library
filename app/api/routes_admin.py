"""GET/PUT /v1/admin/config — runtime-adjustable operational settings.

Admin-key gated. Never returns or accepts API keys. Updates take effect on the
next request (settings are read per-request from the small app_settings table;
v0.1 keeps this uncached for correctness — hot-adjustability is the
requirement, caching is a future optimization).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.core.security import require_admin_key
from app.schemas.admin import (
    AdminConfig,
    AdminConfigUpdate,
    AdminIngestRequest,
    SourceInfo,
    SourceUpdate,
)
from app.schemas.ingestion import UploadCompleteResponse
from app.services.deps import (
    get_ingestion_service,
    get_maintenance_service,
    get_settings_service,
)
from app.services.ingestion_service import IngestionService
from app.services.maintenance_service import MaintenanceService
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
    """Toggle a source's enabled flag (query visibility; for the community
    source, also gates job uploads)."""
    if not await svc.set_source_enabled(name, body.enabled):
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
