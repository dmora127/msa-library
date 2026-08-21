"""Inbound WLCG-profile JWT (SciToken) validation for the uploads routes.

This validates bearer tokens that CHTC/OSPool jobs present when calling
`/v1/uploads/*`. It is the OPPOSITE direction from `token_issuer` — that module
*mints* short-lived OSDF write tokens; this one *verifies* the job's inbound
identity token.

Design (locked with the operator):
  * Static pinned public key on the deployment — NO issuer discovery / JWKS
    fetch. `jwt_public_key_file` points at a PEM or a JWKS JSON. When it is
    unset/unreadable the JWT path FAILS CLOSED (every token rejected) while the
    API-key path keeps working — so the API can deploy before the key lands.
  * ES256 only. `alg: none` and any other algorithm are rejected outright.
  * `kid` is NOT enforced (operator opted out for now).
  * Expiry uses clock skew PLUS a one-sided `jwt_allowed_past_exp_seconds`
    grace so a long job whose token died mid-upload isn't bounced.
"""

from __future__ import annotations

import json
import logging
import time
from functools import lru_cache

import jwt
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from jwt.algorithms import ECAlgorithm
from jwt.exceptions import InvalidSignatureError, PyJWTError

from app.core.config import Settings

log = logging.getLogger("af3cache.scitokens")

# WLCG profile version we accept.
_WLCG_VER = "1.0"


class TokenError(Exception):
    """Token is absent/malformed/unauthenticated -> 401."""


class ScopeError(Exception):
    """Token authenticates but lacks the required scope -> 403."""


@lru_cache(maxsize=4)
def _load_keys(key_file: str | None) -> tuple:
    """Load pinned verification key(s) from a PEM or JWKS file.

    Cached for process lifetime keyed on the path — the operator restarts the
    container after dropping/rotating the key, which clears this. Returns an
    empty tuple when the key is missing/unreadable/unparseable so callers fail
    closed on the JWT path.
    """
    if not key_file:
        return ()
    try:
        with open(key_file, "rb") as fh:
            raw = fh.read()
    except OSError as e:
        log.warning("JWT public key unreadable (%s): %s — JWT auth disabled", key_file, e)
        return ()

    text = raw.strip()
    # JWKS JSON vs PEM: a JWKS starts with '{'.
    if text[:1] == b"{":
        try:
            doc = json.loads(text)
            keys = doc.get("keys", []) if isinstance(doc, dict) else []
            loaded = tuple(ECAlgorithm.from_jwk(json.dumps(k)) for k in keys)
        except (ValueError, KeyError, PyJWTError) as e:
            log.warning("JWT public key JWKS parse failed (%s): %s", key_file, e)
            return ()
        if not loaded:
            log.warning("JWT public key JWKS had no keys (%s)", key_file)
        return loaded
    try:
        return (load_pem_public_key(text),)
    except (ValueError, TypeError) as e:
        log.warning("JWT public key PEM parse failed (%s): %s", key_file, e)
        return ()


def _parse_scope_token(tok: str) -> tuple[str, str]:
    """`write:/community-contributed` -> ("write", "/community-contributed").

    A bare action with no path (e.g. `compute.read`) is treated as path "/".
    """
    if ":" in tok:
        action, path = tok.split(":", 1)
        return action, path or "/"
    return tok, "/"


def _path_covers(granted_path: str, required_path: str) -> bool:
    """True if a grant on `granted_path` authorizes the `required_path`.

    WLCG paths are hierarchical: a grant at an ancestor (or the same path)
    covers a descendant. So `write:/` and `write:/community-contributed` both
    cover the `/community-contributed` namespace, but `write:/community-contributed/foo`
    (a strictly narrower grant) does NOT.
    """
    g = granted_path.rstrip("/")
    r = required_path.rstrip("/")
    if g == "":  # grant on "/" covers everything
        return True
    return r == g or r.startswith(g + "/")


def _scope_satisfies(granted_scope: str, required_scope: str) -> bool:
    req_action, req_path = _parse_scope_token(required_scope)
    for tok in granted_scope.split():
        action, path = _parse_scope_token(tok)
        if action == req_action and _path_covers(path, req_path):
            return True
    return False


def looks_like_jwt(token: str) -> bool:
    """Cheap discriminator for the dual-auth window: does this credential look
    like a JWT (three dot-separated segments whose header decodes with an
    `alg`) rather than an opaque API key? Never raises."""
    if token.count(".") != 2:
        return False
    try:
        return "alg" in jwt.get_unverified_header(token)
    except PyJWTError:
        return False


def verify_bearer(token: str, settings: Settings) -> dict:
    """Validate a WLCG-profile ES256 token. Returns claims or raises.

    TokenError -> 401 (auth), ScopeError -> 403 (authenticated but unauthorized).
    """
    allowed_algs = settings.jwt_allowed_alg_set

    # 1. Header alg allowlist — rejects `none` and anything but ES256 before we
    #    touch the signature.
    try:
        header = jwt.get_unverified_header(token)
    except PyJWTError as e:
        raise TokenError(f"malformed token header: {e}") from e
    alg = header.get("alg")
    if alg not in allowed_algs:
        raise TokenError(f"disallowed alg: {alg!r}")

    # 2. Pinned key(s). Empty -> fail closed (key not deployed yet).
    keys = _load_keys(settings.jwt_public_key_file)
    if not keys:
        raise TokenError("JWT public key not configured")

    # 3. Signature + standard claims. exp is checked manually below (PyJWT's
    #    leeway is symmetric and can't express a one-sided past-exp grace), so
    #    verify_exp is disabled here; presence of exp is still required.
    options = {
        "require": ["exp", "iat", "iss", "aud"],
        "verify_exp": False,
        "verify_aud": True,
        "verify_signature": True,
    }
    claims = None
    for key in keys:
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=list(allowed_algs),
                audience=settings.token_bearer_audience,
                leeway=settings.jwt_clock_skew_seconds,
                options=options,
            )
            break
        except InvalidSignatureError:
            continue  # try the next pinned key
        except PyJWTError as e:
            # aud/nbf/missing-claim/decode errors are key-independent.
            raise TokenError(f"invalid token: {e}") from e
    if claims is None:
        raise TokenError("signature verification failed")

    # 4. Issuer allowlist.
    if claims.get("iss") not in settings.trusted_issuer_set:
        raise TokenError(f"untrusted issuer: {claims.get('iss')!r}")

    # 5. Expiry with clock skew + one-sided grace.
    deadline = (
        float(claims["exp"])
        + settings.jwt_clock_skew_seconds
        + settings.jwt_allowed_past_exp_seconds
    )
    if time.time() > deadline:
        raise TokenError("token expired")

    # 6. WLCG profile marker.
    if str(claims.get("wlcg.ver")) != _WLCG_VER:
        raise TokenError(f"unsupported wlcg.ver: {claims.get('wlcg.ver')!r}")

    # 7. Scope.
    if not _scope_satisfies(str(claims.get("scope", "")), settings.upload_required_scope):
        raise ScopeError(
            f"scope {claims.get('scope')!r} does not satisfy "
            f"{settings.upload_required_scope!r}"
        )

    log.info(
        "upload token accepted iss=%s sub=%s jti=%s scope=%s",
        claims.get("iss"), claims.get("sub"), claims.get("jti"), claims.get("scope"),
    )
    return claims
