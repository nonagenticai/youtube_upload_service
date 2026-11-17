"""Scale-in reaper: reclaim uploads stranded by a terminated pod.

An upload runs as a fire-and-forget in-process background task under a DB lease
that :class:`~src.services.upload_service.UploadService` heartbeats while the
upload is live. If the pod is terminated mid-upload the lease is never renewed;
this reaper periodically fails every non-terminal job whose lease has expired (or
that never acquired one and is older than the grace window), so the orchestrator
can retry instead of the job hanging non-terminal forever.

The reap runs through the SECURITY DEFINER ``reap_stranded_upload_jobs`` resolver
(``src.database.rls_resolvers``) because under RLS enforce the runtime holds no
tenant context and the scan spans all tenants. A brief
``pg_try_advisory_xact_lock`` makes exactly one replica do the work on any given
sweep — a no-op on the single-replica deployment today, correct if it ever
scales out.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.config import Settings

logger = logging.getLogger(__name__)

# Stable, youtube_upload_service-private advisory-lock namespace so an operator
# who sees it in pg_locks knows what owns it. Any int fits.
_REAPER_LOCK_KEY = -6315000315


class UploadJobReaper:
    """Owns the per-pod reaper task; started/stopped from the ASGI lifespan."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        *,
        pod_id: str | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._pod_id = (pod_id or socket.gethostname())[:64]
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Launch the periodic reaper loop (one immediate sweep, then on interval)."""
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "Upload-job reaper started (interval=%ds, grace=%ds, pod=%s)",
            self._settings.upload.reaper_interval_seconds,
            self._settings.upload.reaper_grace_seconds,
            self._pod_id,
        )

    async def _loop(self) -> None:
        # An immediate sweep on startup reclaims anything a dead predecessor left
        # behind without waiting a full interval.
        while True:
            try:
                await self.reap_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Reaper sweep failed")
            try:
                await asyncio.sleep(self._settings.upload.reaper_interval_seconds)
            except asyncio.CancelledError:
                raise

    async def reap_once(self) -> int:
        """Fail every stranded (expired-/stale-lease) job. Returns the count reaped.

        Guarded by ``pg_try_advisory_xact_lock`` so only one replica reaps per
        sweep; a replica that loses the race returns 0 and no-ops."""
        async with self._session_factory() as session:
            locked = (
                await session.execute(
                    text("SELECT pg_try_advisory_xact_lock(:k)"),
                    {"k": _REAPER_LOCK_KEY},
                )
            ).scalar()
            if not locked:
                logger.debug("Reaper: another replica holds the lock; skipping")
                return 0
            count = (
                await session.execute(
                    text("SELECT reap_stranded_upload_jobs(:g)"),
                    {"g": self._settings.upload.reaper_grace_seconds},
                )
            ).scalar() or 0
            await session.commit()  # releases the xact-scoped advisory lock
            if count:
                logger.warning("Reaper failed %d stranded upload job(s) as retryable", count)
            return count

    async def release_pod_leases(self) -> int:
        """Expire this pod's in-flight leases at shutdown (the lease-release drain).

        Lets a successor pod reap them on its next sweep instead of waiting out
        the full lease TTL. Best-effort; leases expire on their own regardless."""
        try:
            async with self._session_factory() as session:
                count = (
                    await session.execute(
                        text("SELECT expire_upload_job_leases(:o)"),
                        {"o": self._pod_id},
                    )
                ).scalar() or 0
                await session.commit()
                if count:
                    logger.warning(
                        "Released %d in-flight lease(s) for %s at shutdown", count, self._pod_id
                    )
                return count
        except Exception:  # best-effort; leases expire on their own anyway
            logger.warning("Lease release at shutdown failed", exc_info=True)
            return 0

    async def stop(self) -> None:
        """Cancel the loop and expire this pod's leases for a fast successor reclaim."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        await self.release_pod_leases()
        logger.info("Upload-job reaper stopped")
