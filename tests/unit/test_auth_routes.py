"""Tests for OAuth2 auth routes."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.auth_routes import public_router, router
from src.api.dependencies import get_oauth_service, get_redis_client, get_tenant_id
from src.services.youtube_config_service import OAuthClient
from tests.orchestrator_sa import SA_SUBJECT, allow_orchestrator_sa, sa_introspection
from tests.tenants import tenant_uuid

# ---------------------------------------------------------------------------
# App fixture
# ---------------------------------------------------------------------------


PLATFORM_CLIENT = OAuthClient(client_id="test-client-id", client_secret="s", recorded_id=None)


@pytest.fixture
def mock_oauth_service():
    """OAuthService mock; ``.config`` resolves the platform client (no tenant client)."""
    service = MagicMock()
    service.config.connect_client = AsyncMock(return_value=PLATFORM_CLIENT)
    service.config.client_for_recorded_id = AsyncMock(return_value=PLATFORM_CLIENT)
    return service


@pytest.fixture
def mock_redis():
    redis = AsyncMock()
    redis.set = AsyncMock()
    redis.get = AsyncMock()
    redis.delete = AsyncMock()
    return redis


OWNER = tenant_uuid("test-tenant")


@pytest.fixture
def caller_license_subject() -> list[str | None]:
    """Introspected licence subject of the caller; set ``[0] = None`` for a Keycloak-JWT
    caller (no licence introspection). Defaults to the orchestrator SA."""
    return [SA_SUBJECT]


@pytest.fixture
def caller_roles() -> list[str]:
    """Realm roles a Keycloak-JWT caller would carry (ignored by the routes)."""
    return []


@pytest.fixture
def app(mock_oauth_service, mock_redis, caller_license_subject, caller_roles, monkeypatch):
    """FastAPI test app with auth routes and mocked services.

    Identity is stamped the way w2v_common's AuthMiddleware does it, so the REAL
    ``get_auth_owner_tenant_id`` runs — it is not overridden. By default the
    caller is the orchestrator SA on the licence lane.
    """
    allow_orchestrator_sa(monkeypatch)
    test_app = FastAPI()

    @test_app.middleware("http")
    async def _stash_identity(request, call_next):
        subject = caller_license_subject[0]
        request.state.license = sa_introspection(subject) if subject is not None else None
        request.state.roles = list(caller_roles)
        return await call_next(request)

    test_app.include_router(router)
    # GET /api/auth/callback lives on the UNGATED ``public_router`` (Google
    # redirects here with no licence token); include it so these callback tests
    # still reach a route rather than a 404.
    test_app.include_router(public_router)

    test_app.dependency_overrides[get_tenant_id] = lambda: tenant_uuid("orchestrator-sa")
    test_app.dependency_overrides[get_oauth_service] = lambda: mock_oauth_service
    test_app.dependency_overrides[get_redis_client] = lambda: mock_redis

    return test_app


@pytest.fixture
def client(app):
    return TestClient(app, headers={"X-Owner-Tenant-Id": OWNER})


# ---------------------------------------------------------------------------
# POST /api/auth/connect
# ---------------------------------------------------------------------------


class TestAuthConnect:
    def test_connect_returns_authorization_url(
        self, client: TestClient, mock_oauth_service: MagicMock, mock_redis: AsyncMock
    ):
        mock_oauth_service.build_authorization_url.return_value = (
            "https://accounts.google.com/o/oauth2/auth?state=abc"
        )

        response = client.post("/api/auth/connect")
        assert response.status_code == 200
        data = response.json()
        assert "authorization_url" in data
        assert "state" in data
        assert data["authorization_url"] == "https://accounts.google.com/o/oauth2/auth?state=abc"

    def test_connect_stores_state_in_redis(
        self, client: TestClient, mock_oauth_service: MagicMock, mock_redis: AsyncMock
    ):
        mock_oauth_service.build_authorization_url.return_value = "https://example.com"

        client.post("/api/auth/connect")

        mock_redis.set.assert_called_once()
        call_args = mock_redis.set.call_args
        key = call_args[0][0]
        value = call_args[0][1]
        assert key.startswith("youtube:oauth_state:")
        assert json.loads(value) == {"tenant_id": OWNER, "oauth_client_id": None}

    def test_connect_fails_without_redis(self, app: FastAPI, mock_oauth_service: MagicMock):
        app.dependency_overrides[get_redis_client] = lambda: None
        client = TestClient(app, headers={"X-Owner-Tenant-Id": OWNER})

        response = client.post("/api/auth/connect")
        assert response.status_code == 503


# ---------------------------------------------------------------------------
# Caller lane — connect/status/disconnect accept ONLY the orchestrator SA
# ---------------------------------------------------------------------------

_TENANT_ROUTES = [
    ("POST", "/api/auth/connect"),
    ("GET", "/api/auth/status"),
    ("DELETE", "/api/auth/disconnect"),
]


class TestOrchestratorLaneOnly:
    @pytest.mark.parametrize(("method", "path"), _TENANT_ROUTES)
    @pytest.mark.parametrize("roles", [["super_admin"], ["tenant_admin"], ["member"], []])
    @pytest.mark.parametrize("send_owner_header", [True, False])
    def test_keycloak_jwt_caller_is_403(
        self,
        app: FastAPI,
        caller_license_subject: list[str | None],
        caller_roles: list[str],
        mock_oauth_service: MagicMock,
        mock_redis: AsyncMock,
        method: str,
        path: str,
        roles: list[str],
        send_owner_header: bool,
    ):
        """The deleted user-JWT lane stays deleted: a Keycloak-JWT caller (no
        licence introspection) — even a super_admin, with or without an owner
        header — is refused before any state is minted or any grant touched."""
        caller_license_subject[0] = None
        caller_roles[:] = roles
        mock_oauth_service.delete_tokens = AsyncMock(return_value=True)
        mock_oauth_service.get_token_info = AsyncMock(return_value={"status": "connected"})
        headers = {"X-Owner-Tenant-Id": OWNER} if send_owner_header else {}

        response = TestClient(app).request(method, path, headers=headers)

        assert response.status_code == 403, response.text
        assert "orchestrator service account" in response.json()["detail"]
        mock_redis.set.assert_not_called()
        mock_oauth_service.build_authorization_url.assert_not_called()
        mock_oauth_service.delete_tokens.assert_not_called()
        mock_oauth_service.get_token_info.assert_not_called()

    @pytest.mark.parametrize(("method", "path"), _TENANT_ROUTES)
    def test_sa_without_owner_header_is_400(self, app: FastAPI, method: str, path: str):
        response = TestClient(app).request(method, path)
        assert response.status_code == 400
        assert "X-Owner-Tenant-Id" in response.json()["detail"]

    def test_unauthenticated_is_401_before_403(
        self, app: FastAPI, caller_license_subject: list[str | None]
    ):
        """The real get_tenant_id runs first: no tenant bound -> 401, not a lane 403."""
        del app.dependency_overrides[get_tenant_id]
        caller_license_subject[0] = None
        response = TestClient(app).post("/api/auth/connect")
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# GET /api/auth/callback
# ---------------------------------------------------------------------------


class TestAuthCallback:
    def test_callback_with_error_returns_400(self, client: TestClient):
        response = client.get("/api/auth/callback?error=access_denied")
        assert response.status_code == 400
        assert "access_denied" in response.text

    def test_callback_missing_code_returns_400(self, client: TestClient):
        response = client.get("/api/auth/callback?state=abc")
        assert response.status_code == 400
        assert "Missing" in response.text

    def test_callback_missing_state_returns_400(self, client: TestClient):
        response = client.get("/api/auth/callback?code=abc")
        assert response.status_code == 400
        assert "Missing" in response.text

    def test_callback_invalid_state_returns_400(self, client: TestClient, mock_redis: AsyncMock):
        mock_redis.get.return_value = None  # state not found

        response = client.get("/api/auth/callback?code=auth-code&state=bad-state")
        assert response.status_code == 400
        assert "expired" in response.text.lower() or "invalid" in response.text.lower()

    def test_callback_success(
        self, client: TestClient, mock_oauth_service: MagicMock, mock_redis: AsyncMock
    ):
        mock_redis.get.return_value = json.dumps(
            {"tenant_id": tenant_uuid("test-tenant"), "oauth_client_id": None}
        )

        mock_creds = MagicMock()
        mock_creds.token = "access-tok"
        mock_creds.refresh_token = "refresh-tok"
        mock_creds.token_uri = "https://oauth2.googleapis.com/token"
        mock_creds.scopes = ["https://www.googleapis.com/auth/youtube.upload"]
        mock_creds.expiry = None

        mock_oauth_service.exchange_code = MagicMock(return_value=mock_creds)
        mock_oauth_service.store_tokens = AsyncMock()

        # Mock the YouTube API channel info fetch
        with patch("googleapiclient.discovery.build") as mock_build:
            mock_youtube = MagicMock()
            mock_build.return_value = mock_youtube
            mock_youtube.channels.return_value.list.return_value.execute.return_value = {
                "items": [{"id": "UC123", "snippet": {"title": "Test Channel"}}]
            }

            response = client.get("/api/auth/callback?code=valid-code&state=valid-state")

        assert response.status_code == 200
        assert "Test Channel" in response.text
        mock_redis.delete.assert_called_once()
        mock_oauth_service.store_tokens.assert_called_once()

    def test_callback_without_redis_returns_503(self, app: FastAPI, mock_oauth_service: MagicMock):
        app.dependency_overrides[get_redis_client] = lambda: None
        client = TestClient(app)

        response = client.get("/api/auth/callback?code=abc&state=xyz")
        assert response.status_code == 503

    def test_callback_exchange_failure_returns_500(
        self, client: TestClient, mock_oauth_service: MagicMock, mock_redis: AsyncMock
    ):
        mock_redis.get.return_value = json.dumps(
            {"tenant_id": tenant_uuid("test-tenant"), "oauth_client_id": None}
        )
        mock_oauth_service.exchange_code = MagicMock(side_effect=ValueError("bad code"))

        response = client.get("/api/auth/callback?code=bad&state=valid")
        assert response.status_code == 500
        assert "Token exchange failed" in response.text

    def test_callback_channel_fetch_httperror_still_persists_tokens(
        self, app: FastAPI, mock_oauth_service: MagicMock, mock_redis: AsyncMock
    ):
        """A transient Google HttpError during the BEST-EFFORT channel-info fetch
        must NOT block token persistence.

        `youtube.channels().list().execute()` raises googleapiclient HttpError on
        any non-2xx (403 quota, transient 5xx). HttpError subclasses only
        Exception — none of the old (OSError, ValueError, RuntimeError,
        TimeoutError) tuple. Before the fix the error escaped the best-effort
        block, the callback 500'd, and store_tokens() never ran, so the tenant's
        just-exchanged one-time code was burned with nothing persisted.

        RED (pre-fix): callback 500s and store_tokens is NOT called.
        GREEN (post-fix): callback returns 200, store_tokens IS called with
        channel_id=None (best-effort degraded, tokens still persisted).
        """
        from googleapiclient.errors import HttpError

        mock_redis.get.return_value = json.dumps(
            {"tenant_id": tenant_uuid("test-tenant"), "oauth_client_id": None}
        )

        mock_creds = MagicMock()
        mock_oauth_service.exchange_code = MagicMock(return_value=mock_creds)
        mock_oauth_service.store_tokens = AsyncMock()

        resp = MagicMock()
        resp.status = 403
        resp.reason = "quota exceeded"
        http_error = HttpError(resp, b'{"error": {"message": "quota"}}', uri="https://youtube")

        # raise_server_exceptions=False so an escaping exception surfaces as a
        # clean 500 instead of propagating out of the test client — the RED
        # failure is "tokens not persisted / 500", not a raised exception.
        client = TestClient(app, raise_server_exceptions=False)
        with patch("googleapiclient.discovery.build") as mock_build:
            mock_youtube = MagicMock()
            mock_build.return_value = mock_youtube
            mock_youtube.channels.return_value.list.return_value.execute.side_effect = http_error

            response = client.get("/api/auth/callback?code=valid-code&state=valid-state")

        # Tokens MUST be persisted despite the best-effort channel fetch failing.
        mock_oauth_service.store_tokens.assert_called_once()
        args, _kwargs = mock_oauth_service.store_tokens.call_args
        # store_tokens(tenant_id, credentials, channel_id, channel_title)
        assert args[0] == tenant_uuid("test-tenant")
        assert args[2] is None  # channel_id degraded to None
        assert args[3] is None  # channel_title degraded to None
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# GET /api/auth/status
# ---------------------------------------------------------------------------


class TestAuthStatus:
    def test_status_connected(self, client: TestClient, mock_oauth_service: MagicMock):
        mock_oauth_service.get_token_info = AsyncMock(
            return_value={
                "status": "connected",
                "mode": "tenant",
                "needs_reconnect": False,
                "channel_id": "UC123",
                "channel_title": "My Channel",
                "scopes": ["youtube.upload"],
                "connected_at": "2025-01-01T00:00:00+00:00",
                "updated_at": "2025-06-01T00:00:00+00:00",
                "token_expiry": None,
            }
        )

        response = client.get("/api/auth/status")
        assert response.status_code == 200
        data = response.json()
        assert data["connected"] is True
        assert data["status"] == "connected"
        assert data["can_publish"] is True
        assert data["channel_id"] == "UC123"
        assert data["tenant_id"] == tenant_uuid("test-tenant")

    def test_status_not_connected(self, client: TestClient, mock_oauth_service: MagicMock):
        mock_oauth_service.get_token_info = AsyncMock(
            return_value={
                "tenant_id": tenant_uuid("test-tenant"),
                "status": "not_connected",
                "mode": "none",
                "needs_reconnect": False,
            }
        )

        response = client.get("/api/auth/status")
        assert response.status_code == 200
        data = response.json()
        assert data["connected"] is False
        assert data["status"] == "not_connected"
        assert data["can_publish"] is False
        assert data["tenant_id"] == tenant_uuid("test-tenant")

    def test_status_platform_default_is_publishable_but_not_connected(
        self, client: TestClient, mock_oauth_service: MagicMock
    ):
        """Riding OUR channel is not the tenant being connected. Old code said
        ``connected: true`` here, so the UI showed our channel as theirs."""
        mock_oauth_service.get_token_info = AsyncMock(
            return_value={
                "tenant_id": tenant_uuid("test-tenant"),
                "status": "platform_default",
                "mode": "platform",
                "needs_reconnect": False,
                "channel_id": "UCplatform",
                "channel_title": "Platform",
                "scopes": [],
            }
        )

        data = client.get("/api/auth/status").json()
        assert data["connected"] is False
        assert data["status"] == "platform_default"
        assert data["can_publish"] is True
        assert data["channel_id"] == "UCplatform"

    def test_status_reconnect_required(self, client: TestClient, mock_oauth_service: MagicMock):
        """A dead grant is not ``connected`` and cannot publish (old code: connected=true)."""
        mock_oauth_service.get_token_info = AsyncMock(
            return_value={
                "tenant_id": tenant_uuid("test-tenant"),
                "status": "reconnect_required",
                "mode": "tenant",
                "needs_reconnect": True,
                "reconnect_reason": "invalid_grant",
                "channel_id": "UCtenant",
                "channel_title": "Tenant Channel",
                "scopes": ["s"],
            }
        )

        data = client.get("/api/auth/status").json()
        assert data["connected"] is False
        assert data["status"] == "reconnect_required"
        assert data["can_publish"] is False
        assert data["reconnect_reason"] == "invalid_grant"
        assert data["channel_title"] == "Tenant Channel"


# ---------------------------------------------------------------------------
# DELETE /api/auth/disconnect
# ---------------------------------------------------------------------------


class TestAuthDisconnect:
    def test_disconnect_success(self, client: TestClient, mock_oauth_service: MagicMock):
        mock_oauth_service.delete_tokens = AsyncMock(return_value=True)

        response = client.delete("/api/auth/disconnect")
        assert response.status_code == 200
        data = response.json()
        assert data["disconnected"] is True
        assert data["tenant_id"] == tenant_uuid("test-tenant")

    def test_disconnect_not_found(self, client: TestClient, mock_oauth_service: MagicMock):
        mock_oauth_service.delete_tokens = AsyncMock(return_value=False)

        response = client.delete("/api/auth/disconnect")
        assert response.status_code == 404
