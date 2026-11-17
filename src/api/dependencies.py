"""FastAPI Depends() helpers for dependency injection."""

from __future__ import annotations

import logging
from collections.abc import Mapping

from fastapi import Depends, HTTPException, Request
from w2v_common.auth.cell_jwt import CellClaims, CellScope
from w2v_common.auth.cell_service import SERVICE_LANE_STATE_ATTR, require_cell_service_caller
from w2v_common.auth.entitlement import require_license
from w2v_common.auth.identity_context import get_tenant_source_from_context
from w2v_common.cache.redis_client import RedisClient
from w2v_common.config import AuthSettings
from w2v_common.tenant import TenantError, TenantSource

from src.infra.task_manager import TaskManager
from src.services.oauth_service import OAuthService
from src.services.upload_service import UploadService
from src.services.youtube_config_service import YouTubeConfigService
from src.tenant import require_tenant

logger = logging.getLogger(__name__)


def get_tenant_id(request: Request) -> str:
    """The tenant whose YouTube channel this request acts on — the key under which
    its OAuth credentials are stored and looked up. It is the CALLER's tenant,
    resolved per request by w2v_common's AuthMiddleware:

    * Uploads arrive from the orchestrator with its service-account token plus
      ``X-Effective-Tenant-Id``. The SA subject is listed in
      ``AUTH_DELEGATING_SUBJECTS``, so w2v_common re-scopes ``tenant_id`` from the
      SA's own tenant to the effective (end-user) tenant — each tenant publishes
      with its own grant, not a shared one.
    * Connect / disconnect / status arrive the same way (SPA -> w2v-frontend ->
      orchestrator, gitops #4565); the tenant they act on is the OWNER tenant
      (:func:`get_auth_owner_tenant_id`), so this value only proves the request
      authenticated.

    FAIL CLOSED. This used to return the literal string ``"anonymous"`` when
    ``request.state.tenant_id`` was unset. That fallback is exactly the class of
    substitution w2v_common 0.67.0 deleted fleet-wide: ``"anonymous"`` is not a
    tenant, it reached ``bind_session_tenant`` as the RLS binding for a real
    Postgres session, and it was written into ``oauth_tokens.tenant_id`` — so a
    request that never authenticated could read and overwrite an unauthenticated
    channel's stored Google refresh token. An unbound channel tenant is an
    authentication failure and must be answered as one.

    Returns:
        The tenant id in canonical dashed form. Canonicalising here (rather than
        passing the raw header spelling through) means the undashed/braced/urn
        spellings of one tenant can no longer create two distinct
        ``oauth_tokens`` rows.

    Raises:
        HTTPException: 401 when no real tenant is bound to the request.
    """
    try:
        return str(require_tenant(getattr(request.state, "tenant_id", None), source="X-Tenant-Id"))
    except TenantError as exc:
        # WARNING, not INFO: this rejects the whole request, and under the
        # fleet's LOG_LEVEL=WARNING an INFO line would make a misconfigured
        # caller's 401 storm completely invisible. ``exc`` never echoes the
        # value — an X-Tenant-Id field carries a license token.
        logger.warning(
            "Rejecting %s %s: no channel tenant bound (%s)",
            request.method,
            request.url.path,
            exc,
        )
        raise HTTPException(
            status_code=401,
            detail=(
                "No tenant is bound to this request. /api/auth* and /api/upload* "
                "require a valid X-Tenant-Id license token that introspects to a real "
                f"registered tenant ({exc})."
            ),
        ) from exc


OWNER_TENANT_HEADER = "X-Owner-Tenant-Id"


def _delegating_subjects() -> frozenset[str]:
    """``AUTH_DELEGATING_SUBJECTS`` (CSV), read at call time like w2v_common does."""
    raw = AuthSettings().delegating_subjects or ""
    return frozenset(s.strip() for s in raw.split(",") if s.strip())


#: The orchestrator's cell identity (the ``iss`` of its P7 cell tokens).
ORCHESTRATOR_CELL_CALLER = "w2v-orchestrator"

_ORCHESTRATOR_IDENTITY = (
    f"the orchestrator service account (cell caller {ORCHESTRATOR_CELL_CALLER}, or a "
    "licence-authenticated subject listed in AUTH_DELEGATING_SUBJECTS)"
)


def cell_lane_claims(request: Request) -> CellClaims | None:
    """The signed claims when the P7 cell LANE authenticated this request, else ``None``.

    Same rule as w2v_common's entitlement gate: ``request.state.cell_claims``
    alone is not enough. In ``CELL_AUTH_MODE=dual`` a valid cell token riding
    alongside a licence / JWT is only RECORDED while the legacy lane
    authenticates (and resolves the tenant), so that request keeps the legacy
    semantics here too. Only the cell lane binds the tenant with provenance
    ``CELL_JWT``. Service-scope (tenant-less) tokens are never a cell-lane
    identity for these tenant routes.
    """
    claims = getattr(request.state, "cell_claims", None)
    if not isinstance(claims, CellClaims) or claims.scope is not CellScope.TENANT:
        return None
    if get_tenant_source_from_context() is not TenantSource.CELL_JWT:
        return None
    return claims


