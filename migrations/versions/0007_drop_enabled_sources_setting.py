"""Drop app_settings.enabled_sources.

Source enablement now lives solely on msa_sources.enabled:
- query visibility: SequenceService already filters msa_sources.enabled
- upload: jobs may only contribute to the community source, and only while
  that source's msa_sources.enabled is true (admin toggle)

Removes the redundant/ambiguous app_settings row. 0001/0004 text untouched.

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-15
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("delete from public.app_settings where key = 'enabled_sources'")


def downgrade() -> None:
    op.execute(
        """
        insert into public.app_settings(key, value)
        values ('enabled_sources', 'Community Contributed - Cached')
        on conflict (key) do nothing
        """
    )
