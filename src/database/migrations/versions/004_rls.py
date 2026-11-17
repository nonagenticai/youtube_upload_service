"""Phase D RLS — enable row-level security on the tenant-scoped tables.

Applies the SAME policies the ``create_all`` ``register_rls_ddl`` event applies in
tests, so the two paths cannot drift. Both ``oauth_tokens`` and ``upload_jobs``
carry ``tenant_id`` (NOT NULL) → strict equality policy keyed on the per-request
GUC ``app.current_tenant``.

ENABLE, not FORCE: the DB owner ``w2v_service`` keeps bypassing RLS, so this is
**inert** until the runtime connects as the non-owner restricted role
``w2v_youtube_app`` with ``TENANT_RLS_MODE=enforce`` (the atomic gitops cutover).
Fully reversible.

⚠️ **These statements are PINNED LITERALS, deliberately.** This file used to call
``w2v_common.database.rls.upgrade_statements(Base.metadata)`` and execute
whatever it rendered from the *live* model metadata. That is a latent break: a
migration is immutable history, but the metadata it read is not. When
``010_tenant_id_uuid`` flipped the model columns from ``String(255)`` to
``postgresql.UUID``, ``_predicate`` (which keys the ``::uuid`` GUC cast off
``isinstance(col.type, String)``) started emitting

    tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid

*here at revision 004*, where the columns are still ``character varying`` —
``alembic upgrade head`` on a fresh database then died with
``operator does not exist: character varying = uuid``. Measured, not
theoretical: it broke every from-scratch CI/dev bootstrap.

The literals below are exactly what ``upgrade_statements`` /
``downgrade_statements`` rendered for this schema BEFORE that flip (captured and
diffed, not retyped from memory), so the historical behaviour is unchanged. The
``::uuid`` predicate is installed by ``010_tenant_id_uuid``, in the same
migration that changes the column type — which is where it belongs. Adding a
third ``tenant_id`` table later must add DDL to a NEW revision, not silently
mutate this one.

Revision ID: 004_rls
Revises: 003_add_caption_source
Create Date: 2026-06-16
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "004_rls"
down_revision = "003_add_caption_source"
branch_labels = None
depends_on = None

# Tables carrying a NOT NULL ``tenant_id`` at THIS point in the revision chain.
TABLES = ("oauth_tokens", "upload_jobs")

# The text-to-text predicate that matches a ``character varying`` tenant_id.
# ``current_setting(..., true)`` is missing-ok (unset → NULL); ``NULLIF(…, '')``
# folds the empty string a pooled PgBouncer connection leaves behind back to
# NULL, so the predicate is simply false (0 rows) rather than raising.
_PREDICATE = "tenant_id = NULLIF(current_setting('app.current_tenant', true), '')"

# Every policy name w2v_common may create (strict + the nullable "global-or-own"
# variants), so the downgrade is exhaustive regardless of which shape was applied.
_ALL_POLICY_NAMES = (
    "tenant_isolation",
    "tenant_isolation_read",
    "tenant_isolation_ins",
    "tenant_isolation_upd",
    "tenant_isolation_del",
)


def upgrade() -> None:
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
        )


def downgrade() -> None:
    for table in TABLES:
        for policy in _ALL_POLICY_NAMES:
            op.execute(f"DROP POLICY IF EXISTS {policy} ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
