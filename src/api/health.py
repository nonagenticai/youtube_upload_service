"""Health check endpoints for youtube_upload_service."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])

# Exception types that health checks should gracefully handle.
# SQLAlchemyError covers OperationalError, InterfaceError, etc.
# OSError covers network-level failures (ConnectionRefusedError, etc.).
_HEALTH_CHECK_ERRORS = (SQLAlchemyError, OSError, ConnectionError, TimeoutError, RuntimeError)

# ``db_engine`` is wired onto app.state by the lifespan startup. If startup never
# ran -- or failed before that line -- the attribute is ABSENT, and a bare
# ``request.app.state.db_engine`` raises ``AttributeError``. That is NOT a member
# of ``_HEALTH_CHECK_ERRORS``, so it escaped to the ASGI layer as a **500**.
# A readiness probe must answer 503 ("not ready"), never 500: kubelet treats a
# non-2xx as not-ready either way, but a 500 is indistinguishable from a genuine
# server fault and hides an unstarted process. Resolve the engine defensively and
# short-circuit BEFORE attempting a query.
_DB_ENGINE_MISSING = "database engine not initialized"


@router.get(
    "/health",
    response_model=None,
    operation_id="health_check",
    summary="Combined health probe — database + redis + api status",
)
async def health_check(request: Request) -> dict[str, Any] | JSONResponse:
    """Combined health check for all components."""
    try:
        db_status = "healthy"
        engine = getattr(request.app.state, "db_engine", None)
        if engine is None:
            db_status = f"unhealthy: {_DB_ENGINE_MISSING}"
            logger.warning("Database health check failed: %s", _DB_ENGINE_MISSING)
        else:
            try:
                async with engine.connect() as conn:
                    await conn.execute(text("SELECT 1"))
            except _HEALTH_CHECK_ERRORS as e:
                db_status = f"unhealthy: {e}"
                logger.warning("Database health check failed: %s", e)

        redis_status = "healthy"
        try:
            redis_client = getattr(request.app.state, "redis_client", None)
            if redis_client:
                await redis_client.ping()
            else:
                redis_status = "not configured"
        except _HEALTH_CHECK_ERRORS as e:
            redis_status = f"unhealthy: {e}"
            logger.warning("Redis health check failed: %s", e)

        overall = "healthy" if db_status == "healthy" and redis_status == "healthy" else "degraded"

        return {
            "status": overall,
            "components": {
                "database": db_status,
                "redis": redis_status,
                "api": "healthy",
            },
        }
    except _HEALTH_CHECK_ERRORS as e:
        logger.error("Health check failed unexpectedly: %s", e)
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "error": str(e)},
        )


@router.get(
    "/health/live",
    response_model=None,
    operation_id="health_live",
    summary="Liveness probe — 200 if the process is up",
)
async def health_live() -> dict[str, str]:
    """Kubernetes liveness probe."""
    return {"status": "ok"}


@router.get("/healthz", operation_id="health.live")
async def healthz() -> dict[str, str]:
    """Modern K8s liveness probe path (alias of /health/live)."""
    return {"status": "ok"}


@router.get(
    "/health/ready",
    response_model=None,
    operation_id="health_ready",
    summary="Readiness probe — 200 once the database is reachable, 503 otherwise",
)
async def health_ready(request: Request) -> dict[str, Any] | JSONResponse:
    """Kubernetes readiness probe -- checks database connectivity.

    Returns 503 (never 500) when the engine is missing or unreachable; see the
    ``_DB_ENGINE_MISSING`` note above.
    """
    engine = getattr(request.app.state, "db_engine", None)
    if engine is None:
        logger.warning("Readiness check failed: %s", _DB_ENGINE_MISSING)
        return JSONResponse(
            status_code=503,
            content={
                "status": "unhealthy",
                "ready": False,
                "checks": {"database": {"status": "down", "error": _DB_ENGINE_MISSING}},
            },
        )
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {"status": "healthy", "ready": True, "checks": {"database": {"status": "up"}}}
    except _HEALTH_CHECK_ERRORS as e:
        logger.warning("Readiness check failed: %s", e)
        return JSONResponse(
            status_code=503,
            content={
                "status": "unhealthy",
                "ready": False,
                "checks": {"database": {"status": "down", "error": str(e)}},
            },
        )


@router.get("/readyz", response_model=None, operation_id="health.ready")
async def readyz(request: Request) -> dict[str, Any] | JSONResponse:
    """Modern K8s readiness probe path (alias of /health/ready)."""
    return await health_ready(request)