def caller_is_orchestrator_sa(request: Request) -> bool:
    """True only when the request authenticated as the orchestrator.

    Cell lane (P7): the verified cell token's issuer is ``w2v-orchestrator``.
    The issuer is bound to the signing key by w2v_common, so no other service
    (and no header) can claim it. When the cell lane authenticated, the licence
    allowlist below is NOT consulted.

    Licence lane (dual-mode fallback, no cell-lane identity): the licence lane
    of w2v_common's ``AuthMiddleware`` ran (``request.state.license`` is the raw
    introspection result — ``None`` on every other lane) AND the INTROSPECTED
    ``subject_id`` is listed in ``AUTH_DELEGATING_SUBJECTS``. The introspected
    subject is read from the licences service's answer, never from a request
    header (``X-Subject-Id`` is forwardable and re-binds ``request.state.name``
    on delegation), so a customer token cannot pass this by sending headers.
    This is the same allowlist w2v_common uses to honour
    ``X-Effective-Tenant-Id``: whoever may act for another tenant there may
    assert the owner tenant here.
    """
    claims = cell_lane_claims(request)
    if claims is not None:
        return claims.issuer == ORCHESTRATOR_CELL_CALLER
    introspection = getattr(request.state, "license", None)
    if not isinstance(introspection, Mapping):
        return False
    subject = introspection.get("subject_id")
    return bool(subject) and str(subject) in _delegating_subjects()


def get_owner_tenant_id(request: Request) -> str:
    """The REAL caller tenant, forwarded by the orchestrator as the
    ``X-Owner-Tenant-Id`` header.

    The header is honoured ONLY from the orchestrator service account
    (:func:`caller_is_orchestrator_sa`); anyone else asserting it gets 403. It
    used to be trusted on the assumption that only the orchestrator could reach
    these routes — but any licence holder can, and since the owner tenant now
    selects WHICH tenant's YouTube grant publishes/retracts a video, an
    unchecked header would let one tenant publish on another tenant's channel.

    FAIL CLOSED when the header is absent, blank, or not a real tenant. The old
    fallback to the channel tenant made ``create_job``'s owner guard INERT: the
    channel tenant is a real non-sentinel UUID, identical for every caller, so a
    header-less request sailed past the guard and wrote exactly the write-only
    row it exists to prevent — and on the read side it scoped ``get_job`` /
    ``list_jobs`` to that same shared value.

    The guard used to be a lowercase string denylist (``UNBOUND_OWNER_TENANTS``:
    ``anonymous``/``none``/``null``/the dashed nil UUID). It is now
    :func:`src.tenant.require_tenant`, which is strictly stronger: the denylist
    only recognised ONE of the nil UUID's four spellings, so
    ``00000000000000000000000000000000`` and the braced/urn forms passed it and
    were written as an unreadable ``owner_tenant_id``. Every slug the denylist
    named is also not a UUID, so nothing it caught is now let through.

    Returns:
        The caller tenant in canonical dashed form, so one tenant cannot split
        into several ``owner_tenant_id`` values by spelling and lose its own rows.

    Raises:
        HTTPException: 400 naming ``X-Owner-Tenant-Id`` when no real caller
            tenant is bound; 403 when the caller is not the orchestrator SA.

    Cell lane (P7): the owner tenant IS the signed ``tenant`` claim and the
    ``X-Owner-Tenant-Id`` header is ignored — a header cannot re-scope a
    signed identity. Only the orchestrator's token is accepted (403 otherwise).
    """
    claims = cell_lane_claims(request)
    if claims is not None:
        if claims.issuer != ORCHESTRATOR_CELL_CALLER:
            logger.warning(
                "Rejecting %s %s: cell caller %s is not %s",
                request.method,
                request.url.path,
                claims.issuer,
                ORCHESTRATOR_CELL_CALLER,
            )
            raise HTTPException(
                status_code=403,
                detail=f"The owner tenant may only be asserted by {_ORCHESTRATOR_IDENTITY}.",
            )
        owner = str(require_tenant(str(claims.tenant_id), source="cell token tenant"))
        raw = request.headers.get(OWNER_TENANT_HEADER)
        if raw is not None and raw.strip().lower() != owner:
            logger.warning(
                "Ignoring %s on the cell lane for %s %s: the signed tenant %s wins",
                OWNER_TENANT_HEADER,
                request.method,
                request.url.path,
                owner,
            )
        return owner
    raw = request.headers.get(OWNER_TENANT_HEADER)
    try:
        owner = str(require_tenant(raw, source=OWNER_TENANT_HEADER))
    except TenantError as exc:
        # WARNING, not INFO: this rejects the whole request, and under the
        # fleet's LOG_LEVEL=WARNING an INFO line would make a misconfigured
        # caller's 400 storm completely invisible.
        logger.warning(
            "Rejecting %s %s: X-Owner-Tenant-Id is absent or unbound (%s)",
            request.method,
            request.url.path,
            exc,
        )
        raise HTTPException(
            status_code=400,
            detail=(
                "The X-Owner-Tenant-Id header is required on /api/upload* and must "
                f"carry the real caller tenant ({exc}). The YouTube channel "
                "tenant is the same value for every caller and is NOT a valid "
                "substitute: a job written with it is unreadable by its own poller."
            ),
        ) from exc
    if not caller_is_orchestrator_sa(request):
        # The owner tenant selects whose YouTube grant is used; only the
        # delegating orchestrator SA may assert it. ``owner`` is a validated
        # UUID, safe to log; the licence token itself is never logged.
        logger.warning(
            "Rejecting %s %s: X-Owner-Tenant-Id=%s asserted by a caller that is not "
            "the orchestrator service account",
            request.method,
            request.url.path,
            owner,
        )
        raise HTTPException(
            status_code=403,
            detail=f"X-Owner-Tenant-Id may only be asserted by {_ORCHESTRATOR_IDENTITY}.",
        )
    return owner


