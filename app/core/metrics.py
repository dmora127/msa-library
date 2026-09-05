"""Prometheus metrics.

SINGLE-PROCESS ASSUMPTION
------------------------
The Dockerfile CMD is `uvicorn app.main:app` with NO `--workers`, so there is
exactly one process and the default global REGISTRY is correct. If anyone ever
adds `--workers N`, /metrics silently starts returning ONE RANDOM WORKER'S view
of the world -- counters will appear to jump backwards and rates will be wrong.
Fixing that needs PROMETHEUS_MULTIPROC_DIR + multiprocess.MultiProcessCollector.

NAMING
------
Everything is prefixed `af3cache_` to match what postgres-exporter already emits
(af3cache_entries_count, af3cache_a3m_bytes, ...), so `{__name__=~"af3cache_.*"}`
finds the whole system. Renaming a metric orphans its history -- don't.

WHAT LIVES WHERE
----------------
DB-shaped facts (row counts, storage totals) come from postgres-exporter's
custom queries, NOT from here: GET /v1/stats is 8 scalar subqueries over a
465k-row table and must never run on a scrape. This module holds only
request-shaped and subprocess-shaped facts the app alone can see.

CONTRACT
--------
Every recorder below is best-effort and swallows its own exceptions.
Instrumentation must never be able to fail a request. This mirrors the existing
house style -- see the SAVEPOINT-wrapped query_metrics bump in
app/api/routes_query.py, which the comment there explains at length.
"""

from __future__ import annotations

import logging

from prometheus_client import Counter, Gauge, Histogram

log = logging.getLogger("af3cache.metrics")

# Buckets run out to 120s on purpose: the long tail is /v1/uploads/{id}/complete
# blocking on a `pelican object get`, not ordinary API latency.
_REQ_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120)
# OSDF fetches are whole-object downloads; seconds-to-minutes is normal.
_FETCH_BUCKETS = (0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600)

# --- HTTP -------------------------------------------------------------------
# `route` is ALWAYS a route TEMPLATE ("/v1/uploads/{ingestion_id}/complete"),
# never a raw path. There are 423k+ ingestion ids; one series each would dwarf
# the entire rest of the stack. Unrouted requests collapse to "<unmatched>" so
# internet background noise (/wp-login.php, /.env, ...) cannot mint series.
HTTP_REQUESTS = Counter(
    "af3cache_http_requests_total",
    "HTTP requests by method, route template and status code.",
    ["method", "route", "status"],
)
HTTP_DURATION = Histogram(
    "af3cache_http_request_duration_seconds",
    "HTTP request duration by method and route template.",
    ["method", "route"],
    buckets=_REQ_BUCKETS,
)

# --- application errors -----------------------------------------------------
# Labelled by AppError.code, which already exists as a closed set:
# osdf_unavailable, token_issuance_failed, upload_too_large, source_not_enabled,
# ingestion_not_found, already_cached. Before this, AppErrors were logged
# NOWHERE -- a 503 storm was completely invisible.
APP_ERRORS = Counter(
    "af3cache_app_errors_total",
    "Typed application errors by AppError.code.",
    ["code"],
)

# --- cache effectiveness ----------------------------------------------------
QUERY_SEQUENCES = Counter(
    "af3cache_query_sequences_total",
    "Sequences looked up across all /v1/query requests (hit-ratio denominator).",
)
QUERY_HITS = Counter(
    "af3cache_query_hits_total",
    "Sequences that returned at least one hit (hit-ratio numerator).",
)
# /v1/query has no DB row/status like an ingestion -- it's one read + a
# best-effort metrics bump. This is the only signal for "requests currently
# being handled" (vs. HTTP_DURATION, which only reports after a request ends).
QUERY_INFLIGHT = Gauge(
    "af3cache_query_inflight",
    "Requests to /v1/query currently being handled.",
)

