"""Per-tenant YouTube settings + the platform channel in the DB (migration 014).

Real postgres (``db_engine`` / ``db_session_factory``), real ``OAuthService`` /
``YouTubeConfigService``, real Fernet key, fakeredis. Google is the only fake:
``Credentials.refresh`` / ``exchange_code`` are patched to RECORD which client
they were called with — that is the property under test.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import importlib.util
import io
import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import create_async_engine

from src.api.auth_routes import public_router, router
from src.api.dependencies import get_oauth_service, get_redis_client
from src.api.settings_routes import admin_router
from src.api.settings_routes import router as settings_router
from src.database.models import OAuthToken, TenantYouTubeSettings, YouTubePlatformChannel
from src.database.rls_resolvers import LIST_TENANT_SETTINGS_FN_SQL
from src.services.encryption_service import EncryptionService
from src.services.oauth_service import (
    OAuthService,
    YouTubeNotConnectedError,
    YouTubeReconnectRequiredError,
)
from tests.orchestrator_sa import SA_SUBJECT, allow_orchestrator_sa, sa_introspection
from tests.platform_channel import seed_platform_channel
from tests.tenants import tenant_uuid

pytestmark = pytest.mark.asyncio

SA_TENANT = tenant_uuid("orchestrator-sa")
TENANT_A = tenant_uuid("yt-cfg-a")
TENANT_B = tenant_uuid("yt-cfg-b")

# Synthetic secrets — distinctive so a leak is unambiguous in any output.
TENANT_SECRET = "tenant-client-secret-zq9"  # pragma: allowlist secret
PLATFORM_SECRET = "platform-client-secret-zq9"  # pragma: allowlist secret
PLATFORM_RT = "platform-refresh-token-zq9"  # pragma: allowlist secret
SECRETS = (TENANT_SECRET, PLATFORM_SECRET, PLATFORM_RT)


@pytest.fixture
def encryption() -> EncryptionService:
    return EncryptionService(key=EncryptionService.generate_key())


@pytest.fixture
def oauth(db_session_factory, encryption, mock_settings) -> OAuthService:
    return OAuthService(
        session_factory=db_session_factory, encryption=encryption, settings=mock_settings
    )


def _creds(*, expired: bool = True, token: str = "at") -> MagicMock:
    creds = MagicMock()
    creds.token = token
    creds.refresh_token = "grant-rt"
    creds.token_uri = "https://oauth2.googleapis.com/token"
    creds.scopes = ["https://www.googleapis.com/auth/youtube.upload"]
    creds.expiry = dt.datetime(2020, 1, 1, tzinfo=dt.UTC) if expired else None
    return creds


class _RefreshRecorder:
    """Stands in for ``Credentials.refresh``; records which client refreshed what."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def __call__(self, creds, _request) -> None:
        self.calls.append((creds.client_id, creds.client_secret, creds.refresh_token))
        creds.token = "fresh-at"
        creds.expiry = dt.datetime(2099, 1, 1)


def _patch_refresh(recorder: _RefreshRecorder):
    return (
        patch("src.services.oauth_service.GoogleAuthRequest"),
        # A plain function (not the instance) so it binds as a method: self = creds.
        patch(
            "google.oauth2.credentials.Credentials.refresh",
            lambda creds, request: recorder(creds, request),
        ),
    )


async def _settings_row(factory, tenant: str) -> TenantYouTubeSettings | None:
    async with factory() as session:
        return await session.get(TenantYouTubeSettings, tenant)


async def _grant(factory, tenant: str) -> OAuthToken | None:
    async with factory() as session:
        stmt = select(OAuthToken).where(OAuthToken.tenant_id == tenant)
        return (await session.execute(stmt)).scalar_one_or_none()


