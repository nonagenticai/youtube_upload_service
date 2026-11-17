"""OAuth2 authorization routes for YouTube API.

Endpoints:
- POST /api/auth/connect   — start OAuth2 flow (returns Google consent URL)
- GET  /api/auth/callback   — OAuth2 callback (PUBLIC — Google redirects here)
- GET  /api/auth/status     — report the connection truthfully
- DELETE /api/auth/disconnect — remove stored tokens

connect / status / disconnect accept ONLY the orchestrator lane
(``get_auth_owner_tenant_id``): SA licence + ``X-Owner-Tenant-Id`` → the OWNER
tenant, 403 for anyone else. Role gating is the orchestrator's job (P5-3).
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from oauthlib.oauth2.rfc6749.errors import OAuth2Error
from w2v_common.auth import LICENSE_GATE
from w2v_common.cache.redis_client import RedisClient
from w2v_common.tenant import TenantError

from src.api.dependencies import (
    get_auth_owner_tenant_id,
    get_config_service,
    get_oauth_service,
    get_redis_client,
)
from src.api.schemas import AuthConnectResponse, AuthDisconnectResponse, AuthStatusResponse
from src.services.oauth_service import STATUS_CONNECTED, STATUS_PLATFORM_DEFAULT, OAuthService
from src.services.youtube_config_service import YouTubeConfigService
from src.tenant import require_tenant

logger = logging.getLogger(__name__)

# Two routers on the SAME prefix, split by who is allowed to call them.
#
# ``router`` — the tenant-facing surface (connect / status / disconnect). Gated
# router-wide so a route added later is gated by default. This service is in
# ``w2v_common.auth.outbound.METERED_DESTINATION_SERVICES`` but had no gate at
# all: ``AuthMiddleware`` answers "who are you", never "may you", so an expired
# or revoked tenant could still (re)connect a channel and publish.
#
# ``public_router`` — ``GET /callback`` ONLY. Google redirects a browser here
# with no licence token and no tenant identity; the tenant is recovered from the
# one-time OAuth ``state`` in Redis. Gating it would 402 the third party, and the
# gate has no tenant to evaluate anyway — the consent flow would break for every
# tenant, entitled or not. It is already declared public to ``AuthMiddleware``
# via ``create_app(auth_public_endpoints=...)``; that set is per-middleware-
# instance and does NOT feed ``LICENSE_EXEMPT_PATHS``, so the exemption has to be
# structural, which is what this second router makes it.
#
# Both ship DORMANT: no-op until ``W2V_LICENSE_GATE_ENABLED=true`` in gitops.
router = APIRouter(prefix="/api/auth", tags=["auth"], dependencies=list(LICENSE_GATE))

public_router = APIRouter(prefix="/api/auth", tags=["auth"])

STATE_TTL_SECONDS = 600  # 10 minutes


def _encode_state(tenant_id: str, oauth_client_id: str | None) -> str:
    """The Redis value bound to an OAuth ``state``: owner tenant + the client that
    started the flow (``None`` = platform client). Never a secret."""
    return json.dumps({"tenant_id": tenant_id, "oauth_client_id": oauth_client_id})


def _decode_state(raw: object) -> tuple[object, str | None]:
    """``(raw tenant, client id)``; anything malformed yields ``(None, None)`` → 400."""
    if not isinstance(raw, str):
        return None, None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None, None
    if not isinstance(payload, dict):
        return None, None
    client_id = payload.get("oauth_client_id")
    return payload.get("tenant_id"), client_id if isinstance(client_id, str) else None


# ---------------------------------------------------------------------------
# POST /api/auth/connect
# ---------------------------------------------------------------------------


@router.post(
    "/connect",
    response_model=AuthConnectResponse,
    operation_id="connect_youtube_auth",
    summary="Start the OAuth2 flow — returns a Google consent URL",
)
async def auth_connect(
    tenant_id: str = Depends(get_auth_owner_tenant_id),
    oauth_service: OAuthService = Depends(get_oauth_service),
    config: YouTubeConfigService = Depends(get_config_service),
    redis_client: RedisClient | None = Depends(get_redis_client),
) -> AuthConnectResponse:
    """Start the OAuth2 flow. Returns a Google consent URL for the tenant.

    The one-time ``state`` is bound to ``tenant_id`` — the OWNER tenant — so the
    public callback stores the grant under the tenant whose uploads will use it —
    and to the OAuth client that starts the flow (the tenant's own client when
    configured, else the platform client), so the callback exchanges the code
    with that same client and records it on the grant.
    """
    if redis_client is None:
        raise HTTPException(status_code=503, detail="Redis unavailable — required for OAuth state")

    state = secrets.token_urlsafe(32)
    state_key = f"youtube:oauth_state:{state}"
    client = await config.connect_client(tenant_id)
    await redis_client.set(
        state_key, _encode_state(tenant_id, client.recorded_id), ttl=STATE_TTL_SECONDS
    )

    authorization_url = oauth_service.build_authorization_url(state, client)
    logger.info(
        "OAuth connect initiated for tenant %s (client: %s)",
        tenant_id,
        client.recorded_id or "platform",
    )

    return AuthConnectResponse(authorization_url=authorization_url, state=state)


# ---------------------------------------------------------------------------
# GET /api/auth/callback  (PUBLIC — no license token required)
# ---------------------------------------------------------------------------

_CALLBACK_HTML = """<!DOCTYPE html>
<html><head><title>YouTube Connected</title></head>
<body style="font-family:sans-serif;text-align:center;padding:60px">
<h1>{title}</h1><p>{message}</p>
<p style="color:#666">You can close this window.</p>
</body></html>"""


@public_router.get(
    "/callback",
    response_class=HTMLResponse,
    operation_id="handle_youtube_auth_callback",
    summary="OAuth2 redirect endpoint — exchanges code for tokens (PUBLIC)",
)
async def auth_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    oauth_service: OAuthService = Depends(get_oauth_service),
    config: YouTubeConfigService = Depends(get_config_service),
    redis_client: RedisClient | None = Depends(get_redis_client),
) -> HTMLResponse:
    """Handle Google's OAuth2 redirect. Exchanges code for tokens and stores them."""
    if error:
        logger.warning("OAuth callback received error: %s", error)
        return HTMLResponse(
            _CALLBACK_HTML.format(
                title="Authorization Failed", message=f"Google returned: {html.escape(error)}"
            ),
            status_code=400,
        )

    if not code or not state:
        logger.warning(
            "OAuth callback missing required parameters (code=%s, state=%s) -- 400",
            bool(code),
            bool(state),
        )
        return HTMLResponse(
            _CALLBACK_HTML.format(
                title="Invalid Request", message="Missing code or state parameter"
            ),
            status_code=400,
        )

    if redis_client is None:
        logger.warning(
            "OAuth callback cannot validate state: Redis unavailable -- 503, consent flow lost"
        )
        return HTMLResponse(
            _CALLBACK_HTML.format(
                title="Service Unavailable", message="Redis unavailable — cannot validate state"
            ),
            status_code=503,
        )

    # Validate state and retrieve tenant_id
    state_key = f"youtube:oauth_state:{state}"
    raw_tenant_id, state_client_id = _decode_state(await redis_client.get(state_key))
    # This route is PUBLIC (Google redirects here with no license token), so the
    # state payload is the ONLY source of the tenant — validate it as strictly as
    # a header. A state token minted before this change can still be in Redis for
    # its 600s TTL carrying the old ``"anonymous"`` fallback; that value would
    # otherwise reach ``store_tokens`` and blow up at ``bind_session_tenant`` as an
    # opaque 500 instead of the 400 the operator can act on.
    try:
        tenant_id = str(require_tenant(raw_tenant_id, source="OAuth state token"))
    except TenantError as exc:
        logger.warning("OAuth callback with invalid/expired state %s: %s", state[:16], exc)
        return HTMLResponse(
            _CALLBACK_HTML.format(
                title="Session Expired",
                message="State token is invalid or expired. Please try again.",
            ),
            status_code=400,
        )

    # Delete state token (one-time use)
    await redis_client.delete(state_key)

    # The flow must finish with the client that started it.
    try:
        client = await config.client_for_recorded_id(tenant_id, state_client_id)
    except LookupError as exc:
        logger.warning("OAuth callback for tenant %s: %s", tenant_id, exc)
        return HTMLResponse(
            _CALLBACK_HTML.format(title="Authorization Failed", message=html.escape(str(exc))),
            status_code=409,
        )

    # Exchange code for credentials
    try:
        credentials = await asyncio.to_thread(oauth_service.exchange_code, code, state, client)
    except (OSError, ValueError, RuntimeError, TimeoutError, OAuth2Error) as exc:
        logger.exception("Failed to exchange OAuth code for tenant %s", tenant_id)
        return HTMLResponse(
            _CALLBACK_HTML.format(
                title="Authorization Failed",
                message=f"Token exchange failed: {html.escape(str(exc))}",
            ),
            status_code=500,
        )

    # Fetch channel info — strictly best-effort. Any failure (googleapiclient
    # HttpError on a 403 quota / transient 5xx, or an unexpected response shape
    # raising KeyError/IndexError/TypeError) must degrade to channel_id=None and
    # MUST NEVER block token persistence below. googleapiclient.errors.HttpError
    # subclasses only Exception, so we catch Exception broadly here on purpose.
    channel_id = None
    channel_title = None
    try:
        from googleapiclient.discovery import build as build_youtube

        youtube = await asyncio.to_thread(build_youtube, "youtube", "v3", credentials=credentials)
        resp = await asyncio.to_thread(
            lambda: youtube.channels().list(part="snippet", mine=True).execute()
        )
        items = resp.get("items", [])
        if items:
            channel_id = items[0]["id"]
            channel_title = items[0]["snippet"]["title"]
    except Exception as exc:  # noqa: BLE001 — best-effort lookup; must never 500 the callback
        logger.warning("Could not fetch channel info for tenant %s: %s", tenant_id, exc)

    # Store tokens — the just-exchanged one-time code + Redis state are already
    # consumed, so this persistence must happen regardless of the best-effort
    # channel lookup above; otherwise a transient Google error forces the tenant
    # to restart the entire consent flow.
    await oauth_service.store_tokens(
        tenant_id, credentials, channel_id, channel_title, oauth_client_id=client.recorded_id
    )

    safe_title = html.escape(channel_title) if channel_title else None
    title_display = f"Connected to {safe_title}" if safe_title else "YouTube Connected"
    logger.info(
        "OAuth flow completed for tenant %s (channel: %s)", tenant_id, channel_title or "unknown"
    )
    return HTMLResponse(
        _CALLBACK_HTML.format(title=title_display, message="Your YouTube account has been linked."),
        status_code=200,
    )