def get_auth_owner_tenant_id(request: Request, _bound: str = Depends(get_tenant_id)) -> str:
    """The tenant whose YouTube grant ``/api/auth/{connect,status,disconnect}`` act on.

    These routes accept ONLY the orchestrator lane: the SPA reaches them through
    w2v-frontend -> w2v-orchestrator (gitops #4565), which calls with its
    service-account licence plus ``X-Owner-Tenant-Id``. Role gating (who in the
    tenant may connect/disconnect) is the orchestrator's job (P5-3,
    ``TenantAdminDep``). The former user-JWT lane — tenant from the Keycloak
    claim, gated on ``tenant_admin``/``super_admin`` realm roles — is deleted:
    w2v is retiring Keycloak-claim authorization, and nothing routes a user JWT
    here any more.

    ``get_tenant_id`` runs first, so an unauthenticated caller still gets 401.
    The SA check runs BEFORE the owner header is parsed, so any non-SA caller
    (Keycloak-JWT user of any role, customer licence) gets 403 whether or not it
    sends the header — never a 400 that hints the header is the missing piece.

    Returns:
        The owner tenant in canonical dashed form.

    Raises:
        HTTPException: 403 when the caller is not the orchestrator SA; 400 when
            the SA sends no real owner tenant.
    """
    if not caller_is_orchestrator_sa(request):
        logger.warning(
            "Rejecting %s %s: /api/auth/* accepts only the orchestrator service account",
            request.method,
            request.url.path,
        )
        raise HTTPException(
            status_code=403,
            detail=(
                f"/api/auth/{{connect,status,disconnect}} accept only {_ORCHESTRATOR_IDENTITY}."
            ),
        )
    return get_owner_tenant_id(request)


#: Opt-in for SERVICE-scope cell tokens on ``/api/admin/*``. ``AuthMiddleware``
#: reads this marker off the matched route to admit an orchestrator service
#: token (any other issuer's service token is refused there, before routing).
#: ``allow_other_lanes`` keeps the licence lane (dual-send) reaching the
#: checks below; it returns ``None`` for those requests.
operator_service_caller = require_cell_service_caller(
    {ORCHESTRATOR_CELL_CALLER}, allow_other_lanes=True
)


def operator_service_claims(request: Request) -> CellClaims | None:
    """The orchestrator's service-scope claims when the SERVICE lane authenticated."""
    claims = getattr(request.state, "cell_claims", None)
    if (
        getattr(request.state, SERVICE_LANE_STATE_ATTR, False) is True
        and isinstance(claims, CellClaims)
        and claims.scope is CellScope.SERVICE
        and claims.issuer == ORCHESTRATOR_CELL_CALLER
    ):
        return claims
    return None


def _refuse_operator(request: Request, reason: str, detail: str) -> HTTPException:
    logger.warning("Rejecting %s %s: %s", request.method, request.url.path, reason)
    return HTTPException(status_code=403, detail=detail)


def _refuse_owner_header(request: Request) -> None:
    if request.headers.get(OWNER_TENANT_HEADER) is not None:
        raise _refuse_operator(
            request,
            "operator route called with X-Owner-Tenant-Id (tenant-scoped call)",
            "/api/admin/* are operator routes and are not owner-tenant scoped: "
            "X-Owner-Tenant-Id must not be sent.",
        )