# ---------------------------------------------------------------------------
# Resolution matrix (upload + status agree)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("platform_row", "default", "tenant_allowed", "expected_mode"),
    [
        (True, False, True, "platform"),  # allowed overrides default-off
        (True, True, False, "none"),  # denied overrides default-on
        (True, True, None, "platform"),  # NULL follows default-on
        (True, False, None, "none"),  # NULL follows default-off
        (False, True, True, "none"),  # allowed but no platform row
        (False, False, None, "none"),  # nothing at all
    ],
)
async def test_no_grant_resolution_matrix(
    oauth, db_session_factory, encryption, platform_row, default, tenant_allowed, expected_mode
):
    if platform_row:
        await seed_platform_channel(
            db_session_factory,
            default_for_tenants=default,
            encryption=encryption,
            client_secret=PLATFORM_SECRET,
            refresh_token=PLATFORM_RT,
        )
    if tenant_allowed is not None:
        await oauth.config.set_tenant_platform_allowed(TENANT_A, tenant_allowed, "op")

    info = await oauth.get_token_info(TENANT_A)
    assert info["mode"] == expected_mode
    assert info["needs_reconnect"] is False

    recorder = _RefreshRecorder()
    p1, p2 = _patch_refresh(recorder)
    with p1, p2:
        if expected_mode == "platform":
            _creds_out, via = await oauth.resolve_credentials(TENANT_A)
            assert via == "platform"
            assert recorder.calls == [("fallback-cid", PLATFORM_SECRET, PLATFORM_RT)]
            assert info["channel_title"] == "George Dekker"
        else:
            with pytest.raises(YouTubeNotConnectedError, match="connect YouTube"):
                await oauth.resolve_credentials(TENANT_A)
            assert recorder.calls == [], "the platform credential must not even be touched"


async def test_grant_wins_over_platform_and_refreshes_with_the_platform_client_when_null(
    oauth, db_session_factory, encryption
):
    await seed_platform_channel(db_session_factory, default_for_tenants=True, encryption=encryption)
    await oauth.store_tokens(TENANT_A, _creds(), channel_title="Tenant A Channel")

    info = await oauth.get_token_info(TENANT_A)
    assert (info["mode"], info["status"], info["channel_title"]) == (
        "tenant",
        "connected",
        "Tenant A Channel",
    )
    recorder = _RefreshRecorder()
    p1, p2 = _patch_refresh(recorder)
    with p1, p2:
        creds, via = await oauth.resolve_credentials(TENANT_A)
    assert via == "tenant"
    # NULL oauth_client_id = the deployment client (YOUTUBE_CLIENT_ID in conftest).
    assert recorder.calls == [("test-client-id", "test-client-secret", "grant-rt")]
    assert creds.token == "fresh-at"
    row = await _grant(db_session_factory, TENANT_A)
    assert row is not None
    assert encryption.decrypt(row.access_token) == "fresh-at"
    assert row.oauth_client_id is None


async def test_tenant_client_grant_refreshes_with_the_issuing_tenant_client(
    oauth, db_session_factory
):
    await oauth.config.set_oauth_client(TENANT_A, "tenant-cid", TENANT_SECRET, "admin")
    await oauth.store_tokens(TENANT_A, _creds(), oauth_client_id="tenant-cid")

    recorder = _RefreshRecorder()
    p1, p2 = _patch_refresh(recorder)
    with p1, p2:
        _c, via = await oauth.resolve_credentials(TENANT_A)
    assert via == "tenant"
    assert recorder.calls == [("tenant-cid", TENANT_SECRET, "grant-rt")]
    row = await _grant(db_session_factory, TENANT_A)
    assert row is not None
    assert (row.oauth_client_id, row.needs_reconnect) == ("tenant-cid", False)


