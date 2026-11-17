"""Tests for YouTubeService."""

from __future__ import annotations

import asyncio
import http.client
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httplib2
import pytest

from src.services.youtube_service import UploadAbortedError, YouTubeService


class _FakeHttpError(Exception):
    """Minimal stand-in for ``googleapiclient.errors.HttpError`` with typed ``resp``."""

    def __init__(self, message: str, *, status: int) -> None:
        super().__init__(message)
        self.resp = SimpleNamespace(status=status)


def _make_http_error(*, status: int, message: str = "Server error") -> _FakeHttpError:
    """Create a fake HttpError with the given HTTP status code."""
    return _FakeHttpError(message, status=status)


@pytest.fixture
def settings(mock_settings):
    return mock_settings


@pytest.fixture
def youtube_service(settings):
    return YouTubeService(settings=settings)


# ---------------------------------------------------------------------------
# build_service
# ---------------------------------------------------------------------------


class TestBuildService:
    @patch("src.services.youtube_service.build")
    def test_build_service_calls_google_build(self, mock_build):
        creds = MagicMock()
        YouTubeService.build_service(creds)
        mock_build.assert_called_once_with(
            "youtube", "v3", credentials=creds, cache_discovery=False
        )

    @patch("src.services.youtube_service.build")
    def test_build_service_returns_service_object(self, mock_build):
        creds = MagicMock()
        mock_build.return_value = MagicMock()
        result = YouTubeService.build_service(creds)
        assert result is mock_build.return_value


# ---------------------------------------------------------------------------
# get_channel_info
# ---------------------------------------------------------------------------


class TestGetChannelInfo:
    @patch("src.services.youtube_service.build")
    def test_returns_channel_info(self, mock_build):
        mock_service = MagicMock()
        mock_build.return_value = mock_service
        mock_service.channels.return_value.list.return_value.execute.return_value = {
            "items": [{"id": "UC123", "snippet": {"title": "My Channel"}}]
        }

        result = YouTubeService.get_channel_info(MagicMock())
        assert result == {"id": "UC123", "title": "My Channel"}

    @patch("src.services.youtube_service.build")
    def test_returns_none_when_no_channels(self, mock_build):
        mock_service = MagicMock()
        mock_build.return_value = mock_service
        mock_service.channels.return_value.list.return_value.execute.return_value = {"items": []}

        result = YouTubeService.get_channel_info(MagicMock())
        assert result is None


# ---------------------------------------------------------------------------
# upload_video
# ---------------------------------------------------------------------------


