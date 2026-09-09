"""Test harness.

Runs against a real Postgres (docker-compose) for constraint/enum/RLS
fidelity. A dedicated `af3cache_test` database is dropped, recreated, and
migrated once per session; each test runs inside a transaction that is rolled
back, so tests are isolated and fast.

The DATABASE_URL env var is set BEFORE app modules import so the lru_cached
Settings pick up the test DSN.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import tempfile
from collections.abc import AsyncGenerator

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

TEST_DB = "af3cache_test"
# Host/port of the docker-compose test Postgres. Overridable so the suite can
# run on a box where 5432 is already taken (e.g. the production VM, where the
# prod db is bound to 127.0.0.1:5432 with a different password):
#   docker run -d --name af3cache-test-pg -p 127.0.0.1:5433:5432 \
#     -e POSTGRES_USER=af3 -e POSTGRES_PASSWORD=af3 -e POSTGRES_DB=af3cache postgres:16
#   AF3_TEST_PG_PORT=5433 uv run pytest -q
_PG_HOST = os.environ.get("AF3_TEST_PG_HOST", "localhost")
_PG_PORT = os.environ.get("AF3_TEST_PG_PORT", "5432")
_ADMIN_DSN = f"postgresql://af3:af3@{_PG_HOST}:{_PG_PORT}/postgres"
TEST_DATABASE_URL = f"postgresql+asyncpg://af3:af3@{_PG_HOST}:{_PG_PORT}/{TEST_DB}"

# Inbound-JWT test config. A single ES256 (P-256) keypair is generated once per
# session; its PUBLIC half is written to a temp PEM that the app loads as the
# pinned verification key, and the PRIVATE half signs tokens via the
# `mint_token` fixture. Env is set at import time — BEFORE any app module
# imports get_settings() — so the pinned-key path is picked up.
TEST_ISSUER = "https://ap40.uw.osg-htc.org"
TEST_AUDIENCE = "https://af3-library.osg-htc.org"
TEST_SCOPE = "write:/community-contributed"
_EC_PRIVATE_KEY = ec.generate_private_key(ec.SECP256R1())
_PUB_PEM = _EC_PRIVATE_KEY.public_key().public_bytes(
    serialization.Encoding.PEM,
    serialization.PublicFormat.SubjectPublicKeyInfo,
)
_PUB_KEY_FILE = pathlib.Path(tempfile.mkdtemp()) / "ap40-pub.pem"
_PUB_KEY_FILE.write_bytes(_PUB_PEM)

os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ.setdefault("API_KEYS", "test-key")
os.environ.setdefault("ADMIN_API_KEY", "admin-test-key")
os.environ.setdefault("TOKEN_ISSUER_IMPL", "stub")
os.environ.setdefault("OSDF_CLIENT_IMPL", "stub")
os.environ.setdefault("TRUSTED_ISSUERS", TEST_ISSUER)
os.environ.setdefault("TOKEN_BEARER_AUDIENCE", TEST_AUDIENCE)
os.environ.setdefault("UPLOAD_REQUIRED_SCOPE", TEST_SCOPE)
os.environ.setdefault("JWT_PUBLIC_KEY_FILE", str(_PUB_KEY_FILE))


async def _recreate_database() -> None:
    import asyncpg

    conn = await asyncpg.connect(_ADMIN_DSN)
    try:
        await conn.execute(
            f"select pg_terminate_backend(pid) from pg_stat_activity "
            f"where datname = '{TEST_DB}' and pid <> pg_backend_pid()"
        )
        await conn.execute(f'drop database if exists "{TEST_DB}"')
        await conn.execute(f'create database "{TEST_DB}"')
    finally:
        await conn.close()


def _run_migrations() -> None:
    from alembic import command
    from alembic.config import Config

    from app.core.config import get_settings

    get_settings.cache_clear()
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")


@pytest.fixture(scope="session", autouse=True)
def _migrated_db():
    asyncio.run(_recreate_database())
    _run_migrations()
    yield


@pytest.fixture
async def db_session() -> AsyncGenerator:
    """A session on a connection whose outer transaction is rolled back."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.db.base import make_engine

    engine = make_engine(TEST_DATABASE_URL)
    conn = await engine.connect()
    trans = await conn.begin()
    maker = async_sessionmaker(
        bind=conn, expire_on_commit=False, class_=AsyncSession,
        join_transaction_mode="create_savepoint",
    )
    session = maker()
    try:
        yield session
    finally:
        await session.close()
        await trans.rollback()
        await conn.close()
        await engine.dispose()


