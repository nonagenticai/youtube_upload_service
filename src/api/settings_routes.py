"""Per-tenant YouTube settings and the operator platform-channel surface.

Tenant lane (``/api/settings``): orchestrator SA + ``X-Owner-Tenant-Id`` — the
same ``get_auth_owner_tenant_id`` lane and check as ``/api/auth/*``. Role gating
(any member reads, tenant admin writes) is the orchestrator's.

Operator lane (``/api/admin``): orchestrator with NO owner tenant — a
service-scope cell token, or the SA licence (``require_operator_sa``); the
orchestrator enforces ``super_admin``.

Secrets are write-only: request bodies carry them as ``SecretStr``; no response
model has a secret field, and nothing here logs one.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from w2v_common.auth import LICENSE_GATE
from w2v_common.tenant import TenantError

from src.api.dependencies import (
    get_actor,
    get_auth_owner_tenant_id,
    get_config_service,
    operator_license_gate,
    require_operator_sa,
)
from src.api.schemas import (
    OAuthClientRequest,
    PlatformChannelRequest,
    PlatformChannelResponse,
    TenantPlatformChannelRequest,
    TenantPlatformChannelResponse,
    TenantSettingsListItem,
    TenantSettingsResponse,
)
from src.services.youtube_config_service import (
    PlatformChannelIncompleteError,
    YouTubeConfigService,
)
from src.tenant import require_tenant

router = APIRouter(prefix="/api/settings", tags=["settings"], dependencies=list(LICENSE_GATE))
admin_router = APIRouter(
    prefix="/api/admin",
    tags=["admin"],
    dependencies=[Depends(operator_license_gate), Depends(require_operator_sa)],
)


# ---------------------------------------------------------------------------
# Tenant lane
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=TenantSettingsResponse,
    operation_id="get_youtube_settings",
    summary="The owner tenant's YouTube settings (no secrets)",
)
async def get_settings(
    tenant_id: str = Depends(get_auth_owner_tenant_id),
    config: YouTubeConfigService = Depends(get_config_service),
) -> TenantSettingsResponse:
    return TenantSettingsResponse.model_validate(await config.settings_view(tenant_id))


@router.put(
    "/oauth-client",
    response_model=TenantSettingsResponse,
    operation_id="put_youtube_oauth_client",
    summary="Store the tenant's own Google OAuth client (secret is write-only)",
)
async def put_oauth_client(
    body: OAuthClientRequest,
    tenant_id: str = Depends(get_auth_owner_tenant_id),
    actor: str = Depends(get_actor),
    config: YouTubeConfigService = Depends(get_config_service),
) -> TenantSettingsResponse:
    """A grant issued by a different client is marked ``needs_reconnect``."""
    view = await config.set_oauth_client(
        tenant_id, body.client_id.strip(), body.client_secret.get_secret_value(), actor
    )
    return TenantSettingsResponse.model_validate(view)


@router.delete(
    "/oauth-client",
    response_model=TenantSettingsResponse,
    operation_id="delete_youtube_oauth_client",
    summary="Remove the tenant's own Google OAuth client",
)
async def delete_oauth_client(
    tenant_id: str = Depends(get_auth_owner_tenant_id),
    actor: str = Depends(get_actor),
    config: YouTubeConfigService = Depends(get_config_service),
) -> TenantSettingsResponse:
    """A grant the removed client issued is marked ``needs_reconnect``."""
    return TenantSettingsResponse.model_validate(await config.clear_oauth_client(tenant_id, actor))


# ---------------------------------------------------------------------------
# Operator lane
# ---------------------------------------------------------------------------


@admin_router.get(
    "/platform-channel",
    response_model=PlatformChannelResponse | None,
    operation_id="get_youtube_platform_channel",
    summary="The platform YouTube channel (null when unconfigured; no secrets)",
)
async def get_platform_channel(
    config: YouTubeConfigService = Depends(get_config_service),
) -> PlatformChannelResponse | None:
    view = await config.platform_channel_view()
    return PlatformChannelResponse.model_validate(view) if view is not None else None


@admin_router.put(
    "/platform-channel",
    response_model=PlatformChannelResponse,
    operation_id="put_youtube_platform_channel",
    summary="Create/update the platform YouTube channel (omitted secrets are kept)",
)
async def put_platform_channel(
    body: PlatformChannelRequest,
    actor: str = Depends(get_actor),
    config: YouTubeConfigService = Depends(get_config_service),
) -> PlatformChannelResponse:
    try:
        view = await config.put_platform_channel(
            client_id=body.client_id.strip(),
            client_secret=body.client_secret.get_secret_value() if body.client_secret else None,
            refresh_token=body.refresh_token.get_secret_value() if body.refresh_token else None,
            channel_id=body.channel_id,
            channel_title=body.channel_title,
            default_for_tenants=body.default_for_tenants,
            updated_by=actor,
        )
    except PlatformChannelIncompleteError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if view is None:  # pragma: no cover — the upsert just wrote the row
        raise HTTPException(status_code=500, detail="platform channel write did not persist")
    return PlatformChannelResponse.model_validate(view)


@admin_router.put(
    "/tenants/{tenant_id}/platform-channel",
    response_model=TenantPlatformChannelResponse,
    operation_id="put_tenant_youtube_platform_channel",
    summary="Allow/deny a tenant the platform channel (null = follow the default)",
)
async def put_tenant_platform_channel(
    tenant_id: str,
    body: TenantPlatformChannelRequest,
    actor: str = Depends(get_actor),
    config: YouTubeConfigService = Depends(get_config_service),
) -> TenantPlatformChannelResponse:
    try:
        tenant = str(require_tenant(tenant_id, source="tenant_id path parameter"))
    except TenantError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    view = await config.set_tenant_platform_allowed(tenant, body.allowed, actor)
    return TenantPlatformChannelResponse.model_validate(view)


@admin_router.get(
    "/tenant-settings",
    response_model=list[TenantSettingsListItem],
    operation_id="list_youtube_tenant_settings",
    summary="Every tenant with YouTube settings or a grant (no secrets)",
)
async def list_tenant_settings(
    config: YouTubeConfigService = Depends(get_config_service),
) -> list[TenantSettingsListItem]:
    return [TenantSettingsListItem.model_validate(r) for r in await config.list_tenant_settings()]