class TestUploadVideo:
    @patch("src.services.youtube_service.MediaFileUpload")
    @patch("src.services.youtube_service.build")
    def test_upload_video_success(self, mock_build, mock_media_cls, youtube_service):
        mock_service = MagicMock()
        mock_build.return_value = mock_service
        mock_media = MagicMock()
        mock_media_cls.return_value = mock_media

        # Simulate successful upload in one chunk
        mock_request = MagicMock()
        mock_service.videos.return_value.insert.return_value = mock_request
        mock_request.next_chunk.return_value = (None, {"id": "abc123"})

        result = youtube_service.upload_video(
            credentials=MagicMock(),
            file_path="/tmp/test.mp4",
            title="Test Video",
            description="A test",
            tags=["tag1", "tag2"],
            category_id="22",
            privacy_status="private",
        )

        assert result == {"video_id": "abc123", "url": "https://youtu.be/abc123"}

    @patch("src.services.youtube_service.MediaFileUpload")
    @patch("src.services.youtube_service.build")
    def test_upload_video_with_progress(self, mock_build, mock_media_cls, youtube_service):
        mock_service = MagicMock()
        mock_build.return_value = mock_service

        mock_request = MagicMock()
        mock_service.videos.return_value.insert.return_value = mock_request

        # First chunk: progress, second: complete
        status_mock = MagicMock()
        status_mock.progress.return_value = 0.5
        mock_request.next_chunk.side_effect = [
            (status_mock, None),
            (None, {"id": "xyz789"}),
        ]

        result = youtube_service.upload_video(
            credentials=MagicMock(),
            file_path="/tmp/test.mp4",
            title="Test",
        )

        assert result["video_id"] == "xyz789"

    @patch("src.services.youtube_service.MediaFileUpload")
    @patch("src.services.youtube_service.build")
    def test_upload_video_permanent_failure(self, mock_build, mock_media_cls, youtube_service):
        mock_service = MagicMock()
        mock_build.return_value = mock_service

        mock_request = MagicMock()
        mock_service.videos.return_value.insert.return_value = mock_request
        mock_request.next_chunk.side_effect = ValueError("permanent error")

        with pytest.raises(ValueError, match="permanent error"):
            youtube_service.upload_video(
                credentials=MagicMock(),
                file_path="/tmp/test.mp4",
                title="Fail",
            )

    @patch("src.services.youtube_service.time")
    @patch("src.services.youtube_service.MediaFileUpload")
    @patch("src.services.youtube_service.build")
    def test_upload_video_retries_on_500(
        self, mock_build, mock_media_cls, mock_time, youtube_service
    ):
        mock_service = MagicMock()
        mock_build.return_value = mock_service

        mock_request = MagicMock()
        mock_service.videos.return_value.insert.return_value = mock_request

        # First call raises retryable error, second succeeds
        # Google API errors have a .resp attribute with .status
        error = _make_http_error(status=500)
        mock_request.next_chunk.side_effect = [error, (None, {"id": "retry-ok"})]

        result = youtube_service.upload_video(
            credentials=MagicMock(),
            file_path="/tmp/test.mp4",
            title="Retry Test",
        )

        assert result["video_id"] == "retry-ok"
        mock_time.sleep.assert_called_once()

    @patch("src.services.youtube_service.time")
    @patch("src.services.youtube_service.MediaFileUpload")
    @patch("src.services.youtube_service.build")
    def test_upload_video_retries_on_transient_network_error(
        self, mock_build, mock_media_cls, mock_time, youtube_service
    ):
        """A mid-stream network exception carries no HTTP ``.resp`` — it must
        still be retried, not treated as a permanent failure that strands the
        rendered video. Regression for the resp-only retry gate."""
        mock_service = MagicMock()
        mock_build.return_value = mock_service

        mock_request = MagicMock()
        mock_service.videos.return_value.insert.return_value = mock_request

        # ConnectionResetError (a ConnectionError) has no ``.resp`` attribute.
        mock_request.next_chunk.side_effect = [
            ConnectionResetError("connection reset by peer"),
            (None, {"id": "net-retry-ok"}),
        ]

        result = youtube_service.upload_video(
            credentials=MagicMock(),
            file_path="/tmp/test.mp4",
            title="Transient Net Retry",
        )

        assert result["video_id"] == "net-retry-ok"
        mock_time.sleep.assert_called_once()

    @patch("src.services.youtube_service.MediaFileUpload")
    @patch("src.services.youtube_service.build")
    def test_upload_video_uses_correct_body(self, mock_build, mock_media_cls, youtube_service):
        mock_service = MagicMock()
        mock_build.return_value = mock_service

        mock_request = MagicMock()
        mock_service.videos.return_value.insert.return_value = mock_request
        mock_request.next_chunk.return_value = (None, {"id": "vid1"})

        youtube_service.upload_video(
            credentials=MagicMock(),
            file_path="/tmp/test.mp4",
            title="My Title",
            description="My Desc",
            tags=["a", "b"],
            category_id="10",
            privacy_status="unlisted",
        )

        call_kwargs = mock_service.videos.return_value.insert.call_args[1]
        assert call_kwargs["part"] == "snippet,status"
        body = call_kwargs["body"]
        assert body["snippet"]["title"] == "My Title"
        assert body["snippet"]["description"] == "My Desc"
        assert body["snippet"]["tags"] == ["a", "b"]
        assert body["snippet"]["categoryId"] == "10"
        assert body["status"]["privacyStatus"] == "unlisted"


