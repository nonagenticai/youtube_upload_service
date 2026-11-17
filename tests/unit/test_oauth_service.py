"""Tests for OAuthService — token lifecycle management.

Uses a real postgres+psycopg database via conftest fixtures to verify actual
SQL operations (INSERT, UPDATE, SELECT, DELETE) instead of mocked sessions.
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import MagicMock, patch

import pytest
from google.auth.exceptions import RefreshError, TransportError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.database.models import OAuthToken
from src.services.encryption_service import EncryptionService
from src.services.oauth_service import (
    STATUS_CONNECTED,
    STATUS_NOT_CONNECTED,
    STATUS_PLATFORM_DEFAULT,
    STATUS_RECONNECT_REQUIRED,
    OAuthService,
    YouTubeNotConnectedError,
    YouTubePlatformChannelUnavailableError,
    YouTubeReconnectRequiredError,
    YouTubeTokenRefreshUnavailableError,
)
from tests.platform_channel import seed_platform_channel
from tests.tenants import tenant_uuid

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def encryption_service():
    """EncryptionService with encryption disabled (dev mode)."""
    return EncryptionService(key="")


@pytest.fixture
def oauth_service(
    db_session_factory: async_sessionmaker[AsyncSession],
    encryption_service: EncryptionService,
    mock_settings,
):
    return OAuthService(
        session_factory=db_session_factory,
        encryption=encryption_service,
        settings=mock_settings,
    )


def _mock_credentials(
    *,
    token: str = "access-token-123",
    refresh_token: str = "refresh-token-456",
    token_uri: str = "https://oauth2.googleapis.com/token",
    scopes: list[str] | None = None,
    expiry: dt.datetime | None = None,
) -> MagicMock:
    """Build a mock google.oauth2.credentials.Credentials."""
    creds = MagicMock()
    creds.token = token
    creds.refresh_token = refresh_token
    creds.token_uri = token_uri
    creds.scopes = scopes or ["https://www.googleapis.com/auth/youtube.upload"]
    creds.expiry = expiry
    return creds


# ---------------------------------------------------------------------------
# Tests — build_flow / build_authorization_url
# ---------------------------------------------------------------------------


class TestBuildFlow:
    def test_build_flow_returns_flow_object(self, oauth_service: OAuthService):
        flow = oauth_service.build_flow()
        assert flow is not None
        assert flow.redirect_uri == "http://localhost:6315/api/auth/callback"

    def test_build_flow_with_state(self, oauth_service: OAuthService):
        flow = oauth_service.build_flow(state="test-state")
        assert flow is not None

    def test_build_authorization_url(self, oauth_service: OAuthService):
        url = oauth_service.build_authorization_url(state="test-state-123")
        assert "accounts.google.com" in url
        assert "test-state-123" in url
        assert "offline" in url or "access_type" in url


# ---------------------------------------------------------------------------
# Tests — store_tokens (INSERT)
# ---------------------------------------------------------------------------


class TestStoreTokens:
    @pytest.mark.asyncio
    async def test_store_tokens_creates_new_row(
        self,
        oauth_service: OAuthService,
        db_session: AsyncSession,
    ):
        creds = _mock_credentials()

        await oauth_service.store_tokens(
            tenant_uuid("tenant-1"), creds, channel_id="UC123", channel_title="Test Channel"
        )

        # Verify the row in the database directly
        result = await db_session.execute(
            select(OAuthToken).where(OAuthToken.tenant_id == tenant_uuid("tenant-1"))
        )
        row = result.scalar_one_or_none()
        assert row is not None
        assert row.tenant_id == tenant_uuid("tenant-1")
        assert row.access_token == "access-token-123"
        assert row.refresh_token == "refresh-token-456"
        assert row.token_uri == "https://oauth2.googleapis.com/token"
        assert row.scopes == "https://www.googleapis.com/auth/youtube.upload"
        assert row.channel_id == "UC123"
        assert row.channel_title == "Test Channel"

    @pytest.mark.asyncio
    async def test_store_tokens_updates_existing_row(
        self,
        oauth_service: OAuthService,
        db_session: AsyncSession,
    ):
        # Insert initial credentials
        creds_v1 = _mock_credentials(
            token="old-access",
            refresh_token="old-refresh",
        )
        await oauth_service.store_tokens(
            tenant_uuid("tenant-1"), creds_v1, channel_id="UC111", channel_title="Old Channel"
        )

        # Update with new credentials
        creds_v2 = _mock_credentials(
            token="new-access",
            refresh_token="new-refresh",
            scopes=["https://www.googleapis.com/auth/youtube.readonly"],
        )
        await oauth_service.store_tokens(
            tenant_uuid("tenant-1"), creds_v2, channel_id="UC222", channel_title="New Channel"
        )

        # Verify upsert: only 1 row, with updated values
        result = await db_session.execute(
            select(OAuthToken).where(OAuthToken.tenant_id == tenant_uuid("tenant-1"))
        )
        rows = result.scalars().all()
        assert len(rows) == 1

        row = rows[0]
        assert row.access_token == "new-access"
        assert row.refresh_token == "new-refresh"
        assert row.scopes == "https://www.googleapis.com/auth/youtube.readonly"
        assert row.channel_id == "UC222"
        assert row.channel_title == "New Channel"


# ---------------------------------------------------------------------------
# Tests — load_credentials
# ---------------------------------------------------------------------------


class TestLoadCredentials:
    @pytest.mark.asyncio
    async def test_load_credentials_refuses_when_not_stored(
        self,
        oauth_service: OAuthService,
    ):
        with pytest.raises(YouTubeNotConnectedError, match="connect YouTube"):
            await oauth_service.load_credentials(tenant_uuid("tenant-nonexistent"))

    @pytest.mark.asyncio
    async def test_load_credentials_returns_credentials(
        self,
        oauth_service: OAuthService,
    ):
        """Prove the full encrypt-store-load-decrypt roundtrip works."""
        original = _mock_credentials(
            token="my-access-token",
            refresh_token="my-refresh-token",
            scopes=[
                "https://www.googleapis.com/auth/youtube.upload",
                "https://www.googleapis.com/auth/youtube.readonly",
            ],
        )
        await oauth_service.store_tokens(
            tenant_uuid("tenant-rt"), original, channel_id="UC999", channel_title="Roundtrip"
        )

        creds = await oauth_service.load_credentials(tenant_uuid("tenant-rt"))
        assert creds is not None
        assert creds.token == "my-access-token"
        assert creds.refresh_token == "my-refresh-token"
        assert creds.token_uri == "https://oauth2.googleapis.com/token"
        assert creds.scopes is not None
        assert set(creds.scopes) == {
            "https://www.googleapis.com/auth/youtube.upload",
            "https://www.googleapis.com/auth/youtube.readonly",
        }

    @pytest.mark.asyncio
    async def test_load_credentials_refreshes_expired_tokens(
        self,
        oauth_service: OAuthService,
        db_session: AsyncSession,
    ):
        """Store an expired token, verify refresh is called and re-stored."""
        expired_time = dt.datetime(2020, 1, 1, tzinfo=dt.UTC)
        creds = _mock_credentials(
            token="old-access",
            refresh_token="my-refresh",
            expiry=expired_time,
        )
        await oauth_service.store_tokens(
            tenant_uuid("tenant-exp"), creds, channel_id="UC123", channel_title="Expired"
        )

        with (
            patch("src.services.oauth_service.GoogleAuthRequest"),
            patch("google.oauth2.credentials.Credentials.refresh") as mock_refresh,
        ):
            loaded = await oauth_service.load_credentials(tenant_uuid("tenant-exp"))

        assert loaded is not None
        mock_refresh.assert_called_once()


# ---------------------------------------------------------------------------
# Tests — get_token_info
# ---------------------------------------------------------------------------


class TestGetTokenInfo:
    @pytest.mark.asyncio
    async def test_get_token_info_reports_not_connected(
        self,
        oauth_service: OAuthService,
    ):
        info = await oauth_service.get_token_info(tenant_uuid("tenant-none"))
        assert info == {
            "tenant_id": tenant_uuid("tenant-none"),
            "status": STATUS_NOT_CONNECTED,
            "mode": "none",
            "needs_reconnect": False,
        }

    @pytest.mark.asyncio
    async def test_get_token_info_returns_metadata(
        self,
        oauth_service: OAuthService,
    ):
        creds = _mock_credentials(
            scopes=["scope1", "scope2"],
        )
        await oauth_service.store_tokens(
            tenant_uuid("tenant-info"), creds, channel_id="UC123", channel_title="My Channel"
        )

        info = await oauth_service.get_token_info(tenant_uuid("tenant-info"))
        assert info is not None
        assert info["tenant_id"] == tenant_uuid("tenant-info")
        assert info["status"] == STATUS_CONNECTED
        assert info["channel_id"] == "UC123"
        assert info["channel_title"] == "My Channel"
        assert info["scopes"] == ["scope1", "scope2"]
        # token_expiry should be None since we didn't set an expiry
        assert info["token_expiry"] is None


# ---------------------------------------------------------------------------
# Tests — platform channel (no per-tenant row) is an explicit operator opt-in
# ---------------------------------------------------------------------------


async def _platform_service(
    db_session_factory: async_sessionmaker[AsyncSession],
    encryption_service: EncryptionService,
    mock_settings,
    *,
    as_default: bool,
) -> OAuthService:
    """OAuthService with the platform channel row CONFIGURED; ``as_default`` is its
    ``default_for_tenants`` (the per-tenant setting stays NULL = follow it)."""
    await seed_platform_channel(
        db_session_factory, default_for_tenants=as_default, encryption=encryption_service
    )
    return OAuthService(
        session_factory=db_session_factory,
        encryption=encryption_service,
        settings=mock_settings,
    )


class TestPlatformChannel:
    @pytest.fixture
    async def platform_default_service(
        self, db_session_factory, encryption_service, mock_settings
    ) -> OAuthService:
        return await _platform_service(
            db_session_factory, encryption_service, mock_settings, as_default=True
        )

    @pytest.fixture
    async def configured_not_default_service(
        self, db_session_factory, encryption_service, mock_settings
    ) -> OAuthService:
        return await _platform_service(
            db_session_factory, encryption_service, mock_settings, as_default=False
        )

    async def test_no_platform_row_means_no_fallback(self, oauth_service: OAuthService):
        """A deployment that configured nothing has no platform row → no fallback,
        and the seed env default is off."""
        assert oauth_service._settings.youtube.platform_channel_as_default is False
        assert await oauth_service.config.platform_channel() is None
        with pytest.raises(YouTubeNotConnectedError):
            await oauth_service.resolve_credentials(tenant_uuid("tenant-nothing"))

    @pytest.mark.asyncio
    async def test_no_row_with_operator_opt_in_uses_platform_channel(
        self, platform_default_service: OAuthService
    ):
        with (
            patch("src.services.oauth_service.GoogleAuthRequest"),
            patch("google.oauth2.credentials.Credentials.refresh") as mock_refresh,
        ):
            creds = await platform_default_service.load_credentials(
                tenant_uuid("tenant-without-row")
            )
        assert creds.refresh_token == "fallback-rt"
        assert creds.client_id == "fallback-cid"
        mock_refresh.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_row_without_opt_in_refuses_even_though_platform_is_configured(
        self, configured_not_default_service: OAuthService
    ):
        """Configured platform credentials are NOT consent to publish a tenant's video
        to our channel. Old code returned the platform credentials here."""
        with (
            patch("src.services.oauth_service.GoogleAuthRequest"),
            patch("google.oauth2.credentials.Credentials.refresh") as mock_refresh,
            pytest.raises(YouTubeNotConnectedError) as exc_info,
        ):
            await configured_not_default_service.load_credentials(tenant_uuid("tenant-no-row"))
        assert "connect YouTube" in str(exc_info.value)
        assert exc_info.value.error_code == "youtube_not_connected"
        mock_refresh.assert_not_called()  # the platform grant was never even touched

    @pytest.mark.asyncio
    async def test_resolve_reports_platform_channel_for_no_grant(
        self, platform_default_service: OAuthService
    ):
        with (
            patch("src.services.oauth_service.GoogleAuthRequest"),
            patch("google.oauth2.credentials.Credentials.refresh"),
        ):
            creds, via = await platform_default_service.resolve_credentials(
                tenant_uuid("tenant-without-row")
            )
        assert via == "platform"
        assert creds.refresh_token == "fallback-rt"

    @pytest.mark.asyncio
    async def test_resolve_without_platform_refuses_no_grant_even_with_opt_in(
        self, platform_default_service: OAuthService
    ):
        """``allow_platform=False`` is for videos on the TENANT's channel: the
        platform channel would be the wrong channel, so no fallback."""
        with (
            patch("google.oauth2.credentials.Credentials.refresh") as mock_refresh,
            pytest.raises(YouTubeNotConnectedError),
        ):
            await platform_default_service.resolve_credentials(
                tenant_uuid("tenant-without-row"), allow_platform=False
            )
        mock_refresh.assert_not_called()

    @pytest.mark.asyncio
    async def test_platform_credentials_for_published_ignore_the_opt_in_flag(
        self, configured_not_default_service: OAuthService
    ):
        """The flag governs NEW uploads; a video already on the platform channel
        stays reachable while the credential is configured."""
        with (
            patch("src.services.oauth_service.GoogleAuthRequest"),
            patch("google.oauth2.credentials.Credentials.refresh"),
        ):
            creds = await configured_not_default_service.platform_credentials_for_published(
                "Upload job j1"
            )
        assert creds.refresh_token == "fallback-rt"

    @pytest.mark.asyncio
    async def test_platform_credentials_for_published_unconfigured_is_clear(
        self, oauth_service: OAuthService
    ):
        with pytest.raises(YouTubePlatformChannelUnavailableError) as exc_info:
            await oauth_service.platform_credentials_for_published("Upload job j1")
        assert "PLATFORM YouTube channel" in str(exc_info.value)
        assert exc_info.value.error_code == "youtube_platform_channel_unavailable"

    @pytest.mark.asyncio
    async def test_status_platform_default_is_not_reported_as_connected(
        self, platform_default_service: OAuthService
    ):
        info = await platform_default_service.get_token_info(tenant_uuid("tenant-without-row"))
        assert info["status"] == STATUS_PLATFORM_DEFAULT
        assert info["channel_title"] == "George Dekker"
        assert info["channel_id"] == "UCfallback"

    @pytest.mark.asyncio
    async def test_status_without_opt_in_is_not_connected(
        self, configured_not_default_service: OAuthService
    ):
        info = await configured_not_default_service.get_token_info(tenant_uuid("tenant-no-row"))
        assert info["status"] == STATUS_NOT_CONNECTED
        assert "channel_id" not in info  # the platform channel is not advertised

    @pytest.mark.asyncio
    async def test_per_tenant_row_takes_precedence_over_platform(
        self, platform_default_service: OAuthService
    ):
        creds = _mock_credentials(scopes=["s"])
        await platform_default_service.store_tokens(
            tenant_uuid("tenant-own"), creds, channel_id="UCown", channel_title="Their Own Channel"
        )
        info = await platform_default_service.get_token_info(tenant_uuid("tenant-own"))
        assert info["status"] == STATUS_CONNECTED
        assert info["channel_title"] == "Their Own Channel"  # not the platform channel

    @pytest.mark.asyncio
    async def test_nothing_configured_refuses(self, oauth_service: OAuthService):
        with pytest.raises(YouTubeNotConnectedError):
            await oauth_service.load_credentials(tenant_uuid("nobody"))
        info = await oauth_service.get_token_info(tenant_uuid("nobody"))
        assert info["status"] == STATUS_NOT_CONNECTED

    @pytest.mark.asyncio
    async def test_dead_platform_credential_raises_an_actionable_error(
        self, platform_default_service: OAuthService
    ):
        """The platform credential is what every grant-less tenant rides under the
        opt-in. When it dies the error must name it and the env var to rotate."""

        def _refresh(_self_creds, _request):
            raise RefreshError("invalid_grant: Bad Request")

        with (
            patch("src.services.oauth_service.GoogleAuthRequest"),
            patch("google.oauth2.credentials.Credentials.refresh", _refresh),
            pytest.raises(RuntimeError) as exc_info,
        ):
            await platform_default_service.load_credentials(tenant_uuid("tenant-no-row"))

        message = str(exc_info.value)
        assert "PUT /api/admin/platform-channel" in message
        assert "fallback-cid" in message
        assert isinstance(exc_info.value.__cause__, RefreshError)


# ---------------------------------------------------------------------------
# Tests — delete_tokens / has_tokens
# ---------------------------------------------------------------------------


class TestDeleteTokens:
    @pytest.mark.asyncio
    async def test_delete_tokens_removes_stored_tokens(
        self,
        oauth_service: OAuthService,
        db_session: AsyncSession,
    ):
        creds = _mock_credentials()
        await oauth_service.store_tokens(
            tenant_uuid("tenant-del"), creds, channel_id="UC123", channel_title="Delete Me"
        )

        # Verify it exists first
        result = await db_session.execute(
            select(OAuthToken).where(OAuthToken.tenant_id == tenant_uuid("tenant-del"))
        )
        assert result.scalar_one_or_none() is not None

        deleted = await oauth_service.delete_tokens(tenant_uuid("tenant-del"))
        assert deleted is True

        # Verify it is gone
        result = await db_session.execute(
            select(OAuthToken).where(OAuthToken.tenant_id == tenant_uuid("tenant-del"))
        )
        assert result.scalar_one_or_none() is None

    @pytest.mark.asyncio
    async def test_delete_tokens_returns_false_when_not_found(
        self,
        oauth_service: OAuthService,
    ):
        deleted = await oauth_service.delete_tokens(tenant_uuid("tenant-nonexistent"))
        assert deleted is False


class TestHasTokens:
    @pytest.mark.asyncio
    async def test_has_tokens_false_for_new_tenant(
        self,
        oauth_service: OAuthService,
    ):
        assert await oauth_service.has_tokens(tenant_uuid("tenant-new")) is False

    @pytest.mark.asyncio
    async def test_has_tokens_true_after_storing(
        self,
        oauth_service: OAuthService,
    ):
        creds = _mock_credentials()
        await oauth_service.store_tokens(tenant_uuid("tenant-has"), creds)

        assert await oauth_service.has_tokens(tenant_uuid("tenant-has")) is True


# ---------------------------------------------------------------------------
# Regression: store_tokens must be an atomic upsert
# ---------------------------------------------------------------------------


class TestStoreTokensIsAtomic:
    """``store_tokens`` documents itself as an "upsert" but is a non-atomic
    read-modify-write: SELECT the row, then either ``session.add`` a new one or
    mutate the existing one, then commit.

    Two callers that arrive for the same tenant before either has committed
    both see ``None`` and both INSERT. The second one loses to
    ``uq_oauth_tokens_tenant_id`` and raises ``IntegrityError``. On
    ``GET /api/auth/callback`` that surfaces as a 500 *after* the one-time
    authorization code and the one-time Redis state token have already been
    consumed (auth_routes.py:128/132), so the tenant's YouTube connection is
    lost and the whole consent flow has to be restarted. Two browser tabs, or a
    duplicated redirect delivery, is all it takes.

    The fix is a real upsert (``INSERT … ON CONFLICT (tenant_id) DO UPDATE``),
    which is atomic in one statement.
    """

    @pytest.mark.asyncio
    async def test_precondition_sequential_store_upserts_cleanly(
        self, oauth_service: OAuthService, db_session: AsyncSession
    ):
        """PRECONDITION: two *sequential* stores for one tenant are fine, so a
        failure below is about concurrency, not about the fixture."""
        await oauth_service.store_tokens(tenant_uuid("t-seq"), _mock_credentials(token="a1"))
        await oauth_service.store_tokens(tenant_uuid("t-seq"), _mock_credentials(token="a2"))

        rows = (
            (
                await db_session.execute(
                    select(OAuthToken).where(OAuthToken.tenant_id == tenant_uuid("t-seq"))
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].access_token == "a2"

    @pytest.mark.asyncio
    async def test_concurrent_first_connect_does_not_raise(
        self, oauth_service: OAuthService, db_session: AsyncSession
    ):
        """Two concurrent first-time stores for one tenant must converge on a
        single row, not blow up on the unique constraint."""
        import asyncio

        pre = (
            await db_session.execute(
                select(OAuthToken).where(OAuthToken.tenant_id == tenant_uuid("t-conc"))
            )
        ).scalar_one_or_none()
        assert pre is None, "precondition: tenant must start with no stored row"

        await asyncio.gather(
            oauth_service.store_tokens(
                tenant_uuid("t-conc"), _mock_credentials(token="a1", refresh_token="r1")
            ),
            oauth_service.store_tokens(
                tenant_uuid("t-conc"), _mock_credentials(token="a2", refresh_token="r2")
            ),
        )

        rows = (
            (
                await db_session.execute(
                    select(OAuthToken).where(OAuthToken.tenant_id == tenant_uuid("t-conc"))
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# Tests — a dead tenant grant is flagged reconnect-required, NEVER re-routed
# ---------------------------------------------------------------------------


class TestRefreshErrorHandling:
    """A tenant whose own grant Google refuses used to have its row DELETED and its
    video published to the platform channel, with only a log line saying so. Now
    the row is kept and flagged, and the upload fails with "reconnect YouTube" —
    even when the operator opted in to the platform channel (that opt-in is for
    tenants that never connected, not for tenants whose connection died).
    """

    @pytest.fixture
    async def service(self, db_session_factory, encryption_service, mock_settings) -> OAuthService:
        # Platform channel configured AND default: the most tempting setup for a
        # silent fallback, so the refusal is proven where it matters.
        return await _platform_service(
            db_session_factory, encryption_service, mock_settings, as_default=True
        )

    async def _store_expired(self, service: OAuthService, slug: str) -> None:
        await service.store_tokens(
            tenant_uuid(slug),
            _mock_credentials(
                refresh_token="revoked-rt", expiry=dt.datetime(2020, 1, 1, tzinfo=dt.UTC)
            ),
            channel_id="UCdead",
            channel_title="Tenant Channel",
        )

    @pytest.mark.asyncio
    async def test_refused_tenant_grant_raises_reconnect_and_never_uses_platform(
        self, service: OAuthService
    ):
        await self._store_expired(service, "tenant-dead")
        calls: list[object] = []

        def _refresh(self_creds, _request):
            calls.append(self_creds.refresh_token)
            raise RefreshError("invalid_grant: Token has been expired or revoked.")

        with (
            patch("src.services.oauth_service.GoogleAuthRequest"),
            patch("google.oauth2.credentials.Credentials.refresh", _refresh),
            pytest.raises(YouTubeReconnectRequiredError) as exc_info,
        ):
            await service.load_credentials(tenant_uuid("tenant-dead"))

        # Only the tenant's grant was tried — the platform grant was never refreshed.
        assert calls == ["revoked-rt"]
        assert "reconnect YouTube" in str(exc_info.value)
        assert "invalid_grant" in str(exc_info.value)
        assert exc_info.value.error_code == "youtube_reconnect_required"

    @pytest.mark.asyncio
    async def test_refused_grant_row_is_kept_and_flagged(
        self, service: OAuthService, db_session: AsyncSession
    ):
        await self._store_expired(service, "tenant-flag")

        def _refresh(_self_creds, _request):
            raise RefreshError("invalid_grant")

        with (
            patch("src.services.oauth_service.GoogleAuthRequest"),
            patch("google.oauth2.credentials.Credentials.refresh", _refresh),
            pytest.raises(YouTubeReconnectRequiredError),
        ):
            await service.load_credentials(tenant_uuid("tenant-flag"))

        row = (
            await db_session.execute(
                select(OAuthToken).where(OAuthToken.tenant_id == tenant_uuid("tenant-flag"))
            )
        ).scalar_one_or_none()
        assert row is not None, "the tenant's grant must not be silently deleted"
        assert row.needs_reconnect is True
        assert row.reconnect_reason == "invalid_grant"

        info = await service.get_token_info(tenant_uuid("tenant-flag"))
        assert info["status"] == STATUS_RECONNECT_REQUIRED
        assert info["channel_title"] == "Tenant Channel"  # theirs, not the platform's

    @pytest.mark.asyncio
    async def test_flagged_grant_is_refused_without_calling_google(self, service: OAuthService):
        await self._store_expired(service, "tenant-flagged")

        def _refuse(_self_creds, _request):
            raise RefreshError("invalid_grant")

        with (
            patch("src.services.oauth_service.GoogleAuthRequest"),
            patch("google.oauth2.credentials.Credentials.refresh", _refuse),
            pytest.raises(YouTubeReconnectRequiredError),
        ):
            await service.load_credentials(tenant_uuid("tenant-flagged"))

        with (
            patch("src.services.oauth_service.GoogleAuthRequest"),
            patch("google.oauth2.credentials.Credentials.refresh") as mock_refresh,
            pytest.raises(YouTubeReconnectRequiredError),
        ):
            await service.load_credentials(tenant_uuid("tenant-flagged"))
        mock_refresh.assert_not_called()

    @pytest.mark.asyncio
    async def test_reconnecting_clears_the_flag(self, service: OAuthService):
        await self._store_expired(service, "tenant-heal")

        def _refuse(_self_creds, _request):
            raise RefreshError("invalid_grant")

        with (
            patch("src.services.oauth_service.GoogleAuthRequest"),
            patch("google.oauth2.credentials.Credentials.refresh", _refuse),
            pytest.raises(YouTubeReconnectRequiredError),
        ):
            await service.load_credentials(tenant_uuid("tenant-heal"))

        # The consent callback stores a fresh, unexpired grant.
        await service.store_tokens(
            tenant_uuid("tenant-heal"), _mock_credentials(refresh_token="fresh-rt")
        )
        info = await service.get_token_info(tenant_uuid("tenant-heal"))
        assert info["status"] == STATUS_CONNECTED
        assert info["reconnect_reason"] is None
        creds = await service.load_credentials(tenant_uuid("tenant-heal"))
        assert creds.refresh_token == "fresh-rt"

    @pytest.mark.asyncio
    async def test_transport_error_is_transient_not_flagged_and_not_rerouted(
        self, service: OAuthService
    ):
        """``TransportError`` (network blip to Google's token endpoint) is a SIBLING
        of ``RefreshError``. It says nothing about the grant: no flag, no fallback."""
        await self._store_expired(service, "tenant-transport")
        calls: list[object] = []

        def _refresh(self_creds, _request):
            calls.append(self_creds.refresh_token)
            raise TransportError("connection reset while refreshing token")

        with (
            patch("src.services.oauth_service.GoogleAuthRequest"),
            patch("google.oauth2.credentials.Credentials.refresh", _refresh),
            pytest.raises(YouTubeTokenRefreshUnavailableError),
        ):
            await service.load_credentials(tenant_uuid("tenant-transport"))

        assert calls == ["revoked-rt"]
        info = await service.get_token_info(tenant_uuid("tenant-transport"))
        assert info["status"] == STATUS_CONNECTED

    @pytest.mark.asyncio
    async def test_flag_write_failure_still_raises_reconnect(self, service: OAuthService):
        """Marking is best-effort: a DB error while flagging must not replace the
        reconnect error with a DB error, and must not fall back either."""
        await self._store_expired(service, "tenant-flag-fails")

        def _refuse(_self_creds, _request):
            raise RefreshError("invalid_grant")

        real_factory = service._session_factory
        calls = {"n": 0}

        def _factory():
            calls["n"] += 1
            if calls["n"] == 2:  # 1 = the load's read session, 2 = the flag write
                raise OSError("db down")
            return real_factory()

        with (
            patch.object(service, "_session_factory", _factory),
            patch("src.services.oauth_service.GoogleAuthRequest"),
            patch("google.oauth2.credentials.Credentials.refresh", _refuse),
            pytest.raises(YouTubeReconnectRequiredError),
        ):
            await service.load_credentials(tenant_uuid("tenant-flag-fails"))
        assert calls["n"] == 2
