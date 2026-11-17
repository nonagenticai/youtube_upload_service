"""The orchestrator lane on ``/api/auth/{connect,status,disconnect}`` (P5-3).

The orchestrator proxies the YouTube connection routes with its service-account
licence + ``X-Owner-Tenant-Id``. Here the tenant acted on is the OWNER tenant:
the OAuth ``state`` binds it, so the public callback stores the grant under it;
``status`` reports the owner's grant through the same decision the upload worker
makes. No realm-role check on this lane (the orchestrator gates roles).

Anyone else asserting ``X-Owner-Tenant-Id`` — a customer licence, or a
Keycloak-JWT user, even an admin — is refused with 403.

Real ``OAuthService`` on postgres + fakeredis: the assertions read the stored
``oauth_tokens`` rows and the Redis state, not mock call args.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from src.api.auth_routes import public_router, router
from src.api.dependencies import get_oauth_service, get_redis_client
from src.database.models import OAuthToken
from src.services.encryption_service import EncryptionService
from src.services.oauth_service import OAuthService
from tests.orchestrator_sa import SA_SUBJECT, allow_orchestrator_sa, sa_introspection
from tests.platform_channel import seed_platform_channel
from tests.tenants import tenant_uuid

SA_TENANT = tenant_uuid("orchestrator-sa")
OWNER_A = tenant_uuid("owner-a")
OWNER_B = tenant_uuid("owner-b")


def _granted_creds() -> MagicMock:
    creds = MagicMock()
    creds.token = "owner-access"
    creds.refresh_token = "owner-refresh"
    creds.token_uri = "https://oauth2.googleapis.com/token"
    creds.scopes = ["https://www.googleapis.com/auth/youtube.upload"]
    creds.expiry = None
    return creds


def _make_app(
    oauth: OAuthService,
    redis,
    *,
    license_subject: str | None,
    roles: list[str] | None = None,
) -> FastAPI:
    """``license_subject`` = the introspected subject (licence lane); ``None`` = JWT lane."""
    app = FastAPI()

    @app.middleware("http")
    async def _identity(request, call_next):
        request.state.tenant_id = SA_TENANT
        request.state.roles = list(roles or [])
        request.state.license = (
            sa_introspection(license_subject) if license_subject is not None else None
        )
        return await call_next(request)

    app.include_router(router)
    app.include_router(public_router)
    app.dependency_overrides[get_oauth_service] = lambda: oauth
    app.dependency_overrides[get_redis_client] = lambda: redis
    return app


async def _oauth(
    db_session_factory, mock_settings, *, platform_default: bool = False
) -> OAuthService:
    """Platform channel row configured; ``platform_default`` = its default_for_tenants."""
    await seed_platform_channel(db_session_factory, default_for_tenants=platform_default)
    return OAuthService(
        session_factory=db_session_factory,
        encryption=EncryptionService(key=""),
        settings=mock_settings,
    )


async def _rows(db_session_factory) -> list[tuple[str, str]]:
    async with db_session_factory() as session:
        rows = (await session.execute(select(OAuthToken))).scalars().all()
    return [(r.tenant_id, r.access_token) for r in rows]


async def _state_keys(redis) -> list:
    """Minted OAuth states (RedisClient has no KEYS wrapper; fakeredis underneath)."""
    return await redis._client.keys("youtube:oauth_state:*")


@pytest.fixture
def sa_env(monkeypatch):
    allow_orchestrator_sa(monkeypatch)


@pytest.mark.asyncio
async def test_connect_then_callback_stores_the_grant_under_the_owner(
    sa_env, db_session_factory, mock_settings, real_redis_client
):
    oauth = await _oauth(db_session_factory, mock_settings)
    client = TestClient(_make_app(oauth, real_redis_client, license_subject=SA_SUBJECT))

    resp = client.post("/api/auth/connect", headers={"X-Owner-Tenant-Id": OWNER_A})
    assert resp.status_code == 200, resp.text
    state = resp.json()["state"]
    # The consent URL carries the state that binds the owner tenant.
    assert parse_qs(urlparse(resp.json()["authorization_url"]).query)["state"] == [state]
    # The state binds the owner tenant and the client that started the flow
    # (no tenant client configured → the platform client, recorded as null).
    assert json.loads(await real_redis_client.get(f"youtube:oauth_state:{state}")) == {
        "tenant_id": OWNER_A,
        "oauth_client_id": None,
    }

    with (
        patch.object(oauth, "exchange_code", return_value=_granted_creds()),
        patch("googleapiclient.discovery.build", side_effect=RuntimeError("no network")),
    ):
        cb = client.get(f"/api/auth/callback?code=c&state={state}")
    assert cb.status_code == 200, cb.text

    # Stored under the OWNER — not the SA tenant the request authenticated as.
    assert await _rows(db_session_factory) == [(OWNER_A, "owner-access")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("platform_default", "other_status", "other_can_publish"),
    [(False, "not_connected", False), (True, "platform_default", True)],
)
async def test_status_reports_the_owners_channel_truthfully(
    sa_env,
    db_session_factory,
    mock_settings,
    real_redis_client,
    platform_default,
    other_status,
    other_can_publish,
):
    oauth = await _oauth(db_session_factory, mock_settings, platform_default=platform_default)
    await oauth.store_tokens(OWNER_A, _granted_creds(), channel_id="UC-a", channel_title="A")
    # The SA tenant has its own grant: a lane that read the SA tenant would say
    # "connected" for B. It must not.
    await oauth.store_tokens(SA_TENANT, _granted_creds(), channel_id="UC-sa")
    client = TestClient(_make_app(oauth, real_redis_client, license_subject=SA_SUBJECT))

    a = client.get("/api/auth/status", headers={"X-Owner-Tenant-Id": OWNER_A}).json()
    b = client.get("/api/auth/status", headers={"X-Owner-Tenant-Id": OWNER_B}).json()

    assert (a["tenant_id"], a["status"], a["connected"], a["can_publish"]) == (
        OWNER_A,
        "connected",
        True,
        True,
    )
    assert a["channel_id"] == "UC-a"
    assert (b["tenant_id"], b["status"], b["connected"], b["can_publish"]) == (
        OWNER_B,
        other_status,
        False,
        other_can_publish,
    )


@pytest.mark.asyncio
async def test_disconnect_removes_only_the_owners_grant(
    sa_env, db_session_factory, mock_settings, real_redis_client
):
    oauth = await _oauth(db_session_factory, mock_settings)
    await oauth.store_tokens(OWNER_A, _granted_creds())
    await oauth.store_tokens(OWNER_B, _granted_creds())
    # No roles at all: role gating is the orchestrator's job on this lane.
    client = TestClient(_make_app(oauth, real_redis_client, license_subject=SA_SUBJECT, roles=[]))

    resp = client.delete("/api/auth/disconnect", headers={"X-Owner-Tenant-Id": OWNER_A})

    assert resp.status_code == 200, resp.text
    assert resp.json()["tenant_id"] == OWNER_A
    assert [t for t, _ in await _rows(db_session_factory)] == [OWNER_B]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("license_subject", "roles"),
    [
        # A customer licence: licence lane, subject NOT in AUTH_DELEGATING_SUBJECTS.
        ("0198a5e2-0000-7000-8000-c0570e000001", []),
        # A Keycloak-JWT user — even a super_admin — has no licence introspection.
        (None, ["super_admin"]),
    ],
)
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/auth/connect"),
        ("GET", "/api/auth/status"),
        ("DELETE", "/api/auth/disconnect"),
    ],
)
async def test_owner_header_without_sa_auth_is_refused(
    sa_env,
    db_session_factory,
    mock_settings,
    real_redis_client,
    license_subject,
    roles,
    method,
    path,
):
    oauth = await _oauth(db_session_factory, mock_settings)
    await oauth.store_tokens(OWNER_A, _granted_creds())
    client = TestClient(
        _make_app(oauth, real_redis_client, license_subject=license_subject, roles=roles)
    )

    resp = client.request(method, path, headers={"X-Owner-Tenant-Id": OWNER_A})

    assert resp.status_code == 403, resp.text
    assert "orchestrator service account" in resp.json()["detail"]
    assert await _rows(db_session_factory) == [(OWNER_A, "owner-access")]  # not deleted
    assert await _state_keys(real_redis_client) == []  # no state minted


@pytest.mark.asyncio
async def test_sa_lane_is_refused_when_no_delegating_subjects_are_configured(
    db_session_factory, mock_settings, real_redis_client
):
    """Empty ``AUTH_DELEGATING_SUBJECTS`` (the default) disables the lane."""
    oauth = await _oauth(db_session_factory, mock_settings)
    client = TestClient(_make_app(oauth, real_redis_client, license_subject=SA_SUBJECT))

    resp = client.post("/api/auth/connect", headers={"X-Owner-Tenant-Id": OWNER_A})

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_sa_lane_rejects_a_nil_owner(
    sa_env, db_session_factory, mock_settings, real_redis_client
):
    oauth = await _oauth(db_session_factory, mock_settings)
    client = TestClient(_make_app(oauth, real_redis_client, license_subject=SA_SUBJECT))

    resp = client.post(
        "/api/auth/connect", headers={"X-Owner-Tenant-Id": "00000000-0000-0000-0000-000000000000"}
    )

    assert resp.status_code == 400
    assert await _state_keys(real_redis_client) == []
