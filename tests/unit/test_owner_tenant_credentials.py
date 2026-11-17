"""Uploads publish with the job OWNER's YouTube grant — never the channel/SA tenant's.

``upload_jobs.tenant_id`` is the orchestrator service account's tenant whenever
the SA call was not delegated (the same value for every caller);
``owner_tenant_id`` is the real caller (``X-Owner-Tenant-Id``). The worker used
to call ``load_credentials(job.tenant_id)``, so a tenant's own grant in
``oauth_tokens`` was never read, and with the platform channel as default
on every video went to the platform channel.

These tests run a REAL :class:`OAuthService` against postgres, as a restricted
``NOBYPASSRLS`` login role with ``TENANT_RLS_MODE=enforce`` — the production
posture — so the ``oauth_tokens`` read and the refresh write-back are proven to
run under the OWNER tenant's RLS binding, not merely to pass the right string.
They skip (loudly) where the DB user cannot ``CREATE ROLE``.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.database.models import OAuthToken, UploadJob
from src.services.encryption_service import EncryptionService
from src.services.oauth_service import OAuthService
from src.services.upload_service import RetractUnavailableError, UploadService
from tests.platform_channel import seed_platform_channel
from tests.tenants import tenant_uuid

pytestmark = pytest.mark.asyncio

SA_TENANT = tenant_uuid("orchestrator-sa")
OWNER_A = tenant_uuid("owner-a")
OWNER_B = tenant_uuid("owner-b")

_ROLE = "yt_owner_cred_rls_test"
_ROLE_PW = "yt-owner-cred-rls-test"  # pragma: allowlist secret


def _creds(token: str, *, expiry: dt.datetime | None = None) -> MagicMock:
    creds = MagicMock()
    creds.token = token
    creds.refresh_token = f"rt-{token}"
    creds.token_uri = "https://oauth2.googleapis.com/token"
    creds.scopes = ["https://www.googleapis.com/auth/youtube.upload"]
    creds.expiry = expiry
    return creds


@pytest.fixture
async def restricted_factory(
    db_engine, monkeypatch
) -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
    """Session factory connecting as a NOBYPASSRLS login role, RLS enforced."""
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
                "GRANT SELECT, INSERT, UPDATE, DELETE ON oauth_tokens, upload_jobs, "
                f'tenant_youtube_settings, youtube_platform_channel TO "{_ROLE}"'
            )
        )
        await conn.execute(text(f'GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO "{_ROLE}"'))
    monkeypatch.setenv("TENANT_RLS_MODE", "enforce")
    url = make_url(str(db_engine.url)).set(username=_ROLE, password=_ROLE_PW)
    engine = create_async_engine(url)
    try:
        yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    finally:
        await engine.dispose()
        async with db_engine.begin() as conn:
            await conn.execute(
                text(
                    "REVOKE ALL ON oauth_tokens, upload_jobs, tenant_youtube_settings, "
                    f'youtube_platform_channel FROM "{_ROLE}"'
                )
            )
            await conn.execute(text(f'REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM "{_ROLE}"'))
            await conn.execute(text(f'DROP ROLE IF EXISTS "{_ROLE}"'))


async def _oauth(
    factory, mock_settings, *, platform_default: bool, platform_configured: bool = True
) -> OAuthService:
    """``platform_configured`` writes the platform-channel row; ``platform_default``
    is its ``default_for_tenants`` (the per-tenant setting is left NULL = follow)."""
    if platform_configured:
        await seed_platform_channel(factory, default_for_tenants=platform_default)
    return OAuthService(
        session_factory=factory, encryption=EncryptionService(key=""), settings=mock_settings
    )


async def _seed_grant(db_session_factory, mock_settings, tenant: str, token: str, **kw) -> None:
    # Seeded as the table owner (bypasses RLS) — the row must exist regardless.
    owner_svc = OAuthService(
        session_factory=db_session_factory,
        encryption=EncryptionService(key=""),
        settings=mock_settings,
    )
    await owner_svc.store_tokens(tenant, _creds(token, **kw), channel_id=f"UC-{token}")


async def _seed_job(db_session_factory, job_id: str, owner: str | None) -> None:
    async with db_session_factory() as session:
        session.add(
            UploadJob(
                job_id=job_id,
                tenant_id=SA_TENANT,
                owner_tenant_id=owner,
                status="pending",
                file_source="https://storage.test/w2v/v.mp4",
                file_source_type="url",
                title="t",
                privacy_status="private",
            )
        )
        await session.commit()


async def _job(db_session_factory, job_id: str) -> UploadJob:
    async with db_session_factory() as session:
        return (
            await session.execute(select(UploadJob).where(UploadJob.job_id == job_id))
        ).scalar_one()


def _service(factory, oauth: OAuthService, youtube: AsyncMock, mock_settings) -> UploadService:
    return UploadService(
        session_factory=factory,
        oauth_service=oauth,
        youtube_service=youtube,
        task_manager=MagicMock(),
        settings=mock_settings,
    )


async def _run(service: UploadService, job_id: str) -> None:
    with (
        patch("pathlib.Path.stat", return_value=MagicMock(st_size=1024)),
        patch.object(service, "_download_file", new_callable=AsyncMock) as dl,
    ):
        dl.return_value = f"/tmp/youtube_uploads/{job_id}.mp4"
        await service._process_job(job_id, SA_TENANT)


def _youtube() -> AsyncMock:
    yt = AsyncMock()
    yt.async_upload_video.return_value = {"video_id": "vid", "url": "https://youtu.be/vid"}
    return yt


async def test_each_job_publishes_with_its_own_owners_grant(
    restricted_factory, db_session_factory, mock_settings
):
    """Two tenants, two grants, the SA tenant has none: each job uses its owner's."""
    await _seed_grant(db_session_factory, mock_settings, OWNER_A, "token-a")
    await _seed_grant(db_session_factory, mock_settings, OWNER_B, "token-b")
    await _seed_job(db_session_factory, "job-a", OWNER_A)
    await _seed_job(db_session_factory, "job-b", OWNER_B)
    youtube = _youtube()
    # Platform default ON: the pre-fix code silently published both to the
    # platform channel here, because the SA tenant has no grant.
    oauth = await _oauth(restricted_factory, mock_settings, platform_default=True)
    service = _service(restricted_factory, oauth, youtube, mock_settings)

    with patch.object(oauth, "_platform_credentials", new_callable=AsyncMock) as platform:
        await _run(service, "job-a")
        await _run(service, "job-b")

    platform.assert_not_awaited()
    used = [call.args[0].token for call in youtube.async_upload_video.await_args_list]
    assert used == ["token-a", "token-b"]
    assert (await _job(db_session_factory, "job-a")).status == "completed"
    assert (await _job(db_session_factory, "job-b")).status == "completed"


