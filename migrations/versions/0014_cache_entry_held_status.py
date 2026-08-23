"""Add 'held' to the msa_cache_entries.status CHECK.

'held' parks an entry out of circulation without deleting it. The query
lookup already requires status='active', so a held entry stops being served
the moment it flips, and BOTH maintenance sweeps ignore it (mark_idle_stale
reads only 'active', reap only 'stale') — so the OSDF object and the row
survive indefinitely. Intended for held-out benchmark/test-set sequences that
must not be answered from cache but must not be lost either.

The 0006 CHECK was declared inline, so Postgres auto-named it. We look the
name up from the catalog rather than assuming `msa_cache_entries_status_check`,
then re-add it under an explicit name.

Downgrade demotes held rows to 'active', NOT 'stale' — demoting to stale would
hand them to the reaper, which deletes the OSDF object after reap_grace_hours.
It also resets last_accessed_at: _bump_access only touches 'active' rows, so a
held row's last_accessed_at is frozen at hold time and would otherwise be
immediately re-marked stale by the next idle sweep.

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-23
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# 0006 added the CHECK inline (auto-named). Resolve whatever it is called.
_DROP_STATUS_CHECK = """
do $$
declare
    cname text;
begin
    for cname in
        select con.conname
        from pg_constraint con
        where con.conrelid = 'public.msa_cache_entries'::regclass
          and con.contype = 'c'
          and pg_get_constraintdef(con.oid) like '%status%'
    loop
        execute format(
            'alter table public.msa_cache_entries drop constraint %I', cname
        );
    end loop;
end $$;
"""


def upgrade() -> None:
    op.execute(_DROP_STATUS_CHECK)
    op.execute(
        "alter table public.msa_cache_entries "
        "add constraint msa_cache_entries_status_check "
        "check (status in ('active','stale','held'))"
    )


def downgrade() -> None:
    op.execute(
        "update public.msa_cache_entries "
        "set status = 'active', stale_since = null, "
        "    last_accessed_at = now(), updated_at = now() "
        "where status = 'held'"
    )
    op.execute(_DROP_STATUS_CHECK)
    op.execute(
        "alter table public.msa_cache_entries "
        "add constraint msa_cache_entries_status_check "
        "check (status in ('active','stale'))"
    )
