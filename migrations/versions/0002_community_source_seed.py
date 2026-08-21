"""Seed the 'Community Contributed - Cached' source.

Every MSA ingested through this server's upload pipeline uses this source name,
so contributed entries surface through the unchanged msa_cache_lookup view to
the existing client with zero client changes.

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-15
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        insert into public.msa_sources(name, description, enabled) values
          ('Community Contributed - Cached',
           'MSAs contributed by AF3 jobs via the af3-cache upload pipeline',
           true)
        on conflict (name) do nothing;
        """
    )


def downgrade() -> None:
    op.execute(
        "delete from public.msa_sources where name = 'Community Contributed - Cached';"
    )
