"""Community-contributed osdf:// object template (drop per-store sharding).

Community uploads now mint a scoped write token via
  `pelican token create <osdf-uri> --write --issuer <issuer> -k <key>
   --lifetime <ttl>`
and are stored at a single, flat namespace path keyed by the ingestion UUID:
  osdf:///af3-msa-library/community-contributed/<ingestion_id>.a3m
(no random store load-balancing; supersedes 0008's pelican://{namespace} form).

Revision ID: 0011
Revises: 0010
Create Date: 2026-05-23
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "update public.app_settings set value = "
        "'osdf:///af3-msa-library/community-contributed/{ingestion_id}.a3m', "
        "updated_at = now() where key = 'osdf_namespace_template'"
    )


def downgrade() -> None:
    op.execute(
        "update public.app_settings set value = "
        "'pelican://{namespace}/msa-cache/{seq_hash}/{source_slug}/{ingestion_id}.a3m', "
        "updated_at = now() where key = 'osdf_namespace_template'"
    )
