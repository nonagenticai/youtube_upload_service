"""Tests for UploadService — real DB operations, mocked externals."""

from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from src.database.models import UploadJob
from src.services.oauth_service import YouTubeNotConnectedError, YouTubeReconnectRequiredError
from src.services.upload_service import (
    AlreadyPublishedError,
    RetractUnavailableError,
    UploadService,
)
from tests.tenants import tenant_uuid

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _insert_job(session, **overrides) -> UploadJob:
    """Insert an UploadJob directly into the DB and return it."""
    defaults = dict(
        job_id="job-1",
        tenant_id=tenant_uuid("t1"),
        status="pending",
        file_source="https://storage.test/w2v/test.mp4",
        file_source_type="url",
        title="Test Video",
        description="A test video",
        tags=["test"],
        category_id="22",
        privacy_status="private",
    )
    defaults.update(overrides)
    # Default the owner to the channel tenant so existing single-tenant tests
    # (where caller == channel) keep working; the leak-fix test sets it apart.
    defaults.setdefault("owner_tenant_id", defaults["tenant_id"])
    job = UploadJob(**defaults)
    session.add(job)
    await session.commit()
    return job


async def _fetch_job(session, job_id: str) -> UploadJob | None:
    """Fetch an UploadJob by job_id for assertions."""
    result = await session.execute(select(UploadJob).where(UploadJob.job_id == job_id))
    return result.scalar_one_or_none()


async def _fetch_fresh(session_factory, job_id: str) -> UploadJob | None:
    """Fetch a job through a brand-new session (no identity-map staleness)."""
    async with session_factory() as session:
        result = await session.execute(select(UploadJob).where(UploadJob.job_id == job_id))
        return result.scalar_one_or_none()


_REAPER_MESSAGE = "Upload interrupted by pod scale-in/crash: lease expired. Safe to retry."


async def _simulate_reap(session_factory, job_id: str) -> None:
    """Do exactly what ``reap_stranded_upload_jobs`` does to a stranded row."""
    async with session_factory() as session:
        await session.execute(
            update(UploadJob)
            .where(UploadJob.job_id == job_id)
            .values(
                status="failed",
                error_message=_REAPER_MESSAGE,
                completed_at=dt.datetime.now(dt.UTC),
            )
        )
        await session.commit()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_oauth():
    """OAuth mock whose ``resolve_*`` delegate to the ``load_*`` mocks.

    Tests configure ``load_credentials`` (return value / side effect); the
    service calls ``resolve_credentials`` (credentials + channel). Delegating
    keeps every test's configuration meaningful and reports the channel the
    real service would: an owner grant is ``tenant``, the no-owner path is
    ``platform``.
    """
    oauth = AsyncMock()

    async def _resolve(tenant_id, **_kwargs):
        return await oauth.load_credentials(tenant_id), "tenant"

    async def _resolve_without_grant(label):
        return await oauth.load_credentials_without_grant(label), "platform"

    oauth.resolve_credentials.side_effect = _resolve
    oauth.resolve_credentials_without_grant.side_effect = _resolve_without_grant
    return oauth


@pytest.fixture
def mock_youtube():
    return AsyncMock()


@pytest.fixture
def mock_task_manager():
    tm = AsyncMock()

    async def _submit(_job_id, coro):
        # Close the submitted coroutine so we don't leak "coroutine was never
        # awaited" RuntimeWarnings — UploadService.create_job hands us the
        # actual _process_job(...) coroutine.
        if hasattr(coro, "close"):
            coro.close()

    tm.submit = AsyncMock(side_effect=_submit)
    return tm


@pytest.fixture
def upload_service(db_session_factory, mock_oauth, mock_youtube, mock_task_manager, mock_settings):
    return UploadService(
        session_factory=db_session_factory,
        oauth_service=mock_oauth,
        youtube_service=mock_youtube,
        task_manager=mock_task_manager,
        settings=mock_settings,
    )


# ---------------------------------------------------------------------------
# create_job
# ---------------------------------------------------------------------------


class TestCreateJob:
    async def test_creates_real_db_row_and_submits_task(
        self, upload_service, db_session, mock_task_manager
    ):
        """create_job inserts a real row in the DB and fires a background task."""
        job = await upload_service.create_job(
            tenant_id=tenant_uuid("tenant-1"),
            owner_tenant_id=tenant_uuid("owner-tenant-1"),
            file_source="https://storage.test/w2v/test.mp4",
            file_source_type="url",
            title="Test Upload",
            description="A test video",
            tags=["test"],
            category_id="22",
            privacy_status="private",
        )

        # Verify the returned object has correct fields
        assert job.tenant_id == tenant_uuid("tenant-1")
        assert job.owner_tenant_id == tenant_uuid("owner-tenant-1")
        assert job.status == "pending"
        assert job.file_source == "https://storage.test/w2v/test.mp4"
        assert job.file_source_type == "url"
        assert job.title == "Test Upload"
        assert job.description == "A test video"
        assert job.tags == ["test"]
        assert job.category_id == "22"
        assert job.privacy_status == "private"

        # Verify job_id is a valid UUID string
        import uuid

        uuid.UUID(job.job_id)  # raises ValueError if not valid

        # Query DB directly to verify the row actually exists
        db_job = await _fetch_job(db_session, job.job_id)
        assert db_job is not None
        assert db_job.tenant_id == tenant_uuid("tenant-1")
        # The owner column must hold the CALLER tenant, never the channel tenant.
        assert db_job.owner_tenant_id == tenant_uuid("owner-tenant-1")
        assert db_job.status == "pending"
        assert db_job.file_source == "https://storage.test/w2v/test.mp4"
        assert db_job.title == "Test Upload"
        assert db_job.description == "A test video"
        assert db_job.tags == ["test"]

        # Background task was submitted with the job_id
        mock_task_manager.submit.assert_called_once()
        submitted_job_id = mock_task_manager.submit.call_args[0][0]
        assert submitted_job_id == job.job_id

    async def test_create_job_with_url_source(self, upload_service, db_session, mock_task_manager):
        """create_job correctly stores URL source type with non-default fields."""
        job = await upload_service.create_job(
            tenant_id=tenant_uuid("tenant-x"),
            owner_tenant_id=tenant_uuid("owner-tenant-x"),
            file_source="https://example.com/video.mp4",
            file_source_type="url",
            title="URL Upload",
            category_id="10",
            privacy_status="unlisted",
        )

        db_job = await _fetch_job(db_session, job.job_id)
        assert db_job is not None
        assert db_job.tenant_id == tenant_uuid("tenant-x")
        assert db_job.owner_tenant_id == tenant_uuid("owner-tenant-x")
        assert db_job.file_source == "https://example.com/video.mp4"
        assert db_job.file_source_type == "url"
        assert db_job.title == "URL Upload"
        assert db_job.status == "pending"
        assert db_job.category_id == "10"
        assert db_job.privacy_status == "unlisted"
        assert db_job.description is None
        assert db_job.tags is None

    async def test_create_job_persists_thumbnail_fields(self, upload_service, db_session):
        """create_job stores the optional thumbnail source columns."""
        job = await upload_service.create_job(
            tenant_id=tenant_uuid("tenant-thumb"),
            owner_tenant_id=tenant_uuid("owner-tenant-thumb"),
            file_source="https://storage.test/w2v/course.mp4",
            file_source_type="url",
            title="Course",
            thumbnail_source="https://storage.test/w2v/cover.jpg",
            thumbnail_source_type="url",
        )

        db_job = await _fetch_job(db_session, job.job_id)
        assert db_job is not None
        assert db_job.owner_tenant_id == tenant_uuid("owner-tenant-thumb")
        assert db_job.thumbnail_source == "https://storage.test/w2v/cover.jpg"
        assert db_job.thumbnail_source_type == "url"

    async def test_create_job_persists_caption_fields(self, upload_service, db_session):
        """create_job stores the optional soft-caption source columns."""
        job = await upload_service.create_job(
            tenant_id=tenant_uuid("tenant-cc"),
            owner_tenant_id=tenant_uuid("owner-tenant-cc"),
            file_source="https://storage.test/w2v/course.mp4",
            file_source_type="url",
            title="Course",
            caption_source="https://storage.test/w2v/track.srt",
            caption_source_type="url",
            caption_language="en",
        )

        db_job = await _fetch_job(db_session, job.job_id)
        assert db_job is not None
        assert db_job.owner_tenant_id == tenant_uuid("owner-tenant-cc")
        assert db_job.caption_source == "https://storage.test/w2v/track.srt"
        assert db_job.caption_source_type == "url"
        assert db_job.caption_language == "en"


# ---------------------------------------------------------------------------
# get_job
# ---------------------------------------------------------------------------


class TestGetJob:
    async def test_returns_job_when_found(self, upload_service, db_session):
        """get_job returns the job when job_id and tenant_id both match."""
        await _insert_job(db_session, job_id="job-get-1", tenant_id=tenant_uuid("tenant-1"))

        result = await upload_service.get_job(
            "job-get-1", tenant_uuid("tenant-1"), tenant_uuid("tenant-1")
        )
        assert result is not None
        assert result.job_id == "job-get-1"
        assert result.tenant_id == tenant_uuid("tenant-1")

    async def test_returns_none_for_wrong_tenant(self, upload_service, db_session):
        """get_job enforces tenant scoping — wrong tenant_id returns None."""
        await _insert_job(db_session, job_id="job-get-2", tenant_id=tenant_uuid("tenant-A"))

        result = await upload_service.get_job(
            "job-get-2", tenant_uuid("tenant-B"), tenant_uuid("tenant-B")
        )
        assert result is None

    async def test_returns_none_for_nonexistent_job(self, upload_service, db_session):
        """get_job returns None for a job_id that does not exist."""
        await _insert_job(db_session, job_id="job-exists", tenant_id=tenant_uuid("tenant-1"))

        result = await upload_service.get_job(
            "no-such-job", tenant_uuid("tenant-1"), tenant_uuid("tenant-1")
        )
        assert result is None


# ---------------------------------------------------------------------------
# list_jobs
# ---------------------------------------------------------------------------


