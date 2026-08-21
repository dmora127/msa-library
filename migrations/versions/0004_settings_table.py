"""app_settings: runtime-adjustable operational config.

Env vars are the floor; these rows override at request time. API keys are
NEVER stored here. RLS on, no anon policies.

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-15
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UPGRADE_STATEMENTS: list[str] = [
    """
    create table public.app_settings (
        key        text primary key,
        value      text not null,
        updated_at timestamptz not null default now(),
        updated_by text
    )
    """,
    """
    insert into public.app_settings(key, value) values
      ('max_upload_size_bytes',   '104857600'),
      ('token_ttl_seconds',       '900'),
      ('osdf_namespace_template', 'osdf:///chtc/msa-cache/{seq_hash}/{source_slug}.a3m'),
      ('enabled_sources',         'Community Contributed - Cached')
    on conflict (key) do nothing
    """,
    # RLS on, no anon policies
    "alter table public.app_settings enable row level security",
]

DOWNGRADE_STATEMENTS: list[str] = ["drop table if exists public.app_settings"]


def upgrade() -> None:
    for stmt in UPGRADE_STATEMENTS:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE_STATEMENTS:
        op.execute(stmt)
