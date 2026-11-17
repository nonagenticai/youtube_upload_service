"""Add scale-in reaper lease columns + SECURITY DEFINER reaper resolvers.

An upload job runs as a fire-and-forget in-process background task; before this
migration a scale-in (pod terminated mid-upload) stranded the job non-terminal
forever. This adds a lease the worker heartbeats while an upload is live:

- ``lease_owner``       the pod holding the job (for fast shutdown release)
- ``lease_expires_at``  when the claim expires; a non-terminal row past this (or
                        with a NULL lease older than the grace window) has no
                        live owner and is reclaimable by the reaper.

Plus the two SECURITY DEFINER resolvers (single-sourced from
``src.database.rls_resolvers`` so tests' ``create_all`` and this migration build
identical functions): ``reap_stranded_upload_jobs`` (fail stranded jobs,
retryable) and ``expire_upload_job_leases`` (shutdown lease-release). They must
be SECURITY DEFINER because under RLS enforce the runtime is a non-bypass role
with no tenant bound, so a cross-tenant scan would fail-closed to 0 rows.

Idempotent raw SQL: a no-op on a DB that already has the columns/functions,
corrective on a fresh CI DB. The service role owns ``upload_jobs`` (created in
001), so the ``ALTER ... IF NOT EXISTS`` ownership check passes.

Revision ID: 006_add_job_lease
Revises: 005_add_owner_tenant_id
Create Date: 2026-07-09
"""

from alembic import op

from src.database.rls_resolvers import EXPIRE_FN_SQL, REAP_FN_SQL

# revision identifiers, used by Alembic.
revision = "006_add_job_lease"
down_revision = "005_add_owner_tenant_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE upload_jobs ADD COLUMN IF NOT EXISTS lease_owner VARCHAR(64)")
    op.execute("ALTER TABLE upload_jobs ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_upload_jobs_status_lease "
        "ON upload_jobs (status, lease_expires_at)"
    )
    op.execute(REAP_FN_SQL)
    op.execute(EXPIRE_FN_SQL)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS expire_upload_job_leases(text)")
    op.execute("DROP FUNCTION IF EXISTS reap_stranded_upload_jobs(int)")
    op.execute("DROP INDEX IF EXISTS ix_upload_jobs_status_lease")
    op.execute("ALTER TABLE upload_jobs DROP COLUMN IF EXISTS lease_expires_at")
    op.execute("ALTER TABLE upload_jobs DROP COLUMN IF EXISTS lease_owner")