async def test_client_mismatch_is_reconnect_and_never_falls_back(
    oauth, db_session_factory, encryption
):
    """Grant issued by a client the tenant no longer has → needs_reconnect, fail loud,
    no refresh attempt, no platform channel — even with platform default on."""
    await seed_platform_channel(db_session_factory, default_for_tenants=True, encryption=encryption)
    await oauth.config.set_oauth_client(TENANT_A, "tenant-cid-new", TENANT_SECRET, "admin")
    # A grant issued by an older client (written directly: simulates drift).
    await oauth.store_tokens(TENANT_A, _creds(), oauth_client_id="tenant-cid-old")

    info = await oauth.get_token_info(TENANT_A)
    assert (info["mode"], info["status"], info["needs_reconnect"]) == (
        "tenant",
        "reconnect_required",
        True,
    )

    recorder = _RefreshRecorder()
    p1, p2 = _patch_refresh(recorder)
    with p1, p2, pytest.raises(YouTubeReconnectRequiredError, match="reconnect YouTube"):
        await oauth.resolve_credentials(TENANT_A)
    assert recorder.calls == []
    row = await _grant(db_session_factory, TENANT_A)
    assert row is not None and row.needs_reconnect is True


async def test_put_oauth_client_marks_a_grant_from_another_client(oauth, db_session_factory):
    await oauth.store_tokens(TENANT_A, _creds(expired=False))  # platform-client grant
    await oauth.store_tokens(TENANT_B, _creds(expired=False))

    await oauth.config.set_oauth_client(TENANT_A, "tenant-cid", TENANT_SECRET, "admin")

    a = await _grant(db_session_factory, TENANT_A)
    b = await _grant(db_session_factory, TENANT_B)
    assert a is not None and a.needs_reconnect is True
    assert b is not None and b.needs_reconnect is False, "another tenant's grant is untouched"
    with pytest.raises(YouTubeReconnectRequiredError):
        await oauth.resolve_credentials(TENANT_A)


async def test_rotating_the_secret_of_the_same_client_keeps_the_grant(oauth, db_session_factory):
    await oauth.config.set_oauth_client(TENANT_A, "tenant-cid", TENANT_SECRET, "admin")
    await oauth.store_tokens(TENANT_A, _creds(expired=False), oauth_client_id="tenant-cid")
    await oauth.config.set_oauth_client(TENANT_A, "tenant-cid", "rotated-secret", "admin")
    row = await _grant(db_session_factory, TENANT_A)
    assert row is not None and row.needs_reconnect is False


async def test_delete_oauth_client_marks_the_grant_it_issued(oauth, db_session_factory):
    await oauth.config.set_oauth_client(TENANT_A, "tenant-cid", TENANT_SECRET, "admin")
    await oauth.store_tokens(TENANT_A, _creds(expired=False), oauth_client_id="tenant-cid")
    view = await oauth.config.clear_oauth_client(TENANT_A, "admin")
    assert view["oauth_client_id"] is None and view["has_oauth_client_secret"] is False
    row = await _grant(db_session_factory, TENANT_A)
    assert row is not None and row.needs_reconnect is True


async def test_refresh_does_not_heal_a_grant_flagged_mid_refresh(oauth, db_session_factory):
    """A client swap while a refresh is in flight flags the grant; the refresh's
    write-back must not clear that flag (it is guarded on client + flag)."""
    await oauth.config.set_oauth_client(TENANT_A, "tenant-cid", TENANT_SECRET, "admin")
    await oauth.store_tokens(TENANT_A, _creds(), oauth_client_id="tenant-cid")

    def _refresh_then_swap(creds, _request):
        creds.token = "fresh-at"
        creds.expiry = dt.datetime(2099, 1, 1)
        asyncio.run_coroutine_threadsafe(
            oauth.config.set_oauth_client(TENANT_A, "other-cid", TENANT_SECRET, "admin"), loop
        ).result(timeout=10)

    loop = asyncio.get_running_loop()
    with (
        patch("src.services.oauth_service.GoogleAuthRequest"),
        patch("google.oauth2.credentials.Credentials.refresh", _refresh_then_swap),
    ):
        await oauth.resolve_credentials(TENANT_A)
    row = await _grant(db_session_factory, TENANT_A)
    assert row is not None and row.needs_reconnect is True


