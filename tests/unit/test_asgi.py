"""Tests for ASGI application: lifespan, middleware, error handling."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.asgi import HealthCheckLoggingFilter


@asynccontextmanager
async def _noop_lifespan(app: FastAPI) -> AsyncGenerator[None]:
    yield


def _make_test_client() -> TestClient:
    """Create a test client with mocked app state."""
    from src.config import clear_settings_cache

    clear_settings_cache()
    from src.asgi import app

    app.router.lifespan_context = _noop_lifespan

    mock_engine = MagicMock()
    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock()
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_cm.__aexit__ = AsyncMock(return_value=None)
    mock_engine.connect = MagicMock(return_value=mock_cm)

    app.state.db_engine = mock_engine
    app.state.redis_client = AsyncMock()
    app.state.task_manager = MagicMock()
    app.state.session_factory = MagicMock()
    app.state.oauth_service = MagicMock()

    return TestClient(app)


@pytest.mark.unit
class TestHealthCheckLoggingFilter:
    def test_suppresses_health_paths(self) -> None:
        f = HealthCheckLoggingFilter()
        for path in ["/health", "/health/ready", "/health/live"]:
            record = MagicMock()
            record.getMessage.return_value = f"GET {path} 200"
            assert f.filter(record) is False

    def test_allows_non_health_paths(self) -> None:
        f = HealthCheckLoggingFilter()
        record = MagicMock()
        record.getMessage.return_value = "GET /api/upload 200"
        assert f.filter(record) is True


@pytest.mark.unit
class TestRequestContextMiddleware:
    def test_adds_request_id_header(self) -> None:
        with _make_test_client() as client:
            response = client.get("/health/live")
            assert "x-request-id" in response.headers

    def test_preserves_incoming_request_id(self) -> None:
        with _make_test_client() as client:
            response = client.get("/health/live", headers={"x-request-id": "custom-rid-123"})
            assert response.headers["x-request-id"] == "custom-rid-123"


@pytest.mark.unit
class TestGlobalExceptionHandler:
    def test_returns_500_on_unhandled_exception(self) -> None:
        """Test the global exception handler using a minimal app (no middleware chain)."""
        from fastapi import Request
        from fastapi.responses import JSONResponse

        minimal_app = FastAPI(lifespan=_noop_lifespan)

        @minimal_app.exception_handler(Exception)
        async def handler(request: Request, exc: Exception) -> JSONResponse:
            return JSONResponse(
                status_code=500,
                content={
                    "detail": str(exc),
                    "type": type(exc).__name__,
                    "request_id": getattr(request.state, "request_id", "unknown"),
                },
            )

        @minimal_app.get("/test-500-error")
        async def broken_endpoint():
            raise RuntimeError("test explosion")

        with TestClient(minimal_app, raise_server_exceptions=False) as client:
            response = client.get("/test-500-error")
            assert response.status_code == 500
            data = response.json()
            assert data["type"] == "RuntimeError"
            assert "request_id" in data


@pytest.mark.unit
class TestLifespan:
    @pytest.mark.asyncio
    async def test_startup_and_shutdown_hooks(self) -> None:
        """Verify the create_app on_startup / on_shutdown hooks with mocks.

        The old single ``lifespan`` context manager is now two hooks passed to
        ``create_app`` (``_startup`` / ``_shutdown``); the factory wraps them in
        its own lifespan. Drive them directly against a real ``FastAPI`` app
        (a real app is required — the hooks are typed ``on_startup(app: FastAPI)``).
        """
        mock_engine = MagicMock()
        mock_engine.dispose = AsyncMock()

        mock_session_factory = MagicMock()

        mock_redis = AsyncMock()
        mock_redis.connect = AsyncMock()
        mock_redis.disconnect = AsyncMock()

        with (
            patch(
                "src.infra.database.create_engine_with_retry",
                new_callable=AsyncMock,
                return_value=mock_engine,
            ),
            patch(
                "src.infra.database.create_session_factory",
                return_value=mock_session_factory,
            ),
            patch(
                "w2v_common.cache.redis_client.RedisClient",
                return_value=mock_redis,
            ),
            # The platform-channel seed needs a real DB; it has its own
            # postgres-backed tests (test_youtube_tenant_config.py).
            patch(
                "src.services.youtube_config_service.YouTubeConfigService."
                "seed_platform_channel_from_env",
                new_callable=AsyncMock,
            ) as mock_seed,
        ):
            from src.asgi import _shutdown, _startup

            app = FastAPI()
            await _startup(app)
            assert hasattr(app.state, "db_engine")
            assert hasattr(app.state, "task_manager")
            mock_seed.assert_awaited_once()

            await _shutdown(app)
            mock_engine.dispose.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_shutdown_disposes_engine_even_when_redis_disconnect_raises_rediserror(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The sibling STARTUP handler correctly includes ``RedisError`` in its
        except tuple; shutdown did not. A ``RedisError`` closing Redis used to
        escape ``_shutdown`` uncaught, so the ``engine.dispose()`` call right
        after it never ran -- leaking the DB pool on every rough shutdown.

        Prove the actual consequence: use a fake engine whose ``dispose()``
        flips its own ``disposed`` flag (state genuinely owned by the fake,
        not a call recorder), and assert that flag is True after shutdown --
        i.e. the DB pool was actually released, not merely that a mock was
        invoked. Also assert the RedisError was logged rather than silently
        dropped, and that ``_shutdown`` itself did not raise.
        """
        from redis.exceptions import RedisError

        class _FakeEngine:
            def __init__(self) -> None:
                self.disposed = False

            async def dispose(self) -> None:
                self.disposed = True

        app = FastAPI()
        app.state.task_manager = None
        app.state.reaper = None

        mock_redis = AsyncMock()
        mock_redis.disconnect = AsyncMock(side_effect=RedisError("connection reset"))
        app.state.redis_client = mock_redis

        fake_engine = _FakeEngine()
        app.state.db_engine = fake_engine

        from src.asgi import _shutdown

        with caplog.at_level(logging.ERROR):
            await _shutdown(app)  # must not raise

        assert fake_engine.disposed is True, (
            "engine.dispose() must still run after redis.disconnect() raises RedisError"
        )
        assert any("redis" in record.getMessage().lower() for record in caplog.records), (
            "the RedisError must be logged, not silently swallowed"
        )


