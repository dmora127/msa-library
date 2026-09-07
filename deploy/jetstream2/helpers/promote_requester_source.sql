-- promote_requester_source.sql — flip cache entries contributed by one
-- requester from one source to another. Driven by promote_requester_source.sh,
-- which sets the psql variables :requester, :from_source, :to_source.
--
-- Only rows whose CURRENT source is :from_source are touched; anything else
-- (other sources, in-flight/rejected ingestions with no cache entry) is
-- ignored. Rows that would collide with an existing :to_source entry for the
-- same (sequence_id, db_version) — i.e. the requester re-uploaded a sequence
-- already promoted — are SKIPPED and reported, never deleted.
-- Idempotent: a second run finds nothing and changes nothing.
begin;

-- 1. Freeze the target set so both updates hit exactly the same rows.
create temp table _reclass_ids on commit drop as
select distinct e.id,
       exists (select 1 from public.msa_cache_entries o
                where o.sequence_id = e.sequence_id
                  and o.db_version  = e.db_version
                  and o.source      = :'to_source') as collides
  from public.msa_cache_entries e
  join public.msa_ingestions   i on i.cache_entry_id = e.id
 where i.requester = :'requester'
   and e.source    = :'from_source';

-- 2. Cache entries (what the lookup view / clients see).
update public.msa_cache_entries e
   set source = :'to_source', updated_at = now()
  from _reclass_ids r
 where e.id = r.id
   and not r.collides
   and e.source = :'from_source';

-- 3. Keep the ingestion audit rows consistent with their published entry.
update public.msa_ingestions i
   set source = :'to_source', updated_at = now()
  from _reclass_ids r
 where i.cache_entry_id = r.id
   and not r.collides
   and i.requester = :'requester'
   and i.source    = :'from_source';

-- 4. One line for the log: "<n_promoted> <n_skipped> <skipped ids or ->"
select count(*) filter (where not collides)
       || ' ' || count(*) filter (where collides)
       || ' ' || coalesce(string_agg(id::text, ',' order by id) filter (where collides), '-')
  from _reclass_ids;

commit;