class TestListJobs:
    async def test_returns_jobs_filtered_by_tenant(self, upload_service, db_session):
        """list_jobs returns only jobs belonging to the requested tenant."""
        await _insert_job(db_session, job_id="a-1", tenant_id=tenant_uuid("tenant-A"), title="A1")
        await _insert_job(db_session, job_id="a-2", tenant_id=tenant_uuid("tenant-A"), title="A2")
        await _insert_job(db_session, job_id="a-3", tenant_id=tenant_uuid("tenant-A"), title="A3")
        await _insert_job(db_session, job_id="b-1", tenant_id=tenant_uuid("tenant-B"), title="B1")

        jobs_a, total_a = await upload_service.list_jobs(
            tenant_uuid("tenant-A"), tenant_uuid("tenant-A")
        )
        assert total_a == 3
        assert len(jobs_a) == 3
        assert all(j.tenant_id == tenant_uuid("tenant-A") for j in jobs_a)

        jobs_b, total_b = await upload_service.list_jobs(
            tenant_uuid("tenant-B"), tenant_uuid("tenant-B")
        )
        assert total_b == 1
        assert len(jobs_b) == 1
        assert jobs_b[0].tenant_id == tenant_uuid("tenant-B")

    async def test_pagination_limit(self, upload_service, db_session):
        """list_jobs respects the limit parameter while returning full total."""
        await _insert_job(db_session, job_id="p-1", tenant_id=tenant_uuid("tenant-P"), title="P1")
        await _insert_job(db_session, job_id="p-2", tenant_id=tenant_uuid("tenant-P"), title="P2")
        await _insert_job(db_session, job_id="p-3", tenant_id=tenant_uuid("tenant-P"), title="P3")

        jobs, total = await upload_service.list_jobs(
            tenant_uuid("tenant-P"), tenant_uuid("tenant-P"), limit=2
        )
        assert total == 3
        assert len(jobs) == 2

    async def test_pagination_offset(self, upload_service, db_session):
        """list_jobs respects the offset parameter while returning full total."""
        await _insert_job(db_session, job_id="o-1", tenant_id=tenant_uuid("tenant-O"), title="O1")
        await _insert_job(db_session, job_id="o-2", tenant_id=tenant_uuid("tenant-O"), title="O2")
        await _insert_job(db_session, job_id="o-3", tenant_id=tenant_uuid("tenant-O"), title="O3")

        jobs, total = await upload_service.list_jobs(
            tenant_uuid("tenant-O"), tenant_uuid("tenant-O"), offset=2
        )
        assert total == 3
        assert len(jobs) == 1

    async def test_returns_empty_for_unknown_tenant(self, upload_service, db_session):
        """list_jobs returns empty list and total=0 for a tenant with no jobs."""
        await _insert_job(db_session, job_id="x-1", tenant_id=tenant_uuid("tenant-X"))

        jobs, total = await upload_service.list_jobs(
            tenant_uuid("tenant-NONE"), tenant_uuid("tenant-NONE")
        )
        assert total == 0
        assert jobs == []


# ---------------------------------------------------------------------------
# Real-owner isolation (the O7 leak fix)
# ---------------------------------------------------------------------------


class TestOwnerScoping:
    """Jobs from different real tenants share the SAME channel tenant (the
    orchestrator's service account), so scoping by the channel tenant leaked
    every tenant's jobs to every other. list/get must scope by owner_tenant_id."""

    async def test_list_isolates_owners_under_same_channel(self, upload_service, db_session):
        sa = tenant_uuid("sa-channel-tenant")
        await _insert_job(
            db_session, job_id="oa-1", tenant_id=sa, owner_tenant_id=tenant_uuid("tenant-A")
        )
        await _insert_job(
            db_session, job_id="oa-2", tenant_id=sa, owner_tenant_id=tenant_uuid("tenant-A")
        )
        await _insert_job(
            db_session, job_id="ob-1", tenant_id=sa, owner_tenant_id=tenant_uuid("tenant-B")
        )

        jobs_a, total_a = await upload_service.list_jobs(sa, tenant_uuid("tenant-A"))
        assert total_a == 2
        assert {j.job_id for j in jobs_a} == {"oa-1", "oa-2"}

        jobs_b, total_b = await upload_service.list_jobs(sa, tenant_uuid("tenant-B"))
        assert total_b == 1
        assert jobs_b[0].job_id == "ob-1"

    async def test_get_blocks_cross_owner_under_same_channel(self, upload_service, db_session):
        sa = tenant_uuid("sa-channel-tenant")
        await _insert_job(
            db_session, job_id="secret", tenant_id=sa, owner_tenant_id=tenant_uuid("tenant-A")
        )

        # Tenant B (same channel) must NOT be able to read tenant A's job.
        assert await upload_service.get_job("secret", sa, tenant_uuid("tenant-B")) is None
        # Tenant A still can.
        got = await upload_service.get_job("secret", sa, tenant_uuid("tenant-A"))
        assert got is not None and got.job_id == "secret"

    async def test_create_refuses_when_owner_is_unspecified(self, upload_service, db_session):
        """An unbound caller (no X-Owner-Tenant-Id) is REFUSED, not silently
        defaulted to the channel tenant — such a row is unreadable by its own
        poller (35 of 48 prod rows were born that way)."""
        before = await db_session.execute(
            select(func.count()).select_from(UploadJob).where(UploadJob.title == "Legacy")
        )
        assert before.scalar_one() == 0

        with pytest.raises(ValueError, match="owner_tenant_id is required"):
            await upload_service.create_job(
                tenant_id=tenant_uuid("legacy-tenant"),
                file_source="https://storage.test/w2v/v.mp4",
                file_source_type="url",
                title="Legacy",
            )

        # Nothing was written: no row exists for the channel tenant either.
        result = await db_session.execute(
            select(UploadJob).where(UploadJob.tenant_id == tenant_uuid("legacy-tenant"))
        )
        assert result.scalars().all() == []

    @pytest.mark.parametrize("owner", ["", "   ", "anonymous", "NONE", None])
    async def test_create_refuses_sentinel_owners(self, upload_service, db_session, owner):
        """Placeholder/sentinel owners are refused the same way."""
        with pytest.raises(ValueError, match="owner_tenant_id is required"):
            await upload_service.create_job(
                tenant_id=tenant_uuid("sa-channel-tenant"),
                owner_tenant_id=owner,
                file_source="https://storage.test/w2v/v.mp4",
                file_source_type="url",
                title="Sentinel",
            )
        result = await db_session.execute(select(UploadJob).where(UploadJob.title == "Sentinel"))
        assert result.scalars().all() == []

    async def test_create_records_real_owner(self, upload_service, db_session):
        job = await upload_service.create_job(
            tenant_id=tenant_uuid("sa-channel-tenant"),
            owner_tenant_id=tenant_uuid("real-tenant-7"),
            file_source="https://storage.test/w2v/v.mp4",
            file_source_type="url",
            title="Owned",
        )
        db_job = await _fetch_job(db_session, job.job_id)
        assert db_job is not None
        assert db_job.tenant_id == tenant_uuid("sa-channel-tenant")
        assert db_job.owner_tenant_id == tenant_uuid("real-tenant-7")


# ---------------------------------------------------------------------------
# _process_job — successful path upload
# ---------------------------------------------------------------------------


class TestProcessJob:
    async def test_successful_upload_sets_completed(
        self,
        upload_service,
        db_session,
        db_session_factory,
        mock_oauth,
        mock_youtube,
    ):
        """A successful (url) upload sets status=completed with youtube fields."""
        await _insert_job(
            db_session,
            job_id="proc-ok",
            tenant_id=tenant_uuid("tenant-1"),
            file_source="https://storage.test/w2v/test.mp4",
            file_source_type="url",
            title="Test",
            description="Desc",
            tags=["tag1"],
            category_id="22",
            privacy_status="private",
        )

        mock_oauth.load_credentials.return_value = MagicMock()
        mock_youtube.async_upload_video.return_value = {
            "video_id": "yt123",
            "url": "https://youtu.be/yt123",
        }

        with (
            patch("pathlib.Path.is_file", return_value=True),
            patch("pathlib.Path.stat", return_value=MagicMock(st_size=100 * 1024 * 1024)),
            patch.object(upload_service, "_download_file", new_callable=AsyncMock) as mock_dl,
        ):
            mock_dl.return_value = "/tmp/youtube_uploads/upload_proc-ok.mp4"
            await upload_service._process_job("proc-ok", tenant_uuid("tenant-1"))

        mock_youtube.async_upload_video.assert_called_once()

        # Verify DB state after processing
        db_job = await _fetch_job(db_session, "proc-ok")
        assert db_job is not None
        assert db_job.status == "completed"
        assert db_job.youtube_video_id == "yt123"
        assert db_job.youtube_url == "https://youtu.be/yt123"
        assert db_job.started_at is not None
        assert db_job.completed_at is not None

    async def test_fails_with_connect_youtube_when_no_grant(
        self, upload_service, db_session, mock_oauth, mock_youtube
    ):
        """No grant and no operator opt-in: the job fails naming the fix, nothing uploads."""
        await _insert_job(
            db_session, job_id="proc-nocred", tenant_id=tenant_uuid("tenant-no-creds")
        )

        mock_oauth.load_credentials.side_effect = YouTubeNotConnectedError(
            tenant_uuid("tenant-no-creds")
        )

        await upload_service._process_job("proc-nocred", tenant_uuid("tenant-no-creds"))

        db_job = await _fetch_job(db_session, "proc-nocred")
        assert db_job is not None
        assert db_job.status == "failed"
        assert db_job.error_message is not None
        assert "connect YouTube" in db_job.error_message
        assert db_job.completed_at is not None
        mock_youtube.async_upload_video.assert_not_called()

    async def test_fails_with_reconnect_youtube_when_grant_is_dead(
        self, upload_service, db_session, mock_oauth, mock_youtube
    ):
        """A dead tenant grant fails the job with "reconnect YouTube" — it is NOT
        published anywhere else."""
        await _insert_job(db_session, job_id="proc-dead", tenant_id=tenant_uuid("tenant-dead"))

        mock_oauth.load_credentials.side_effect = YouTubeReconnectRequiredError(
            tenant_uuid("tenant-dead"), "invalid_grant"
        )

        await upload_service._process_job("proc-dead", tenant_uuid("tenant-dead"))

        db_job = await _fetch_job(db_session, "proc-dead")
        assert db_job is not None
        assert db_job.status == "failed"
        assert db_job.error_message is not None
        assert "reconnect YouTube" in db_job.error_message
        mock_youtube.async_upload_video.assert_not_called()

    async def test_fails_when_source_cannot_be_fetched(
        self, upload_service, db_session, mock_oauth
    ):
        """A source that cannot be fetched fails the job (was: local file missing).

        Path mode is no longer served, so "the local file does not exist" is
        not a reachable state; the equivalent live failure is a download that
        never produces a file.
        """
        await _insert_job(
            db_session,
            job_id="proc-nofile",
            tenant_id=tenant_uuid("tenant-1"),
            file_source="https://storage.test/w2v/missing.mp4",
            file_source_type="url",
        )

        mock_oauth.load_credentials.return_value = MagicMock()

        with patch.object(upload_service, "_download_file", new_callable=AsyncMock) as mock_dl:
            mock_dl.side_effect = FileNotFoundError("Video file not found: missing.mp4")
            await upload_service._process_job("proc-nofile", tenant_uuid("tenant-1"))

        db_job = await _fetch_job(db_session, "proc-nofile")
        assert db_job is not None
        assert db_job.status == "failed"
        assert db_job.error_message is not None
        assert "not found" in db_job.error_message
        assert db_job.completed_at is not None

    async def test_fails_when_file_too_large(self, upload_service, db_session, mock_oauth):
        """When file exceeds max_file_size_mb, job fails with size error."""
        await _insert_job(
            db_session,
            job_id="proc-big",
            tenant_id=tenant_uuid("tenant-1"),
            file_source="https://storage.test/w2v/huge.mp4",
            file_source_type="url",
        )

        mock_oauth.load_credentials.return_value = MagicMock()

        # 6000 MB > 5120 MB default limit
        with (
            patch("pathlib.Path.is_file", return_value=True),
            patch("pathlib.Path.stat", return_value=MagicMock(st_size=6000 * 1024 * 1024)),
            patch.object(upload_service, "_download_file", new_callable=AsyncMock) as mock_dl,
        ):
            mock_dl.return_value = "/tmp/youtube_uploads/upload_proc-big.mp4"
            await upload_service._process_job("proc-big", tenant_uuid("tenant-1"))

        db_job = await _fetch_job(db_session, "proc-big")
        assert db_job is not None
        assert db_job.status == "failed"
        assert db_job.error_message is not None
        assert "exceeds" in db_job.error_message
        assert db_job.completed_at is not None

    async def test_url_upload_with_cleanup(
        self, upload_service, db_session, mock_oauth, mock_youtube
    ):
        """URL upload downloads file, uploads, then cleans up the temp file."""
        await _insert_job(
            db_session,
            job_id="proc-url",
            tenant_id=tenant_uuid("tenant-1"),
            file_source="https://example.com/video.mp4",
            file_source_type="url",
            title="URL Video",
            description=None,
            tags=None,
            category_id="22",
            privacy_status="private",
        )

        mock_oauth.load_credentials.return_value = MagicMock()
        mock_youtube.async_upload_video.return_value = {
            "video_id": "url-vid",
            "url": "https://youtu.be/url-vid",
        }

        with (
            patch.object(upload_service, "_download_file", new_callable=AsyncMock) as mock_dl,
            patch("pathlib.Path.stat", return_value=MagicMock(st_size=50 * 1024 * 1024)),
            patch("pathlib.Path.exists", return_value=True),
            patch("pathlib.Path.unlink") as mock_unlink,
        ):
            mock_dl.return_value = "/tmp/youtube_uploads/temp.mp4"
            await upload_service._process_job("proc-url", tenant_uuid("tenant-1"))

            mock_dl.assert_called_once()
            # Path.unlink is a method on the Path instance; the path is implicit `self`.
            mock_unlink.assert_called_once_with()

        db_job = await _fetch_job(db_session, "proc-url")
        assert db_job is not None
        assert db_job.status == "completed"
        assert db_job.youtube_video_id == "url-vid"
        assert db_job.youtube_url == "https://youtu.be/url-vid"
        assert db_job.started_at is not None
        assert db_job.completed_at is not None