# --- OSDF verify ------------------------------------------------------------
OSDF_FETCH = Counter(
    "af3cache_osdf_fetch_total",
    "OSDF object fetches by outcome.",
    ["result"],  # ok | error | too_large | queue_timeout | cli_missing
)
OSDF_FETCH_SECONDS = Histogram(
    "af3cache_osdf_fetch_seconds",
    "Duration of `pelican object get`.",
    buckets=_FETCH_BUCKETS,
)
# THE POINT OF THESE TWO: asyncio.timeout() in OsdfClient.fetch covers only the
# semaphore acquire, NOT the fetch itself. A hung `pelican object get` holds a
# slot forever. inflight pinned at verify_concurrency with a zero fetch rate is
# the exact signature of that bug, and is otherwise completely unobservable.
OSDF_INFLIGHT = Gauge(
    "af3cache_osdf_verify_inflight",
    "Fetches currently running (compare against verify_concurrency).",
)
OSDF_WAITING = Gauge(
    "af3cache_osdf_verify_waiting",
    "Callers queued waiting for a verify slot.",
)

# --- token issuance ---------------------------------------------------------
# `pelican token create` has no timeout and no retry. If this stops succeeding,
# no OSPool job can upload anything -- and /healthz still reports ok.
TOKEN_ISSUE = Counter(
    "af3cache_token_issue_total",
    "Write-token issuance attempts by outcome.",
    ["result"],  # ok | not_configured | cli_missing | nonzero_exit | empty_output
)
TOKEN_ISSUE_SECONDS = Histogram(
    "af3cache_token_issue_seconds",
    "Duration of `pelican token create`.",
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
)

# --- classifier -------------------------------------------------------------
# DiamondClassifier returns UNCLASSIFIED on EVERY failure path and never raises,
# so a broken diamond is invisible over HTTP. This separates "no hit" (a real
# answer) from "binary missing / timed out / crashed" (a broken deployment).
CLASSIFY = Counter(
    "af3cache_classify_total",
    "Classification attempts by outcome.",
    ["result"],  # hit | unclassified | empty_input | binary_missing | timeout
                 # | nonzero_exit
)
CLASSIFY_SECONDS = Histogram(
    "af3cache_classify_seconds",
    "Duration of `diamond blastp`.",
    buckets=(0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 40, 60),
)

# --- similarity search (mmseqs2) --------------------------------------------
# The exact-match hit ratio above deliberately does NOT count similarity hits;
# this is where they show up. `result` separates a genuine no-neighbour answer
# from a broken deployment (binary missing / timeout / queue full).
SIMILARITY = Counter(
    "af3cache_similarity_total",
    "Similarity searches by outcome.",
    ["result"],  # hit | no_hit | empty_targets | disabled | queue_timeout
                 # | binary_missing | timeout | nonzero_exit | parse_error
                 # | db_missing
)
SIMILARITY_SECONDS = Histogram(
    "af3cache_similarity_seconds",
    "End-to-end duration of one similarity search (incl. ephemeral build).",
    buckets=_FETCH_BUCKETS,
)
SIMILARITY_STAGE_SECONDS = Histogram(
    "af3cache_similarity_stage_seconds",
    "Duration of one mmseqs/dump stage inside a similarity search or build.",
    ["stage"],  # dump | createdb | createindex | search | convertalis | version
    buckets=_FETCH_BUCKETS,
)
# Same rationale as OSDF_INFLIGHT/OSDF_WAITING: the queue timeout covers only
# the semaphore acquire, so a hung mmseqs pins inflight at max_concurrent.
SIMILARITY_INFLIGHT = Gauge(
    "af3cache_similarity_inflight",
    "Similarity searches currently running (compare against max_concurrent).",
)
SIMILARITY_WAITING = Gauge(
    "af3cache_similarity_waiting",
    "Callers queued waiting for a similarity slot.",
)
SIMILARITY_BUILD = Counter(
    "af3cache_similarity_build_total",
    "Nightly/delta similarity database builds by outcome.",
    ["build_type", "result"],  # ok | failed | disabled | in_progress
)
SIMILARITY_BUILD_SECONDS = Histogram(
    "af3cache_similarity_build_seconds",
    "Duration of a nightly/delta similarity database build.",
    ["build_type"],
    buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1200, 1800, 3600),
)


def _safe(fn):
    """Wrap a recorder so instrumentation can never raise into a request path."""

    def wrapper(*args, **kwargs):
        try:
            fn(*args, **kwargs)
        except Exception:  # noqa: BLE001 - deliberate: metrics must not break requests
            log.warning("metrics: %s failed", fn.__name__, exc_info=True)

    wrapper.__name__ = fn.__name__
    return wrapper


