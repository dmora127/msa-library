"""Upload endpoints (dual-auth: WLCG bearer token OR API key).

POST /v1/uploads/request  -> scoped write token + target (413 if oversize)
POST /v1/uploads/{id}/complete -> verification callback (added next phase)
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.core.security import require_upload_auth
from app.schemas.ingestion import (
    UploadCompleteRequest,
    UploadCompleteResponse,
    UploadRequest,
    UploadRequestResponse,
)
from app.services.deps import get_ingestion_service
from app.services.ingestion_service import IngestionService

router = APIRouter(
    prefix="/v1/uploads", tags=["uploads"], dependencies=[Depends(require_upload_auth)]
)


@router.post("/request", response_model=UploadRequestResponse)
async def request_upload(
    req: UploadRequest,
    svc: IngestionService = Depends(get_ingestion_service),
) -> UploadRequestResponse:
    return await svc.request_upload(req)


@router.post("/{ingestion_id}/complete", response_model=UploadCompleteResponse)
async def complete_upload(
    ingestion_id: str,
    body: UploadCompleteRequest | None = None,
    svc: IngestionService = Depends(get_ingestion_service),
) -> UploadCompleteResponse:
    return await svc.complete_upload(
        ingestion_id, requester=body.requester if body else None
    )
