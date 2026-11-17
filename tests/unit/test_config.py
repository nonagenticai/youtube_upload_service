"""Tests for youtube_upload_service configuration."""

from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from src.config import (
    APISettings,
    EncryptionSettings,
    Settings,
    UploadSettings,
    clear_settings_cache,
    get_settings,
)

# A real Fernet key for tests that need a non-empty encryption key
_VALID_FERNET_KEY = Fernet.generate_key().decode()


class TestSettings:
    """Test Settings class."""

    @pytest.fixture(autouse=True)
    def _isolate_from_local_dotenv(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Run every settings test from an empty cwd.

        Every ``SettingsConfigDict`` in ``src/config.py`` declares
        ``env_file=".env"`` as a RELATIVE path, so pydantic resolves it against
        the process cwd. A developer's untracked ``.env`` therefore re-supplies
        variables these tests deliberately delete, and assertions about
        "required" / "defaults when unset" pass or fail based on a file that is
        not in the repo. CI has no ``.env``, so the breakage only ever appeared
        on a real machine — exactly backwards. Chdir'ing to an empty tmp dir
        makes the whole class depend on the environment it actually asserts on.
        """
        monkeypatch.chdir(tmp_path)

    def setup_method(self) -> None:
        clear_settings_cache()

    def test_default_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify default settings values."""
        monkeypatch.delenv("ENVIRONMENT", raising=False)
        s = Settings()
        assert s.app_name == "YouTube Upload Service"
        assert s.app_version == "0.1.0"
        assert s.environment == "development"

    def test_default_port(self) -> None:
        """Default API port is 6315."""
        s = Settings()
        assert s.api.port == 6315

    def test_port_8000_rejected(self) -> None:
        """Port 8000 is not allowed."""
        with pytest.raises(ValueError, match="8000"):
            Settings(api=APISettings(port=8000))

    def test_database_url_required(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """DATABASE_URL is required env (no localhost fallback in w2v_common 0.15.0+).

        ``chdir`` into an empty tmp dir first: every ``SettingsConfigDict`` here
        declares ``env_file=".env"`` as a RELATIVE path, so a developer's local
        (untracked) ``.env`` re-supplies the very variable this test deletes and
        the assertion silently passes on broken config. CI has no ``.env``, so
        the test only ever failed on a real machine — the wrong way round.
        """
        monkeypatch.delenv("DATABASE_URL", raising=False)
        clear_settings_cache()
        with pytest.raises(ValueError):
            Settings()

    def test_redis_url_required(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """REDIS_URL is required env (no localhost fallback in w2v_common 0.15.0+).

        ``chdir`` for the same reason as ``test_database_url_required``.
        """
        monkeypatch.delenv("REDIS_URL", raising=False)
        clear_settings_cache()
        with pytest.raises(ValueError):
            Settings()

    def test_youtube_settings_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """YouTube settings default to empty client_id/secret/redirect_uri when env unset."""
        monkeypatch.delenv("YOUTUBE_CLIENT_ID", raising=False)
        monkeypatch.delenv("YOUTUBE_CLIENT_SECRET", raising=False)
        monkeypatch.delenv("YOUTUBE_REDIRECT_URI", raising=False)
        s = Settings()
        assert s.youtube.client_id == ""
        assert s.youtube.redirect_uri == ""
        assert len(s.youtube.scopes) == 3
        assert "https://www.googleapis.com/auth/youtube.force-ssl" in s.youtube.scopes

    def test_upload_settings_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Upload settings fall back to their declared defaults when env is clean.

        Two traps this test used to sit in. It asserted the literal
        ``/tmp/youtube_uploads``, but the declared default is
        ``tempfile.gettempdir()/youtube_uploads`` — ``/tmp`` only on Linux, a
        ``/var/folders/...`` path on macOS. And it deleted no env, so an ambient
        ``UPLOAD_TEMP_DIR`` (or a developer ``.env``) satisfied the assertion
        without the default ever being exercised. Compare against the field's own
        declared default so the test means "no override leaked in" on any
        platform.
        """
        monkeypatch.delenv("UPLOAD_TEMP_DIR", raising=False)
        monkeypatch.delenv("UPLOAD_MAX_CONCURRENT", raising=False)
        monkeypatch.delenv("UPLOAD_MAX_RETRIES", raising=False)
        clear_settings_cache()

        s = Settings()

        assert s.upload.max_concurrent == 4
        assert s.upload.max_retries == 5
        assert s.upload.temp_dir == UploadSettings.model_fields["temp_dir"].default
        assert s.upload.temp_dir.endswith("youtube_uploads")
        assert Path(s.upload.temp_dir).is_absolute()

    def test_encryption_settings_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Encryption settings default to empty key when not set anywhere."""
        monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
        s = Settings(encryption=EncryptionSettings(key=""))
        assert s.encryption.key == ""

    def test_get_settings_cached(self) -> None:
        """get_settings() returns the same instance."""
        s1 = get_settings()
        s2 = get_settings()
        assert s1 is s2

    def test_clear_settings_cache(self) -> None:
        """clear_settings_cache() forces a new instance."""
        s1 = get_settings()
        clear_settings_cache()
        s2 = get_settings()
        assert s1 is not s2

    def test_is_production(self) -> None:
        s = Settings(
            environment="production",
            encryption=EncryptionSettings(key=_VALID_FERNET_KEY),
        )
        assert s.is_production()
        assert not s.is_development()

    def test_is_development(self) -> None:
        s = Settings(environment="development")
        assert s.is_development()
        assert not s.is_production()

    def test_production_requires_encryption_key(self) -> None:
        """Production environment rejects empty ENCRYPTION_KEY."""
        with pytest.raises(ValueError, match="ENCRYPTION_KEY is required in production"):
            Settings(environment="production", encryption=EncryptionSettings(key=""))

    def test_production_accepts_encryption_key(self) -> None:
        """Production environment accepts a non-empty ENCRYPTION_KEY."""
        s = Settings(
            environment="production",
            encryption=EncryptionSettings(key=_VALID_FERNET_KEY),
        )
        assert s.is_production()
        assert s.encryption.key != ""
