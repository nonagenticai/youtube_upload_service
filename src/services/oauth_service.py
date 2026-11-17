"""OAuth2 token lifecycle management for YouTube API.

Handles storing, loading, refreshing, and deleting per-tenant OAuth2
credentials.  Tokens are Fernet-encrypted at rest via EncryptionService.
"""

from __future__ import annotations

import logging
from typing import Literal, cast

from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from sqlalchemy import CursorResult, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from w2v_common.database.rls import bind_session_tenant

from src.config import Settings
from src.database.models import OAuthToken, TenantYouTubeSettings
from src.services.encryption_service import EncryptionService
from src.services.youtube_config_service import (
    GOOGLE_TOKEN_URI,
    OAuthClient,
    PlatformChannel,
    YouTubeConfigService,
    effective_platform_allowed,
)
from src.tenant import require_tenant

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Connection status vocabulary (``GET /api/auth/status``)
# ---------------------------------------------------------------------------
#: The tenant's OWN grant is stored and has not been refused by Google.
STATUS_CONNECTED = "connected"
#: The tenant HAD a grant and Google refused to refresh it. Publishing is
#: refused until a tenant admin reconnects; the platform channel is NOT used.
STATUS_RECONNECT_REQUIRED = "reconnect_required"
#: No grant of its own; the tenant's effective ``platform_channel_allowed``
#: (its own setting, else ``youtube_platform_channel.default_for_tenants``) is on.
STATUS_PLATFORM_DEFAULT = "platform_default"
#: No grant of its own and the platform channel is not allowed/configured.
STATUS_NOT_CONNECTED = "not_connected"
CONNECTION_STATUSES = (
    STATUS_CONNECTED,
    STATUS_RECONNECT_REQUIRED,
    STATUS_PLATFORM_DEFAULT,
    STATUS_NOT_CONNECTED,
)


#: Which channel a set of credentials acts on — persisted as
#: ``upload_jobs.published_via`` so retract/re-privacy reach the SAME channel the
#: video was published to, whatever grant the owner holds later.
PUBLISHED_VIA_PLATFORM = "platform"
PUBLISHED_VIA_TENANT = "tenant"
PublishedVia = Literal["platform", "tenant"]

#: Which of the three resolution branches applies (``GET /api/auth/status``).
MODE_TENANT = "tenant"
MODE_PLATFORM = "platform"
MODE_NONE = "none"

_CLIENT_MISMATCH_REASON = (
    "This YouTube grant was issued by an OAuth client the tenant no longer has "
    "configured; reconnect YouTube."
)


class YouTubeCredentialsUnavailableError(RuntimeError):
    """No credential may publish for this tenant right now. ``error_code`` is stable."""

    error_code = "youtube_credentials_unavailable"


class YouTubeNotConnectedError(YouTubeCredentialsUnavailableError):
    """The tenant has no YouTube grant and the platform channel is not the default."""

    error_code = "youtube_not_connected"

    def __init__(self, tenant_id: str) -> None:
        self.tenant_id = tenant_id
        super().__init__(
            f"YouTube is not connected for tenant {tenant_id}: connect YouTube "
            "(a tenant admin must complete the YouTube connection) before publishing."
        )


class YouTubeReconnectRequiredError(YouTubeCredentialsUnavailableError):
    """The tenant's grant was refused by Google; it must be reconnected. No fallback."""

    error_code = "youtube_reconnect_required"

    def __init__(self, tenant_id: str, reason: str | None = None) -> None:
        self.tenant_id = tenant_id
        self.reason = reason
        detail = f" Google said: {reason}." if reason else ""
        super().__init__(
            f"The YouTube connection for tenant {tenant_id} is no longer valid: reconnect "
            "YouTube (a tenant admin must reconnect the channel) before publishing. The "
            f"video was NOT published to any other channel.{detail}"
        )


