"""Test helper: put the platform channel where production keeps it — the DB row.

The platform channel used to come from ``YOUTUBE_FALLBACK_*`` settings; since
migration 014 it is the ``youtube_platform_channel`` singleton row, so tests that
need it configured must write that row (as any role with DML on the table).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.database.models import YouTubePlatformChannel
from src.services.encryption_service import EncryptionService

__all__ = ["seed_platform_channel"]


async def seed_platform_channel(
    factory: async_sessionmaker[AsyncSession],
    *,
    default_for_tenants: bool,
    encryption: EncryptionService | None = None,
    client_id: str = "fallback-cid",
    client_secret: str = "x",  # pragma: allowlist secret
    refresh_token: str = "fallback-rt",  # pragma: allowlist secret
    channel_id: str = "UCfallback",
    channel_title: str = "George Dekker",
) -> None:
    enc = encryption or EncryptionService(key="")
    async with factory() as session:
        session.add(
            YouTubePlatformChannel(
                id=1,
                client_id=client_id,
                client_secret_enc=enc.encrypt(client_secret),
                refresh_token_enc=enc.encrypt(refresh_token),
                channel_id=channel_id,
                channel_title=channel_title,
                default_for_tenants=default_for_tenants,
            )
        )
        await session.commit()