# ---------------------------------------------------------------------------
# Cooperative cancellation of an in-flight resumable upload
# ---------------------------------------------------------------------------


class _NeverFinishingChunker:
    """A ``next_chunk`` stand-in that never completes the upload.

    Counts every chunk it transfers so a test can prove that transfer actually
    *ceased* after cancellation, not merely that a flag was flipped.
    """

    def __init__(self, chunk_delay: float = 0.01) -> None:
        self.calls = 0
        self.first_chunk = threading.Event()
        self._chunk_delay = chunk_delay
        self._lock = threading.Lock()

    def next_chunk(self):
        with self._lock:
            self.calls += 1
        self.first_chunk.set()
        time.sleep(self._chunk_delay)
        status = MagicMock()
        status.progress.return_value = 0.1
        return (status, None)  # never returns a response => never completes

    @property
    def count(self) -> int:
        with self._lock:
            return self.calls


class TestUploadCancellation:
    """A1: cancelling the awaiting coroutine must STOP the upload thread.

    ``asyncio.to_thread`` cannot interrupt a blocking ``next_chunk()``; without a
    cooperative stop signal the thread runs to completion and publishes the video
    even after the job's lease has been released for a successor to retry.
    """

    def test_preset_stop_event_aborts_before_any_chunk(self, youtube_service):
        chunker = _NeverFinishingChunker()
        stop = threading.Event()
        stop.set()

        with pytest.raises(UploadAbortedError):
            youtube_service._resumable_upload(chunker, stop_event=stop)

        assert chunker.count == 0, "aborted upload must not transfer a single chunk"

    def test_stop_event_mid_upload_halts_the_loop(self, youtube_service):
        chunker = _NeverFinishingChunker()
        stop = threading.Event()

        def _stop_after_first_chunk() -> None:
            chunker.first_chunk.wait(timeout=5)
            stop.set()

        stopper = threading.Thread(target=_stop_after_first_chunk)
        stopper.start()
        try:
            with pytest.raises(UploadAbortedError):
                youtube_service._resumable_upload(chunker, stop_event=stop)
        finally:
            stopper.join(timeout=5)

        # The loop exits at a chunk boundary, so at most the chunk that was
        # already in flight when the signal landed may still have run.
        assert chunker.count <= 2

    def test_sleep_or_abort_wakes_early_when_stopped(self, youtube_service):
        stop = threading.Event()
        stop.set()
        started = time.monotonic()
        youtube_service._sleep_or_abort(30.0, stop)
        assert time.monotonic() - started < 5.0

    @pytest.mark.asyncio
    @patch("src.services.youtube_service.MediaFileUpload")
    @patch("src.services.youtube_service.build")
    async def test_cancelling_async_upload_stops_chunk_transfer(
        self, mock_build, mock_media_cls, youtube_service
    ):
        mock_service = MagicMock()
        mock_build.return_value = mock_service
        chunker = _NeverFinishingChunker()
        mock_service.videos.return_value.insert.return_value = chunker

        task = asyncio.create_task(
            youtube_service.async_upload_video(
                credentials=MagicMock(),
                file_path="/tmp/test.mp4",
                title="Cancel Me",
            )
        )
        # Wait for the thread to actually be transferring.
        await asyncio.to_thread(chunker.first_chunk.wait, 5)
        assert chunker.count >= 1

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # Give the thread ample time to keep going if it were still uploading:
        # each chunk takes ~10ms, so an unstopped thread would add ~50 chunks.
        await asyncio.sleep(0.5)
        settled = chunker.count
        await asyncio.sleep(0.5)
        assert chunker.count == settled, (
            "upload thread kept transferring chunks after cancellation "
            f"({settled} -> {chunker.count})"
        )

    @pytest.mark.asyncio
    @patch("src.services.youtube_service.MediaFileUpload")
    @patch("src.services.youtube_service.build")
    async def test_task_manager_shutdown_stops_chunk_transfer(
        self, mock_build, mock_media_cls, youtube_service
    ):
        """The pod-shutdown straggler cancel reaches the upload thread too.

        ``TaskManager.shutdown`` drains, then cancels whatever is left — that
        cancel is the same signal, so the thread stops before the reaper's
        lease-release hands the job to a successor.
        """
        from src.infra.task_manager import TaskManager

        mock_service = MagicMock()
        mock_build.return_value = mock_service
        chunker = _NeverFinishingChunker()
        mock_service.videos.return_value.insert.return_value = chunker

        manager = TaskManager(max_concurrent=1)
        await manager.submit(
            "job-1",
            youtube_service.async_upload_video(
                credentials=MagicMock(),
                file_path="/tmp/test.mp4",
                title="Shutdown Me",
            ),
        )
        await asyncio.to_thread(chunker.first_chunk.wait, 5)

        await manager.shutdown(timeout=0.1)

        await asyncio.sleep(0.5)
        settled = chunker.count
        await asyncio.sleep(0.5)
        assert chunker.count == settled


