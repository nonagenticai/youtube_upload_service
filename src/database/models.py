"""SQLAlchemy ORM models for youtube_upload_service.

Tables:
- oauth_tokens: Per-tenant YouTube OAuth2 credentials (encrypted at rest)
- upload_jobs: Async video upload job tracking
- tenant_youtube_settings: per-tenant OAuth client + platform-channel choice (RLS)
- youtube_platform_channel: the singleton platform channel (not tenant data, no RLS)
"""

import datetime as dt

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID  # noqa: N811
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from w2v_common.database.rls import register_rls_ddl

from src.database.rls_resolvers import register_reaper_fns

# ---------------------------------------------------------------------------
# Tenant key column type
# ---------------------------------------------------------------------------
# The three tenant-key columns are real Postgres ``uuid`` (migration
# ``010_tenant_id_uuid``), not ``varchar(255)``.
#
# ``as_uuid=False`` is LOAD-BEARING, not style. With ``as_uuid=True`` SQLAlchemy
# returns ``uuid.UUID`` objects, and ``uuid.UUID(t) == t`` is ``False`` for every
# string ``t``. No type checker flags a ``UUID == str`` comparison, so the service
# would break SILENTLY in production at sites like:
#   * ``UploadJobResponse(tenant_id=job.tenant_id, ...)`` — pydantic ``tenant_id:
#     str`` rejects a ``UUID`` (src/api/upload_routes.py, src/api/schemas.py);
#   * ``{"tenant_id": token_row.tenant_id}`` in ``OAuthService.get_token_info``;
#   * ``tenant_id_var.set(tenant_id)`` (``ContextVar[str]``) in the worker path;
#   * every ``owner == owner_tenant_id`` / ``owner is None`` ownership check.
# Keeping ``as_uuid=False`` makes the DATABASE column a uuid (the whole point)
# while the Python value stays ``str``, so no comparison site changes.
# Pinned by ``tests/unit/test_tenant_column_type.py`` — do not "tidy" it to True.
TenantId = PgUUID(as_uuid=False)


class Base(DeclarativeBase):
    """Base class for all models."""


# ---------------------------------------------------------------------------
# OAuth2 token storage
# ---------------------------------------------------------------------------


class OAuthToken(Base):
    """Per-tenant YouTube OAuth2 credentials.

    Tokens (access_token, refresh_token) are Fernet-encrypted at rest.
    One YouTube channel connection per tenant (unique constraint on tenant_id).
    """

    __tablename__ = "oauth_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # uuid column, str in Python — see the ``TenantId`` note above.
    tenant_id: Mapped[str] = mapped_column(TenantId, nullable=False, index=True)

    # Encrypted OAuth2 tokens
    access_token: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token: Mapped[str] = mapped_column(Text, nullable=False)
    token_uri: Mapped[str] = mapped_column(
        String(512), nullable=False, default="https://oauth2.googleapis.com/token"
    )
    scopes: Mapped[str] = mapped_column(Text, nullable=False)
    expiry: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    # Cached channel info
    channel_id: Mapped[str | None] = mapped_column(String(255))
    channel_title: Mapped[str | None] = mapped_column(String(255))

    # Set when Google refused to refresh this grant (revoked, expired, client
    # rotated). The row is KEPT — deleting it used to silently re-route the
    # tenant's uploads to the platform channel. Cleared by the next successful
    # consent (``store_tokens``). Migration ``012_oauth_needs_reconnect``.
    needs_reconnect: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    reconnect_reason: Mapped[str | None] = mapped_column(Text)

    # The OAuth client that ISSUED this grant (migration 014). NULL = the
    # deployment's platform OAuth client (``YOUTUBE_CLIENT_ID``), which is also
    # what every pre-014 grant used. A refresh must use the issuing client;
    # a grant whose client the tenant no longer has is ``needs_reconnect``.
    oauth_client_id: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (UniqueConstraint("tenant_id", name="uq_oauth_tokens_tenant_id"),)


# ---------------------------------------------------------------------------
# Per-tenant YouTube settings + the platform channel (migration 014)
# ---------------------------------------------------------------------------


