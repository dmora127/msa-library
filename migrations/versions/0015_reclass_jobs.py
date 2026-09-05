"""reclass_jobs: tracking table for external NR BLAST reclassification jobs.

One row per job submitted to the remote BLAST box by
deploy/jetstream2/helpers/reclassify_export.sh (job_id is the REMOTE id, e.g.
20260905T071245Z-64839f). reclassify_poll.sh stores each `reclass-status`
JSON here via PUT /v1/admin/classification/jobs/{id}/status. The remote box
forgets jobs after 15 days, so this row is the durable record.

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-05
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UPGRADE_STATEMENTS: list[str] = [
    """
    create table public.reclass_jobs (
        job_id              text primary key,
        input_name          text not null,
        sequence_count      integer not null,
        residue_count       bigint,
        input_bytes         bigint,
        status              text not null default 'QUEUED'
                            check (status in ('QUEUED','RUNNING','DONE','FAILED','KILLED','UNKNOWN')),
        remote_submitted_at timestamptz,
        remote_started_at   timestamptz,
        remote_finished_at  timestamptz,
        exit_code           integer,
        posted_so_far       integer,
        summary             jsonb,
        last_status         jsonb,
        last_error          text,
        poll_count          integer not null default 0,
        last_polled_at      timestamptz,
        created_at          timestamptz not null default now(),
        updated_at          timestamptz not null default now()
    )
    """,
    "create index reclass_jobs_active_idx on public.reclass_jobs (status) "
    "where status in ('QUEUED','RUNNING')",
    "alter table public.reclass_jobs enable row level security",
]

DOWNGRADE_STATEMENTS: list[str] = [
    "drop table if exists public.reclass_jobs",
]


def upgrade() -> None:
    for stmt in UPGRADE_STATEMENTS:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE_STATEMENTS:
        op.execute(stmt)
