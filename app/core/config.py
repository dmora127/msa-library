"""Application settings.

Env vars are the floor/default. Operationally-tuned values (max upload size,
token TTL, namespace template, enabled sources) can be overridden at runtime by
`app_settings` rows via SettingsService. API keys live ONLY here (env), never in
the DB, never logged.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Database (Supabase service-role / direct Postgres DSN; bypasses RLS by design)
    database_url: str = "postgresql+asyncpg://af3:af3@localhost:5432/af3cache"

    # Job auth: comma-separated set of accepted API keys (rotation-friendly).
    # Kept during the JWT migration window — /v1/uploads/* accepts EITHER a
    # valid API key OR a valid WLCG bearer token. Torn down once jobs migrate.
    api_keys: str = "dev-key-change-me"
    admin_api_key: str | None = None

    # Inbound WLCG-profile JWT (SciToken) auth for /v1/uploads/* (see
    # app/core/scitokens.py). Static pinned key, no issuer discovery.
    #   trusted_issuers            comma-separated `iss` allowlist (empty = deny)
    #   token_bearer_audience      required `aud` value the token must carry
    #   upload_required_scope      WLCG <action>:<path> the token must grant
    #   jwt_allowed_algs           signature alg allowlist (ES256 only for now)
    #   jwt_public_key_file        PEM or JWKS file; unset/unreadable = JWT off
    #   jwt_clock_skew_seconds     symmetric drift tolerance (nbf/iat/exp)
    #   jwt_allowed_past_exp_seconds  one-sided grace ACCEPTING tokens N seconds
    #                              past a genuine exp (default 0 = strict)
    trusted_issuers: str = ""
    token_bearer_audience: str = "https://af3-library.osg-htc.org"
    upload_required_scope: str = "write:/community-contributed"
    jwt_allowed_algs: str = "ES256"
    jwt_public_key_file: str | None = None
    jwt_clock_skew_seconds: int = 60
    jwt_allowed_past_exp_seconds: int = 0

    # Token issuer: pelican (prod: `pelican token create`) | local (self-sign,
    # dev) | stub (tests) | oauth2 (v0.2)
    token_issuer_impl: str = "pelican"
    token_issuer_url: str = "https://af3-cache.local/"
    token_audience: str = "https://osdf-origin.example/"
    token_kid: str = "af3-cache-v1"
    token_signing_key: str | None = None
    token_signing_key_file: str | None = None
    pelican_token_binary: str = "pelican"
    # `pelican` issuer (TOKEN_ISSUER_IMPL=pelican): the API runs
    #   pelican token create <object_uri> --write --issuer <pelican_token_issuer>
    #       -k <pelican_token_private_key> --lifetime <token_ttl_seconds>
    # Both must be set (and the key file readable inside the container) for the
    # pelican issuer to mint tokens.
    pelican_token_issuer: str | None = None
    pelican_token_private_key: str | None = None

    # Pelican object-store namespace(s) uploads are written to. The live
    # deployment uses the single OSDF namespace `af3-msa-library` (the one
    # PELICAN_TOKEN_ISSUER mints for). Listing more than one load-balances:
    # they are independent stores (no sharding/replication) and one is picked
    # at random per upload. Only consumed where osdf_namespace_template
    # contains `{namespace}` — the default template hardcodes the namespace.
    pelican_store_namespaces: str = "af3-msa-library"

    # OSDF client (upload-complete fetch+verify, reaper object rm).
    osdf_client_impl: str = "pelican"  # pelican | dryrun | stub
    # pelican CLI used for `object get`/`object rm` (needs OSDF read creds, and
    # write/delete creds for the reaper). Usually the same binary as
    # pelican_token_binary; both must exist INSIDE the api container.
    osdf_pelican_binary: str = "pelican"
    # Cap on concurrent pelican `object get` subprocesses (directread is heavy
    # on the origin and each verify holds CPU + a /tmp file). Excess /complete
    # requests queue at the semaphore; if they wait longer than
    # verify_queue_timeout_seconds they get 503 osdf_unavailable (retryable —
    # the ingestion row stays 'uploaded').
    verify_concurrency: int = 4
    verify_queue_timeout_seconds: float = 60.0

    # Sequence classifier (verify pipeline adds species + query_name):
    # `noop` (default; everything Unclassified — keeps the pipeline working
    # without diamond installed), `stub` (tests), `diamond` (real blastp).
    classifier_impl: str = "noop"
    diamond_binary: str = "diamond"
    diamond_db: str | None = None
    diamond_timeout_seconds: float = 60.0
    # Match the DiamondClassifier command — diamond enforces id+coverage at
    # the source; we re-check evalue strictly less than the threshold.
    diamond_id_threshold: float = 99.99
    diamond_query_cover: float = 100.0
    diamond_evalue_threshold: float = 0.0005

    # --- MMseqs2 similarity search (POST /v2/query similarity_search=true) ---
    # Opt-in neighbour lookup after an exact miss. All env-only (no
    # app_settings override). See app/services/similarity/.
    similarity_search_enabled: bool = True
    # ephemeral: dump msa_sequences + build + index + search per request, then
    #            delete the workspace (active by default).
    # snapshot:  search the indexed nightly generation + the unindexed hot delta
    #            under similarity_db_root (built by the admin build endpoints).
    similarity_db_mode: str = "ephemeral"
    similarity_db_root: str = "/var/lib/af3cache/similarity"
    # Parent for per-request ephemeral workspaces; None = system temp dir.
    similarity_tmp_root: str | None = None
    # Run `mmseqs createindex` on the per-request ephemeral target. Spec
    # default; flip to false if benchmarks show search-time indexing is cheaper
    # at the current table size.
    similarity_ephemeral_index: bool = True
    similarity_mmseqs_binary: str = "mmseqs"
    similarity_mmseqs_threads: int = 4
    similarity_mmseqs_prefilter_mode: int = 1
    similarity_mmseqs_alignment_mode: int = 3
    similarity_mmseqs_max_candidates: int = 300  # --max-seqs
    # Passed to createindex + search when set (e.g. "1G"); bounds mmseqs RAM
    # inside the container's mem_limit.
    similarity_mmseqs_split_memory_limit: str | None = None
    similarity_mmseqs_timeout_seconds: float = 300.0  # per step, request path
    similarity_build_timeout_seconds: float = 3600.0  # per step, builders
    # Thresholds a hit must meet. Identity is 0-100, coverages are 0-1 and are
    # enforced SEPARATELY (query->target and target->query).
    similarity_min_percent_identity: float = 90.0
    similarity_min_query_coverage: float = 0.9
    similarity_min_target_coverage: float = 0.9
    similarity_max_hits: int = 5
    # Bounded concurrency for similarity searches (each ephemeral search dumps
    # the whole active table and runs mmseqs). A semaphore, not a lock:
    # workspaces stay independent. Excess callers wait up to the queue timeout
    # then get 503 similarity_unavailable. 0 = unlimited.
    similarity_max_concurrent: int = 4
    similarity_queue_timeout_seconds: float = 30.0
    # Snapshot-mode builders. Both DISABLED by default; the admin endpoints
    # return 409 similarity_build_disabled until these are true.
    similarity_nightly_build_enabled: bool = False
    similarity_nightly_retention_minutes: int = 60
    similarity_delta_build_enabled: bool = False
    similarity_delta_refresh_minutes: int = 60  # cron cadence; reported by /status
    similarity_delta_retention_minutes: int = 60

    # Adjustable operational defaults (overridable via app_settings)
    max_upload_size_bytes: int = 104_857_600
    token_ttl_seconds: int = 1200
    osdf_namespace_template: str = (
        "osdf:///af3-msa-library/community-contributed/{ingestion_id}.a3m"
    )
    reap_grace_hours: int = 12
    # Coarsen the read-side last_accessed_at bump: only update when the stored
    # value is older than this, bounding write amplification on hot sequences.
    access_bump_granularity_seconds: int = 86_400

    # v0.2 OAuth2 client issuer (unused while token_issuer_impl=local)
    oauth2_token_url: str | None = None
    oauth2_client_id: str | None = None
    oauth2_client_secret: str | None = None
    oauth2_audience: str | None = None
    oauth2_scope_template: str = "storage.create:/{path} storage.modify:/{path}"

    # Prometheus /metrics. Blocked at the edge (the Caddyfile 404s /metrics and
    # /readyz); Prometheus scrapes api:8000 directly over the compose network.
    # Setting this false removes the middleware from the stack entirely and
    # unmounts the endpoint -- an instant kill switch that needs only a restart,
    # not a rebuild.
    metrics_enabled: bool = True

    app_name: str = Field(default="af3-cache", exclude=True)

    @property
    def api_key_set(self) -> set[str]:
        return {k.strip() for k in self.api_keys.split(",") if k.strip()}

    @property
    def admin_key(self) -> str:
        if self.admin_api_key:
            return self.admin_api_key
        return next(iter(self.api_key_set), "")

    @property
    def trusted_issuer_set(self) -> set[str]:
        return {i.strip() for i in self.trusted_issuers.split(",") if i.strip()}

    @property
    def jwt_allowed_alg_set(self) -> set[str]:
        return {a.strip() for a in self.jwt_allowed_algs.split(",") if a.strip()}

    @property
    def pelican_store_list(self) -> list[str]:
        return [
            s.strip() for s in self.pelican_store_namespaces.split(",") if s.strip()
        ]


@lru_cache
def get_settings() -> Settings:
    return Settings()
