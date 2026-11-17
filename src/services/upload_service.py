"""Upload orchestration service.

Creates upload jobs in the database, fires background tasks to process them,
handles file download (for URL sources), and delegates the actual upload to
:class:`YouTubeService`.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import os
import socket
import tempfile
import uuid
from pathlib import Path
from typing import Any, cast

import httpx
from google.oauth2.credentials import Credentials
from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from w2v_common.database.rls import bind_session_tenant
from w2v_common.tenant import TenantError

from src.config import Settings
from src.database.models import UploadJob
from src.infra.task_manager import TaskManager
from src.services.oauth_service import (
    PUBLISHED_VIA_PLATFORM,
    PUBLISHED_VIA_TENANT,
    OAuthService,
    PublishedVia,
    YouTubeCredentialsUnavailableError,
)
from src.services.url_guard import UnsafeURLError, pin_request, validate_url
from src.services.youtube_service import RAISABLE_PRIVACY, YouTubeService
from src.tenant import require_tenant

logger = logging.getLogger(__name__)

# Maximum number of HTTP redirects we follow manually for upload-source URLs.
# We do NOT use httpx's built-in follow_redirects because each hop must be
# re-validated by the SSRF guard (a public host may 302 to 127.0.0.1).
_MAX_REDIRECTS = 3

# Hard ceiling on one page of upload jobs (mirrors the API route's bound).
MAX_PAGE_SIZE = 200

# NOTE: the ``UNBOUND_OWNER_TENANTS`` frozenset that used to live here is GONE.
# It was a lowercase string denylist (``anonymous``/``none``/``null`` plus the
# dashed nil UUID) standing in for "no real caller tenant was bound", and it had
# a hole it could not close: the nil UUID has four accepted spellings and the set
# named exactly one of them, so ``00000000000000000000000000000000`` and the
# braced/urn forms passed the guard and were written as an unreadable
# ``owner_tenant_id``. :func:`src.tenant.require_tenant` replaces it and is
# strictly stronger — it rejects by parsed VALUE (``parsed.int == 0``), which
# covers every spelling, and every slug the denylist named is also not a UUID.
#
# ``00000000-0000-0000-0000-000000000001`` remains ACCEPTED. It looks like a
# placeholder and this module used to treat it as one, but it is a real ``active``
# row in the orchestrator ``tenants`` table owning 97 projects and 9
# ``tenant_members`` (measured on the prod primary 2026-08-21). Rejecting it
# locked the largest tenant in the system out of creating uploads.


class AlreadyPublishedError(Exception):
    """Raised when a render has already been published to YouTube for this tenant.

    Carries the existing ``youtube_video_id``/``youtube_url`` so the caller (and
    the operator reading the 409) is told WHICH video the render became, instead
    of a bare "duplicate" they have to go and look up.
    """

    def __init__(
        self,
        file_source: str,
        job_id: str,
        youtube_video_id: str,
        youtube_url: str | None,
    ) -> None:
        self.file_source = file_source
        self.job_id = job_id
        self.youtube_video_id = youtube_video_id
        self.youtube_url = youtube_url
        super().__init__(
            f"Refusing to publish {file_source!r} again: it was already published to "
            f"YouTube as {youtube_video_id} ({youtube_url or 'no url recorded'}) by "
            f"upload job {job_id}. If that video is wrong, delete it on the channel "
            f"first; re-submitting would put the same render on the channel twice."
        )


class NotPublishedError(Exception):
    """A retraction was asked for a job that never put a video on YouTube."""

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        super().__init__(f"Upload job {job_id} has no YouTube video to retract")


class RetractUnavailableError(Exception):
    """No YouTube credentials can act on the channel the job published to."""


def _present(**fields: object) -> dict[str, object]:
    """Drop the ``None`` fields — a `None` means "caller did not supply it", not
    "write NULL". Keeping this out of the update method keeps that method's
    branch count under the complexity gate."""
    return {k: v for k, v in fields.items() if v is not None}


class UploadService:
    """Orchestrates async video upload jobs."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        oauth_service: OAuthService,
        youtube_service: YouTubeService,
        task_manager: TaskManager,
        settings: Settings,
        pod_id: str | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._oauth = oauth_service
        self._youtube = youtube_service
        self._task_manager = task_manager
        self._settings = settings
        # Stamped onto ``lease_owner`` for every in-flight upload so the shutdown
        # lease-release can expire exactly this pod's leases. Defaults to the
        # hostname (the k8s pod name), matching the reaper's release predicate.
        self._pod_id = (pod_id or socket.gethostname())[:64]

    async def _credentials_for_job(self, job: UploadJob) -> tuple[Credentials, PublishedVia]:
        """The credentials that PUBLISH ``job`` and the channel they act on: its OWNER's.

        ``job.owner_tenant_id`` is the real caller tenant (``X-Owner-Tenant-Id``,
        SA-only). ``job.tenant_id`` is NOT used: it is the orchestrator SA's
        tenant whenever the SA call was not delegated — the same value for every
        caller — so loading by it ignored every tenant's own grant and, with
        ``YOUTUBE_PLATFORM_CHANNEL_AS_DEFAULT=true``, sent every video to the
        platform channel while ``/api/auth/status`` could say "connected".

        ``load_credentials`` binds the ``oauth_tokens`` RLS GUC to the owner
        tenant and writes a refreshed token back under the same tenant.

        A row with NO owner (pre-005 rows; ``create_job`` refuses to write new
        ones) is treated exactly like a tenant without a grant: the platform
        channel only when the operator flag is on, else "connect YouTube". It is
        NEVER resolved to the channel/SA tenant.

        The returned ``published_via`` ('platform' | 'tenant') is persisted on
        the row with the video id; :meth:`_credentials_for_published` reads it.
        """
        owner = job.owner_tenant_id
        if owner:
            return await self._oauth.resolve_credentials(str(owner))
        return await self._oauth.resolve_credentials_without_grant(
            f"<unrecorded owner of upload job {job.job_id}>"
        )

    async def _credentials_for_published(self, job: UploadJob) -> Credentials:
        """Credentials for the channel ``job``'s video ACTUALLY lives on (retract/privacy).

        - ``platform`` → the platform channel, regardless of any grant the owner
          made since (its own channel does not hold this video → YouTube 404).
          Unconfigured platform credential → a clear error, never the tenant's.
        - ``tenant`` → the owner's grant; if it is gone/flagged, the usual
          "connect"/"reconnect YouTube" error. Never the platform channel.
        - NULL (legacy, predates migration 013) → the publish-time resolution
          (owner grant, else platform when the operator flag is on).
        """
        via = job.published_via
        if via == PUBLISHED_VIA_PLATFORM:
            return await self._oauth.platform_credentials_for_published(f"Upload job {job.job_id}")
        if via == PUBLISHED_VIA_TENANT and job.owner_tenant_id:
            credentials, _via = await self._oauth.resolve_credentials(
                str(job.owner_tenant_id), allow_platform=False
            )
            return credentials
        credentials, _via = await self._credentials_for_job(job)
        return credentials

    async def create_job(
        self,
        tenant_id: str,
        file_source: str,
        file_source_type: str,
        title: str,
        *,
        owner_tenant_id: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
        category_id: str = "22",
        privacy_status: str = "private",
        thumbnail_source: str | None = None,
        thumbnail_source_type: str | None = None,
        caption_source: str | None = None,
        caption_source_type: str | None = None,
        caption_language: str | None = None,
    ) -> UploadJob:
        """Create an upload job in the DB and fire a background task.

        Returns the newly created :class:`UploadJob` row.

        Raises:
            ValueError: if ``owner_tenant_id`` is missing/blank or a placeholder
                sentinel — i.e. the caller tenant was not bound. Fail closed
                rather than write a row nobody can read back.
            AlreadyPublishedError: if this caller has already published this
                exact ``file_source`` to YouTube. This route is the ONLY caller
                of the upload path — there is no automatic retry anywhere — so
                a second request is always a human re-submitting, typically
                after reading a job row the reaper failed. The reaper can fail a
                row whose upload actually succeeded (it fails on a lost lease,
                and the pod can publish and only then fail to write back), so
                "the job says failed" is not evidence that nothing shipped.
                Refuse and name the existing video instead of paying for and
                publishing the same render twice.
        """
        job_id = str(uuid.uuid7())

        # FAIL CLOSED: never substitute a sentinel or the channel tenant for a
        # missing caller. ``tenant_id`` is the SA *channel* tenant and is the
        # same for every caller, so falling back to it (the old behaviour)
        # manufactured a row whose ``owner_tenant_id`` no real caller can ever
        # match — 35 of 48 prod rows were born unreadable this way.
        #
        # This is the service-level backstop for the route's 400 — a background or
        # direct caller never passes through ``get_owner_tenant_id``. It raises
        # ``ValueError``; ``require_tenant``'s ``TenantError`` IS a ``ValueError``,
        # so every existing caller that catches ValueError keeps working, while the
        # message now also names the nil UUID in any of its four spellings.
        try:
            owner = str(require_tenant(owner_tenant_id, source="owner_tenant_id"))
        except TenantError as exc:
            raise ValueError(
                "Refusing to create upload job: owner_tenant_id is required but the "
                f"caller tenant was not bound ({exc}). The caller must "
                "forward the X-Owner-Tenant-Id header with its real tenant; the channel "
                f"tenant {tenant_id!r} is NOT a valid substitute — a job written with it "
                "is unreadable by its own poller."
            ) from exc

        async with self._session_factory() as session:
            await bind_session_tenant(session, require_tenant(tenant_id))
            # Publish-once pre-claim check. Runs BEFORE the insert so a duplicate
            # request costs nothing and never reaches YouTube. The DB-level
            # backstop is the partial unique index added in migration 008.
            await self._reject_if_already_published(session, owner, file_source)
            job = UploadJob(
                job_id=job_id,
                tenant_id=tenant_id,
                # Validated above — never defaulted to the channel tenant.
                owner_tenant_id=owner,
                status="pending",
                file_source=file_source,
                file_source_type=file_source_type,
                title=title,
                description=description,
                tags=tags,
                category_id=category_id,
                privacy_status=privacy_status,
                thumbnail_source=thumbnail_source,
                thumbnail_source_type=thumbnail_source_type,
                caption_source=caption_source,
                caption_source_type=caption_source_type,
                caption_language=caption_language,
            )
            session.add(job)
            # flush + refresh BEFORE commit so the re-read runs inside the
            # tenant-GUC-bound transaction. The GUC is transaction-scoped
            # (set_config is_local=true), so a refresh AFTER commit runs with no
            # tenant bound → RLS hides the just-inserted row → "Could not refresh
            # instance" (500). Keeping the refresh pre-commit fixes that.
            await session.flush()
            await session.refresh(job)
            await session.commit()
            logger.info("Created upload job %s for tenant %s", job_id, tenant_id)

        # Fire background processing
        await self._task_manager.submit(
            job_id,
            self._process_job(job_id, tenant_id),
        )

        return job

    async def _reject_if_already_published(
        self, session: AsyncSession, owner_tenant_id: str, file_source: str
    ) -> None:
        """Raise :class:`AlreadyPublishedError` if this render is already on YouTube.

        Keyed on ``youtube_video_id IS NOT NULL`` rather than
        ``status == 'completed'`` **on purpose**. The dangerous row is precisely
        the one whose status lies: when the reaper fails a job whose upload then
        succeeds, the finishing task cannot flip the row back to ``completed``
        (the compare-and-set correctly refuses), so the row reads ``failed``
        while a real video exists on the channel. ``_record_published_video``
        stamps the video id onto exactly that row, and this check reads it. A
        status-only check would wave the duplicate straight through — the one
        case worth catching.

        ``session`` is the caller's, already bound to the RLS tenant GUC; the
        lookup must run inside that transaction, not a fresh one.
        """
        stmt = select(UploadJob).where(
            UploadJob.owner_tenant_id == owner_tenant_id,
            UploadJob.file_source == file_source,
            UploadJob.youtube_video_id.is_not(None),
        )
        published = (await session.execute(stmt)).scalars().first()
        if published is None:
            return
        # Narrowed for the type checker: the WHERE clause guarantees non-NULL.
        video_id = published.youtube_video_id or ""
        logger.warning(
            "Refusing duplicate publish of %s for owner tenant %s: already on YouTube as "
            "%s (job %s)",
            file_source,
            owner_tenant_id,
            video_id,
            published.job_id,
        )
        raise AlreadyPublishedError(file_source, published.job_id, video_id, published.youtube_url)

    async def _record_published_video(
        self,
        job_id: str,
        tenant_id: str,
        video_id: str,
        video_url: str,
        *,
        published_via: PublishedVia | None = None,
        thumbnail_applied: bool | None = None,
        caption_applied: bool | None = None,
    ) -> None:
        """Stamp the YouTube result onto a row we are no longer allowed to advance.

        Deliberately guard-free (no ``expected_status``, no lease check): the
        whole point is that the compare-and-set has just refused, because the
        reaper already terminalised this row while the upload was in fact
        succeeding. The status stays whatever the reaper wrote — we do NOT
        resurrect it — but the video id is written so the fact that this render
        IS on the channel survives in the database instead of only in a log
        line. ``_reject_if_already_published`` reads exactly this, which is what
        stops the operator re-publishing the same paid render.

        Best-effort: if this write also fails, the loud ERROR the caller logs is
        still the record of last resort.
        """
        async with self._session_factory() as session:
            await bind_session_tenant(session, require_tenant(tenant_id))
            await session.execute(
                update(UploadJob)
                .where(UploadJob.job_id == job_id)
                .values(
                    _present(
                        youtube_video_id=video_id,
                        youtube_url=video_url,
                        published_via=published_via,
                        thumbnail_applied=thumbnail_applied,
                        caption_applied=caption_applied,
                    )
                )
            )
            await session.commit()

    async def get_job(
        self, job_id: str, channel_tenant_id: str, owner_tenant_id: str
    ) -> UploadJob | None:
        """Fetch a single upload job by ID, scoped to the REAL caller.

        ``channel_tenant_id`` (the SA channel tenant the rows live under) binds
        the RLS GUC so the rows are visible; ``owner_tenant_id`` (the real
        caller) is the application-level scope so a tenant only sees its own job.
        """
        async with self._session_factory() as session:
            await bind_session_tenant(
                session, require_tenant(channel_tenant_id, source="channel tenant")
            )
            stmt = select(UploadJob).where(
                UploadJob.job_id == job_id,
                UploadJob.owner_tenant_id == owner_tenant_id,
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def retract_job(
        self, job_id: str, channel_tenant_id: str, owner_tenant_id: str
    ) -> UploadJob | None:
        """Take a published job's video off YouTube by setting it ``private``.

        Scoped exactly like :meth:`get_job` (a tenant can only retract its OWN job;
        another owner's job is ``None`` → 404, indistinguishable from absent).
        Private, not deleted: the video stays on the channel, so a retraction made
        in error is reversed in YouTube Studio without re-rendering or re-uploading.
        Idempotent — retracting an already-private video succeeds and writes nothing
        new to YouTube. The row's ``privacy_status`` is updated only AFTER YouTube
        accepted the change, so the stored value never claims a takedown that did not
        happen.

        Raises :class:`NotPublishedError` when the job has no ``youtube_video_id``,
        :class:`RetractUnavailableError` when no credentials exist for the channel;
        YouTube API errors propagate to the route.
        """
        job = await self.get_job(job_id, channel_tenant_id, owner_tenant_id)
        if job is None:
            return None
        if not job.youtube_video_id:
            raise NotPublishedError(job_id)
        try:
            credentials = await self._credentials_for_published(job)
        except YouTubeCredentialsUnavailableError as exc:
            raise RetractUnavailableError(
                f"No usable YouTube credentials for the channel of upload job {job_id}: {exc}"
            ) from exc
        await self._youtube.async_set_private(credentials, job.youtube_video_id)
        async with self._session_factory() as session:
            await bind_session_tenant(
                session, require_tenant(channel_tenant_id, source="channel tenant")
            )
            await session.execute(
                update(UploadJob)
                .where(
                    UploadJob.job_id == job_id,
                    UploadJob.owner_tenant_id == owner_tenant_id,
                )
                .values(privacy_status="private")
            )
            await session.commit()
        logger.warning(
            "Retracted upload job %s (video %s) to private for owner tenant %s",
            job_id,
            job.youtube_video_id,
            owner_tenant_id,
        )
        return await self.get_job(job_id, channel_tenant_id, owner_tenant_id)

    async def set_job_privacy(
        self, job_id: str, channel_tenant_id: str, owner_tenant_id: str, target: str
    ) -> UploadJob | None:
        """Raise a published job's video to ``unlisted`` or ``public`` (a governed release).

        The counterpart of :meth:`retract_job`, scoped identically (another owner's job is
        ``None`` → 404). The caller is the governed ``marketing.publish_video`` release of a
        video that was uploaded private. Never deletes. Idempotent — a video already at
        ``target`` is not written again on YouTube. The row's ``privacy_status`` is written
        only AFTER YouTube accepted the change.

        Raises :class:`NotPublishedError` when the job has no ``youtube_video_id``,
        :class:`RetractUnavailableError` when no credentials exist for the channel,
        ``ValueError`` for a target outside ``RAISABLE_PRIVACY``; YouTube API errors propagate.
        """
        if target not in RAISABLE_PRIVACY:
            raise ValueError(f"privacy target must be one of {sorted(RAISABLE_PRIVACY)}")
        job = await self.get_job(job_id, channel_tenant_id, owner_tenant_id)
        if job is None:
            return None
        if not job.youtube_video_id:
            raise NotPublishedError(job_id)
        try:
            credentials = await self._credentials_for_published(job)
        except YouTubeCredentialsUnavailableError as exc:
            raise RetractUnavailableError(
                f"No usable YouTube credentials for the channel of upload job {job_id}: {exc}"
            ) from exc
        await self._youtube.async_set_privacy(credentials, job.youtube_video_id, target)
        async with self._session_factory() as session:
            await bind_session_tenant(
                session, require_tenant(channel_tenant_id, source="channel tenant")
            )
            await session.execute(
                update(UploadJob)
                .where(
                    UploadJob.job_id == job_id,
                    UploadJob.owner_tenant_id == owner_tenant_id,
                )
                .values(privacy_status=target)
            )
            await session.commit()
        logger.warning(
            "Set upload job %s (video %s) to %s for owner tenant %s",
            job_id,
            job.youtube_video_id,
            target,
            owner_tenant_id,
        )
        return await self.get_job(job_id, channel_tenant_id, owner_tenant_id)

    async def list_jobs(
        self,
        channel_tenant_id: str,
        owner_tenant_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[UploadJob], int]:
        """List the calling tenant's upload jobs, newest first.

        ``channel_tenant_id`` binds RLS (rows live under the SA channel tenant);
        ``owner_tenant_id`` is the real-caller application scope. Returns
        ``(jobs, total_count)``.

        ``limit``/``offset`` are clamped here as well as bounded by the route's
        ``Query`` constraints: Postgres rejects a negative row count outright
        (``22023``), so an unclamped value reaching ``.limit()``/``.offset()``
        is a 500 rather than a 422, and an unclamped upper bound is an
        unbounded read. The route is the primary gate; this is the backstop for
        direct/background callers that never pass through FastAPI validation.
        """
        limit = max(1, min(limit, MAX_PAGE_SIZE))
        offset = max(0, offset)
        async with self._session_factory() as session:
            await bind_session_tenant(
                session, require_tenant(channel_tenant_id, source="channel tenant")
            )
            count_stmt = (
                select(func.count())
                .select_from(UploadJob)
                .where(UploadJob.owner_tenant_id == owner_tenant_id)
            )
            total = (await session.execute(count_stmt)).scalar() or 0

            # Fetch
            stmt = (
                select(UploadJob)
                .where(UploadJob.owner_tenant_id == owner_tenant_id)
                .order_by(UploadJob.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
            result = await session.execute(stmt)
            jobs = list(result.scalars().all())

            return jobs, total

    # ------------------------------------------------------------------
    # Background processing
    # ------------------------------------------------------------------

    async def _process_job(self, job_id: str, tenant_id: str) -> None:
        """Background task wrapper: mark processing, acquire+heartbeat the reaper
        lease, run the upload, and mark terminal.

        The lease + heartbeat live here (not in :meth:`_run_upload`) so that if
        this pod is terminated mid-upload the lease stops being renewed and the
        reaper (:func:`reap_stranded_upload_jobs`) fails the job as retryable
        rather than leaving it stranded in ``processing`` forever.

        The job is **claimed** with a compare-and-set (``pending`` → ``processing``)
        before any work happens. A task that queued behind the concurrency
        semaphore long enough for the reaper to fail its row loses that CAS and
        abandons here — otherwise it would flip the reaped job back to
        ``processing`` and, after the orchestrator had already retried, publish
        the same paid render to YouTube a second time."""
        from src.logging_config import job_id_var, tenant_id_var

        job_id_var.set(job_id)
        tenant_id_var.set(tenant_id)

        now = dt.datetime.now(dt.UTC)
        try:
            claimed = await self._update_job_status(
                job_id,
                "processing",
                tenant_id,
                started_at=now,
                lease_owner=self._pod_id,
                lease_expires_at=now + dt.timedelta(seconds=self._settings.upload.lease_seconds),
                expected_status="pending",
            )
        except Exception:
            logger.exception("Could not claim upload job %s; abandoning", job_id)
            return
        if not claimed:
            logger.warning(
                "Upload job %s was not claimable (already reaped or claimed elsewhere); "
                "abandoning without uploading",
                job_id,
            )
            return

        stop_heartbeat = asyncio.Event()
        heartbeat: asyncio.Task[None] | None = None
        try:
            heartbeat = asyncio.create_task(self._heartbeat(job_id, tenant_id, stop_heartbeat))
            await self._run_upload(job_id, tenant_id)
        except Exception as exc:
            logger.exception("Upload job %s failed", job_id)
            # Guarded: if the reaper already terminalised this row we must not
            # overwrite its retryable message with ours.
            #
            # This write is itself best-effort: it can throw (DB down, pool
            # exhausted, ...), and an unguarded throw here would propagate out
            # of _process_job and replace the real upload failure (SSRF
            # rejection, YouTube API error, size limit, ...) with a DB error in
            # whatever logs it as "Background job %s failed" -- exactly the
            # failure this repo's other except-arm helper calls are guarded
            # against. If it fails, the row stays "processing" until the
            # lease expires and the reaper reclaims it as retryable; that is
            # the acceptable degraded path, not losing the real cause.
            try:
                await self._update_job_status(
                    job_id,
                    "failed",
                    tenant_id,
                    completed_at=dt.datetime.now(dt.UTC),
                    error_message=str(exc)[:1000],
                    expected_status="processing",
                    require_lease_owner=True,
                )
            except Exception:
                logger.exception(
                    "Upload job %s failed AND could not be marked 'failed' in "
                    "the DB; row stays 'processing' until the lease expires "
                    "and the reaper reclaims it. Original failure logged "
                    "above as %r",
                    job_id,
                    exc,
                )
        finally:
            # Stop the lease heartbeat first: once the job is terminal (or the
            # task is being torn down) the lease must not keep being renewed.
            stop_heartbeat.set()
            if heartbeat is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await heartbeat

    async def _run_upload(self, job_id: str, tenant_id: str) -> None:
        """Core upload work: resolve the source, upload to YouTube, mark completed.

        Raises on any failure so :meth:`_process_job` marks the job ``failed``."""
        temp_file: str | None = None
        try:
            # Load job metadata FIRST: the credentials are the job OWNER's, so
            # the row must be read before they can be chosen. ``tenant_id``
            # (the channel/SA tenant the row lives under) only binds RLS here.
            job = await self._get_job_by_id(job_id, tenant_id)
            if job is None:
                raise RuntimeError(f"Job {job_id} not found")

            # Load credentials of the job's OWNER tenant.
            # Raises a YouTubeCredentialsUnavailableError naming the fix ("connect
            # YouTube" / "reconnect YouTube"); its message becomes the job's
            # error_message. There is no silent platform-channel fallback.
            credentials, published_via = await self._credentials_for_job(job)

            # Resolve file path. ``url`` is the only mode we serve: a path-mode
            # row would make us read this pod's own filesystem (no PVC is
            # mounted) and upload those bytes to YouTube. The API can no longer
            # create such a row, but rows written while the column's
            # server_default was ``'path'`` are still in the table.
            if job.file_source_type != "url":
                raise ValueError(
                    f"Unsupported file_source_type {job.file_source_type!r}: "
                    "only 'url' sources are accepted"
                )
            temp_file = await self._download_file(job.file_source, job_id)
            file_path = temp_file

            # Validate file size
            max_bytes = self._settings.upload.max_file_size_mb * 1024 * 1024
            file_size = Path(file_path).stat().st_size
            if file_size > max_bytes:
                raise ValueError(
                    f"File size {file_size / 1024 / 1024:.0f}MB exceeds "
                    f"limit of {self._settings.upload.max_file_size_mb}MB"
                )

            # Upload to YouTube
            # tags is JSONB (stored as list[str] but typed as dict by SQLAlchemy)
            tags: list[str] | None = job.tags if isinstance(job.tags, list) else None
            result = await self._youtube.async_upload_video(
                credentials,
                file_path,
                job.title,
                description=job.description,
                tags=tags,
                category_id=job.category_id,
                privacy_status=job.privacy_status,
            )

            # Best-effort custom thumbnail (no-ops when the job has none). A
            # custom thumbnail requires a verified YouTube channel (unverified →
            # 403); the video is already uploaded, so this must never fail the job.
            thumbnail_applied = await self._apply_thumbnail(
                credentials, result["video_id"], job, job_id
            )

            # Best-effort soft closed-captions (no-ops when the job has none).
            # captions().insert needs a youtube.force-ssl token; a token without
            # it 403s. The video is already uploaded, so this must never fail
            # the job.
            caption_applied = await self._apply_captions(
                credentials, result["video_id"], job, job_id
            )

            # Mark completed — guarded, so a task whose lease was reaped
            # mid-upload cannot resurrect the row the reaper already failed.
            marked = await self._update_job_status(
                job_id,
                "completed",
                tenant_id,
                completed_at=dt.datetime.now(dt.UTC),
                youtube_video_id=result["video_id"],
                youtube_url=result["url"],
                published_via=published_via,
                thumbnail_applied=thumbnail_applied,
                caption_applied=caption_applied,
                expected_status="processing",
                require_lease_owner=True,
            )
            if not marked:
                # The upload DID happen; the row's STATUS is no longer ours to
                # write. Record the video id anyway (status untouched) so the
                # publish is durable evidence rather than a log line: this is
                # what ``_reject_if_already_published`` reads to stop a human
                # who sees the reaper's "failed" from publishing it a second
                # time. Best-effort — never let it mask the ERROR below.
                with contextlib.suppress(Exception):
                    await self._record_published_video(
                        job_id,
                        tenant_id,
                        result["video_id"],
                        result["url"],
                        published_via=published_via,
                        thumbnail_applied=thumbnail_applied,
                        caption_applied=caption_applied,
                    )
                # Logged loudly because a human may act on the reaper's
                # "failed" status.
                logger.error(
                    "Upload job %s was reaped while uploading; video %s (%s) is published "
                    "but the job row could not be marked completed",
                    job_id,
                    result["video_id"],
                    result["url"],
                )
                return
            logger.info("Upload job %s completed: %s", job_id, result["url"])
        finally:
            if temp_file:
                temp_path = Path(temp_file)
                if temp_path.exists():
                    try:
                        temp_path.unlink()
                    except OSError:
                        logger.warning("Failed to clean up temp file: %s", temp_file)

    async def _heartbeat(self, job_id: str, tenant_id: str, stop: asyncio.Event) -> None:
        """Renew the job's lease every ~lease_seconds/3 until ``stop`` is set.

        Keeps a live (possibly multi-GB, multi-minute) upload's lease in the
        future so the reaper never mistakes it for a stranded job. Best-effort: a
        missed beat only shortens the lease; a genuinely dead pod stops beating
        entirely and the lease expires."""
        interval = max(5, self._settings.upload.lease_seconds // 3)
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return  # stop set → job finished / task ending
            except TimeoutError:
                pass
            try:
                await self._renew_lease(job_id, tenant_id)
            except Exception:  # best-effort; a missed beat just shortens the lease
                logger.warning("Lease renew failed for job %s", job_id, exc_info=True)

    async def _renew_lease(self, job_id: str, tenant_id: str) -> None:
        """Extend a processing job's lease (heartbeat). No-op once it leaves processing.

        Scoped to this pod's lease as well as the status so a heartbeat can never
        keep alive a lease that has been reassigned away from this task."""
        async with self._session_factory() as session:
            await bind_session_tenant(session, require_tenant(tenant_id))
            await session.execute(
                update(UploadJob)
                .where(
                    UploadJob.job_id == job_id,
                    UploadJob.status == "processing",
                    UploadJob.lease_owner == self._pod_id,
                )
                .values(
                    lease_expires_at=dt.datetime.now(dt.UTC)
                    + dt.timedelta(seconds=self._settings.upload.lease_seconds)
                )
            )
            await session.commit()

    async def _apply_thumbnail(
        self,
        credentials: Credentials,
        video_id: str,
        job: UploadJob,
        job_id: str,
    ) -> bool | None:
        """Apply a custom thumbnail to *video_id*, best-effort.

        Downloads the thumbnail (for a URL source, via the same SSRF-guarded
        path as the video) and calls ``thumbnails().set``. Any failure is
        swallowed and logged — a custom thumbnail needs a verified channel and
        the video is already uploaded, so this must never fail the job.

        Returns the OUTCOME so the caller can persist it on the job row:
        ``None`` when no thumbnail was requested, ``True`` when the set call
        returned, ``False`` when it failed. Swallowing the exception keeps the
        publish non-fatal; returning the outcome keeps it from also being
        invisible, which is what made a lost thumbnail indistinguishable from a
        clean publish everywhere except a pod log line.
        """
        source = job.thumbnail_source
        if not source:
            return None
        temp_thumb: str | None = None
        applied = False
        try:
            if (job.thumbnail_source_type or "url") != "url":
                raise ValueError(
                    f"Unsupported thumbnail_source_type {job.thumbnail_source_type!r}: "
                    "only 'url' sources are accepted"
                )
            temp_thumb = await self._download_file(
                source,
                f"{job_id}_thumb",
                max_bytes=self._settings.upload.max_thumbnail_size_mb * 1024 * 1024,
            )
            thumb_path = temp_thumb
            mimetype = "image/png" if thumb_path.lower().endswith(".png") else "image/jpeg"
            await self._youtube.async_set_thumbnail(
                credentials, video_id, thumb_path, mimetype=mimetype
            )
            applied = True
            logger.info("Set custom thumbnail on video %s", video_id)
        except Exception:
            logger.exception("Failed to set custom thumbnail on video %s (non-fatal)", video_id)
        finally:
            if temp_thumb:
                thumb = Path(temp_thumb)
                if thumb.exists():
                    try:
                        thumb.unlink()
                    except OSError:
                        logger.warning("Failed to clean up temp thumbnail: %s", temp_thumb)
        return applied

    async def _apply_captions(
        self,
        credentials: Credentials,
        video_id: str,
        job: UploadJob,
        job_id: str,
    ) -> bool | None:
        """Attach a soft caption track to *video_id*, best-effort.

        Downloads the caption file (for a URL source, via the same SSRF-guarded
        path as the video) and calls ``captions().insert``. Any failure is
        swallowed and logged — captions need a ``youtube.force-ssl`` token and
        the video is already uploaded, so this must never fail the job.

        Returns the OUTCOME so the caller can persist it: ``None`` when no
        caption track was requested, ``True`` when the insert returned,
        ``False`` when it failed. A token minted without ``youtube.force-ssl``
        403s for a WHOLE TENANT, so this failure is systematic rather than
        incidental — exactly the shape that must not stay invisible.
        """
        source = job.caption_source
        if not source:
            return None
        temp_caption: str | None = None
        applied = False
        try:
            if (job.caption_source_type or "url") != "url":
                raise ValueError(
                    f"Unsupported caption_source_type {job.caption_source_type!r}: "
                    "only 'url' sources are accepted"
                )
            temp_caption = await self._download_file(
                source,
                f"{job_id}_caption",
                max_bytes=self._settings.upload.max_caption_size_mb * 1024 * 1024,
            )
            caption_path = temp_caption
            await self._youtube.async_insert_captions(
                credentials,
                video_id,
                caption_path,
                language=job.caption_language or "en",
            )
            applied = True
            logger.info("Attached soft captions on video %s", video_id)
        except Exception:
            logger.exception("Failed to attach captions on video %s (non-fatal)", video_id)
        finally:
            if temp_caption:
                cap = Path(temp_caption)
                if cap.exists():
                    try:
                        cap.unlink()
                    except OSError:
                        logger.warning("Failed to clean up temp caption: %s", temp_caption)
        return applied

    async def _download_file(self, url: str, job_id: str, *, max_bytes: int | None = None) -> str:
        """Download a file from *url* to a temp directory. Returns the file path.

        SSRF defense: the URL (and every redirect hop) is validated by
        :func:`src.services.url_guard.validate_url` before any network I/O. We
        manually follow up to :data:`_MAX_REDIRECTS` redirects so each hop is
        re-checked; httpx's ``follow_redirects`` is disabled for this reason.

        ``max_bytes`` caps the download. It defaults to the *video* limit
        (``upload.max_file_size_mb``, 5 GB); the thumbnail and caption callers
        pass their own, far smaller caps so a tenant cannot stream gigabytes
        into an auxiliary asset that YouTube would reject at a few MB anyway.
        """
        # NOTE: the initial URL is NOT validated here. ``_stream_with_redirects``
        # validates EVERY hop including the first, and pins the connection to the
        # address that validation returned. Validating here as well would resolve
        # the hostname a second time — wasteful, and actively misleading, because
        # the address checked here would not be the address connected to. One
        # validate, one pin, same call. An unsafe URL still raises before any
        # bytes are written, and the partial-file cleanup below removes the empty
        # temp file (asserted by TestDownloadFileSSRF).
        temp_dir = self._settings.upload.temp_dir
        Path(temp_dir).mkdir(parents=True, exist_ok=True)

        # Use job_id as filename prefix
        suffix = Path(url.split("?")[0]).suffix or ".mp4"
        fd, temp_path = tempfile.mkstemp(prefix=f"upload_{job_id}_", suffix=suffix, dir=temp_dir)
        os.close(fd)

        logger.info("Downloading %s to %s", url, temp_path)
        cap = (
            max_bytes
            if max_bytes is not None
            else self._settings.upload.max_file_size_mb * 1024 * 1024
        )

        try:
            downloaded = await self._stream_with_redirects(url, temp_path, cap)
        except Exception:
            # Clean up partial download on any error. The cleanup must never
            # raise: an OSError here (file already gone, EBUSY, permission)
            # would mask the original download error that we are re-raising.
            with contextlib.suppress(OSError):
                Path(temp_path).unlink(missing_ok=True)
            raise

        logger.info("Download complete: %s (%d bytes)", temp_path, downloaded)
        return temp_path

    async def _stream_with_redirects(self, url: str, temp_path: str, max_bytes: int) -> int:
        """Stream *url* into *temp_path*, following ≤ ``_MAX_REDIRECTS`` hops.

        Every hop — including the first — is validated by :func:`validate_url`
        and then **pinned** to the address that validation approved, via
        :func:`pin_request`. Validating a hostname and then passing that
        hostname to httpx would let httpx resolve it a second time, which is the
        DNS-rebinding hole: the guard approves a public address, the resolver
        flips, and the connect lands on an internal one. Pinning removes the
        second lookup, so there is no window to win.

        Returns the number of bytes downloaded.
        """
        current_url = url
        trusted_hosts = self._settings.upload.trusted_download_host_set
        async with httpx.AsyncClient(follow_redirects=False, timeout=300.0) as client:
            for hop in range(_MAX_REDIRECTS + 1):
                pin_ip = validate_url(current_url, trusted_hosts=trusted_hosts)
                request_url, pin_headers, pin_extensions = pin_request(current_url, pin_ip)
                async with client.stream(
                    "GET", request_url, headers=pin_headers, extensions=pin_extensions
                ) as response:
                    if response.is_redirect:
                        if hop == _MAX_REDIRECTS:
                            raise UnsafeURLError(
                                f"Exceeded {_MAX_REDIRECTS} redirects starting from {url!r}"
                            )
                        location = response.headers.get("location")
                        if not location:
                            raise UnsafeURLError(
                                f"Redirect response from {current_url!r} had no Location header"
                            )
                        # Resolve a relative Location against the LOGICAL url, not
                        # response.url — the latter is the pinned literal-IP form,
                        # so joining against it would rewrite the hostname to an IP
                        # and both lose vhost routing and defeat the next hop's
                        # hostname-based checks.
                        next_url = str(httpx.URL(current_url).join(location))
                        logger.info("Following redirect %s -> %s", current_url, next_url)
                        current_url = next_url
                        continue

                    response.raise_for_status()
                    downloaded = 0
                    with Path(temp_path).open("wb") as f:  # noqa: ASYNC230 — why: file handle for streaming async-iter download; sync open() returns handle, not blocking IO
                        async for chunk in response.aiter_bytes(chunk_size=1024 * 1024):
                            downloaded += len(chunk)
                            if downloaded > max_bytes:
                                raise ValueError(
                                    f"Download exceeds {max_bytes / 1024 / 1024:.0f}MB limit"
                                )
                            f.write(chunk)
                    return downloaded

        # Unreachable: the loop either returns, raises, or continues.
        raise UnsafeURLError(  # pragma: no cover
            f"Redirect loop terminated unexpectedly for {url!r}"
        )

    async def _get_job_by_id(self, job_id: str, tenant_id: str) -> UploadJob | None:
        """Fetch a job by ID. ``tenant_id`` binds the RLS GUC (background path)."""
        async with self._session_factory() as session:
            await bind_session_tenant(session, require_tenant(tenant_id))
            stmt = select(UploadJob).where(UploadJob.job_id == job_id)
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def _update_job_status(
        self,
        job_id: str,
        status: str,
        tenant_id: str,
        *,
        started_at: dt.datetime | None = None,
        completed_at: dt.datetime | None = None,
        youtube_video_id: str | None = None,
        youtube_url: str | None = None,
        published_via: PublishedVia | None = None,
        error_message: str | None = None,
        thumbnail_applied: bool | None = None,
        caption_applied: bool | None = None,
        lease_owner: str | None = None,
        lease_expires_at: dt.datetime | None = None,
        expected_status: str | None = None,
        require_lease_owner: bool = False,
    ) -> bool:
        """Update job status and optional result fields. ``tenant_id`` binds the RLS GUC.

        This is a **compare-and-set**: when ``expected_status`` (and/or
        ``require_lease_owner``) is given, the row is only advanced while it
        still looks the way the caller last left it. That is what stops a task
        whose lease was reaped from resurrecting a job the reaper already
        terminalised — without the guard, a reaped ``pending`` job was flipped
        back to ``processing`` by the still-running original task and later
        written ``completed``, so the orchestrator's retry published the video a
        second time.

        Returns ``True`` when a row was actually updated, ``False`` when the
        guard did not match (the caller must then abandon, not keep writing).
        """
        values = _present(
            status=status,
            started_at=started_at,
            completed_at=completed_at,
            lease_owner=lease_owner,
            lease_expires_at=lease_expires_at,
            youtube_video_id=youtube_video_id,
            youtube_url=youtube_url,
            published_via=published_via,
            error_message=error_message,
            thumbnail_applied=thumbnail_applied,
            caption_applied=caption_applied,
        )

        async with self._session_factory() as session:
            await bind_session_tenant(session, require_tenant(tenant_id))
            stmt = update(UploadJob).where(UploadJob.job_id == job_id)
            if expected_status is not None:
                stmt = stmt.where(UploadJob.status == expected_status)
            if require_lease_owner:
                stmt = stmt.where(UploadJob.lease_owner == self._pod_id)
            # `session.execute` is typed `Result`, but an UPDATE always yields a
            # CursorResult — that is the only variant carrying `rowcount`, and
            # rowcount is the whole point of a compare-and-set.
            result = cast(CursorResult[Any], await session.execute(stmt.values(values)))
            # Read rowcount BEFORE the commit closes the cursor.
            rowcount = result.rowcount
            await session.commit()

        if rowcount:
            return True
        logger.error(
            "Cannot set job %s to %s: row not found or no longer owned by this task "
            "(expected_status=%s, lease_owner=%s)",
            job_id,
            status,
            expected_status,
            self._pod_id if require_lease_owner else None,
        )
        return False