# ── The startup "Redis connected" line must never carry the Redis password ──
#
# REGRESSION: `_startup` logged `settings.redis.url` raw -- the full
# redis://[user]:<password>@host:port/db URL -- at INFO on the Redis connect
# path, so the live Redis password landed in this pod's stdout and in anything
# shipping those logs, on every single pod start.
#
# The test drives the REAL `_startup` rather than the redactor, because the
# defect was at the CALL SITE: a test that only exercises
# `w2v_common.observability.redis_endpoint` passes for the whole duration of the
# leak. That helper has its own unit tests in w2v_common; this one exists to
# fail if anyone hands the raw url back to the logger here.

# Wholly synthetic and shares no prefix with any real credential -- this string
# is committed, printed by pytest on failure, and ends up in CI logs, i.e.
# exactly the places this change exists to keep secrets out of.
_FAKE_PASSWORD = "not-a-real-password-xyzzy"  # pragma: allowlist secret
_FAKE_REDIS_URL = f"redis://yt:{_FAKE_PASSWORD}@redis.redis:6379/7"


@pytest.mark.unit
class TestStartupRedisLogRedaction:
    @pytest.mark.asyncio
    async def test_startup_log_does_not_leak_the_redis_password(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The emitted startup log record must not contain the Redis password."""
        import io

        import src.asgi as asgi_mod

        monkeypatch.setattr(asgi_mod.settings.redis, "url", _FAKE_REDIS_URL)

        # A handler attached directly to the module logger, NOT ``caplog``. This
        # service calls ``configure_logging()`` at import time, which re-points
        # the root handlers, so caplog's root handler can observe nothing and
        # every assertion below would pass vacuously.
        buffer = io.StringIO()
        handler = logging.StreamHandler(buffer)
        handler.setLevel(logging.INFO)
        asgi_mod.logger.addHandler(handler)
        # ``setLevel`` and not ``monkeypatch.setattr(logger, "level", ...)``:
        # ``logging.Logger`` caches ``isEnabledFor`` results and only
        # ``setLevel()`` clears that cache. A stale cache entry would drop the
        # record before it reached ``handler`` and the assertions below would
        # pass on empty output with the leak fully intact.
        original_level = asgi_mod.logger.level
        asgi_mod.logger.setLevel(logging.INFO)

        mock_engine = MagicMock()
        mock_engine.dispose = AsyncMock()
        mock_redis = AsyncMock()
        mock_redis.connect = AsyncMock()

        app = FastAPI()
        try:
            with (
                patch(
                    "src.infra.database.create_engine_with_retry",
                    new_callable=AsyncMock,
                    return_value=mock_engine,
                ),
                patch(
                    "src.infra.database.create_session_factory",
                    return_value=MagicMock(),
                ),
                patch(
                    "w2v_common.cache.redis_client.RedisClient",
                    return_value=mock_redis,
                ),
                patch(
                    "src.services.youtube_config_service.YouTubeConfigService."
                    "seed_platform_channel_from_env",
                    new_callable=AsyncMock,
                ),
            ):
                await asgi_mod._startup(app)
        finally:
            asgi_mod.logger.setLevel(original_level)
            asgi_mod.logger.removeHandler(handler)
            await asgi_mod._shutdown(app)

        captured = buffer.getvalue()
        # Guard against a vacuous pass: if the line never emitted, the
        # assertions below would hold for the wrong reason.
        assert "Redis connected" in captured, (
            "the 'Redis connected' line never emitted; the assertions would be vacuous"
        )
        assert _FAKE_PASSWORD not in captured, "Redis password leaked into the startup log"
        assert "yt:" not in captured, "Redis userinfo leaked into the startup log"
        # Still useful, or the next person deletes the log line instead of keeping it.
        assert "redis.redis:6379" in captured, (
            "the redacted line must still say which Redis this pod attached to"
        )
