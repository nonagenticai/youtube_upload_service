"""SECURITY DEFINER maintenance resolvers for the scale-in reaper (single source).

An upload job runs as a fire-and-forget in-process background task under a DB
lease that a heartbeat renews while the upload is live. If the pod is terminated
mid-upload (a scale-in or crash), the job is stranded: left non-terminal
(``processing``, or ``pending`` if the pod died before the task started) with no
live owner, and nothing ever finishes or retries it.

Two functions reclaim those strands:

- ``reap_stranded_upload_jobs(p_grace_seconds)`` — fails every non-terminal job
  whose lease has expired (owner died: the heartbeat would otherwise keep a live
  upload's lease fresh) OR that never acquired a lease and is older than the
  grace window (a legacy row, a job that queued behind the concurrency
  semaphore past the grace window, or a pod that died between create-commit and
  task-start). The failure message distinguishes those two histories — see the
  comment above ``REAP_FN_SQL``; a never-claimed job definitely uploaded
  nothing, a lease-lost job may already have published.
- ``expire_upload_job_leases(p_owner)`` — expires the calling pod's in-flight
  leases at shutdown so a successor reaps them on its next sweep instead of
  waiting out the full lease TTL (the "shutdown lease-release" / preStop drain).

Why SECURITY DEFINER: under Phase D RLS **enforce** the runtime connects as the
restricted ``w2v_youtube_app`` role with no ``app.current_tenant`` bound (the
reaper spans ALL tenants), so a plain cross-tenant ``UPDATE upload_jobs`` would
fail-closed to 0 rows. These functions are owned by the table owner → bypass
ENABLE RLS. Mirrors ``html_render_service``'s ``claim_next_render_job`` resolver.

EXECUTE is deliberately left at the default (granted to PUBLIC) rather than
REVOKEd + GRANTed to the app role: the app role does not exist in CI, and the
reaper only FAILS already-stranded jobs (a bounded blast radius, unlike a claim
resolver that moves work), so no out-of-band DBA GRANT is needed for the runtime
role to call it. Single-sourced so BOTH the migration (prod) and the
``create_all`` ``after_create`` event (tests) build identical functions.
"""

from __future__ import annotations

from sqlalchemy import Connection, MetaData, event, text

# The reaper only ever touches non-terminal jobs, so a completed/failed job is
# never re-failed regardless of its (stale) lease columns.
#
# ``error_message`` is branched on ``lease_owner``, because the two reap
# predicates below describe two genuinely different histories and a single
# message can only be true of one of them:
#
# - ``lease_owner IS NULL`` — the row was NEVER claimed. The lease columns are
#   stamped only at claim time (``UploadService._process_job``), never at
#   enqueue, so a job that merely sat behind the ``UPLOAD_MAX_CONCURRENT``
#   semaphore for longer than the grace window arrives here with a NULL lease
#   and no pod has ever touched it. Reporting that as "interrupted by pod
#   scale-in/crash" is simply false, and it sends the operator hunting a
#   termination event that never happened.
# - ``lease_owner IS NOT NULL`` — the row WAS claimed and then lost its lease:
#   the owning pod stopped heartbeating. That one really is a crash/scale-in.
#
# Neither message promises "safe to retry" any more. A lost lease does not mean
# the upload did not happen — the pod can complete the YouTube upload and only
# then fail to write the row back — so re-publishing blind is how the same paid
# render reaches YouTube twice.
REAP_FN_SQL = """
CREATE OR REPLACE FUNCTION reap_stranded_upload_jobs(p_grace_seconds int)
RETURNS int LANGUAGE plpgsql SECURITY DEFINER SET search_path FROM CURRENT AS $fn$
DECLARE
  v_count int;
BEGIN
  UPDATE upload_jobs
    SET status = 'failed',
        error_message = CASE
          WHEN lease_owner IS NULL THEN
            'Upload never started: no worker ever claimed this job and it is older '
            'than the reaper grace window (queued behind the concurrency limit, or '
            'the pod died before the background task began). Nothing was uploaded '
            'to YouTube, so it is safe to retry.'
          ELSE
            'Upload interrupted: the worker claimed this job and then stopped '
            'renewing its lease (pod scale-in or crash). The upload may already '
            'have reached YouTube -- check the channel before retrying.'
        END,
        completed_at = now()
  WHERE status IN ('pending', 'processing')
    AND (
      -- Lease expired → owning pod is gone. A live upload's heartbeat keeps this
      -- in the future, so an expired lease is never a running job.
      (lease_expires_at IS NOT NULL AND lease_expires_at < now())
      -- Never leased and older than the grace window → a legacy row, or a pod
      -- that died between create-commit and background-task start. The grace
      -- keeps a freshly-created (still-queued) pending job from being reaped.
      OR (lease_expires_at IS NULL
          AND created_at < now() - make_interval(secs => p_grace_seconds))
    );
  GET DIAGNOSTICS v_count = ROW_COUNT;
  RETURN v_count;
END
$fn$
"""

EXPIRE_FN_SQL = """
CREATE OR REPLACE FUNCTION expire_upload_job_leases(p_owner text)
RETURNS int LANGUAGE plpgsql SECURITY DEFINER SET search_path FROM CURRENT AS $fn$
DECLARE
  v_count int;
BEGIN
  UPDATE upload_jobs
    SET lease_expires_at = now()
  WHERE lease_owner = p_owner
    AND status = 'processing'
    AND (lease_expires_at IS NULL OR lease_expires_at > now());
  GET DIAGNOSTICS v_count = ROW_COUNT;
  RETURN v_count;
END
$fn$
"""

# Operator listing across ALL tenants (``GET /api/admin/tenant-settings``). Under
# RLS enforce the runtime role sees only the bound tenant's rows, and the
# operator lane binds none — so the cross-tenant read goes through this
# SECURITY DEFINER function (owned by the table owner → bypasses ENABLE RLS),
# exactly like the reaper. It returns booleans + tenant ids only, never a
# secret column. EXECUTE is revoked from PUBLIC; migration 014 grants it to
# ``w2v_youtube_app`` when that role exists. Migration 014 carries a pinned
# literal copy of this SQL (migrations never import live definitions);
# ``tests/unit/test_youtube_tenant_config.py`` asserts the two are identical.
LIST_TENANT_SETTINGS_FN_SQL = """
CREATE OR REPLACE FUNCTION list_tenant_youtube_settings()
RETURNS TABLE (
  tenant_id uuid,
  platform_channel_allowed boolean,
  has_grant boolean,
  has_oauth_client boolean
)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path FROM CURRENT AS $fn$
  SELECT COALESCE(s.tenant_id, t.tenant_id),
         s.platform_channel_allowed,
         t.tenant_id IS NOT NULL,
         COALESCE(s.oauth_client_id IS NOT NULL, false)
    FROM tenant_youtube_settings s
    FULL OUTER JOIN oauth_tokens t ON t.tenant_id = s.tenant_id
   ORDER BY 1
$fn$
"""


def register_reaper_fns(metadata: MetaData) -> None:
    """Create the reaper resolvers after the tables exist (test create_all path)."""

    def _apply(target: object, connection: Connection, **kw: object) -> None:
        connection.execute(text(REAP_FN_SQL))
        connection.execute(text(EXPIRE_FN_SQL))
        connection.execute(text(LIST_TENANT_SETTINGS_FN_SQL))
        connection.execute(
            text("REVOKE EXECUTE ON FUNCTION list_tenant_youtube_settings() FROM PUBLIC")
        )

    event.listen(metadata, "after_create", _apply)
