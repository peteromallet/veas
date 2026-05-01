"""Symmetric encryption helpers for sensitive columns.

The operator sets ``DATA_ENCRYPTION_KEY`` (a base64-encoded 32-byte key) in
the environment. With the key set, ``encrypt_value`` returns AES-GCM
ciphertext (12-byte nonce || 16-byte tag || ct) and ``decrypt_value`` reverses
it. Without the key set the helpers pass-through plaintext (str <-> bytes via
UTF-8) and a one-time warning is logged at import time so dev/test continues
to work while production must opt in.

Key rotation is intentionally out of scope for this pass: rotating means
re-encrypting every row under a new key, which requires either downtime or a
dual-key read path. Documented as a follow-up in docs/SECURITY.md.
"""

from __future__ import annotations

import base64
import logging
import os
import secrets
from functools import lru_cache

logger = logging.getLogger(__name__)

# Magic prefix for AES-GCM ciphertext we produce. Lets ``decrypt_value`` tell
# AES-GCM ciphertext apart from passthrough bytes (which would be raw UTF-8)
# and from pgcrypto pgp_sym_encrypt output (which starts with 0xC3 / 0x85).
_AESGCM_PREFIX = b"AGV1"  # "AES-GCM v1", arbitrary 4-byte sentinel.
_NONCE_SIZE = 12
_KEY_SIZE = 32


class CryptoError(RuntimeError):
    """Raised on decrypt failure (wrong key, tampered ciphertext, bad framing)."""


def _decode_key(raw: str | None) -> bytes | None:
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None
    try:
        key = base64.b64decode(raw, validate=True)
    except Exception as exc:  # pragma: no cover - defensive
        raise CryptoError(f"DATA_ENCRYPTION_KEY is not valid base64: {exc}") from exc
    if len(key) != _KEY_SIZE:
        raise CryptoError(
            f"DATA_ENCRYPTION_KEY must decode to {_KEY_SIZE} bytes, got {len(key)}"
        )
    return key


@lru_cache(maxsize=1)
def _key() -> bytes | None:
    """Resolve the active encryption key.

    Reads from settings if available, otherwise falls back to the env var
    directly. Cached so we only hit the import-time warning once per process.
    """
    raw: str | None = None
    try:
        from app.config import get_settings

        settings = get_settings()
        secret = getattr(settings, "data_encryption_key", None)
        if secret is not None:
            raw = secret.get_secret_value()
    except Exception:  # pragma: no cover - settings unavailable in some tools
        raw = None
    if raw is None:
        raw = os.environ.get("DATA_ENCRYPTION_KEY")
    key = _decode_key(raw)
    if key is None:
        logger.warning(
            "DATA_ENCRYPTION_KEY is not set; sensitive columns will be stored as plaintext. "
            "Set it in production to enable AES-GCM encryption at rest."
        )
    return key


def reset_cache_for_tests() -> None:
    """Drop the cached key so tests can flip the env mid-run."""
    _key.cache_clear()


def _aesgcm():
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    return AESGCM


def encrypt_value(plaintext: str | None) -> bytes | None:
    """Return AES-GCM ciphertext for ``plaintext``.

    Returns ``None`` when ``plaintext`` is ``None``. When the key is not
    configured, falls back to UTF-8 bytes (passthrough). The returned bytes
    are safe to store in a ``bytea`` column.
    """
    if plaintext is None:
        return None
    key = _key()
    if key is None:
        return plaintext.encode("utf-8")
    AESGCM = _aesgcm()
    nonce = secrets.token_bytes(_NONCE_SIZE)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    return _AESGCM_PREFIX + nonce + ct


def decrypt_value(ciphertext: bytes | memoryview | None) -> str | None:
    """Reverse :func:`encrypt_value`.

    Recognises three input shapes:

    * ``None`` -> ``None``
    * AES-GCM framed bytes (with the ``_AESGCM_PREFIX`` magic) -> decrypt
    * Anything else -> treated as UTF-8 plaintext passthrough

    The passthrough branch is what lets pre-encryption rows keep working
    without a backfill step, and it lets dev/test stacks run without a key.
    """
    if ciphertext is None:
        return None
    if isinstance(ciphertext, memoryview):
        ciphertext = bytes(ciphertext)
    if not isinstance(ciphertext, (bytes, bytearray)):
        # asyncpg sometimes returns str for bytea when registered codecs run.
        # Best effort: assume already-plaintext.
        return str(ciphertext)
    raw = bytes(ciphertext)
    if raw.startswith(_AESGCM_PREFIX):
        key = _key()
        if key is None:
            raise CryptoError(
                "ciphertext is AES-GCM framed but DATA_ENCRYPTION_KEY is not set"
            )
        body = raw[len(_AESGCM_PREFIX):]
        if len(body) < _NONCE_SIZE + 16:
            raise CryptoError("AES-GCM ciphertext truncated")
        nonce = body[:_NONCE_SIZE]
        ct = body[_NONCE_SIZE:]
        AESGCM = _aesgcm()
        try:
            pt = AESGCM(key).decrypt(nonce, ct, None)
        except Exception as exc:
            raise CryptoError("AES-GCM decryption failed (wrong key or tampered ciphertext)") from exc
        return pt.decode("utf-8")
    # Passthrough: assume the bytes were stored as UTF-8 plaintext (legacy /
    # key-not-set path).
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CryptoError("non-UTF-8 bytes in passthrough decrypt") from exc


def is_configured() -> bool:
    """Return True when an encryption key is active."""
    return _key() is not None


__all__ = [
    "CryptoError",
    "decrypt_value",
    "encrypt_value",
    "is_configured",
    "reset_cache_for_tests",
]