async def test_refreshed_token_is_written_back_under_the_owner_tenant(
    restricted_factory, db_session_factory, mock_settings
):
    """The refresh write-back runs under the OWNER's RLS binding (an SA-bound
    upsert would be refused by the policy's WITH CHECK) and lands on the owner row."""
    await _seed_grant(
        db_session_factory,
        mock_settings,
        OWNER_A,
        "stale-a",
        expiry=dt.datetime(2020, 1, 1, tzinfo=dt.UTC),
    )
    await _seed_job(db_session_factory, "job-refresh", OWNER_A)
    youtube = _youtube()
    oauth = await _oauth(restricted_factory, mock_settings, platform_default=False)
    service = _service(restricted_factory, oauth, youtube, mock_settings)

    def _refresh(self_creds, _request):
        self_creds.token = "fresh-a"
        self_creds.expiry = dt.datetime(2099, 1, 1)

    with (
        patch("src.services.oauth_service.GoogleAuthRequest"),
        patch("google.oauth2.credentials.Credentials.refresh", _refresh),
    ):
        await _run(service, "job-refresh")

    assert (await _job(db_session_factory, "job-refresh")).status == "completed"
    async with db_session_factory() as session:
        rows = (await session.execute(select(OAuthToken))).scalars().all()
    assert [(r.tenant_id, r.access_token) for r in rows] == [(OWNER_A, "fresh-a")]


