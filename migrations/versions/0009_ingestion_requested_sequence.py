"""msa_ingestions.requested_sequence — job-declared normalized sequence.

Persisted only when the upload-request supplied `sequence` (not seq_hash).
Audit value, and lets simulation mode publish a queryable cache entry without
fetching an a3m. NULL for seq_hash-only requests.

Revision ID: 0009
Revises: 0008
Create Date: 2026-05-18
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "alter table public.msa_ingestions add column requested_sequence text"
    )


def downgrade() -> None:
    op.execute(
        "alter table public.msa_ingestions drop column requested_sequence"
    )