# ---------------------------------------------------------------------------
# _process_job — custom thumbnail (best-effort, post-upload)
# ---------------------------------------------------------------------------


class TestProcessJobThumbnail:
    async def test_sets_custom_thumbnail_after_upload(
        self, upload_service, db_session, mock_oauth, mock_youtube
    ):
        """A job with a thumbnail_source downloads it and calls thumbnails().set."""
        await _insert_job(
            db_session,
            job_id="proc-thumb",
            tenant_id=tenant_uuid("tenant-1"),
            file_source="https://storage.test/w2v/test.mp4",
            file_source_type="url",
            thumbnail_source="https://storage.test/w2v/cover.jpg",
            thumbnail_source_type="url",
        )

        mock_oauth.load_credentials.return_value = MagicMock()
        mock_youtube.async_upload_video.return_value = {
            "video_id": "vt1",
            "url": "https://youtu.be/vt1",
        }

        with (
            patch("pathlib.Path.is_file", return_value=True),
            patch("pathlib.Path.stat", return_value=MagicMock(st_size=10 * 1024 * 1024)),
            patch.object(upload_service, "_download_file", new_callable=AsyncMock) as mock_dl,
        ):
            mock_dl.return_value = "/tmp/youtube_uploads/upload_proc-thumb_thumb_x.jpg"
            await upload_service._process_job("proc-thumb", tenant_uuid("tenant-1"))

        # Two downloads now: the video itself, then the thumbnail.
        assert mock_dl.await_count == 2
        mock_youtube.async_set_thumbnail.assert_called_once()
        args, kwargs = mock_youtube.async_set_thumbnail.call_args
        assert args[1] == "vt1"  # video_id
        assert kwargs.get("mimetype") == "image/jpeg"

        db_job = await _fetch_job(db_session, "proc-thumb")
        assert db_job is not None
        assert db_job.status == "completed"
        assert db_job.youtube_video_id == "vt1"

    async def test_thumbnail_failure_does_not_fail_job(
        self, upload_service, db_session, mock_oauth, mock_youtube
    ):
        """A thumbnails().set failure (e.g. unverified channel 403) is non-fatal."""
        await _insert_job(
            db_session,
            job_id="proc-thumb-fail",
            tenant_id=tenant_uuid("tenant-1"),
            file_source="https://storage.test/w2v/test.mp4",
            file_source_type="url",
            thumbnail_source="https://storage.test/w2v/cover.png",
            thumbnail_source_type="url",
        )

        mock_oauth.load_credentials.return_value = MagicMock()
        mock_youtube.async_upload_video.return_value = {
            "video_id": "vt2",
            "url": "https://youtu.be/vt2",
        }
        mock_youtube.async_set_thumbnail.side_effect = RuntimeError("403 thumbnailNotAvailable")

        with (
            patch("pathlib.Path.is_file", return_value=True),
            patch("pathlib.Path.stat", return_value=MagicMock(st_size=10 * 1024 * 1024)),
            patch.object(upload_service, "_download_file", new_callable=AsyncMock) as mock_dl,
        ):
            mock_dl.return_value = "/tmp/youtube_uploads/cover.png"
            await upload_service._process_job("proc-thumb-fail", tenant_uuid("tenant-1"))

        db_job = await _fetch_job(db_session, "proc-thumb-fail")
        assert db_job is not None
        assert db_job.status == "completed"  # thumbnail failure must not fail the upload
        assert db_job.youtube_video_id == "vt2"

    async def test_no_thumbnail_skips_set(
        self, upload_service, db_session, mock_oauth, mock_youtube
    ):
        """A job without a thumbnail_source never calls thumbnails().set."""
        await _insert_job(
            db_session,
            job_id="proc-nothumb",
            tenant_id=tenant_uuid("tenant-1"),
            file_source="https://storage.test/w2v/test.mp4",
            file_source_type="url",
        )

        mock_oauth.load_credentials.return_value = MagicMock()
        mock_youtube.async_upload_video.return_value = {
            "video_id": "vt3",
            "url": "https://youtu.be/vt3",
        }

        with (
            patch("pathlib.Path.is_file", return_value=True),
            patch("pathlib.Path.stat", return_value=MagicMock(st_size=10 * 1024 * 1024)),
            patch.object(upload_service, "_download_file", new_callable=AsyncMock),
        ):
            await upload_service._process_job("proc-nothumb", tenant_uuid("tenant-1"))

        mock_youtube.async_set_thumbnail.assert_not_called()


# ---------------------------------------------------------------------------
# _process_job — soft captions (best-effort, post-upload)
# ---------------------------------------------------------------------------


class TestProcessJobCaptions:
    async def test_attaches_captions_after_upload(
        self, upload_service, db_session, mock_oauth, mock_youtube
    ):
        """A job with a caption_source downloads it and calls captions().insert."""
        await _insert_job(
            db_session,
            job_id="proc-cc",
            tenant_id=tenant_uuid("tenant-1"),
            file_source="https://storage.test/w2v/test.mp4",
            file_source_type="url",
            caption_source="https://storage.test/w2v/track.srt",
            caption_source_type="url",
            caption_language="en",
        )

        mock_oauth.load_credentials.return_value = MagicMock()
        mock_youtube.async_upload_video.return_value = {
            "video_id": "vc1",
            "url": "https://youtu.be/vc1",
        }

        with (
            patch("pathlib.Path.is_file", return_value=True),
            patch("pathlib.Path.stat", return_value=MagicMock(st_size=10 * 1024 * 1024)),
            patch.object(upload_service, "_download_file", new_callable=AsyncMock) as mock_dl,
        ):
            mock_dl.return_value = "/tmp/youtube_uploads/upload_proc-cc_caption_x.srt"
            await upload_service._process_job("proc-cc", tenant_uuid("tenant-1"))

        mock_youtube.async_insert_captions.assert_called_once()
        args, kwargs = mock_youtube.async_insert_captions.call_args
        assert args[1] == "vc1"  # video_id
        assert kwargs.get("language") == "en"

        db_job = await _fetch_job(db_session, "proc-cc")
        assert db_job is not None
        assert db_job.status == "completed"

    async def test_caption_failure_does_not_fail_job(
        self, upload_service, db_session, mock_oauth, mock_youtube
    ):
        """A captions().insert failure (e.g. missing force-ssl scope 403) is non-fatal."""
        await _insert_job(
            db_session,
            job_id="proc-cc-fail",
            tenant_id=tenant_uuid("tenant-1"),
            file_source="https://storage.test/w2v/test.mp4",
            file_source_type="url",
            caption_source="https://storage.test/w2v/track.srt",
            caption_source_type="url",
            caption_language="en",
        )

        mock_oauth.load_credentials.return_value = MagicMock()
        mock_youtube.async_upload_video.return_value = {
            "video_id": "vc2",
            "url": "https://youtu.be/vc2",
        }
        mock_youtube.async_insert_captions.side_effect = RuntimeError("403 insufficientPermissions")

        with (
            patch("pathlib.Path.is_file", return_value=True),
            patch("pathlib.Path.stat", return_value=MagicMock(st_size=10 * 1024 * 1024)),
            patch.object(upload_service, "_download_file", new_callable=AsyncMock) as mock_dl,
        ):
            mock_dl.return_value = "/tmp/youtube_uploads/track.srt"
            await upload_service._process_job("proc-cc-fail", tenant_uuid("tenant-1"))

        db_job = await _fetch_job(db_session, "proc-cc-fail")
        assert db_job is not None
        assert db_job.status == "completed"  # caption failure must not fail the upload
        assert db_job.youtube_video_id == "vc2"

    async def test_no_caption_skips_insert(
        self, upload_service, db_session, mock_oauth, mock_youtube
    ):
        """A job without a caption_source never calls captions().insert."""
        await _insert_job(
            db_session,
            job_id="proc-nocc",
            tenant_id=tenant_uuid("tenant-1"),
            file_source="https://storage.test/w2v/test.mp4",
            file_source_type="url",
        )

        mock_oauth.load_credentials.return_value = MagicMock()
        mock_youtube.async_upload_video.return_value = {
            "video_id": "vc3",
            "url": "https://youtu.be/vc3",
        }

        with (
            patch("pathlib.Path.is_file", return_value=True),
            patch("pathlib.Path.stat", return_value=MagicMock(st_size=10 * 1024 * 1024)),
            patch.object(upload_service, "_download_file", new_callable=AsyncMock),
        ):
            await upload_service._process_job("proc-nocc", tenant_uuid("tenant-1"))

        mock_youtube.async_insert_captions.assert_not_called()


