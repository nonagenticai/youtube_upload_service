"""Record which YouTube channel published each upload job.

Retract and set-privacy picked credentials from the owner's CURRENT grant. A
video published to the PLATFORM channel (operator flag on, tenant had no grant)
became un-retractable the moment the tenant connected its own channel: the
takedown went to the tenant's channel, YouTube answered 404, and the video
stayed up with no API path to take it down.

    published_via text NULL CHECK (published_via IN ('platform', 'tenant'))

Written by the worker from the credential decision it made at upload time.
NULL keeps its honest meaning on rows that predate this migration: UNKNOWN — it
is NOT backfilled, since nothing recorded which channel those rows used; they
keep the old behaviour (owner's grant, else platform when the flag is on).

Drift-tolerant: ``ADD COLUMN IF NOT EXISTS`` and a guarded constraint add, so a
database that already carries them (a ``create_all`` test DB, a re-run)
upgrades cleanly. Inline DDL only; no import of the live models.

Revision ID: 013_upload_job_published_via
Revises: 012_oauth_needs_reconnect
Create Date: 2026-10-09
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "013_upload_job_published_via"
down_revision = "012_oauth_needs_reconnect"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE upload_jobs ADD COLUMN IF NOT EXISTS published_via text")
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'ck_upload_jobs_published_via'
                  AND conrelid = 'upload_jobs'::regclass
            ) THEN
                ALTER TABLE upload_jobs
                    ADD CONSTRAINT ck_upload_jobs_published_via
                    CHECK (published_via IN ('platform', 'tenant'));
            END IF;
        END
        $$
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE upload_jobs DROP CONSTRAINT IF EXISTS ck_upload_jobs_published_via")
    op.execute("ALTER TABLE upload_jobs DROP COLUMN IF EXISTS published_via")
