"""MMseqs2 similarity search: opt-in neighbour lookup after an exact miss.

Public surface used by the rest of the app:

    query_similarity(sequence) -> list[SimilarityHit]   (SimilarityService)
    build_nightly_similarity_database()                 (builders)
    refresh_delta_similarity_database()                 (builders)

Postgres (`msa_sequences` joined to active `msa_cache_entries`) is the source
of truth. Two target strategies, selected by SIMILARITY_DB_MODE:

    ephemeral  per request: dump -> createdb -> createindex -> search -> delete
    snapshot   indexed nightly generation + unindexed hot delta, both
               immutable, published under SIMILARITY_DB_ROOT by atomic
               symlink promotion; the request never rebuilds anything.
"""

from __future__ import annotations

from app.services.similarity.models import (
    Manifest,
    SimilarityConfig,
    SimilarityHit,
    Thresholds,
    build_similarity_config,
)
from app.services.similarity.service import (
    SimilarityRuntime,
    SimilarityService,
    SimilarityServiceBase,
    StubSimilarityService,
)

__all__ = [
    "Manifest",
    "SimilarityConfig",
    "SimilarityHit",
    "SimilarityRuntime",
    "SimilarityService",
    "SimilarityServiceBase",
    "StubSimilarityService",
    "Thresholds",
    "build_similarity_config",
]
