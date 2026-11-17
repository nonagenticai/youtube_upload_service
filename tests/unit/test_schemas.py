"""Tests for Pydantic request/response schemas."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.api.schemas import (
    AuthCallbackResponse,
    AuthConnectRequest,
    AuthConnectResponse,
    AuthDisconnectResponse,
    AuthStatusResponse,
    UploadJobListResponse,
    UploadJobResponse,
    UploadRequest,
)
from tests.tenants import tenant_uuid


@pytest.mark.unit
class TestAuthSchemas:
    def test_auth_connect_request_empty(self) -> None:
        req = AuthConnectRequest()
        assert req is not None

    def test_auth_connect_response(self) -> None:
        resp = AuthConnectResponse(
            authorization_url="https://accounts.google.com/o/oauth2/auth?...",
            state="abc123",
        )
        assert resp.authorization_url.startswith("https://")
        assert resp.state == "abc123"

    def test_auth_status_disconnected(self) -> None:
        resp = AuthStatusResponse(
            connected=False,
            status="not_connected",
            mode="none",
            can_publish=False,
            tenant_id=tenant_uuid("t-1"),
        )
        assert resp.connected is False
        assert resp.needs_reconnect is False
        assert resp.channel_id is None
        assert resp.scopes == []

    def test_auth_status_rejects_unknown_status(self) -> None:
        with pytest.raises(ValidationError):
            AuthStatusResponse.model_validate(
                {
                    "connected": False,
                    "status": "fallback",
                    "mode": "platform",
                    "can_publish": True,
                    "tenant_id": tenant_uuid("t-1"),
                }
            )

    def test_auth_status_rejects_unknown_mode(self) -> None:
        with pytest.raises(ValidationError):
            AuthStatusResponse.model_validate(
                {
                    "connected": False,
                    "status": "not_connected",
                    "mode": "fallback",
                    "can_publish": False,
                    "tenant_id": tenant_uuid("t-1"),
                }
            )

    def test_auth_status_connected(self) -> None:
        resp = AuthStatusResponse(
            connected=True,
            status="connected",
            mode="tenant",
            can_publish=True,
            tenant_id=tenant_uuid("t-1"),
            channel_id="UC123",
            channel_title="My Channel",
            scopes=["youtube.upload"],
        )
        assert resp.connected is True
        assert resp.channel_id == "UC123"

    def test_auth_disconnect_response(self) -> None:
        resp = AuthDisconnectResponse(disconnected=True, tenant_id=tenant_uuid("t-1"))
        assert resp.disconnected is True

    def test_auth_callback_response(self) -> None:
        resp = AuthCallbackResponse(success=True, channel_title="Ch", message="ok")
        assert resp.success is True


@pytest.mark.unit
class TestUploadSchemas:
    def test_upload_request_defaults(self) -> None:
        req = UploadRequest(file_source="https://storage.test/w2v/video.mp4", title="My Video")
        assert req.file_source_type == "url"
        assert req.category_id == "22"
        assert req.privacy_status == "private"
        assert req.tags is None
        assert req.description is None

    def test_upload_request_url_source(self) -> None:
        req = UploadRequest(
            file_source="https://example.com/video.mp4",
            file_source_type="url",
            title="Video",
        )
        assert req.file_source_type == "url"

    def test_upload_request_invalid_source_type(self) -> None:
        # Built as a payload dict, not kwargs: ``file_source_type`` is typed
        # ``Literal["url"]``, so an invalid literal is a *static* type error
        # too — and ``ty check .`` covers tests. model_validate is how a real
        # request body arrives anyway.
        with pytest.raises(ValidationError, match="file_source_type"):
            UploadRequest.model_validate(
                {"file_source": "/path", "file_source_type": "ftp", "title": "Video"}
            )

    def test_upload_request_invalid_privacy(self) -> None:
        with pytest.raises(ValidationError, match="privacy_status"):
            UploadRequest(file_source="/path", title="Video", privacy_status="secret")

    def test_upload_request_title_max_length(self) -> None:
        with pytest.raises(ValidationError, match="title"):
            UploadRequest(file_source="/path", title="x" * 101)

    def test_upload_request_file_source_max_length(self) -> None:
        with pytest.raises(ValidationError, match="file_source"):
            UploadRequest(file_source="x" * 2_049, title="Video")

    def test_upload_request_empty_file_source(self) -> None:
        with pytest.raises(ValidationError, match="file_source"):
            UploadRequest(file_source="", title="Video")

    def test_upload_request_empty_title(self) -> None:
        with pytest.raises(ValidationError, match="title"):
            UploadRequest(file_source="/path", title="")

    def test_upload_request_description_max_length(self) -> None:
        with pytest.raises(ValidationError, match="description"):
            UploadRequest(file_source="/path", title="Video", description="x" * 5_001)

    def test_upload_request_tags_max_items(self) -> None:
        with pytest.raises(ValidationError, match="tags"):
            UploadRequest(
                file_source="/path",
                title="Video",
                tags=[f"t{i}" for i in range(501)],
            )

    def test_upload_request_tag_length_enforced(self) -> None:
        with pytest.raises(ValidationError, match="tag exceeds"):
            UploadRequest(file_source="/path", title="Video", tags=["x" * 101])

    def test_upload_request_category_id_pattern(self) -> None:
        """category_id must be a small numeric string (YouTube uses 1-3 digit IDs)."""
        with pytest.raises(ValidationError, match="category_id"):
            UploadRequest(file_source="/path", title="Video", category_id="abc")
        with pytest.raises(ValidationError, match="category_id"):
            UploadRequest(file_source="/path", title="Video", category_id="1234")

    def test_upload_request_boundary_values_accepted(self) -> None:
        """Exact-boundary inputs are valid."""
        req = UploadRequest(
            file_source="x" * 2_048,
            title="x" * 100,
            description="y" * 5_000,
            tags=["z" * 100] * 500,
            category_id="999",
        )
        assert len(req.file_source) == 2_048
        assert req.tags is not None
        assert len(req.tags) == 500

    def test_upload_job_response(self) -> None:
        resp = UploadJobResponse(
            job_id="j-1",
            tenant_id=tenant_uuid("t-1"),
            status="completed",
            file_source="/path",
            file_source_type="path",
            title="Video",
            category_id="22",
            privacy_status="private",
            youtube_video_id="abc123",
            youtube_url="https://youtube.com/watch?v=abc123",
        )
        assert resp.status == "completed"
        assert resp.youtube_video_id == "abc123"

    def test_upload_job_list_response(self) -> None:
        job = UploadJobResponse(
            job_id="j-1",
            tenant_id=tenant_uuid("t-1"),
            status="pending",
            file_source="/path",
            file_source_type="path",
            title="Video",
            category_id="22",
            privacy_status="private",
        )
        resp = UploadJobListResponse(jobs=[job], total=1)
        assert resp.total == 1
        assert len(resp.jobs) == 1


@pytest.mark.unit
class TestUploadSourceTypesAreUrlOnly:
    """Path-mode file sources must be unrepresentable at the API boundary.

    Path mode made the worker read the *container's own* filesystem (no PVC is
    mounted) and upload those bytes to YouTube — service-account tokens
    included. The only in-fleet producer always sends ``"url"``.
    """

    def test_omitted_file_source_type_defaults_to_url(self) -> None:
        """An OMITTED file_source_type must never mean path mode."""
        req = UploadRequest(file_source="https://storage.test/w2v/v.mp4", title="V")
        assert req.file_source_type == "url"

    def test_explicit_path_file_source_type_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="file_source_type"):
            UploadRequest.model_validate(
                {"file_source": "/etc/passwd", "file_source_type": "path", "title": "V"}
            )

    def test_explicit_path_thumbnail_source_type_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="thumbnail_source_type"):
            UploadRequest.model_validate(
                {
                    "file_source": "https://storage.test/w2v/v.mp4",
                    "title": "V",
                    "thumbnail_source": "/var/run/secrets/kubernetes.io/serviceaccount/token",
                    "thumbnail_source_type": "path",
                }
            )

    def test_explicit_path_caption_source_type_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="caption_source_type"):
            UploadRequest.model_validate(
                {
                    "file_source": "https://storage.test/w2v/v.mp4",
                    "title": "V",
                    "caption_source": "/var/run/secrets/kubernetes.io/serviceaccount/token",
                    "caption_source_type": "path",
                }
            )