# ---------------------------------------------------------------------------
# Boot seed
# ---------------------------------------------------------------------------


def _seed_env(mock_settings, *, default: bool = True, secret: str = PLATFORM_SECRET):
    s = mock_settings.model_copy(deep=True)
    s.youtube.fallback_client_id = "seed-cid"
    s.youtube.fallback_client_secret = secret
    s.youtube.fallback_refresh_token = PLATFORM_RT
    s.youtube.fallback_channel_id = "UCseed"
    s.youtube.fallback_channel_title = "Seed Channel"
    s.youtube.platform_channel_as_default = default
    return s


async def _platform_rows(factory) -> list[YouTubePlatformChannel]:
    async with factory() as session:
        return list((await session.execute(select(YouTubePlatformChannel))).scalars().all())


async def test_seed_without_env_writes_nothing(oauth, db_session_factory):
    assert await oauth.config.seed_platform_channel_from_env() is False
    assert await _platform_rows(db_session_factory) == []


async def test_seed_is_idempotent_and_env_is_ignored_once_the_row_exists(
    db_session_factory, encryption, mock_settings
):
    first = OAuthService(db_session_factory, encryption, _seed_env(mock_settings, default=True))
    assert await first.config.seed_platform_channel_from_env() is True
    assert await first.config.seed_platform_channel_from_env() is False

    # A later boot with DIFFERENT env must not overwrite the row.
    later = OAuthService(
        db_session_factory,
        encryption,
        _seed_env(mock_settings, default=False, secret="stale"),  # pragma: allowlist secret
    )
    assert await later.config.seed_platform_channel_from_env() is False

    rows = await _platform_rows(db_session_factory)
    assert len(rows) == 1
    row = rows[0]
    assert (row.client_id, row.channel_title, row.default_for_tenants) == (
        "seed-cid",
        "Seed Channel",
        True,
    )
    assert PLATFORM_SECRET not in row.client_secret_enc, "stored encrypted"
    assert encryption.decrypt(row.client_secret_enc) == PLATFORM_SECRET
    assert encryption.decrypt(row.refresh_token_enc) == PLATFORM_RT


