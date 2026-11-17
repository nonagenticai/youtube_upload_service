"""Tests for upload API routes."""

from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.dependencies import get_tenant_id, get_upload_service
from src.api.upload_routes import router
from src.database.models import UploadJob
from src.services.upload_service import AlreadyPublishedError
from tests.orchestrator_sa import SA_SUBJECT, allow_orchestrator_sa, stamp_license_lane
from tests.tenants import NIL_UUID_SPELLINGS, tenant_uuid

# The real caller tenant the orchestrator forwards as ``X-Owner-Tenant-Id``.
# Deliberately DIFFERENT from the channel tenant (tenant_uuid("test-tenant")): the two are
# distinct columns and the fail-closed guard exists precisely because the
# channel tenant is the same value for every caller.
OWNER_TENANT = tenant_uuid("owner-tenant-7")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_job(**overrides) -> MagicMock:
    """Create a mock UploadJob with sensible defaults."""
    defaults = {
        "job_id": "job-123",
        "tenant_id": tenant_uuid("test-tenant"),
        "status": "pending",
        "file_source": "/videos/test.mp4",
        "file_source_type": "path",
        "title": "Test Video",
        "description": "A test",
        "tags": ["tag1"],
        "category_id": "22",
        "privacy_status": "private",
        "youtube_video_id": None,
        "youtube_url": None,
        "error_message": None,
        "created_at": dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        "started_at": None,
        "completed_at": None,
        "thumbnail_source": None,
        "thumbnail_applied": None,
        "caption_source": None,
        "caption_applied": None,
    }
    defaults.update(overrides)
    job = MagicMock(spec=UploadJob)
    for k, v in defaults.items():
        setattr(job, k, v)
    return job


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_upload_svc():
    return AsyncMock()


@pytest.fixture
def app(mock_upload_svc, monkeypatch):
    """The caller is the orchestrator SA (licence lane, delegating subject) —
    the only caller allowed to assert ``X-Owner-Tenant-Id``."""
    allow_orchestrator_sa(monkeypatch)
    test_app = FastAPI()
    stamp_license_lane(test_app)
    test_app.include_router(router)
    test_app.dependency_overrides[get_tenant_id] = lambda: tenant_uuid("test-tenant")
    test_app.dependency_overrides[get_upload_service] = lambda: mock_upload_svc
    return test_app


@pytest.fixture
def client(app):
    """A client that behaves like the real orchestrator: it forwards the real
    caller tenant on every request. ``get_owner_tenant_id`` is NOT overridden —
    the header is the only way to bind an owner, by design."""
    return TestClient(app, headers={"X-Owner-Tenant-Id": OWNER_TENANT})


@pytest.fixture
def headerless_client(app):
    """A client that sends no ``X-Owner-Tenant-Id`` — i.e. the pre-fix caller
    that produced 35 write-only rows in prod."""
    return TestClient(app)


# ---------------------------------------------------------------------------
# POST /api/upload
# ---------------------------------------------------------------------------


