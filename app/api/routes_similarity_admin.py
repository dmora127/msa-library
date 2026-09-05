"""/v1/admin/similarity — snapshot-mode database builds + status.

Admin-key gated. There is deliberately no in-process scheduler (same as
/v1/admin/maintenance/run): host cron hits these via
deploy/jetstream2/helpers/similarity_{nightly,delta}.sh. Both build endpoints
return 409 similarity_build_disabled until SIMILARITY_NIGHTLY_BUILD_ENABLED /
SIMILARITY_DELTA_BUILD_ENABLED are set, so a cron line can be installed ahead
of the flag flip and stays quiet until then.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.core.security import require_admin_key
from app.schemas.admin import (
    SimilarityBuildResponse,
    SimilarityManifestInfo,
    SimilarityStatusResponse,
)
from app.services.deps import get_similarity_service
from app.services.similarity.models import Manifest
from app.services.similarity.service import BuildOutcome, SimilarityService

router = APIRouter(
    prefix="/v1/admin/similarity",
    tags=["admin"],
    dependencies=[Depends(require_admin_key)],
)


def _manifest_info(svc: SimilarityService, m: Manifest) -> SimilarityManifestInfo:
    return SimilarityManifestInfo(
        generation_path=str(svc.rt.cfg.db_root / m.generation), **m.__dict__
    )


def _build_response(svc: SimilarityService, out: BuildOutcome) -> SimilarityBuildResponse:
    return SimilarityBuildResponse(
        manifest=_manifest_info(svc, out.manifest),
        elapsed_seconds=round(out.elapsed_seconds, 3),
        retired=[p.name for p in out.retired],
    )


@router.get("/status", response_model=SimilarityStatusResponse)
async def similarity_status(
    svc: SimilarityService = Depends(get_similarity_service),
) -> SimilarityStatusResponse:
    """Mode, flags, thresholds, mmseqs version and the active nightly/delta
    manifests (null when no generation has been published yet)."""
    return SimilarityStatusResponse(**await svc.status())


@router.post("/nightly-build", response_model=SimilarityBuildResponse)
async def similarity_nightly_build(
    svc: SimilarityService = Depends(get_similarity_service),
) -> SimilarityBuildResponse:
    """Dump every active sequence from Postgres (repeatable-read snapshot),
    build + index a new immutable generation, write its manifest, atomically
    re-point nightly-buildDB, then retire generations past retention.
    Synchronous: the cron helper waits. 409 while the flag is off or another
    build is running; on failure the active generation is untouched."""
    return _build_response(svc, await svc.build_nightly())


@router.post("/delta-refresh", response_model=SimilarityBuildResponse)
async def similarity_delta_refresh(
    svc: SimilarityService = Depends(get_similarity_service),
) -> SimilarityBuildResponse:
    """Rebuild the hot delta from scratch: every active sequence newer than the
    ACTIVE nightly's manifest (id past its high-water mark, or inserted /
    newly-active after its snapshot), unindexed, then atomically re-point
    deltaDB. An empty delta is valid and is published as such."""
    return _build_response(svc, await svc.refresh_delta())