class YouTubeTokenRefreshUnavailableError(YouTubeCredentialsUnavailableError):
    """Google's token endpoint could not be reached (transient). The grant is untouched."""

    error_code = "youtube_token_refresh_unavailable"

    def __init__(self, tenant_id: str, reason: str) -> None:
        self.tenant_id = tenant_id
        super().__init__(
            f"Could not reach Google to refresh the YouTube grant of tenant {tenant_id} "
            f"({reason}); retry later. The video was NOT published to any other channel."
        )


class YouTubePlatformChannelUnavailableError(YouTubeCredentialsUnavailableError):
    """A video lives on the platform channel, but no platform credential is configured."""

    error_code = "youtube_platform_channel_unavailable"

    def __init__(self, label: str) -> None:
        super().__init__(
            f"{label} was published to the PLATFORM YouTube channel, but no platform "
            "channel is configured (youtube_platform_channel is empty), so nothing can act "
            "on that video. The tenant's own YouTube connection is NOT used: the video is "
            "not on its channel."
        )


class OAuthService:
    """Manage per-tenant YouTube OAuth2 credentials."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        encryption: EncryptionService,
        settings: Settings,
        config: YouTubeConfigService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._encryption = encryption
        self._settings = settings
        self.config = config or YouTubeConfigService(session_factory, encryption, settings)

    # ------------------------------------------------------------------
    # OAuth2 flow helpers
    # ------------------------------------------------------------------

    def build_flow(self, state: str | None = None, client: OAuthClient | None = None) -> Flow:
        """Build a google_auth_oauthlib Flow for ``client`` (default: the platform client)."""
        client = client or self.config.platform_oauth_client()
        client_config = {
            "web": {
                "client_id": client.client_id,
                "client_secret": client.client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": GOOGLE_TOKEN_URI,
                "redirect_uris": [self._settings.youtube.redirect_uri],
            }
        }
        flow = Flow.from_client_config(
            client_config,
            scopes=self._settings.youtube.scopes,
            state=state,
        )
        flow.redirect_uri = self._settings.youtube.redirect_uri
        return flow

    def build_authorization_url(self, state: str, client: OAuthClient | None = None) -> str:
        """Return the Google consent URL for the given state token and client."""
        flow = self.build_flow(state=state, client=client)
        url, _ = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
        )
        return url

    def exchange_code(
        self, code: str, state: str, client: OAuthClient | None = None
    ) -> Credentials:
        """Exchange an authorization code with the SAME client that started the flow."""
        flow = self.build_flow(state=state, client=client)
        flow.fetch_token(code=code)
        return cast(Credentials, flow.credentials)

    # ------------------------------------------------------------------
    # Token persistence
    # ------------------------------------------------------------------

    async def store_tokens(
        self,
        tenant_id: str,
        credentials: Credentials,
        channel_id: str | None = None,
        channel_title: str | None = None,
        *,
        oauth_client_id: str | None = None,
    ) -> None:
        """Encrypt and upsert OAuth2 tokens for *tenant_id*.

        ``oauth_client_id`` records WHICH client issued this grant (the tenant's
        own client id; ``None`` = the platform client). It is only ever set by a
        fresh consent — refreshes go through :meth:`_store_refreshed`.

        A single ``INSERT ... ON CONFLICT (tenant_id) DO UPDATE``, deliberately.
        The previous SELECT-then-add/mutate was a non-atomic read-modify-write:
        two callers arriving for the same tenant before either committed both
        saw ``None`` and both INSERTed, and the loser blew up on
        ``uq_oauth_tokens_tenant_id``. On ``GET /api/auth/callback`` that is a
        500 raised *after* the one-time authorization code and the one-time
        Redis state token have already been consumed, so the tenant's YouTube
        connection is lost and the whole consent flow has to be restarted —
        two browser tabs, or one duplicated redirect delivery, was enough.

        ``channel_id``/``channel_title`` keep their "``None`` means the caller
        did not look it up, do not overwrite" semantics via ``COALESCE`` on the
        excluded value, and ``updated_at`` is set explicitly because the ORM's
        ``onupdate`` hook does not fire for a Core ``ON CONFLICT`` clause.
        """
        token_uri = credentials.token_uri or "https://oauth2.googleapis.com/token"  # nosec B105 — Google OAuth2 token endpoint URL, not a secret
        insert_stmt = pg_insert(OAuthToken).values(
            tenant_id=tenant_id,
            access_token=self._encryption.encrypt(credentials.token or ""),
            refresh_token=self._encryption.encrypt(credentials.refresh_token or ""),
            token_uri=token_uri,
            scopes=" ".join(credentials.scopes or []),
            expiry=credentials.expiry,
            channel_id=channel_id,
            channel_title=channel_title,
            needs_reconnect=False,
            reconnect_reason=None,
            oauth_client_id=oauth_client_id,
        )
        excluded = insert_stmt.excluded
        upsert_stmt = insert_stmt.on_conflict_do_update(
            index_elements=[OAuthToken.tenant_id],
            set_={
                "access_token": excluded.access_token,
                "refresh_token": excluded.refresh_token,
                "token_uri": excluded.token_uri,
                "scopes": excluded.scopes,
                "expiry": excluded.expiry,
                "channel_id": func.coalesce(excluded.channel_id, OAuthToken.channel_id),
                "channel_title": func.coalesce(excluded.channel_title, OAuthToken.channel_title),
                # A fresh grant (new consent, or a successful refresh) heals a
                # reconnect-required row.
                "needs_reconnect": False,
                "reconnect_reason": None,
                "oauth_client_id": excluded.oauth_client_id,
                "updated_at": func.now(),
            },
        )

        async with self._session_factory() as session:
            await bind_session_tenant(session, require_tenant(tenant_id))
            await session.execute(upsert_stmt)
            await session.commit()
            logger.info("Stored OAuth tokens for tenant %s", tenant_id)

    async def _store_refreshed(
        self, tenant_id: str, credentials: Credentials, issuing_client_id: str | None
    ) -> None:
        """Write a refreshed access token back — ONLY onto the grant it was refreshed from.

        Guarded on the issuing client and ``needs_reconnect = false``: if the
        tenant swapped/removed its OAuth client while this refresh was in flight
        (which flags the grant), the refresh must not silently heal the flag.
        """
        values: dict[str, object] = {
            "access_token": self._encryption.encrypt(credentials.token or ""),
            "expiry": credentials.expiry,
            "updated_at": func.now(),
        }
        if credentials.refresh_token:
            values["refresh_token"] = self._encryption.encrypt(credentials.refresh_token)
        async with self._session_factory() as session:
            await bind_session_tenant(session, require_tenant(tenant_id))
            await session.execute(
                update(OAuthToken)
                .where(
                    OAuthToken.tenant_id == tenant_id,
                    OAuthToken.oauth_client_id.is_not_distinct_from(issuing_client_id),
                    OAuthToken.needs_reconnect.is_(False),
                )
                .values(**values)
            )
            await session.commit()

    async def _platform_credentials(self, platform: PlatformChannel) -> Credentials:
        """Build refreshed credentials for the platform channel row.

        Not persisted: the access token is short-lived and the row holds only the
        long-lived refresh token.
        """
        import asyncio

        creds = Credentials(
            token=None,
            refresh_token=platform.refresh_token,
            token_uri=GOOGLE_TOKEN_URI,
            client_id=platform.client_id,
            client_secret=platform.client_secret,
            scopes=self._settings.youtube.scopes,
        )
        try:
            await asyncio.to_thread(creds.refresh, GoogleAuthRequest())
        except (RefreshError, TransportError) as exc:
            # Every tenant allowed onto the platform channel rides this
            # credential, so name it: a raw ``RefreshError`` surfaces as
            # "invalid_grant" with no hint about WHICH grant died.
            raise RuntimeError(
                "The platform YouTube channel credential is dead: refreshing the "
                f"youtube_platform_channel refresh token against {GOOGLE_TOKEN_URI} failed "
                f"({exc}). Every tenant publishing to the platform channel is down until "
                "it is re-issued. Re-run the offline consent flow for the platform channel "
                f"(client_id {platform.client_id!r}) and replace the refresh token via "
                "PUT /api/admin/platform-channel."
            ) from exc
        return creds

    async def load_credentials_without_grant(self, label: str) -> Credentials:
        """Credentials for a subject that has no tenant (and so no grant) at all.

        Used for upload rows with no recorded owner tenant, which must never be
        resolved to another tenant's grant.
        """
        creds, _via = await self.resolve_credentials_without_grant(label)
        return creds

    async def resolve_credentials_without_grant(
        self, label: str
    ) -> tuple[Credentials, PublishedVia]:
        """No tenant → no tenant setting: the platform channel only when it is the default."""
        platform = await self.config.platform_channel()
        if platform is not None and effective_platform_allowed(None, platform):
            return await self._platform_credentials(platform), PUBLISHED_VIA_PLATFORM
        raise YouTubeNotConnectedError(label)

    async def platform_credentials_for_published(self, label: str) -> Credentials:
        """Credentials for a video ALREADY on the platform channel (retract/re-privacy).

        Gated on the platform channel being CONFIGURED, not on any tenant's
        permission: that governs where NEW uploads go; revoking it must not
        strand videos already published to the platform channel.
        Unconfigured → :class:`YouTubePlatformChannelUnavailableError`.
        """
        platform = await self.config.platform_channel()
        if platform is None:
            raise YouTubePlatformChannelUnavailableError(label)
        return await self._platform_credentials(platform)

    async def _read_grant_and_settings(
        self, tenant_id: str
    ) -> tuple[OAuthToken | None, TenantYouTubeSettings | None]:
        """The tenant's grant and settings row, in ONE RLS-bound session."""
        async with self._session_factory() as session:
            await bind_session_tenant(session, require_tenant(tenant_id))
            token_row = (
                await session.execute(select(OAuthToken).where(OAuthToken.tenant_id == tenant_id))
            ).scalar_one_or_none()
            settings_row = (
                await session.execute(
                    select(TenantYouTubeSettings).where(
                        TenantYouTubeSettings.tenant_id == tenant_id
                    )
                )
            ).scalar_one_or_none()
        return token_row, settings_row

    @staticmethod
    def _grant_client_mismatch(
        token_row: OAuthToken, settings_row: TenantYouTubeSettings | None
    ) -> bool:
        """True when the grant's issuing client is a tenant client the tenant no longer has."""
        issued_by = token_row.oauth_client_id
        if issued_by is None:  # the platform client — always present
            return False
        return settings_row is None or settings_row.oauth_client_id != issued_by

    def _issuing_client(
        self, token_row: OAuthToken, settings_row: TenantYouTubeSettings | None
    ) -> OAuthClient | None:
        """The client that can refresh this grant, or None on a client mismatch."""
        if token_row.oauth_client_id is None:
            return self.config.platform_oauth_client()
        if self._grant_client_mismatch(token_row, settings_row):
            return None
        return self.config.tenant_oauth_client_from_row(settings_row)

    async def load_credentials(self, tenant_id: str) -> Credentials:
        """The credentials to publish for *tenant_id* (see :meth:`resolve_credentials`)."""
        creds, _via = await self.resolve_credentials(tenant_id)
        return creds

    async def resolve_credentials(
        self, tenant_id: str, *, allow_platform: bool = True
    ) -> tuple[Credentials, PublishedVia]:
        """Resolve the credentials to publish for *tenant_id* — blueprint order, fail loud.

        1. The tenant has a grant → its own credentials, refreshed with the client
           that ISSUED the grant (``oauth_tokens.oauth_client_id``: the tenant's
           own client, or the platform client when NULL). A grant flagged
           ``needs_reconnect``, refused by Google, or issued by a client the
           tenant no longer has → :class:`YouTubeReconnectRequiredError`. Never
           the platform channel. Token endpoint unreachable →
           :class:`YouTubeTokenRefreshUnavailableError` (transient, not flagged).
        2. No grant, effective ``platform_channel_allowed`` (tenant value, else
           ``youtube_platform_channel.default_for_tenants``) and a platform row →
           the platform channel.
        3. Otherwise :class:`YouTubeNotConnectedError` ("connect YouTube").

        ``allow_platform=False`` drops branch 2 — for acting on a video known to
        live on the tenant's channel, where the platform channel is the wrong one.

        Returns ``(credentials, published_via)``.
        """
        token_row, settings_row = await self._read_grant_and_settings(tenant_id)
        if token_row is None:
            if allow_platform:
                platform = await self.config.platform_channel()
                allowed = settings_row.platform_channel_allowed if settings_row else None
                if platform is not None and effective_platform_allowed(allowed, platform):
                    return await self._platform_credentials(platform), PUBLISHED_VIA_PLATFORM
            raise YouTubeNotConnectedError(tenant_id)

        if token_row.needs_reconnect:
            raise YouTubeReconnectRequiredError(tenant_id, token_row.reconnect_reason)

        client = self._issuing_client(token_row, settings_row)
        if client is None:
            logger.warning(
                "Tenant %s's YouTube grant was issued by OAuth client %s, which the tenant "
                "no longer has; marking it reconnect-required",
                tenant_id,
                token_row.oauth_client_id,
            )
            await self._mark_needs_reconnect(tenant_id, _CLIENT_MISMATCH_REASON)
            raise YouTubeReconnectRequiredError(tenant_id, _CLIENT_MISMATCH_REASON)

        creds = Credentials(
            token=self._encryption.decrypt(token_row.access_token),
            refresh_token=self._encryption.decrypt(token_row.refresh_token),
            token_uri=token_row.token_uri,
            client_id=client.client_id,
            client_secret=client.client_secret,
            scopes=token_row.scopes.split() if token_row.scopes else None,
        )
        if token_row.expiry:
            creds.expiry = token_row.expiry.replace(tzinfo=None)

        if creds.expired and creds.refresh_token:
            import asyncio

            try:
                await asyncio.to_thread(creds.refresh, GoogleAuthRequest())
            except RefreshError as exc:
                # Google refused the tenant's own grant (revoked, expired after 6
                # months unused, or the client was rotated). Keep the row, flag
                # it, refuse — never re-route to the platform channel.
                reason = str(exc)[:500] or type(exc).__name__
                logger.warning(
                    "Refresh of tenant %s's own YouTube grant was refused; marking it "
                    "reconnect-required (%s)",
                    tenant_id,
                    reason,
                )
                await self._mark_needs_reconnect(tenant_id, reason)
                raise YouTubeReconnectRequiredError(tenant_id, reason) from exc
            except TransportError as exc:
                # A network failure says nothing about the grant: no flag, no fallback.
                raise YouTubeTokenRefreshUnavailableError(tenant_id, str(exc)) from exc
            await self._store_refreshed(tenant_id, creds, token_row.oauth_client_id)
            logger.info("Refreshed expired tokens for tenant %s", tenant_id)

        return creds, PUBLISHED_VIA_TENANT

    async def _mark_needs_reconnect(self, tenant_id: str, reason: str) -> None:
        """Flag a tenant's unrefreshable grant, best-effort.

        A DB error here must not replace the real failure the caller is about to
        raise (reconnect required), so it is logged and swallowed. The flag is a
        cache of Google's verdict, not the gate: an unflagged dead grant still
        fails the next refresh and is flagged then.
        """
        try:
            async with self._session_factory() as session:
                await bind_session_tenant(session, require_tenant(tenant_id))
                await session.execute(
                    update(OAuthToken)
                    .where(OAuthToken.tenant_id == tenant_id)
                    .values(needs_reconnect=True, reconnect_reason=reason, updated_at=func.now())
                )
                await session.commit()
        except Exception:  # best-effort flag; must not mask the reconnect error
            logger.warning(
                "Could not mark tenant %s's YouTube grant reconnect-required; it will be "
                "re-flagged on the next refresh attempt",
                tenant_id,
                exc_info=True,
            )

    async def get_token_info(self, tenant_id: str) -> dict:
        """Report which resolution branch applies, always with ``status`` and ``mode``.

        ``mode`` is ``tenant`` (the tenant has a grant — ``needs_reconnect`` says
        whether it is usable), ``platform`` (no grant, platform channel allowed
        and configured) or ``none``. ``status`` is one of
        :data:`CONNECTION_STATUSES`; ``connected`` means the tenant's OWN usable
        grant — never the platform channel. Uses the same inputs as
        :meth:`resolve_credentials`, so status and upload cannot disagree.
        """
        token_row, settings_row = await self._read_grant_and_settings(tenant_id)
        if token_row is None:
            platform = await self.config.platform_row()
            allowed = settings_row.platform_channel_allowed if settings_row else None
            if platform is None or not effective_platform_allowed(allowed, platform):
                return {
                    "tenant_id": tenant_id,
                    "status": STATUS_NOT_CONNECTED,
                    "mode": MODE_NONE,
                    "needs_reconnect": False,
                }
            return {
                "tenant_id": tenant_id,
                "status": STATUS_PLATFORM_DEFAULT,
                "mode": MODE_PLATFORM,
                "needs_reconnect": False,
                "channel_id": platform.channel_id,
                "channel_title": platform.channel_title,
                "scopes": self._settings.youtube.scopes,
                "connected_at": None,
                "updated_at": None,
                "token_expiry": None,  # nosec B105 — status timestamp field, not a credential
            }

        mismatch = self._grant_client_mismatch(token_row, settings_row)
        needs_reconnect = token_row.needs_reconnect or mismatch
        reason = token_row.reconnect_reason or (_CLIENT_MISMATCH_REASON if mismatch else None)
        return {
            "tenant_id": token_row.tenant_id,
            "status": STATUS_RECONNECT_REQUIRED if needs_reconnect else STATUS_CONNECTED,
            "mode": MODE_TENANT,
            "needs_reconnect": needs_reconnect,
            "reconnect_reason": reason,
            "channel_id": token_row.channel_id,
            "channel_title": token_row.channel_title,
            "scopes": token_row.scopes.split() if token_row.scopes else [],
            "connected_at": token_row.created_at.isoformat() if token_row.created_at else None,
            "updated_at": token_row.updated_at.isoformat() if token_row.updated_at else None,
            "token_expiry": token_row.expiry.isoformat() if token_row.expiry else None,
        }

    async def delete_tokens(self, tenant_id: str) -> bool:
        """Remove stored tokens for *tenant_id*. Returns True if a row was deleted."""
        async with self._session_factory() as session:
            await bind_session_tenant(session, require_tenant(tenant_id))
            stmt = delete(OAuthToken).where(OAuthToken.tenant_id == tenant_id)
            cursor_result = await session.execute(stmt)
            await session.commit()
            if not isinstance(cursor_result, CursorResult):
                raise RuntimeError(
                    f"Expected CursorResult from DELETE, got {type(cursor_result).__name__}"
                )
            deleted = (cursor_result.rowcount or 0) > 0
            if deleted:
                logger.info("Deleted OAuth tokens for tenant %s", tenant_id)
            return deleted

    async def has_tokens(self, tenant_id: str) -> bool:
        """Return True if the tenant has stored OAuth2 credentials."""
        async with self._session_factory() as session:
            await bind_session_tenant(session, require_tenant(tenant_id))
            stmt = select(OAuthToken.id).where(OAuthToken.tenant_id == tenant_id)
            result = await session.execute(stmt)
            return result.scalar_one_or_none() is not None
