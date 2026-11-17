"""ASGI application for youtube_upload_service.

YouTube upload microservice: OAuth2 authorization and async video upload.

Built on the shared ``w2v_common.asgi.create_app`` factory: the canonical
middleware stack (CORS + SecurityHeaders + Auth + RequestId + error envelope),
the BearerAuth/TenantIdHeader OpenAPI customization, and the ``<tag>_<name>``
operation-id fallback all come from the factory. The only service-specific
wiring left here is the lifespan (DB/Redis/task-manager/OAuth/upload services),
the health-check access-log filter, the root banner, and the OAuth-callback
public-path declaration (``auth_public_endpoints``).
"""

import logging
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI
from w2v_common.asgi import create_app
from w2v_common.observability import redis_endpoint

from src.config import get_settings
from src.logging_config import configure_logging

load_dotenv()

settings = get_settings()
configure_logging(json_logs=settings.logging.json_logs, level=settings.logging.level)
logger = logging.getLogger(__name__)


# ============================================================================
# Health Check Logging Filter (not provided by create_app)
# ============================================================================


class HealthCheckLoggingFilter(logging.Filter):
    """Suppress noisy health-check requests from uvicorn access logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Return False for health-check paths to suppress their log lines."""
        message = record.getMessage()
        return not any(
            path in message
            for path in ["/health", "/health/ready", "/health/live", "/healthz", "/readyz"]
        )


uvicorn_access_logger = logging.getLogger("uvicorn.access")
uvicorn_access_logger.addFilter(HealthCheckLoggingFilter())


# ============================================================================
# Lifespan hooks — passed to create_app as on_startup / on_shutdown
# ============================================================================


async def _startup(app: FastAPI) -> None:
    """Initialise DB, Redis, task manager, and the OAuth/upload services.

    All resources are stashed on ``app.state`` so the health router and
    request handlers can read them back; ``_shutdown`` tears them down.
    """
    logger.info("=" * 70)
    logger.info("Starting YouTube Upload Service v%s", settings.app_version)
    logger.info("=" * 70)

    # ---- Database ----
    from src.infra.database import create_engine_with_retry, create_session_factory

    engine = await create_engine_with_retry(
        settings.database.url,
        pool_size=settings.database.pool_size,
        max_overflow=settings.database.max_overflow,
        pool_timeout=settings.database.pool_timeout,
        pool_recycle=settings.database.pool_recycle,
        echo=settings.database.echo,
    )
    app.state.db_engine = engine
    app.state.session_factory = create_session_factory(engine)

    # Schema is owned by alembic -- the CI/CD pipeline runs
    # `alembic upgrade head` in an initContainer before this app starts
    # (see .github/workflows/ci-cd.yaml). Do NOT call
    # ``Base.metadata.create_all`` here: it bypasses migration history
    # and can drift the live schema away from what alembic expects.
    logger.info("Database connected (schema managed by alembic)")

    # ---- Redis ----
    from redis.exceptions import RedisError

    redis_client = None
    try:
        from w2v_common.cache.redis_client import RedisClient

        redis_client = RedisClient(
            redis_url=settings.redis.url,
            max_connections=settings.redis.max_connections,
            socket_timeout=settings.redis.socket_timeout,
            socket_connect_timeout=settings.redis.socket_connect_timeout,
            decode_responses=settings.redis.decode_responses,
        )
        await redis_client.connect()
        app.state.redis_client = redis_client
        logger.info("Redis connected: %s", redis_endpoint(settings.redis.url))
    except (ImportError, OSError, RuntimeError, ValueError, RedisError) as e:
        logger.warning("Redis unavailable: %s -- continuing without caching", e)
        app.state.redis_client = None

    # ---- Task Manager ----
    from src.infra.task_manager import TaskManager

    task_manager = TaskManager(max_concurrent=settings.upload.max_concurrent)
    app.state.task_manager = task_manager
    logger.info("Task manager initialized (max_concurrent=%d)", settings.upload.max_concurrent)

    # ---- OAuth Service ----
    from src.services.encryption_service import EncryptionService
    from src.services.oauth_service import OAuthService

    encryption_service = EncryptionService(key=settings.encryption.key)
    oauth_service = OAuthService(
        session_factory=app.state.session_factory,
        encryption=encryption_service,
        settings=settings,
    )
    app.state.encryption_service = encryption_service
    app.state.oauth_service = oauth_service
    logger.info("OAuth service initialized")

    # ---- Platform channel seed ----
    # One-time copy of YOUTUBE_FALLBACK_* into youtube_platform_channel (no-op once
    # the row exists; race-safe across replicas). Fails loud: a missing table or
    # grant is a deploy defect, not something to boot past.
    await oauth_service.config.seed_platform_channel_from_env()

    # ---- YouTube + Upload Service ----
    # One pod id shared by the upload worker (stamps lease_owner) and the reaper
    # (expires this pod's leases at shutdown) so the release predicate matches.
    import socket

    from src.services.upload_service import UploadService
    from src.services.youtube_service import YouTubeService

    pod_id = socket.gethostname()[:64]

    youtube_service = YouTubeService(settings=settings)
    upload_service = UploadService(
        session_factory=app.state.session_factory,
        oauth_service=oauth_service,
        youtube_service=youtube_service,
        task_manager=task_manager,
        settings=settings,
        pod_id=pod_id,
    )
    app.state.youtube_service = youtube_service
    app.state.upload_service = upload_service
    logger.info("Upload service initialized")

    # ---- Scale-in reaper ----
    # Reclaims uploads a terminated pod stranded non-terminal (fails them as
    # retryable). One immediate sweep on startup catches a dead predecessor's
    # strands; then it sweeps on an interval.
    from src.infra.reaper import UploadJobReaper

    reaper = UploadJobReaper(app.state.session_factory, settings, pod_id=pod_id)
    await reaper.start()
    app.state.reaper = reaper
    logger.info("Upload-job reaper initialized")

    logger.info("=" * 70)
    logger.info("YouTube Upload Service started successfully")
    logger.info(
        "Environment: %s | API: %s:%d", settings.environment, settings.api.host, settings.api.port
    )
    logger.info("=" * 70)


