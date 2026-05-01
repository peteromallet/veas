"""Tests for app.services.crypto."""

from __future__ import annotations

import base64
import os
import secrets
from uuid import uuid4

import pytest

from app.services import crypto


def _set_key(monkeypatch: pytest.MonkeyPatch, key: bytes | None) -> None:
    if key is None:
        monkeypatch.setenv("DATA_ENCRYPTION_KEY", "")
    else:
        monkeypatch.setenv("DATA_ENCRYPTION_KEY", base64.b64encode(key).decode())
    crypto.reset_cache_for_tests()
    # Also clear the lru_cache on get_settings so the new env is picked up.
    from app.config import get_settings

    get_settings.cache_clear()


def test_roundtrip_with_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_key(monkeypatch, secrets.token_bytes(32))
    plaintext = "the most sensitive thing"
    ct = crypto.encrypt_value(plaintext)
    assert isinstance(ct, bytes)
    assert ct.startswith(b"AGV1")
    assert plaintext.encode("utf-8") not in ct
    assert crypto.decrypt_value(ct) == plaintext


def test_roundtrip_unicode(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_key(monkeypatch, secrets.token_bytes(32))
    plaintext = "naïve — café 🔒 漢字"
    ct = crypto.encrypt_value(plaintext)
    assert crypto.decrypt_value(ct) == plaintext


def test_empty_string_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_key(monkeypatch, secrets.token_bytes(32))
    ct = crypto.encrypt_value("")
    assert crypto.decrypt_value(ct) == ""


def test_none_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_key(monkeypatch, secrets.token_bytes(32))
    assert crypto.encrypt_value(None) is None
    assert crypto.decrypt_value(None) is None


def test_passthrough_when_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_key(monkeypatch, None)
    plaintext = "ok-without-key"
    ct = crypto.encrypt_value(plaintext)
    # No magic prefix; just UTF-8.
    assert ct == plaintext.encode("utf-8")
    assert crypto.decrypt_value(ct) == plaintext
    assert crypto.is_configured() is False


def test_decrypt_legacy_plaintext_with_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A row written before encryption was enabled is plain UTF-8 in bytea."""
    _set_key(monkeypatch, secrets.token_bytes(32))
    legacy = "I was written before we had a key".encode("utf-8")
    assert crypto.decrypt_value(legacy) == "I was written before we had a key"


def test_wrong_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_key(monkeypatch, secrets.token_bytes(32))
    ct = crypto.encrypt_value("secret")
    _set_key(monkeypatch, secrets.token_bytes(32))  # rotate to a different key
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt_value(ct)


def test_tampered_ciphertext_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_key(monkeypatch, secrets.token_bytes(32))
    ct = bytearray(crypto.encrypt_value("secret"))
    ct[-1] ^= 0xFF
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt_value(bytes(ct))


def test_truncated_ciphertext_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_key(monkeypatch, secrets.token_bytes(32))
    ct = crypto.encrypt_value("secret")
    truncated = ct[:6]  # smaller than nonce+tag
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt_value(truncated)


def test_aesgcm_ciphertext_without_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_key(monkeypatch, secrets.token_bytes(32))
    ct = crypto.encrypt_value("secret")
    _set_key(monkeypatch, None)
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt_value(ct)


def test_memoryview_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """asyncpg can hand us memoryview for bytea; helper must accept it."""
    _set_key(monkeypatch, secrets.token_bytes(32))
    ct = crypto.encrypt_value("hello")
    assert crypto.decrypt_value(memoryview(ct)) == "hello"


@pytest.mark.asyncio
async def test_oob_write_round_trip_via_fake_pool(monkeypatch: pytest.MonkeyPatch, fake_pool) -> None:
    """End-to-end: writing an OOB row populates ciphertext that decrypts."""
    _set_key(monkeypatch, secrets.token_bytes(32))

    owner_id = uuid4()
    sensitive = "the protected core"
    row = await fake_pool.fetchrow(
        """
        INSERT INTO out_of_bounds (
            owner_id, sensitive_core, sensitive_core_encrypted, shareable_context, severity, review_at
        )
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING id
        """,
        owner_id,
        sensitive,
        crypto.encrypt_value(sensitive),
        None,
        "firm",
        None,
    )

    stored = fake_pool.out_of_bounds[row["id"]]
    ct = stored["sensitive_core_encrypted"]
    assert isinstance(ct, bytes)
    assert ct.startswith(b"AGV1")
    assert sensitive.encode("utf-8") not in ct
    assert crypto.decrypt_value(ct) == sensitive