# ---------------------------------------------------------------------------
# _download_file
# ---------------------------------------------------------------------------


class TestDownloadFile:
    async def test_downloads_to_temp_dir(self, upload_service, tmp_path):
        """_download_file streams from URL and writes to a temp file.

        Uses :class:`httpx.MockTransport` to exercise the real streaming path,
        and stubs the SSRF guard (the guard is tested separately).
        """
        import httpx as _httpx

        upload_service._settings.upload.temp_dir = str(tmp_path)

        def _handler(request: _httpx.Request) -> _httpx.Response:
            return _httpx.Response(200, content=b"video-data")

        mock_transport = _httpx.MockTransport(_handler)
        original_async_client = _httpx.AsyncClient

        def _patched_client(*args, **kwargs):
            kwargs["transport"] = mock_transport
            return original_async_client(*args, **kwargs)

        with (
            patch("src.services.upload_service.httpx.AsyncClient", side_effect=_patched_client),
            patch(
                "src.services.upload_service.validate_url", return_value=None
            ),  # None = "trusted, do not pin": preserves the pre-pinning request shape these download-mechanics tests were written against
        ):
            result = await upload_service._download_file("https://example.com/video.mp4", "job-1")

        assert result.startswith(str(tmp_path))
        with open(result, "rb") as f:
            assert f.read() == b"video-data"

    async def test_unlink_failure_does_not_mask_download_error(self, upload_service, tmp_path):
        """A failing partial-file cleanup must not mask the original error.

        If the download raises and the subsequent ``unlink`` of the partial
        file also raises OSError (file already gone / EBUSY / permission), the
        ORIGINAL download error must propagate, not the cleanup error.
        """
        upload_service._settings.upload.temp_dir = str(tmp_path)

        class _DownloadBoomError(RuntimeError):
            pass

        async def _boom(*args, **kwargs):
            raise _DownloadBoomError("original download error")

        def _unlink_boom(*args, **kwargs):
            raise OSError("unlink race: file busy")

        with (
            patch(
                "src.services.upload_service.validate_url", return_value=None
            ),  # None = "trusted, do not pin": preserves the pre-pinning request shape these download-mechanics tests were written against
            patch.object(upload_service, "_stream_with_redirects", side_effect=_boom),
            patch("src.services.upload_service.Path.unlink", side_effect=_unlink_boom),
        ):
            with pytest.raises(_DownloadBoomError, match="original download error"):
                await upload_service._download_file("https://example.com/video.mp4", "job-x")


class TestDownloadFileSSRF:
    """Integration tests for the SSRF guard inside _download_file.

    These exercise the *real* validator — no patches on ``validate_url``.
    """

    async def test_rejects_localhost(self, upload_service, tmp_path):
        upload_service._settings.upload.temp_dir = str(tmp_path)
        from src.services.url_guard import UnsafeURLError

        with pytest.raises(UnsafeURLError, match="non-public"):
            await upload_service._download_file("http://127.0.0.1/v.mp4", "j-1")

        # No leftover temp file
        assert list(tmp_path.iterdir()) == []

    async def test_rejects_metadata_ip(self, upload_service, tmp_path):
        upload_service._settings.upload.temp_dir = str(tmp_path)
        from src.services.url_guard import UnsafeURLError

        with pytest.raises(UnsafeURLError, match="non-public"):
            await upload_service._download_file("http://169.254.169.254/latest/meta-data/", "j-1")

    async def test_rejects_file_scheme(self, upload_service, tmp_path):
        upload_service._settings.upload.temp_dir = str(tmp_path)
        from src.services.url_guard import UnsafeURLError

        with pytest.raises(UnsafeURLError, match="Unsupported URL scheme"):
            await upload_service._download_file("file:///etc/passwd", "j-1")

    async def test_rejects_dns_resolving_to_private(self, upload_service, tmp_path):
        upload_service._settings.upload.temp_dir = str(tmp_path)
        from src.services.url_guard import UnsafeURLError

        with patch(
            "src.services.url_guard.socket.getaddrinfo",
            side_effect=lambda *_a, **_kw: [(2, 1, 6, "", ("10.0.0.5", 0))],
        ):
            with pytest.raises(UnsafeURLError, match="non-public"):
                await upload_service._download_file("http://internal.example.com/v.mp4", "j-1")

    async def test_rejects_redirect_to_internal_ip(self, upload_service, tmp_path):
        """Public host 302s to 127.0.0.1 — must be rejected at the redirect hop."""
        import httpx as _httpx

        from src.services.url_guard import UnsafeURLError

        upload_service._settings.upload.temp_dir = str(tmp_path)

        def _handler(request: _httpx.Request) -> _httpx.Response:
            # First and only response: redirect to localhost
            return _httpx.Response(302, headers={"location": "http://127.0.0.1/inner"})

        transport = _httpx.MockTransport(_handler)
        original = _httpx.AsyncClient

        def _patched(*args, **kwargs):
            kwargs["transport"] = transport
            return original(*args, **kwargs)

        # Make the initial public hostname resolve to a public IP so the first
        # validate_url() call passes; the redirect target is a literal private
        # IP so validate_url() rejects it on hop 2.
        with (
            patch(
                "src.services.url_guard.socket.getaddrinfo",
                side_effect=lambda *_a, **_kw: [(2, 1, 6, "", ("1.1.1.1", 0))],
            ),
            patch("src.services.upload_service.httpx.AsyncClient", side_effect=_patched),
        ):
            with pytest.raises(UnsafeURLError, match="non-public"):
                await upload_service._download_file("http://public.example.com/v.mp4", "j-1")

        # Partial-download cleanup ran
        assert list(tmp_path.iterdir()) == []

    async def test_follows_safe_redirect(self, upload_service, tmp_path):
        """A redirect to another public host is followed and content delivered."""
        import httpx as _httpx

        upload_service._settings.upload.temp_dir = str(tmp_path)

        calls: list[str] = []

        def _handler(request: _httpx.Request) -> _httpx.Response:
            calls.append(str(request.url))
            # Dispatch on the Host HEADER, not request.url.host: the request is
            # IP-pinned, so the URL's host is the literal address and the
            # hostname survives only in Host (exactly as a real origin server
            # doing virtual-host routing would see it).
            if request.headers.get("host") == "first.example.com":
                return _httpx.Response(
                    302, headers={"location": "https://second.example.com/v.mp4"}
                )
            return _httpx.Response(200, content=b"final-bytes")

        transport = _httpx.MockTransport(_handler)
        original = _httpx.AsyncClient

        def _patched(*args, **kwargs):
            kwargs["transport"] = transport
            return original(*args, **kwargs)

        with (
            patch(
                "src.services.url_guard.socket.getaddrinfo",
                side_effect=lambda *_a, **_kw: [(2, 1, 6, "", ("1.1.1.1", 0))],
            ),
            patch("src.services.upload_service.httpx.AsyncClient", side_effect=_patched),
        ):
            result = await upload_service._download_file("https://first.example.com/v.mp4", "j-2")

        with open(result, "rb") as f:
            assert f.read() == b"final-bytes"
        assert len(calls) == 2  # one redirect + final

    async def test_too_many_redirects(self, upload_service, tmp_path):
        """Beyond _MAX_REDIRECTS hops, the download is aborted."""
        import httpx as _httpx

        from src.services.url_guard import UnsafeURLError

        upload_service._settings.upload.temp_dir = str(tmp_path)

        def _handler(request: _httpx.Request) -> _httpx.Response:
            # Every response redirects to another public hostname.
            return _httpx.Response(302, headers={"location": "https://hop.example.com/next"})

        transport = _httpx.MockTransport(_handler)
        original = _httpx.AsyncClient

        def _patched(*args, **kwargs):
            kwargs["transport"] = transport
            return original(*args, **kwargs)

        with (
            patch(
                "src.services.url_guard.socket.getaddrinfo",
                side_effect=lambda *_a, **_kw: [(2, 1, 6, "", ("1.1.1.1", 0))],
            ),
            patch("src.services.upload_service.httpx.AsyncClient", side_effect=_patched),
        ):
            with pytest.raises(UnsafeURLError, match=r"Exceeded.*redirects"):
                await upload_service._download_file("https://hop.example.com/start", "j-3")


# ---------------------------------------------------------------------------
# Reaped-task compare-and-set guard (the double-publish fix)
# ---------------------------------------------------------------------------


