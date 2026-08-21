"""Base schema — verbatim from tutorial-CHTC-AF3/plan.md (lines 66-132).

Reproduces the read-only client PoC schema EXACTLY so the existing stdlib
`msa_cache_lookup.py` keeps working unmodified against the same DB:
tables msa_sequences / msa_cache_entries / msa_sources (+7 seed sources),
the msa_cache_lookup view (security_invoker, anon select grant), RLS, and the
three anon read policies.

The only addition is an idempotent `anon` role bootstrap: Supabase ships the
`anon` role already (the IF NOT EXISTS makes this a no-op there); vanilla
Postgres (local docker, CI) does not, and the plan.md grants/policies would
otherwise fail. On an existing Supabase project that already has this schema,
run `alembic stamp 0001` instead of upgrading.

Statements are executed one-per-call (asyncpg forbids multi-command prepared
statements); the SQL text itself is unchanged from plan.md.

Revision ID: 0001
Revises:
Create Date: 2026-05-15
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UPGRADE_STATEMENTS: list[str] = [
    # compat shim: ensure the anon role exists (no-op on Supabase)
    """
    do $$ begin
      if not exists (select from pg_roles where rolname = 'anon') then
        create role anon nologin;
      end if;
    end $$;
    """,
    # One row per unique normalized protein sequence
    """
    create table public.msa_sequences (
        id            bigserial primary key,
        seq_hash      text        not null unique
                                    check (seq_hash ~ '^[0-9a-f]{64}$'),
        sequence      text        not null,
        seq_length    integer     not null,
        molecule_type text        not null default 'protein'
                                    check (molecule_type in ('protein','rna','dna')),
        created_at    timestamptz not null default now()
    )
    """,
    "create index msa_sequences_seq_hash_idx on public.msa_sequences using hash (seq_hash)",
    # One row per (sequence, source) pair
    """
    create table public.msa_cache_entries (
        id              bigserial primary key,
        sequence_id     bigint      not null references public.msa_sequences(id) on delete cascade,
        source          text        not null,
        osdf_uri        text        not null,
        a3m_size_bytes  bigint,
        a3m_sha256      text        check (a3m_sha256 is null or a3m_sha256 ~ '^[0-9a-f]{64}$'),
        n_sequences     integer,
        created_at      timestamptz not null default now(),
        updated_at      timestamptz not null default now(),
        unique (sequence_id, source)
    )
    """,
    "create index msa_cache_entries_seq_source_idx on public.msa_cache_entries (sequence_id, source)",
    # Controlled vocabulary
    """
    create table public.msa_sources (
        name        text primary key,
        description text,
        enabled     boolean default true,
        created_at  timestamptz default now()
    )
    """,
    """
    insert into public.msa_sources(name, description) values
      ('OSG-Generated',  'MSAs computed on OSPool/CHTC for prior AF3 runs'),
      ('GDM',            'Google DeepMind reference set'),
      ('Nvidia',         'Nvidia BioNeMo precomputed MSAs'),
      ('BFVD',           'Big Fantastic Virus Database'),
      ('Viro3D',         'Viro3D viral protein MSAs'),
      ('AllTheBacteria', 'AllTheBacteria pan-bacterial MSAs'),
      ('Kinetoplastid',  'Kinetoplastid-focused MSAs')
    """,
    # Flat lookup view: one GET endpoint replaces a manual join
    """
    create or replace view public.msa_cache_lookup as
      select s.seq_hash, s.sequence, s.seq_length, s.molecule_type,
             e.source, e.osdf_uri, e.a3m_size_bytes, e.a3m_sha256, e.n_sequences
      from public.msa_sequences s
      join public.msa_cache_entries e on e.sequence_id = s.id
    """,
    "alter view public.msa_cache_lookup set (security_invoker = on)",
    "grant select on public.msa_cache_lookup to anon",
    # Supabase implicitly grants SELECT to `anon`; make it explicit so the
    # security_invoker view + RLS read path behaves identically on vanilla
    # Postgres (local/CI). Idempotent / harmless on Supabase. Crucially we do
    # NOT grant anon anything on msa_ingestions / app_settings (later
    # migrations) — those stay deny-by-default to PostgREST.
    "grant usage on schema public to anon",
    "grant select on public.msa_sequences     to anon",
    "grant select on public.msa_cache_entries to anon",
    "grant select on public.msa_sources       to anon",
    # Row-level security
    "alter table public.msa_sequences     enable row level security",
    "alter table public.msa_cache_entries enable row level security",
    "alter table public.msa_sources       enable row level security",
    'create policy "anon_read_sequences"     on public.msa_sequences     for select to anon using (true)',
    'create policy "anon_read_cache_entries" on public.msa_cache_entries for select to anon using (true)',
    'create policy "anon_read_sources"       on public.msa_sources       for select to anon using (true)',
]

DOWNGRADE_STATEMENTS: list[str] = [
    "drop view if exists public.msa_cache_lookup",
    "drop table if exists public.msa_cache_entries",
    "drop table if exists public.msa_sources",
    "drop table if exists public.msa_sequences",
]


def upgrade() -> None:
    for stmt in UPGRADE_STATEMENTS:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE_STATEMENTS:
        op.execute(stmt)
