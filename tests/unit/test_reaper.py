"""Tests for the scale-in reaper — real Postgres, SECURITY DEFINER resolvers.

Covers the three behaviours the reaper must guarantee:
- a stranded in-flight job (expired lease) is failed-and-retryable;
- a live, heartbeating upload (lease in the future) is left untouched;
- only one pod reaps at a time (advisory-lock contention).

Plus the never-leased grace window and the shutdown lease-release drain.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.database.models import UploadJob
from src.infra.reaper import _REAPER_LOCK_KEY, UploadJobReaper
from tests.tenants import tenant_uuid


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


async def _insert(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    job_id: str,
    status: str,
    lease_owner: str | None = None,
    lease_expires_at: dt.datetime | None = None,
    created_at: dt.datetime | None = None,
) -> None:
    async with session_factory() as session:
        session.add(
            UploadJob(
                job_id=job_id,
                tenant_id=tenant_uuid("t1"),
                owner_tenant_id=tenant_uuid("t1"),
                status=status,
                file_source="/v.mp4",
                file_source_type="path",
                title="V",
                lease_owner=lease_owner,
                lease_expires_at=lease_expires_at,
                created_at=created_at or _now(),
            )
        )
        await session.commit()


async def _fetch(session_factory: async_sessionmaker[AsyncSession], job_id: str) -> UploadJob:
    async with session_factory() as session:
        return (
            await session.execute(select(UploadJob).where(UploadJob.job_id == job_id))
        ).scalar_one()


@pytest.fixture
def reaper(db_session_factory, mock_settings) -> UploadJobReaper:
    return UploadJobReaper(db_session_factory, mock_settings, pod_id="pod-A")


# ---------------------------------------------------------------------------
# Reap: stranded in-flight job (expired lease) → failed + retryable
# ---------------------------------------------------------------------------


class TestReapStranded:
    async def test_reaps_processing_job_with_expired_lease(self, reaper, db_session_factory):
        """A processing job whose owning pod died (lease expired) is failed."""
        await _insert(
            db_session_factory,
            job_id="stranded-1",
            status="processing",
            lease_owner="dead-pod",
            lease_expires_at=_now() - dt.timedelta(seconds=30),
        )

        reaped = await reaper.reap_once()

        assert reaped == 1
        job = await _fetch(db_session_factory, "stranded-1")
        assert job.status == "failed"
        assert job.error_message is not None
        assert "retry" in job.error_message.lower()
        assert job.completed_at is not None

    async def test_reaps_stale_pending_job_without_lease(self, reaper, db_session_factory):
        """A pending job that never acquired a lease and is older than the grace
        window (pod died before the task started) is failed."""
        await _insert(
            db_session_factory,
            job_id="stale-pending",
            status="pending",
            lease_owner=None,
            lease_expires_at=None,
            created_at=_now() - dt.timedelta(seconds=3600),  # > 900s grace
        )

        reaped = await reaper.reap_once()

        assert reaped == 1
        job = await _fetch(db_session_factory, "stale-pending")
        assert job.status == "failed"


# ---------------------------------------------------------------------------
# Never reap a LIVE upload or a fresh/terminal job
# ---------------------------------------------------------------------------


class TestReapLeavesLiveAndTerminal:
    async def test_does_not_reap_live_heartbeating_job(self, reaper, db_session_factory):
        """A processing job whose heartbeat keeps the lease in the future is untouched."""
        await _insert(
            db_session_factory,
            job_id="live-1",
            status="processing",
            lease_owner="pod-A",
            lease_expires_at=_now() + dt.timedelta(seconds=90),
        )

        reaped = await reaper.reap_once()

        assert reaped == 0
        job = await _fetch(db_session_factory, "live-1")
        assert job.status == "processing"
        assert job.error_message is None

    async def test_does_not_reap_fresh_pending_without_lease(self, reaper, db_session_factory):
        """A just-created pending job (null lease, inside grace) is left for the
        background task to pick up — not wrongly failed."""
        await _insert(
            db_session_factory,
            job_id="fresh-pending",
            status="pending",
            lease_owner=None,
            lease_expires_at=None,
            created_at=_now(),
        )

        reaped = await reaper.reap_once()

        assert reaped == 0
        job = await _fetch(db_session_factory, "fresh-pending")
        assert job.status == "pending"

    async def test_does_not_touch_terminal_jobs(self, reaper, db_session_factory):
        """Completed/failed jobs are never re-failed, even with a stale lease."""
        await _insert(
            db_session_factory,
            job_id="done-1",
            status="completed",
            lease_owner="old-pod",
            lease_expires_at=_now() - dt.timedelta(days=1),
        )
        await _insert(
            db_session_factory,
            job_id="failed-1",
            status="failed",
            lease_owner="old-pod",
            lease_expires_at=_now() - dt.timedelta(days=1),
        )

        reaped = await reaper.reap_once()

        assert reaped == 0
        assert (await _fetch(db_session_factory, "done-1")).status == "completed"
        assert (await _fetch(db_session_factory, "failed-1")).status == "failed"


# ---------------------------------------------------------------------------
# Only one pod reaps (advisory-lock contention)
# ---------------------------------------------------------------------------


class TestSingleReaper:
    async def test_only_one_pod_reaps(self, reaper, db_session_factory):
        """When another replica holds the advisory lock, reap_once no-ops."""
        await _insert(
            db_session_factory,
            job_id="contended",
            status="processing",
            lease_owner="dead-pod",
            lease_expires_at=_now() - dt.timedelta(seconds=30),
        )

        # Simulate a sibling replica mid-sweep by holding a session-level advisory
        # lock on the reaper's key from another connection.
        holder = db_session_factory()
        holder_session = await holder.__aenter__()
        await holder_session.execute(text("SELECT pg_advisory_lock(:k)"), {"k": _REAPER_LOCK_KEY})
        try:
            reaped = await reaper.reap_once()
            assert reaped == 0
            # The stranded job is left for the lock holder to handle.
            assert (await _fetch(db_session_factory, "contended")).status == "processing"
        finally:
            await holder_session.execute(
                text("SELECT pg_advisory_unlock(:k)"), {"k": _REAPER_LOCK_KEY}
            )
            await holder.__aexit__(None, None, None)

        # Once the lock is free the reaper reclaims it.
        assert await reaper.reap_once() == 1
        assert (await _fetch(db_session_factory, "contended")).status == "failed"


# ---------------------------------------------------------------------------
# Shutdown lease-release drain
# ---------------------------------------------------------------------------


class TestReleasePodLeases:
    async def test_release_expires_only_own_processing_leases(
        self, db_session_factory, mock_settings
    ):
        """release_pod_leases expires THIS pod's in-flight leases, not others'."""
        await _insert(
            db_session_factory,
            job_id="mine",
            status="processing",
            lease_owner="pod-X",
            lease_expires_at=_now() + dt.timedelta(seconds=90),
        )
        await _insert(
            db_session_factory,
            job_id="theirs",
            status="processing",
            lease_owner="pod-Y",
            lease_expires_at=_now() + dt.timedelta(seconds=90),
        )

        reaper = UploadJobReaper(db_session_factory, mock_settings, pod_id="pod-X")
        released = await reaper.release_pod_leases()

        assert released == 1
        mine = await _fetch(db_session_factory, "mine")
        theirs = await _fetch(db_session_factory, "theirs")
        assert mine.lease_expires_at is not None
        assert theirs.lease_expires_at is not None
        # Ours is now expired (reclaimable immediately); theirs is untouched.
        assert mine.lease_expires_at <= _now()
        assert theirs.lease_expires_at > _now()


# ---------------------------------------------------------------------------
# The failure message must describe what actually happened
# ---------------------------------------------------------------------------


class TestReapMessageAccuracy:
    """The lease columns are stamped at CLAIM, never at enqueue, so a NULL-lease
    row was never touched by any worker. Blaming a pod scale-in for it is a
    diagnostic lie: the commonest way to land there is simply waiting behind the
    ``UPLOAD_MAX_CONCURRENT`` semaphore for longer than the grace window."""

    async def test_never_claimed_job_is_not_blamed_on_a_crash(self, reaper, db_session_factory):
        await _insert(
            db_session_factory,
            job_id="never-claimed",
            status="pending",
            lease_owner=None,
            lease_expires_at=None,
            created_at=_now() - dt.timedelta(seconds=3600),
        )

        assert await reaper.reap_once() == 1

        message = (await _fetch(db_session_factory, "never-claimed")).error_message
        assert message is not None
        lowered = message.lower()
        # The lie: no pod ever held this job, so no pod crashed or scaled in.
        assert "scale-in" not in lowered
        assert "crash" not in lowered
        assert "never" in lowered
        # Nothing was uploaded, so retrying really is safe here.
        assert "safe to retry" in lowered

    async def test_lease_lost_job_says_the_upload_may_have_happened(
        self, reaper, db_session_factory
    ):
        """A claimed job that stopped heartbeating genuinely is a crash/scale-in
        — but it is NOT 'safe to retry': the pod can finish the YouTube upload
        and only then fail to write the row back."""
        await _insert(
            db_session_factory,
            job_id="lease-lost",
            status="processing",
            lease_owner="dead-pod",
            lease_expires_at=_now() - dt.timedelta(seconds=30),
        )

        assert await reaper.reap_once() == 1

        message = (await _fetch(db_session_factory, "lease-lost")).error_message
        assert message is not None
        lowered = message.lower()
        assert "lease" in lowered
        assert "check the channel" in lowered
        assert "safe to retry" not in lowered

    async def test_the_two_populations_get_different_messages(self, reaper, db_session_factory):
        """One sweep, both kinds — the CASE must discriminate per row, not per
        statement."""
        await _insert(
            db_session_factory,
            job_id="mixed-never",
            status="pending",
            created_at=_now() - dt.timedelta(seconds=3600),
        )
        await _insert(
            db_session_factory,
            job_id="mixed-lost",
            status="processing",
            lease_owner="dead-pod",
            lease_expires_at=_now() - dt.timedelta(seconds=30),
        )

        assert await reaper.reap_once() == 2

        never = (await _fetch(db_session_factory, "mixed-never")).error_message
        lost = (await _fetch(db_session_factory, "mixed-lost")).error_message
        assert never != lost