async def test_seed_is_race_safe_across_two_replicas(db_engine, encryption, mock_settings):
    """Two pods booting together: separate engines (separate connections), both see
    no row, both INSERT ... ON CONFLICT DO NOTHING — one row, one winner, no error."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    engines = [create_async_engine(db_engine.url) for _ in range(2)]
    try:
        services = [
            OAuthService(
                async_sessionmaker(e, class_=AsyncSession, expire_on_commit=False),
                encryption,
                _seed_env(mock_settings),
            )
            for e in engines
        ]
        results = await asyncio.gather(
            *(s.config.seed_platform_channel_from_env() for s in services)
        )
    finally:
        for e in engines:
            await e.dispose()
    assert sorted(results) == [False, True]
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    assert len(await _platform_rows(factory)) == 1


# ---------------------------------------------------------------------------
# HTTP surface: lanes, contract shapes, secrets never returned or logged
# ---------------------------------------------------------------------------


def _make_app(oauth: OAuthService, redis, *, license_subject: str | None) -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def _identity(request, call_next):
        request.state.tenant_id = SA_TENANT
        request.state.license = (
            sa_introspection(license_subject) if license_subject is not None else None
        )
        return await call_next(request)

    for r in (router, public_router, settings_router, admin_router):
        app.include_router(r)
    app.dependency_overrides[get_oauth_service] = lambda: oauth
    app.dependency_overrides[get_redis_client] = lambda: redis
    return app


@pytest.fixture
def sa_env(monkeypatch):
    allow_orchestrator_sa(monkeypatch)


@pytest.fixture
def log_buffer():
    """Capture every ``src.*`` log record at DEBUG (configure_logging re-points the
    root handlers, so caplog cannot be trusted here)."""
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setLevel(logging.DEBUG)
    src_logger = logging.getLogger("src")
    old_level = src_logger.level
    src_logger.setLevel(logging.DEBUG)
    src_logger.addHandler(handler)
    yield buffer
    src_logger.removeHandler(handler)
    src_logger.setLevel(old_level)


OWNER_HDR = {"X-Owner-Tenant-Id": TENANT_A}


async def test_secrets_are_never_returned_or_logged(
    sa_env, oauth, db_session_factory, real_redis_client, log_buffer
):
    client = TestClient(_make_app(oauth, real_redis_client, license_subject=SA_SUBJECT))
    bodies: list[str] = []

    r = client.get("/api/admin/platform-channel")
    assert (r.status_code, r.json()) == (200, None), "no row → 200 null"

    r = client.put(
        "/api/admin/platform-channel",
        json={
            "client_id": "plat-cid",
            "client_secret": PLATFORM_SECRET,
            "refresh_token": PLATFORM_RT,
            "channel_id": "UCplat",
            "channel_title": "Platform",
            "default_for_tenants": True,
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["has_client_secret"] is True and r.json()["has_refresh_token"] is True
    bodies.append(r.text)

    r = client.put(
        "/api/settings/oauth-client",
        headers=OWNER_HDR,
        json={"client_id": "tenant-cid", "client_secret": TENANT_SECRET},
    )
    assert r.status_code == 200, r.text
    put_body = r.json()
    bodies.append(r.text)

    r = client.get("/api/settings", headers=OWNER_HDR)
    assert r.status_code == 200
    assert r.json() == put_body, "PUT returns the GET /api/settings body"
    assert r.json() == {
        "oauth_client_id": "tenant-cid",
        "has_oauth_client_secret": True,
        "redirect_uri": "http://localhost:6315/api/auth/callback",
        "platform_channel_allowed": None,
        "effective_platform_channel_allowed": True,
        "platform_channel": {"channel_id": "UCplat", "channel_title": "Platform"},
    }
    bodies.append(r.text)

    for path in ("/api/admin/platform-channel", "/api/admin/tenant-settings"):
        r = client.get(path)
        assert r.status_code == 200
        bodies.append(r.text)
    r = client.get("/api/auth/status", headers=OWNER_HDR)
    bodies.append(r.text)
    r = client.post("/api/auth/connect", headers=OWNER_HDR)
    assert r.status_code == 200
    bodies.append(r.text)
    state_value = await real_redis_client.get(f"youtube:oauth_state:{r.json()['state']}")
    bodies.append(state_value)
    r = client.delete("/api/settings/oauth-client", headers=OWNER_HDR)
    assert r.status_code == 200
    assert r.json()["oauth_client_id"] is None
    bodies.append(r.text)

    logs = log_buffer.getvalue()
    assert "tenant-cid" in logs, "the log capture is live (non-vacuous)"
    for secret in SECRETS:
        for body in bodies:
            assert secret not in body
        assert secret not in logs

    # At rest: ciphertext only.
    async with db_session_factory() as session:
        dump = json.dumps(
            [
                list(r)
                for r in (await session.execute(text("SELECT * FROM youtube_platform_channel")))
            ],
            default=str,
        )
    for secret in (PLATFORM_SECRET, PLATFORM_RT):
        assert secret not in dump


async def test_platform_channel_put_requires_secrets_to_create_and_keeps_them_on_update(
    sa_env, oauth, real_redis_client
):
    client = TestClient(_make_app(oauth, real_redis_client, license_subject=SA_SUBJECT))
    body = {
        "client_id": "plat-cid",
        "channel_id": "UCplat",
        "channel_title": "Platform",
        "default_for_tenants": False,
    }
    r = client.put("/api/admin/platform-channel", json=body)
    assert r.status_code == 422
    r = client.put(
        "/api/admin/platform-channel",
        json={**body, "client_secret": PLATFORM_SECRET, "refresh_token": PLATFORM_RT},
    )
    assert r.status_code == 200
    # Update without secrets keeps them.
    r = client.put("/api/admin/platform-channel", json={**body, "channel_title": "Renamed"})
    assert r.status_code == 200 and r.json()["channel_title"] == "Renamed"
    platform = await oauth.config.platform_channel()
    assert platform is not None
    assert (platform.client_secret, platform.refresh_token) == (PLATFORM_SECRET, PLATFORM_RT)
    # Changing client_id without new secrets is refused (a token is bound to its client).
    r = client.put("/api/admin/platform-channel", json={**body, "client_id": "other-cid"})
    assert r.status_code == 422


async def test_tenant_platform_permission_and_listing(sa_env, oauth, real_redis_client):
    client = TestClient(_make_app(oauth, real_redis_client, license_subject=SA_SUBJECT))
    await oauth.store_tokens(TENANT_B, _creds(expired=False))

    r = client.put(f"/api/admin/tenants/{TENANT_A}/platform-channel", json={"allowed": False})
    assert r.status_code == 200, r.text
    assert r.json() == {
        "tenant_id": TENANT_A,
        "platform_channel_allowed": False,
        "effective_platform_channel_allowed": False,
    }
    assert client.put(f"/api/admin/tenants/{TENANT_A}/platform-channel", json={}).status_code == 422
    assert (
        client.put("/api/admin/tenants/not-a-uuid/platform-channel", json={"allowed": True})
    ).status_code == 422

    r = client.get("/api/admin/tenant-settings")
    assert r.status_code == 200
    by_tenant = {row["tenant_id"]: row for row in r.json()}
    assert by_tenant == {
        TENANT_A: {
            "tenant_id": TENANT_A,
            "platform_channel_allowed": False,
            "has_grant": False,
            "has_oauth_client": False,
        },
        TENANT_B: {
            "tenant_id": TENANT_B,
            "platform_channel_allowed": None,
            "has_grant": True,
            "has_oauth_client": False,
        },
    }

    # null → follow the default again.
    r = client.put(f"/api/admin/tenants/{TENANT_A}/platform-channel", json={"allowed": None})
    assert r.status_code == 200 and r.json()["platform_channel_allowed"] is None


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/api/admin/platform-channel", None),
        ("GET", "/api/admin/tenant-settings", None),
        ("PUT", f"/api/admin/tenants/{TENANT_A}/platform-channel", {"allowed": True}),
    ],
)
async def test_operator_routes_reject_owner_scoped_and_non_sa_callers(
    sa_env, oauth, db_session_factory, real_redis_client, method, path, body
):
    sa = TestClient(_make_app(oauth, real_redis_client, license_subject=SA_SUBJECT))
    # Owner-tenant-scoped call → 403 (a tenant proxy path must never reach these).
    r = sa.request(method, path, headers=OWNER_HDR, json=body)
    assert r.status_code == 403, r.text
    # A customer licence (not a delegating subject) → 403.
    other = TestClient(_make_app(oauth, real_redis_client, license_subject="customer-subject"))
    assert other.request(method, path, json=body).status_code == 403
    # Keycloak-JWT lane (no licence introspection) → 403.
    jwt = TestClient(_make_app(oauth, real_redis_client, license_subject=None))
    assert jwt.request(method, path, json=body).status_code == 403
    # Nothing was written by any refused call.
    assert await _settings_row(db_session_factory, TENANT_A) is None
    # The SA without an owner tenant is admitted.
    assert sa.request(method, path, json=body).status_code == 200


async def test_settings_routes_reject_non_sa_and_require_owner(sa_env, oauth, real_redis_client):
    other = TestClient(_make_app(oauth, real_redis_client, license_subject="customer-subject"))
    r = other.put(
        "/api/settings/oauth-client",
        headers=OWNER_HDR,
        json={"client_id": "x", "client_secret": TENANT_SECRET},
    )
    assert r.status_code == 403
    sa = TestClient(_make_app(oauth, real_redis_client, license_subject=SA_SUBJECT))
    assert sa.get("/api/settings").status_code == 400  # SA but no owner tenant


# ---------------------------------------------------------------------------
# Connect flow with the tenant's own client
# ---------------------------------------------------------------------------


async def test_connect_uses_the_tenant_client_and_callback_records_it(
    sa_env, oauth, db_session_factory, real_redis_client
):
    await oauth.config.set_oauth_client(TENANT_A, "tenant-cid", TENANT_SECRET, "admin")
    client = TestClient(_make_app(oauth, real_redis_client, license_subject=SA_SUBJECT))

    r = client.post("/api/auth/connect", headers=OWNER_HDR)
    assert r.status_code == 200, r.text
    query = parse_qs(urlparse(r.json()["authorization_url"]).query)
    assert query["client_id"] == ["tenant-cid"]
    state = r.json()["state"]

    used: list[tuple[str, str]] = []

    def _exchange(code, st, c):
        used.append((c.client_id, c.client_secret))
        return _creds(expired=False)

    with (
        patch.object(oauth, "exchange_code", side_effect=_exchange),
        patch("googleapiclient.discovery.build", side_effect=RuntimeError("no network")),
    ):
        cb = client.get(f"/api/auth/callback?code=c&state={state}")
    assert cb.status_code == 200, cb.text
    assert used == [("tenant-cid", TENANT_SECRET)], "exchanged with the client that started it"
    row = await _grant(db_session_factory, TENANT_A)
    assert row is not None and row.oauth_client_id == "tenant-cid"
    status = client.get("/api/auth/status", headers=OWNER_HDR).json()
    assert (status["mode"], status["status"], status["needs_reconnect"]) == (
        "tenant",
        "connected",
        False,
    )


async def test_callback_refuses_when_the_client_changed_mid_flow(
    sa_env, oauth, db_session_factory, real_redis_client
):
    await oauth.config.set_oauth_client(TENANT_A, "tenant-cid", TENANT_SECRET, "admin")
    client = TestClient(_make_app(oauth, real_redis_client, license_subject=SA_SUBJECT))
    state = client.post("/api/auth/connect", headers=OWNER_HDR).json()["state"]
    await oauth.config.set_oauth_client(TENANT_A, "swapped-cid", TENANT_SECRET, "admin")

    with patch.object(oauth, "exchange_code") as exchange:
        cb = client.get(f"/api/auth/callback?code=c&state={state}")
    assert cb.status_code == 409
    exchange.assert_not_called()
    assert await _grant(db_session_factory, TENANT_A) is None


async def test_callback_refuses_platform_flow_once_the_tenant_set_its_own_client(
    sa_env, oauth, db_session_factory, real_redis_client
):
    """A flow STARTED with the platform client must not complete after the tenant
    configured its own client: setting a client flags every platform-issued grant
    reconnect-required, so a callback landing just after it must not store a fresh,
    unflagged platform grant that silently bypasses the tenant's chosen client."""
    client = TestClient(_make_app(oauth, real_redis_client, license_subject=SA_SUBJECT))
    state = client.post("/api/auth/connect", headers=OWNER_HDR).json()["state"]
    await oauth.config.set_oauth_client(TENANT_A, "tenant-cid", TENANT_SECRET, "admin")

    with patch.object(oauth, "exchange_code") as exchange:
        cb = client.get(f"/api/auth/callback?code=c&state={state}")
    assert cb.status_code == 409
    exchange.assert_not_called()
    assert await _grant(db_session_factory, TENANT_A) is None


