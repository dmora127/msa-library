"""Per-day count of sequences answered from cache (`query_metrics.hit_count`).

`count` counts POST /v1/query *requests*; `hit_count` counts, within those
requests, the individual sequences that came back with at least one hit (after
the hash-collision guard drops mismatched rows). A batch of 5 sequences where 3
hit adds 1 to `count` and 3 to `hit_count` — so the two columns are different
units and `hit_count` may exceed `count`.

NOT BACKFILLABLE: per-query hit outcomes were never recorded, so every
pre-existing row gets 0. Days before this migration read as
`count > 0, hit_count = 0`; that means "unknown", not "nothing was served".

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-20
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Metadata-only on PG 11+ (non-volatile default) — no table rewrite.
    op.execute(
        "alter table public.query_metrics "
        "add column hit_count bigint not null default 0"
    )


def downgrade() -> None:
    op.execute("alter table public.query_metrics drop column if exists hit_count")
