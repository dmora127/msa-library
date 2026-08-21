"""Typed application exceptions mapped to HTTP responses."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


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


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _handle(_: Request, exc: AppError) -> JSONResponse:
        body = {"code": exc.code, "message": exc.message}
        if exc.extra:
            body.update(exc.extra)
        return JSONResponse(status_code=exc.status_code, content={"error": body})