async def test_owner_without_grant_and_flag_off_fails_connect_youtube(
    restricted_factory, db_session_factory, mock_settings
):
    # The SA tenant HAS a grant: proves it is not used as a fallback.
    await _seed_grant(db_session_factory, mock_settings, SA_TENANT, "token-sa")
    await _seed_job(db_session_factory, "job-nogrant", OWNER_A)
    youtube = _youtube()
    oauth = await _oauth(restricted_factory, mock_settings, platform_default=False)
    service = _service(restricted_factory, oauth, youtube, mock_settings)

    await _run(service, "job-nogrant")

    job = await _job(db_session_factory, "job-nogrant")
    assert job.status == "failed"
    assert "connect YouTube" in (job.error_message or "")
    assert OWNER_A in (job.error_message or "")
    youtube.async_upload_video.assert_not_awaited()


async def test_owner_without_grant_and_flag_on_uses_platform(
    restricted_factory, db_session_factory, mock_settings
):
    await _seed_grant(db_session_factory, mock_settings, SA_TENANT, "token-sa")
    await _seed_job(db_session_factory, "job-platform", OWNER_A)
    youtube = _youtube()
    oauth = await _oauth(restricted_factory, mock_settings, platform_default=True)
    service = _service(restricted_factory, oauth, youtube, mock_settings)

    platform_creds = _creds("platform-token")
    with patch.object(oauth, "_platform_credentials", AsyncMock(return_value=platform_creds)):
        await _run(service, "job-platform")

    assert youtube.async_upload_video.await_args.args[0].token == "platform-token"
    assert (await _job(db_session_factory, "job-platform")).status == "completed"


@pytest.mark.parametrize(
    ("platform_default", "expect_status"), [(False, "failed"), (True, "completed")]
)
async def test_job_with_no_recorded_owner_is_never_resolved_to_the_sa_tenant(
    restricted_factory, db_session_factory, mock_settings, platform_default, expect_status
):
    """A pre-005 row (owner NULL) behaves like a tenant without a grant — even
    though the SA tenant the row lives under HAS a grant."""
    await _seed_grant(db_session_factory, mock_settings, SA_TENANT, "token-sa")
    await _seed_job(db_session_factory, "job-noowner", None)
    youtube = _youtube()
    oauth = await _oauth(restricted_factory, mock_settings, platform_default=platform_default)
    service = _service(restricted_factory, oauth, youtube, mock_settings)

    with patch.object(
        oauth, "_platform_credentials", AsyncMock(return_value=_creds("platform-token"))
    ):
        await _run(service, "job-noowner")

    job = await _job(db_session_factory, "job-noowner")
    assert job.status == expect_status
    if platform_default:
        assert youtube.async_upload_video.await_args.args[0].token == "platform-token"
    else:
        assert "connect YouTube" in (job.error_message or "")
        youtube.async_upload_video.assert_not_awaited()


