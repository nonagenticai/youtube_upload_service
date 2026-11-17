"""Pydantic request/response models for the YouTube Upload Service API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, SecretStr, field_validator

# ---------------------------------------------------------------------------
# Auth schemas
# ---------------------------------------------------------------------------


class AuthConnectRequest(BaseModel):
    """Request body for POST /api/auth/connect."""

    # No fields needed — tenant_id comes from auth middleware


class AuthConnectResponse(BaseModel):
    """Response for POST /api/auth/connect."""

    authorization_url: str = Field(description="Google consent URL to redirect the user to")
    state: str = Field(description="Opaque state token for CSRF protection")


class AuthStatusResponse(BaseModel):
    """Response for GET /api/auth/status.

    ``connected`` is true ONLY for the tenant's own live grant. A tenant served by
    the platform channel is ``status="platform_default"`` with ``connected=false``
    (``can_publish=true``); a tenant whose grant Google refused is
    ``status="reconnect_required"`` (``can_publish=false``).
    """

    connected: bool = Field(description="True only when the tenant's OWN grant is live")
    status: Literal["connected", "reconnect_required", "platform_default", "not_connected"]
    mode: Literal["tenant", "platform", "none"] = Field(
        description="Which resolution branch an upload takes: the tenant's own grant, the "
        "platform channel, or none (uploads fail with 'connect YouTube')"
    )
    needs_reconnect: bool = Field(
        default=False,
        description="mode=tenant only: the grant is unusable (refused by Google, or issued by "
        "an OAuth client the tenant no longer has); uploads fail until reconnect",
    )
    can_publish: bool = Field(
        description="Whether an upload for this tenant would be attempted (own grant, or "
        "the operator-enabled platform channel)"
    )
    reconnect_reason: str | None = None
    tenant_id: str
    channel_id: str | None = None
    channel_title: str | None = None
    scopes: list[str] = Field(default_factory=list)
    connected_at: str | None = None
    updated_at: str | None = None
    token_expiry: str | None = None


# ---------------------------------------------------------------------------
# Tenant settings + platform channel (migration 014). Secrets are write-only:
# request models carry them as SecretStr; NO response model has a secret field.
# ---------------------------------------------------------------------------


class PlatformChannelSummary(BaseModel):
    channel_id: str | None = None
    channel_title: str | None = None


class TenantSettingsResponse(BaseModel):
    """``GET /api/settings`` — also the body of oauth-client PUT/DELETE."""

    oauth_client_id: str | None = None
    has_oauth_client_secret: bool
    redirect_uri: str = Field(description="Register this as an Authorized redirect URI")
    platform_channel_allowed: bool | None = Field(
        default=None, description="The tenant's own setting; null = follow the platform default"
    )
    effective_platform_channel_allowed: bool
    platform_channel: PlatformChannelSummary | None = None


class OAuthClientRequest(BaseModel):
    """``PUT /api/settings/oauth-client`` — both fields required."""

    client_id: str = Field(min_length=1, max_length=512)
    client_secret: SecretStr = Field(min_length=1, max_length=512)


class PlatformChannelRequest(BaseModel):
    """``PUT /api/admin/platform-channel``. Omitted secrets keep the stored ones."""

    client_id: str = Field(min_length=1, max_length=512)
    client_secret: SecretStr | None = Field(default=None, max_length=512)
    refresh_token: SecretStr | None = Field(default=None, max_length=2048)
    channel_id: str = Field(min_length=1, max_length=255)
    channel_title: str = Field(min_length=1, max_length=255)
    default_for_tenants: bool


class PlatformChannelResponse(BaseModel):
    """Operator view; secrets as ``has_*`` only."""

    client_id: str
    has_client_secret: bool
    has_refresh_token: bool
    channel_id: str | None = None
    channel_title: str | None = None
    default_for_tenants: bool
    updated_by: str | None = None
    updated_at: str | None = None


class TenantPlatformChannelRequest(BaseModel):
    """``PUT /api/admin/tenants/{tenant_id}/platform-channel`` — ``allowed`` must be sent."""

    allowed: bool | None = Field(description="true/false, or null = follow the platform default")


class TenantPlatformChannelResponse(BaseModel):
    tenant_id: str
    platform_channel_allowed: bool | None = None
    effective_platform_channel_allowed: bool


class TenantSettingsListItem(BaseModel):
    tenant_id: str
    platform_channel_allowed: bool | None = None
    has_grant: bool
    has_oauth_client: bool


class AuthDisconnectResponse(BaseModel):
    """Response for DELETE /api/auth/disconnect."""

    disconnected: bool
    tenant_id: str


class AuthCallbackResponse(BaseModel):
    """Response for GET /api/auth/callback (HTML page or JSON)."""

    success: bool
    channel_title: str | None = None
    message: str = ""


# ---------------------------------------------------------------------------
# Upload schemas (Phase 4 — stubs for forward reference)
# ---------------------------------------------------------------------------


class UploadRequest(BaseModel):
    """Request body for POST /api/upload."""

    file_source: str = Field(
        min_length=1,
        max_length=2_048,
        description="URL to the video",
    )
    # ``url`` is the ONLY accepted source type. Path mode made the worker open
    # a caller-supplied filesystem path with nothing but an existence check —
    # and since no PVC is mounted, that path resolves inside the *pod's own*
    # filesystem (service-account token, mounted secrets), whose bytes were
    # then uploaded to YouTube. The one in-fleet producer (w2v_orchestrator)
    # has always sent "url"; path mode has no legitimate caller. A Literal
    # makes it a clean 422 at the boundary rather than a 500 downstream.
    file_source_type: Literal["url"] = Field(
        default="url", description="Source type — 'url' is the only supported value"
    )
    thumbnail_source: str | None = Field(
        default=None,
        max_length=2_048,
        description="Optional custom thumbnail URL. Applied best-effort post-upload.",
    )
    thumbnail_source_type: Literal["url"] = Field(
        default="url",
        description="Thumbnail source type — 'url' is the only supported value",
    )
    caption_source: str | None = Field(
        default=None,
        max_length=2_048,
        description=(
            "Optional soft closed-caption track (SRT/VTT URL). "
            "Attached best-effort post-upload via captions().insert."
        ),
    )
    caption_source_type: Literal["url"] = Field(
        default="url",
        description="Caption source type — 'url' is the only supported value",
    )
    caption_language: str = Field(
        default="en",
        max_length=10,
        description="ISO 639-1 language code for the caption track (only used when caption_source set)",
    )
    title: str = Field(min_length=1, max_length=100, description="YouTube video title")
    description: str | None = Field(
        default=None,
        max_length=5_000,
        description="YouTube video description (YouTube's API cap is 5000 chars)",
    )
    tags: list[str] | None = Field(
        default=None,
        max_length=500,
        description=(
            "Video tags. YouTube's total tag byte cap is ~500 chars; "
            "we bound the list to 500 items as a DoS ceiling and each "
            "tag to 100 chars."
        ),
    )
    category_id: str = Field(
        default="22",
        pattern=r"^\d{1,3}$",
        description="YouTube category ID (22 = People & Blogs)",
    )
    privacy_status: str = Field(
        default="private",
        pattern="^(public|unlisted|private)$",
        description="Video privacy status",
    )

    @field_validator("tags")
    @classmethod
    def _validate_tag_lengths(cls, v: list[str] | None) -> list[str] | None:
        """Reject individual tag strings longer than 100 chars."""
        if v is None:
            return v
        for tag in v:
            if len(tag) > 100:
                raise ValueError(f"tag exceeds 100-character limit: {tag[:50]!r}…")
        return v


class SetPrivacyRequest(BaseModel):
    """Body of ``POST /api/upload/jobs/{job_id}/privacy`` — the governed release target."""

    privacy_status: Literal["unlisted", "public"] = Field(
        description="Visibility to raise the uploaded video to (``private`` is /retract's job).",
    )


class UploadJobResponse(BaseModel):
    """Response for upload job status."""

    job_id: str
    tenant_id: str
    status: str
    file_source: str
    # Deliberately ``str``, NOT ``Literal["url"]``: rows written before the
    # request schema was locked down still carry ``"path"``, and a response
    # model that refused to serialise them would turn a historical row into a
    # 500 on GET. Reads report what is stored; writes are what we constrain.
    file_source_type: str
    title: str
    description: str | None = None
    tags: list[str] | None = None
    category_id: str
    privacy_status: str
    youtube_video_id: str | None = None
    youtube_url: str | None = None
    error_message: str | None = None
    # Requested vs. applied, kept as two separate claims. ``status`` alone
    # cannot carry this: the thumbnail and caption attach steps are deliberately
    # non-fatal (the video IS published, which is the main thing), so a job that
    # lost both still reports ``completed``. Without these fields the
    # orchestrator polls the job, sees green, and reports a paid render as
    # cleanly published when it went public with YouTube's default
    # auto-thumbnail and no subtitles.
    #
    # ``*_requested`` is derived from the stored source column — a boolean, not
    # the URL itself, which is a presigned storage link and not the caller's
    # business. ``*_applied`` is the persisted outcome and is TRI-state:
    # ``None`` means UNMEASURED (nothing requested, or a row written before the
    # outcome was recorded), which is deliberately not the same claim as
    # ``False`` (requested and failed).
    thumbnail_requested: bool = False
    thumbnail_applied: bool | None = None
    caption_requested: bool = False
    caption_applied: bool | None = None
    created_at: str | None = None
    started_at: str | None = None
    completed_at: str | None = None


class UploadJobListResponse(BaseModel):
    """Response for GET /api/upload/jobs."""

    jobs: list[UploadJobResponse]
    total: int
