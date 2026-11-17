"""Upload API routes for starting and polling video upload jobs."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from w2v_common.auth import LICENSE_GATE

from src.api.dependencies import get_owner_tenant_id, get_tenant_id, get_upload_service
from src.api.schemas import (
    SetPrivacyRequest,
    UploadJobListResponse,
    UploadJobResponse,
    UploadRequest,
)
from src.services.upload_service import (
    AlreadyPublishedError,
    NotPublishedError,
    RetractUnavailableError,
    UploadService,
)
from src.services.youtube_service import VideoNotFoundError

logger = logging.getLogger(__name__)

# Every route here is tenant-facing and BILLED: youtube_upload_service is in
# ``w2v_common.auth.outbound.METERED_DESTINATION_SERVICES``, yet it carried no
# entitlement gate at all — ``AuthMiddleware`` answers "who are you", never
# "may you", so a tenant whose licence was expired or revoked still published to
# YouTube and was metered for it. ``LICENSE_GATE`` is mounted router-wide (not
# per route) so a route added later is gated by default rather than by someone
# remembering.
#
# DORMANT until ``W2V_LICENSE_GATE_ENABLED=true`` is set in gitops — merging
# this changes nothing. The probe paths can never be refused: w2v_common
# short-circuits on ``is_license_exempt_path`` before it asks the licences
# service, so no hand-rolled exemption list belongs here.
router = APIRouter(prefix="/api/upload", tags=["upload"], dependencies=list(LICENSE_GATE))

# Hard ceiling on one page of upload jobs. Unbounded, ``limit`` was handed
# straight to ``LIMIT`` — a negative value is a Postgres 22023 (500, not 422)
# and an enormous one pulls every row the tenant owns into memory.
MAX_PAGE_SIZE = 200


def _job_to_response(job: Any) -> UploadJobResponse:
    """Convert an UploadJob ORM instance to a response schema."""
    return UploadJobResponse(
        job_id=job.job_id,
        tenant_id=job.tenant_id,
        status=job.status,
        file_source=job.file_source,
        file_source_type=job.file_source_type,
        title=job.title,
        description=job.description,
        tags=job.tags,
        category_id=job.category_id,
        privacy_status=job.privacy_status,
        youtube_video_id=job.youtube_video_id,
        youtube_url=job.youtube_url,
        error_message=job.error_message,
        # ``bool(...)`` because the wire contract is "was one asked for", not
        # the presigned source URL. ``*_applied`` passes through untouched so a
        # ``None`` stays None — an unmeasured outcome must not be flattened
        # into a claim that it failed, nor into a claim that it succeeded.
        thumbnail_requested=bool(job.thumbnail_source),
        thumbnail_applied=job.thumbnail_applied,
        caption_requested=bool(job.caption_source),
        caption_applied=job.caption_applied,
        created_at=job.created_at.isoformat() if job.created_at else None,
        started_at=job.started_at.isoformat() if job.started_at else None,
        completed_at=job.completed_at.isoformat() if job.completed_at else None,
    )


# ---------------------------------------------------------------------------
# POST /api/upload — start upload job
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=UploadJobResponse,
    operation_id="start_youtube_upload",
    summary="Create a YouTube upload job — returns immediately with a job_id",
)
async def start_upload(
    body: UploadRequest,
    tenant_id: str = Depends(get_tenant_id),
    owner_tenant_id: str = Depends(get_owner_tenant_id),
    upload_service: UploadService = Depends(get_upload_service),
) -> UploadJobResponse:
    """Create an upload job and return immediately with a job_id for polling.

    A render that is already on YouTube for this tenant is a **409**, not a
    second upload. This route is the only entry point to the upload path (there
    is no automatic retry anywhere in the service), so a repeat request is a
    human re-submitting — usually after reading a job the reaper marked
    ``failed``, which is not evidence that nothing was published. The 409 names
    the existing video so the operator can check the channel.
    """
    try:
        job = await upload_service.create_job(
            tenant_id=tenant_id,
            owner_tenant_id=owner_tenant_id,
            file_source=body.file_source,
            file_source_type=body.file_source_type,
            title=body.title,
            description=body.description,
            tags=body.tags,
            category_id=body.category_id,
            privacy_status=body.privacy_status,
            thumbnail_source=body.thumbnail_source,
            thumbnail_source_type=body.thumbnail_source_type,
            caption_source=body.caption_source,
            caption_source_type=body.caption_source_type,
            caption_language=body.caption_language,
        )
    except AlreadyPublishedError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "already_published",
                "message": str(exc),
                "job_id": exc.job_id,
                "youtube_video_id": exc.youtube_video_id,
                "youtube_url": exc.youtube_url,
            },
        ) from exc
    return _job_to_response(job)


# ---------------------------------------------------------------------------
# GET /api/upload/jobs/{job_id} — poll job status
# ---------------------------------------------------------------------------


@router.get(
    "/jobs/{job_id}",
    response_model=UploadJobResponse,
    operation_id="get_youtube_upload_job",
    summary="Poll the status of a specific YouTube upload job",
)
async def get_upload_job(
    job_id: str,
    tenant_id: str = Depends(get_tenant_id),
    owner_tenant_id: str = Depends(get_owner_tenant_id),
    upload_service: UploadService = Depends(get_upload_service),
) -> UploadJobResponse:
    """Poll the status of a specific upload job (scoped to the calling tenant)."""
    job = await upload_service.get_job(job_id, tenant_id, owner_tenant_id)
    if job is None:
        # Logged at WARNING, not INFO: a poller 404ing on a job it believes it
        # created is a real fault (wrong owner tenant, reaped row, lost write)
        # and the fleet runs LOG_LEVEL=WARNING, so anything lower is invisible.
        logger.warning(
            "Upload job %s not found for owner tenant %s (channel tenant %s) -- returning 404",
            job_id,
            owner_tenant_id,
            tenant_id,
        )
        raise HTTPException(status_code=404, detail=f"Upload job {job_id} not found")
    return _job_to_response(job)


# ---------------------------------------------------------------------------
# POST /api/upload/jobs/{job_id}/retract — set the published video private
# ---------------------------------------------------------------------------


@router.post(
    "/jobs/{job_id}/retract",
    response_model=UploadJobResponse,
    operation_id="retract_youtube_upload",
    summary="Retract a published upload: set its YouTube video to private (reversible)",
)
async def retract_upload_job(
    job_id: str,
    tenant_id: str = Depends(get_tenant_id),
    owner_tenant_id: str = Depends(get_owner_tenant_id),
    upload_service: UploadService = Depends(get_upload_service),
) -> UploadJobResponse:
    """Set the job's YouTube video to ``private``. Only ever reduces visibility.

    404: no such job for this owner. 409: the job never published a video.
    503: no channel credentials. 502: YouTube refused or no longer has the video.
    """
    try:
        job = await upload_service.retract_job(job_id, tenant_id, owner_tenant_id)
    except NotPublishedError as exc:
        raise HTTPException(
            status_code=409, detail={"error": "not_published", "message": str(exc)}
        ) from exc
    except RetractUnavailableError as exc:
        raise HTTPException(
            status_code=503, detail={"error": "no_channel_credentials", "message": str(exc)}
        ) from exc
    except VideoNotFoundError as exc:
        raise HTTPException(
            status_code=502, detail={"error": "video_not_found", "message": str(exc)}
        ) from exc
    except Exception as exc:  # googleapiclient HttpError, auth refresh failure, transport
        logger.exception("Retraction of upload job %s failed at YouTube", job_id)
        raise HTTPException(
            status_code=502,
            detail={"error": "youtube_retract_failed", "message": type(exc).__name__},
        ) from exc
    if job is None:
        raise HTTPException(status_code=404, detail=f"Upload job {job_id} not found")
    return _job_to_response(job)


# ---------------------------------------------------------------------------
# POST /api/upload/jobs/{job_id}/privacy — raise to unlisted/public (governed release)
# ---------------------------------------------------------------------------


@router.post(
    "/jobs/{job_id}/privacy",
    response_model=UploadJobResponse,
    operation_id="set_youtube_upload_privacy",
    summary="Release a private upload: set its YouTube video to unlisted or public",
)
async def set_upload_job_privacy(
    job_id: str,
    body: SetPrivacyRequest,
    tenant_id: str = Depends(get_tenant_id),
    owner_tenant_id: str = Depends(get_owner_tenant_id),
    upload_service: UploadService = Depends(get_upload_service),
) -> UploadJobResponse:
    """Raise the job's YouTube video to ``unlisted``/``public``. Never deletes, never lowers.

    Called by the governed ``marketing.publish_video`` release of a video uploaded private.
    404: no such job for this owner. 409: the job never published a video.
    503: no channel credentials. 502: YouTube refused or no longer has the video.
    """
    try:
        job = await upload_service.set_job_privacy(
            job_id, tenant_id, owner_tenant_id, body.privacy_status
        )
    except NotPublishedError as exc:
        raise HTTPException(
            status_code=409, detail={"error": "not_published", "message": str(exc)}
        ) from exc
    except RetractUnavailableError as exc:
        raise HTTPException(
            status_code=503, detail={"error": "no_channel_credentials", "message": str(exc)}
        ) from exc
    except VideoNotFoundError as exc:
        raise HTTPException(
            status_code=502, detail={"error": "video_not_found", "message": str(exc)}
        ) from exc
    except Exception as exc:  # googleapiclient HttpError, auth refresh failure, transport
        logger.exception("Privacy change of upload job %s failed at YouTube", job_id)
        raise HTTPException(
            status_code=502,
            detail={"error": "youtube_privacy_failed", "message": type(exc).__name__},
        ) from exc
    if job is None:
        raise HTTPException(status_code=404, detail=f"Upload job {job_id} not found")
    return _job_to_response(job)


# ---------------------------------------------------------------------------
# GET /api/upload/jobs — list jobs
# ---------------------------------------------------------------------------


@router.get(
    "/jobs",
    response_model=UploadJobListResponse,
    operation_id="list_youtube_upload_jobs",
    summary="List YouTube upload jobs for the authenticated tenant",
)
async def list_upload_jobs(
    tenant_id: str = Depends(get_tenant_id),
    owner_tenant_id: str = Depends(get_owner_tenant_id),
    upload_service: UploadService = Depends(get_upload_service),
    limit: int = Query(default=50, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
) -> UploadJobListResponse:
    """List upload jobs for the calling tenant (scoped to the real owner)."""
    jobs, total = await upload_service.list_jobs(
        tenant_id, owner_tenant_id, limit=limit, offset=offset
    )
    return UploadJobListResponse(
        jobs=[_job_to_response(j) for j in jobs],
        total=total,
    )
