"""YouTube Data API v3 wrapper.

Handles building an authenticated YouTube service, resumable video upload
with exponential backoff, and channel info retrieval.

All YouTube API calls are synchronous (httplib2 under the hood), so they
run via ``asyncio.to_thread()`` to avoid blocking the event loop.
"""

from __future__ import annotations

import asyncio
import http.client
import logging
import random
import ssl
import threading
import time
from typing import Any

import httplib2
from google.auth import exceptions as google_auth_exceptions
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from src.config import Settings

logger = logging.getLogger(__name__)

# YouTube API quota: videos.insert costs 1600 units out of 10,000 daily (~6 uploads/day)
YOUTUBE_API_SERVICE_NAME = "youtube"
YOUTUBE_API_VERSION = "v3"

# Retryable HTTP status codes for resumable upload
RETRYABLE_STATUS_CODES = {500, 502, 503, 504}

# Transient *network* exceptions raised by ``request.next_chunk()`` that carry no
# HTTP ``.resp`` — a mid-stream TCP reset / read timeout / TLS hiccup on a
# multi-GB chunked upload is routine and must be retried, not treated as a
# permanent failure that strands an already-rendered (paid) video as ``failed``.
# Mirrors Google's canonical resumable-upload RETRIABLE_EXCEPTIONS list.
# (ConnectionError / TimeoutError / ssl.SSLError subclass OSError; the
# http.client ones subclass HTTPException, so they must be listed explicitly.)
#
# ``google.auth.exceptions.TransportError`` is the same blip wearing a
# different type. ``google_auth_httplib2.AuthorizedHttp`` calls
# ``credentials.before_request(self._request, ...)`` on every request and
# ``credentials.refresh(self._request)`` on a 401 — and that ``_request`` is a
# ``google_auth_httplib2.Request``, whose ``__call__`` catches
# ``httplib2.HttpLib2Error`` / ``http.client.HTTPException`` and **re-raises
# them as TransportError** (google_auth_httplib2.py:123-124). So the automatic
# mid-upload token refresh — the one a >1h multi-GB upload always hits —
# converts away exactly the two families listed above. TransportError
# subclasses ``GoogleAuthError(Exception)``: not an OSError, not an
# HttpLib2Error, and it carries no ``.resp``, so without it here the retry gate
# cannot fire and an already-rendered (paid) upload dies permanently at byte N,
# costing another full transfer plus another 1600 quota units on the retry.
# ``google.auth.exceptions.TimeoutError`` shadows the builtin name but likewise
# subclasses GoogleAuthError, so the builtin ``TimeoutError`` entry misses it.
# ``RefreshError`` is deliberately NOT listed: a revoked/invalid grant is
# permanent and must fail fast.
RETRYABLE_EXCEPTIONS = (
    httplib2.HttpLib2Error,
    ConnectionError,
    TimeoutError,
    ssl.SSLError,
    http.client.IncompleteRead,
    http.client.NotConnected,
    http.client.BadStatusLine,
    google_auth_exceptions.TransportError,
    google_auth_exceptions.TimeoutError,
)


class UploadAbortedError(RuntimeError):
    """Raised when a resumable upload is cooperatively aborted mid-transfer.

    Signalled by cancelling the awaiting coroutine (``TaskManager.cancel`` or the
    straggler-cancel in ``TaskManager.shutdown``). The upload loop observes the
    abort between chunks and stops pushing bytes, so no video is ever published
    for an aborted job.

    **What state this leaves:** the resumable session on Google's side is left
    incomplete. An incomplete resumable session yields **no video id and no
    published video** — YouTube only materialises the video when the final chunk
    completes the session — and Google garbage-collects abandoned sessions. We
    deliberately do NOT persist ``request.resumable_uri``, so the session is
    abandoned rather than resumed: a retry re-uploads from byte zero. That is
    exactly the contract the reaper advertises when it fails a stranded job as
    "safe to retry".
    """


# The writable members of a video's ``status`` part (YouTube Data API v3,
# ``videos.update``). Everything else in a ``videos.list`` status (uploadStatus,
# failureReason, rejectionReason, madeForKids) is read-only and is not sent back.
# ``publishAt`` is writable but deliberately NOT carried over: a scheduled publish
# time on a private video makes YouTube flip it public at that moment, which would
# silently undo the retraction. Omitting it from the replaced status clears it.
_WRITABLE_STATUS_FIELDS = (
    "privacyStatus",
    "embeddable",
    "license",
    "publicStatsViewable",
    "selfDeclaredMadeForKids",
    "containsSyntheticMedia",
)


