"""Tests for health check endpoints."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError


@asynccontextmanager
async def _noop_lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """No-op lifespan that skips DB/Redis connection."""
    yield


@pytest.fixture
def client():
    """Create a test client using the real app with a no-op lifespan."""
    from src.config import clear_settings_cache

    clear_settings_cache()

    from src.asgi import app

    # Replace lifespan to avoid real DB/Redis connections
    app.router.lifespan_context = _noop_lifespan

    # Mock db_engine
    mock_engine = MagicMock()
    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock()
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_cm.__aexit__ = AsyncMock(return_value=None)
    mock_engine.connect = MagicMock(return_value=mock_cm)
    app.state.db_engine = mock_engine

    # Mock redis_client
    mock_redis = AsyncMock()
    mock_redis.ping = AsyncMock()
    app.state.redis_client = mock_redis

    # Mock task_manager and session_factory
    app.state.task_manager = MagicMock()
    app.state.session_factory = MagicMock()

    with TestClient(app) as c:
        yield c


class TestHealthEndpoints:
    """Test health check endpoints."""

    def test_health_check(self, client: TestClient) -> None:
        """GET /health returns healthy status."""
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert "database" in data["components"]
        assert "redis" in data["components"]

    def test_health_live(self, client: TestClient) -> None:
        """GET /health/live returns ok."""
        response = client.get("/health/live")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_health_ready(self, client: TestClient) -> None:
        """GET /health/ready checks database connectivity."""
        response = client.get("/health/ready")
        assert response.status_code == 200
        data = response.json()
        assert data["ready"] is True

    def test_health_ready_db_down(self, client: TestClient) -> None:
        """GET /health/ready returns 503 when database is down."""
        mock_engine = MagicMock()
        mock_engine.connect = MagicMock(
            side_effect=OperationalError(
                "SELECT 1", {}, ConnectionRefusedError("Connection refused")
            ),
        )
        app = client.app
        assert isinstance(app, FastAPI)
        app.state.db_engine = mock_engine

        response = client.get("/health/ready")
        assert response.status_code == 503
        data = response.json()
        assert data["ready"] is False

    def test_health_ready_engine_unset_is_503_not_500(self, client: TestClient) -> None:
        """An unstarted app (no ``db_engine`` on state) must read as NOT READY.

        Regression: the handler used to read ``request.app.state.db_engine``
        directly. When the lifespan never wired it, the ``AttributeError`` was
        not in ``_HEALTH_CHECK_ERRORS`` and escaped as a **500**, which is
        indistinguishable from a genuine server fault. It must be a 503.
        """
        app = client.app
        assert isinstance(app, FastAPI)
        if hasattr(app.state, "db_engine"):
            delattr(app.state, "db_engine")

        response = client.get("/health/ready")

        assert response.status_code == 503, (
            f"unset engine must be 503 not-ready, got {response.status_code}"
        )
        assert response.status_code != 500
        data = response.json()
        assert data["ready"] is False
        assert data["checks"]["database"]["status"] == "down"

    def test_readyz_alias_engine_unset_is_503(self, client: TestClient) -> None:
        """``/readyz`` delegates to the same handler, so it must agree."""
        app = client.app
        assert isinstance(app, FastAPI)
        if hasattr(app.state, "db_engine"):
            delattr(app.state, "db_engine")

        response = client.get("/readyz")

        assert response.status_code == 503
        assert response.json()["ready"] is False

    def test_health_check_engine_unset_reports_unhealthy_db(self, client: TestClient) -> None:
        """The combined probe must degrade the db component, not raise."""
        app = client.app
        assert isinstance(app, FastAPI)
        if hasattr(app.state, "db_engine"):
            delattr(app.state, "db_engine")

        response = client.get("/health")

        assert response.status_code != 500
        assert "unhealthy" in response.json()["components"]["database"]

    def test_root_endpoint(self, client: TestClient) -> None:
        """GET / returns service info."""
        response = client.get("/")
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "YouTube Upload Service"
        assert (
            data["description"]
            == "YouTube upload microservice: OAuth2 authorization and async video upload"
        )
        assert "endpoints" in data
        assert data["status"] == "running"
        assert "youtube_api_quota" in data["notes"]