@_safe
def record_request(method: str, route: str, status: int, seconds: float) -> None:
    HTTP_REQUESTS.labels(method=method, route=route, status=str(status)).inc()
    HTTP_DURATION.labels(method=method, route=route).observe(seconds)


@_safe
def record_app_error(code: str) -> None:
    APP_ERRORS.labels(code=code).inc()


@_safe
def record_query(sequences: int, hits: int) -> None:
    QUERY_SEQUENCES.inc(sequences)
    QUERY_HITS.inc(hits)


@_safe
def record_osdf_fetch(result: str, seconds: float | None = None) -> None:
    OSDF_FETCH.labels(result=result).inc()
    if seconds is not None:
        OSDF_FETCH_SECONDS.observe(seconds)


@_safe
def record_token_issue(result: str, seconds: float | None = None) -> None:
    TOKEN_ISSUE.labels(result=result).inc()
    if seconds is not None:
        TOKEN_ISSUE_SECONDS.observe(seconds)


@_safe
def record_classify(result: str, seconds: float | None = None) -> None:
    CLASSIFY.labels(result=result).inc()
    if seconds is not None:
        CLASSIFY_SECONDS.observe(seconds)


@_safe
def record_similarity(result: str, seconds: float | None = None) -> None:
    SIMILARITY.labels(result=result).inc()
    if seconds is not None:
        SIMILARITY_SECONDS.observe(seconds)


@_safe
def record_similarity_stage(stage: str, seconds: float) -> None:
    SIMILARITY_STAGE_SECONDS.labels(stage=stage).observe(seconds)


@_safe
def record_similarity_build(
    build_type: str, result: str, seconds: float | None = None
) -> None:
    SIMILARITY_BUILD.labels(build_type=build_type, result=result).inc()
    if seconds is not None:
        SIMILARITY_BUILD_SECONDS.labels(build_type=build_type).observe(seconds)


class DbPoolCollector:
    """Reports SQLAlchemy pool occupancy at scrape time.

    A collector rather than a Gauge-with-callbacks because the pool is authoritative
    and there is nothing to update on a schedule -- we just read it when asked.

    Deliberately does NOT call get_engine(): that would CREATE the engine on the
    first scrape, defeating the lazy construction in app/db/base.py and opening a
    connection pool on a box that might only be serving /metrics. If no request has
    built the engine yet, this reports nothing, which is the honest answer.

    Compare af3cache_db_pool_checked_out against size + overflow: the pool is
    configured pool_size=20, max_overflow=40, so saturation is 60.
    """

    def collect(self):  # noqa: ANN201 - prometheus_client protocol
        from prometheus_client.core import GaugeMetricFamily

        try:
            from app.db import base as _base

            engine = _base._engine  # noqa: SLF001 - intentional: do not instantiate
            if engine is None:
                return
            pool = engine.sync_engine.pool
            checked_out = pool.checkedout()
            size = pool.size()
            overflow = pool.overflow()
        except Exception:  # noqa: BLE001 - a scrape must never raise
            log.warning("metrics: DbPoolCollector failed", exc_info=True)
            return

        g = GaugeMetricFamily(
            "af3cache_db_pool_checked_out",
            "Connections currently checked out of the SQLAlchemy pool.",
        )
        g.add_metric([], checked_out)
        yield g
        g = GaugeMetricFamily(
            "af3cache_db_pool_size", "Configured pool size (excludes overflow)."
        )
        g.add_metric([], size)
        yield g
        # CAREFUL: SQLAlchemy initialises _overflow to (0 - pool_size) and
        # increments it per connection CREATED, so overflow() is
        # (connections_created - pool_size). It is NEGATIVE until the pool has
        # grown to pool_size -- e.g. -19 with pool_size=20 means ONE connection
        # exists, not "negative nineteen overflow". Only values above 0 are
        # genuine overflow connections.
        g = GaugeMetricFamily(
            "af3cache_db_pool_overflow",
            "SQLAlchemy overflow counter: connections created minus pool_size. "
            "Negative until the pool has grown to pool_size; >0 means true overflow.",
        )
        g.add_metric([], overflow)
        yield g
        # The number most people actually want: connections currently open.
        g = GaugeMetricFamily(
            "af3cache_db_pool_connections",
            "Connections currently open (pool_size + overflow counter). "
            "Saturation is pool_size + max_overflow = 60.",
        )
        g.add_metric([], size + overflow)
        yield g
