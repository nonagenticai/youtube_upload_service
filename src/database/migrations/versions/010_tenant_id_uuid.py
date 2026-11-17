"""Fleet ruling: ``tenant_id`` (and ``owner_tenant_id``) are real ``uuid``, never text.

Three columns in this service carried a tenant key as ``character varying(255)``:
``oauth_tokens.tenant_id``, ``upload_jobs.tenant_id`` and
``upload_jobs.owner_tenant_id``. Measured live before writing this (2026-08-27):
0 / 48 / 48 rows, and **zero** dirty values — every non-NULL value already parses
as a uuid. So there is no name -> uuid backfill here, unlike ``content_service``'s
``006_tenant_id_uuid``; this is a pure type flip.

**The 4 NULL ``owner_tenant_id`` rows are left exactly as they are.**
``owner_tenant_id`` is NULLABLE by design (``Mapped[str | None]``, added by
``005_add_owner_tenant_id`` for rows created before the column existed); NULL
means "no distinct caller owner", not "missing data". ``NULL::uuid`` casts
cleanly, ``ALTER COLUMN ... TYPE`` preserves nullability, and the guard below is
deliberately ``WHERE col IS NOT NULL AND col !~ <uuid regex>`` so NULLs are
excluded from it. Nothing in this migration backfills, deletes, or NOT-NULLs
them. Inventing a value for those rows would silently hand four legacy jobs to
whichever tenant the invented value named.

Order is load-bearing:

1. **Drop the RLS policies first.** Postgres refuses ``ALTER TABLE ... ALTER
   COLUMN ... TYPE`` on a column referenced by a policy predicate (``cannot
   alter type of a column used in a policy definition``). ``004_rls`` created a
   ``tenant_isolation`` policy on ``upload_jobs`` and ``oauth_tokens`` comparing
   ``tenant_id`` to the ``app.current_tenant`` GUC as **text**; leaving them in
   place aborts the migration outright.
2. **Guard, fail loud.** A ``DO`` block counts every remaining non-uuid,
   non-NULL value per column and ``RAISE EXCEPTION``s with the count and first
   offender. Deliberately NOT a ``WHERE col ~ ...`` filter on the ALTER: a
   filter would quietly leave unconvertible rows behind (or drop them), and
   silent data loss on a tenant key is worse than a failed migration. If this
   raises, someone wrote a non-uuid tenant value and it must be triaged by hand.
3. **Cast** each column to ``uuid``. Nullability is preserved; the indexes
   (``ix_upload_jobs_tenant_status``, the ``owner_tenant_id`` index) and the
   partial unique index ``uq_upload_jobs_completed_owner_source`` from ``008``
   are rebuilt automatically by Postgres. With 0 dirty rows there is no
   collision risk on the rebuild.
4. **Recreate the policies with a ``::uuid`` cast on the GUC.** The GUC is
   always text; against a ``uuid`` column the old text predicate would raise
   ``operator does not exist: uuid = text``. Getting this wrong returns ZERO
   rows (silently, not as an error) from the pod after deploy, so it is
   re-created in the SAME migration.

The policies key off ``tenant_id`` ONLY — ``owner_tenant_id`` is application
scope, enforced in ``UploadService``, and never appeared in a policy predicate.
No owner predicate is added here.

The predicates are written out literally rather than rendered from
``w2v_common.database.rls.upgrade_statements(Base.metadata)`` (the style
``004_rls`` uses) on purpose: a migration is immutable history, and a
metadata-rendered statement would start emitting DDL for tables that do not
exist yet at this point in the revision chain the day a third ``tenant_id``
table is added. It matches what ``register_rls_ddl`` renders for the new column
type (``_predicate`` keys the cast off ``isinstance(col.type, String)``, and
``postgresql.UUID`` is not a ``String`` — verified), so migration and
``create_all`` still agree.

No PL/pgSQL function or view in this database references ``tenant_id``
(``reap_stranded_upload_jobs`` / ``expire_upload_job_leases`` from ``006`` touch
only status/lease/timestamp columns), so no function needs recreating.

Downgrade restores ``character varying(255)`` and the text predicate, and is
lossless: a uuid renders back to its canonical text form.

Revision ID: 010_tenant_id_uuid
Revises: 009_reaper_message_accuracy
Create Date: 2026-08-27
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "010_tenant_id_uuid"
down_revision = "009_reaper_message_accuracy"
branch_labels = None
depends_on = None

# Tables carrying an RLS-policed ``tenant_id``, pinned in time (see docstring).
RLS_TABLES = ("oauth_tokens", "upload_jobs")

# Every (table, column) being flipped. ``owner_tenant_id`` is NOT RLS-policed but
# is still a tenant key and still must be a uuid.
COLUMNS = (
    ("oauth_tokens", "tenant_id"),
    ("upload_jobs", "tenant_id"),
    ("upload_jobs", "owner_tenant_id"),
)

# 004_rls / w2v_common render exactly this. The GUC is transaction-scoped and
# always text; NULLIF folds the empty string PgBouncer leaves behind back to NULL
# so the predicate is simply false (0 rows) instead of raising on ''::uuid.
_GUC = "NULLIF(current_setting('app.current_tenant', true), '')"
_PREDICATE_UUID = f"tenant_id = {_GUC}::uuid"
_PREDICATE_TEXT = f"tenant_id = {_GUC}"

_UUID_RE = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"

# Policy names w2v_common may have created: the strict variant (NOT NULL
# tenant_id, which is what both tables have) plus the nullable "global-or-own"
# variants, so the drop is exhaustive regardless of which shape was applied.
_POLICY_NAMES = (
    "tenant_isolation",
    "tenant_isolation_read",
    "tenant_isolation_ins",
    "tenant_isolation_upd",
    "tenant_isolation_del",
)


def _drop_policies() -> None:
    for table in RLS_TABLES:
        for policy in _POLICY_NAMES:
            op.execute(f"DROP POLICY IF EXISTS {policy} ON {table}")


def _create_policies(predicate: str) -> None:
    # ENABLE, never FORCE: the owner (w2v_service) must keep its bypass; the
    # runtime isolates because it connects as the restricted non-owner
    # w2v_youtube_app role. See w2v_common.database.rls.
    for table in RLS_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING ({predicate}) WITH CHECK ({predicate})"
        )


def _guard() -> None:
    """RAISE on any non-NULL value that is not a uuid. NULLs are excluded by design."""
    for table, column in COLUMNS:
        op.execute(
            f"""
            DO $$
            DECLARE
                bad_count bigint;
                sample text;
            BEGIN
                SELECT count(*), min({column})
                  INTO bad_count, sample
                  FROM {table}
                 WHERE {column} IS NOT NULL
                   AND {column} !~ '{_UUID_RE}';
                IF bad_count > 0 THEN
                    RAISE EXCEPTION
                        '{table}.{column}: % row(s) are not a uuid and cannot be '
                        'cast (first offender: %). Triage these by hand - map each '
                        'tenant NAME to its uuid in licenses.tenants and backfill '
                        'before re-running. Refusing to drop or skip rows.',
                        bad_count, sample;
                END IF;
            END $$;
            """  # noqa: S608
        )


def upgrade() -> None:
    # 1. Policies reference tenant_id -> they block the type change.
    _drop_policies()

    # 2. Fail loud on anything non-uuid. No silent skip, no row loss, no backfill
    #    of legitimately-NULL owner_tenant_id.
    _guard()

    # 3. The cast. Preserves nullability; rebuilds indexes + the partial unique
    #    index from 008 automatically.
    for table, column in COLUMNS:
        op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} TYPE uuid USING {column}::uuid")

    # 4. Re-create RLS with the GUC cast to uuid, or the pod reads 0 rows.
    _create_policies(_PREDICATE_UUID)


def downgrade() -> None:
    _drop_policies()

    for table, column in COLUMNS:
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN {column} TYPE character varying(255) "
            f"USING {column}::text"
        )

    # Back to the text-to-text predicate that matches a varchar column.
    _create_policies(_PREDICATE_TEXT)
