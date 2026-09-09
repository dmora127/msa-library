"""Upload endpoints (dual-auth: WLCG bearer token OR API key).

POST /v1/uploads/request  -> scoped write token + target (413 if oversize)
POST /v1/uploads/{id}/complete -> callback: row parked 'uploaded', answered at
                                  once; verify+publish runs in the background
                                  (BackgroundVerifier), the sweeper retries
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from app.core.config import Settings, get_settings
from app.core.security import require_upload_auth
from app.schemas.ingestion import (
    PENDING_VERIFICATION_STATUS,
    UploadCompleteRequest,
    UploadCompleteResponse,
    UploadRequest,
    UploadRequestResponse,
)
from app.services.deps import get_background_verifier, get_ingestion_service
from app.services.ingestion_service import IngestionService
from app.services.upload_verifier import BackgroundVerifier

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
    response: Response,
    body: UploadCompleteRequest | None = None,
    svc: IngestionService = Depends(get_ingestion_service),
    verifier: BackgroundVerifier = Depends(get_background_verifier),
    settings: Settings = Depends(get_settings),
) -> UploadCompleteResponse:
    """Accept the job's "object is on the origin" callback.

    First callback: the row is committed as 'uploaded', the answer goes out
    with status "uploaded - pending verification" (HTTP code from
    UPLOAD_COMPLETE_ASYNC_STATUS_CODE), and the fetch/verify/publish runs in
    the background. A callback on an already-verified row returns the stored
    'available' / 'rejected' verdict with HTTP 200 -- that is how a caller can
    poll. This route never blocks on OSDF and never answers 503 for it.
    """
    out = await svc.accept_upload(
        ingestion_id, requester=body.requester if body else None
    )
    if out.status == PENDING_VERIFICATION_STATUS:
        # Set on the injected sub-response so response_model serialisation
        # (and the OpenAPI schema) still apply.
        response.status_code = settings.upload_complete_async_status_code
        await verifier.submit(ingestion_id, svc)
    return out
