# af3-cache

FastAPI + PostgreSQL API for the **AlphaFold3 MSA alignment cache**.

AF3 on CHTC/OSPool recomputes every protein chain's unpaired MSA from scratch
against ~750 GB of reference databases — hours per job, even for sequences
MSA'd thousands of times before. This service lets OSPool jobs **look up** a
pre-computed `unpairedMsa` by sequence and, on a miss, **contribute** their
freshly computed `.a3m` back into the cache safely.

It is the **server side**. The read-only job-side client is specified
separately in `tutorial-CHTC-AF3/plan.md` (a stdlib helper that talks to
Supabase PostgREST directly). This repo **reuses that schema verbatim** and
**adds the write-back ingestion pipeline** that the client plan deferred:
scoped OSDF write-token issuance, integrity + format verification, and
publishing community-contributed MSAs.

## Architecture

```
OSPool job ──HTTP──> FastAPI (this repo) ──> PostgreSQL (Supabase / docker)
                          │
                          ├─ TokenIssuer   (pelican token create; local/stub/oauth2)
                          ├─ OsdfClient    (pelican CLI; stub in tests)
                          ├─ A3mValidator  (lenient .a3m sniff)
                          └─ SimilarityService (mmseqs2; ephemeral | snapshot targets)
```

- `POST /v1/query` — sequence/hash → cached hits (OSDF URL + metadata) or
  `found:false`. Reads the `msa_cache_lookup` **view**, so contributed entries
  surface identically to OSG/GDM/etc.
- `POST /v1/uploads/request` — declared size/sha → refuses oversize **before**
  minting a scoped, time-limited write token; creates a `pending` ingestion.
- `POST /v1/uploads/{id}/complete` — fetches the object from OSDF, re-verifies
  sha256 + size + `.a3m` format + that the a3m's query hashes to the declared
  sequence, then publishes it as source **`Community Contributed - Cached`**.
