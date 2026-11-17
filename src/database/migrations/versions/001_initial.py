"""Initial migration: create oauth_tokens and upload_jobs tables.

Revision ID: 001_initial
Revises:
Create Date: 2026-03-19
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision = "001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "oauth_tokens",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(255), nullable=False, index=True),
        sa.Column("access_token", sa.Text, nullable=False),
        sa.Column("refresh_token", sa.Text, nullable=False),
        sa.Column(
            "token_uri",
            sa.String(512),
            nullable=False,
            server_default="https://oauth2.googleapis.com/token",
        ),
        sa.Column("scopes", sa.Text, nullable=False),
        sa.Column("expiry", sa.DateTime(timezone=True), nullable=True),
        sa.Column("channel_id", sa.String(255), nullable=True),
        sa.Column("channel_title", sa.String(255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("tenant_id", name="uq_oauth_tokens_tenant_id"),
    )

    op.create_table(
        "upload_jobs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("job_id", sa.String(36), unique=True, nullable=False, index=True),
        sa.Column("tenant_id", sa.String(255), nullable=False, index=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("file_source", sa.Text, nullable=False),
        sa.Column("file_source_type", sa.String(10), nullable=False, server_default="path"),
        sa.Column("title", sa.String(100), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("tags", JSONB, nullable=True),
        sa.Column("category_id", sa.String(10), nullable=False, server_default="22"),
        sa.Column("privacy_status", sa.String(20), nullable=False, server_default="private"),
        sa.Column("youtube_video_id", sa.String(20), nullable=True),
        sa.Column("youtube_url", sa.String(255), nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_upload_jobs_tenant_status", "upload_jobs", ["tenant_id", "status"])
    op.create_index("ix_upload_jobs_status_created", "upload_jobs", ["status", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_upload_jobs_status_created", table_name="upload_jobs")
    op.drop_index("ix_upload_jobs_tenant_status", table_name="upload_jobs")
    op.drop_table("upload_jobs")
    op.drop_table("oauth_tokens")
