"""API-key authentication dependency.

Accepts the key via `X-API-Key` or `Authorization: Bearer <key>`. Constant-time
comparison against the configured rotation-friendly key set.
"""

from __future__ import annotations

import hashlib
import hmac
import logging

from fastapi import Depends, Header, HTTPException, Security, status
from fastapi.security import APIKeyHeader

from app.core.config import Settings, get_settings
from app.core.scitokens import ScopeError, TokenError, looks_like_jwt, verify_bearer

log = logging.getLogger("af3cache.security")

# Registers an OpenAPI security scheme so Swagger /docs shows an "Authorize"
# button that injects the X-API-Key header. auto_error=False -> we keep our own
# 401 message and the Authorization: Bearer fallback.
_api_key_scheme = APIKeyHeader(name="X-API-Key", auto_error=False)


def _extract(x_api_key: str | None, authorization: str | None) -> str | None:
    if x_api_key:
        return x_api_key
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


def _match(candidate: str, accepted: set[str]) -> bool:
    # constant-time over the whole set (no early exit on first mismatch)
    matched = False
    for key in accepted:
        if hmac.compare_digest(candidate, key):
            matched = True
    return matched


def require_api_key(
    x_api_key: str | None = Security(_api_key_scheme),
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    candidate = _extract(x_api_key, authorization)
    if not candidate or not _match(candidate, settings.api_key_set):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid or missing API key"
        )


def jwt_auth_configured(settings: Settings) -> bool:
    """Is the inbound-token path actually deployed?

    It takes BOTH halves to verify anything: a pinned key to check the
    signature against and a non-empty issuer allowlist to check `iss` against
    (an empty `trusted_issuers` denies every token by design). With either
    missing, `verify_bearer` can only ever raise — so we don't call it, and
    /v1/uploads/* runs API-key-only until the operator sets
    JWT_PUBLIC_KEY_FILE + TRUSTED_ISSUERS. The token plumbing stays wired; it
    just isn't consulted.
    """
    return bool(settings.jwt_public_key_file and settings.trusted_issuer_set)


def require_upload_auth(
    x_api_key: str | None = Security(_api_key_scheme),
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    """Dual-auth gate for /v1/uploads/* during the JWT migration window.

    When the JWT path is configured (see `jwt_auth_configured`), a credential
    that looks like a JWT is validated as a WLCG bearer token, and a
    bad/expired/under-scoped one is rejected outright — it never falls through
    to the API-key path. When it is NOT configured, every credential is treated
    as an opaque API key, including JWT-shaped ones: verification is impossible,
    so the shared key is the only thing that can authorize an upload. Note the
    fallback can only ever require a *stronger* credential, never grant
    unauthenticated access. When jobs finish migrating, drop the API-key branch
    and swap this for `require_scope`.
    """
    candidate = _extract(x_api_key, authorization)
    if not candidate:
        log.warning(
            "upload rejected: no credential presented (x-api-key=%s "
            "authorization=%s)",
            "set" if x_api_key else "absent",
            "set" if authorization else "absent",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing upload credential",
        )
    jwt_shaped = looks_like_jwt(candidate)
    if jwt_shaped and jwt_auth_configured(settings):
        try:
            verify_bearer(candidate, settings)
        except ScopeError as e:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail=str(e)
            ) from e
        except TokenError as e:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e)
            ) from e
        return
    if not _match(candidate, settings.api_key_set):
        if jwt_shaped:
            # Logged (not silent) so this shows up as an operator problem
            # rather than a client one: jobs ARE presenting tokens, and the
            # deployment can't check them yet.
            log.warning(
                "upload rejected: bearer token presented but JWT auth is not "
                "configured (set JWT_PUBLIC_KEY_FILE + TRUSTED_ISSUERS); "
                "clients must send the API key until then"
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="token auth is not enabled on this deployment — "
                       "send the API key via X-API-Key",
            )
        # Fingerprint, never the key itself: sha256 prefix + length is enough to
        # tell "same wrong value every time" from "garbage", and to compare
        # against what the job holds (sha256 it there and match the prefix).
        log.warning(
            "upload rejected: credential matched no configured API key "
            "(sha256=%s… len=%d via=%s)",
            hashlib.sha256(candidate.encode()).hexdigest()[:8],
            len(candidate),
            "x-api-key" if x_api_key else "authorization",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid or missing API key"
        )


def require_admin_key(
    x_api_key: str | None = Security(_api_key_scheme),
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    candidate = _extract(x_api_key, authorization)
    if not candidate or not hmac.compare_digest(candidate, settings.admin_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid or missing admin key"
        )
