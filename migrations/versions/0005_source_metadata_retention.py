"""Source metadata/notes + per-source retention.

- msa_sources: notes, homepage_url, license, version, metadata jsonb,
  retention_days (NULL = never retire).
- msa_cache_entries: expires_at (NULL = never expires) + index.
- msa_cache_entries.source -> msa_sources(name) ON DELETE CASCADE, added
  NOT VALID so it is safe on a pre-existing (Supabase) table: retiring a whole
  source removes only THAT source's entries everywhere; other sources' entries
  for the same sequence are independent rows and are untouched.
- msa_cache_lookup view extended with expires_at (additive; the existing client
  uses an explicit select list so it is unaffected).

Does not modify 0001. On an existing Supabase project: stamp 0001, upgrade.

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-15
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_VIEW_WITH_EXPIRES = """
create or replace view public.msa_cache_lookup as
  select s.seq_hash, s.sequence, s.seq_length, s.molecule_type,
         e.source, e.osdf_uri, e.a3m_size_bytes, e.a3m_sha256,
         e.n_sequences, e.expires_at
  from public.msa_sequences s
  join public.msa_cache_entries e on e.sequence_id = s.id
"""

_VIEW_ORIGINAL = """
create or replace view public.msa_cache_lookup as
  select s.seq_hash, s.sequence, s.seq_length, s.molecule_type,
         e.source, e.osdf_uri, e.a3m_size_bytes, e.a3m_sha256, e.n_sequences
  from public.msa_sequences s
  join public.msa_cache_entries e on e.sequence_id = s.id
"""

UPGRADE_STATEMENTS: list[str] = [
    "alter table public.msa_sources add column notes text",
    "alter table public.msa_sources add column homepage_url text",
    "alter table public.msa_sources add column license text",
    "alter table public.msa_sources add column version text",
    # NULL retention_days = permanent (never retire)
    "alter table public.msa_sources add column retention_days integer",
    "alter table public.msa_sources add column metadata jsonb not null default '{}'::jsonb",
    # NULL expires_at = never expires
    "alter table public.msa_cache_entries add column expires_at timestamptz",
    "create index msa_cache_entries_expires_at_idx on public.msa_cache_entries (expires_at)",
    # retire-a-source isolation; NOT VALID => no scan of existing rows
    """
    alter table public.msa_cache_entries
      add constraint msa_cache_entries_source_fkey
      foreign key (source) references public.msa_sources(name)
      on delete cascade not valid
    """,
    _VIEW_WITH_EXPIRES,
    "alter view public.msa_cache_lookup set (security_invoker = on)",
    "grant select on public.msa_cache_lookup to anon",
    # Differential TTL: community uploads churn; reference sets are permanent.
    """
    update public.msa_sources
       set retention_days = 90,
           notes = 'User/job contributed via the af3-cache upload pipeline. '
                   'Shorter TTL; periodically pruned. Lower trust than curated '
                   'reference sets.'
     where name = 'Community Contributed - Cached'
    """,
]

DOWNGRADE_STATEMENTS: list[str] = [
    _VIEW_ORIGINAL,
    "alter view public.msa_cache_lookup set (security_invoker = on)",
    "grant select on public.msa_cache_lookup to anon",
    "alter table public.msa_cache_entries drop constraint msa_cache_entries_source_fkey",
    "drop index if exists public.msa_cache_entries_expires_at_idx",
    "alter table public.msa_cache_entries drop column expires_at",
    "alter table public.msa_sources drop column metadata",
    "alter table public.msa_sources drop column retention_days",
    "alter table public.msa_sources drop column version",
    "alter table public.msa_sources drop column license",
    "alter table public.msa_sources drop column homepage_url",
    "alter table public.msa_sources drop column notes",
]


def upgrade() -> None:
    for stmt in UPGRADE_STATEMENTS:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE_STATEMENTS:
        op.execute(stmt)
