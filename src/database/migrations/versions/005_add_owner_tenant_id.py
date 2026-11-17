"""Add owner_tenant_id to upload_jobs for real-caller scoping.

``tenant_id`` holds the YouTube-channel owner (the orchestrator's
service-account tenant, used by the worker to load OAuth credentials) — it is
the SAME value for every real caller, so scoping list/get by it leaked every
tenant's jobs to every other. ``owner_tenant_id`` records the REAL caller tenant
(forwarded by the orchestrator as ``X-Owner-Tenant-Id``); list/get scope to it.

Nullable + additive, so safe to apply on an existing table without a backfill.
Pre-existing rows keep ``owner_tenant_id = NULL`` and simply stop appearing in
the tenant-scoped list (they were all stored under the SA tenant; there is no
reliable real owner to backfill).

Revision ID: 005_add_owner_tenant_id
Revises: 004_rls
Create Date: 2026-06-17
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "005_add_owner_tenant_id"
down_revision = "004_rls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("upload_jobs", sa.Column("owner_tenant_id", sa.String(255), nullable=True))
    op.create_index("ix_upload_jobs_owner_tenant_id", "upload_jobs", ["owner_tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_upload_jobs_owner_tenant_id", table_name="upload_jobs")
    op.drop_column("upload_jobs", "owner_tenant_id")