class TenantYouTubeSettings(Base):
    """A tenant's own Google OAuth client and its platform-channel permission.

    RLS-isolated by ``tenant_id`` like ``oauth_tokens``. The client secret is
    Fernet ciphertext and is write-only at the API: never returned, never logged.
    """

    __tablename__ = "tenant_youtube_settings"

    tenant_id: Mapped[str] = mapped_column(TenantId, primary_key=True)
    oauth_client_id: Mapped[str | None] = mapped_column(Text)
    oauth_client_secret_enc: Mapped[str | None] = mapped_column(Text)
    # NULL = follow ``youtube_platform_channel.default_for_tenants``.
    platform_channel_allowed: Mapped[bool | None] = mapped_column(Boolean)
    updated_by: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "(oauth_client_id IS NULL) = (oauth_client_secret_enc IS NULL)",
            name="ck_tenant_youtube_settings_client_pair",
        ),
    )


class YouTubePlatformChannel(Base):
    """The platform's own YouTube channel — a singleton row (``id = 1``).

    Not tenant data (no ``tenant_id``, no RLS). Seeded once from the
    ``YOUTUBE_FALLBACK_*`` env at boot; after that the row is the only source.
    """

    __tablename__ = "youtube_platform_channel"

    id: Mapped[int] = mapped_column(
        SmallInteger, primary_key=True, default=1, server_default=text("1")
    )
    client_id: Mapped[str] = mapped_column(Text, nullable=False)
    client_secret_enc: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token_enc: Mapped[str] = mapped_column(Text, nullable=False)
    channel_id: Mapped[str | None] = mapped_column(Text)
    channel_title: Mapped[str | None] = mapped_column(Text)
    default_for_tenants: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    updated_by: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (CheckConstraint("id = 1", name="ck_youtube_platform_channel_singleton"),)


# ---------------------------------------------------------------------------
# Upload job tracking
# ---------------------------------------------------------------------------