# ---------------------------------------------------------------------------
# set_thumbnail
# ---------------------------------------------------------------------------


class TestSetThumbnail:
    @patch("src.services.youtube_service.MediaFileUpload")
    @patch("src.services.youtube_service.build")
    def test_set_thumbnail_calls_thumbnails_set(self, mock_build, mock_media_cls):
        mock_service = MagicMock()
        mock_build.return_value = mock_service
        mock_media_cls.return_value = MagicMock()

        YouTubeService.set_thumbnail(MagicMock(), "vid123", "/tmp/cover.jpg", mimetype="image/jpeg")

        mock_media_cls.assert_called_once_with(
            "/tmp/cover.jpg", mimetype="image/jpeg", resumable=False
        )
        mock_service.thumbnails.return_value.set.assert_called_once_with(
            videoId="vid123", media_body=mock_media_cls.return_value
        )
        mock_service.thumbnails.return_value.set.return_value.execute.assert_called_once()

    @patch("src.services.youtube_service.MediaFileUpload")
    @patch("src.services.youtube_service.build")
    def test_set_thumbnail_propagates_api_error(self, mock_build, mock_media_cls):
        mock_service = MagicMock()
        mock_build.return_value = mock_service
        mock_service.thumbnails.return_value.set.return_value.execute.side_effect = (
            _make_http_error(status=403, message="thumbnailNotAvailable")
        )

        with pytest.raises(_FakeHttpError):
            YouTubeService.set_thumbnail(MagicMock(), "vid", "/tmp/c.png", mimetype="image/png")

    @pytest.mark.asyncio
    @patch("src.services.youtube_service.MediaFileUpload")
    @patch("src.services.youtube_service.build")
    async def test_async_set_thumbnail(self, mock_build, mock_media_cls):
        mock_service = MagicMock()
        mock_build.return_value = mock_service

        await YouTubeService.async_set_thumbnail(MagicMock(), "vid789", "/tmp/cover.jpg")

        mock_service.thumbnails.return_value.set.assert_called_once()


# ---------------------------------------------------------------------------
# insert_captions
# ---------------------------------------------------------------------------