class TestStartUpload:
    def test_creates_job_and_returns_response(self, client, mock_upload_svc):
        mock_upload_svc.create_job.return_value = _make_job()

        response = client.post(
            "/api/upload",
            json={
                "file_source": "https://storage.test/w2v/test.mp4",
                "file_source_type": "url",
                "title": "Test Video",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["job_id"] == "job-123"
        assert data["status"] == "pending"
        assert data["title"] == "Test Video"
        assert data["tenant_id"] == tenant_uuid("test-tenant")

    def test_passes_all_fields_to_service(self, client, mock_upload_svc):
        mock_upload_svc.create_job.return_value = _make_job()

        client.post(
            "/api/upload",
            json={
                "file_source": "https://example.com/vid.mp4",
                "file_source_type": "url",
                "title": "Full Fields",
                "description": "Desc",
                "tags": ["a", "b"],
                "category_id": "10",
                "privacy_status": "unlisted",
            },
        )

        mock_upload_svc.create_job.assert_called_once_with(
            tenant_id=tenant_uuid("test-tenant"),
            owner_tenant_id=OWNER_TENANT,
            file_source="https://example.com/vid.mp4",
            file_source_type="url",
            title="Full Fields",
            description="Desc",
            tags=["a", "b"],
            category_id="10",
            privacy_status="unlisted",
            thumbnail_source=None,
            thumbnail_source_type="url",
            caption_source=None,
            caption_source_type="url",
            caption_language="en",
        )

    def test_forwards_thumbnail_fields(self, client, mock_upload_svc):
        mock_upload_svc.create_job.return_value = _make_job()

        client.post(
            "/api/upload",
            json={
                "file_source": "https://example.com/vid.mp4",
                "file_source_type": "url",
                "title": "With Cover",
                "thumbnail_source": "https://storage.test/w2v/cover.jpg",
                "thumbnail_source_type": "url",
            },
        )

        call_kwargs = mock_upload_svc.create_job.call_args.kwargs
        assert call_kwargs["thumbnail_source"] == "https://storage.test/w2v/cover.jpg"
        assert call_kwargs["thumbnail_source_type"] == "url"

    def test_rejects_invalid_file_source_type(self, client):
        response = client.post(
            "/api/upload",
            json={
                "file_source": "/videos/test.mp4",
                "file_source_type": "ftp",
                "title": "Bad Type",
            },
        )
        assert response.status_code == 422

    def test_rejects_invalid_privacy_status(self, client):
        response = client.post(
            "/api/upload",
            json={
                "file_source": "/videos/test.mp4",
                "title": "Bad Privacy",
                "privacy_status": "secret",
            },
        )
        assert response.status_code == 422

    def test_defaults_applied(self, client, mock_upload_svc):
        mock_upload_svc.create_job.return_value = _make_job()

        client.post(
            "/api/upload",
            json={"file_source": "https://storage.test/w2v/test.mp4", "title": "Defaults"},
        )

        call_kwargs = mock_upload_svc.create_job.call_args[1]
        assert call_kwargs["file_source_type"] == "url"
        assert call_kwargs["category_id"] == "22"
        assert call_kwargs["privacy_status"] == "private"

    def test_title_required(self, client):
        response = client.post(
            "/api/upload",
            json={"file_source": "/videos/test.mp4"},
        )
        assert response.status_code == 422

    def test_file_source_required(self, client):
        response = client.post(
            "/api/upload",
            json={"title": "No Source"},
        )
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/upload/jobs/{job_id}
# ---------------------------------------------------------------------------


class TestGetUploadJob:
    def test_returns_job(self, client, mock_upload_svc):
        mock_upload_svc.get_job.return_value = _make_job(status="completed", youtube_video_id="yt1")

        response = client.get("/api/upload/jobs/job-123")
        assert response.status_code == 200
        data = response.json()
        assert data["job_id"] == "job-123"
        assert data["status"] == "completed"
        assert data["youtube_video_id"] == "yt1"

    def test_returns_404_when_not_found(self, client, mock_upload_svc):
        mock_upload_svc.get_job.return_value = None

        response = client.get("/api/upload/jobs/no-such-job")
        assert response.status_code == 404

    def test_scopes_to_tenant(self, client, mock_upload_svc):
        mock_upload_svc.get_job.return_value = _make_job()

        client.get("/api/upload/jobs/job-123")

        mock_upload_svc.get_job.assert_called_once_with(
            "job-123", tenant_uuid("test-tenant"), OWNER_TENANT
        )


# ---------------------------------------------------------------------------
# GET /api/upload/jobs
# ---------------------------------------------------------------------------


class TestListUploadJobs:
    def test_returns_job_list(self, client, mock_upload_svc):
        mock_upload_svc.list_jobs.return_value = ([_make_job(), _make_job(job_id="job-456")], 2)

        response = client.get("/api/upload/jobs")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 2
        assert len(data["jobs"]) == 2

    def test_returns_empty_list(self, client, mock_upload_svc):
        mock_upload_svc.list_jobs.return_value = ([], 0)

        response = client.get("/api/upload/jobs")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 0
        assert data["jobs"] == []

    def test_passes_pagination_params(self, client, mock_upload_svc):
        mock_upload_svc.list_jobs.return_value = ([], 0)

        client.get("/api/upload/jobs?limit=10&offset=20")

        mock_upload_svc.list_jobs.assert_called_once_with(
            tenant_uuid("test-tenant"), OWNER_TENANT, limit=10, offset=20
        )

    def test_x_owner_tenant_id_header_scopes_the_real_owner(self, client, mock_upload_svc):
        """The orchestrator forwards the real caller as X-Owner-Tenant-Id; list
        must scope to it (channel tenant stays the SA tenant for RLS binding)."""
        mock_upload_svc.list_jobs.return_value = ([], 0)

        client.get("/api/upload/jobs", headers={"X-Owner-Tenant-Id": tenant_uuid("real-tenant-9")})

        mock_upload_svc.list_jobs.assert_called_once_with(
            tenant_uuid("test-tenant"), tenant_uuid("real-tenant-9"), limit=50, offset=0
        )

    def test_completed_job_includes_youtube_fields(self, client, mock_upload_svc):
        job = _make_job(
            status="completed",
            youtube_video_id="yt-abc",
            youtube_url="https://youtu.be/yt-abc",
            completed_at=dt.datetime(2026, 1, 2, tzinfo=dt.UTC),
        )
        mock_upload_svc.list_jobs.return_value = ([job], 1)

        response = client.get("/api/upload/jobs")
        data = response.json()
        j = data["jobs"][0]
        assert j["youtube_video_id"] == "yt-abc"
        assert j["youtube_url"] == "https://youtu.be/yt-abc"
        assert j["completed_at"] is not None

    def test_failed_job_includes_error(self, client, mock_upload_svc):
        job = _make_job(
            status="failed",
            error_message="Upload quota exceeded",
        )
        mock_upload_svc.list_jobs.return_value = ([job], 1)

        response = client.get("/api/upload/jobs")
        j = response.json()["jobs"][0]
        assert j["status"] == "failed"
        assert j["error_message"] == "Upload quota exceeded"


class TestSourceTypeGuard:
    """POST /api/upload must never put a job into path mode."""

    def test_omitted_file_source_type_reaches_service_as_url(self, client, mock_upload_svc):
        """The real regression guard: omitting the field must NOT mean path mode.

        Both the request schema default AND the DB column's ``server_default``
        used to be ``"path"``, so a caller who simply left the field out got a
        container-filesystem read.
        """
        mock_upload_svc.create_job.return_value = _make_job(file_source_type="url")

        response = client.post(
            "/api/upload",
            json={"file_source": "https://storage.test/w2v/v.mp4", "title": "V"},
        )

        assert response.status_code == 200, response.text
        assert mock_upload_svc.create_job.await_args.kwargs["file_source_type"] == "url"

    def test_explicit_path_source_type_is_422_not_500(self, client, mock_upload_svc):
        mock_upload_svc.create_job.return_value = _make_job(file_source_type="path")

        response = client.post(
            "/api/upload",
            json={
                "file_source": "/var/run/secrets/kubernetes.io/serviceaccount/token",
                "file_source_type": "path",
                "title": "V",
            },
        )

        assert response.status_code == 422, response.text
        mock_upload_svc.create_job.assert_not_awaited()


# ---------------------------------------------------------------------------
# Regression: pagination bounds
# ---------------------------------------------------------------------------


class TestListJobsPaginationBounds:
    """``limit``/``offset`` on GET /api/upload/jobs are plain ``int`` defaults
    with no bounds, so whatever the caller sends is handed straight to
    ``UploadService.list_jobs`` → ``.limit()/.offset()`` → Postgres.

    A negative value renders ``LIMIT -1`` / ``OFFSET -1``, which Postgres
    rejects (``22023 invalid_row_count_in_limit_clause``) — a 500 where the
    boundary should have produced a 422. An enormous value renders no ceiling
    at all, so one request can pull every row the tenant owns into memory and
    serialise it.
    """

    def test_precondition_default_pagination_is_accepted(self, client, mock_upload_svc):
        """PRECONDITION: the route works and forwards sane values, so a failure
        below is about missing validation, not a broken fixture."""
        mock_upload_svc.list_jobs.return_value = ([], 0)

        response = client.get("/api/upload/jobs?limit=10&offset=20")

        assert response.status_code == 200
        assert mock_upload_svc.list_jobs.call_args.kwargs == {"limit": 10, "offset": 20}

    def test_negative_limit_is_rejected_at_the_boundary(self, client, mock_upload_svc):
        """``?limit=-1`` must 422, not reach the DB as ``LIMIT -1``."""
        mock_upload_svc.list_jobs.return_value = ([], 0)

        response = client.get("/api/upload/jobs?limit=-1")

        assert response.status_code == 422, (
            f"expected 422, got {response.status_code}; "
            f"list_jobs called with {mock_upload_svc.list_jobs.call_args}"
        )
        mock_upload_svc.list_jobs.assert_not_called()

    def test_negative_offset_is_rejected_at_the_boundary(self, client, mock_upload_svc):
        """``?offset=-5`` must 422, not reach the DB as ``OFFSET -5``."""
        mock_upload_svc.list_jobs.return_value = ([], 0)

        response = client.get("/api/upload/jobs?offset=-5")

        assert response.status_code == 422, (
            f"expected 422, got {response.status_code}; "
            f"list_jobs called with {mock_upload_svc.list_jobs.call_args}"
        )
        mock_upload_svc.list_jobs.assert_not_called()

    def test_oversized_limit_is_rejected_at_the_boundary(self, client, mock_upload_svc):
        """An unbounded ``limit`` is an unbounded read: cap it at the boundary."""
        mock_upload_svc.list_jobs.return_value = ([], 0)

        response = client.get("/api/upload/jobs?limit=100000000")

        assert response.status_code == 422, (
            f"expected 422, got {response.status_code}; "
            f"list_jobs called with {mock_upload_svc.list_jobs.call_args}"
        )
        mock_upload_svc.list_jobs.assert_not_called()


# ---------------------------------------------------------------------------
# Regression: owner tenant must be bound, fail closed
# ---------------------------------------------------------------------------


class TestOwnerTenantFailsClosed:
    """``X-Owner-Tenant-Id`` is the ONLY source of the real caller tenant.

    The old ``get_owner_tenant_id`` fell back to the *channel* tenant when the
    header was missing. That value is identical for every caller, so the job was
    written under a tenant its own poller never queries: 27 of 48 prod
    ``upload_jobs`` rows are NULL/sentinel-owned and unreadable (20 NULL, 6
    synthetic, 1 other). These tests lock the header in as mandatory and
    non-sentinel.

    The earlier count of 35 included 8 rows owned by
    ``00000000-0000-0000-0000-000000000001``. That is a REAL tenant, not a
    sentinel (only the nil VALUE is), so those 8 rows are readable by
    their owner and were never part of the defect.
    """

    # The nil UUID is included in ALL FOUR of its accepted spellings. The guard
    # this replaces was a lowercase string set naming only the dashed form, so
    # ``00000000000000000000000000000000`` and the braced/urn forms parsed to the
    # same nil VALUE and were written as an unreadable ``owner_tenant_id``.
    SENTINELS = ("anonymous", "none", "null", "n8n-dev", *NIL_UUID_SPELLINGS)

    def test_post_without_owner_header_is_400_naming_the_header(
        self, headerless_client, mock_upload_svc
    ):
        mock_upload_svc.create_job.return_value = _make_job()

        response = headerless_client.post(
            "/api/upload",
            json={
                "file_source": "https://storage.test/w2v/test.mp4",
                "file_source_type": "url",
                "title": "No Owner",
            },
        )

        assert response.status_code == 400, response.text
        assert "X-Owner-Tenant-Id" in response.json()["detail"]
        mock_upload_svc.create_job.assert_not_awaited()

    def test_get_job_without_owner_header_is_400(self, headerless_client, mock_upload_svc):
        mock_upload_svc.get_job.return_value = _make_job()

        response = headerless_client.get("/api/upload/jobs/job-123")

        assert response.status_code == 400, response.text
        assert "X-Owner-Tenant-Id" in response.json()["detail"]
        mock_upload_svc.get_job.assert_not_awaited()

    def test_list_jobs_without_owner_header_is_400(self, headerless_client, mock_upload_svc):
        mock_upload_svc.list_jobs.return_value = ([], 0)

        response = headerless_client.get("/api/upload/jobs")

        assert response.status_code == 400, response.text
        mock_upload_svc.list_jobs.assert_not_awaited()

    def test_blank_owner_header_is_400(self, headerless_client, mock_upload_svc):
        response = headerless_client.post(
            "/api/upload",
            json={
                "file_source": "https://storage.test/w2v/test.mp4",
                "file_source_type": "url",
                "title": "Blank Owner",
            },
            headers={"X-Owner-Tenant-Id": "   "},
        )

        assert response.status_code == 400, response.text
        mock_upload_svc.create_job.assert_not_awaited()

    @pytest.mark.parametrize("sentinel", SENTINELS)
    def test_sentinel_owner_header_is_400(self, headerless_client, mock_upload_svc, sentinel):
        response = headerless_client.post(
            "/api/upload",
            json={
                "file_source": "https://storage.test/w2v/test.mp4",
                "file_source_type": "url",
                "title": "Sentinel Owner",
            },
            headers={"X-Owner-Tenant-Id": sentinel},
        )

        assert response.status_code == 400, response.text
        assert "X-Owner-Tenant-Id" in response.json()["detail"]
        mock_upload_svc.create_job.assert_not_awaited()

    def test_nil_like_uuid_one_is_a_real_tenant_and_passes(
        self, headerless_client, mock_upload_svc
    ):
        """``...0001`` LOOKS like a sentinel and was rejected as one. It is a
        real ``active`` row in the orchestrator ``tenants`` table owning 97
        projects and 9 members; its ``name`` is NULL, which is the only reason
        the UI renders a bare UUID. Rejecting it locked the largest tenant in
        the system out of the upload API, so the guard must let it through."""
        mock_upload_svc.get_job.return_value = _make_job()

        response = headerless_client.get(
            "/api/upload/jobs/job-123",
            headers={"X-Owner-Tenant-Id": "00000000-0000-0000-0000-000000000001"},
        )

        assert response.status_code == 200, response.text
        mock_upload_svc.get_job.assert_awaited()

    def test_valid_owner_header_still_works(self, headerless_client, mock_upload_svc):
        """The guard is not over-broad: a real owner tenant passes through
        unchanged and reaches the service as ``owner_tenant_id``."""
        mock_upload_svc.create_job.return_value = _make_job()

        response = headerless_client.post(
            "/api/upload",
            json={
                "file_source": "https://storage.test/w2v/test.mp4",
                "file_source_type": "url",
                "title": "Real Owner",
            },
            headers={"X-Owner-Tenant-Id": "8f1c2b60-0d8e-4a1f-9c3d-2b7e6a5f4d31"},
        )

        assert response.status_code == 200, response.text
        assert (
            mock_upload_svc.create_job.await_args.kwargs["owner_tenant_id"]
            == "8f1c2b60-0d8e-4a1f-9c3d-2b7e6a5f4d31"
        )

    def test_owner_header_is_whitespace_trimmed(self, headerless_client, mock_upload_svc):
        mock_upload_svc.list_jobs.return_value = ([], 0)

        response = headerless_client.get(
            "/api/upload/jobs", headers={"X-Owner-Tenant-Id": f"  {tenant_uuid('real-tenant-9')}  "}
        )

        assert response.status_code == 200, response.text
        assert mock_upload_svc.list_jobs.await_args.args == (
            tenant_uuid("test-tenant"),
            tenant_uuid("real-tenant-9"),
        )


# ---------------------------------------------------------------------------
# POST /api/upload — publish-once
# ---------------------------------------------------------------------------


class TestStartUploadRefusesRepublish:
    """This route is the ONLY caller of the upload path — no automatic retry
    exists anywhere — so a repeat POST is always a human re-submitting, usually
    after reading a job the reaper marked ``failed``. It must be a 409 that
    names the existing video, not a second paid publish."""

    def test_already_published_render_is_409_naming_the_video(self, client, mock_upload_svc):
        mock_upload_svc.create_job.side_effect = AlreadyPublishedError(
            "https://storage.test/w2v/paid.mp4",
            "job-earlier",
            "yt-ABC",
            "https://youtu.be/yt-ABC",
        )

        response = client.post(
            "/api/upload",
            json={
                "file_source": "https://storage.test/w2v/paid.mp4",
                "file_source_type": "url",
                "title": "Paid Render",
            },
        )

        assert response.status_code == 409
        detail = response.json()["detail"]
        assert detail["error"] == "already_published"
        assert detail["youtube_video_id"] == "yt-ABC"
        assert detail["youtube_url"] == "https://youtu.be/yt-ABC"
        assert detail["job_id"] == "job-earlier"
        assert "yt-ABC" in detail["message"]


# ---------------------------------------------------------------------------
# The job response must distinguish REQUESTED from APPLIED
# ---------------------------------------------------------------------------


class TestAppliedFlagsOnJobResponse:
    """The orchestrator polls GET /api/upload/jobs/{id} and reads ``status``.

    With only ``status`` on the wire, a publish that lost its thumbnail and its
    subtitles is indistinguishable from a complete one. The response therefore
    carries both halves: what was requested, and what actually landed.
    """

    def test_completed_job_reports_thumbnail_requested_but_not_applied(
        self, client, mock_upload_svc
    ):
        mock_upload_svc.get_job.return_value = _make_job(
            status="completed",
            youtube_video_id="yt1",
            thumbnail_source="https://storage.test/cover.png",
            thumbnail_applied=False,
            caption_source="https://storage.test/track.srt",
            caption_applied=False,
        )
        body = client.get("/api/upload/jobs/job-123").json()
        assert body["status"] == "completed"
        assert body["thumbnail_requested"] is True
        assert body["thumbnail_applied"] is False
        assert body["caption_requested"] is True
        assert body["caption_applied"] is False

    def test_fully_applied_job_reports_true(self, client, mock_upload_svc):
        mock_upload_svc.get_job.return_value = _make_job(
            status="completed",
            youtube_video_id="yt1",
            thumbnail_source="https://storage.test/cover.png",
            thumbnail_applied=True,
            caption_source="https://storage.test/track.srt",
            caption_applied=True,
        )
        body = client.get("/api/upload/jobs/job-123").json()
        assert body["thumbnail_requested"] is True
        assert body["thumbnail_applied"] is True
        assert body["caption_requested"] is True
        assert body["caption_applied"] is True

    def test_nothing_requested_reports_false_requested_and_null_applied(
        self, client, mock_upload_svc
    ):
        """Not requested is not the same claim as requested-and-failed."""
        mock_upload_svc.get_job.return_value = _make_job(status="completed", youtube_video_id="yt1")
        body = client.get("/api/upload/jobs/job-123").json()
        assert body["thumbnail_requested"] is False
        assert body["thumbnail_applied"] is None
        assert body["caption_requested"] is False
        assert body["caption_applied"] is None


# ---------------------------------------------------------------------------
# POST /api/upload/jobs/{job_id}/retract
# ---------------------------------------------------------------------------


class TestRetractUpload:
    def test_retracts_and_returns_private_job(self, client, mock_upload_svc):
        mock_upload_svc.retract_job.return_value = _make_job(
            status="completed", privacy_status="private", youtube_video_id="v1"
        )
        response = client.post("/api/upload/jobs/job-123/retract")
        assert response.status_code == 200
        assert response.json()["privacy_status"] == "private"
        mock_upload_svc.retract_job.assert_awaited_once_with(
            "job-123", tenant_uuid("test-tenant"), OWNER_TENANT
        )

    def test_unknown_job_is_404(self, client, mock_upload_svc):
        mock_upload_svc.retract_job.return_value = None
        assert client.post("/api/upload/jobs/nope/retract").status_code == 404

    def test_unpublished_job_is_409(self, client, mock_upload_svc):
        from src.services.upload_service import NotPublishedError

        mock_upload_svc.retract_job.side_effect = NotPublishedError("job-123")
        response = client.post("/api/upload/jobs/job-123/retract")
        assert response.status_code == 409
        assert response.json()["detail"]["error"] == "not_published"

    def test_youtube_failure_is_502_not_500(self, client, mock_upload_svc):
        mock_upload_svc.retract_job.side_effect = RuntimeError("quota")
        response = client.post("/api/upload/jobs/job-123/retract")
        assert response.status_code == 502
        assert response.json()["detail"]["error"] == "youtube_retract_failed"

    def test_requires_owner_header(self, headerless_client, mock_upload_svc):
        response = headerless_client.post("/api/upload/jobs/job-123/retract")
        assert response.status_code in (400, 401, 403, 422)
        mock_upload_svc.retract_job.assert_not_called()


# ---------------------------------------------------------------------------
# POST /api/upload/jobs/{job_id}/privacy
# ---------------------------------------------------------------------------


class TestSetUploadPrivacy:
    def test_raises_and_returns_job(self, client, mock_upload_svc):
        mock_upload_svc.set_job_privacy.return_value = _make_job(
            status="completed", privacy_status="unlisted", youtube_video_id="v1"
        )
        response = client.post(
            "/api/upload/jobs/job-123/privacy", json={"privacy_status": "unlisted"}
        )
        assert response.status_code == 200
        assert response.json()["privacy_status"] == "unlisted"
        mock_upload_svc.set_job_privacy.assert_awaited_once_with(
            "job-123", tenant_uuid("test-tenant"), OWNER_TENANT, "unlisted"
        )

    def test_private_target_is_rejected(self, client, mock_upload_svc):
        response = client.post(
            "/api/upload/jobs/job-123/privacy", json={"privacy_status": "private"}
        )
        assert response.status_code == 422
        mock_upload_svc.set_job_privacy.assert_not_called()

    def test_unknown_job_is_404(self, client, mock_upload_svc):
        mock_upload_svc.set_job_privacy.return_value = None
        response = client.post("/api/upload/jobs/nope/privacy", json={"privacy_status": "public"})
        assert response.status_code == 404

    def test_unpublished_job_is_409(self, client, mock_upload_svc):
        from src.services.upload_service import NotPublishedError

        mock_upload_svc.set_job_privacy.side_effect = NotPublishedError("job-123")
        response = client.post(
            "/api/upload/jobs/job-123/privacy", json={"privacy_status": "public"}
        )
        assert response.status_code == 409

    def test_youtube_failure_is_502_not_500(self, client, mock_upload_svc):
        mock_upload_svc.set_job_privacy.side_effect = RuntimeError("quota")
        response = client.post(
            "/api/upload/jobs/job-123/privacy", json={"privacy_status": "public"}
        )
        assert response.status_code == 502
        assert response.json()["detail"]["error"] == "youtube_privacy_failed"

    def test_requires_owner_header(self, headerless_client, mock_upload_svc):
        response = headerless_client.post(
            "/api/upload/jobs/job-123/privacy", json={"privacy_status": "public"}
        )
        assert response.status_code in (400, 401, 403, 422)
        mock_upload_svc.set_job_privacy.assert_not_called()


# ---------------------------------------------------------------------------
# X-Owner-Tenant-Id is honoured ONLY from the orchestrator service account
# ---------------------------------------------------------------------------


class TestOwnerHeaderRequiresOrchestratorSA:
    """The owner tenant now selects WHOSE YouTube grant publishes/retracts, so a
    caller that is not the delegating orchestrator SA must not be able to assert
    it — otherwise any licence holder could publish on another tenant's channel."""

    @staticmethod
    def _client(monkeypatch, mock_upload_svc, *, subject: str | None, configured: bool = True):
        if configured:
            allow_orchestrator_sa(monkeypatch)
        test_app = FastAPI()
        stamp_license_lane(test_app, subject)
        test_app.include_router(router)
        test_app.dependency_overrides[get_tenant_id] = lambda: tenant_uuid("test-tenant")
        test_app.dependency_overrides[get_upload_service] = lambda: mock_upload_svc
        return TestClient(test_app, headers={"X-Owner-Tenant-Id": OWNER_TENANT})

    @pytest.mark.parametrize(
        ("subject", "configured"),
        [
            ("0198a5e2-0000-7000-8000-c0570e000001", True),  # customer licence
            (None, True),  # Keycloak-JWT / trusted-network lane: no introspection
            (SA_SUBJECT, False),  # AUTH_DELEGATING_SUBJECTS unset -> lane disabled
        ],
    )
    def test_spoofed_owner_header_is_403_and_never_reaches_the_service(
        self, monkeypatch, mock_upload_svc, subject, configured
    ):
        client = self._client(monkeypatch, mock_upload_svc, subject=subject, configured=configured)

        post = client.post(
            "/api/upload",
            json={"file_source": "https://storage.test/w2v/x.mp4", "title": "spoof"},
        )
        retract = client.post("/api/upload/jobs/job-1/retract")

        assert post.status_code == 403, post.text
        assert retract.status_code == 403, retract.text
        mock_upload_svc.create_job.assert_not_awaited()
        mock_upload_svc.retract_job.assert_not_awaited()

    def test_orchestrator_sa_is_accepted(self, monkeypatch, mock_upload_svc):
        mock_upload_svc.create_job.return_value = _make_job()
        client = self._client(monkeypatch, mock_upload_svc, subject=SA_SUBJECT)

        resp = client.post(
            "/api/upload",
            json={"file_source": "https://storage.test/w2v/x.mp4", "title": "ok"},
        )

        assert resp.status_code == 200, resp.text
        assert mock_upload_svc.create_job.await_args.kwargs["owner_tenant_id"] == OWNER_TENANT
