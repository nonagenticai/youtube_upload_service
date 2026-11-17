"""Proves the Phase D RLS policies ISOLATE tenants (not just that they exist).

Connects AS a throwaway non-owner ``NOBYPASSRLS`` role — the posture the runtime
``w2v_youtube_app`` role has under enforce — and asserts the shipped policies
(applied by ``register_rls_ddl`` on ``create_all``) actually scope reads/writes.
Needs a superuser test DB (to ``CREATE ROLE`` + ``SET ROLE``); skips loudly in CI
where the DB user lacks ``CREATEROLE``.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

TENANT_A = "11111111-1111-1111-1111-111111111111"
TENANT_B = "22222222-2222-2222-2222-222222222222"
_ROLE = "w2v_youtube_app_rls_test"

# token_uri is NOT NULL with only a client-side default (no server_default), so a
# raw-SQL seed must supply it (create_all builds the schema without the ORM default).
_INS_OAUTH = (
    "INSERT INTO oauth_tokens (tenant_id, access_token, refresh_token, token_uri, scopes) "
    "VALUES (:tenant, 'a', 'r', 'https://oauth2.googleapis.com/token', 's')"
)
_INS_JOB = (
    "INSERT INTO upload_jobs (job_id, tenant_id, file_source, title) "
    "VALUES (:job_id, :tenant, '/tmp/v.mp4', 't')"
)


@pytest.mark.asyncio
async def test_rls_isolates_tenants_under_restricted_role(db_engine) -> None:
    job_a, job_b = str(uuid.uuid7()), str(uuid.uuid7())
    async with db_engine.begin() as conn:
        # seed as the owner (bypasses RLS) — one row per tenant in each table
        await conn.execute(text(_INS_OAUTH), {"tenant": TENANT_A})
        await conn.execute(text(_INS_OAUTH), {"tenant": TENANT_B})
        await conn.execute(text(_INS_JOB), {"job_id": job_a, "tenant": TENANT_A})
        await conn.execute(text(_INS_JOB), {"job_id": job_b, "tenant": TENANT_B})

        try:
            await conn.execute(text(f'CREATE ROLE "{_ROLE}" NOSUPERUSER NOBYPASSRLS NOINHERIT'))
        except ProgrammingError as exc:  # CI user lacks CREATEROLE
            pytest.skip(f"cannot CREATE ROLE (needs superuser/CREATEROLE): {exc.orig}")
        await conn.execute(
            text(f'GRANT SELECT, INSERT, UPDATE, DELETE ON oauth_tokens TO "{_ROLE}"')
        )
        await conn.execute(
            text(f'GRANT SELECT, INSERT, UPDATE, DELETE ON upload_jobs TO "{_ROLE}"')
        )

        try:
            await conn.execute(text(f'SET LOCAL ROLE "{_ROLE}"'))

            # no GUC → fail-closed (0 rows in both tables)
            for table in ("oauth_tokens", "upload_jobs"):
                n = (await conn.execute(text(f"SELECT count(*) FROM {table}"))).scalar()  # noqa: S608
                assert n == 0, f"{table} leaked {n} rows with no GUC"

            # GUC = tenant A → only A's rows, never B's
            await conn.execute(
                text("SELECT set_config('app.current_tenant', :t, true)"), {"t": TENANT_A}
            )
            for table in ("oauth_tokens", "upload_jobs"):
                # ``tenant_id`` is a real ``uuid`` column (migration 010). This is a
                # RAW ``text()`` query, so it has no SQLAlchemy type attached and
                # psycopg hands back ``uuid.UUID`` objects regardless of the model's
                # ``as_uuid=False`` (which only governs the ORM/Core-typed path the
                # service actually uses). ``uuid.UUID(t) != t`` for every str ``t``,
                # so compare canonical text on both sides — otherwise this assertion
                # fails on correctly-isolated rows and says nothing about isolation.
                rows = (await conn.execute(text(f"SELECT tenant_id FROM {table}"))).scalars().all()  # noqa: S608
                assert [str(r) for r in rows] == [TENANT_A], (
                    f"{table} not isolated under GUC=A: {rows}"
                )

            # cross-tenant WITH CHECK: insert stamped B while GUC=A is rejected
            await conn.execute(text("SAVEPOINT s"))
            with pytest.raises(ProgrammingError):
                await conn.execute(text(_INS_OAUTH), {"tenant": TENANT_B})
            await conn.execute(text("ROLLBACK TO SAVEPOINT s"))
        finally:
            await conn.execute(text("RESET ROLE"))
            await conn.execute(text(f'DROP OWNED BY "{_ROLE}"'))
            await conn.execute(text(f'DROP ROLE IF EXISTS "{_ROLE}"'))
