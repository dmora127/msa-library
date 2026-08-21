"""GET /.well-known/jwks.json — public key set for verifying issued tokens.

Mounted ONLY when the active TokenIssuer is a JwksProvider (i.e. we self-sign).
Once the origin's OAuth2 issuer is in play (v0.2) this endpoint does not exist.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.services.deps import get_token_issuer
from app.services.token_issuer import JwksProvider, TokenIssuer

router = APIRouter(tags=["jwks"])


@router.get("/.well-known/jwks.json")
async def jwks(issuer: TokenIssuer = Depends(get_token_issuer)) -> dict:
    assert isinstance(issuer, JwksProvider)  # guaranteed by conditional mount
    return issuer.jwks()
