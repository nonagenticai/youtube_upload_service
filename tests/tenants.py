"""Real tenant UUIDs for tests, derived deterministically from the old slug names.

Every tenant id in this service is now a real ``uuid.UUID`` end to end
(w2v_common 0.67.0: ``bind_session_tenant`` takes ``uuid.UUID | None``, the auth
middleware 401s a non-UUID tenant, and there is no fallback tenant left to
substitute). The suite used to identify test tenants by readable slugs —
``"tenant-1"``, ``"owner-tenant-7"``, ``"test-tenant"`` — which are now exactly
the values production refuses.

Rather than scatter opaque literal UUIDs through the tests and lose the
readability, :func:`tenant_uuid` maps each of those slugs onto a stable UUID5.
That preserves what the assertions were actually testing:

* the same slug always yields the same UUID, so "these two rows belong to the
  same tenant" still holds;
* different slugs always yield different UUIDs, so every cross-tenant isolation
  assertion still separates two genuinely different tenants;
* the value is a real, non-nil UUID, so it survives ``require_tenant`` — the
  tests exercise the same acceptance path a live request takes.

UUID5 (not ``uuid7``) on purpose: it is a pure function of the slug, so a test
tenant is reproducible across processes, xdist workers and reruns. The
``uuid.uuid4`` ban in ``[tool.ruff.lint.flake8-tidy-imports]`` does not apply —
``uuid5`` is deterministic, which is the whole point here.
"""

from __future__ import annotations

import uuid

__all__ = ["NIL_UUID_SPELLINGS", "TENANT_NAMESPACE", "tenant_uuid"]

#: Every spelling ``uuid.UUID`` accepts for the nil UUID. They are four distinct
#: STRINGS that all parse to the same VALUE (``UUID(...).int == 0``), which is why
#: a string denylist can never guard against the sentinel and
#: ``src.tenant.require_tenant`` compares the parsed integer instead. Any guard
#: that lets one of these through lets a write-only, unreadable row into the DB.
NIL_UUID_SPELLINGS = (
    "00000000-0000-0000-0000-000000000000",
    "00000000000000000000000000000000",
    "{00000000-0000-0000-0000-000000000000}",
    "urn:uuid:00000000-0000-0000-0000-000000000000",
)

#: Namespace for this repo's test tenants. Any fixed UUID works; this one is
#: uuid5(DNS, "youtube-upload-service.tests") so it is itself reproducible.
TENANT_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "youtube-upload-service.tests")


def tenant_uuid(slug: str) -> str:
    """Return the stable, real (non-nil) tenant UUID for a test *slug*, as a string.

    Returned as ``str`` because that is what the API layer, the ``String(255)``
    ``tenant_id``/``owner_tenant_id`` columns and the HTTP headers all carry;
    the conversion to ``uuid.UUID`` happens once, in ``src.tenant.require_tenant``,
    exactly as it does for a real request.
    """
    return str(uuid.uuid5(TENANT_NAMESPACE, slug))