class TestInsertCaptions:
    @patch("src.services.youtube_service.MediaFileUpload")
    @patch("src.services.youtube_service.build")
    def test_insert_captions_calls_captions_insert(self, mock_build, mock_media_cls):
        mock_service = MagicMock()
        mock_build.return_value = mock_service
        mock_media_cls.return_value = MagicMock()

        YouTubeService.insert_captions(
            MagicMock(), "vid123", "/tmp/track.srt", language="en", name="English"
        )

        mock_media_cls.assert_called_once_with(
            "/tmp/track.srt", mimetype="application/octet-stream", resumable=False
        )
        call_kwargs = mock_service.captions.return_value.insert.call_args.kwargs
        assert call_kwargs["part"] == "snippet"
        assert call_kwargs["media_body"] is mock_media_cls.return_value
        snippet = call_kwargs["body"]["snippet"]
        assert snippet["videoId"] == "vid123"
        assert snippet["language"] == "en"
        assert snippet["name"] == "English"
        assert snippet["isDraft"] is False
        mock_service.captions.return_value.insert.return_value.execute.assert_called_once()

    @patch("src.services.youtube_service.MediaFileUpload")
    @patch("src.services.youtube_service.build")
    def test_insert_captions_propagates_api_error(self, mock_build, mock_media_cls):
        mock_service = MagicMock()
        mock_build.return_value = mock_service
        mock_service.captions.return_value.insert.return_value.execute.side_effect = (
            _make_http_error(status=403, message="insufficientPermissions")
        )

        with pytest.raises(_FakeHttpError):
            YouTubeService.insert_captions(MagicMock(), "vid", "/tmp/track.srt")

    @pytest.mark.asyncio
    @patch("src.services.youtube_service.MediaFileUpload")
    @patch("src.services.youtube_service.build")
    async def test_async_insert_captions(self, mock_build, mock_media_cls):
        mock_service = MagicMock()
        mock_build.return_value = mock_service

        await YouTubeService.async_insert_captions(MagicMock(), "vid789", "/tmp/track.srt")

        mock_service.captions.return_value.insert.assert_called_once()


# ---------------------------------------------------------------------------
# async wrappers
# ---------------------------------------------------------------------------


class TestAsyncWrappers:
    @pytest.mark.asyncio
    @patch("src.services.youtube_service.build")
    async def test_async_get_channel_info(self, mock_build):
        mock_service = MagicMock()
        mock_build.return_value = mock_service
        mock_service.channels.return_value.list.return_value.execute.return_value = {
            "items": [{"id": "UC456", "snippet": {"title": "Async Channel"}}]
        }

        result = await YouTubeService.async_get_channel_info(MagicMock())
        assert result == {"id": "UC456", "title": "Async Channel"}

    @pytest.mark.asyncio
    @patch("src.services.youtube_service.MediaFileUpload")
    @patch("src.services.youtube_service.build")
    async def test_async_upload_video(self, mock_build, mock_media_cls, youtube_service):
        mock_service = MagicMock()
        mock_build.return_value = mock_service

        mock_request = MagicMock()
        mock_service.videos.return_value.insert.return_value = mock_request
        mock_request.next_chunk.return_value = (None, {"id": "async-vid"})

        result = await youtube_service.async_upload_video(
            credentials=MagicMock(),
            file_path="/tmp/test.mp4",
            title="Async Test",
        )

        assert result == {"video_id": "async-vid", "url": "https://youtu.be/async-vid"}


# ---------------------------------------------------------------------------
# Regression: google-auth TransportError must be retryable
# ---------------------------------------------------------------------------