- `GET /.well-known/jwks.json` — public keys, mounted **only** while we
  self-sign (gone once the origin's OAuth2 issuer is used).
- `GET|PUT /v1/admin/config` — hot-adjustable max upload size, token TTL, OSDF
  namespace template, reap grace hours. Never returns/accepts API keys.
- `GET|PUT /v1/admin/sources` — list / toggle `msa_sources.enabled`
  (query visibility; the community source's flag also gates job uploads).
- `POST /v1/admin/maintenance/run` — idle sweep + reaper (cron/scheduler hits
  this). Admin-key gated.
- `POST /v2/query` — **experimental**: `/v1/query` plus an opt-in
  `"similarity_search": true`. On an exact miss it runs an MMseqs2 search over
  every sequence with an active cache entry and returns near-identical
  neighbours (identity ≥ 90 %, query AND target coverage ≥ 0.9 by default)
  through the same metadata path. Similar results carry `match_type:
  "similar"` and a per-hit `similarity` block (`percent_identity` 0–100,
  `query_coverage`/`target_coverage` 0–1, alignment/query/target lengths,
  bitscore, evalue); `hit.seq_hash` is the neighbour's, `result.seq_hash` the
  query's. Exact hits gain only `match_type: "exact"`; misses are byte-identical
  to v1. Batch `seq_hashes` + the flag → 422. Merged back into v1 once proven.
- `GET /v1/admin/similarity/status`, `POST /v1/admin/similarity/nightly-build`,
  `POST /v1/admin/similarity/delta-refresh` — snapshot-mode target builders
  (indexed nightly generation + hot delta, atomic symlink promotion, retention).
  Implemented but **disabled** until `SIMILARITY_*_BUILD_ENABLED=true`; host
  cron drives them (`deploy/jetstream2/helpers/similarity_*.sh`). The default
  `SIMILARITY_DB_MODE=ephemeral` rebuilds the target per request instead.
- `POST /v1/admin/classification/jobs/submit` — NR-BLAST reclassification
  hand-off, on demand or from cron: exports every sequence with an active entry
  still marked `Pending Classification` (≥ `RECLASS_EXPORT_MIN_LENGTH` aa;
  body `{min_length?, max_length?, limit?}`), scp's it to the BLAST box, runs
  `reclass-submit` there over ssh and records the job. `POST …/jobs/poll`
  stores `reclass-status` for every QUEUED/RUNNING job (no ssh when none is
  active); `GET …/jobs[?active=true]` lists them;
  `GET …/pending.fa` is the export alone (`>{sequence_id}|{seq_hash}`).
  Results come back per sequence through `POST /v1/admin/reclassify`. The
  container needs the dedicated deploy key + pinned known_hosts mounts from
  `docker-compose.api.yml`; `helpers/reclassify_{export,poll}.sh` are curl
  wrappers for cron.
- `GET /healthz` — DB ping, no auth.

### Retention & identity

A cached entry is identified by `(sequence, source, db_version)` — the job
declares the reference-DB bundle version it used; two db_versions of the same
seq+source coexist and `db_version` is returned in every hit (the job picks).
Retention is **access-based**: an entry not *queried* within its source's
`idle_ttl_days` (NULL = never cull) is swept to `stale` (immediately hidden
from queries); the reaper deletes the OSDF object only after `REAP_GRACE_HOURS`
of staleness (mid-read safety). Query hits refresh `last_accessed_at` with a
coarsened bump (≤1 write/day per hot entry). Upload-request refuses a
`(seq, source, db_version)` that already has a live entry with HTTP 409
`already_cached` (carrying the existing `osdf_uri`) so the job uses the cache
instead of recomputing; once stale it's contributable again. Objects are
immutable & uniquely named (`…/{seq_hash}/{source_slug}/{ingestion_id}.a3m`).

All `/v1/*` require a static API key (`X-API-Key:` or `Authorization: Bearer`).
`/v1/admin/*` requires the admin key.

## Token issuance

`TokenIssuer` is the swap boundary (`TOKEN_ISSUER_IMPL`) — issuer-agnostic
schema (`token_ref` / `token_claim_jti` / observed `token_expires_at`):

- **`pelican`** (default, production): on upload approval the app runs
  `pelican token create --write --scope-path pelican://<store>/<obj>
  --lifetime <token_ttl_seconds>`; stdout is the scoped write token handed to
  the job. The host needs the pelican CLI with a credential authorized to mint
  write tokens for the store namespaces.
- **`local`** (dev): self-sign a SciToken/WLCG JWT + publish JWKS; offline,
  unit-testable; honored by a live origin only once OSG registers the JWKS.
- **`stub`** (tests) · **`oauth2`** (v0.2 — origin's OAuth2 issuer; drop-in).

Uploads target the OSDF namespace in `PELICAN_STORE_NAMESPACES` (now the
single `af3-msa-library` — the namespace `PELICAN_TOKEN_ISSUER` mints for).
Listing several load-balances across them: independent stores, one picked at
random per upload (no sharding/replication). Object URLs come from
`OSDF_NAMESPACE_TEMPLATE`, live value
`osdf:///af3-msa-library/community-contributed/{ingestion_id}.a3m`, stored and
returned to jobs as-is. A template may interpolate `{namespace}` (the picked
store), `{seq_hash}`, `{source_slug}` and `{ingestion_id}`; note that a
`pelican://<host>/…` URL resolves `<host>` as a *federation hostname*, so a
bare namespace name belongs in the path, as the `osdf:///` form does.

## Deployment

Runs on a **Jetstream2** cloud VM (NSF/ACCESS academic cloud, OpenStack /
Exosphere). Everything is Docker (compose project `af3cache`); only Caddy is
public.

```
OSPool/CHTC job
   │  HTTPS  →  https://<api-host-ip>.sslip.io
   ▼
Jetstream2 VM  (floating IP <api-host-ip>, attached volume for DB data)
   Caddy  (:80 / :443 — the only public service)
     ├─ /webdbinterface*  → pgweb:8081   (admin DB console, basic-auth)
     └─ /*                → api:8000      (FastAPI / uvicorn, ASGI)
                                 └─ db:5432  (Postgres 16, 127.0.0.1-only, on the volume)
```

- **Jetstream2** — the host: one Ubuntu VM with a floating public IP and an
  attached volume holding Postgres data + alignment staging. Postgres binds to
  localhost only and is never internet-exposed.
- **Caddy** — the public edge, and the only container publishing ports
  (80/443). It terminates HTTPS and reverse-proxies to the internal API
  (`api:8000`) and the DB console (`/webdbinterface` → pgweb). Jobs only ever
  talk to Caddy.
- **sslip.io** — DNS without owning a domain: `<ip>.sslip.io` resolves to
  `<ip>`, so `<api-host-ip>.sslip.io` points at the VM's floating IP — a
  hostname for TLS/URLs with zero DNS setup.

TLS is currently Caddy's **internal CA** (self-signed): sslip.io is a shared
domain that Let's Encrypt rate-limits, so clients pass `curl -k` / trust the
local CA. A real domain flips Caddy to automatic Let's Encrypt via `PUBLIC_HOST`
+ `CADDY_TLS` with no other change. Full runbook + cutover steps:
`deploy/jetstream2/README-phase2.md`.

## Quick start (local)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh        # if needed
docker compose up -d                                    # Postgres 16
cp .env.example .env                                    # tweak as needed
uv sync
uv run alembic upgrade head                             # migrations 0001..0008
uv run uvicorn app.main:app --reload
```

```bash
KEY=dev-key-change-me
# no-hit
curl -s localhost:8000/v1/query -H "X-API-Key: $KEY" \
  -d '{"sequence":"MKTAYIAK"}' | jq         # -> found:false
# upload-request -> scoped token + osdf target (413 if declared size too big)
curl -s localhost:8000/v1/uploads/request -H "X-API-Key: $KEY" \
  -d '{"sequence":"MKTAYIAKQR","declared_size_bytes":500,
       "declared_sha256":"<64-hex>"}' | jq
# JWKS (present only with TOKEN_ISSUER_IMPL=local)
curl -s localhost:8000/.well-known/jwks.json | jq
```

## Tests

Run against real Postgres (docker-compose) for constraint/enum/RLS fidelity. A
dedicated `af3cache_test` DB is recreated + migrated once per session; each
test is rolled back.

```bash
docker compose up -d
uv run pytest -q          # 79 tests
uv run ruff check .
```

## Pointing at a real Supabase project

Use the **service-role / direct Postgres** connection string (the server
intentionally bypasses RLS for writes; this DSN is the crown-jewel secret —
env-only, gitignored):

```bash
DATABASE_URL=postgresql+asyncpg://postgres:<pw>@db.<ref>.supabase.co:5432/postgres
```

- **Fresh project:** `alembic upgrade head` creates everything.
- **Project that already has plan.md's schema** (created via the SQL Editor):
  `alembic stamp 0001` (mark the base as applied without re-running it), then
  `alembic upgrade head` to apply only `0002`–`0008`.

Migration `0001` reproduces plan.md's tables/view/RLS verbatim plus an
idempotent `anon` role + explicit `anon` SELECT grants (no-op on Supabase,
required on vanilla Postgres). The existing `tutorial-CHTC-AF3` client keeps
working unmodified and additionally sees `Community Contributed - Cached`
entries through the unchanged `msa_cache_lookup` view.

## Environment

See `.env.example`. Key vars: `DATABASE_URL`, `API_KEYS` (comma-separated set,
rotation-friendly), `ADMIN_API_KEY`, `TOKEN_ISSUER_IMPL`, `OSDF_CLIENT_IMPL`,
`MAX_UPLOAD_SIZE_BYTES`, `TOKEN_TTL_SECONDS` (1200), `OSDF_NAMESPACE_TEMPLATE`,
`PELICAN_STORE_NAMESPACES`, `PELICAN_TOKEN_BINARY`,
`REAP_GRACE_HOURS`, `ACCESS_BUMP_GRANULARITY_SECONDS`, and the
`SIMILARITY_*` group (`SIMILARITY_DB_MODE`, thresholds, `SIMILARITY_DB_ROOT`,
`SIMILARITY_MMSEQS_*`, `SIMILARITY_MAX_CONCURRENT`, the two `*_BUILD_ENABLED`
flags) documented in `.env.example`. Source enablement is not
a setting — it's `msa_sources.enabled`, managed via `GET|PUT /v1/admin/sources`
(query honors it; the community source must be enabled for jobs to upload).
Env is the floor; `app_settings` rows (via `/v1/admin/config`)
override the operational values at runtime. API keys are env-only — never in
the DB, never logged (a log filter redacts JWTs/keys).

## Status / open questions

v0.1 PoC: protein chains, single job-contributed source, `pelican token
create` issuance. Unresolved (per plan.md open Q#3): which OSDF origin/stores
host the blobs and the credential authorizing `pelican token create` for the
store namespaces — gates *live* token acceptance, not the API/schema. `local`
self-signing and `oauth2` (v0.2) remain drop-in via `TOKEN_ISSUER_IMPL`.
