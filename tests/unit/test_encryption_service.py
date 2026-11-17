"""Tests for the Fernet encryption service."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet, InvalidToken

from src.services.encryption_service import EncryptionService


@pytest.mark.unit
class TestEncryptionServiceEnabled:
    """Tests with a valid Fernet key."""

    @pytest.fixture
    def key(self) -> str:
        return Fernet.generate_key().decode()

    @pytest.fixture
    def svc(self, key: str) -> EncryptionService:
        return EncryptionService(key)

    def test_enabled(self, svc: EncryptionService) -> None:
        assert svc.enabled is True

    def test_roundtrip(self, svc: EncryptionService) -> None:
        plaintext = "my-secret-refresh-token"
        encrypted = svc.encrypt(plaintext)
        assert encrypted != plaintext
        assert svc.decrypt(encrypted) == plaintext

    def test_encrypt_produces_different_ciphertext_each_time(self, svc: EncryptionService) -> None:
        """Fernet includes a timestamp, so two encryptions of the same value differ."""
        a = svc.encrypt("same")
        b = svc.encrypt("same")
        assert a != b

    def test_decrypt_wrong_key_raises(self, svc: EncryptionService) -> None:
        encrypted = svc.encrypt("secret")
        other = EncryptionService(Fernet.generate_key().decode())
        with pytest.raises(InvalidToken):
            other.decrypt(encrypted)

    def test_decrypt_corrupted_data_raises(self, svc: EncryptionService) -> None:
        with pytest.raises(InvalidToken):
            svc.decrypt("not-valid-fernet-data")

    def test_empty_string_roundtrip(self, svc: EncryptionService) -> None:
        assert svc.decrypt(svc.encrypt("")) == ""

    def test_unicode_roundtrip(self, svc: EncryptionService) -> None:
        text = "token-with-unicode-\u00e9\u00e8\u00ea"
        assert svc.decrypt(svc.encrypt(text)) == text


@pytest.mark.unit
class TestEncryptionServiceDisabled:
    """Tests with no key (passthrough mode)."""

    @pytest.fixture
    def svc(self) -> EncryptionService:
        return EncryptionService("")

    def test_disabled(self, svc: EncryptionService) -> None:
        assert svc.enabled is False

    def test_encrypt_passthrough(self, svc: EncryptionService) -> None:
        assert svc.encrypt("plaintext") == "plaintext"

    def test_decrypt_passthrough(self, svc: EncryptionService) -> None:
        assert svc.decrypt("plaintext") == "plaintext"


@pytest.mark.unit
class TestGenerateKey:
    def test_generates_valid_fernet_key(self) -> None:
        key = EncryptionService.generate_key()
        # Should be usable as a Fernet key without error
        Fernet(key.encode())

    def test_generates_unique_keys(self) -> None:
        assert EncryptionService.generate_key() != EncryptionService.generate_key()
