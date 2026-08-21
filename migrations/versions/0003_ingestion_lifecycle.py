"""Ingestion lifecycle: msa_ingestions + status enum + concurrency guard.

Issuer-agnostic token bookkeeping (token_ref app-generated correlation id;
token_claim_jti optional; token_expires_at = OBSERVED expiry) so switching
self-sign -> OAuth2-client -> CLI needs no migration. RLS enabled with NO anon
policies => invisible to PostgREST; the read path's posture is unchanged.

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-15
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UPGRADE_STATEMENTS: list[str] = [
    """
    create type public.ingestion_status as enum
      ('pending','uploaded','verified','rejected','available')
    """,
    """
    create table public.msa_ingestions (
        id                   uuid primary key default gen_random_uuid(),
        status               public.ingestion_status not null default 'pending',
        seq_hash             text not null check (seq_hash ~ '^[0-9a-f]{64}$'),
        seq_length           integer not null,
        molecule_type        text not null default 'protein'
                               check (molecule_type in ('protein','rna','dna')),
        source               text not null references public.msa_sources(name),
        declared_size_bytes  bigint not null,
        declared_sha256      text not null check (declared_sha256 ~ '^[0-9a-f]{64}$'),
        observed_size_bytes  bigint,
        observed_sha256      text,
        observed_n_sequences integer,
        osdf_uri             text not null,
        token_ref            text not null unique,
        token_claim_jti      text,
        token_expires_at     timestamptz not null,
        requester            text,
        reject_reason        text,
        cache_entry_id       bigint references public.msa_cache_entries(id) on delete set null,
        created_at           timestamptz not null default now(),
        updated_at           timestamptz not null default now()
    )
    """,
    "create index msa_ingestions_status_idx  on public.msa_ingestions(status)",
    "create index msa_ingestions_seqhash_idx on public.msa_ingestions(seq_hash)",
    # concurrency guard: at most one in-flight ingestion per (seq_hash, source)
    """
    create unique index msa_ingestions_inflight_uniq
      on public.msa_ingestions(seq_hash, source)
      where status in ('pending','uploaded')
    """,
    # RLS on, NO anon policies => deny-by-default to PostgREST/anon
    "alter table public.msa_ingestions enable row level security",
]

DOWNGRADE_STATEMENTS: list[str] = [
    "drop table if exists public.msa_ingestions",
    "drop type if exists public.ingestion_status",
]


def upgrade() -> None:
    for stmt in UPGRADE_STATEMENTS:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE_STATEMENTS:
        op.execute(stmt)