async def test_retract_and_privacy_use_the_owners_grant(
    restricted_factory, db_session_factory, mock_settings
):
    await _seed_grant(db_session_factory, mock_settings, OWNER_A, "token-a")
    async with db_session_factory() as session:
        session.add(
            UploadJob(
                job_id="job-pub",
                tenant_id=SA_TENANT,
                owner_tenant_id=OWNER_A,
                status="completed",
                file_source="https://storage.test/w2v/v.mp4",
                file_source_type="url",
                title="t",
                privacy_status="private",
                youtube_video_id="vidP",
            )
        )
        await session.commit()
    youtube = AsyncMock()
    oauth = await _oauth(restricted_factory, mock_settings, platform_default=False)
    service = _service(restricted_factory, oauth, youtube, mock_settings)

    await service.set_job_privacy("job-pub", SA_TENANT, OWNER_A, "public")
    await service.retract_job("job-pub", SA_TENANT, OWNER_A)

    assert youtube.async_set_privacy.await_args.args[0].token == "token-a"
    assert youtube.async_set_private.await_args.args[0].token == "token-a"


# ---------------------------------------------------------------------------
# published_via — retract / re-privacy act on the channel that PUBLISHED the video
# ---------------------------------------------------------------------------


async def _seed_published(db_session_factory, job_id: str, owner: str | None, via: str | None):
    async with db_session_factory() as session:
        session.add(
            UploadJob(
                job_id=job_id,
                tenant_id=SA_TENANT,
                owner_tenant_id=owner,
                status="completed",
                file_source=f"https://storage.test/w2v/{job_id}.mp4",
                file_source_type="url",
                title="t",
                privacy_status="private",
                youtube_video_id="vidP",
                published_via=via,
            )
        )
        await session.commit()


async def test_platform_published_then_tenant_connects_retracts_on_platform(
    restricted_factory, db_session_factory, mock_settings
):
    """The review scenario end to end: published on the platform channel (no grant,
    flag on), the tenant later connects its own channel — retract and re-privacy
    must still reach the PLATFORM channel, where the video actually is."""
    await _seed_job(db_session_factory, "job-plat", OWNER_A)
    youtube = _youtube()
    oauth = await _oauth(restricted_factory, mock_settings, platform_default=True)
    service = _service(restricted_factory, oauth, youtube, mock_settings)
    platform_creds = _creds("platform-token")

    with patch.object(oauth, "_platform_credentials", AsyncMock(return_value=platform_creds)):
        await _run(service, "job-plat")
    job = await _job(db_session_factory, "job-plat")
    assert job.status == "completed"
    assert job.published_via == "platform"

    # The tenant now connects its own channel.
    await _seed_grant(db_session_factory, mock_settings, OWNER_A, "token-a")

    with patch.object(
        oauth, "_platform_credentials", AsyncMock(return_value=platform_creds)
    ) as platform:
        await service.set_job_privacy("job-plat", SA_TENANT, OWNER_A, "public")
        await service.retract_job("job-plat", SA_TENANT, OWNER_A)

    assert platform.await_count == 2
    assert youtube.async_set_privacy.await_args.args[0].token == "platform-token"
    assert youtube.async_set_private.await_args.args[0].token == "platform-token"


async def test_platform_published_survives_the_flag_being_turned_off(
    restricted_factory, db_session_factory, mock_settings
):
    """The opt-in governs NEW uploads; turning it off must not strand videos already
    on the platform channel while the credential is still configured."""
    await _seed_grant(db_session_factory, mock_settings, OWNER_A, "token-a")
    await _seed_published(db_session_factory, "job-plat-off", OWNER_A, "platform")
    youtube = AsyncMock()
    oauth = await _oauth(restricted_factory, mock_settings, platform_default=False)
    service = _service(restricted_factory, oauth, youtube, mock_settings)

    with patch.object(
        oauth, "_platform_credentials", AsyncMock(return_value=_creds("platform-token"))
    ):
        await service.retract_job("job-plat-off", SA_TENANT, OWNER_A)

    assert youtube.async_set_private.await_args.args[0].token == "platform-token"