class TestReapedTaskCannotWrite:
    """A job that waited behind the concurrency semaphore long enough to be
    reaped must NOT be resurrected by its original in-process task. Without a
    CAS the task flipped the reaped row failed → processing → completed, and
    because the orchestrator had already retried, the paid render was published
    to YouTube twice."""

    async def test_reaped_pending_job_is_not_claimed_and_never_uploads(
        self, upload_service, db_session, db_session_factory, mock_oauth, mock_youtube
    ):
        """The reaper failed the row while it queued → the task abandons."""
        await _insert_job(db_session, job_id="reaped-pending", tenant_id=tenant_uuid("tenant-1"))
        await _simulate_reap(db_session_factory, "reaped-pending")

        mock_oauth.load_credentials.return_value = MagicMock()
        mock_youtube.async_upload_video.return_value = {
            "video_id": "should-never-happen",
            "url": "https://youtu.be/should-never-happen",
        }

        with (
            patch("pathlib.Path.is_file", return_value=True),
            patch("pathlib.Path.stat", return_value=MagicMock(st_size=1024)),
            patch.object(upload_service, "_download_file", new_callable=AsyncMock),
        ):
            await upload_service._process_job("reaped-pending", tenant_uuid("tenant-1"))

        # The upload never happened — this is the money assertion.
        mock_youtube.async_upload_video.assert_not_called()

        db_job = await _fetch_fresh(db_session_factory, "reaped-pending")
        assert db_job is not None
        assert db_job.status == "failed"
        assert db_job.error_message == _REAPER_MESSAGE
        assert db_job.youtube_video_id is None
        # The task did not take the lease either.
        assert db_job.lease_owner is None

    async def test_reaped_mid_upload_cannot_write_completed(
        self, upload_service, db_session, db_session_factory, mock_oauth, mock_youtube
    ):
        """Lease reaped while the upload was in flight → no terminal write."""
        await _insert_job(db_session, job_id="reaped-inflight", tenant_id=tenant_uuid("tenant-1"))

        mock_oauth.load_credentials.return_value = MagicMock()

        async def _upload_then_get_reaped(*_args, **_kwargs):
            # The reaper terminalises the row while videos.insert is running.
            await _simulate_reap(db_session_factory, "reaped-inflight")
            return {"video_id": "yt-live", "url": "https://youtu.be/yt-live"}

        mock_youtube.async_upload_video.side_effect = _upload_then_get_reaped

        with (
            patch("pathlib.Path.is_file", return_value=True),
            patch("pathlib.Path.stat", return_value=MagicMock(st_size=1024)),
            patch.object(upload_service, "_download_file", new_callable=AsyncMock),
        ):
            await upload_service._process_job("reaped-inflight", tenant_uuid("tenant-1"))

        db_job = await _fetch_fresh(db_session_factory, "reaped-inflight")
        assert db_job is not None
        # The reaper's terminal STATE stands — the stale task does not resurrect
        # the row it lost.
        assert db_job.status == "failed"
        assert db_job.error_message == _REAPER_MESSAGE
        # ...but the video really IS on the channel, so the id is recorded on
        # the row rather than surviving only in a log line. That record is what
        # ``_reject_if_already_published`` reads to refuse the human re-publish
        # that this exact state (a "failed" job whose upload succeeded) invites.
        assert db_job.youtube_video_id == "yt-live"
        assert db_job.youtube_url == "https://youtu.be/yt-live"
        # ...and so is the channel it went to, or the video could not be retracted
        # on the right channel later.
        assert db_job.published_via == "tenant"

    async def test_reaped_mid_upload_failure_does_not_overwrite_reaper_message(
        self, upload_service, db_session, db_session_factory, mock_oauth, mock_youtube
    ):
        """Even the ``failed`` write is guarded — the reaper's retryable message
        must survive a stale task's own error."""
        await _insert_job(db_session, job_id="reaped-then-error", tenant_id=tenant_uuid("tenant-1"))

        mock_oauth.load_credentials.return_value = MagicMock()

        async def _reap_then_raise(*_args, **_kwargs):
            await _simulate_reap(db_session_factory, "reaped-then-error")
            raise RuntimeError("upstream 500 from videos.insert")

        mock_youtube.async_upload_video.side_effect = _reap_then_raise

        with (
            patch("pathlib.Path.is_file", return_value=True),
            patch("pathlib.Path.stat", return_value=MagicMock(st_size=1024)),
            patch.object(upload_service, "_download_file", new_callable=AsyncMock),
        ):
            await upload_service._process_job("reaped-then-error", tenant_uuid("tenant-1"))

        db_job = await _fetch_fresh(db_session_factory, "reaped-then-error")
        assert db_job is not None
        assert db_job.status == "failed"
        assert db_job.error_message == _REAPER_MESSAGE

    async def test_claim_takes_the_lease_for_this_pod(
        self, upload_service, db_session, db_session_factory, mock_oauth, mock_youtube
    ):
        """The happy path still claims: pending → processing under this pod's lease."""
        await _insert_job(db_session, job_id="claim-ok", tenant_id=tenant_uuid("tenant-1"))

        mock_oauth.load_credentials.return_value = MagicMock()
        mock_youtube.async_upload_video.return_value = {
            "video_id": "yt-claim",
            "url": "https://youtu.be/yt-claim",
        }

        with (
            patch("pathlib.Path.is_file", return_value=True),
            patch("pathlib.Path.stat", return_value=MagicMock(st_size=1024)),
            patch.object(upload_service, "_download_file", new_callable=AsyncMock),
        ):
            await upload_service._process_job("claim-ok", tenant_uuid("tenant-1"))

        db_job = await _fetch_fresh(db_session_factory, "claim-ok")
        assert db_job is not None
        assert db_job.status == "completed"
        assert db_job.lease_owner == upload_service._pod_id
        assert db_job.youtube_video_id == "yt-claim"

    async def test_update_status_cas_returns_false_on_mismatch(
        self, upload_service, db_session, db_session_factory
    ):
        """_update_job_status reports whether it actually moved the row."""
        await _insert_job(
            db_session, job_id="cas-row", tenant_id=tenant_uuid("tenant-1"), status="failed"
        )

        moved = await upload_service._update_job_status(
            "cas-row", "processing", tenant_uuid("tenant-1"), expected_status="pending"
        )
        assert moved is False

        db_job = await _fetch_fresh(db_session_factory, "cas-row")
        assert db_job is not None
        assert db_job.status == "failed"


# ---------------------------------------------------------------------------
# Per-asset download caps (thumbnail / caption must not inherit the 5 GB cap)
# ---------------------------------------------------------------------------


class TestAuxiliaryDownloadCaps:
    async def test_thumbnail_download_uses_thumbnail_cap(
        self, upload_service, db_session, mock_oauth, mock_youtube
    ):
        await _insert_job(
            db_session,
            job_id="cap-thumb",
            tenant_id=tenant_uuid("tenant-1"),
            thumbnail_source="https://storage.test/w2v/cover.jpg",
            thumbnail_source_type="url",
        )
        mock_oauth.load_credentials.return_value = MagicMock()
        mock_youtube.async_upload_video.return_value = {
            "video_id": "ct1",
            "url": "https://youtu.be/ct1",
        }

        with (
            patch("pathlib.Path.is_file", return_value=True),
            patch("pathlib.Path.stat", return_value=MagicMock(st_size=1024)),
            patch.object(upload_service, "_download_file", new_callable=AsyncMock) as mock_dl,
        ):
            mock_dl.return_value = "/tmp/youtube_uploads/cover.jpg"
            await upload_service._process_job("cap-thumb", tenant_uuid("tenant-1"))

        expected = upload_service._settings.upload.max_thumbnail_size_mb * 1024 * 1024
        assert mock_dl.call_args.kwargs["max_bytes"] == expected
        # Emphatically not the 5 GB video cap.
        assert expected < upload_service._settings.upload.max_file_size_mb * 1024 * 1024

    async def test_caption_download_uses_caption_cap(
        self, upload_service, db_session, mock_oauth, mock_youtube
    ):
        await _insert_job(
            db_session,
            job_id="cap-cc",
            tenant_id=tenant_uuid("tenant-1"),
            caption_source="https://storage.test/w2v/track.srt",
            caption_source_type="url",
            caption_language="en",
        )
        mock_oauth.load_credentials.return_value = MagicMock()
        mock_youtube.async_upload_video.return_value = {
            "video_id": "cc1",
            "url": "https://youtu.be/cc1",
        }

        with (
            patch("pathlib.Path.is_file", return_value=True),
            patch("pathlib.Path.stat", return_value=MagicMock(st_size=1024)),
            patch.object(upload_service, "_download_file", new_callable=AsyncMock) as mock_dl,
        ):
            mock_dl.return_value = "/tmp/youtube_uploads/track.srt"
            await upload_service._process_job("cap-cc", tenant_uuid("tenant-1"))

        expected = upload_service._settings.upload.max_caption_size_mb * 1024 * 1024
        assert mock_dl.call_args.kwargs["max_bytes"] == expected
        assert expected < upload_service._settings.upload.max_file_size_mb * 1024 * 1024

    async def test_download_aborts_when_over_the_supplied_cap(self, upload_service, tmp_path):
        """A stream larger than the caller's cap is aborted mid-flight."""
        import httpx as _httpx

        upload_service._settings.upload.temp_dir = str(tmp_path)

        def _handler(request: _httpx.Request) -> _httpx.Response:
            return _httpx.Response(200, content=b"x" * (3 * 1024 * 1024))

        transport = _httpx.MockTransport(_handler)
        original = _httpx.AsyncClient

        def _patched(*args, **kwargs):
            kwargs["transport"] = transport
            return original(*args, **kwargs)

        with (
            patch("src.services.upload_service.httpx.AsyncClient", side_effect=_patched),
            patch(
                "src.services.upload_service.validate_url", return_value=None
            ),  # None = "trusted, do not pin": preserves the pre-pinning request shape these download-mechanics tests were written against
        ):
            with pytest.raises(ValueError, match="exceeds"):
                await upload_service._download_file(
                    "https://example.com/cover.jpg", "j-cap", max_bytes=2 * 1024 * 1024
                )

        # Partial download cleaned up.
        assert list(tmp_path.iterdir()) == []


class TestDownloadFileIsIPPinned:
    """The download must connect to the address the guard APPROVED.

    Validating a hostname and then handing that hostname to httpx leaves a
    second, unguarded DNS lookup at connect time -- the DNS-rebinding hole. The
    proof that it is closed is that the request reaching the transport carries a
    literal IP as its host: there is nothing left for a resolver to flip.

    A NetworkPolicy egress block does back this up in the ``w2v`` namespace, but
    it is per-namespace and this service's downloads are the kind of thing that
    gets reused elsewhere, so the guard must stand on its own.
    """

    async def test_request_is_sent_to_the_validated_ip_not_the_hostname(
        self, upload_service, tmp_path
    ):
        import httpx as _httpx

        upload_service._settings.upload.temp_dir = str(tmp_path)
        seen: list[tuple[str, str]] = []

        def _handler(request: _httpx.Request) -> _httpx.Response:
            seen.append((request.url.host, request.headers.get("host", "")))
            return _httpx.Response(200, content=b"video-bytes")

        transport = _httpx.MockTransport(_handler)
        original = _httpx.AsyncClient

        def _patched(*args, **kwargs):
            kwargs["transport"] = transport
            return original(*args, **kwargs)

        # A rebinding resolver: public on the guard's lookup, internal on every
        # lookup after it. If the connect re-resolved, it would reach 10.0.0.5.
        answers = [[(2, 1, 6, "", ("1.1.1.1", 0))], [(2, 1, 6, "", ("10.0.0.5", 0))]]

        def _rebinding(*_a, **_kw):
            return answers.pop(0) if len(answers) > 1 else answers[0]

        with (
            patch("src.services.url_guard.socket.getaddrinfo", side_effect=_rebinding),
            patch("src.services.upload_service.httpx.AsyncClient", side_effect=_patched),
        ):
            await upload_service._download_file("http://public.example.com/v.mp4", "j-pin")

        assert len(seen) == 1
        connected_host, host_header = seen[0]
        # THE assertion: the connect target is the approved literal address.
        assert connected_host == "1.1.1.1", (
            f"expected the request to be pinned to the validated IP, got {connected_host!r} "
            "-- an un-pinned hostname is re-resolved by httpx and can rebind to an internal address"
        )
        # Identity is preserved so TLS/vhost routing still work.
        assert host_header == "public.example.com"

    async def test_every_redirect_hop_is_pinned_too(self, upload_service, tmp_path):
        """A safe redirect is followed, and the SECOND hop is pinned as well."""
        import httpx as _httpx

        upload_service._settings.upload.temp_dir = str(tmp_path)
        seen: list[tuple[str, str]] = []

        def _handler(request: _httpx.Request) -> _httpx.Response:
            seen.append((request.url.host, request.headers.get("host", "")))
            if request.headers.get("host") == "first.example.com":
                return _httpx.Response(302, headers={"location": "http://second.example.com/v.mp4"})
            return _httpx.Response(200, content=b"video-bytes")

        transport = _httpx.MockTransport(_handler)
        original = _httpx.AsyncClient

        def _patched(*args, **kwargs):
            kwargs["transport"] = transport
            return original(*args, **kwargs)

        def _by_host(host, *_a, **_kw):
            return [(2, 1, 6, "", ("1.1.1.1" if host == "first.example.com" else "8.8.8.8", 0))]

        with (
            patch("src.services.url_guard.socket.getaddrinfo", side_effect=_by_host),
            patch("src.services.upload_service.httpx.AsyncClient", side_effect=_patched),
        ):
            await upload_service._download_file("http://first.example.com/v.mp4", "j-pin2")

        assert [h for h, _ in seen] == ["1.1.1.1", "8.8.8.8"]
        assert [hh for _, hh in seen] == ["first.example.com", "second.example.com"]

    async def test_trusted_host_is_not_pinned(self, upload_service, tmp_path):
        """Presigned artifact URLs keep their original Host -- the signature covers it."""
        import httpx as _httpx

        upload_service._settings.upload.temp_dir = str(tmp_path)
        upload_service._settings.upload.trusted_download_hosts = "storage.test"
        seen: list[str] = []

        def _handler(request: _httpx.Request) -> _httpx.Response:
            seen.append(request.url.host)
            return _httpx.Response(200, content=b"video-bytes")

        transport = _httpx.MockTransport(_handler)
        original = _httpx.AsyncClient

        def _patched(*args, **kwargs):
            kwargs["transport"] = transport
            return original(*args, **kwargs)

        with patch("src.services.upload_service.httpx.AsyncClient", side_effect=_patched):
            await upload_service._download_file("http://storage.test/a.mp4", "j-pin3")

        assert seen == ["storage.test"]