def require_operator_sa(
    request: Request, service: CellClaims | None = Depends(operator_service_caller)
) -> str:
    """Gate for ``/api/admin/*``: the orchestrator, acting for NO owner tenant.

    The orchestrator enforces ``super_admin`` before proxying; this service only
    proves the caller is the orchestrator. A request carrying
    ``X-Owner-Tenant-Id`` is a tenant-scoped call and is refused (403): the
    operator surface changes the platform channel / other tenants' permissions,
    which no owner-tenant-scoped proxy path may reach — a mis-wired tenant
    proxy must fail, not escalate.

    Cell lane (P7): ONLY a service-scope (tenant-less) token from
    ``w2v-orchestrator`` is accepted. A tenant-scope cell token is refused —
    it speaks for one tenant, which is exactly the mis-wired-proxy shape.

    Licence lane (dual-send fallback): a bound tenant (401 otherwise) and the
    orchestrator SA subject (``AUTH_DELEGATING_SUBJECTS``).

    Returns:
        ``cell:<issuer>`` on the cell lane, else the SA's introspected subject id.
    """
    if service is not None:
        _refuse_owner_header(request)
        return f"cell:{service.issuer}"
    if cell_lane_claims(request) is not None:
        raise _refuse_operator(
            request,
            "tenant-scope cell token on an operator route",
            "/api/admin/* accept on the cell lane only a service-scope token from "
            f"{ORCHESTRATOR_CELL_CALLER}.",
        )
    get_tenant_id(request)
    if not caller_is_orchestrator_sa(request):
        raise _refuse_operator(
            request,
            "/api/admin/* accepts only the orchestrator service account",
            f"/api/admin/* accepts only {_ORCHESTRATOR_IDENTITY}.",
        )
    _refuse_owner_header(request)
    introspection = getattr(request.state, "license", None)
    subject = introspection.get("subject_id") if isinstance(introspection, Mapping) else None
    return str(subject)


async def operator_license_gate(request: Request) -> None:
    """``LICENSE_GATE`` for ``/api/admin/*``, skipped for the orchestrator service lane.

    A service-scope token binds no tenant, so the tenant licence gate would
    derive ``none`` and 402 every operator call once armed. The operator
    surface is not tenant-scoped; the licence lane is gated exactly as before.
    """
    if operator_service_claims(request) is not None:
        return
    await require_license(request)


ACTOR_HEADER = "X-Actor-Id"


def get_actor(request: Request) -> str:
    """Who made a settings change, for ``updated_by``.

    Cell lane (P7): the SIGNED ``actor`` claim (the human the orchestrator acted
    for); ``X-Actor-Id`` is ignored. A null actor means service-originated
    work, recorded as the calling service (``cell:w2v-orchestrator``) — the
    same role ``orchestrator-sa:<subject>`` plays on the licence lane.

    Operator service lane (``/api/admin/*``, service-scope token): the
    ``X-Actor-Id`` header, else ``cell:w2v-orchestrator``.

    Licence lane: the orchestrator may forward the acting user as
    ``X-Actor-Id`` (optional, trusted only because every route using this
    already requires the SA); otherwise the SA subject is recorded.
    """
    claims = cell_lane_claims(request)
    if claims is not None:
        return claims.actor or f"cell:{claims.issuer}"
    actor = (request.headers.get(ACTOR_HEADER) or "").strip()
    service = operator_service_claims(request)
    if service is not None:
        # Operator lane: the token is tenant-less and actor-less by design, so
        # the orchestrator's X-Actor-Id is attribution only (the caller itself
        # is authenticated as the orchestrator service).
        return actor[:255] if actor else f"cell:{service.issuer}"
    if actor:
        return actor[:255]
    introspection = getattr(request.state, "license", None)
    subject = introspection.get("subject_id") if isinstance(introspection, Mapping) else None
    return f"orchestrator-sa:{subject}"


def get_task_manager(request: Request) -> TaskManager:
    """Retrieve the TaskManager instance from application state."""
    return request.app.state.task_manager


def get_oauth_service(request: Request) -> OAuthService:
    """Retrieve the OAuthService instance from application state."""
    return request.app.state.oauth_service


def get_upload_service(request: Request) -> UploadService:
    """Retrieve the UploadService instance from application state."""
    return request.app.state.upload_service


def get_redis_client(request: Request) -> RedisClient | None:
    """Retrieve the RedisClient instance (or None) from application state."""
    return request.app.state.redis_client


def get_config_service(
    oauth_service: OAuthService = Depends(get_oauth_service),
) -> YouTubeConfigService:
    """The per-tenant settings / platform channel service (``OAuthService.config``).

    Derived from :func:`get_oauth_service` so the two can never be different
    instances (and an override of one is honoured by the other).
    """
    return oauth_service.config