# The privacy values a governed release may raise a video to. ``private`` is excluded: lowering
# visibility is the retraction path (``set_private``), never this one.
RAISABLE_PRIVACY: frozenset[str] = frozenset({"unlisted", "public"})


class VideoNotFoundError(LookupError):
    """The channel the credentials belong to has no video with this id."""

    def __init__(self, video_id: str) -> None:
        self.video_id = video_id
        super().__init__(f"YouTube video {video_id} not found on the authenticated channel")


class YouTubeService:
    """Wraps the YouTube Data API v3 for video upload and channel queries."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @staticmethod
    def build_service(credentials: Credentials) -> Any:
        """Build an authenticated YouTube API service object (synchronous)."""
        return build(
            YOUTUBE_API_SERVICE_NAME,
            YOUTUBE_API_VERSION,
            credentials=credentials,
            cache_discovery=False,
        )

    @staticmethod
    def get_channel_info(credentials: Credentials) -> dict | None:
        """Fetch the authenticated user's channel ID and title (synchronous).

        Returns ``{"id": "UC...", "title": "My Channel"}`` or ``None``.
        """
        service = build(
            YOUTUBE_API_SERVICE_NAME,
            YOUTUBE_API_VERSION,
            credentials=credentials,
            cache_discovery=False,
        )
        response = service.channels().list(part="snippet", mine=True).execute()
        items = response.get("items", [])
        if not items:
            return None
        channel = items[0]
        return {
            "id": channel["id"],
            "title": channel["snippet"]["title"],
        }

    def upload_video(
        self,
        credentials: Credentials,
        file_path: str,
        title: str,
        *,
        description: str | None = None,
        tags: list[str] | None = None,
        category_id: str = "22",
        privacy_status: str = "private",
        stop_event: threading.Event | None = None,
    ) -> dict:
        """Upload a video via resumable upload with exponential backoff (synchronous).

        Returns ``{"video_id": "...", "url": "https://youtu.be/..."}`` on success.
        Raises on permanent failure.

        ``stop_event`` is the cooperative-cancellation signal: when it is set the
        loop stops between chunks and raises :class:`UploadAbortedError` instead
        of running the transfer to completion. ``None`` (the default) keeps the
        historical uninterruptible behaviour for direct synchronous callers.
        """
        service = self.build_service(credentials)
        body = {
            "snippet": {
                "title": title,
                "description": description or "",
                "tags": tags or [],
                "categoryId": category_id,
            },
            "status": {
                "privacyStatus": privacy_status,
            },
        }

        media = MediaFileUpload(
            file_path,
            chunksize=self._settings.upload.chunk_size_bytes,
            resumable=True,
        )

        request = service.videos().insert(
            part="snippet,status",
            body=body,
            media_body=media,
        )

        return self._resumable_upload(request, stop_event=stop_event)

    @staticmethod
    def _sleep_or_abort(delay: float, stop_event: threading.Event | None) -> None:
        """Sleep *delay* seconds, waking early if *stop_event* is set.

        Without a stop event this is a plain ``time.sleep`` (the historical
        behaviour). With one, the backoff wait is itself interruptible so an
        abort is not stuck behind a multi-second retry delay.
        """
        if stop_event is None:
            time.sleep(delay)
        else:
            stop_event.wait(delay)

    def _resumable_upload(self, request: Any, *, stop_event: threading.Event | None = None) -> dict:
        """Execute a resumable upload with exponential backoff.

        Loops indefinitely for successful chunk uploads, only counting retries
        on transient errors. A large file may need hundreds of chunks — retries
        are for errors, not progress.

        The loop is *driven by us*, one ``next_chunk()`` call per iteration, so
        the chunk boundary is the cooperative-cancellation point: ``stop_event``
        is checked before every chunk (and after every backoff sleep). A set
        event aborts the transfer with :class:`UploadAbortedError` rather than
        letting a thread whose awaiting coroutine has already been cancelled
        keep publishing — the double-publish window that the DB-side CAS could
        only mitigate after the fact.

        Returns ``{"video_id": "...", "url": "https://youtu.be/..."}`` on success.
        """
        max_retries = self._settings.upload.max_retries
        base_delay = self._settings.upload.retry_base_delay
        retries = 0

        while True:
            if stop_event is not None and stop_event.is_set():
                logger.warning(
                    "Resumable upload aborted before chunk %d: stop signalled "
                    "(cancellation/shutdown); session abandoned, nothing published",
                    retries,
                )
                raise UploadAbortedError(
                    "Upload aborted by cancellation/shutdown before completion"
                )
            try:
                status, response = request.next_chunk()
                if status:
                    logger.info("Upload progress: %.1f%%", status.progress() * 100)
                if response is not None:
                    video_id = response["id"]
                    logger.info("Upload complete: video_id=%s", video_id)
                    return {
                        "video_id": video_id,
                        "url": f"https://youtu.be/{video_id}",
                    }
                retries = 0  # reset on successful chunk
            except Exception as exc:
                http_status = getattr(exc, "resp", {})
                status_code = getattr(http_status, "status", None) if http_status else None

                retryable = status_code in RETRYABLE_STATUS_CODES or isinstance(
                    exc, RETRYABLE_EXCEPTIONS
                )
                if retryable and retries < max_retries:
                    retries += 1
                    delay = base_delay * (2**retries) + random.random()  # noqa: S311  # jitter, not crypto
                    logger.warning(
                        "Retryable error (HTTP %s / %s), retry %d/%d in %.1fs: %s",
                        status_code,
                        type(exc).__name__,
                        retries,
                        max_retries,
                        delay,
                        exc,
                    )
                    self._sleep_or_abort(delay, stop_event)
                else:
                    logger.error(
                        "Upload failed permanently after %d retries: %s",
                        retries,
                        exc,
                    )
                    raise

    async def async_upload_video(
        self,
        credentials: Credentials,
        file_path: str,
        title: str,
        *,
        description: str | None = None,
        tags: list[str] | None = None,
        category_id: str = "22",
        privacy_status: str = "private",
        stop_event: threading.Event | None = None,
    ) -> dict:
        """Async wrapper around :meth:`upload_video` — runs in a thread.

        Cancelling this coroutine (``TaskManager.cancel``, or the straggler
        cancel inside ``TaskManager.shutdown``) cannot interrupt the blocking
        ``next_chunk()`` call the thread is inside — ``asyncio.to_thread`` has no
        way to kill a thread. So on ``CancelledError`` we *set the stop event*
        before re-raising: the thread wakes at the next chunk boundary, aborts
        the transfer and abandons the resumable session. Without this the pod
        could keep publishing a video whose job row had already been released
        (``expire_upload_job_leases`` at shutdown) for a successor to retry —
        the double-publish class the DB-side CAS could not reach.

        Callers may pass their own ``stop_event`` to share one signal with other
        machinery; otherwise one is created per call.
        """
        stop = stop_event if stop_event is not None else threading.Event()
        try:
            return await asyncio.to_thread(
                self.upload_video,
                credentials,
                file_path,
                title,
                description=description,
                tags=tags,
                category_id=category_id,
                privacy_status=privacy_status,
                stop_event=stop,
            )
        except asyncio.CancelledError:
            stop.set()
            logger.warning(
                "Upload coroutine cancelled for %r — signalled the upload thread to abort "
                "at the next chunk boundary",
                title,
            )
            raise

    @staticmethod
    def set_thumbnail(
        credentials: Credentials,
        video_id: str,
        file_path: str,
        *,
        mimetype: str = "image/jpeg",
    ) -> None:
        """Set a custom thumbnail on an uploaded video (synchronous).

        Requires a verified YouTube channel — an unverified channel gets a 403.
        Callers should treat any failure as non-fatal (the video is already up).
        """
        service = YouTubeService.build_service(credentials)
        media = MediaFileUpload(file_path, mimetype=mimetype, resumable=False)
        service.thumbnails().set(videoId=video_id, media_body=media).execute()
        logger.info("Set thumbnail for video %s", video_id)

    @staticmethod
    async def async_set_thumbnail(
        credentials: Credentials,
        video_id: str,
        file_path: str,
        *,
        mimetype: str = "image/jpeg",
    ) -> None:
        """Async wrapper around :meth:`set_thumbnail` — runs in a thread."""
        await asyncio.to_thread(
            YouTubeService.set_thumbnail, credentials, video_id, file_path, mimetype=mimetype
        )

    @staticmethod
    def insert_captions(
        credentials: Credentials,
        video_id: str,
        file_path: str,
        *,
        language: str = "en",
        name: str = "",
        mimetype: str = "application/octet-stream",
    ) -> None:
        """Upload a soft caption track (SRT/VTT) to *video_id* (synchronous).

        Requires the ``youtube.force-ssl`` scope — a token without it gets a
        403. Callers should treat any failure as non-fatal (the video is
        already up).
        """
        service = YouTubeService.build_service(credentials)
        media = MediaFileUpload(file_path, mimetype=mimetype, resumable=False)
        body = {
            "snippet": {
                "videoId": video_id,
                "language": language,
                "name": name,
                "isDraft": False,
            }
        }
        service.captions().insert(part="snippet", body=body, media_body=media).execute()
        logger.info("Inserted caption track (%s) for video %s", language, video_id)

    @staticmethod
    async def async_insert_captions(
        credentials: Credentials,
        video_id: str,
        file_path: str,
        *,
        language: str = "en",
        name: str = "",
        mimetype: str = "application/octet-stream",
    ) -> None:
        """Async wrapper around :meth:`insert_captions` — runs in a thread."""
        await asyncio.to_thread(
            YouTubeService.insert_captions,
            credentials,
            video_id,
            file_path,
            language=language,
            name=name,
            mimetype=mimetype,
        )

    @staticmethod
    def set_private(credentials: Credentials, video_id: str) -> str:
        """Set an uploaded video's privacy to ``private`` (synchronous); return the prior value.

        This is the RETRACTION direction only: a video that left the platform with
        claims it should not carry is taken off every viewer except the channel
        owner, and it stays on the channel so the act is reversible from YouTube
        Studio. Raising visibility is the separate :meth:`set_privacy`, reached only
        through a governed release (``marketing.publish_video``) upstream.

        ``videos.update(part="status")`` REPLACES the whole status part: a writable
        field left out of the body is reset to its default. So the current status is
        read first and every writable field is sent back unchanged, with only
        ``privacyStatus`` flipped. Requires ``youtube.force-ssl`` (or ``youtube``).
        Idempotent: an already-private video is not written again.
        """
        service = YouTubeService.build_service(credentials)
        response = service.videos().list(part="status", id=video_id).execute()
        items = response.get("items", [])
        if not items:
            raise VideoNotFoundError(video_id)
        current = dict(items[0].get("status") or {})
        previous = str(current.get("privacyStatus") or "")
        if previous == "private":
            logger.info("Video %s is already private — nothing to retract", video_id)
            return previous
        status = {k: current[k] for k in _WRITABLE_STATUS_FIELDS if k in current}
        status["privacyStatus"] = "private"
        service.videos().update(part="status", body={"id": video_id, "status": status}).execute()
        logger.warning("Retracted video %s: privacy %s -> private", video_id, previous)
        return previous

    @staticmethod
    def set_privacy(credentials: Credentials, video_id: str, target: str) -> str:
        """Set an uploaded video's privacy to ``unlisted`` or ``public``; return the prior value.

        The RELEASE direction, the counterpart of :meth:`set_private`: a video uploaded
        private is made visible once a human approved it upstream
        (``marketing.publish_video``). ``target`` is restricted to
        :data:`RAISABLE_PRIVACY` — taking a video private is :meth:`set_private`'s job.
        Never deletes. Same read-then-write as :meth:`set_private` because
        ``videos.update(part="status")`` replaces the whole status part; ``publishAt``
        is never sent (YouTube accepts it only on private videos). Idempotent: a video
        already at ``target`` is not written again.
        """
        if target not in RAISABLE_PRIVACY:
            raise ValueError(f"privacy target must be one of {sorted(RAISABLE_PRIVACY)}")
        service = YouTubeService.build_service(credentials)
        response = service.videos().list(part="status", id=video_id).execute()
        items = response.get("items", [])
        if not items:
            raise VideoNotFoundError(video_id)
        current = dict(items[0].get("status") or {})
        previous = str(current.get("privacyStatus") or "")
        if previous == target:
            logger.info("Video %s is already %s — nothing to change", video_id, target)
            return previous
        status = {k: current[k] for k in _WRITABLE_STATUS_FIELDS if k in current}
        status["privacyStatus"] = target
        service.videos().update(part="status", body={"id": video_id, "status": status}).execute()
        logger.warning("Released video %s: privacy %s -> %s", video_id, previous, target)
        return previous

    @staticmethod
    async def async_set_privacy(credentials: Credentials, video_id: str, target: str) -> str:
        """Async wrapper around :meth:`set_privacy` — runs in a thread."""
        return await asyncio.to_thread(YouTubeService.set_privacy, credentials, video_id, target)

    @staticmethod
    async def async_set_private(credentials: Credentials, video_id: str) -> str:
        """Async wrapper around :meth:`set_private` — runs in a thread."""
        return await asyncio.to_thread(YouTubeService.set_private, credentials, video_id)

    @staticmethod
    async def async_get_channel_info(credentials: Credentials) -> dict | None:
        """Async wrapper around :meth:`get_channel_info` — runs in a thread."""
        return await asyncio.to_thread(YouTubeService.get_channel_info, credentials)
