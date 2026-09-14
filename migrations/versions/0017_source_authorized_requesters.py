"""msa_sources.authorized_requesters: publish-time source flip by requester.

Job uploads can only be requested as 'Community Contributed - Cached'
(app/schemas/ingestion.py DEFAULT_SOURCE; ingestion_service.request_upload
rejects anything else). Some requesters (trusted CHTC/OSPool pipelines) are
really OSG-Generated. Until now a nightly cron re-sourced those rows after the
fact, so any consumer of GET /v1/stats that diffs consecutive days saw a large
negative "Community Contributed" delta every night.

This migration moves the mapping into the database so a matching upload is
OSG-Generated from the moment it exists in msa_cache_entries:

* `msa_sources.authorized_requesters text[]` — SQL LIKE patterns on the TARGET
  source row. A community upload whose msa_ingestions.requester matches any
  element is published under that source instead. Seeded on 'OSG-Generated'.
* BEFORE INSERT trigger on msa_cache_entries (apply_requester_source_rule):
  looks up the in-flight ingestion being published via the existing partial
  unique index msa_ingestions_inflight_uniq (seq_hash, source, db_version)
  and rewrites NEW.source. Because BEFORE ROW trigger output feeds ON CONFLICT
  arbitration, a re-upload of a sequence that already has an OSG-Generated
  entry upserts that entry instead of creating a colliding community
  duplicate. Only rows whose incoming source is the community source are
  touched, so admin ingests (GDM etc.) with the same requester are unaffected.
* BEFORE UPDATE OF cache_entry_id trigger on msa_ingestions
  (sync_ingestion_source_from_entry): when the publish UPDATE links the
  ingestion to its cache entry, copy the entry's source onto the audit row in
  the same transaction. The WHEN clause keeps it off token refreshes, reject,
  the FK SET NULL and the nightly helper.

Why not flip at msa_ingestions INSERT: request_upload dedups in-flight rows by
`source = req.source` (community) and its IntegrityError adoption path does
`.one()` on the same predicate; an earlier flip would turn retries into 500s.

Lock footprint: `add column ... default '{}'` on the 8-row msa_sources is
instant. `create trigger` takes SHARE ROW EXCLUSIVE on its table — catalog-only
and instant, but it queues behind any in-progress writer and blocks new writers
while queued, hence the lock_timeout. Run with the api stopped (see
deploy/jetstream2/docs/redeploy.md). No backfill here: run
deploy/jetstream2/helpers/promote_requester_source.sh once after deploying (it
now reads the same column and stays on cron as a safety net).

Revision ID: 0017
Revises: 0016
Create Date: 2026-09-11
"""

from __future__ import annotations

from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels = None
depends_on = None

COMMUNITY_SOURCE = "Community Contributed - Cached"

UPGRADE_STATEMENTS: list[str] = [
    "set local lock_timeout = '10s'",
    """
    alter table public.msa_sources
      add column authorized_requesters text[] not null default '{}'
    """,
    f"""
    comment on column public.msa_sources.authorized_requesters is
      'SQL LIKE patterns. A community upload (source ''{COMMUNITY_SOURCE}'') whose '
      'msa_ingestions.requester matches any element is published under THIS source '
      'instead. Applied by trigger msa_cache_entries_requester_source_bi at '
      'cache-entry insert; the nightly promote_requester_source.sh is a safety net.'
    """,
    """
    update public.msa_sources
       set authorized_requesters = array['pipeline-bot%', 'batch.runner@ap.example.org']
     where name = 'OSG-Generated'
    """,
    f"""
    create or replace function public.apply_requester_source_rule() returns trigger
    language plpgsql set search_path = pg_catalog, public as $$
    declare
        v_hash      text;
        v_requester text;
        v_to        text;
    begin
        if NEW.source <> '{COMMUNITY_SOURCE}' then
            return NEW;
        end if;
        select seq_hash into v_hash from public.msa_sequences where id = NEW.sequence_id;
        if v_hash is null then
            return NEW;
        end if;
        -- the in-flight ingestion being published: at most one per
        -- (seq_hash, source, db_version) among pending/uploaded rows
        -- (msa_ingestions_inflight_uniq). osdf_uri is a belt-and-braces filter.
        select i.requester into v_requester
          from public.msa_ingestions i
         where i.seq_hash = v_hash
           and i.source = NEW.source
           and i.db_version = NEW.db_version
           and i.status in ('pending', 'uploaded')
           and i.osdf_uri = NEW.osdf_uri
         limit 1;
        if v_requester is null then
            return NEW;
        end if;
        select s.name into v_to
          from public.msa_sources s
         where s.enabled
           and s.name <> NEW.source
           and cardinality(s.authorized_requesters) > 0
           and v_requester like any (s.authorized_requesters)
         order by s.name
         limit 1;
        if v_to is not null then
            NEW.source := v_to;
        end if;
        return NEW;
    end
    $$
    """,
    """
    create trigger msa_cache_entries_requester_source_bi
        before insert on public.msa_cache_entries
        for each row execute function public.apply_requester_source_rule()
    """,
    """
    create or replace function public.sync_ingestion_source_from_entry() returns trigger
    language plpgsql set search_path = pg_catalog, public as $$
    declare
        v_src text;
    begin
        select source into v_src from public.msa_cache_entries where id = NEW.cache_entry_id;
        if v_src is not null then
            NEW.source := v_src;
        end if;
        return NEW;
    end
    $$
    """,
    """
    create trigger msa_ingestions_sync_source_bu
        before update of cache_entry_id on public.msa_ingestions
        for each row
        when (NEW.cache_entry_id is not null
              and NEW.cache_entry_id is distinct from OLD.cache_entry_id)
        execute function public.sync_ingestion_source_from_entry()
    """,
]

DOWNGRADE_STATEMENTS: list[str] = [
    "drop trigger if exists msa_ingestions_sync_source_bu on public.msa_ingestions",
    "drop trigger if exists msa_cache_entries_requester_source_bi on public.msa_cache_entries",
    "drop function if exists public.sync_ingestion_source_from_entry()",
    "drop function if exists public.apply_requester_source_rule()",
    "alter table public.msa_sources drop column if exists authorized_requesters",
]


def upgrade() -> None:
    for stmt in UPGRADE_STATEMENTS:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE_STATEMENTS:
        op.execute(stmt)