@pytest.fixture
async def client(db_session) -> AsyncGenerator:
    """ASGI client with the request session bound to the rolled-back db_session."""
    import httpx

    from app.db.session import get_session
    from app.main import app
    from app.services.deps import get_background_verifier

    async def _override():
        yield db_session

    app.dependency_overrides[get_session] = _override
    # Background upload verification runs INLINE in tests (awaited inside the
    # request) on the same rolled-back session, so a POST /complete's DB
    # outcome is observable right after the response. The real @lru_cache
    # verifier must never schedule a task here: each test has its own event
    # loop and a task left on a closed loop fails the suite via
    # filterwarnings=error.
    app.dependency_overrides[get_background_verifier] = lambda: _inline_verifier(
        db_session
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c
    app.dependency_overrides.clear()


def _inline_verifier(db_session):
    """One BackgroundVerifier per test, memoised on the session so the route
    and the `inline_verifier` fixture see the same instance."""
    from contextlib import asynccontextmanager

    from app.services.upload_verifier import BackgroundVerifier

    v = getattr(db_session, "_af3_inline_verifier", None)
    if v is None:

        @asynccontextmanager
        async def _same_session():
            yield db_session  # never closed here: the db_session fixture owns it

        v = BackgroundVerifier(_same_session, inline=True)
        db_session._af3_inline_verifier = v
    return v


@pytest.fixture
def inline_verifier(client, db_session):
    """The inline BackgroundVerifier the `client` fixture installed, for tests
    that want to spy on / replace `submit`."""
    return _inline_verifier(db_session)


NS_ISSUER = "https://issuer.test/api/v1.0/issuer/ns/af3-msa-library"


@pytest.fixture
def namespaces_yaml(tmp_path):
    """Factory writing a namespaces.yaml plus fake key files under tmp_path.

    `_write(entries=None, *, min_headroom_pct=None, filename=...)` returns the
    file path. Entries default to the single live namespace; an entry may omit
    `private_key` to get a generated fake key, or set it to a path of its own.
    """
    import yaml

    def _key(name: str) -> str:
        k = tmp_path / f"{name}.pem"
        if not k.exists():
            k.write_text("-----BEGIN FAKE KEY-----\nnot-a-real-key\n-----END FAKE KEY-----\n")
        return str(k)

    def _write(entries=None, *, min_headroom_pct=None, filename="namespaces.yaml"):
        if entries is None:
            entries = [{"namespace": "af3-msa-library", "issuer": NS_ISSUER,
                        "capacity_tb": 50}]
        out = []
        for e in entries:
            e = dict(e)
            e.setdefault("private_key", _key(e["namespace"].replace("/", "_")))
            out.append(e)
        doc = {"namespaces": out}
        if min_headroom_pct is not None:
            doc["min_headroom_pct"] = min_headroom_pct
        path = tmp_path / filename
        path.write_text(yaml.safe_dump(doc, sort_keys=False))
        return path

    _write.key = _key
    return _write


@pytest.fixture
def registry_override(namespaces_yaml):
    """Install a NamespaceRegistry (built from `entries`) as the app's process
    registry + capacity gate for one test. Returns the registry."""
    from app.core.namespaces import load_namespaces
    from app.main import app
    from app.services.deps import get_capacity_gate, get_namespace_registry
    from app.services.namespace_capacity import NamespaceCapacityGate

    def _install(entries=None, *, min_headroom_pct=None, ttl_seconds=60):
        reg = load_namespaces(
            namespaces_yaml(entries, min_headroom_pct=min_headroom_pct), required=True
        )
        gate = NamespaceCapacityGate(reg, ttl_seconds)
        app.dependency_overrides[get_namespace_registry] = lambda: reg
        app.dependency_overrides[get_capacity_gate] = lambda: gate
        return reg

    yield _install
    app.dependency_overrides.pop(get_namespace_registry, None)
    app.dependency_overrides.pop(get_capacity_gate, None)


@pytest.fixture
def mint_token():
    """Factory minting WLCG-profile ES256 tokens signed by the session keypair.

    Defaults produce a token that passes the validator; override any field to
    exercise a rejection path. `signing_key=<other ec key>` forges a bad
    signature; `alg="none"` builds an unsigned token.
    """
    import time
    import uuid

    import jwt

    def _mint(
        *,
        scope: str = TEST_SCOPE,
        aud=TEST_AUDIENCE,
        iss: str = TEST_ISSUER,
        sub: str = "danny.morales",
        wlcg_ver: str | None = "1.0",
        exp_delta: int = 1200,
        nbf_delta: int = 0,
        iat_delta: int = 0,
        include_iat: bool = True,
        alg: str = "ES256",
        kid: str | None = "6804",
        signing_key=None,
    ) -> str:
        now = int(time.time())
        payload: dict = {
            "sub": sub,
            "scope": scope,
            "aud": aud,
            "iss": iss,
            "nbf": now + nbf_delta,
            "exp": now + exp_delta,
            "jti": str(uuid.uuid4()),
        }
        if include_iat:
            payload["iat"] = now + iat_delta
        if wlcg_ver is not None:
            payload["wlcg.ver"] = wlcg_ver
        headers = {"kid": kid} if kid is not None else {}
        key = "" if alg == "none" else (signing_key or _EC_PRIVATE_KEY)
        return jwt.encode(payload, key, algorithm=alg, headers=headers)

    return _mint
