"""Per-day query count for /v1/stats.queriesToday.

Single tiny table: one row per UTC date, an integer counter. The query route
UPSERTs `(current_date, 1)` after a successful lookup; the stats endpoint reads
today's row. Both are best-effort — a write failure here must never break a
query, and a missing row simply means zero so far today.

Revision ID: 0012
Revises: 0011
Create Date: 2026-06-04
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        create table public.query_metrics (
          day   date   primary key default current_date,
          count bigint not null   default 0
        )
        """
    )
    # No anon policies -> invisible to PostgREST. The API reads/writes it via
    # the service-role connection.
    op.execute("alter table public.query_metrics enable row level security")


def downgrade() -> None:
    op.execute("drop table if exists public.query_metrics")
