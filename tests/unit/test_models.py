"""Tests for database ORM models — metadata validation and real DB operations."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, cast

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.database.models import Base, OAuthToken, UploadJob
from tests.tenants import tenant_uuid

# ---------------------------------------------------------------------------
# Metadata tests (synchronous, no DB needed)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestModelsMetadata:
    def test_base_has_expected_tables(self) -> None:
        table_names = set(Base.metadata.tables.keys())
        assert "oauth_tokens" in table_names
        assert "upload_jobs" in table_names

    def test_oauth_tokens_columns(self) -> None:
        columns = {c.name for c in OAuthToken.__table__.columns}
        expected = {
            "id",
            "tenant_id",
            "access_token",
            "refresh_token",
            "token_uri",
            "scopes",
            "expiry",
            "channel_id",
            "channel_title",
            "needs_reconnect",
            "reconnect_reason",
            "oauth_client_id",
            "created_at",
            "updated_at",
        }
        assert expected == columns

    def test_upload_jobs_columns(self) -> None:
        columns = {c.name for c in UploadJob.__table__.columns}
        expected = {
            "id",
            "job_id",
            "tenant_id",
            "owner_tenant_id",
            "status",
            "file_source",
            "file_source_type",
            "thumbnail_source",
            "thumbnail_source_type",
            "caption_source",
            "caption_source_type",
            "caption_language",
            "title",
            "description",
            "tags",
            "category_id",
            "privacy_status",
            "youtube_video_id",
            "youtube_url",
            # Which channel published the video (migration 013) — retract and
            # re-privacy act on it, not on the owner's current grant.
            "published_via",
            "error_message",
            # The OUTCOME of the two best-effort attach steps, distinct from the
            # ``*_source`` columns that record what was REQUESTED.
            "thumbnail_applied",
            "caption_applied",
            "lease_owner",
            "lease_expires_at",
            "created_at",
            "started_at",
            "completed_at",
        }
        assert expected == columns

    def test_oauth_tokens_unique_tenant(self) -> None:
        table = cast(Any, OAuthToken.__table__)
        constraints = {c.name for c in table.constraints if c.name}
        assert "uq_oauth_tokens_tenant_id" in constraints

    def test_upload_jobs_indexes(self) -> None:
        table = cast(Any, UploadJob.__table__)
        index_names = {idx.name for idx in table.indexes}
        assert "ix_upload_jobs_tenant_status" in index_names
        assert "ix_upload_jobs_status_created" in index_names
        assert "ix_upload_jobs_job_id" in index_names

    def test_oauth_tokens_tablename(self) -> None:
        assert OAuthToken.__tablename__ == "oauth_tokens"

    def test_upload_jobs_tablename(self) -> None:
        assert UploadJob.__tablename__ == "upload_jobs"

    def test_upload_jobs_status_default(self) -> None:
        status_col = UploadJob.__table__.c.status
        assert status_col.server_default is not None, "status column must have a server_default"
        assert str(status_col.server_default.arg) == "pending"

    def test_upload_jobs_privacy_default(self) -> None:
        col = UploadJob.__table__.c.privacy_status
        assert col.server_default is not None, "privacy_status column must have a server_default"
        assert str(col.server_default.arg) == "private"

    def test_upload_jobs_category_default(self) -> None:
        col = UploadJob.__table__.c.category_id
        assert col.server_default is not None, "category_id column must have a server_default"
        assert str(col.server_default.arg) == "22"


# ---------------------------------------------------------------------------
# Real DB operation tests (async, real postgres+psycopg via conftest fixtures)
# ---------------------------------------------------------------------------


def _make_oauth_token(
    tenant_id: str = tenant_uuid("tenant-1"),
    *,
    access_token: str = "access-tok-abc",
    refresh_token: str = "refresh-tok-xyz",
    token_uri: str = "https://oauth2.googleapis.com/token",
    scopes: str = "https://www.googleapis.com/auth/youtube.upload",
    expiry: dt.datetime | None = None,
    channel_id: str | None = None,
    channel_title: str | None = None,
) -> OAuthToken:
    return OAuthToken(
        tenant_id=tenant_id,
        access_token=access_token,
        refresh_token=refresh_token,
        token_uri=token_uri,
        scopes=scopes,
        expiry=expiry,
        channel_id=channel_id,
        channel_title=channel_title,
    )


def _make_upload_job(
    tenant_id: str = tenant_uuid("tenant-1"),
    *,
    job_id: str | None = None,
    file_source: str = "/tmp/video.mp4",
    title: str = "My Video",
    description: str | None = None,
    tags: list[str] | None = None,
    status: str = "pending",
    privacy_status: str = "private",
    category_id: str = "22",
    file_source_type: str = "path",
) -> UploadJob:
    return UploadJob(
        job_id=job_id or str(uuid.uuid7()),
        tenant_id=tenant_id,
        file_source=file_source,
        file_source_type=file_source_type,
        title=title,
        description=description,
        tags=tags,
        status=status,
        privacy_status=privacy_status,
        category_id=category_id,
    )


@pytest.mark.unit
class TestOAuthTokenDB:
    """Real DB operations for OAuthToken."""

    async def test_insert_and_query_oauth_token(self, db_session: AsyncSession) -> None:
        """Insert an OAuthToken and verify all fields are persisted correctly."""
        expiry = dt.datetime(2026, 6, 1, 12, 0, 0, tzinfo=dt.UTC)
        token = _make_oauth_token(
            tenant_uuid("tenant-roundtrip"),
            access_token="acc-123",
            refresh_token="ref-456",
            scopes="scope-a scope-b",
            expiry=expiry,
            channel_id="UC_abc",
            channel_title="My Channel",
        )
        db_session.add(token)
        await db_session.commit()

        result = await db_session.execute(
            select(OAuthToken).where(OAuthToken.tenant_id == tenant_uuid("tenant-roundtrip"))
        )
        row = result.scalar_one()

        assert row.id is not None
        assert row.tenant_id == tenant_uuid("tenant-roundtrip")
        assert row.access_token == "acc-123"
        assert row.refresh_token == "ref-456"
        assert row.token_uri == "https://oauth2.googleapis.com/token"
        assert row.scopes == "scope-a scope-b"
        assert row.expiry is not None
        assert row.channel_id == "UC_abc"
        assert row.channel_title == "My Channel"
        assert row.created_at is not None

    async def test_unique_constraint_on_tenant_id(self, db_session: AsyncSession) -> None:
        """Inserting two OAuthTokens with the same tenant_id raises IntegrityError."""
        token1 = _make_oauth_token(tenant_uuid("dup-tenant"))
        token2 = _make_oauth_token(tenant_uuid("dup-tenant"), access_token="different")

        db_session.add(token1)
        await db_session.commit()

        db_session.add(token2)
        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_nullable_fields_default_to_none(self, db_session: AsyncSession) -> None:
        """Optional fields (expiry, channel_id, channel_title) persist as None."""
        token = _make_oauth_token(tenant_uuid("tenant-nulls"))
        db_session.add(token)
        await db_session.commit()

        result = await db_session.execute(
            select(OAuthToken).where(OAuthToken.tenant_id == tenant_uuid("tenant-nulls"))
        )
        row = result.scalar_one()

        assert row.expiry is None
        assert row.channel_id is None
        assert row.channel_title is None

    async def test_multiple_tenants_coexist(self, db_session: AsyncSession) -> None:
        """Different tenant_ids can each have their own OAuthToken."""
        for i in range(3):
            db_session.add(_make_oauth_token(tenant_uuid(f"tenant-{i}"), access_token=f"tok-{i}"))
        await db_session.commit()

        result = await db_session.execute(select(OAuthToken))
        rows = result.scalars().all()
        assert len(rows) == 3
        assert {r.tenant_id for r in rows} == {
            tenant_uuid("tenant-0"),
            tenant_uuid("tenant-1"),
            tenant_uuid("tenant-2"),
        }


@pytest.mark.unit
class TestUploadJobDB:
    """Real DB operations for UploadJob."""

    async def test_insert_and_query_upload_job(self, db_session: AsyncSession) -> None:
        """Insert an UploadJob with all fields and verify round-trip."""
        job_id = str(uuid.uuid7())
        job = _make_upload_job(
            tenant_uuid("tenant-full"),
            job_id=job_id,
            file_source="https://storage.example.com/video.mp4",
            title="Full Upload Test",
            description="A test upload with every field",
            tags=["python", "testing", "ci"],
            privacy_status="unlisted",
            category_id="28",
            file_source_type="url",
        )
        db_session.add(job)
        await db_session.commit()

        result = await db_session.execute(select(UploadJob).where(UploadJob.job_id == job_id))
        row = result.scalar_one()

        assert row.id is not None
        assert row.job_id == job_id
        assert row.tenant_id == tenant_uuid("tenant-full")
        assert row.status == "pending"
        assert row.file_source == "https://storage.example.com/video.mp4"
        assert row.file_source_type == "url"
        assert row.title == "Full Upload Test"
        assert row.description == "A test upload with every field"
        assert row.tags == ["python", "testing", "ci"]
        assert row.privacy_status == "unlisted"
        assert row.category_id == "28"
        assert row.youtube_video_id is None
        assert row.youtube_url is None
        assert row.error_message is None
        assert row.created_at is not None
        assert row.started_at is None
        assert row.completed_at is None

    async def test_server_defaults_applied(self, db_session: AsyncSession) -> None:
        """Server defaults (status, file_source_type, category_id, privacy_status) are applied."""
        job = UploadJob(
            job_id=str(uuid.uuid7()),
            tenant_id=tenant_uuid("tenant-defaults"),
            file_source="/tmp/test.mp4",
            title="Default Check",
        )
        db_session.add(job)
        await db_session.commit()

        result = await db_session.execute(
            select(UploadJob).where(UploadJob.tenant_id == tenant_uuid("tenant-defaults"))
        )
        row = result.scalar_one()

        assert row.status == "pending"
        # An omitted file_source_type must land in url mode, never path mode.
        assert row.file_source_type == "url"
        assert row.category_id == "22"
        assert row.privacy_status == "private"

    async def test_query_by_tenant_id(self, db_session: AsyncSession) -> None:
        """Insert multiple jobs for different tenants and filter by tenant_id."""
        for i in range(3):
            db_session.add(_make_upload_job(tenant_uuid("tenant-A"), title=f"A-video-{i}"))
        for i in range(2):
            db_session.add(_make_upload_job(tenant_uuid("tenant-B"), title=f"B-video-{i}"))
        await db_session.commit()

        result = await db_session.execute(
            select(UploadJob).where(UploadJob.tenant_id == tenant_uuid("tenant-A"))
        )
        rows = result.scalars().all()
        assert len(rows) == 3
        assert all(r.tenant_id == tenant_uuid("tenant-A") for r in rows)

        result_b = await db_session.execute(
            select(UploadJob).where(UploadJob.tenant_id == tenant_uuid("tenant-B"))
        )
        rows_b = result_b.scalars().all()
        assert len(rows_b) == 2

    async def test_update_status_to_completed(self, db_session: AsyncSession) -> None:
        """Update an UploadJob from pending to completed with YouTube result fields."""
        job_id = str(uuid.uuid7())
        job = _make_upload_job(tenant_uuid("tenant-update"), job_id=job_id)
        db_session.add(job)
        await db_session.commit()

        # Simulate successful upload completion
        result = await db_session.execute(select(UploadJob).where(UploadJob.job_id == job_id))
        row = result.scalar_one()
        assert row.status == "pending"

        now = dt.datetime.now(dt.UTC)
        row.status = "completed"
        row.youtube_video_id = "dQw4w9WgXcQ"
        row.youtube_url = "https://youtu.be/dQw4w9WgXcQ"
        row.started_at = now - dt.timedelta(minutes=5)
        row.completed_at = now
        await db_session.commit()

        # Re-fetch and verify
        result2 = await db_session.execute(select(UploadJob).where(UploadJob.job_id == job_id))
        updated = result2.scalar_one()
        assert updated.status == "completed"
        assert updated.youtube_video_id == "dQw4w9WgXcQ"
        assert updated.youtube_url == "https://youtu.be/dQw4w9WgXcQ"
        assert updated.started_at is not None
        assert updated.completed_at is not None
        assert updated.error_message is None

    async def test_status_flow_pending_processing_failed(self, db_session: AsyncSession) -> None:
        """Walk an UploadJob through pending -> processing -> failed."""
        job_id = str(uuid.uuid7())
        job = _make_upload_job(tenant_uuid("tenant-fail"), job_id=job_id)
        db_session.add(job)
        await db_session.commit()

        # pending -> processing
        result = await db_session.execute(select(UploadJob).where(UploadJob.job_id == job_id))
        row = result.scalar_one()
        row.status = "processing"
        row.started_at = dt.datetime.now(dt.UTC)
        await db_session.commit()

        result2 = await db_session.execute(select(UploadJob).where(UploadJob.job_id == job_id))
        row2 = result2.scalar_one()
        assert row2.status == "processing"
        assert row2.started_at is not None

        # processing -> failed
        row2.status = "failed"
        row2.error_message = "YouTube API quota exceeded"
        row2.completed_at = dt.datetime.now(dt.UTC)
        await db_session.commit()

        result3 = await db_session.execute(select(UploadJob).where(UploadJob.job_id == job_id))
        row3 = result3.scalar_one()
        assert row3.status == "failed"
        assert row3.error_message == "YouTube API quota exceeded"
        assert row3.youtube_video_id is None
        assert row3.youtube_url is None
        assert row3.completed_at is not None

    async def test_nullable_fields_persist_as_none(self, db_session: AsyncSession) -> None:
        """Optional fields (description, tags, youtube_video_id, etc.) can be None."""
        job = _make_upload_job(tenant_uuid("tenant-nulls"))
        db_session.add(job)
        await db_session.commit()

        result = await db_session.execute(
            select(UploadJob).where(UploadJob.tenant_id == tenant_uuid("tenant-nulls"))
        )
        row = result.scalar_one()
        assert row.description is None
        assert row.tags is None
        assert row.youtube_video_id is None
        assert row.youtube_url is None
        assert row.error_message is None
        assert row.started_at is None
        assert row.completed_at is None

    async def test_jsonb_tags_round_trip(self, db_session: AsyncSession) -> None:
        """JSONB tags column stores and retrieves a list of strings correctly."""
        tags = ["tutorial", "python", "sqlalchemy", "async"]
        job = _make_upload_job(tenant_uuid("tenant-tags"), tags=tags, title="Tags Test")
        db_session.add(job)
        await db_session.commit()

        result = await db_session.execute(
            select(UploadJob).where(UploadJob.tenant_id == tenant_uuid("tenant-tags"))
        )
        row = result.scalar_one()
        assert row.tags == tags
        assert isinstance(row.tags, list)
        assert len(row.tags) == 4

    async def test_unique_job_id_constraint(self, db_session: AsyncSession) -> None:
        """Inserting two UploadJobs with the same job_id raises IntegrityError."""
        shared_id = str(uuid.uuid7())
        job1 = _make_upload_job(tenant_uuid("tenant-a"), job_id=shared_id, title="First")
        job2 = _make_upload_job(tenant_uuid("tenant-b"), job_id=shared_id, title="Second")

        db_session.add(job1)
        await db_session.commit()

        db_session.add(job2)
        with pytest.raises(IntegrityError):
            await db_session.flush()


@pytest.mark.unit
class TestUploadJobSourceTypeDefault:
    def test_file_source_type_server_default_is_url(self) -> None:
        """An INSERT that omits file_source_type must not land in path mode."""
        col = UploadJob.__table__.c.file_source_type
        assert col.server_default is not None
        assert str(col.server_default.arg) == "url"
