"""Tests for :mod:`src.tenant` — the single str -> uuid.UUID conversion.

Every tenant in this service passes through :func:`src.tenant.require_tenant`
before it can bind an RLS session or be written to a row, so this is the one
place worth testing exhaustively: a hole here is a hole in every call site.
"""

from __future__ import annotations

import uuid

import pytest
from w2v_common.tenant import InvalidTenantIdError, MissingTenantError

from src.tenant import require_tenant
from tests.tenants import NIL_UUID_SPELLINGS, tenant_uuid


@pytest.mark.unit
class TestRequireTenantAccepts:
    def test_returns_a_uuid_not_a_string(self) -> None:
        """The whole point of the module: w2v_common 0.67.0's
        ``bind_session_tenant`` takes ``uuid.UUID | None``, so returning a
        canonical *string* here would still be a type error at every call site."""
        result = require_tenant(tenant_uuid("tenant-a"))
        assert isinstance(result, uuid.UUID)
        assert str(result) == tenant_uuid("tenant-a")

    def test_accepts_a_uuid_object_unchanged(self) -> None:
        value = uuid.UUID(tenant_uuid("tenant-b"))
        assert require_tenant(value) == value

    @pytest.mark.parametrize(
        "spelling",
        [
            "8f1c2b60-0d8e-4a1f-9c3d-2b7e6a5f4d31",
            "8f1c2b600d8e4a1f9c3d2b7e6a5f4d31",
            "{8f1c2b60-0d8e-4a1f-9c3d-2b7e6a5f4d31}",
            "urn:uuid:8f1c2b60-0d8e-4a1f-9c3d-2b7e6a5f4d31",
        ],
        ids=["dashed", "undashed", "braced", "urn"],
    )
    def test_canonicalises_every_spelling_of_one_real_tenant(self, spelling: str) -> None:
        """One tenant, four spellings, ONE value. This is why the callers store
        ``str(require_tenant(...))`` rather than the raw header: otherwise a
        caller that switches spelling loses sight of its own rows."""
        assert str(require_tenant(spelling)) == "8f1c2b60-0d8e-4a1f-9c3d-2b7e6a5f4d31"

    def test_accepts_the_all_but_nil_tenant(self) -> None:
        """``…0001`` is a REAL ``active`` tenant owning 97 projects (CLAUDE.md),
        not a sentinel. The control for the nil test below: it proves the guard
        keys on the value zero, not on "a UUID that looks mostly like zeros"."""
        assert str(require_tenant("00000000-0000-0000-0000-000000000001")) == (
            "00000000-0000-0000-0000-000000000001"
        )


@pytest.mark.unit
class TestRequireTenantRefuses:
    @pytest.mark.parametrize("spelling", NIL_UUID_SPELLINGS)
    def test_refuses_the_nil_uuid_in_every_spelling(self, spelling: str) -> None:
        """The nil UUID is never a tenant.

        ``uuid.UUID`` accepts the dashed, undashed, braced and ``urn:uuid:``
        spellings of the same nil value, and w2v_common's older trusted-network
        path wrote exactly that value — so those rows exist in prod today. A
        guard that only checks ``value is None``, or one that string-compares
        against the dashed form (which is what the deleted
        ``UNBOUND_OWNER_TENANTS`` frozenset did), lets three of these four
        through. ``parsed.int == 0`` closes all of them at once.

        WHICH layer closes them moved in w2v-common 0.68.0: ``parse_tenant_id``
        now refuses the nil uuid itself, so it raises before ``require_tenant``
        reaches its own ``parsed.int == 0`` line, with its own wording. The
        ``match`` is therefore written against the REASON (nil) rather than
        either layer's exact sentence — both say "nil uuid" — so the pin holds
        whichever one fires and does not have to be rewritten if the guard moves
        again. It stays strict: the library's generic "tenant id is not a UUID
        (len=…)" refusal does NOT contain "nil uuid", so this cannot pass for
        the wrong reason, and ``test_refuses_a_legacy_slug`` covers that lane.
        """
        with pytest.raises(InvalidTenantIdError, match=r"(?i)nil uuid"):
            require_tenant(spelling)

    def test_refuses_a_nil_uuid_object(self) -> None:
        """Not only strings: a caller holding an already-parsed ``uuid.UUID``
        skips the parse branch entirely, so the nil check must sit AFTER it."""
        with pytest.raises(InvalidTenantIdError):
            require_tenant(uuid.UUID(int=0))

    @pytest.mark.parametrize(
        "value",
        ["anonymous", "none", "null", "n8n-dev", "tenant-1", "unknown"],
    )
    def test_refuses_a_legacy_slug(self, value: str) -> None:
        with pytest.raises(InvalidTenantIdError):
            require_tenant(value)

    @pytest.mark.parametrize("value", [None, "", "   "], ids=["none", "empty", "whitespace"])
    def test_refuses_an_absent_tenant(self, value: object) -> None:
        """An absent tenant RAISES; 0.67.0 deleted every fallback that used to
        substitute the nil UUID here."""
        with pytest.raises(MissingTenantError):
            require_tenant(value)

    def test_the_source_names_the_lane_in_the_message(self) -> None:
        """An operator must be able to tell WHICH boundary produced the bad
        tenant — the header, the OAuth state token, or a DB column."""
        with pytest.raises(InvalidTenantIdError, match="X-Owner-Tenant-Id"):
            require_tenant("00000000-0000-0000-0000-000000000000", source="X-Owner-Tenant-Id")

    def test_never_echoes_the_value(self) -> None:
        """An ``X-Tenant-Id`` field carries ``Bearer <license token>``. The
        message must report a length, never the value."""
        with pytest.raises(InvalidTenantIdError) as exc_info:
            require_tenant("Bearer super.secret.token")
        assert "super.secret.token" not in str(exc_info.value)
