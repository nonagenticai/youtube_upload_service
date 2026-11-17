"""Record whether the thumbnail and captions were actually APPLIED.

``upload_jobs`` already carries ``thumbnail_source`` and ``caption_source`` —
what the caller REQUESTED. It carried nothing about the OUTCOME. Both attach
steps are deliberately best-effort (``_apply_thumbnail`` / ``_apply_captions``
swallow their exception: the video is already on YouTube, and a 403 from an
unverified channel or from a token minted without ``youtube.force-ssl`` must
never fail the job), so the failure lived only in a pod log line. The row went
``completed``, the orchestrator polled ``status``, and a paid render that went
public with YouTube's default auto-thumbnail and no subtitles was byte-identical
to one that got everything.

Both columns are nullable Boolean and are written only by the worker, so this
is additive and needs no backfill. NULL keeps its honest meaning on the rows
that predate it: UNMEASURED. It is NOT rewritten to false — that would assert a
failure this migration cannot know about.

    NULL  -> not requested, or the row predates this migration
    true  -> requested and applied
    false -> requested and it FAILED; the video is public without it

Revision ID: 011_add_applied_outcome_columns
Revises: 010_tenant_id_uuid
Create Date: 2026-09-06
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "011_add_applied_outcome_columns"
down_revision = "010_tenant_id_uuid"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("upload_jobs", sa.Column("thumbnail_applied", sa.Boolean, nullable=True))
    op.add_column("upload_jobs", sa.Column("caption_applied", sa.Boolean, nullable=True))


def downgrade() -> None:
    op.drop_column("upload_jobs", "caption_applied")
    op.drop_column("upload_jobs", "thumbnail_applied")
