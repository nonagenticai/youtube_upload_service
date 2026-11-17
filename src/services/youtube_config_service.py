"""Per-tenant YouTube settings and the platform channel, stored in the database.

Two tables (migration ``014_tenant_youtube_settings``):

- ``tenant_youtube_settings`` (RLS by tenant): the tenant's own Google OAuth
  client (id + Fernet-encrypted secret) and ``platform_channel_allowed``
  (``NULL`` = follow the platform default).
- ``youtube_platform_channel`` (singleton, no RLS): the platform's own channel
  credential and ``default_for_tenants``.

Secrets are WRITE-ONLY: no method here returns or logs a plaintext secret except
the internal ``OAuthClient`` / ``PlatformChannel`` values the OAuth service hands
to Google (their ``repr`` hides the secret). Every API view reports only
``has_*`` booleans.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import TypedDict

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from w2v_common.database.rls import bind_session_tenant

from src.config import Settings
from src.database.models import OAuthToken, TenantYouTubeSettings, YouTubePlatformChannel
from src.services.encryption_service import EncryptionService
from src.tenant import require_tenant

logger = logging.getLogger(__name__)

GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"  # noqa: S105  # nosec B105 — endpoint URL

PLATFORM_CHANNEL_ID = 1


class PlatformChannelSummaryView(TypedDict):
    channel_id: str | None
    channel_title: str | None


class SettingsView(TypedDict):
    """``GET /api/settings`` body (no secrets)."""

    oauth_client_id: str | None
    has_oauth_client_secret: bool
    redirect_uri: str
    platform_channel_allowed: bool | None
    effective_platform_channel_allowed: bool
    platform_channel: PlatformChannelSummaryView | None


class PlatformChannelView(TypedDict):
    """Operator view of the platform channel (secrets as ``has_*`` only)."""

    client_id: str
    has_client_secret: bool
    has_refresh_token: bool
    channel_id: str | None
    channel_title: str | None
    default_for_tenants: bool
    updated_by: str | None
    updated_at: str | None


class TenantPlatformView(TypedDict):
    tenant_id: str
    platform_channel_allowed: bool | None
    effective_platform_channel_allowed: bool


class TenantSettingsListRow(TypedDict):
    tenant_id: str
    platform_channel_allowed: bool | None
    has_grant: bool
    has_oauth_client: bool


class PlatformChannelIncompleteError(ValueError):
    """A platform-channel write that would leave it without a usable credential."""


@dataclass(frozen=True)
class OAuthClient:
    """A Google OAuth client. ``client_id`` is None for the deployment (platform) client."""

    client_id: str
    client_secret: str = field(repr=False)
    #: The value recorded on ``oauth_tokens.oauth_client_id`` for a grant this
    #: client issues: the tenant's client id, or ``None`` for the platform client.
    recorded_id: str | None = None


@dataclass(frozen=True)
class PlatformChannel:
    """Decrypted platform channel row (internal only — never serialised)."""

    client_id: str
    client_secret: str = field(repr=False)
    refresh_token: str = field(repr=False)
    channel_id: str | None
    channel_title: str | None
    default_for_tenants: bool
    updated_by: str | None
    updated_at: dt.datetime | None


def effective_platform_allowed(
    tenant_allowed: bool | None, platform: PlatformChannel | YouTubePlatformChannel | None
) -> bool:
    """``tenant_allowed ?? platform.default_for_tenants``; False when no platform row."""
    if platform is None:
        return False
    if tenant_allowed is not None:
        return tenant_allowed
    return platform.default_for_tenants


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value else None


class YouTubeConfigService:
    """Read/write the per-tenant settings and the platform channel."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        encryption: EncryptionService,
        settings: Settings,
    ) -> None:
        self._session_factory = session_factory
        self._encryption = encryption
        self._settings = settings

    # ------------------------------------------------------------------
    # OAuth clients
    # ------------------------------------------------------------------

    def platform_oauth_client(self) -> OAuthClient:
        """The deployment OAuth client (``YOUTUBE_CLIENT_ID/SECRET``) — deployment infra."""
        yt = self._settings.youtube
        return OAuthClient(client_id=yt.client_id, client_secret=yt.client_secret, recorded_id=None)

    def tenant_oauth_client_from_row(self, row: TenantYouTubeSettings | None) -> OAuthClient | None:
        """The tenant's own client from its settings row, or None when it has none."""
        if row is None or not row.oauth_client_id or not row.oauth_client_secret_enc:
            return None
        return OAuthClient(
            client_id=row.oauth_client_id,
            client_secret=self._encryption.decrypt(row.oauth_client_secret_enc),
            recorded_id=row.oauth_client_id,
        )

    async def connect_client(self, tenant_id: str) -> OAuthClient:
        """The client a NEW connect flow uses: the tenant's own when configured."""
        row = await self.tenant_settings(tenant_id)
        return self.tenant_oauth_client_from_row(row) or self.platform_oauth_client()

    async def client_for_recorded_id(self, tenant_id: str, recorded_id: str | None) -> OAuthClient:
        """Resolve the client a flow STARTED with (``recorded_id`` from the OAuth state).

        Raises :class:`LookupError` when the tenant's client changed (set,
        swapped or removed) between connect and callback — exchanging the code
        with a different client than the one that started the flow cannot work,
        and a platform-client flow completing after the tenant set its own
        client would store an unflagged platform grant that
        :meth:`set_oauth_client` just declared reconnect-required.
        """
        client = self.tenant_oauth_client_from_row(await self.tenant_settings(tenant_id))
        if recorded_id is None:
            if client is not None:
                raise LookupError(
                    "The tenant configured its own YouTube OAuth client while the "
                    "connection was in progress; start the connection again."
                )
            return self.platform_oauth_client()
        if client is None or client.client_id != recorded_id:
            raise LookupError(
                "The tenant's YouTube OAuth client changed while the connection was in "
                "progress; start the connection again."
            )
        return client

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def tenant_settings(self, tenant_id: str) -> TenantYouTubeSettings | None:
        async with self._session_factory() as session:
            await bind_session_tenant(session, require_tenant(tenant_id))
            return await self._tenant_settings_in(session, tenant_id)

    @staticmethod
    async def _tenant_settings_in(
        session: AsyncSession, tenant_id: str
    ) -> TenantYouTubeSettings | None:
        stmt = select(TenantYouTubeSettings).where(TenantYouTubeSettings.tenant_id == tenant_id)
        return (await session.execute(stmt)).scalar_one_or_none()

    async def platform_row(self) -> YouTubePlatformChannel | None:
        """The platform row WITHOUT decrypting anything — for status/settings views."""
        async with self._session_factory() as session:
            return await session.get(YouTubePlatformChannel, PLATFORM_CHANNEL_ID)

    async def platform_channel(self) -> PlatformChannel | None:
        """The decrypted platform credential — only for talking to Google."""
        return self._decrypt_platform(await self.platform_row())

    def _decrypt_platform(self, row: YouTubePlatformChannel | None) -> PlatformChannel | None:
        if row is None:
            return None
        return PlatformChannel(
            client_id=row.client_id,
            client_secret=self._encryption.decrypt(row.client_secret_enc),
            refresh_token=self._encryption.decrypt(row.refresh_token_enc),
            channel_id=row.channel_id,
            channel_title=row.channel_title,
            default_for_tenants=row.default_for_tenants,
            updated_by=row.updated_by,
            updated_at=row.updated_at,
        )

    async def settings_view(self, tenant_id: str) -> SettingsView:
        """``GET /api/settings`` payload. No secret, ever."""
        row = await self.tenant_settings(tenant_id)
        platform = await self.platform_row()
        allowed = row.platform_channel_allowed if row is not None else None
        return {
            "oauth_client_id": row.oauth_client_id if row is not None else None,
            "has_oauth_client_secret": bool(row is not None and row.oauth_client_secret_enc),
            "redirect_uri": self._settings.youtube.redirect_uri,
            "platform_channel_allowed": allowed,
            "effective_platform_channel_allowed": effective_platform_allowed(allowed, platform),
            "platform_channel": (
                {"channel_id": platform.channel_id, "channel_title": platform.channel_title}
                if platform is not None
                else None
            ),
        }

    async def platform_channel_view(self) -> PlatformChannelView | None:
        """Operator view of the platform channel (``None`` when unconfigured).

        Secrets are reported as ``has_*`` booleans only.
        """
        row = await self.platform_row()
        if row is None:
            return None
        return {
            "client_id": row.client_id,
            "has_client_secret": bool(row.client_secret_enc),
            "has_refresh_token": bool(row.refresh_token_enc),
            "channel_id": row.channel_id,
            "channel_title": row.channel_title,
            "default_for_tenants": row.default_for_tenants,
            "updated_by": row.updated_by,
            "updated_at": _iso(row.updated_at),
        }

    async def list_tenant_settings(self) -> list[TenantSettingsListRow]:
        """Operator: every tenant that has a settings row or a grant (no secrets).

        Cross-tenant, so it cannot be a plain SELECT under RLS enforce (no tenant
        is bound on the operator lane → 0 rows). It calls the SECURITY DEFINER
        ``list_tenant_youtube_settings()`` (migration 014). A tenant absent from
        the list has no row: ``platform_channel_allowed`` NULL, no grant, no client.
        """
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT tenant_id, platform_channel_allowed, has_grant, has_oauth_client "
                        "FROM list_tenant_youtube_settings()"
                    )
                )
            ).all()
        return [
            {
                "tenant_id": str(r.tenant_id),
                "platform_channel_allowed": r.platform_channel_allowed,
                "has_grant": bool(r.has_grant),
                "has_oauth_client": bool(r.has_oauth_client),
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Tenant writes
    # ------------------------------------------------------------------

    async def set_oauth_client(
        self, tenant_id: str, client_id: str, client_secret: str, updated_by: str | None
    ) -> SettingsView:
        """Store the tenant's own OAuth client; a grant from another client → reconnect."""
        insert_stmt = pg_insert(TenantYouTubeSettings).values(
            tenant_id=tenant_id,
            oauth_client_id=client_id,
            oauth_client_secret_enc=self._encryption.encrypt(client_secret),
            updated_by=updated_by,
        )
        upsert = insert_stmt.on_conflict_do_update(
            index_elements=[TenantYouTubeSettings.tenant_id],
            set_={
                "oauth_client_id": insert_stmt.excluded.oauth_client_id,
                "oauth_client_secret_enc": insert_stmt.excluded.oauth_client_secret_enc,
                "updated_by": insert_stmt.excluded.updated_by,
                "updated_at": dt.datetime.now(dt.UTC),
            },
        )
        async with self._session_factory() as session:
            await bind_session_tenant(session, require_tenant(tenant_id))
            await session.execute(upsert)
            marked = await self._mark_grants_from_other_client(session, tenant_id, client_id)
            await session.commit()
        logger.info(
            "Tenant %s set its own YouTube OAuth client %s (grants marked reconnect: %d)",
            tenant_id,
            client_id,
            marked,
        )
        return await self.settings_view(tenant_id)

    async def clear_oauth_client(self, tenant_id: str, updated_by: str | None) -> SettingsView:
        """Remove the tenant's own client; a grant it issued → reconnect."""
        async with self._session_factory() as session:
            await bind_session_tenant(session, require_tenant(tenant_id))
            await session.execute(
                update(TenantYouTubeSettings)
                .where(TenantYouTubeSettings.tenant_id == tenant_id)
                .values(
                    oauth_client_id=None,
                    oauth_client_secret_enc=None,
                    updated_by=updated_by,
                    updated_at=dt.datetime.now(dt.UTC),
                )
            )
            marked = await self._mark_grants_from_other_client(session, tenant_id, None)
            await session.commit()
        logger.info(
            "Tenant %s removed its own YouTube OAuth client (grants marked reconnect: %d)",
            tenant_id,
            marked,
        )
        return await self.settings_view(tenant_id)

    @staticmethod
    async def _mark_grants_from_other_client(
        session: AsyncSession, tenant_id: str, current_client_id: str | None
    ) -> int:
        """Flag the tenant's grant ``needs_reconnect`` if another client issued it.

        A grant can only be refreshed by the client that issued it; once the
        tenant no longer has that client the grant is dead weight. It is kept
        (not deleted, not re-routed) and the upload fails loud until reconnect.
        """
        result = await session.execute(
            update(OAuthToken)
            .where(
                OAuthToken.tenant_id == tenant_id,
                OAuthToken.oauth_client_id.is_distinct_from(current_client_id),
            )
            .values(
                needs_reconnect=True,
                reconnect_reason=(
                    "The OAuth client that issued this YouTube grant is no longer the "
                    "tenant's configured client; reconnect YouTube."
                ),
                updated_at=dt.datetime.now(dt.UTC),
            )
        )
        return int(getattr(result, "rowcount", 0) or 0)

    async def set_tenant_platform_allowed(
        self, tenant_id: str, allowed: bool | None, updated_by: str | None
    ) -> TenantPlatformView:
        """Operator: per-tenant platform-channel permission (``None`` = follow default)."""
        insert_stmt = pg_insert(TenantYouTubeSettings).values(
            tenant_id=tenant_id, platform_channel_allowed=allowed, updated_by=updated_by
        )
        upsert = insert_stmt.on_conflict_do_update(
            index_elements=[TenantYouTubeSettings.tenant_id],
            set_={
                "platform_channel_allowed": insert_stmt.excluded.platform_channel_allowed,
                "updated_by": insert_stmt.excluded.updated_by,
                "updated_at": dt.datetime.now(dt.UTC),
            },
        )
        async with self._session_factory() as session:
            await bind_session_tenant(session, require_tenant(tenant_id))
            await session.execute(upsert)
            await session.commit()
        logger.info(
            "Operator %s set platform_channel_allowed=%s for tenant %s",
            updated_by,
            allowed,
            tenant_id,
        )
        platform = await self.platform_row()
        return {
            "tenant_id": tenant_id,
            "platform_channel_allowed": allowed,
            "effective_platform_channel_allowed": effective_platform_allowed(allowed, platform),
        }

    # ------------------------------------------------------------------
    # Platform channel writes
    # ------------------------------------------------------------------

    async def put_platform_channel(
        self,
        *,
        client_id: str,
        client_secret: str | None,
        refresh_token: str | None,
        channel_id: str,
        channel_title: str,
        default_for_tenants: bool,
        updated_by: str | None,
    ) -> PlatformChannelView | None:
        """Operator upsert. Omitted secrets keep the stored ones.

        Raises :class:`PlatformChannelIncompleteError` when the result would have
        no usable credential: creating the row without both secrets, or changing
        ``client_id`` without both (a refresh token is bound to its client).
        """
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(YouTubePlatformChannel)
                    .where(YouTubePlatformChannel.id == PLATFORM_CHANNEL_ID)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            both = bool(client_secret) and bool(refresh_token)
            if row is None and not both:
                raise PlatformChannelIncompleteError(
                    "The platform channel is not configured yet: client_secret and "
                    "refresh_token are both required to create it."
                )
            if row is not None and row.client_id != client_id and not both:
                raise PlatformChannelIncompleteError(
                    "Changing the platform channel's client_id requires a new client_secret "
                    "AND refresh_token: a refresh token only works with the client that "
                    "issued it."
                )
            values: dict[str, str | bool | dt.datetime | None] = {
                "client_id": client_id,
                "channel_id": channel_id,
                "channel_title": channel_title,
                "default_for_tenants": default_for_tenants,
                "updated_by": updated_by,
                "updated_at": dt.datetime.now(dt.UTC),
            }
            if client_secret:
                values["client_secret_enc"] = self._encryption.encrypt(client_secret)
            if refresh_token:
                values["refresh_token_enc"] = self._encryption.encrypt(refresh_token)
            if row is None:
                stmt = pg_insert(YouTubePlatformChannel).values(id=PLATFORM_CHANNEL_ID, **values)
                await session.execute(
                    stmt.on_conflict_do_update(
                        index_elements=[YouTubePlatformChannel.id], set_=values
                    )
                )
            else:
                await session.execute(
                    update(YouTubePlatformChannel)
                    .where(YouTubePlatformChannel.id == PLATFORM_CHANNEL_ID)
                    .values(**values)
                )
            await session.commit()
        logger.info(
            "Operator %s updated the platform YouTube channel (channel %s, default_for_tenants=%s, "
            "secrets replaced: client_secret=%s refresh_token=%s)",
            updated_by,
            channel_id,
            default_for_tenants,
            bool(client_secret),
            bool(refresh_token),
        )
        return await self.platform_channel_view()

    async def seed_platform_channel_from_env(self) -> bool:
        """Boot seed: copy ``YOUTUBE_FALLBACK_*`` into the empty platform table, once.

        Idempotent and race-safe across replicas: ``INSERT ... ON CONFLICT DO
        NOTHING`` on the singleton key, so two pods booting together write one
        row and neither fails. Once a row exists the env is not read at all — an
        operator edit through the API is never overwritten by a stale secret.

        Returns True only for the call that actually inserted the row.
        """
        async with self._session_factory() as session:
            exists = (
                await session.execute(
                    select(YouTubePlatformChannel.id).where(
                        YouTubePlatformChannel.id == PLATFORM_CHANNEL_ID
                    )
                )
            ).scalar_one_or_none()
            if exists is not None:
                return False
            yt = self._settings.youtube
            if not yt.fallback_seed_configured:
                logger.info(
                    "No platform YouTube channel configured (no youtube_platform_channel row "
                    "and no complete YOUTUBE_FALLBACK_* env to seed it)"
                )
                return False
            stmt = (
                pg_insert(YouTubePlatformChannel)
                .values(
                    id=PLATFORM_CHANNEL_ID,
                    client_id=yt.fallback_client_id,
                    client_secret_enc=self._encryption.encrypt(yt.fallback_client_secret),
                    refresh_token_enc=self._encryption.encrypt(yt.fallback_refresh_token),
                    channel_id=yt.fallback_channel_id or None,
                    channel_title=yt.fallback_channel_title or None,
                    default_for_tenants=yt.platform_channel_as_default,
                    updated_by="seed:YOUTUBE_FALLBACK_env",
                )
                .on_conflict_do_nothing(index_elements=[YouTubePlatformChannel.id])
                .returning(YouTubePlatformChannel.id)
            )
            inserted = (await session.execute(stmt)).scalar_one_or_none()
            await session.commit()
        if inserted is None:
            return False
        logger.warning(
            "Seeded youtube_platform_channel from YOUTUBE_FALLBACK_* env (channel %s, "
            "default_for_tenants=%s). The env is no longer read; manage the platform "
            "channel via /api/admin/platform-channel.",
            yt.fallback_channel_id or "<unset>",
            yt.platform_channel_as_default,
        )
        return True