class TestGoogleAuthTransportErrorIsRetryable:
    """``google.auth.exceptions.TransportError`` is the shape a transport blip
    takes when it happens inside the *credential refresh* that
    ``google_auth_httplib2.AuthorizedHttp`` performs automatically mid-upload.

    ``AuthorizedHttp.request`` calls ``credentials.before_request(self._request…)``
    on every request and ``credentials.refresh(self._request)`` on a 401, where
    ``self._request`` is a ``google_auth_httplib2.Request``. That wrapper
    (google_auth_httplib2.py:123-124) catches ``httplib2.HttpLib2Error`` and
    ``http.client.HTTPException`` and **re-raises them as
    ``google.auth.exceptions.TransportError``** — i.e. it converts away exactly
    the two families ``RETRYABLE_EXCEPTIONS`` lists. ``TransportError``
    subclasses ``GoogleAuthError(Exception)``: it is not an ``OSError``, not an
    ``HttpLib2Error``, and carries no ``.resp``, so the retry gate cannot fire
    and a multi-GB already-rendered (paid) upload dies permanently at byte N.
    """

    @patch("src.services.youtube_service.time")
    @patch("src.services.youtube_service.MediaFileUpload")
    @patch("src.services.youtube_service.build")
    def test_precondition_plain_connection_error_is_retried(
        self, mock_build, mock_media_cls, mock_time, youtube_service
    ):
        """PRECONDITION: the retry machinery under test really does work for an
        exception that IS in ``RETRYABLE_EXCEPTIONS`` — so a failure of the
        sibling test below is about the missing type, not a broken fixture."""
        mock_service = MagicMock()
        mock_build.return_value = mock_service
        mock_request = MagicMock()
        mock_service.videos.return_value.insert.return_value = mock_request
        mock_request.next_chunk.side_effect = [
            ConnectionResetError("reset"),
            (None, {"id": "precondition-ok"}),
        ]

        result = youtube_service.upload_video(
            credentials=MagicMock(), file_path="/tmp/test.mp4", title="Precondition"
        )

        assert result["video_id"] == "precondition-ok"
        assert mock_request.next_chunk.call_count == 2

    def test_transport_error_must_be_listed_explicitly(self):
        """The premise: TransportError is reachable by NO generic gate.

        It is not an ``OSError`` (so the ``ConnectionError``/``TimeoutError``/
        ``ssl.SSLError`` entries miss it), it is not an ``HttpLib2Error`` or an
        ``HTTPException`` (the wrapper converted those away), and it carries no
        ``.resp`` (so the status-code gate misses it). The only way the retry
        loop can ever see it is by naming it in ``RETRYABLE_EXCEPTIONS``.
        """
        from google.auth.exceptions import TransportError

        from src.services.youtube_service import RETRYABLE_EXCEPTIONS

        exc = TransportError("connection reset during token refresh")
        assert not isinstance(exc, OSError)
        assert not isinstance(exc, httplib2.HttpLib2Error)
        assert not isinstance(exc, http.client.HTTPException)
        assert not hasattr(exc, "resp")
        assert isinstance(exc, RETRYABLE_EXCEPTIONS)

    @patch("src.services.youtube_service.time")
    @patch("src.services.youtube_service.MediaFileUpload")
    @patch("src.services.youtube_service.build")
    def test_upload_video_retries_on_google_auth_transport_error(
        self, mock_build, mock_media_cls, mock_time, youtube_service
    ):
        """A transport blip during the automatic mid-upload token refresh must
        be retried, not treated as a permanent failure."""
        from google.auth.exceptions import TransportError

        mock_service = MagicMock()
        mock_build.return_value = mock_service
        mock_request = MagicMock()
        mock_service.videos.return_value.insert.return_value = mock_request
        mock_request.next_chunk.side_effect = [
            TransportError("connection reset during token refresh"),
            (None, {"id": "transport-retry-ok"}),
        ]

        result = youtube_service.upload_video(
            credentials=MagicMock(), file_path="/tmp/test.mp4", title="Transport Retry"
        )

        assert result["video_id"] == "transport-retry-ok"
        assert mock_request.next_chunk.call_count == 2

    @patch("src.services.youtube_service.time")
    @patch("src.services.youtube_service.MediaFileUpload")
    @patch("src.services.youtube_service.build")
    def test_upload_video_retries_on_google_auth_timeout_error(
        self, mock_build, mock_media_cls, mock_time, youtube_service
    ):
        """``google.auth.exceptions.TimeoutError`` shadows the builtin name but
        subclasses ``GoogleAuthError``, NOT ``builtins.TimeoutError`` — so the
        ``TimeoutError`` entry in the tuple does not cover it."""
        from google.auth.exceptions import TimeoutError as GoogleAuthTimeoutError

        assert not isinstance(GoogleAuthTimeoutError("t"), TimeoutError)

        mock_service = MagicMock()
        mock_build.return_value = mock_service
        mock_request = MagicMock()
        mock_service.videos.return_value.insert.return_value = mock_request
        mock_request.next_chunk.side_effect = [
            GoogleAuthTimeoutError("token endpoint timed out"),
            (None, {"id": "timeout-retry-ok"}),
        ]

        result = youtube_service.upload_video(
            credentials=MagicMock(), file_path="/tmp/test.mp4", title="Timeout Retry"
        )

        assert result["video_id"] == "timeout-retry-ok"
        assert mock_request.next_chunk.call_count == 2


