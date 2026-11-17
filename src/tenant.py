"""Tenant-id coercion for this service — the one place a ``str`` becomes a ``uuid.UUID``.

``w2v_common`` 0.67.0 removed every tenant fallback: ``config.auth.default_tenant_id``,
``UNATTRIBUTED_TENANT_ID`` and ``DEFAULT_TENANT_ID`` are gone, and an absent tenant now
RAISES (:class:`w2v_common.tenant.MissingTenantError` /
:class:`~w2v_common.tenant.InvalidTenantIdError`) instead of being silently substituted
with the nil UUID. ``bind_session_tenant`` correspondingly takes ``uuid.UUID | None``,
not ``str``.

This module is that conversion, in ONE place, so the rule "a tenant id is a real
registered tenant or the request fails" cannot drift between call sites. Everything
upstream of Postgres — request state, Redis OAuth-state payloads, log fields, the
``String(255)`` ``tenant_id``/``owner_tenant_id`` columns — keeps carrying the canonical
``str`` spelling; the RLS GUC bind is the choke point that every query runs behind, so
that is where the ``uuid.UUID`` is required.

Why the nil check is by VALUE and not by string comparison
----------------------------------------------------------
The nil UUID has at least four accepted spellings, and ``uuid.UUID`` parses all of
them to the same value::

    "00000000-0000-0000-0000-000000000000"
    "00000000000000000000000000000000"
    "{00000000-0000-0000-0000-000000000000}"
    "urn:uuid:00000000-0000-0000-0000-000000000000"

A string denylist (which is what ``UNBOUND_OWNER_TENANTS`` used to be) only ever
covered the first of those, so the other three sailed through and were written to
``upload_jobs.owner_tenant_id`` as an unreadable row. And w2v_common's *older*
trusted-network path wrote exactly that nil value on purpose, so those rows exist in
prod today — a ``None``-only guard therefore leaves the hole wide open. Comparing
``parsed.int == 0`` closes every spelling at once, now and for any spelling added
later.

``00000000-0000-0000-0000-000000000001`` is deliberately NOT rejected: it looks like a
placeholder but is a real ``active`` tenant owning 97 projects (see ``CLAUDE.md``).
Only the all-zeros value is a sentinel.

Who actually refuses the nil UUID, as of w2v-common 0.68.0
---------------------------------------------------------
``parse_tenant_id`` now rejects it itself, on both of its paths (an
already-constructed ``uuid.UUID`` and text), checked on the PARSED value — so it
covers every spelling listed above for the same reason this module does. It
therefore raises :class:`~w2v_common.tenant.InvalidTenantIdError` **before** the
``parsed.int == 0`` check below is ever reached, which makes that check
UNREACHABLE and this module's refusal message unobservable.

The check is kept rather than deleted: it is defence-in-depth against a
regression in the library, it costs one comparison, and deleting it would leave
this service with no local statement of a rule it depends on. Callers and tests
should not care which layer refused — both raise ``InvalidTenantIdError`` and
both name the ``source`` lane in the message.
"""

from __future__ import annotations

import uuid

from w2v_common.tenant import InvalidTenantIdError, parse_tenant_id

__all__ = ["require_tenant"]


def require_tenant(value: object, *, source: str = "tenant_id") -> uuid.UUID:
    """Return ``value`` as a real, non-nil ``uuid.UUID`` or raise.

    Args:
        value: The candidate tenant id — a ``str``, a ``uuid.UUID``, or ``None``.
        source: Names the origin (a header, a DB column, an OAuth ``state`` payload)
            so the exception message says WHICH lane produced an unusable tenant.

    Raises:
        MissingTenantError: ``value`` is ``None``, empty, or whitespace-only.
        InvalidTenantIdError: ``value`` is not a UUID (a legacy slug such as
            ``anonymous`` or ``tenant-1``), or is the nil UUID in any spelling.
    """
    parsed = parse_tenant_id(value, source=source)
    # Unreachable since w2v-common 0.68.0 — `parse_tenant_id` refuses the nil
    # uuid first. Kept as defence-in-depth; see the module docstring.
    if parsed.int == 0:
        raise InvalidTenantIdError(
            f"{source}: nil UUID is not a tenant — every row must carry a real registered tenant"
        )
    return parsed
