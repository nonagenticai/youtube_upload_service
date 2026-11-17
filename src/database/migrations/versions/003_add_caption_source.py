"""Add caption_source columns to upload_jobs.

Lets an upload job carry an optional soft closed-caption track (SRT/VTT, path
or URL) that is attached best-effort after the video insert via
``captions().insert``. All columns are nullable, so this is additive and safe
to apply on an existing table without a data backfill.

Revision ID: 003_add_caption_source
Revises: 002_add_thumbnail_source
Create Date: 2026-06-13
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "003_add_caption_source"
down_revision = "002_add_thumbnail_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("upload_jobs", sa.Column("caption_source", sa.Text, nullable=True))
    op.add_column("upload_jobs", sa.Column("caption_source_type", sa.String(10), nullable=True))
    op.add_column("upload_jobs", sa.Column("caption_language", sa.String(10), nullable=True))


def downgrade() -> None:
    op.drop_column("upload_jobs", "caption_language")
    op.drop_column("upload_jobs", "caption_source_type")
    op.drop_column("upload_jobs", "caption_source")
