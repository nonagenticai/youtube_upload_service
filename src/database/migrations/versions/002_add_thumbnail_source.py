"""Add thumbnail_source columns to upload_jobs.

Lets an upload job carry an optional custom thumbnail (path or URL) that is
applied best-effort after the video insert. Both columns are nullable, so this
is additive and safe to apply on an existing table without a data backfill.

Revision ID: 002_add_thumbnail_source
Revises: 001_initial
Create Date: 2026-06-09
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "002_add_thumbnail_source"
down_revision = "001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("upload_jobs", sa.Column("thumbnail_source", sa.Text, nullable=True))
    op.add_column("upload_jobs", sa.Column("thumbnail_source_type", sa.String(10), nullable=True))


def downgrade() -> None:
    op.drop_column("upload_jobs", "thumbnail_source_type")
    op.drop_column("upload_jobs", "thumbnail_source")
