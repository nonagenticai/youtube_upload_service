"""Per-tenant YouTube settings + the platform channel, in the database.

The platform channel and the per-tenant choices lived in gitops env
(``YOUTUBE_FALLBACK_*``, ``YOUTUBE_PLATFORM_CHANNEL_AS_DEFAULT``): one switch for
every tenant, and no way for a tenant to bring its own Google OAuth client — so a
channel owned by an account the platform client's consent screen does not admit
could never connect. Blueprint: ``w2v-youtube-tenant-config-blueprint.md``.

    youtube_platform_channel   singleton (id = 1), NOT tenant data, no RLS
    tenant_youtube_settings    one row per tenant, RLS tenant_isolation (uuid GUC)
    oauth_tokens.oauth_client_id   the client that issued the grant (NULL = platform client)

Secrets (``*_enc``) are Fernet ciphertext written by the service; never plaintext.

Drift-tolerant: ``CREATE TABLE IF NOT EXISTS`` / ``ADD COLUMN IF NOT EXISTS`` /
``DROP POLICY IF EXISTS`` before ``CREATE POLICY``, so a ``create_all`` test DB or
a re-run upgrades cleanly. Inline literal DDL only — nothing is rendered from the
live model metadata (see ``004_rls`` for why that broke fresh bootstraps).

Grants: the runtime connects as the restricted ``w2v_youtube_app`` role under
``TENANT_RLS_MODE=enforce``. It needs DML on both new tables (the boot seed and
the operator routes write ``youtube_platform_channel`` from the runtime). The
GRANT is guarded on the role existing — it does not exist in CI or dev.

Revision ID: 014_tenant_youtube_settings
Revises: 013_upload_job_published_via
Create Date: 2026-10-09
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "014_tenant_youtube_settings"
down_revision = "013_upload_job_published_via"
branch_labels = None
depends_on = None

_APP_ROLE = "w2v_youtube_app"

# Same uuid predicate 010 installed on oauth_tokens / upload_jobs.
_PREDICATE = "tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid"

# Pinned literal (identical to src.database.rls_resolvers.LIST_TENANT_SETTINGS_FN_SQL
# at the time of writing; a test asserts equality). Cross-tenant operator listing
# under RLS enforce — SECURITY DEFINER, like the 006 reaper functions.
LIST_FN_SQL = """
CREATE OR REPLACE FUNCTION list_tenant_youtube_settings()
RETURNS TABLE (
  tenant_id uuid,
  platform_channel_allowed boolean,
  has_grant boolean,
  has_oauth_client boolean
)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path FROM CURRENT AS $fn$
  SELECT COALESCE(s.tenant_id, t.tenant_id),
         s.platform_channel_allowed,
         t.tenant_id IS NOT NULL,
         COALESCE(s.oauth_client_id IS NOT NULL, false)
    FROM tenant_youtube_settings s
    FULL OUTER JOIN oauth_tokens t ON t.tenant_id = s.tenant_id
   ORDER BY 1
$fn$
"""

_POLICY_NAMES = (
    "tenant_isolation",
    "tenant_isolation_read",
    "tenant_isolation_ins",
    "tenant_isolation_upd",
    "tenant_isolation_del",
)


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS youtube_platform_channel (
            id smallint NOT NULL DEFAULT 1,
            client_id text NOT NULL,
            client_secret_enc text NOT NULL,
            refresh_token_enc text NOT NULL,
            channel_id text,
            channel_title text,
            default_for_tenants boolean NOT NULL DEFAULT false,
            updated_by text,
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT youtube_platform_channel_pkey PRIMARY KEY (id),
            CONSTRAINT ck_youtube_platform_channel_singleton CHECK (id = 1)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS tenant_youtube_settings (
            tenant_id uuid NOT NULL,
            oauth_client_id text,
            oauth_client_secret_enc text,
            platform_channel_allowed boolean,
            updated_by text,
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT tenant_youtube_settings_pkey PRIMARY KEY (tenant_id),
            CONSTRAINT ck_tenant_youtube_settings_client_pair
                CHECK ((oauth_client_id IS NULL) = (oauth_client_secret_enc IS NULL))
        )
        """
    )
    op.execute("ALTER TABLE oauth_tokens ADD COLUMN IF NOT EXISTS oauth_client_id text")

    # RLS: ENABLE, never FORCE (the owner keeps its bypass; the runtime role is
    # isolated). Same shape as oauth_tokens.
    op.execute("ALTER TABLE tenant_youtube_settings ENABLE ROW LEVEL SECURITY")
    for policy in _POLICY_NAMES:
        op.execute(f"DROP POLICY IF EXISTS {policy} ON tenant_youtube_settings")
    op.execute(
        "CREATE POLICY tenant_isolation ON tenant_youtube_settings "
        f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
    )

    op.execute(LIST_FN_SQL)
    op.execute("REVOKE EXECUTE ON FUNCTION list_tenant_youtube_settings() FROM PUBLIC")

    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_APP_ROLE}') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE ON youtube_platform_channel TO {_APP_ROLE};
                GRANT SELECT, INSERT, UPDATE, DELETE ON tenant_youtube_settings TO {_APP_ROLE};
                GRANT EXECUTE ON FUNCTION list_tenant_youtube_settings() TO {_APP_ROLE};
            END IF;
        END
        $$
        """  # noqa: S608 — constant role name, no external input
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS list_tenant_youtube_settings()")
    op.execute("ALTER TABLE oauth_tokens DROP COLUMN IF EXISTS oauth_client_id")
    op.execute("DROP TABLE IF EXISTS tenant_youtube_settings")
    op.execute("DROP TABLE IF EXISTS youtube_platform_channel")
