"""Tests for infra/database.py — engine creation and session factory."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.infra.database import (
    _ensure_database_exists,
    create_engine_with_retry,
    create_session_factory,
)


@pytest.mark.unit
class TestCreateSessionFactory:
    def test_returns_async_sessionmaker(self) -> None:
        mock_engine = MagicMock()
        factory = create_session_factory(mock_engine)
        assert isinstance(factory, async_sessionmaker)


@pytest.mark.unit
class TestEnsureDatabaseExists:
    @pytest.mark.asyncio
    async def test_invalid_db_name_rejected(self) -> None:
        with pytest.raises(ValueError, match="invalid characters"):
            await _ensure_database_exists("postgresql://user:pass@localhost:5432/bad;drop table")

    @pytest.mark.asyncio
    async def test_db_already_exists(self) -> None:
        mock_engine = MagicMock()
        mock_conn = MagicMock()
        mock_result = MagicMock()
        mock_result.scalar.return_value = 1  # DB exists
        mock_conn.execute.return_value = mock_result
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_engine.connect.return_value = mock_conn
        mock_engine.dispose = MagicMock()

        with patch("src.infra.database.create_engine", return_value=mock_engine):
            await _ensure_database_exists(
                "postgresql://user:pass@localhost:5432/testdb", max_retries=1
            )

        # Should have checked for DB existence
        mock_conn.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_connection_failure_retries_and_raises(self) -> None:
        with (
            patch(
                "src.infra.database.create_engine",
                side_effect=OperationalError("conn", {}, Exception("refused")),
            ),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            with pytest.raises(ConnectionError, match="Failed to connect"):
                await _ensure_database_exists(
                    "postgresql://user:pass@localhost:5432/testdb", max_retries=1
                )

    @pytest.mark.asyncio
    async def test_unexpected_connection_error_fails_loud_not_retried(self) -> None:
        """A genuine ``ConnectionError``/``TimeoutError`` must escape immediately.

        Both are ``OSError`` subclasses. If the broad ``except (..., OSError,
        ...)`` clause is listed before the specific ``except (ConnectionError,
        TimeoutError, RuntimeError)`` clause, the broad clause traps them first
        and the intended "fail loud" branch is dead code — the call silently
        retries ``max_retries`` times instead of raising on the first attempt.
        """
        create_engine_calls = 0

        def _raise_connection_error(*args, **kwargs):
            nonlocal create_engine_calls
            create_engine_calls += 1
            raise ConnectionError("connection reset by peer")

        with (
            patch("src.infra.database.create_engine", side_effect=_raise_connection_error),
            patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            with pytest.raises(ConnectionError, match="connection reset by peer"):
                await _ensure_database_exists(
                    "postgresql://user:pass@localhost:5432/testdb", max_retries=3
                )

        # Escaped on the FIRST attempt: no retry sleep, create_engine called once.
        assert create_engine_calls == 1
        mock_sleep.assert_not_awaited()


@pytest.mark.unit
class TestCreateEngineWithRetry:
    @pytest.mark.asyncio
    async def test_success_on_first_attempt(self) -> None:
        mock_engine = MagicMock()
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()

        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_engine.begin = MagicMock(return_value=mock_cm)

        with patch("src.infra.database.create_async_engine", return_value=mock_engine):
            engine = await create_engine_with_retry(
                "postgresql+psycopg://localhost/testdb", max_retries=1
            )
        assert engine is mock_engine

    @pytest.mark.asyncio
    async def test_retries_on_connection_error(self) -> None:
        mock_engine = MagicMock()

        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise OSError("Connection refused")
            return mock_engine

        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_engine.begin = MagicMock(return_value=mock_cm)

        with (
            patch("src.infra.database.create_async_engine", side_effect=side_effect),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            engine = await create_engine_with_retry(
                "postgresql+psycopg://localhost/testdb", max_retries=3
            )
        assert engine is mock_engine

    @pytest.mark.asyncio
    async def test_raises_after_all_retries_exhausted(self) -> None:
        with (
            patch(
                "src.infra.database.create_async_engine",
                side_effect=TimeoutError("timeout"),
            ),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            with pytest.raises(ConnectionError, match="Failed to connect"):
                await create_engine_with_retry(
                    "postgresql+psycopg://localhost/testdb", max_retries=2
                )

    @pytest.mark.asyncio
    async def test_creates_db_when_not_exists(self) -> None:
        """When the error says 'database does not exist', try to create it."""
        mock_engine = MagicMock()
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_engine.begin = MagicMock(return_value=mock_cm)

        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise OperationalError(
                    "connection failed",
                    {},
                    Exception('database "testdb" does not exist'),
                )
            return mock_engine

        with (
            patch("src.infra.database.create_async_engine", side_effect=side_effect),
            patch("src.infra.database._ensure_database_exists", new_callable=AsyncMock),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            engine = await create_engine_with_retry(
                "postgresql+psycopg://localhost/testdb", max_retries=3
            )
        assert engine is mock_engine
