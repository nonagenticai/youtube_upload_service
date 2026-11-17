"""Fernet encryption service for OAuth2 token storage at rest.

Tokens stored in the oauth_tokens table are encrypted with a symmetric Fernet key.
The key is loaded from ENCRYPTION_KEY env var. If empty, encryption is disabled
(passthrough) — suitable for development but NOT for production.
"""

from __future__ import annotations

import logging

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)


class EncryptionService:
    """Encrypt and decrypt strings using Fernet symmetric encryption."""

    def __init__(self, key: str) -> None:
        if key:
            self._fernet: Fernet | None = Fernet(key.encode() if isinstance(key, str) else key)
            self._enabled = True
        else:
            self._fernet = None
            self._enabled = False
            logger.warning("ENCRYPTION_KEY not set — token encryption disabled (dev mode)")

    @property
    def enabled(self) -> bool:
        return self._enabled

    def encrypt(self, plaintext: str) -> str:
        """Encrypt a plaintext string. Returns base64-encoded ciphertext."""
        if not self._enabled:
            return plaintext
        if self._fernet is None:
            raise RuntimeError("Encryption enabled but Fernet key not configured")
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        """Decrypt a Fernet-encrypted string. Raises InvalidToken on failure."""
        if not self._enabled:
            return ciphertext
        if self._fernet is None:
            raise RuntimeError("Encryption enabled but Fernet key not configured")
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken:
            logger.exception("Failed to decrypt token — key mismatch or corrupted data")
            raise

    @staticmethod
    def generate_key() -> str:
        """Generate a new Fernet key (utility for setup)."""
        return Fernet.generate_key().decode()
