"""Shared test fixtures for youtube_upload_service tests."""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from w2v_common.cache.redis_client import RedisClient
from w2v_common.testing import inject_fakeredis

# Set test environment before importing anything from src
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("AUTH_ENABLED", "false")
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://postgres:changeme@localhost:5455/youtube_upload_service_test",  # pragma: allowlist secret
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6395/1")
# Required field on StorageSettings (no localhost fallback in w2v_common 0.15.0+).
os.environ.setdefault("STORAGE_MINIO_ENDPOINT", "localhost:9000")
os.environ.setdefault("YOUTUBE_CLIENT_ID", "test-client-id")
os.environ.setdefault("YOUTUBE_CLIENT_SECRET", "test-client-secret")
os.environ.setdefault("ENCRYPTION_KEY", "")

# Capture the test DB URL BEFORE the autouse _clear_settings fixture
# monkeypatches DATABASE_URL to its in-test default. CI sets DATABASE_URL
# to its youtube_upload_service_ci postgres; locally falls back to the
# default above (developer must run a local postgres on :5455 to execute
# db_engine tests).
_TEST_DATABASE_URL = os.environ["DATABASE_URL"]


def pytest_configure(config: object) -> None:
    """Register custom markers so ``--strict-markers`` does not complain."""
    if not isinstance(config, pytest.Config):
        return

    config.addinivalue_line("markers", "unit: unit tests (fast, no external deps)")
    config.addinivalue_line("markers", "integration: integration tests (may need services)")


# ---------------------------------------------------------------------------
# Settings cache management
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_settings(monkeypatch):
    """Remove CI env vars that interfere with config-default tests."""
    for key in list(os.environ):
        if any(
            key.startswith(p)
            for p in (
                "DATABASE_",
                "AUTH_",
                "REDIS_",
                "API_",
                "STORAGE_",
                "LOG_",
                "CACHE_",
                "TRACING_",
                "LLM_",
                "FFMPEG_",
                "YOUTUBE_",
                "ENCRYPTION_",
            )
        ):
            monkeypatch.delenv(key, raising=False)
    # Restore test-only env vars that were cleared above
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg://postgres:changeme@localhost:5455/youtube_upload_service_test",  # pragma: allowlist secret
    )
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6395/1")
    monkeypatch.setenv("STORAGE_MINIO_ENDPOINT", "localhost:9000")
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setenv("YOUTUBE_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("YOUTUBE_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("YOUTUBE_REDIRECT_URI", "http://localhost:6315/api/auth/callback")
    monkeypatch.setenv("ENCRYPTION_KEY", "")


@pytest.fixture
def mock_settings():
    """Return a Settings instance for testing."""
    from src.config import Settings

    return Settings(
        environment="test",
        app_name="YouTube Upload Service Test",
    )


# ---------------------------------------------------------------------------
# Real database fixtures (postgres+psycopg, TRUNCATE-based isolation)
# ---------------------------------------------------------------------------


@pytest.fixture
async def db_engine():
    """Per-test async postgres engine with TRUNCATE-based isolation.

    Connects to _TEST_DATABASE_URL captured at module load (CI postgres in
    CI, localhost:5455 locally). Runs Base.metadata.create_all (idempotent)
    then TRUNCATEs every model table so each test starts from a clean slate.
    """
    from src.database.models import Base

    engine = create_async_engine(_TEST_DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if Base.metadata.sorted_tables:
            tables = ", ".join(f'"{t.name}"' for t in reversed(Base.metadata.sorted_tables))
            await conn.execute(text(f"TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE"))
    yield engine
    await engine.dispose()


@pytest.fixture
def db_session_factory(db_engine) -> async_sessionmaker[AsyncSession]:
    """Session factory bound to the postgres engine."""
    return async_sessionmaker(
        db_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autocommit=False,
        autoflush=False,
    )


@pytest.fixture
async def db_session(db_session_factory) -> AsyncGenerator[AsyncSession]:
    """A single async session for direct DB assertions in tests."""
    async with db_session_factory() as session:
        yield session


# ---------------------------------------------------------------------------
# Real Redis fixtures (fakeredis)
# ---------------------------------------------------------------------------


@pytest.fixture
def real_redis_client():
    """RedisClient backed by fakeredis (real commands, in-memory)."""
    client = RedisClient(redis_url="redis://localhost:6379/0")
    inject_fakeredis(client)
    return client