# ---------------------------------------------------------------------------
# _process_job — path mode is refused outright (defence in depth)
# ---------------------------------------------------------------------------


class TestPathModeRefused:
    """A legacy/hand-written ``file_source_type='path'`` row must not be served.

    The schema boundary stops new path rows, but rows written before that fix
    (the DB column's ``server_default`` was ``'path'``) are still in the table
    and would otherwise read the pod's own filesystem.
    """

    async def test_legacy_path_row_never_reaches_youtube(
        self, upload_service, db_session, mock_oauth, mock_youtube, tmp_path
    ):
        secret = tmp_path / "token"
        secret.write_text("eyJhbGciOi-service-account-token")

        await _insert_job(
            db_session,
            job_id="proc-path-legacy",
            tenant_id=tenant_uuid("tenant-1"),
            file_source=str(secret),
            file_source_type="path",
        )

        mock_oauth.load_credentials.return_value = MagicMock()
        mock_youtube.async_upload_video.return_value = {
            "video_id": "leaked",
            "url": "https://youtu.be/leaked",
        }

        await upload_service._process_job("proc-path-legacy", tenant_uuid("tenant-1"))

        mock_youtube.async_upload_video.assert_not_called()
        db_job = await _fetch_job(db_session, "proc-path-legacy")
        assert db_job is not None
        assert db_job.status == "failed"
        assert db_job.error_message is not None
        assert "url" in db_job.error_message.lower()


# ---------------------------------------------------------------------------
# Regression: a negative page bound is fatal at the DB, not merely odd
# ---------------------------------------------------------------------------


class TestListJobsNegativeBoundsAreFatal:
    """Proof that the unvalidated ``limit``/``offset`` on GET /api/upload/jobs
    is a 500 and not a harmless pass-through: Postgres rejects a negative row
    count outright (``22023 invalid_row_count_in_limit_clause``), so the request
    dies inside the DB driver. ``list_jobs`` therefore clamps defensively — the
    route's ``Query`` bounds are the primary gate, this is the backstop for the
    background/direct callers that never pass through FastAPI validation."""

    @pytest.mark.asyncio
    async def test_precondition_valid_bounds_work(self, upload_service, db_session):
        await _insert_job(db_session, job_id="pg-1", tenant_id=tenant_uuid("tenant-PG"), title="A")

        jobs, total = await upload_service.list_jobs(
            tenant_uuid("tenant-PG"), tenant_uuid("tenant-PG"), limit=1, offset=0
        )

        assert total == 1
        assert len(jobs) == 1

    @pytest.mark.asyncio
    async def test_negative_limit_is_clamped_not_sent_to_postgres(self, upload_service, db_session):
        await _insert_job(db_session, job_id="pg-2", tenant_id=tenant_uuid("tenant-PG2"), title="A")

        jobs, total = await upload_service.list_jobs(
            tenant_uuid("tenant-PG2"), tenant_uuid("tenant-PG2"), limit=-1
        )

        assert total == 1
        assert len(jobs) == 1

    @pytest.mark.asyncio
    async def test_negative_offset_is_clamped_not_sent_to_postgres(
        self, upload_service, db_session
    ):
        await _insert_job(db_session, job_id="pg-3", tenant_id=tenant_uuid("tenant-PG3"), title="A")

        jobs, total = await upload_service.list_jobs(
            tenant_uuid("tenant-PG3"), tenant_uuid("tenant-PG3"), offset=-5
        )

        assert total == 1
        assert len(jobs) == 1


# ---------------------------------------------------------------------------
# Publish-once: the same render must never reach YouTube twice
# ---------------------------------------------------------------------------


class TestPublishOnce:
    """``start_upload`` is the ONLY caller of the upload path — no automatic
    retry exists — so a repeat request is always a human re-submitting. These
    tests pin that the second submission is REFUSED, not uploaded."""

    _SOURCE = "https://storage.test/w2v/paid-render.mp4"

    async def test_republish_of_completed_render_is_refused(self, upload_service, db_session):
        """A second create_job for an already-completed render raises."""
        await _insert_job(
            db_session,
            job_id="published-1",
            tenant_id=tenant_uuid("chan"),
            owner_tenant_id=tenant_uuid("owner-A"),
            status="completed",
            file_source=self._SOURCE,
            youtube_video_id="yt-ABC",
            youtube_url="https://youtu.be/yt-ABC",
        )

        with pytest.raises(AlreadyPublishedError) as exc_info:
            await upload_service.create_job(
                tenant_id=tenant_uuid("chan"),
                owner_tenant_id=tenant_uuid("owner-A"),
                file_source=self._SOURCE,
                file_source_type="url",
                title="Paid Render",
            )

        assert exc_info.value.youtube_video_id == "yt-ABC"
        assert "yt-ABC" in str(exc_info.value)

    async def test_republish_of_reaped_but_published_render_is_refused(
        self, upload_service, db_session, db_session_factory
    ):
        """The dangerous case: the row reads ``failed`` (the reaper terminalised
        it) but the upload actually succeeded and the video id was recorded. A
        status-only check would wave this straight through."""
        await _insert_job(
            db_session,
            job_id="reaped-but-live",
            tenant_id=tenant_uuid("chan"),
            owner_tenant_id=tenant_uuid("owner-B"),
            status="failed",
            file_source=self._SOURCE,
            error_message="Upload interrupted: ... check the channel before retrying.",
            youtube_video_id="yt-GHOST",
            youtube_url="https://youtu.be/yt-GHOST",
        )

        with pytest.raises(AlreadyPublishedError) as exc_info:
            await upload_service.create_job(
                tenant_id=tenant_uuid("chan"),
                owner_tenant_id=tenant_uuid("owner-B"),
                file_source=self._SOURCE,
                file_source_type="url",
                title="Paid Render",
            )

        assert exc_info.value.youtube_video_id == "yt-GHOST"
        # And nothing was written: the duplicate never became a job at all.
        async with db_session_factory() as session:
            count = (
                await session.execute(
                    select(func.count())
                    .select_from(UploadJob)
                    .where(UploadJob.owner_tenant_id == tenant_uuid("owner-B"))
                )
            ).scalar()
        assert count == 1

    async def test_failed_render_that_never_published_is_retryable(
        self, upload_service, db_session, mock_task_manager
    ):
        """A genuinely failed job (no video id) does NOT block a retry — the
        guard must refuse duplicates without breaking legitimate re-submission."""
        await _insert_job(
            db_session,
            job_id="genuinely-failed",
            tenant_id=tenant_uuid("chan"),
            owner_tenant_id=tenant_uuid("owner-C"),
            status="failed",
            file_source=self._SOURCE,
            error_message="Upload never started: ... safe to retry.",
        )

        job = await upload_service.create_job(
            tenant_id=tenant_uuid("chan"),
            owner_tenant_id=tenant_uuid("owner-C"),
            file_source=self._SOURCE,
            file_source_type="url",
            title="Paid Render",
        )

        assert job.status == "pending"
        mock_task_manager.submit.assert_awaited_once()

    async def test_another_tenant_may_publish_the_same_source(
        self, upload_service, db_session, mock_task_manager
    ):
        """The key is scoped to the REAL caller (``owner_tenant_id``), not the
        shared SA channel tenant — otherwise one tenant's publish would block
        every other tenant's."""
        await _insert_job(
            db_session,
            job_id="published-other",
            tenant_id=tenant_uuid("chan"),
            owner_tenant_id=tenant_uuid("owner-D"),
            status="completed",
            file_source=self._SOURCE,
            youtube_video_id="yt-D",
        )

        job = await upload_service.create_job(
            tenant_id=tenant_uuid("chan"),
            owner_tenant_id=tenant_uuid("owner-E"),
            file_source=self._SOURCE,
            file_source_type="url",
            title="Paid Render",
        )

        assert job.owner_tenant_id == tenant_uuid("owner-E")
        mock_task_manager.submit.assert_awaited_once()

    async def test_db_backstop_rejects_a_second_completed_row(self, db_session_factory):
        """The partial unique index (migration 008 / models.__table_args__) is the
        database-level backstop under the application check: two COMPLETED rows
        for one (owner, file_source) are impossible even if the app check is
        bypassed entirely (a direct INSERT, a future code path, a restore)."""
        async with db_session_factory() as session:
            session.add(
                UploadJob(
                    job_id="idx-1",
                    tenant_id=tenant_uuid("chan"),
                    owner_tenant_id=tenant_uuid("owner-F"),
                    status="completed",
                    file_source=self._SOURCE,
                    file_source_type="url",
                    title="A",
                    youtube_video_id="yt-1",
                )
            )
            await session.commit()

        with pytest.raises(IntegrityError):
            async with db_session_factory() as session:
                session.add(
                    UploadJob(
                        job_id="idx-2",
                        tenant_id=tenant_uuid("chan"),
                        owner_tenant_id=tenant_uuid("owner-F"),
                        status="completed",
                        file_source=self._SOURCE,
                        file_source_type="url",
                        title="A",
                        youtube_video_id="yt-2",
                    )
                )
                await session.commit()

    async def test_db_backstop_is_partial_and_allows_repeated_failures(self, db_session_factory):
        """Only *completed* rows occupy the slot — repeated failed attempts at the
        same source must stay insertable, or a flaky upload becomes unretryable."""
        async with db_session_factory() as session:
            for n in (1, 2, 3):
                session.add(
                    UploadJob(
                        job_id=f"partial-{n}",
                        tenant_id=tenant_uuid("chan"),
                        owner_tenant_id=tenant_uuid("owner-G"),
                        status="failed",
                        file_source=self._SOURCE,
                        file_source_type="url",
                        title="A",
                    )
                )
            await session.commit()

        async with db_session_factory() as session:
            count = (
                await session.execute(
                    select(func.count())
                    .select_from(UploadJob)
                    .where(UploadJob.owner_tenant_id == tenant_uuid("owner-G"))
                )
            ).scalar()
        assert count == 3


