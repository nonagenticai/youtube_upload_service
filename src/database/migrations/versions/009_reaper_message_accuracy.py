"""Make the reaper's failure message tell the truth about WHY a job was reaped.

``reap_stranded_upload_jobs`` fails two structurally different populations and
stamped one message on both:

    'Upload interrupted by pod scale-in/crash: lease expired before completion
     (no live owner). Safe to retry.'

The lease columns are stamped only at CLAIM time
(``UploadService._process_job``), never at enqueue (``create_job`` inserts them
NULL). So a job that simply waited behind the ``UPLOAD_MAX_CONCURRENT``
semaphore for longer than the reaper's grace window is failed with a message
blaming a pod termination that never happened -- sending whoever reads it to
hunt a scale-in event in the deploy history.

The replacement branches on ``lease_owner``:

- NULL  -> never claimed by any worker. Nothing was uploaded, genuinely safe to
  retry.
- NOT NULL -> claimed, then the heartbeat stopped. That one really is a
  crash/scale-in, and it is NOT safe to retry blind: the pod can finish the
  YouTube upload and only then fail to write the row back, so the message says
  to check the channel first.

Function body single-sourced from ``src.database.rls_resolvers`` so this
migration and the tests' ``create_all`` build byte-identical functions.
``CREATE OR REPLACE FUNCTION`` keeps the signature, so no dependent object is
disturbed; the downgrade restores the previous single-message body verbatim.

Revision ID: 009_reaper_message_accuracy
Revises: 008_publish_once_unique_index
Create Date: 2026-08-21
"""

from alembic import op

from src.database.rls_resolvers import REAP_FN_SQL

# revision identifiers, used by Alembic.
revision = "009_reaper_message_accuracy"
down_revision = "008_publish_once_unique_index"
branch_labels = None
depends_on = None

# The 006-era body, kept verbatim so the downgrade is a true inverse.
_PREVIOUS_REAP_FN_SQL = """
CREATE OR REPLACE FUNCTION reap_stranded_upload_jobs(p_grace_seconds int)
RETURNS int LANGUAGE plpgsql SECURITY DEFINER SET search_path FROM CURRENT AS $fn$
DECLARE
  v_count int;
BEGIN
  UPDATE upload_jobs
    SET status = 'failed',
        error_message = 'Upload interrupted by pod scale-in/crash: lease expired '
                        'before completion (no live owner). Safe to retry.',
        completed_at = now()
  WHERE status IN ('pending', 'processing')
    AND (
      (lease_expires_at IS NOT NULL AND lease_expires_at < now())
      OR (lease_expires_at IS NULL
          AND created_at < now() - make_interval(secs => p_grace_seconds))
    );
  GET DIAGNOSTICS v_count = ROW_COUNT;
  RETURN v_count;
END
$fn$
"""


def upgrade() -> None:
    op.execute(REAP_FN_SQL)


def downgrade() -> None:
    op.execute(_PREVIOUS_REAP_FN_SQL)
