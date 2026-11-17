"""Tests for FastAPI dependency helpers."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from src.api.dependencies import (
    get_oauth_service,
    get_redis_client,
    get_task_manager,
    get_tenant_id,
    get_upload_service,
)
from tests.tenants import NIL_UUID_SPELLINGS, tenant_uuid


@pytest.mark.unit
class TestGetTenantId:
    def test_returns_tenant_from_state(self) -> None:
        request = MagicMock()
        request.state.tenant_id = tenant_uuid("tenant-42")
        assert get_tenant_id(request) == tenant_uuid("tenant-42")

    def test_refuses_with_401_when_tenant_id_missing(self) -> None:
        """This used to assert ``get_tenant_id(request) == "anonymous"``.

        That substitution is the bug w2v_common 0.67.0 removed fleet-wide, and
        here it was load-bearing in the worst way: ``"anonymous"`` was passed to
        ``bind_session_tenant`` as a real session's RLS binding and written into
        ``oauth_tokens.tenant_id``, so an unauthenticated request could read and
        overwrite a channel's stored Google refresh token. The assertion is
        inverted rather than deleted: the same input must now be REFUSED.
        """
        request = MagicMock()
        # ``spec=[]`` = a state object with NO ``tenant_id`` attribute at all,
        # i.e. a request the auth middleware never annotated. The request itself
        # stays a plain MagicMock so the rejection's WARNING log line (which reads
        # ``request.method``/``request.url.path``) resolves.
        request.state = MagicMock(spec=[])
        with pytest.raises(HTTPException) as exc_info:
            get_tenant_id(request)
        assert exc_info.value.status_code == 401

    @pytest.mark.parametrize(
        "slug",
        ["anonymous", "none", "null", "n8n-dev", ""],
        ids=["anonymous", "none", "null", "legacy-slug", "empty"],
    )
    def test_refuses_a_slug_tenant(self, slug: str) -> None:
        """A legacy slug is not a tenant. These are the exact values the deleted
        ``UNBOUND_OWNER_TENANTS`` denylist named plus a real fleet slug it did
        not, and all of them are now refused by the same UUID rule."""
        request = MagicMock()
        request.state.tenant_id = slug
        with pytest.raises(HTTPException) as exc_info:
            get_tenant_id(request)
        assert exc_info.value.status_code == 401

    @pytest.mark.parametrize("spelling", NIL_UUID_SPELLINGS)
    def test_refuses_the_nil_uuid_in_every_spelling(self, spelling: str) -> None:
        """The nil UUID is a sentinel, never a tenant — in ANY of its spellings.

        The old guard was a lowercase string denylist containing only the dashed
        form, so the other three parsed to the same nil value and sailed through.
        This is the mutation-sensitive test for ``src.tenant.require_tenant``'s
        ``parsed.int == 0`` check: without it, all four of these return 200-able
        tenants.
        """
        request = MagicMock()
        request.state.tenant_id = spelling
        with pytest.raises(HTTPException) as exc_info:
            get_tenant_id(request)
        assert exc_info.value.status_code == 401

    def test_accepts_the_real_all_but_nil_tenant(self) -> None:
        """``…0001`` looks like a placeholder but is a real ``active`` tenant
        owning 97 projects (see CLAUDE.md). A guard that rejects it locks the
        largest tenant in the system out — so this is the control that proves
        the nil check keys on the VALUE zero and not on "looks like zeros"."""
        request = MagicMock()
        request.state.tenant_id = "00000000-0000-0000-0000-000000000001"
        assert get_tenant_id(request) == "00000000-0000-0000-0000-000000000001"

    def test_returns_different_tenant_ids(self) -> None:
        request = MagicMock()
        request.state.tenant_id = tenant_uuid("org-abc-123")
        assert get_tenant_id(request) == tenant_uuid("org-abc-123")

        request.state.tenant_id = tenant_uuid("user-xyz")
        assert get_tenant_id(request) == tenant_uuid("user-xyz")


@pytest.mark.unit
class TestGetTaskManager:
    def test_returns_task_manager_from_app_state(self) -> None:
        request = MagicMock()
        sentinel = object()
        request.app.state.task_manager = sentinel
        assert get_task_manager(request) is sentinel

    def test_returns_exact_instance(self) -> None:
        """Verify identity -- the function must not copy or wrap the object."""
        request = MagicMock()
        task_mgr = MagicMock()
        request.app.state.task_manager = task_mgr
        result = get_task_manager(request)
        assert result is task_mgr


@pytest.mark.unit
class TestGetOAuthService:
    def test_returns_oauth_service_from_app_state(self) -> None:
        request = MagicMock()
        sentinel = object()
        request.app.state.oauth_service = sentinel
        assert get_oauth_service(request) is sentinel

    def test_returns_exact_instance(self) -> None:
        """Verify identity -- the function must not copy or wrap the object."""
        request = MagicMock()
        oauth_svc = MagicMock()
        request.app.state.oauth_service = oauth_svc
        result = get_oauth_service(request)
        assert result is oauth_svc


@pytest.mark.unit
class TestGetUploadService:
    def test_returns_upload_service_from_app_state(self) -> None:
        request = MagicMock()
        sentinel = object()
        request.app.state.upload_service = sentinel
        assert get_upload_service(request) is sentinel

    def test_returns_exact_instance(self) -> None:
        """Verify identity -- the function must not copy or wrap the object."""
        request = MagicMock()
        upload_svc = MagicMock()
        request.app.state.upload_service = upload_svc
        result = get_upload_service(request)
        assert result is upload_svc


@pytest.mark.unit
class TestGetRedisClient:
    def test_returns_redis_client_from_app_state(self) -> None:
        request = MagicMock()
        sentinel = object()
        request.app.state.redis_client = sentinel
        assert get_redis_client(request) is sentinel

    def test_returns_none_when_set_to_none(self) -> None:
        request = MagicMock()
        request.app.state.redis_client = None
        assert get_redis_client(request) is None