# ---------------------------------------------------------------------------
# RLS isolation of tenant_youtube_settings + the cross-tenant operator listing
# ---------------------------------------------------------------------------

_ROLE = "yt_tenant_cfg_rls_test"
_ROLE_PW = "yt-tenant-cfg-rls-test"  # pragma: allowlist secret


async def test_tenant_settings_are_rls_isolated_and_listing_spans_tenants(
    db_engine, oauth, monkeypatch
):
    await oauth.config.set_oauth_client(TENANT_A, "a-cid", TENANT_SECRET, "admin")
    await oauth.config.set_oauth_client(TENANT_B, "b-cid", TENANT_SECRET, "admin")

    async with db_engine.begin() as conn:
        try:
            await conn.execute(text(f'DROP ROLE IF EXISTS "{_ROLE}"'))
            await conn.execute(
                text(f"CREATE ROLE \"{_ROLE}\" LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD '{_ROLE_PW}'")
            )
        except ProgrammingError as exc:
            pytest.skip(f"cannot CREATE ROLE (needs superuser/CREATEROLE): {exc.orig}")
        await conn.execute(
            text(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON tenant_youtube_settings, oauth_tokens, "
                f'youtube_platform_channel TO "{_ROLE}"'
            )
        )
        await conn.execute(
            text(f'GRANT EXECUTE ON FUNCTION list_tenant_youtube_settings() TO "{_ROLE}"')
        )

    monkeypatch.setenv("TENANT_RLS_MODE", "enforce")
    restricted = create_async_engine(
        make_url(str(db_engine.url)).set(username=_ROLE, password=_ROLE_PW)
    )
    try:
        async with restricted.begin() as conn:
            # No GUC → fail closed.
            n = (await conn.execute(text("SELECT count(*) FROM tenant_youtube_settings"))).scalar()
            assert n == 0
            await conn.execute(
                text("SELECT set_config('app.current_tenant', :t, true)"), {"t": TENANT_A}
            )
            rows = (
                await conn.execute(
                    text("SELECT tenant_id, oauth_client_id FROM tenant_youtube_settings")
                )
            ).all()
            assert [(str(t), c) for t, c in rows] == [(TENANT_A, "a-cid")], "A cannot read B"
            # A cannot touch B's row (invisible → 0 rows) nor insert one stamped B.
            touched = (
                await conn.execute(
                    text(
                        "UPDATE tenant_youtube_settings SET platform_channel_allowed = true "
                        "WHERE tenant_id = :t RETURNING 1"
                    ),
                    {"t": TENANT_B},
                )
            ).all()
            assert touched == []
            await conn.execute(text("SAVEPOINT s"))
            with pytest.raises(DBAPIError, match="row-level security"):
                await conn.execute(
                    text("INSERT INTO tenant_youtube_settings (tenant_id) VALUES (:t)"),
                    {"t": tenant_uuid("yt-cfg-c")},
                )
            await conn.execute(text("ROLLBACK TO SAVEPOINT s"))

        # The operator listing (no GUC bound) sees every tenant via the definer fn.
        async with restricted.begin() as conn:
            listed = (
                (await conn.execute(text("SELECT tenant_id FROM list_tenant_youtube_settings()")))
                .scalars()
                .all()
            )
            assert sorted(str(t) for t in listed) == sorted([TENANT_A, TENANT_B])
    finally:
        await restricted.dispose()
        async with db_engine.begin() as conn:
            await conn.execute(text(f'DROP OWNED BY "{_ROLE}"'))
            await conn.execute(text(f'DROP ROLE IF EXISTS "{_ROLE}"'))


# ---------------------------------------------------------------------------
# Migration literal == create_all source
# ---------------------------------------------------------------------------


async def test_migration_list_function_matches_the_create_all_source():
    path = (
        Path(__file__).resolve().parents[2]
        / "src/database/migrations/versions/014_tenant_youtube_settings.py"
    )
    spec = importlib.util.spec_from_file_location("mig014", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.LIST_FN_SQL == LIST_TENANT_SETTINGS_FN_SQL
    assert len(module.revision) <= 32
