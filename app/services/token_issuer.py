"""Scoped write-token issuance — the swap boundary.

`TokenIssuer.issue()` is issuer-agnostic so the impls are drop-in via
TOKEN_ISSUER_IMPL (no router/service/schema/DB change):

- pelican  (prod): shell out to `pelican token create <uri> --write
            --issuer <iss> --scope-path <path-sans-namespace> -k <key>
            --lifetime <ttl>`; stdout IS the token. issuer/key/scope come from
            the namespace registry (app/core/namespaces.py), never the director.
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
from app.core.namespaces import (
    EMPTY_REGISTRY,
    NamespaceRegistry,
    load_namespaces,
)

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
    binary: str,
    object_uri: str,
    *,
    issuer: str,
    private_key: str,
    scope_path: str,
    ttl_seconds: int,
) -> list[str]:
    """The `pelican token create` invocation, shared by the real + dryrun issuers
    so the simulated log matches what production runs.

    --issuer AND --scope-path together are what let pelican skip its director
    lookup (osdf-director, 10 s deadline): with both supplied it has nothing
    left to discover and goes straight to signing.
    """
    return [
        binary, "token", "create", object_uri, "--write",
        "--issuer", issuer, "-k", private_key,
        "--scope-path", scope_path, "--lifetime", str(ttl_seconds),
    ]


def _redacted(argv: list[str]) -> str:
    """argv for logs with the value after -k masked. The key PATH is not itself
    a secret, but masking keeps the line grep-safe if inline key material is
    ever passed instead."""
    out = list(argv)
    for i, a in enumerate(out[:-1]):
        if a == "-k":
            out[i + 1] = "<redacted>"
    return " ".join(out)


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

    def __init__(
        self,
        settings: Settings | None = None,
        namespaces: NamespaceRegistry | None = None,
    ):
        self.binary = settings.pelican_token_binary if settings else "pelican"
        if namespaces is not None:
            self.namespaces = namespaces
        elif settings is not None:
            # Absent file is fine in dryrun (the "start before creds exist"
            # workflow); a present-but-invalid file still raises.
            self.namespaces = load_namespaces(
                settings.pelican_namespaces_file, required=False
            )
        else:
            self.namespaces = EMPTY_REGISTRY

    async def issue(
        self, *, subject: str, object_uri: str, ttl_seconds: int
    ) -> IssuedToken:
        hit = self.namespaces.resolve(object_uri)
        if hit is not None:
            ns, scope_path = hit
            entry = self.namespaces.credentials_for(ns)
            issuer, key = entry.issuer, entry.private_key
        else:
            issuer, key, scope_path = (
                "<ISSUER for unconfigured namespace>", "<PRIVATE_KEY>", "<SCOPE_PATH>"
            )
        argv = _pelican_argv(
            self.binary, object_uri, issuer=issuer, private_key=key,
            scope_path=scope_path, ttl_seconds=ttl_seconds,
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
            --scope-path <path-sans-namespace> -k <private_key> --lifetime <ttl>

    stdout (the token) is captured to /tmp/<uuid>_<ts>.token, then read back and
    returned (the file is removed afterward — it holds a live credential).
    issuer / key / scope_path come from the namespace registry entry the object
    URI resolves to, so the container needs the pelican CLI plus every key the
    registry references.
    """

    def __init__(
        self, settings: Settings, namespaces: NamespaceRegistry | None = None
    ):
        self.binary = settings.pelican_token_binary
        # Direct construction (tests, or anything bypassing deps) still
        # validates: this is the startup gate for the pelican impl.
        self.namespaces = (
            namespaces
            if namespaces is not None
            else load_namespaces(settings.pelican_namespaces_file, required=True)
        )

    async def issue(
        self, *, subject: str, object_uri: str, ttl_seconds: int
    ) -> IssuedToken:
        # `pelican token create` has no timeout and no retry. If this stops
        # succeeding no OSPool job can upload anything, while /healthz keeps
        # reporting ok -- so every exit path below is counted by outcome AND
        # logged with pelican's stderr (a 503 alone told nobody why).
        _t0 = time.monotonic()
        hit = self.namespaces.resolve(object_uri)
        if hit is None:
            record_token_issue("not_configured")
            log.warning(
                "token issue FAILED not_configured: object uri %s matches no "
                "configured namespace (configured: %s)",
                object_uri, ", ".join(sorted(self.namespaces.entries)) or "-",
            )
            raise TokenIssuanceError(
                "no configured namespace for object uri; check PELICAN_NAMESPACES_FILE"
            )
        ns, scope_path = hit
        entry = self.namespaces.credentials_for(ns)  # disabled entries still mint
        argv = _pelican_argv(
            self.binary, object_uri, issuer=entry.issuer,
            private_key=entry.private_key, scope_path=scope_path,
            ttl_seconds=ttl_seconds,
        )
        log.debug("token issue: namespace=%s argv=%s", ns, _redacted(argv))
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
                elapsed = time.monotonic() - _t0
                record_token_issue("cli_missing", elapsed)
                log.warning(
                    "token issue FAILED cli_missing namespace=%s binary=%s "
                    "elapsed=%.2fs", ns, self.binary, elapsed,
                )
                raise TokenIssuanceError(
                    f"pelican CLI not found: {self.binary}"
                ) from e
            finally:
                out_fh.close()  # child inherited its own fd; close the parent's
            _, err = await proc.communicate()
            stderr = err.decode(errors="replace").strip()
            if proc.returncode != 0:
                elapsed = time.monotonic() - _t0
                record_token_issue("nonzero_exit", elapsed)
                log.warning(
                    "token issue FAILED nonzero_exit namespace=%s rc=%s "
                    "elapsed=%.2fs stderr=%r",
                    ns, proc.returncode, elapsed, stderr[:500],
                )
                raise TokenIssuanceError(
                    f"pelican token create exit {proc.returncode}: {stderr[:200]}"
                )
            token = (await asyncio.to_thread(_read_text, out_path)).strip()
        finally:
            with contextlib.suppress(OSError):
                os.unlink(out_path)

        if not token:
            elapsed = time.monotonic() - _t0
            record_token_issue("empty_output", elapsed)
            log.warning(
                "token issue FAILED empty_output namespace=%s rc=0 elapsed=%.2fs "
                "stderr=%r", ns, elapsed, stderr[:500],
            )
            raise TokenIssuanceError("pelican token create produced no token")

        elapsed = time.monotonic() - _t0
        record_token_issue("ok", elapsed)
        log.debug("token issue ok namespace=%s elapsed=%.2fs", ns, elapsed)

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


def build_token_issuer(
    settings: Settings, namespaces: NamespaceRegistry | None = None
) -> TokenIssuer:
    """`namespaces` is the process registry from deps; None makes the pelican /
    dryrun impls load it themselves from settings.pelican_namespaces_file."""
    impl = settings.token_issuer_impl.lower()
    if impl == "stub":
        return StubTokenIssuer()
    if impl == "dryrun":
        return LoggingDryRunTokenIssuer(settings, namespaces)
    if impl == "pelican":
        return PelicanCliTokenIssuer(settings, namespaces)
    if impl == "local":
        return LocalSigningTokenIssuer(settings)
    if impl == "oauth2":  # pragma: no cover - v0.2
        raise NotImplementedError(
            "OAuth2ClientTokenIssuer is the v0.2 target (origin's OAuth2 issuer); "
            "not implemented in v0.1"
        )
    raise ValueError(f"unknown TOKEN_ISSUER_IMPL: {settings.token_issuer_impl!r}")
