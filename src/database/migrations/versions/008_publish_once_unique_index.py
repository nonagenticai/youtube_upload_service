"""Publish-once: partial unique index on ``(owner_tenant_id, file_source)``.

Nothing in the schema stopped the same render being published to YouTube twice.
``upload_jobs.job_id`` is unique, but it is minted fresh on every ``POST
/api/upload``, so two requests naming the same ``file_source`` were two
independent, fully-paid publishes to the channel.

That is not hypothetical. When a DB blip outlasts the worker's lease the reaper
fails the job as a crash while the upload is in fact completing; the completing
task then (correctly) refuses to resurrect the row the reaper terminalised. The
operator sees a failed job and re-publishes -- and the same render lands on the
channel a second time.

This index is the database-level backstop under the application's pre-claim
check (``UploadService.create_job``): at most one **completed** upload per
(real caller tenant, file source).

- Partial (``WHERE status = 'completed'``) so retrying a genuinely failed or
  still-pending job is unaffected -- only a *successful* publish occupies the
  slot.
- ``owner_tenant_id`` leads the key because it is the real caller scope.
  ``tenant_id`` is the shared SA channel tenant and is the SAME value for every
  caller, so keying on it would make one tenant's publish block another's.
- Rows with a NULL ``owner_tenant_id`` (pre-``005`` legacy) are exempt, since a
  NULL never collides in a unique index. ``create_job`` refuses to write a NULL
  owner, so no new row can land in that gap.

Pre-flight: if the table ALREADY contains duplicate completed rows, creating the
index fails with a bare ``duplicate key value violates unique constraint``,
which names neither the tenant nor the render. The DO block below fails first
with the offending pairs, so the operator gets an actionable message instead of
having to reverse-engineer one from a wedged rollout.

Idempotent (``CREATE UNIQUE INDEX IF NOT EXISTS``) and reversible (``DROP INDEX
IF EXISTS``). The service role owns ``upload_jobs`` (created in 001).

Revision ID: 008_publish_once_unique_index
Revises: 007_file_source_type_default_url
Create Date: 2026-08-21
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "008_publish_once_unique_index"
down_revision = "007_file_source_type_default_url"
branch_labels = None
depends_on = None

INDEX_NAME = "uq_upload_jobs_completed_owner_source"

_PREFLIGHT = """
DO $pre$
DECLARE
  v_dupes text;
BEGIN
  SELECT string_agg(format('(%s, %s) x%s', owner_tenant_id, file_source, n), '; ')
    INTO v_dupes
    FROM (
      SELECT owner_tenant_id, file_source, count(*) AS n
        FROM upload_jobs
       WHERE status = 'completed'
         AND owner_tenant_id IS NOT NULL
       GROUP BY owner_tenant_id, file_source
      HAVING count(*) > 1
    ) d;
  IF v_dupes IS NOT NULL THEN
    RAISE EXCEPTION
      'upload_jobs already contains renders published to YouTube more than once, '
      'so the publish-once index cannot be created. Duplicated (owner_tenant_id, '
      'file_source): %. Decide which youtube_video_id to keep, delete or re-status '
      'the redundant upload_jobs rows (and take the extra video down on the '
      'channel), then re-run this migration.', v_dupes;
  END IF;
END
$pre$
"""


def upgrade() -> None:
    op.execute(_PREFLIGHT)
    op.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {INDEX_NAME} "
        "ON upload_jobs (owner_tenant_id, file_source) "
        "WHERE status = 'completed'"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")