# ---------------------------------------------------------------------------
# REPRODUCTION (2026-09-01 audit): _process_job's except-arm helper call can
# itself throw and replace the real upload failure with a DB error, and it
# does so with NO surrounding try/except -- the exception propagates out of
# _process_job entirely, so TaskManager's outer handler logs the DB error
# under "Background job %s failed" while the job row is never marked
# failed (it stays "processing" forever, wedged behind its lease).
# ---------------------------------------------------------------------------
class TestProcessJobStatusUpdateExceptionMasking:
    async def test_update_job_status_failure_in_except_arm_propagates_and_wedges_row(
        self, upload_service, db_session, db_session_factory, mock_oauth, caplog
    ):
        await _insert_job(
            db_session,
            job_id="proc-mask",
            tenant_id=tenant_uuid("tenant-1"),
            file_source="https://storage.test/w2v/test.mp4",
            file_source_type="url",
        )

        # The real upload failure: distinctive exception type/message.
        mock_oauth.load_credentials.side_effect = RuntimeError("SSRF guard rejected upload host")

        original_update = upload_service._update_job_status

        async def flaky_update(job_id, status, tenant_id, **kwargs):
            if status == "failed":
                # A distinct, different exception type -- simulates a DB
                # error while writing the failure row.
                raise ConnectionError("db pool exhausted while marking failed")
            return await original_update(job_id, status, tenant_id, **kwargs)

        caplog.set_level("ERROR")
        with patch.object(upload_service, "_update_job_status", side_effect=flaky_update):
            # Fixed behaviour: the DB write failure while marking the job
            # "failed" must NOT replace/propagate over the real upload
            # failure. _process_job must not raise at all here -- it is a
            # background task whose exceptions are otherwise swallowed by
            # TaskManager's outer handler, and we don't want that handler
            # logging a ConnectionError in place of the real RuntimeError.
            await upload_service._process_job("proc-mask", tenant_uuid("tenant-1"))

        # Second-order damage (bounded, not silent): the row is stuck at
        # "processing" -- the original RuntimeError never gets recorded on
        # the row, and the job cannot be retried until its lease expires and
        # the reaper reclaims it. That is the accepted degraded path; losing
        # the RuntimeError out of _process_job entirely is what is fixed.
        db_job = await _fetch_fresh(db_session_factory, "proc-mask")
        assert db_job is not None
        assert db_job.status == "processing"
        assert db_job.error_message is None

        # The real cause is still visible in the logs (not silently lost),
        # and so is the fact that the DB write itself then failed.
        messages = [r.getMessage() for r in caplog.records]
        assert any("Upload job proc-mask failed" in m for m in messages)
        assert any("could not be marked 'failed'" in m for m in messages)


# ---------------------------------------------------------------------------
# _process_job — what was APPLIED, recorded distinctly from what was REQUESTED
# ---------------------------------------------------------------------------


