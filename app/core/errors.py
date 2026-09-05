"""Typed application exceptions mapped to HTTP responses."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.metrics import record_app_error


class AppError(Exception):
    status_code = 500
    code = "internal_error"

    def __init__(self, message: str, *, extra: dict | None = None):
        super().__init__(message)
        self.message = message
        self.extra = extra or {}


class UploadTooLarge(AppError):
    status_code = 413
    code = "upload_too_large"


class SourceNotEnabled(AppError):
    status_code = 422
    code = "source_not_enabled"


class IngestionNotFound(AppError):
    status_code = 404
    code = "ingestion_not_found"


class OsdfUnavailable(AppError):
    """Transient: object store unreachable / pelican missing. Caller may retry."""

    status_code = 503
    code = "osdf_unavailable"


class TokenIssuanceError(AppError):
    """Failed to mint the scoped write token (e.g. pelican CLI missing /
    misconfigured). Server/infra problem — the job got no token, may retry."""

    status_code = 503
    code = "token_issuance_failed"


class AlreadyCached(AppError):
    """A live (active, non-idle) entry already exists for this
    (seq_hash, source, db_version). `extra` carries the existing osdf_uri so the
    job uses the cache instead of recomputing."""

    status_code = 409
    code = "already_cached"


class SequenceNotFound(AppError):
    status_code = 404
    code = "sequence_not_found"


class SequenceHashMismatch(AppError):
    """Caller's sequence_hash disagrees with msa_sequences.seq_hash for the
    given id — the caller's id↔hash pairing is stale or transposed; nothing
    is updated."""

    status_code = 409
    code = "sequence_hash_mismatch"


class SimilarityDisabled(AppError):
    """Caller opted into similarity_search but SIMILARITY_SEARCH_ENABLED is
    false on this deployment. The exact-match part of the query still ran."""

    status_code = 409
    code = "similarity_disabled"


class SimilarityUnavailable(AppError):
    """Transient: mmseqs missing/failed/timed out, queue full, or (snapshot
    mode) no active nightly generation. Caller may retry without the flag."""

    status_code = 503
    code = "similarity_unavailable"


class SimilarityBuildDisabled(AppError):
    """Nightly/delta build requested while its *_BUILD_ENABLED flag is false."""

    status_code = 409
    code = "similarity_build_disabled"


class SimilarityBuildInProgress(AppError):
    """Another nightly/delta build holds the process-level build lock."""

    status_code = 409
    code = "similarity_build_in_progress"


class SimilarityBuildFailed(AppError):
    """A nightly/delta build failed before promotion; the active generation
    is untouched."""

    status_code = 500
    code = "similarity_build_failed"


class ReclassJobExists(AppError):
    """POST /v1/admin/classification/jobs with a job_id already recorded."""

    status_code = 409
    code = "reclass_job_exists"


class ReclassJobNotFound(AppError):
    status_code = 404
    code = "reclass_job_not_found"


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _handle(_: Request, exc: AppError) -> JSONResponse:
        # Until now these were counted and logged NOWHERE: a storm of 503
        # osdf_unavailable or token_issuance_failed was completely invisible,
        # visible only as a status code in uvicorn's access log.
        record_app_error(exc.code)
        body = {"code": exc.code, "message": exc.message}
        if exc.extra:
            body.update(exc.extra)
        return JSONResponse(status_code=exc.status_code, content={"error": body})
