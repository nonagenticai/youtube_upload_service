"""Database engine creation and session factory."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from urllib.parse import urlparse, urlunparse

from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import AsyncAdaptedQueuePool, NullPool

logger = logging.getLogger(__name__)


def _create_database_if_missing(sync_admin_url: str, db_name: str) -> None:
    """Connect to the admin DB and CREATE *db_name* if it does not yet exist.

    Returns silently on success or if the database already exists. Raises any
    other :class:`ProgrammingError` to the caller.
    """
    engine = create_engine(sync_admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        with engine.connect() as conn:
            result = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :db_name"),
                {"db_name": db_name},
            )
            if result.scalar() is not None:
                logger.info("Database '%s' already exists", db_name)
                return
            try:
                conn.execute(text(f"CREATE DATABASE {db_name}"))
                logger.info("Database '%s' created successfully", db_name)
            except ProgrammingError as e:
                if "already exists" in str(e).lower():
                    return
                raise
    finally:
        try:
            engine.dispose()
        except (OSError, ConnectionError, TimeoutError, RuntimeError) as exc:
            logger.warning("Failed to dispose engine during cleanup: %s", exc)


async def _ensure_database_exists(
    database_url: str,
    max_retries: int = 3,
) -> None:
    """Ensure the target database exists, creating it if necessary."""
    parsed = urlparse(database_url)
    db_name = parsed.path.lstrip("/") if parsed.path else "postgres"
    db_host = parsed.hostname or "localhost"
    db_port = parsed.port or 5432
    admin_db = os.getenv("POSTGRES_ADMIN_DB", "postgres")

    if not re.fullmatch(r"[A-Za-z0-9_]+", db_name):
        raise ValueError(
            f"Database name '{db_name}' contains invalid characters; "
            f"only alphanumeric and underscore are supported."
        )

    admin_parts = list(urlparse(database_url))
    admin_parts[2] = f"/{admin_db}"
    sync_admin_url = urlunparse(admin_parts)

    logger.info(
        "Ensuring PostgreSQL database '%s' exists at %s:%s",
        db_name,
        db_host,
        db_port,
    )

    # ⚠️ ORDER MATTERS. ConnectionError and TimeoutError are BOTH OSError
    # subclasses, so a leading broad `OSError` handler makes the loud branch
    # below unreachable for either — a genuine unexpected connection error
    # would be silently retried instead of failing loudly. ConnectionRefusedError
    # is itself a ConnectionError subclass and is listed first so the ordinary
    # "nothing is listening yet" cold-start case still retries quietly.
    # Specific/loud first, broad/quiet-retry last (matches w2v_common's
    # `_try_ensure_database_once`).
    last_error: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            _create_database_if_missing(sync_admin_url, db_name)
            return
        except (OperationalError, ConnectionRefusedError) as e:
            last_error = e
            if attempt < max_retries:
                await asyncio.sleep(attempt * 2)
        except (ConnectionError, TimeoutError, RuntimeError) as e:
            logger.error("Unexpected error ensuring database exists: %s", e, exc_info=True)
            raise
        except OSError as e:
            last_error = e
            if attempt < max_retries:
                await asyncio.sleep(attempt * 2)

    raise ConnectionError(
        f"Failed to connect to admin database at {db_host}:{db_port} "
        f"after {max_retries} attempts. Last error: {last_error}"
    )


async def create_engine_with_retry(
    database_url: str,
    *,
    pool_size: int = 2,
    max_overflow: int = 8,
    pool_timeout: int = 30,
    pool_recycle: int = 3600,
    echo: bool = False,
    max_retries: int = 5,
    retry_delay: float = 2.0,
) -> AsyncEngine:
    """Create database engine with connection retry logic."""
    last_error: Exception | None = None
    current_delay = retry_delay

    pool_config = {
        "poolclass": AsyncAdaptedQueuePool,
        "pool_size": pool_size,
        "max_overflow": max_overflow,
        "pool_timeout": pool_timeout,
        "pool_recycle": pool_recycle,
        "pool_pre_ping": True,
        "echo": echo,
    }

    for attempt in range(1, max_retries + 1):
        try:
            engine = create_async_engine(database_url, **pool_config)
            async with engine.begin() as conn:
                await conn.execute(text("SELECT 1"))
            logger.info("Database engine created on attempt %d", attempt)
            return engine

        except (OperationalError, ProgrammingError) as e:
            error_msg = str(e).lower()
            if "database" in error_msg and "does not exist" in error_msg:
                try:
                    await _ensure_database_exists(database_url)
                    engine = create_async_engine(database_url, **pool_config)
                    async with engine.begin() as conn:
                        await conn.execute(text("SELECT 1"))
                    return engine
                except (OSError, ConnectionError, RuntimeError) as create_err:
                    last_error = create_err
            else:
                last_error = e
            if attempt < max_retries:
                await asyncio.sleep(current_delay)
                current_delay *= 2

        except (TimeoutError, OSError, ConnectionRefusedError) as e:
            last_error = e
            if attempt < max_retries:
                await asyncio.sleep(current_delay)
                current_delay *= 2

        except (ConnectionError, RuntimeError) as e:
            last_error = e
            if attempt < max_retries:
                await asyncio.sleep(current_delay)
                current_delay *= 2

    parsed = urlparse(database_url)
    raise ConnectionError(
        f"Failed to connect to PostgreSQL at {parsed.hostname}:{parsed.port}"
        f"/{parsed.path.lstrip('/')} after {max_retries} attempts. Last error: {last_error}"
    ) from last_error


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create a session factory bound to *engine*."""
    return async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autocommit=False,
        autoflush=False,
    )
