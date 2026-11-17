"""Pin the tenant-key column type: real Postgres ``uuid``, ``str`` in Python.

Two independent things are asserted here, and BOTH matter.

1. **The DDL type is ``uuid``.** Migration ``010_tenant_id_uuid`` flipped
   ``oauth_tokens.tenant_id``, ``upload_jobs.tenant_id`` and
   ``upload_jobs.owner_tenant_id`` off ``character varying(255)``. If a model
   ever regressed to ``String``, ``create_all`` (tests/dev) would build a schema
   that no longer matches prod, and — worse — ``w2v_common.database.rls``
   ``_predicate`` keys the ``::uuid`` GUC cast off ``isinstance(col.type,
   String)``, so the RLS policy would silently revert to a text comparison.

2. **``as_uuid`` is False.** With ``as_uuid=True`` SQLAlchemy hands back
   ``uuid.UUID`` objects, and ``uuid.UUID(t) == t`` is ``False`` for every string
   ``t``. That is not a type error, so ruff and ``ty`` stay green while ownership
   checks, ``ContextVar[str]`` binds, and pydantic ``tenant_id: str`` response
   fields break at RUNTIME, in production only. The third test below is the
   mutation guard: it demonstrates the exact silent falsehood ``as_uuid=True``
   would introduce, so the reason for the pin cannot be lost.

``owner_tenant_id`` staying NULLABLE is asserted too: NULL there means "no
distinct caller owner" (four such rows exist in prod) and migration 010
deliberately neither backfills them nor makes the column NOT NULL.
"""

from __future__ import annotations

import uuid

from sqlalchemy import String
from sqlalchemy.dialects.postgresql import UUID as PgUUID  # noqa: N811

from src.database.models import OAuthToken, UploadJob

TENANT_COLUMNS = (
    (OAuthToken, "tenant_id", False),
    (UploadJob, "tenant_id", False),
    (UploadJob, "owner_tenant_id", True),
)


def test_tenant_columns_are_postgres_uuid() -> None:
    for model, column_name, _nullable in TENANT_COLUMNS:
        col = model.__table__.columns[column_name]
        assert isinstance(col.type, PgUUID), (
            f"{model.__tablename__}.{column_name} must be a Postgres uuid "
            f"(migration 010), got {col.type!r}"
        )
        # Not a String: w2v_common's RLS _predicate emits the ``::uuid`` GUC cast
        # only for a non-String column. A String here silently un-casts the policy.
        assert not isinstance(col.type, String)


def test_tenant_columns_return_str_not_uuid_objects() -> None:
    for model, column_name, _nullable in TENANT_COLUMNS:
        col_type = model.__table__.columns[column_name].type
        # Narrowing, not a second copy of the test above: ``TypeEngine`` has no
        # ``as_uuid``, so the checker needs the isinstance before the real assert.
        assert isinstance(col_type, PgUUID)
        assert col_type.as_uuid is False, (
            f"{model.__tablename__}.{column_name}: as_uuid must stay False. "
            "as_uuid=True returns uuid.UUID objects, which compare False against "
            "every equivalent str and break ownership checks and pydantic "
            "str-typed response fields with no type error anywhere."
        )


def test_uuid_object_does_not_equal_its_own_string() -> None:
    """Why the pin exists: this asymmetry is what as_uuid=True would inject."""
    text = "019de842-c92a-7e37-8fdd-eb0d8c5396f9"
    assert uuid.UUID(text) != text
    assert str(uuid.UUID(text)) == text


def test_owner_tenant_id_stays_nullable() -> None:
    """Four prod rows have a NULL owner. Migration 010 must not have changed that."""
    for model, column_name, nullable in TENANT_COLUMNS:
        col = model.__table__.columns[column_name]
        assert col.nullable is nullable, (
            f"{model.__tablename__}.{column_name} nullability changed; "
            "NULL owner_tenant_id means 'no distinct caller owner', not missing data."
        )
