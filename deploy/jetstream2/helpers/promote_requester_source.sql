-- promote_requester_source.sql — safety net behind the publish-time trigger
-- (migration 0017). Flips cache entries whose CURRENT source is :from_source
-- to the msa_sources row whose authorized_requesters (SQL LIKE patterns)
-- matches the contributing ingestion's requester. Driven by
-- promote_requester_source.sh, which sets the psql variable :from_source.
--
-- Since 0017 the trigger msa_cache_entries_requester_source_bi does this at
-- insert, so a normal run finds nothing. It still matters for (a) the one-off
-- backfill of rows published before the trigger existed, and (b) anything
-- that slipped past the trigger (e.g. rows inserted without an in-flight
-- ingestion row). Rows that would collide with an existing target-source entry
-- for the same (sequence_id, db_version) — a re-upload of an already-promoted
-- sequence — are SKIPPED and reported, never deleted.
-- Idempotent: a second run finds nothing and changes nothing.
begin;

-- 1. Freeze the target set so both updates hit exactly the same rows.
--    One target per entry: if several sources claim a requester, the
--    alphabetically first name wins (mirrors the trigger's order by s.name).
create temp table _reclass_ids on commit drop as
with t as (
  select e.id, e.sequence_id, e.db_version, min(s.name) as to_source
    from public.msa_cache_entries e
    join public.msa_ingestions   i on i.cache_entry_id = e.id
    join public.msa_sources      s on s.enabled
                                   and s.name <> e.source
                                   and cardinality(s.authorized_requesters) > 0
                                   and i.requester like any (s.authorized_requesters)
   where e.source = :'from_source'
   group by e.id, e.sequence_id, e.db_version
)
select t.id, t.to_source,
       exists (select 1 from public.msa_cache_entries o
                where o.sequence_id = t.sequence_id
                  and o.db_version  = t.db_version
                  and o.source      = t.to_source) as collides
  from t;

-- 2. Cache entries (what the lookup view / clients see).
update public.msa_cache_entries e
   set source = r.to_source, updated_at = now()
  from _reclass_ids r
 where e.id = r.id
   and not r.collides
   and e.source = :'from_source';

-- 3. Keep the ingestion audit rows consistent with their published entry.
update public.msa_ingestions i
   set source = r.to_source, updated_at = now()
  from _reclass_ids r
 where i.cache_entry_id = r.id
   and not r.collides
   and i.source = :'from_source';

-- 4. One line for the log: "<n_promoted> <n_skipped> <skipped ids or ->"
select count(*) filter (where not collides)
       || ' ' || count(*) filter (where collides)
       || ' ' || coalesce(string_agg(id::text, ',' order by id) filter (where collides), '-')
  from _reclass_ids;

commit;
