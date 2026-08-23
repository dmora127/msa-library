"""Scoped write-token issuance — the swap boundary.

`TokenIssuer.issue()` is issuer-agnostic so the impls are drop-in via
TOKEN_ISSUER_IMPL (no router/service/schema/DB change):

- pelican  (prod): shell out to `pelican token create --write --scope-path
            <pelican://store/obj> --lifetime <ttl>`; stdout IS the token.
- local    (dev) : self-sign a SciToken/WLCG JWT + publish JWKS.
- stub     (test): deterministic fake.
- oauth2   (v0.2): the origin's OAuth2 issuer.

`token_ref` is always an app-generated correlation id (issuer-agnostic, unique,
ties the issuance to its msa_ingestions row). `claim_jti` / `expires_at` are the
token's own jti / exp when discoverable, else best-effort. `jwks()` is NOT on
the ABC — only LocalSigningTokenIssuer is a JwksProvider, and the JWKS route is
mounted conditionally on that.
"""

from __future__ import annotations

import abc
import asyncio
import contextlib
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable
from urllib.parse import urlparse

import jwt
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from app.core.config import Settings
from app.core.errors import TokenIssuanceError
from app.core.metrics import record_token_issue

log = logging.getLogger("af3cache.tokens")


@dataclass(frozen=True)
class IssuedToken:
    token: str
    token_ref: str
    expires_at: datetime
    claim_jti: str | None


class TokenIssuer(abc.ABC):
    @abc.abstractmethod
    async def issue(
        self, *, subject: str, object_uri: str, ttl_seconds: int
    ) -> IssuedToken: ...


@runtime_checkable
class JwksProvider(Protocol):
    def jwks(self) -> dict: ...


def _scope_path(object_uri: str) -> str:
    """Path portion of the object URI (handles pelican://store/p and /p)."""
    p = urlparse(object_uri).path or object_uri
    return p if p.startswith("/") else f"/{p}"


def _decode_unverified(token: str) -> dict | None:
    try:
        return jwt.decode(
            token,
            options={"verify_signature": False, "verify_aud": False,
                     "verify_exp": False},
        )
    except Exception:
        return None


class StubTokenIssuer(TokenIssuer):
    """Deterministic, unsigned — for tests of non-token logic. No JWKS."""

    async def issue(
        self, *, subject: str, object_uri: str, ttl_seconds: int
    ) -> IssuedToken:
        ref = uuid.uuid4().hex
        exp = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
        return IssuedToken(
            token=f"stub.{ref}.{int(exp.timestamp())}",
            token_ref=ref,
            expires_at=exp,
            claim_jti=None,
        )


def _pelican_argv(
    binary: str, object_uri: str, issuer: str, private_key: str, ttl_seconds: int
) -> list[str]:
    """The `pelican token create` invocation, shared by the real + dryrun issuers
    so the simulated log matches what production runs."""
    return [
        binary, "token", "create", object_uri, "--write",
        "--issuer", issuer, "-k", private_key, "--lifetime", str(ttl_seconds),
    ]


# Sync file helpers run via asyncio.to_thread so the async issuer never blocks
# the event loop on disk I/O (ASYNC230).
def _open_write(path: str):
    return open(path, "wb")


def _read_text(path: str) -> str:
    with open(path) as fh:
        return fh.read()


class LoggingDryRunTokenIssuer(TokenIssuer):
    """Simulation: logs the exact `pelican token create` command it WOULD run
    (issuer key not ready yet) and returns a clearly-marked synthetic token. No
    JWKS. Swap to PelicanCliTokenIssuer once the issuer credential exists."""

    def __init__(self, settings: Settings | None = None):
        self.binary = settings.pelican_token_binary if settings else "pelican"
        self.issuer = (
            (settings.pelican_token_issuer if settings else None)
            or "<PELICAN_TOKEN_ISSUER>"
        )
        self.private_key = (
            (settings.pelican_token_private_key if settings else None)
            or "<PELICAN_TOKEN_PRIVATE_KEY>"
        )

    async def issue(
        self, *, subject: str, object_uri: str, ttl_seconds: int
    ) -> IssuedToken:
        argv = _pelican_argv(
            self.binary, object_uri, self.issuer, self.private_key, ttl_seconds
        )
        log.info("SIMULATE token issuance — would run: %s", " ".join(argv))
        ref = uuid.uuid4().hex
        jti = uuid.uuid4().hex
        exp = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
        return IssuedToken(
            token=f"SIMULATED-TOKEN.{jti}",
            token_ref=ref,
            expires_at=exp,
            claim_jti=jti,
        )


