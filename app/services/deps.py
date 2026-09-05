"""Single dependency-injection wiring point.

Concrete service/impl selection lives here so tests can override one place and
the swappable interfaces (TokenIssuer, OsdfClient, ...) have exactly one switch.
"""

from __future__ import annotations

from functools import lru_cache

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import get_session
from app.services.a3m_validator import A3mValidator
from app.services.classifier import Classifier, build_classifier
from app.services.ingestion_service import IngestionService
from app.services.maintenance_service import MaintenanceService
from app.services.osdf_client import OsdfClient, build_osdf_client
from app.services.sequence_service import SequenceService
from app.services.settings_service import SettingsService
from app.services.similarity import (
    SimilarityRuntime,
    SimilarityService,
    SimilarityServiceBase,
    build_similarity_config,
)
from app.services.token_issuer import TokenIssuer, build_token_issuer


def get_sequence_service(
    session: AsyncSession = Depends(get_session),
) -> SequenceService:
    return SequenceService(session)


@lru_cache
def get_token_issuer() -> TokenIssuer:
    """Process-singleton: the local signer's key must stay stable across requests."""
    return build_token_issuer(get_settings())


def get_settings_service(
    session: AsyncSession = Depends(get_session),
) -> SettingsService:
    return SettingsService(session, get_settings())


@lru_cache
def get_osdf_client() -> OsdfClient:
    s = get_settings()
    return build_osdf_client(
        s.osdf_client_impl, s.osdf_pelican_binary,
        concurrency=s.verify_concurrency,
        queue_timeout=s.verify_queue_timeout_seconds,
    )


@lru_cache
def get_a3m_validator() -> A3mValidator:
    return A3mValidator()


@lru_cache
def get_classifier() -> Classifier:
    return build_classifier(get_settings())


@lru_cache
def get_similarity_runtime() -> SimilarityRuntime:
    """Process-singleton: holds the request semaphore and the build lock."""
    return SimilarityRuntime(build_similarity_config(get_settings()))


def get_similarity_service(
    session: AsyncSession = Depends(get_session),
    runtime: SimilarityRuntime = Depends(get_similarity_runtime),
) -> SimilarityServiceBase:
    return SimilarityService(session, runtime)


def get_ingestion_service(
    session: AsyncSession = Depends(get_session),
    token_issuer: TokenIssuer = Depends(get_token_issuer),
    settings_service: SettingsService = Depends(get_settings_service),
    osdf_client: OsdfClient = Depends(get_osdf_client),
    a3m_validator: A3mValidator = Depends(get_a3m_validator),
    sequence_service: SequenceService = Depends(get_sequence_service),
    classifier: Classifier = Depends(get_classifier),
) -> IngestionService:
    return IngestionService(
        session,
        token_issuer,
        settings_service,
        osdf_client,
        a3m_validator,
        sequence_service,
        classifier,
    )


def get_maintenance_service(
    session: AsyncSession = Depends(get_session),
    osdf_client: OsdfClient = Depends(get_osdf_client),
    settings_service: SettingsService = Depends(get_settings_service),
    classifier: Classifier = Depends(get_classifier),
) -> MaintenanceService:
    return MaintenanceService(session, osdf_client, settings_service, classifier)