async def _shutdown(app: FastAPI) -> None:
    """Graceful teardown of the task manager, Redis, and DB engine."""
    logger.info("Shutting down YouTube Upload Service")

    # Stop accepting/renewing first: drain in-flight uploads, then the reaper
    # expires this pod's remaining leases so a successor reclaims stragglers
    # immediately instead of waiting out the lease TTL.
    #
    # Every resource below gets its own try/except: a failure tearing down one
    # (e.g. Redis) must never skip the ones after it (e.g. engine.dispose()) —
    # each teardown step is independent, not a chain that aborts on the first
    # exception.
    task_manager = getattr(app.state, "task_manager", None)
    if task_manager is not None:
        try:
            await task_manager.shutdown(timeout=30.0)
        except (OSError, RuntimeError, ValueError) as e:
            logger.error("Error shutting down task manager: %s", e)

    reaper = getattr(app.state, "reaper", None)
    if reaper is not None:
        try:
            await reaper.stop()
        except (OSError, RuntimeError, ValueError) as e:
            logger.error("Error stopping reaper: %s", e)

    redis_client = getattr(app.state, "redis_client", None)
    if redis_client is not None:
        from redis.exceptions import RedisError

        try:
            await redis_client.disconnect()
        except (ImportError, OSError, RuntimeError, ValueError, RedisError) as e:
            logger.error("Error closing Redis: %s", e)

    engine = getattr(app.state, "db_engine", None)
    if engine is not None:
        try:
            await engine.dispose()
        except (OSError, RuntimeError, ValueError) as e:
            logger.error("Error disposing DB engine: %s", e)
    logger.info("YouTube Upload Service shutdown complete")


# ============================================================================
# Create App — shared factory
# ============================================================================

# Mount routers
# why: routers imported after settings/logging are configured
from src.api.auth_routes import public_router as auth_public_router
from src.api.auth_routes import router as auth_router
from src.api.health import router as health_router
from src.api.settings_routes import admin_router as settings_admin_router
from src.api.settings_routes import router as settings_router
from src.api.upload_routes import router as upload_router

app = create_app(
    settings,
    # ``auth_public_router`` carries GET /api/auth/callback ONLY. It is a separate
    # router because it must stay OUTSIDE the entitlement gate mounted on
    # ``auth_router``/``upload_router``: Google redirects a browser here with no
    # licence token and no tenant identity (see src/api/auth_routes.py).
    routers=[
        health_router,
        auth_router,
        auth_public_router,
        upload_router,
        settings_router,
        settings_admin_router,
    ],
    title=settings.app_name,
    description="YouTube upload microservice: OAuth2 authorization and async video upload",
    version=settings.app_version,
    on_startup=[_startup],
    on_shutdown=[_shutdown],
    # GET /api/auth/callback is Google's OAuth redirect target — it carries no
    # license token (the tenant is recovered from the one-time OAuth `state`
    # stored in Redis), so it must bypass AuthMiddleware. Declared as config
    # instead of the old AuthMiddleware subclass; the factory threads it to
    # both enforcement and the advertised OpenAPI schema.
    auth_public_endpoints={"/api/auth/callback"},
    redirect_slashes=False,
    swagger_ui_parameters={"persistAuthorization": True},
    openapi_tags=[
        {"name": "health", "description": "Liveness, readiness, and basic service-status probes."},
        {
            "name": "auth",
            "description": (
                "OAuth2 authorization for the YouTube Data API — connect, "
                "callback, status, disconnect."
            ),
        },
        {
            "name": "upload",
            "description": "Async YouTube video upload jobs — create, poll, list.",
        },
        {
            "name": "settings",
            "description": "Per-tenant YouTube settings (own OAuth client; no secrets returned).",
        },
        {
            "name": "admin",
            "description": "Operator: platform YouTube channel and per-tenant permission.",
        },
    ],
)


@app.get(
    "/",
    operation_id="service_root",
    tags=["health"],
    summary="Service banner + endpoint index",
)
async def root() -> dict[str, Any]:
    """Return service info and available endpoint listing."""
    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "description": "YouTube upload microservice: OAuth2 authorization and async video upload",
        "endpoints": {
            "health": "/health",
            "docs": "/docs",
            "auth_connect": "/api/auth/connect",
            "auth_callback": "/api/auth/callback",
            "auth_status": "/api/auth/status",
            "auth_disconnect": "/api/auth/disconnect",
            "upload": "/api/upload",
            "upload_jobs": "/api/upload/jobs",
            "upload_job": "/api/upload/jobs/{job_id}",
            "settings": "/api/settings",
            "admin_platform_channel": "/api/admin/platform-channel",
        },
        "status": "running",
        "environment": settings.environment,
        "notes": {
            "youtube_api_quota": "videos.insert costs 1600 units out of 10,000 daily (~6 uploads/day per project)",
        },
    }


# ============================================================================
# Main Entry Point
# ============================================================================


def main() -> None:
    """Main entry point for running the ASGI application."""
    import uvicorn

    uvicorn.run(
        "src.asgi:app",
        host=settings.api.host,
        port=settings.api.port,
        log_level=settings.logging.level.lower(),
        reload=settings.api.reload,
    )


if __name__ == "__main__":
    main()
