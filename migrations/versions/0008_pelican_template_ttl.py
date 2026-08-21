"""Pelican object-URL template + 1200s token lifetime.

Uploads now mint a scoped token via `pelican token create --write
--scope-path pelican://<store>/... --lifetime <ttl>` and store/return
pelican:// object URLs. The namespace is chosen at random per upload from the
configured store list (load-balancing across geo-distributed stores).

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-15
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def upgrade() -> None:
    op.execute(
        "update public.app_settings set value = "
        "'pelican://{namespace}/msa-cache/{seq_hash}/{source_slug}/{ingestion_id}.a3m', "
        "updated_at = now() where key = 'osdf_namespace_template'"
    )
    op.execute(
        "update public.app_settings set value = '1200', updated_at = now() "
        "where key = 'token_ttl_seconds'"
    )


def downgrade() -> None:
    op.execute(
        "update public.app_settings set value = "
        "'osdf:///chtc/msa-cache/{seq_hash}/{source_slug}/{ingestion_id}.a3m', "
        "updated_at = now() where key = 'osdf_namespace_template'"
    )
    op.execute(
        "update public.app_settings set value = '900', updated_at = now() "
        "where key = 'token_ttl_seconds'"
    )
