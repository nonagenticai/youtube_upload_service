"""Configuration management for youtube_upload_service using Pydantic Settings."""

import tempfile
from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from w2v_common.config import APISettings as BaseAPISettings
from w2v_common.config import AuthSettings as AuthSettings
from w2v_common.config import CacheSettings as CacheSettings
from w2v_common.config import DatabaseSettings as BaseDatabaseSettings
from w2v_common.config import LoggingSettings as LoggingSettings
from w2v_common.config import RedisSettings as BaseRedisSettings

# ---------------------------------------------------------------------------
# Shared infrastructure settings (imported from w2v_common, with overrides)
# ---------------------------------------------------------------------------


class DatabaseSettings(BaseDatabaseSettings):
    """Database settings — DATABASE_URL required env (no localhost fallback)."""


class RedisSettings(BaseRedisSettings):
    """Redis settings — REDIS_URL required env (no localhost fallback)."""


class APISettings(BaseAPISettings):
    """API settings with youtube_upload_service default port."""

    port: int = Field(default=6315, description="API server port")


# ---------------------------------------------------------------------------
# YouTube-specific settings
# ---------------------------------------------------------------------------


class YouTubeSettings(BaseSettings):
    """YouTube OAuth2 configuration."""

    client_id: str = Field(default="", description="YouTube OAuth2 client ID")
    client_secret: str = Field(default="", description="YouTube OAuth2 client secret")
    redirect_uri: str = Field(
        default="",
        description="OAuth2 callback URL (must be set via YOUTUBE_REDIRECT_URI; "
        "registered Authorized redirect URI on the OAuth client)",
    )
    scopes: list[str] = Field(
        default=[
            "https://www.googleapis.com/auth/youtube.upload",
            "https://www.googleapis.com/auth/youtube.readonly",
            # Required for captions().insert (soft-CC track upload). Existing
            # tokens minted before this scope was added will lack it — caption
            # attach is best-effort and swallows the resulting 403, so uploads
            # are unaffected until the token is re-minted with this scope.
            "https://www.googleapis.com/auth/youtube.force-ssl",
        ],
        description="YouTube API scopes",
    )

    # ── Platform channel SEED (read once, at boot, by the seeder only) ─────
    # The platform channel lives in the ``youtube_platform_channel`` table
    # (migration 014). These env values are read ONLY by
    # ``src.services.youtube_config_service.seed_platform_channel_from_env``,
    # and only while that table has no row: the first boot copies them in, and
    # from then on the row is the single source — nothing at upload/status time
    # reads them. Removing them from gitops after the seed is verified is safe.
    platform_channel_as_default: bool = Field(
        default=False,
        description=(
            "Seed only: initial youtube_platform_channel.default_for_tenants "
            "(YOUTUBE_PLATFORM_CHANNEL_AS_DEFAULT)."
        ),
    )
    fallback_client_id: str = Field(default="", description="Seed only: platform-channel client id")
    fallback_client_secret: str = Field(
        default="", description="Seed only: platform-channel client secret"
    )
    fallback_refresh_token: str = Field(
        default="", description="Seed only: platform-channel refresh token"
    )
    fallback_channel_id: str = Field(default="", description="Seed only: platform channel id")
    fallback_channel_title: str = Field(default="", description="Seed only: platform channel title")

    @property
    def fallback_seed_configured(self) -> bool:
        """True when the env carries a complete platform-channel credential to seed."""
        return bool(
            self.fallback_refresh_token and self.fallback_client_id and self.fallback_client_secret
        )

    model_config = SettingsConfigDict(
        env_prefix="YOUTUBE_", env_file=".env", case_sensitive=False, extra="ignore"
    )


