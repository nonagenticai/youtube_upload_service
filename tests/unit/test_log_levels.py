"""Level-pinning tests for failure paths that must be visible at LOG_LEVEL=WARNING.

The fleet runs ``LOG_LEVEL=WARNING`` in production, so anything a failure path
logs at INFO/DEBUG is *invisible* — a 484-count 404 storm on this service
produced zero log lines for two months. Each test below asserts the exact
``levelno``, not merely "something was logged": a test that only checks the
message passes just as happily at INFO and would not have caught the defect.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.api.auth_routes import auth_callback
from src.api.dependencies import get_owner_tenant_id, get_tenant_id, get_upload_service
from src.api.upload_routes import router as upload_router
from src.infra.reaper import UploadJobReaper
from src.infra.task_manager import TaskManager
from tests.orchestrator_sa import allow_orchestrator_sa, sa_introspection, stamp_license_lane
from tests.tenants import tenant_uuid

OWNER_TENANT = tenant_uuid("owner-tenant-7")


def _records(caplog, logger_name: str, needle: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == logger_name and needle in r.getMessage()]


# ---------------------------------------------------------------------------
# src/infra/task_manager.py
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTaskManagerLogLevels:
    @pytest.mark.asyncio
    async def test_skipped_job_on_shutdown_logs_at_warning(self, caplog) -> None:
        """A job dropped by shutdown is never retried — it must not be INFO."""
        tm = TaskManager(max_concurrent=2)
        await tm.shutdown(timeout=0.1)

        async def job() -> None:  # pragma: no cover - must never run
            raise AssertionError("job should not run after shutdown")

        coro = job()
        try:
            with caplog.at_level(logging.DEBUG, logger="src.infra.task_manager"):
                await tm.submit("job-dropped", coro)
                await asyncio.sleep(0.05)
        finally:
            # The manager returns without awaiting a post-shutdown coroutine;
            # close it explicitly to avoid an "unawaited coroutine" warning.
            coro.close()

        matches = _records(caplog, "src.infra.task_manager", "Skipping job job-dropped")
        assert matches, "the dropped job produced no log line at all"
        assert matches[0].levelno == logging.WARNING

    @pytest.mark.asyncio
    async def test_shutdown_with_cancelled_stragglers_logs_at_warning(self, caplog) -> None:
        """Cancelled stragglers are uploads dropped mid-flight."""
        tm = TaskManager(max_concurrent=2)
        started = asyncio.Event()

        async def forever() -> None:
            started.set()
            await asyncio.Event().wait()

        await tm.submit("j1", forever())
        await started.wait()

        with caplog.at_level(logging.DEBUG, logger="src.infra.task_manager"):
            await tm.shutdown(timeout=0.1)

        matches = _records(caplog, "src.infra.task_manager", "Shutdown complete")
        assert matches, "shutdown produced no completion line"
        assert "1 cancelled" in matches[0].getMessage()
        assert matches[0].levelno == logging.WARNING

    @pytest.mark.asyncio
    async def test_clean_shutdown_stays_at_info(self, caplog) -> None:
        """No stragglers => routine event, must NOT be promoted to WARNING."""
        tm = TaskManager(max_concurrent=2)
        with caplog.at_level(logging.DEBUG, logger="src.infra.task_manager"):
            await tm.shutdown(timeout=0.1)
        assert not _records(caplog, "src.infra.task_manager", "Shutdown complete")


# ---------------------------------------------------------------------------
# src/infra/reaper.py
# ---------------------------------------------------------------------------


def _session_factory_returning(scalar_value: int) -> MagicMock:
    session = AsyncMock()
    result = MagicMock()
    result.scalar.return_value = scalar_value
    session.execute.return_value = result
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=ctx)


@pytest.mark.unit
class TestReaperLogLevels:
    @pytest.mark.asyncio
    async def test_released_inflight_leases_log_at_warning(self, caplog) -> None:
        """Released leases == uploads abandoned mid-flight by this pod."""
        settings = MagicMock()
        reaper = UploadJobReaper(
            _session_factory_returning(2), settings, pod_id="youtube-upload-abc"
        )
        with caplog.at_level(logging.DEBUG, logger="src.infra.reaper"):
            assert await reaper.release_pod_leases() == 2

        matches = _records(caplog, "src.infra.reaper", "Released 2 in-flight lease(s)")
        assert matches, "abandoned leases produced no log line"
        assert matches[0].levelno == logging.WARNING

    @pytest.mark.asyncio
    async def test_no_released_leases_logs_nothing(self, caplog) -> None:
        reaper = UploadJobReaper(_session_factory_returning(0), MagicMock(), pod_id="p")
        with caplog.at_level(logging.DEBUG, logger="src.infra.reaper"):
            assert await reaper.release_pod_leases() == 0
        assert not _records(caplog, "src.infra.reaper", "in-flight lease")


# ---------------------------------------------------------------------------
# src/api/upload_routes.py  — the 404 storm site
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestUploadJob404LogLevel:
    def test_job_not_found_logs_at_warning(self, caplog, monkeypatch) -> None:
        allow_orchestrator_sa(monkeypatch)
        app = FastAPI()
        stamp_license_lane(app)
        app.include_router(upload_router)
        svc = AsyncMock()
        svc.get_job.return_value = None
        app.dependency_overrides[get_tenant_id] = lambda: tenant_uuid("channel-tenant")
        app.dependency_overrides[get_upload_service] = lambda: svc
        client = TestClient(app, headers={"X-Owner-Tenant-Id": OWNER_TENANT})

        with caplog.at_level(logging.DEBUG, logger="src.api.upload_routes"):
            response = client.get("/api/upload/jobs/job-missing")

        assert response.status_code == 404
        matches = _records(caplog, "src.api.upload_routes", "Upload job job-missing not found")
        assert matches, "the 404 produced no log line at all (the two-month blind spot)"
        assert matches[0].levelno == logging.WARNING
        assert OWNER_TENANT in matches[0].getMessage()


# ---------------------------------------------------------------------------
# src/api/dependencies.py — fail-closed owner-tenant denial
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOwnerTenantDenialLogLevel:
    def test_missing_owner_tenant_header_logs_at_warning(self, caplog) -> None:
        request = MagicMock()
        request.headers = {}
        request.method = "GET"
        request.url.path = "/api/upload/jobs"

        with caplog.at_level(logging.DEBUG, logger="src.api.dependencies"):
            with pytest.raises(HTTPException) as exc:
                get_owner_tenant_id(request)

        assert exc.value.status_code == 400
        matches = _records(caplog, "src.api.dependencies", "X-Owner-Tenant-Id is absent")
        assert matches, "the fail-closed 400 produced no log line"
        assert matches[0].levelno == logging.WARNING

    def test_accepted_owner_tenant_logs_nothing(self, caplog, monkeypatch) -> None:
        allow_orchestrator_sa(monkeypatch)
        request = MagicMock()
        request.headers = {"X-Owner-Tenant-Id": OWNER_TENANT}
        request.state.license = sa_introspection()
        with caplog.at_level(logging.DEBUG, logger="src.api.dependencies"):
            assert get_owner_tenant_id(request) == OWNER_TENANT
        assert not _records(caplog, "src.api.dependencies", "X-Owner-Tenant-Id")


# ---------------------------------------------------------------------------
# src/api/auth_routes.py — OAuth callback failure branches
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAuthCallbackLogLevels:
    @pytest.mark.asyncio
    async def test_missing_code_or_state_logs_at_warning(self, caplog) -> None:
        with caplog.at_level(logging.DEBUG, logger="src.api.auth_routes"):
            response = await auth_callback(
                code=None, state=None, error=None, oauth_service=MagicMock(), redis_client=None
            )
        assert response.status_code == 400
        matches = _records(caplog, "src.api.auth_routes", "missing required parameters")
        assert matches, "the malformed OAuth callback produced no log line"
        assert matches[0].levelno == logging.WARNING

    @pytest.mark.asyncio
    async def test_redis_unavailable_logs_at_warning(self, caplog) -> None:
        with caplog.at_level(logging.DEBUG, logger="src.api.auth_routes"):
            response = await auth_callback(
                code="c", state="s", error=None, oauth_service=MagicMock(), redis_client=None
            )
        assert response.status_code == 503
        matches = _records(caplog, "src.api.auth_routes", "Redis unavailable")
        assert matches, "the lost consent flow produced no log line"
        assert matches[0].levelno == logging.WARNING