class PelicanCliTokenIssuer(TokenIssuer):
    """Production: mint a scoped write token by running

        pelican token create <object_uri> --write --issuer <issuer>
            -k <private_key> --lifetime <ttl>

    stdout (the token) is captured to /tmp/<uuid>_<ts>.token, then read back and
    returned (the file is removed afterward — it holds a live credential). The
    host needs the pelican CLI and the issuer's private key authorized to mint
    write tokens for the target namespace.
    """

    def __init__(self, settings: Settings):
        self.binary = settings.pelican_token_binary
        self.issuer = settings.pelican_token_issuer
        self.private_key = settings.pelican_token_private_key

    async def issue(
        self, *, subject: str, object_uri: str, ttl_seconds: int
    ) -> IssuedToken:
        # `pelican token create` has no timeout and no retry. If this stops
        # succeeding no OSPool job can upload anything, while /healthz keeps
        # reporting ok -- so every exit path below is counted by outcome.
        _t0 = time.monotonic()
        if not self.issuer:
            record_token_issue("not_configured")
            raise TokenIssuanceError("PELICAN_TOKEN_ISSUER is not configured")
        if not self.private_key:
            record_token_issue("not_configured")
            raise TokenIssuanceError("PELICAN_TOKEN_PRIVATE_KEY is not configured")

        argv = _pelican_argv(
            self.binary, object_uri, self.issuer, self.private_key, ttl_seconds
        )
        out_path = os.path.join(
            "/tmp", f"{uuid.uuid4().hex}_{int(time.time())}.token"
        )
        token = ""
        try:
            out_fh = await asyncio.to_thread(_open_write, out_path)
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv, stdout=out_fh, stderr=asyncio.subprocess.PIPE,
                )
            except FileNotFoundError as e:
                record_token_issue("cli_missing", time.monotonic() - _t0)
                raise TokenIssuanceError(
                    f"pelican CLI not found: {self.binary}"
                ) from e
            finally:
                out_fh.close()  # child inherited its own fd; close the parent's
            _, err = await proc.communicate()
            if proc.returncode != 0:
                record_token_issue("nonzero_exit", time.monotonic() - _t0)
                raise TokenIssuanceError(
                    f"pelican token create exit {proc.returncode}: "
                    f"{err.decode(errors='replace').strip()[:200]}"
                )
            token = (await asyncio.to_thread(_read_text, out_path)).strip()
        finally:
            with contextlib.suppress(OSError):
                os.unlink(out_path)

        if not token:
            record_token_issue("empty_output", time.monotonic() - _t0)
            raise TokenIssuanceError("pelican token create produced no token")

        record_token_issue("ok", time.monotonic() - _t0)

        ref = uuid.uuid4().hex
        claims = _decode_unverified(token)
        if claims and "exp" in claims:
            exp = datetime.fromtimestamp(int(claims["exp"]), UTC)
        else:
            exp = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
        jti = claims.get("jti") if claims else None
        return IssuedToken(
            token=token, token_ref=ref, expires_at=exp, claim_jti=jti
        )


class LocalSigningTokenIssuer(TokenIssuer):
    """Dev: self-sign a SciToken/WLCG-shaped JWT and publish JWKS.

    Structurally valid + fully unit-testable offline; a live origin only honors
    these once OSG registers our JWKS.
    """

    def __init__(self, settings: Settings):
        self._iss = settings.token_issuer_url
        self._aud = settings.token_audience
        self._kid = settings.token_kid
        self._priv = self._load_or_generate_key(settings)
        if isinstance(self._priv, rsa.RSAPrivateKey):
            self._alg = "RS256"
            self._jwk_algo = jwt.algorithms.RSAAlgorithm
        elif isinstance(self._priv, ec.EllipticCurvePrivateKey):
            self._alg = "ES256"
            self._jwk_algo = jwt.algorithms.ECAlgorithm
        else:  # pragma: no cover - defensive
            raise ValueError("unsupported signing key type")

    @staticmethod
    def _load_or_generate_key(settings: Settings):
        from cryptography.hazmat.primitives import serialization

        pem: bytes | None = None
        if settings.token_signing_key:
            pem = settings.token_signing_key.replace("\\n", "\n").encode()
        elif settings.token_signing_key_file:
            with open(settings.token_signing_key_file, "rb") as fh:
                pem = fh.read()
        if pem is not None:
            return serialization.load_pem_private_key(pem, password=None)
        # No key configured: ephemeral RSA (dev only).
        return rsa.generate_private_key(public_exponent=65537, key_size=2048)

    async def issue(
        self, *, subject: str, object_uri: str, ttl_seconds: int
    ) -> IssuedToken:
        now = datetime.now(UTC)
        exp = now + timedelta(seconds=ttl_seconds)
        jti = uuid.uuid4().hex
        ref = uuid.uuid4().hex
        p = _scope_path(object_uri)
        payload = {
            "iss": self._iss,
            "sub": subject,
            "aud": self._aud,
            "scope": f"storage.create:{p} storage.modify:{p}",
            "jti": jti,
            "iat": int(now.timestamp()),
            "nbf": int(now.timestamp()),
            "exp": int(exp.timestamp()),
            "wlcg.ver": "1.0",
        }
        token = jwt.encode(
            payload, self._priv, algorithm=self._alg, headers={"kid": self._kid}
        )
        return IssuedToken(
            token=token, token_ref=ref, expires_at=exp, claim_jti=jti
        )

    def jwks(self) -> dict:
        pub = self._priv.public_key()
        jwk = json.loads(self._jwk_algo.to_jwk(pub))
        jwk.update({"kid": self._kid, "use": "sig", "alg": self._alg})
        return {"keys": [jwk]}


def build_token_issuer(settings: Settings) -> TokenIssuer:
    impl = settings.token_issuer_impl.lower()
    if impl == "stub":
        return StubTokenIssuer()
    if impl == "dryrun":
        return LoggingDryRunTokenIssuer(settings)
    if impl == "pelican":
        return PelicanCliTokenIssuer(settings)
    if impl == "local":
        return LocalSigningTokenIssuer(settings)
    if impl == "oauth2":  # pragma: no cover - v0.2
        raise NotImplementedError(
            "OAuth2ClientTokenIssuer is the v0.2 target (origin's OAuth2 issuer); "
            "not implemented in v0.1"
        )
    raise ValueError(f"unknown TOKEN_ISSUER_IMPL: {settings.token_issuer_impl!r}")