# ---------------------------------------------------------------------------
# GET /api/auth/status
# ---------------------------------------------------------------------------


@router.get(
    "/status",
    response_model=AuthStatusResponse,
    operation_id="get_youtube_auth_status",
    summary="Check whether the tenant has a connected YouTube account",
)
async def auth_status(
    tenant_id: str = Depends(get_auth_owner_tenant_id),
    oauth_service: OAuthService = Depends(get_oauth_service),
) -> AuthStatusResponse:
    """Report the tenant's YouTube connection truthfully.

    ``connected`` only for the tenant's own live grant; ``platform_default`` and
    ``reconnect_required`` are distinct states, never folded into ``connected``.
    Readable by any member (it only reports), unlike connect/disconnect.
    """
    info = await oauth_service.get_token_info(tenant_id)
    status = info["status"]
    return AuthStatusResponse(
        connected=status == STATUS_CONNECTED,
        status=status,
        mode=info["mode"],
        needs_reconnect=info["needs_reconnect"],
        can_publish=status in (STATUS_CONNECTED, STATUS_PLATFORM_DEFAULT),
        reconnect_reason=info.get("reconnect_reason"),
        tenant_id=tenant_id,
        channel_id=info.get("channel_id"),
        channel_title=info.get("channel_title"),
        scopes=info.get("scopes", []),
        connected_at=info.get("connected_at"),
        updated_at=info.get("updated_at"),
        token_expiry=info.get("token_expiry"),
    )


# ---------------------------------------------------------------------------
# DELETE /api/auth/disconnect
# ---------------------------------------------------------------------------


@router.delete(
    "/disconnect",
    response_model=AuthDisconnectResponse,
    operation_id="disconnect_youtube_auth",
    summary="Remove the tenant's stored YouTube OAuth2 tokens",
)
async def auth_disconnect(
    tenant_id: str = Depends(get_auth_owner_tenant_id),
    oauth_service: OAuthService = Depends(get_oauth_service),
) -> AuthDisconnectResponse:
    """Remove the tenant's stored YouTube OAuth2 tokens."""
    deleted = await oauth_service.delete_tokens(tenant_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="No YouTube connection found for this tenant")

    logger.info("OAuth disconnected for tenant %s", tenant_id)
    return AuthDisconnectResponse(disconnected=True, tenant_id=tenant_id)