class UploadJob(Base):
    """Async video upload job.

    Status flow: pending → processing → completed | failed
    """

    __tablename__ = "upload_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False, index=True)
    # ``tenant_id`` is the YouTube *channel* owner — the orchestrator's
    # service-account tenant, under which the OAuth credentials are stored. It
    # is used by the background worker to load credentials, NOT for caller
    # scoping. The orchestrator authenticates with that SA token for every real
    # tenant, so it is the SAME value for all callers and must never be used to
    # scope list/get (doing so leaked every tenant's jobs to every other).
    # uuid column, str in Python — see the ``TenantId`` note above.
    tenant_id: Mapped[str] = mapped_column(TenantId, nullable=False, index=True)
    # ``owner_tenant_id`` is the REAL caller tenant the orchestrator forwards as
    # ``X-Owner-Tenant-Id``. list/get are scoped to it so a tenant only ever
    # sees its own upload jobs. Nullable for rows created before this column:
    # NULL means "no distinct caller owner" and is a legitimate value, NOT
    # missing data — migration 010 leaves those rows NULL and does not backfill.
    # uuid column, str|None in Python — see the ``TenantId`` note above.
    owner_tenant_id: Mapped[str | None] = mapped_column(TenantId, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="pending")

    # File source
    file_source: Mapped[str] = mapped_column(Text, nullable=False)
    # server_default was ``"path"`` until migration 007. An INSERT that omitted
    # the column therefore landed in path mode — a container-filesystem read —
    # by default. ``"url"`` is the only mode the worker will serve.
    file_source_type: Mapped[str] = mapped_column(String(10), nullable=False, server_default="url")

    # Optional custom thumbnail source (path or URL). Applied best-effort after
    # a successful insert; a custom thumbnail needs a verified YouTube channel,
    # so a failure here never fails the upload.
    thumbnail_source: Mapped[str | None] = mapped_column(Text)
    thumbnail_source_type: Mapped[str | None] = mapped_column(String(10))

    # Optional soft closed-caption track (SRT/VTT, path or URL). Attached
    # best-effort after a successful insert via captions().insert; needs a
    # youtube.force-ssl token, so a failure here never fails the upload.
    caption_source: Mapped[str | None] = mapped_column(Text)
    caption_source_type: Mapped[str | None] = mapped_column(String(10))
    caption_language: Mapped[str | None] = mapped_column(String(10))

    # OUTCOME of the two best-effort post-upload steps, recorded distinctly from
    # the ``*_source`` columns above, which only say what was REQUESTED. Both
    # attach calls swallow their exception on purpose (the video is published;
    # a 403 from an unverified channel or a token minted without
    # ``youtube.force-ssl`` must not fail the job) — but a swallowed failure
    # that is never persisted makes "published with everything" and "published
    # with YouTube's default auto-thumbnail and no subtitles" the SAME record,
    # visible only in a pod log line the orchestrator never reads.
    #
    # Tri-state, and the third state is load-bearing:
    #   NULL  -> nothing was requested, OR the row predates migration 011.
    #            UNMEASURED, not "did not happen".
    #   True  -> requested and the YouTube call returned.
    #   False -> requested and it FAILED. The video is public without it.
    # Read together with ``thumbnail_source``/``caption_source``: source set +
    # applied NULL on a terminal row is the unmeasured case, not a success.
    thumbnail_applied: Mapped[bool | None] = mapped_column(Boolean)
    caption_applied: Mapped[bool | None] = mapped_column(Boolean)

    # YouTube video metadata
    title: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list[str] | None] = mapped_column(JSONB)
    category_id: Mapped[str] = mapped_column(String(10), nullable=False, server_default="22")
    privacy_status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="private"
    )

    # Result (populated on success)
    youtube_video_id: Mapped[str | None] = mapped_column(String(20))
    youtube_url: Mapped[str | None] = mapped_column(String(255))

    # WHICH channel the video was published to (migration 013), decided by the
    # credential choice at upload time: 'platform' (no owner grant, operator
    # flag on) or 'tenant' (the owner's own grant). Retract / re-privacy act on
    # THIS channel — re-deriving it from the owner's CURRENT grant sent a
    # platform-published video's takedown to the tenant's channel (404) once the
    # tenant connected. NULL = legacy/unknown (falls back to the current grant).
    published_via: Mapped[str | None] = mapped_column(Text)

    # Error (populated on failure)
    error_message: Mapped[str | None] = mapped_column(Text)

    # Scale-in reaper lease. While an upload is in-flight the worker sets
    # ``lease_owner`` (the pod id) and a ``lease_expires_at`` that a heartbeat
    # renews. If the pod is terminated mid-upload the lease is never renewed and
    # the reaper (``reap_stranded_upload_jobs``) fails the stranded job so it is
    # retryable. Both NULL for a job before it reaches ``processing``.
    lease_owner: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    # Timestamps
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_upload_jobs_tenant_status", "tenant_id", "status"),
        Index("ix_upload_jobs_status_created", "status", "created_at"),
        # Keeps the reaper's status/lease scan cheap as terminal jobs accumulate.
        Index("ix_upload_jobs_status_lease", "status", "lease_expires_at"),
        CheckConstraint(
            "published_via IN ('platform', 'tenant')",
            name="ck_upload_jobs_published_via",
        ),
        # Publish-once backstop: one COMPLETED upload per (caller tenant, render).
        # Nothing above stops the same render being published to YouTube twice --
        # ``job_id`` is unique but it is minted fresh per request, so a second
        # POST for the same ``file_source`` was a second, fully-paid publish.
        # Partial (only completed rows) so retries after a genuine failure are
        # still allowed: a failed/pending row never occupies the slot.
        # ``owner_tenant_id`` leads the key because it is the real caller scope;
        # ``tenant_id`` is the shared SA channel tenant and identical for
        # everyone, so keying on it would collide across tenants.
        # NULL ``owner_tenant_id`` rows (pre-005 legacy) are exempt: a NULL never
        # equals a NULL in a unique index. Acceptable -- ``create_job`` refuses
        # to write a NULL owner, so no new row can land in that gap.
        Index(
            "uq_upload_jobs_completed_owner_source",
            "owner_tenant_id",
            "file_source",
            unique=True,
            postgresql_where=text("status = 'completed'"),
        ),
    )


# Phase D RLS: enable + tenant_isolation policy on every tenant_id table when a
# fresh schema is built via create_all (tests/dev). Prod gets the SAME DDL from the
# alembic migration (single source: w2v_common.database.rls). Inert until the
# runtime connects as the restricted w2v_youtube_app role + TENANT_RLS_MODE=enforce.
register_rls_ddl(Base.metadata)

# Scale-in reaper resolvers (SECURITY DEFINER, cross-tenant under RLS enforce).
# Prod gets the SAME functions from migration 006; single source in rls_resolvers.
register_reaper_fns(Base.metadata)