@pytest.mark.unit
class TestRetryCapIsEnforced:
    """The retry cap is what stops a permanently-broken resumable upload from
    looping forever (each iteration re-uploading a chunk of a paid render).

    Every other retry test in this file uses exactly ONE transient failure, so
    none of them ever reaches ``retries == max_retries`` — flipping the gate at
    ``youtube_service.py`` from ``retries < max_retries`` to ``<=`` left them all
    green. These drive repeated failures to the cap and pin the exact attempt
    count, so an off-by-one on either side of the boundary is caught.
    """

    @patch("src.services.youtube_service.time")
    def test_stops_after_exactly_max_retries(self, mock_time, youtube_service):
        youtube_service._settings.upload.max_retries = 3

        request = MagicMock()
        request.next_chunk.side_effect = _make_http_error(status=500)

        with pytest.raises(_FakeHttpError):
            youtube_service._resumable_upload(request)

        # 1 initial attempt + exactly 3 retries, then it gives up.
        assert request.next_chunk.call_count == 4, (
            "retry cap off by "
            f"{request.next_chunk.call_count - 4}: max_retries=3 must allow "
            "4 total attempts"
        )
        assert mock_time.sleep.call_count == 3

    @patch("src.services.youtube_service.time")
    def test_max_retries_zero_means_no_retry_at_all(self, mock_time, youtube_service):
        """The lower boundary: ``max_retries=0`` must not retry even once."""
        youtube_service._settings.upload.max_retries = 0

        request = MagicMock()
        request.next_chunk.side_effect = _make_http_error(status=500)

        with pytest.raises(_FakeHttpError):
            youtube_service._resumable_upload(request)

        assert request.next_chunk.call_count == 1, (
            "max_retries=0 retried anyway — the cap is off by one"
        )
        mock_time.sleep.assert_not_called()

    @patch("src.services.youtube_service.time")
    def test_a_successful_chunk_resets_the_retry_budget(self, mock_time, youtube_service):
        """The counter resets on progress, so the cap bounds CONSECUTIVE
        failures, not failures over the life of a multi-chunk upload."""
        youtube_service._settings.upload.max_retries = 2

        status = MagicMock()
        status.progress.return_value = 0.5
        request = MagicMock()
        request.next_chunk.side_effect = [
            _make_http_error(status=500),
            _make_http_error(status=503),
            (status, None),  # progress: budget resets here
            _make_http_error(status=500),
            _make_http_error(status=503),
            (None, {"id": "reset-ok"}),
        ]

        result = youtube_service._resumable_upload(request)

        assert result["video_id"] == "reset-ok"
        assert request.next_chunk.call_count == 6
        assert mock_time.sleep.call_count == 4


# ---------------------------------------------------------------------------
# set_private — the retraction direction
# ---------------------------------------------------------------------------


def _videos_service(status: dict | None) -> MagicMock:
    service = MagicMock()
    items = [] if status is None else [{"id": "vid1", "status": status}]
    service.videos.return_value.list.return_value.execute.return_value = {"items": items}
    return service


