"""msa_ingestions.namespace: which OSDF namespace (store) each object lives in.

Until now the namespace existed only inside the `osdf_uri` string. With more
than one namespace coming (each with its own issuer, key and capacity), the
capacity gate and postgres-exporter need per-namespace byte totals over ~750k
rows on every refresh; parsing URIs for that is slow and ambiguous once a
namespace itself contains '/'. So: a nullable text column, written by the app
at insert from the namespace registry, backfilled here for the `osdf:///<ns>/`
form every live row uses, plus a covering index so the usage query
(app/services/namespace_capacity.py NAMESPACE_USAGE_SQL) is an index-only scan.

Backfill uses split_part(osdf_uri, '/', 4), which is exact for the single-
segment namespace all osdf:/// rows carry (af3-msa-library). The two legacy
`pelican://osgaf3_store{1,2}/msa-cache/...` test rows keep NULL: they predate
the osdf:/// template and match no configured namespace.

Lock footprint: add column (nullable, no default) is instant; the backfill
UPDATE rewrites every row in one transaction (seconds on ~1 GB); CREATE INDEX
takes a SHARE lock for the build. Run while the api is stopped (see
docs/redeploy.md) and `vacuum analyze public.msa_ingestions` afterwards.

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-07
"""

from __future__ import annotations

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels = None
depends_on = None

UPGRADE_STATEMENTS: list[str] = [
    "alter table public.msa_ingestions add column namespace text",
    """
    update public.msa_ingestions
       set namespace = split_part(osdf_uri, '/', 4)
     where osdf_uri like 'osdf:///%'
       and split_part(osdf_uri, '/', 4) <> ''
    """,
    """
    create index msa_ingestions_namespace_status_idx
        on public.msa_ingestions (namespace, status)
        include (observed_size_bytes, declared_size_bytes, cache_entry_id)
    """,
]

DOWNGRADE_STATEMENTS: list[str] = [
    "drop index if exists public.msa_ingestions_namespace_status_idx",
    "alter table public.msa_ingestions drop column if exists namespace",
]


def upgrade() -> None:
    for stmt in UPGRADE_STATEMENTS:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE_STATEMENTS:
        op.execute(stmt)
