"""Flip ``upload_jobs.file_source_type`` server_default off ``'path'``.

``001_initial`` created the column as::

    sa.Column("file_source_type", sa.String(10), nullable=False, server_default="path")

so any INSERT that simply OMITTED the column landed in **path mode** — the
worker then opened a caller-supplied filesystem path with nothing but an
existence check. No PVC is mounted on this Deployment, so that path resolves
inside the pod's own filesystem (the mounted service-account token, any mounted
secret) and the bytes were uploaded to YouTube.

The request schema now types the field ``Literal["url"]``, so the API can no
longer produce a path row. This migration removes the *other* way in: a direct
INSERT (psql, a future ORM path, a restored fixture) that leaves the column out
now defaults to ``'url'`` instead of silently selecting path mode.

Existing rows are left untouched on purpose — this is a default change, not a
data rewrite; rewriting a historical row's ``file_source_type`` would misreport
what that job actually did. The worker refuses to serve non-``url`` rows
regardless (see ``UploadService._run_upload``).

Idempotent: ``ALTER COLUMN ... SET DEFAULT`` is unconditional and safe to
re-run. The service role owns ``upload_jobs`` (created in 001).

Revision ID: 007_file_source_type_default_url
Revises: 006_add_job_lease
Create Date: 2026-08-03
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "007_file_source_type_default_url"
down_revision = "006_add_job_lease"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE upload_jobs ALTER COLUMN file_source_type SET DEFAULT 'url'")


def downgrade() -> None:
    op.execute("ALTER TABLE upload_jobs ALTER COLUMN file_source_type SET DEFAULT 'path'")