class TestSetPrivate:
    def test_flips_privacy_and_preserves_other_writable_fields(self):
        service = _videos_service(
            {
                "privacyStatus": "unlisted",
                "embeddable": False,
                "license": "creativeCommon",
                "publicStatsViewable": False,
                "selfDeclaredMadeForKids": False,
                "containsSyntheticMedia": True,
                "uploadStatus": "processed",
            }
        )
        with patch.object(YouTubeService, "build_service", return_value=service):
            previous = YouTubeService.set_private(MagicMock(), "vid1")

        assert previous == "unlisted"
        service.videos.return_value.list.assert_called_once_with(part="status", id="vid1")
        service.videos.return_value.update.assert_called_once_with(
            part="status",
            body={
                "id": "vid1",
                "status": {
                    "privacyStatus": "private",
                    "embeddable": False,
                    "license": "creativeCommon",
                    "publicStatsViewable": False,
                    "selfDeclaredMadeForKids": False,
                    "containsSyntheticMedia": True,
                },
            },
        )

    def test_drops_publish_at_so_a_schedule_cannot_undo_the_retraction(self):
        service = _videos_service({"privacyStatus": "public", "publishAt": "2026-12-01T00:00:00Z"})
        with patch.object(YouTubeService, "build_service", return_value=service):
            YouTubeService.set_private(MagicMock(), "vid1")

        body = service.videos.return_value.update.call_args.kwargs["body"]
        assert body["status"] == {"privacyStatus": "private"}

    def test_already_private_is_not_written_again(self):
        service = _videos_service({"privacyStatus": "private"})
        with patch.object(YouTubeService, "build_service", return_value=service):
            previous = YouTubeService.set_private(MagicMock(), "vid1")

        assert previous == "private"
        service.videos.return_value.update.assert_not_called()

    def test_unknown_video_raises(self):
        from src.services.youtube_service import VideoNotFoundError

        service = _videos_service(None)
        with (
            patch.object(YouTubeService, "build_service", return_value=service),
            pytest.raises(VideoNotFoundError),
        ):
            YouTubeService.set_private(MagicMock(), "vid1")
        service.videos.return_value.update.assert_not_called()


# ---------------------------------------------------------------------------
# set_privacy — the governed release direction
# ---------------------------------------------------------------------------


class TestSetPrivacy:
    def test_raises_privacy_and_preserves_other_writable_fields(self):
        service = _videos_service(
            {
                "privacyStatus": "private",
                "embeddable": False,
                "containsSyntheticMedia": True,
                "publishAt": "2026-12-01T00:00:00Z",
                "uploadStatus": "processed",
            }
        )
        with patch.object(YouTubeService, "build_service", return_value=service):
            previous = YouTubeService.set_privacy(MagicMock(), "vid1", "unlisted")

        assert previous == "private"
        service.videos.return_value.update.assert_called_once_with(
            part="status",
            body={
                "id": "vid1",
                "status": {
                    "privacyStatus": "unlisted",
                    "embeddable": False,
                    "containsSyntheticMedia": True,
                },
            },
        )

    def test_already_at_target_is_not_written_again(self):
        service = _videos_service({"privacyStatus": "public"})
        with patch.object(YouTubeService, "build_service", return_value=service):
            previous = YouTubeService.set_privacy(MagicMock(), "vid1", "public")

        assert previous == "public"
        service.videos.return_value.update.assert_not_called()

    def test_private_target_is_refused_before_any_call(self):
        service = _videos_service({"privacyStatus": "unlisted"})
        with (
            patch.object(YouTubeService, "build_service", return_value=service),
            pytest.raises(ValueError, match="privacy target"),
        ):
            YouTubeService.set_privacy(MagicMock(), "vid1", "private")
        service.videos.return_value.list.assert_not_called()
        service.videos.return_value.update.assert_not_called()

    def test_unknown_video_raises(self):
        from src.services.youtube_service import VideoNotFoundError

        service = _videos_service(None)
        with (
            patch.object(YouTubeService, "build_service", return_value=service),
            pytest.raises(VideoNotFoundError),
        ):
            YouTubeService.set_privacy(MagicMock(), "vid1", "public")
        service.videos.return_value.update.assert_not_called()