class UploadSettings(BaseSettings):
    """Upload job configuration."""

    max_concurrent: int = Field(default=4, description="Max concurrent upload workers")
    temp_dir: str = Field(
        default=str(Path(tempfile.gettempdir()) / "youtube_uploads"),
        description="Temporary directory for downloads",
    )
    max_file_size_mb: int = Field(default=5120, description="Max file size in MB (5GB)")
    # Auxiliary-asset download caps. The video cap (5 GB) must NOT apply to the
    # thumbnail/caption downloads: those are small by construction (YouTube
    # rejects a thumbnail over 2 MB outright, and a caption track is text), so
    # reusing the video cap let a tenant stream gigabytes into either one.
    max_thumbnail_size_mb: int = Field(
        default=2, description="Max custom-thumbnail download size in MB (YouTube's own limit)"
    )
    max_caption_size_mb: int = Field(
        default=1, description="Max soft-caption track download size in MB"
    )
    chunk_size_bytes: int = Field(
        default=10 * 1024 * 1024, description="Resumable upload chunk size (10MB)"
    )
    max_retries: int = Field(default=5, description="Max retry attempts for upload")
    retry_base_delay: float = Field(default=1.0, description="Base delay for exponential backoff")

    # ── Scale-in reaper ──────────────────────────────────────────────────────
    # While an upload is in-flight the worker holds a lease renewed by a
    # heartbeat every ~lease_seconds/3. If the pod dies the lease expires and the
    # reaper fails the stranded job. lease_seconds must comfortably exceed one
    # heartbeat interval; a long (multi-GB) upload stays alive because the
    # heartbeat keeps renewing regardless of upload duration.
    lease_seconds: int = Field(
        default=120, description="Upload-job lease TTL (seconds); heartbeat renews at ~1/3 of it"
    )
    reaper_interval_seconds: int = Field(
        default=60, description="How often the reaper sweeps for stranded (expired-lease) jobs"
    )
    reaper_grace_seconds: int = Field(
        default=900,
        description=(
            "Grace before a never-leased pending/processing job (a pod that died "
            "before the task started, or a pre-reaper legacy row) is reaped. Kept "
            "generous so a legitimately queued pending job is never wrongly failed."
        ),
    )
    trusted_download_hosts: str = Field(
        default="",
        description=(
            "Comma-separated hostnames exempt from the SSRF private-IP + port checks "
            "when downloading a url file_source. Set to the in-cluster artifact host "
            "(e.g. 'storage.test') so the orchestrator-built storage_service "
            "download URL is fetchable. Empty (default) keeps strict fail-closed SSRF."
        ),
    )

    @property
    def trusted_download_host_set(self) -> frozenset[str]:
        """Parsed, stripped set of trusted download hosts."""
        return frozenset(h.strip() for h in self.trusted_download_hosts.split(",") if h.strip())

    model_config = SettingsConfigDict(
        env_prefix="UPLOAD_", env_file=".env", case_sensitive=False, extra="ignore"
    )


class EncryptionSettings(BaseSettings):
    """Fernet encryption configuration for token storage."""

    key: str = Field(default="", description="Fernet encryption key for token encryption at rest")

    model_config = SettingsConfigDict(
        env_prefix="ENCRYPTION_", env_file=".env", case_sensitive=False, extra="ignore"
    )


# ---------------------------------------------------------------------------
# Root Settings
# ---------------------------------------------------------------------------


class Settings(BaseSettings):
    """Root settings aggregating all sub-configurations for youtube_upload_service."""

    app_name: str = Field(default="YouTube Upload Service", description="Application name")
    app_version: str = Field(default="0.1.0", description="Application version")
    environment: str = Field(
        default="development", description="Environment (development, staging, production)"
    )

    # Shared infrastructure
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    api: APISettings = Field(default_factory=APISettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    cache: CacheSettings = Field(default_factory=CacheSettings)

    # YouTube-specific
    youtube: YouTubeSettings = Field(default_factory=YouTubeSettings)
    upload: UploadSettings = Field(default_factory=UploadSettings)
    encryption: EncryptionSettings = Field(default_factory=EncryptionSettings)

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")

    @model_validator(mode="after")
    def validate_settings(self) -> Settings:
        """Validate cross-field constraints."""
        if self.api.port == 8000:
            raise ValueError("API port 8000 not allowed. Default: 6315.")
        if self.environment.lower() == "production" and not self.encryption.key:
            raise ValueError(
                "ENCRYPTION_KEY is required in production. "
                "Generate one with: python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
            )
        return self

    def get_database_url(self) -> str:
        """Return the configured PostgreSQL database URL."""
        return self.database.url

    def get_redis_url(self) -> str:
        """Return the configured Redis connection URL."""
        return self.redis.url

    def is_production(self) -> bool:
        """Return True if the environment is production."""
        return self.environment.lower() == "production"

    def is_development(self) -> bool:
        """Return True if the environment is development."""
        return self.environment.lower() == "development"


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()


def clear_settings_cache() -> None:
    """Clear the cached settings instance (useful for testing)."""
    get_settings.cache_clear()
