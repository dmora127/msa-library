"""Classification metadata: msa_cache_entries.species + query_name.

Populated by the verification pipeline (Classifier: DiamondClassifier runs
`diamond blastp --fast --max-target-seqs 1` against swissprot and sets
species=sscinames + query_name=stitle iff the top hit has pident>=100 and
evalue<0.0005; otherwise 'Unclassified'). Default 'Unclassified' so existing
rows backfill cleanly and a missing/unavailable classifier doesn't break
publish.

Revision ID: 0010
Revises: 0009
Create Date: 2026-05-20
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_VIEW_NEW = """
create view public.msa_cache_lookup as
  select s.seq_hash, s.sequence, s.seq_length, s.molecule_type,
         e.source, e.osdf_uri, e.a3m_size_bytes, e.a3m_sha256, e.n_sequences,
         e.db_version, e.status, e.last_accessed_at,
         e.species, e.query_name
  from public.msa_sequences s
  join public.msa_cache_entries e on e.sequence_id = s.id
"""

_VIEW_0009 = """
create view public.msa_cache_lookup as
  select s.seq_hash, s.sequence, s.seq_length, s.molecule_type,
         e.source, e.osdf_uri, e.a3m_size_bytes, e.a3m_sha256, e.n_sequences,
         e.db_version, e.status, e.last_accessed_at
  from public.msa_sequences s
  join public.msa_cache_entries e on e.sequence_id = s.id
"""


UPGRADE_STATEMENTS: list[str] = [
    "alter table public.msa_cache_entries "
    "add column species text not null default 'Unclassified'",
    "alter table public.msa_cache_entries "
    "add column query_name text not null default 'Unclassified'",
    # drop+create (rather than CREATE OR REPLACE) for column-list safety
    "drop view if exists public.msa_cache_lookup",
    _VIEW_NEW,
    "alter view public.msa_cache_lookup set (security_invoker = on)",
    "grant select on public.msa_cache_lookup to anon",
]

DOWNGRADE_STATEMENTS: list[str] = [
    "drop view if exists public.msa_cache_lookup",
    _VIEW_0009,
    "alter view public.msa_cache_lookup set (security_invoker = on)",
    "grant select on public.msa_cache_lookup to anon",
    "alter table public.msa_cache_entries drop column query_name",
    "alter table public.msa_cache_entries drop column species",
]


def upgrade() -> None:
    for stmt in UPGRADE_STATEMENTS:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE_STATEMENTS:
        op.execute(stmt)