class TestAppliedIsRecordedDistinctlyFromRequested:
    """A swallowed thumbnail/caption failure must not look like a clean publish.

    ``thumbnail_source``/``caption_source`` record what the caller ASKED for.
    Before these columns the OUTCOME lived only in a pod log line, so a video
    that went public with YouTube's default auto-thumbnail and no subtitles was
    byte-identical, in the database and on the API, to one that got everything.

    Tri-state, and the third state is load-bearing:
      NULL  -> nothing was requested (or the row predates the column) — UNMEASURED
      True  -> requested and the YouTube call returned
      False -> requested and it FAILED (403 unverified channel / missing force-ssl)
    """

    async def _run(self, upload_service, job_id, tenant):
        with (
            patch("pathlib.Path.is_file", return_value=True),
            patch("pathlib.Path.stat", return_value=MagicMock(st_size=10 * 1024 * 1024)),
            patch.object(upload_service, "_download_file", new_callable=AsyncMock) as mock_dl,
        ):
            mock_dl.return_value = "/tmp/youtube_uploads/aux.jpg"
            await upload_service._process_job(job_id, tenant)

    async def test_thumbnail_failure_persists_applied_false(
        self, upload_service, db_session, mock_oauth, mock_youtube
    ):
        await _insert_job(
            db_session,
            job_id="applied-thumb-fail",
            tenant_id=tenant_uuid("tenant-1"),
            thumbnail_source="https://storage.test/w2v/cover.png",
            thumbnail_source_type="url",
        )
        mock_oauth.load_credentials.return_value = MagicMock()
        mock_youtube.async_upload_video.return_value = {"video_id": "a1", "url": "https://y/a1"}
        mock_youtube.async_set_thumbnail.side_effect = RuntimeError("403 thumbnailNotAvailable")

        await self._run(upload_service, "applied-thumb-fail", tenant_uuid("tenant-1"))

        job = await _fetch_job(db_session, "applied-thumb-fail")
        assert job is not None
        assert job.status == "completed"  # still non-fatal: the video IS published
        assert job.thumbnail_source is not None  # it was REQUESTED
        assert job.thumbnail_applied is False  # and it was NOT applied

    async def test_thumbnail_success_persists_applied_true(
        self, upload_service, db_session, mock_oauth, mock_youtube
    ):
        await _insert_job(
            db_session,
            job_id="applied-thumb-ok",
            tenant_id=tenant_uuid("tenant-1"),
            thumbnail_source="https://storage.test/w2v/cover.png",
            thumbnail_source_type="url",
        )
        mock_oauth.load_credentials.return_value = MagicMock()
        mock_youtube.async_upload_video.return_value = {"video_id": "a2", "url": "https://y/a2"}

        await self._run(upload_service, "applied-thumb-ok", tenant_uuid("tenant-1"))

        job = await _fetch_job(db_session, "applied-thumb-ok")
        assert job is not None
        assert job.thumbnail_applied is True

    async def test_caption_failure_persists_applied_false(
        self, upload_service, db_session, mock_oauth, mock_youtube
    ):
        await _insert_job(
            db_session,
            job_id="applied-cc-fail",
            tenant_id=tenant_uuid("tenant-1"),
            caption_source="https://storage.test/w2v/track.srt",
            caption_source_type="url",
            caption_language="en",
        )
        mock_oauth.load_credentials.return_value = MagicMock()
        mock_youtube.async_upload_video.return_value = {"video_id": "a3", "url": "https://y/a3"}
        mock_youtube.async_insert_captions.side_effect = RuntimeError("403 insufficientPermissions")

        await self._run(upload_service, "applied-cc-fail", tenant_uuid("tenant-1"))

        job = await _fetch_job(db_session, "applied-cc-fail")
        assert job is not None
        assert job.status == "completed"
        assert job.caption_source is not None
        assert job.caption_applied is False

    async def test_caption_success_persists_applied_true(
        self, upload_service, db_session, mock_oauth, mock_youtube
    ):
        await _insert_job(
            db_session,
            job_id="applied-cc-ok",
            tenant_id=tenant_uuid("tenant-1"),
            caption_source="https://storage.test/w2v/track.srt",
            caption_source_type="url",
            caption_language="en",
        )
        mock_oauth.load_credentials.return_value = MagicMock()
        mock_youtube.async_upload_video.return_value = {"video_id": "a4", "url": "https://y/a4"}

        await self._run(upload_service, "applied-cc-ok", tenant_uuid("tenant-1"))

        job = await _fetch_job(db_session, "applied-cc-ok")
        assert job is not None
        assert job.caption_applied is True

    async def test_nothing_requested_leaves_both_null(
        self, upload_service, db_session, mock_oauth, mock_youtube
    ):
        """NULL means 'never asked for', which must NOT be confused with False."""
        await _insert_job(
            db_session,
            job_id="applied-none",
            tenant_id=tenant_uuid("tenant-1"),
        )
        mock_oauth.load_credentials.return_value = MagicMock()
        mock_youtube.async_upload_video.return_value = {"video_id": "a5", "url": "https://y/a5"}

        await self._run(upload_service, "applied-none", tenant_uuid("tenant-1"))

        job = await _fetch_job(db_session, "applied-none")
        assert job is not None
        assert job.status == "completed"
        assert job.thumbnail_applied is None
        assert job.caption_applied is None

    async def test_thumbnail_download_failure_also_persists_false(
        self, upload_service, db_session, mock_oauth, mock_youtube
    ):
        """The failure need not come from YouTube — a dead source URL counts too."""
        await _insert_job(
            db_session,
            job_id="applied-thumb-dl",
            tenant_id=tenant_uuid("tenant-1"),
            thumbnail_source="https://storage.test/w2v/gone.png",
            thumbnail_source_type="url",
        )
        mock_oauth.load_credentials.return_value = MagicMock()
        mock_youtube.async_upload_video.return_value = {"video_id": "a6", "url": "https://y/a6"}

        call_count = {"n": 0}

        async def _dl(*_args, **_kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return "/tmp/youtube_uploads/video.mp4"
            raise RuntimeError("404 from storage")

        with (
            patch("pathlib.Path.is_file", return_value=True),
            patch("pathlib.Path.stat", return_value=MagicMock(st_size=10 * 1024 * 1024)),
            patch.object(upload_service, "_download_file", side_effect=_dl),
        ):
            await upload_service._process_job("applied-thumb-dl", tenant_uuid("tenant-1"))

        job = await _fetch_job(db_session, "applied-thumb-dl")
        assert job is not None
        assert job.status == "completed"
        assert job.thumbnail_applied is False
        mock_youtube.async_set_thumbnail.assert_not_called()

    # ------------------------------------------------------------------
    # The REAPED / late-success path writes the outcomes too.
    #
    # When the compare-and-set refuses (the reaper terminalised the row while
    # the upload was in fact succeeding) the status write is abandoned and the
    # publish is instead recorded by ``_record_published_video``. That call is
    # the ONLY writer on this path, and it sits inside
    # ``contextlib.suppress(Exception)`` — so a dropped kwarg there is silent:
    # the outcomes revert to NULL, which reads as "never requested" for a
    # thumbnail that was requested and FAILED. These pin the outcomes onto the
    # persisted row, which is where the suppression cannot hide them.
    # ------------------------------------------------------------------

    async def _run_reaped(self, upload_service, db_session_factory, job_id, tenant, video_id):
        """Drive _process_job so the reaper terminalises the row mid-upload."""

        async def _upload_then_get_reaped(*_args, **_kwargs):
            await _simulate_reap(db_session_factory, job_id)
            return {"video_id": video_id, "url": f"https://youtu.be/{video_id}"}

        upload_service._youtube.async_upload_video.side_effect = _upload_then_get_reaped

        with (
            patch("pathlib.Path.is_file", return_value=True),
            patch("pathlib.Path.stat", return_value=MagicMock(st_size=10 * 1024 * 1024)),
            patch.object(upload_service, "_download_file", new_callable=AsyncMock) as mock_dl,
        ):
            mock_dl.return_value = "/tmp/youtube_uploads/aux.jpg"
            await upload_service._process_job(job_id, tenant)

    async def test_reaped_late_success_persists_the_failed_outcomes(
        self, upload_service, db_session, db_session_factory, mock_oauth, mock_youtube
    ):
        """A reaped job whose thumbnail AND captions failed must not look unrequested.

        This is the worst combination on offer: the operator sees the reaper's
        ``failed`` status, checks the channel, finds the video really is live —
        and then has to decide whether the missing thumbnail was ever asked for.
        NULL would answer "no". The truth is "yes, and it 403'd".
        """
        await _insert_job(
            db_session,
            job_id="reaped-aux-fail",
            tenant_id=tenant_uuid("tenant-1"),
            thumbnail_source="https://storage.test/w2v/cover.png",
            thumbnail_source_type="url",
            caption_source="https://storage.test/w2v/track.srt",
            caption_source_type="url",
            caption_language="en",
        )
        mock_oauth.load_credentials.return_value = MagicMock()
        mock_youtube.async_set_thumbnail.side_effect = RuntimeError("403 thumbnailNotAvailable")
        mock_youtube.async_insert_captions.side_effect = RuntimeError("403 insufficientPermissions")

        await self._run_reaped(
            upload_service, db_session_factory, "reaped-aux-fail", tenant_uuid("tenant-1"), "yt-r1"
        )

        job = await _fetch_fresh(db_session_factory, "reaped-aux-fail")
        assert job is not None
        # The reaper's terminal state stands — this path never resurrects it.
        assert job.status == "failed"
        assert job.error_message == _REAPER_MESSAGE
        # The publish is recorded (the precondition for the outcomes to matter).
        assert job.youtube_video_id == "yt-r1"
        # ...and so is WHAT WAS ACTUALLY DELIVERED. NULL here would be a lie:
        # both were requested, both failed.
        assert job.thumbnail_applied is False
        assert job.caption_applied is False

    async def test_reaped_late_success_persists_the_succeeded_outcomes(
        self, upload_service, db_session, db_session_factory, mock_oauth, mock_youtube
    ):
        """The same path must carry a True through, not just a False.

        Pins the outcomes as PROPAGATED values rather than a constant: a
        hardcoded ``False`` on the reaped path would satisfy the test above and
        still be wrong.
        """
        await _insert_job(
            db_session,
            job_id="reaped-aux-ok",
            tenant_id=tenant_uuid("tenant-1"),
            thumbnail_source="https://storage.test/w2v/cover.png",
            thumbnail_source_type="url",
            caption_source="https://storage.test/w2v/track.srt",
            caption_source_type="url",
            caption_language="en",
        )
        mock_oauth.load_credentials.return_value = MagicMock()

        await self._run_reaped(
            upload_service, db_session_factory, "reaped-aux-ok", tenant_uuid("tenant-1"), "yt-r2"
        )

        job = await _fetch_fresh(db_session_factory, "reaped-aux-ok")
        assert job is not None
        assert job.status == "failed"
        assert job.youtube_video_id == "yt-r2"
        assert job.thumbnail_applied is True
        assert job.caption_applied is True

    async def test_reaped_late_success_leaves_unrequested_aux_null(
        self, upload_service, db_session, db_session_factory, mock_oauth, mock_youtube
    ):
        """And the tri-state's third leg survives the reaped path too.

        Guards the opposite over-correction: writing ``False`` for a thumbnail
        nobody asked for would manufacture a defect out of a clean publish.
        """
        await _insert_job(
            db_session,
            job_id="reaped-aux-none",
            tenant_id=tenant_uuid("tenant-1"),
        )
        mock_oauth.load_credentials.return_value = MagicMock()

        await self._run_reaped(
            upload_service, db_session_factory, "reaped-aux-none", tenant_uuid("tenant-1"), "yt-r3"
        )

        job = await _fetch_fresh(db_session_factory, "reaped-aux-none")
        assert job is not None
        assert job.youtube_video_id == "yt-r3"
        assert job.thumbnail_applied is None
        assert job.caption_applied is None


# ---------------------------------------------------------------------------
# retract_job
# ---------------------------------------------------------------------------


class TestRetractJob:
    async def test_sets_video_private_then_records_it(
        self, upload_service, db_session, db_session_factory, mock_oauth, mock_youtube
    ):
        await _insert_job(
            db_session,
            job_id="job-r1",
            tenant_id=tenant_uuid("chan"),
            owner_tenant_id=tenant_uuid("owner"),
            status="completed",
            privacy_status="unlisted",
            youtube_video_id="vidR1",
            youtube_url="https://youtu.be/vidR1",
        )
        creds = MagicMock()
        mock_oauth.load_credentials.return_value = creds
        mock_youtube.async_set_private.return_value = "unlisted"

        job = await upload_service.retract_job("job-r1", tenant_uuid("chan"), tenant_uuid("owner"))

        mock_oauth.load_credentials.assert_awaited_once_with(tenant_uuid("owner"))
        mock_youtube.async_set_private.assert_awaited_once_with(creds, "vidR1")
        assert job is not None and job.privacy_status == "private"
        stored = await _fetch_fresh(db_session_factory, "job-r1")
        assert stored is not None and stored.privacy_status == "private"

    async def test_dead_grant_maps_to_retract_unavailable(
        self, upload_service, db_session, mock_oauth, mock_youtube
    ):
        await _insert_job(
            db_session,
            job_id="job-rdead",
            tenant_id=tenant_uuid("chan"),
            owner_tenant_id=tenant_uuid("owner"),
            status="completed",
            privacy_status="unlisted",
            youtube_video_id="vidDead",
            youtube_url="https://youtu.be/vidDead",
        )
        mock_oauth.load_credentials.side_effect = YouTubeReconnectRequiredError(
            tenant_uuid("chan"), "invalid_grant"
        )

        with pytest.raises(RetractUnavailableError, match="reconnect YouTube"):
            await upload_service.retract_job("job-rdead", tenant_uuid("chan"), tenant_uuid("owner"))
        mock_youtube.async_set_private.assert_not_called()

    async def test_other_owner_cannot_retract(
        self, upload_service, db_session, mock_oauth, mock_youtube
    ):
        await _insert_job(
            db_session,
            job_id="job-r2",
            tenant_id=tenant_uuid("chan"),
            owner_tenant_id=tenant_uuid("owner"),
            status="completed",
            youtube_video_id="vidR2",
        )
        job = await upload_service.retract_job(
            "job-r2", tenant_uuid("chan"), tenant_uuid("intruder")
        )
        assert job is None
        mock_youtube.async_set_private.assert_not_called()

    async def test_unpublished_job_is_refused(self, upload_service, db_session, mock_youtube):
        from src.services.upload_service import NotPublishedError

        await _insert_job(db_session, job_id="job-r3", tenant_id=tenant_uuid("chan"))
        with pytest.raises(NotPublishedError):
            await upload_service.retract_job("job-r3", tenant_uuid("chan"), tenant_uuid("chan"))
        mock_youtube.async_set_private.assert_not_called()

    async def test_youtube_failure_leaves_row_unchanged(
        self, upload_service, db_session, db_session_factory, mock_oauth, mock_youtube
    ):
        await _insert_job(
            db_session,
            job_id="job-r4",
            tenant_id=tenant_uuid("chan"),
            status="completed",
            privacy_status="unlisted",
            youtube_video_id="vidR4",
        )
        mock_oauth.load_credentials.return_value = MagicMock()
        mock_youtube.async_set_private.side_effect = RuntimeError("403 insufficientPermissions")
        with pytest.raises(RuntimeError):
            await upload_service.retract_job("job-r4", tenant_uuid("chan"), tenant_uuid("chan"))
        stored = await _fetch_fresh(db_session_factory, "job-r4")
        assert stored is not None and stored.privacy_status == "unlisted"


# ---------------------------------------------------------------------------
# set_job_privacy — the governed release direction
# ---------------------------------------------------------------------------


class TestSetJobPrivacy:
    async def test_raises_video_then_records_it(
        self, upload_service, db_session, db_session_factory, mock_oauth, mock_youtube
    ):
        await _insert_job(
            db_session,
            job_id="job-p1",
            tenant_id=tenant_uuid("chan"),
            owner_tenant_id=tenant_uuid("owner"),
            status="completed",
            privacy_status="private",
            youtube_video_id="vidP1",
        )
        creds = MagicMock()
        mock_oauth.load_credentials.return_value = creds
        mock_youtube.async_set_privacy.return_value = "private"

        job = await upload_service.set_job_privacy(
            "job-p1", tenant_uuid("chan"), tenant_uuid("owner"), "public"
        )

        mock_youtube.async_set_privacy.assert_awaited_once_with(creds, "vidP1", "public")
        assert job is not None and job.privacy_status == "public"
        stored = await _fetch_fresh(db_session_factory, "job-p1")
        assert stored is not None and stored.privacy_status == "public"

    async def test_other_owner_cannot_release(
        self, upload_service, db_session, mock_oauth, mock_youtube
    ):
        await _insert_job(
            db_session,
            job_id="job-p2",
            tenant_id=tenant_uuid("chan"),
            owner_tenant_id=tenant_uuid("owner"),
            status="completed",
            youtube_video_id="vidP2",
        )
        job = await upload_service.set_job_privacy(
            "job-p2", tenant_uuid("chan"), tenant_uuid("intruder"), "public"
        )
        assert job is None
        mock_youtube.async_set_privacy.assert_not_called()

    async def test_private_target_is_refused(self, upload_service, db_session, mock_youtube):
        await _insert_job(
            db_session,
            job_id="job-p3",
            tenant_id=tenant_uuid("chan"),
            status="completed",
            youtube_video_id="vidP3",
        )
        with pytest.raises(ValueError, match="privacy target"):
            await upload_service.set_job_privacy(
                "job-p3", tenant_uuid("chan"), tenant_uuid("chan"), "private"
            )
        mock_youtube.async_set_privacy.assert_not_called()

    async def test_unpublished_job_is_refused(self, upload_service, db_session, mock_youtube):
        from src.services.upload_service import NotPublishedError

        await _insert_job(db_session, job_id="job-p4", tenant_id=tenant_uuid("chan"))
        with pytest.raises(NotPublishedError):
            await upload_service.set_job_privacy(
                "job-p4", tenant_uuid("chan"), tenant_uuid("chan"), "public"
            )
        mock_youtube.async_set_privacy.assert_not_called()

    async def test_youtube_failure_leaves_row_unchanged(
        self, upload_service, db_session, db_session_factory, mock_oauth, mock_youtube
    ):
        await _insert_job(
            db_session,
            job_id="job-p5",
            tenant_id=tenant_uuid("chan"),
            status="completed",
            privacy_status="private",
            youtube_video_id="vidP5",
        )
        mock_oauth.load_credentials.return_value = MagicMock()
        mock_youtube.async_set_privacy.side_effect = RuntimeError("403 insufficientPermissions")
        with pytest.raises(RuntimeError):
            await upload_service.set_job_privacy(
                "job-p5", tenant_uuid("chan"), tenant_uuid("chan"), "public"
            )
        stored = await _fetch_fresh(db_session_factory, "job-p5")
        assert stored is not None and stored.privacy_status == "private"
