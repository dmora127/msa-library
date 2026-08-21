"""Idle-based retention + db_version identity.

Replaces 0005's publish-time fixed expiry with access-based eviction:

- msa_sources: retention_days -> idle_ttl_days (NULL = never cull).
- msa_cache_entries: + last_accessed_at, status('active'|'stale'), stale_since,
  db_version. Drop expires_at. Identity becomes (sequence_id, source,
  db_version): two db_versions of the same seq+source are distinct entries.
- msa_ingestions: + db_version; in-flight uniqueness becomes
  (seq_hash, source, db_version).
- app_settings: + reap_grace_hours (mid-read safety window); namespace
  template now embeds {ingestion_id} so objects are immutable & coexist.
- view exposes db_version + status + last_accessed_at, drops expires_at.

db_version is added NOT NULL DEFAULT 'unknown' then the default is dropped, so
it is safe on a pre-existing Supabase table yet required for new writes.
0001 text is untouched; on an existing project: stamp 0001, upgrade.

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-15
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VIEW_NEW = """
create view public.msa_cache_lookup as
  select s.seq_hash, s.sequence, s.seq_length, s.molecule_type,
         e.source, e.osdf_uri, e.a3m_size_bytes, e.a3m_sha256, e.n_sequences,
         e.db_version, e.status, e.last_accessed_at
  from public.msa_sequences s
  join public.msa_cache_entries e on e.sequence_id = s.id
"""

_VIEW_0005 = """
create view public.msa_cache_lookup as
  select s.seq_hash, s.sequence, s.seq_length, s.molecule_type,
         e.source, e.osdf_uri, e.a3m_size_bytes, e.a3m_sha256,
         e.n_sequences, e.expires_at
  from public.msa_sequences s
  join public.msa_cache_entries e on e.sequence_id = s.id
"""

# Neutral form referencing only base columns — used mid-downgrade so columns on
# either side (expires_at / db_version / status / last_accessed_at) can be
# swapped without the view holding a dependency on them.
_VIEW_NEUTRAL = """
create view public.msa_cache_lookup as
  select s.seq_hash, s.sequence, s.seq_length, s.molecule_type,
         e.source, e.osdf_uri, e.a3m_size_bytes, e.a3m_sha256, e.n_sequences
  from public.msa_sequences s
  join public.msa_cache_entries e on e.sequence_id = s.id
"""

UPGRADE_STATEMENTS: list[str] = [
    "alter table public.msa_sources rename column retention_days to idle_ttl_days",
    # add new columns first so the view can be recreated off them
    "alter table public.msa_cache_entries "
    "add column last_accessed_at timestamptz not null default now()",
    "alter table public.msa_cache_entries "
    "add column status text not null default 'active' "
    "check (status in ('active','stale'))",
    "alter table public.msa_cache_entries add column stale_since timestamptz",
    "alter table public.msa_cache_entries "
    "add column db_version text not null default 'unknown'",
    "alter table public.msa_cache_entries alter column db_version drop default",
    "alter table public.msa_ingestions "
    "add column db_version text not null default 'unknown'",
    "alter table public.msa_ingestions alter column db_version drop default",
    # recreate the view (no longer selects expires_at) BEFORE dropping the
    # column it depended on. CREATE OR REPLACE can't reorder columns, so drop+create.
    "drop view if exists public.msa_cache_lookup",
    _VIEW_NEW,
    "alter view public.msa_cache_lookup set (security_invoker = on)",
    "grant select on public.msa_cache_lookup to anon",
    "drop index if exists public.msa_cache_entries_expires_at_idx",
    "alter table public.msa_cache_entries drop column expires_at",
    # identity now includes db_version
    "alter table public.msa_cache_entries "
    "drop constraint msa_cache_entries_sequence_id_source_key",
    "alter table public.msa_cache_entries "
    "add constraint msa_cache_entries_seq_src_dbver_key "
    "unique (sequence_id, source, db_version)",
    "create index msa_cache_entries_idle_idx "
    "on public.msa_cache_entries (status, last_accessed_at)",
    "create index msa_cache_entries_reap_idx "
    "on public.msa_cache_entries (status, stale_since)",
    "drop index if exists public.msa_ingestions_inflight_uniq",
    "create unique index msa_ingestions_inflight_uniq "
    "on public.msa_ingestions (seq_hash, source, db_version) "
    "where status in ('pending','uploaded')",
    "insert into public.app_settings(key, value) values "
    "('reap_grace_hours', '12') on conflict (key) do nothing",
    "update public.app_settings set value = "
    "'osdf:///chtc/msa-cache/{seq_hash}/{source_slug}/{ingestion_id}.a3m', "
    "updated_at = now() where key = 'osdf_namespace_template'",
]

DOWNGRADE_STATEMENTS: list[str] = [
    # neutralize the view so columns on both sides can be swapped
    "drop view if exists public.msa_cache_lookup",
    _VIEW_NEUTRAL,
    "alter view public.msa_cache_lookup set (security_invoker = on)",
    "grant select on public.msa_cache_lookup to anon",
    "update public.app_settings set value = "
    "'osdf:///chtc/msa-cache/{seq_hash}/{source_slug}.a3m' "
    "where key = 'osdf_namespace_template'",
    "delete from public.app_settings where key = 'reap_grace_hours'",
    "drop index if exists public.msa_ingestions_inflight_uniq",
    "create unique index msa_ingestions_inflight_uniq "
    "on public.msa_ingestions (seq_hash, source) "
    "where status in ('pending','uploaded')",
    "alter table public.msa_ingestions drop column db_version",
    "drop index if exists public.msa_cache_entries_reap_idx",
    "drop index if exists public.msa_cache_entries_idle_idx",
    "alter table public.msa_cache_entries "
    "drop constraint msa_cache_entries_seq_src_dbver_key",
    "alter table public.msa_cache_entries "
    "add constraint msa_cache_entries_sequence_id_source_key "
    "unique (sequence_id, source)",
    "alter table public.msa_cache_entries drop column db_version",
    "alter table public.msa_cache_entries drop column stale_since",
    "alter table public.msa_cache_entries drop column status",
    "alter table public.msa_cache_entries drop column last_accessed_at",
    "alter table public.msa_cache_entries add column expires_at timestamptz",
    "create index msa_cache_entries_expires_at_idx "
    "on public.msa_cache_entries (expires_at)",
    # restore the 0005 view shape (expires_at now exists again)
    "drop view if exists public.msa_cache_lookup",
    _VIEW_0005,
    "alter view public.msa_cache_lookup set (security_invoker = on)",
    "grant select on public.msa_cache_lookup to anon",
    "alter table public.msa_sources rename column idle_ttl_days to retention_days",
]


def upgrade() -> None:
    for stmt in UPGRADE_STATEMENTS:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE_STATEMENTS:
        op.execute(stmt)