async def test_platform_published_without_platform_credential_is_a_clear_error(
    restricted_factory, db_session_factory, mock_settings
):
    """No platform credential configured → refuse, naming the platform channel. The
    tenant's grant is NOT tried: that channel does not hold the video."""
    await _seed_grant(db_session_factory, mock_settings, OWNER_A, "token-a")
    await _seed_published(db_session_factory, "job-plat-gone", OWNER_A, "platform")
    youtube = AsyncMock()
    oauth = await _oauth(
        restricted_factory, mock_settings, platform_default=False, platform_configured=False
    )
    service = _service(restricted_factory, oauth, youtube, mock_settings)

    with pytest.raises(RetractUnavailableError, match="PLATFORM YouTube channel"):
        await service.retract_job("job-plat-gone", SA_TENANT, OWNER_A)
    with pytest.raises(RetractUnavailableError, match="PLATFORM YouTube channel"):
        await service.set_job_privacy("job-plat-gone", SA_TENANT, OWNER_A, "public")
    youtube.async_set_private.assert_not_awaited()
    youtube.async_set_privacy.assert_not_awaited()


async def test_tenant_published_uses_the_owners_grant_even_with_platform_on(
    restricted_factory, db_session_factory, mock_settings
):
    await _seed_grant(db_session_factory, mock_settings, OWNER_A, "token-a")
    await _seed_job(db_session_factory, "job-ten", OWNER_A)
    youtube = _youtube()
    oauth = await _oauth(restricted_factory, mock_settings, platform_default=True)
    service = _service(restricted_factory, oauth, youtube, mock_settings)

    with patch.object(oauth, "_platform_credentials", new_callable=AsyncMock) as platform:
        await _run(service, "job-ten")
        assert (await _job(db_session_factory, "job-ten")).published_via == "tenant"
        await service.set_job_privacy("job-ten", SA_TENANT, OWNER_A, "unlisted")
        await service.retract_job("job-ten", SA_TENANT, OWNER_A)

    platform.assert_not_awaited()
    assert youtube.async_set_privacy.await_args.args[0].token == "token-a"
    assert youtube.async_set_private.await_args.args[0].token == "token-a"


async def test_tenant_published_with_grant_gone_says_connect_not_platform(
    restricted_factory, db_session_factory, mock_settings
):
    """Tenant-published, grant since disconnected, flag ON: the platform channel does
    not hold the video either, so the answer is "connect YouTube", never platform."""
    await _seed_published(db_session_factory, "job-ten-gone", OWNER_A, "tenant")
    youtube = AsyncMock()
    oauth = await _oauth(restricted_factory, mock_settings, platform_default=True)
    service = _service(restricted_factory, oauth, youtube, mock_settings)

    with (
        patch.object(oauth, "_platform_credentials", new_callable=AsyncMock) as platform,
        pytest.raises(RetractUnavailableError, match="connect YouTube"),
    ):
        await service.retract_job("job-ten-gone", SA_TENANT, OWNER_A)

    platform.assert_not_awaited()
    youtube.async_set_private.assert_not_awaited()


async def test_legacy_null_published_via_without_grant_uses_platform_when_flag_on(
    restricted_factory, db_session_factory, mock_settings
):
    """NULL (pre-013) keeps the old resolution: no owner grant + flag on → platform.
    (NULL + owner grant → owner grant is ``test_retract_and_privacy_use_the_owners_grant``.)"""
    await _seed_published(db_session_factory, "job-legacy", OWNER_A, None)
    youtube = AsyncMock()
    oauth = await _oauth(restricted_factory, mock_settings, platform_default=True)
    service = _service(restricted_factory, oauth, youtube, mock_settings)

    with patch.object(
        oauth, "_platform_credentials", AsyncMock(return_value=_creds("platform-token"))
    ):
        await service.retract_job("job-legacy", SA_TENANT, OWNER_A)

    assert youtube.async_set_private.await_args.args[0].token == "platform-token"


async def test_published_via_check_constraint_rejects_unknown_channel(db_session_factory):
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError, match="ck_upload_jobs_published_via"):
        await _seed_published(db_session_factory, "job-bad-via", OWNER_A, "elsewhere")
