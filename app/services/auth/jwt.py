"""Minimal HMAC-SHA256 JWT (compact serialization).

Self-contained — no `python-jose` / `pyjwt` dependency. We only need the
HS256 algorithm with a single signing secret. Tokens are 15-minute by
default with a `kind` claim set to "live_voice".

Reads ``LIVE_VOICE_JWT_SECRET`` from env. If unset, a deterministic
warning-tinted dev key is used so local boot works (the warning is
logged once). Production MUST set the env var.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from dataclasses import dataclass
from functools import lru_cache

logger = logging.getLogger(__name__)

_DEV_FALLBACK_SECRET = "veas-live-voice-dev-secret-not-for-production"
_DEFAULT_TTL_SECONDS = 15 * 60
_ISSUER = "veas-live-voice"


class JWTError(RuntimeError):
    """Raised on bad signature / expiry / malformed payload."""


@dataclass(frozen=True)
class ValidatedToken:
    user_id: str
    discord_id: str | None
    expires_at: int
    issued_at: int
    raw_claims: dict[str, object]


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


@lru_cache(maxsize=1)
def _signing_secret() -> bytes:
    raw = os.environ.get("LIVE_VOICE_JWT_SECRET", "").strip()
    if not raw:
        logger.warning(
            "LIVE_VOICE_JWT_SECRET not set; using dev fallback. "
            "DO NOT use this in production — set the env var to a strong random string."
        )
        raw = _DEV_FALLBACK_SECRET
    return raw.encode("utf-8")


def mint(
    *,
    user_id: str,
    discord_id: str | None = None,
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    extra: dict[str, object] | None = None,
) -> str:
    """Mint a short-lived HS256 JWT.

    Claims: iss, sub (=user_id), iat, exp, plus discord_id and anything
    in ``extra``.
    """
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload: dict[str, object] = {
        "iss": _ISSUER,
        "sub": user_id,
        "iat": now,
        "exp": now + max(60, int(ttl_seconds)),
        "kind": "live_voice",
    }
    if discord_id:
        payload["discord_id"] = discord_id
    if extra:
        payload.update(extra)

    header_part = _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    payload_part = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{header_part}.{payload_part}".encode("ascii")
    sig = hmac.new(_signing_secret(), signing_input, hashlib.sha256).digest()
    return f"{header_part}.{payload_part}.{_b64url_encode(sig)}"


def verify(token: str) -> ValidatedToken:
    """Verify signature + expiry; return the validated claims."""
    parts = token.split(".")
    if len(parts) != 3:
        raise JWTError("malformed token: expected 3 parts")
    header_b64, payload_b64, signature_b64 = parts
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    expected_sig = hmac.new(_signing_secret(), signing_input, hashlib.sha256).digest()
    try:
        actual_sig = _b64url_decode(signature_b64)
    except Exception as exc:
        raise JWTError(f"bad signature encoding: {exc}") from exc
    if not hmac.compare_digest(expected_sig, actual_sig):
        raise JWTError("bad signature")
    try:
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
        header = json.loads(_b64url_decode(header_b64).decode("utf-8"))
    except Exception as exc:
        raise JWTError(f"bad payload encoding: {exc}") from exc
    if header.get("alg") != "HS256":
        raise JWTError(f"unexpected alg: {header.get('alg')!r}")
    if payload.get("iss") != _ISSUER:
        raise JWTError(f"bad issuer: {payload.get('iss')!r}")
    exp = payload.get("exp")
    if not isinstance(exp, int) or exp < int(time.time()):
        raise JWTError("token expired")
    sub = payload.get("sub")
    if not isinstance(sub, str):
        raise JWTError("missing sub claim")
    return ValidatedToken(
        user_id=sub,
        discord_id=payload.get("discord_id") if isinstance(payload.get("discord_id"), str) else None,
        expires_at=exp,
        issued_at=int(payload.get("iat") or 0),
        raw_claims=payload,
    )
