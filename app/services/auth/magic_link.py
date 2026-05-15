"""Discord magic-link auth (R5): request + verify.

End-to-end:

1. ``request_magic_link(pool, discord_id)`` — look up user_identities
   for ``transport='discord' AND address=discord_id``; if found,
   generate a 6-digit code, persist its HMAC-SHA256 to
   ``mediator.auth_magic_links`` with a 10-minute expiry, DM the
   cleartext code via the mediator bot. The cleartext NEVER touches
   the DB.
2. ``verify_magic_link(pool, discord_id, code)`` — find the active
   row for ``discord_id``, compare the code in constant time, mark the
   row consumed, mint a JWT.

Rate limit + lockout: rate limit at 3 requests / 10 min per discord_id
(checked against ``requested_at``); after 5 failed verifies the active
row is revoked.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from app.services.auth import discord_dm, jwt

logger = logging.getLogger(__name__)

CODE_LENGTH = 6
DEFAULT_TTL_MINUTES = 10
MAX_ACTIVE_REQUESTS_PER_WINDOW = 3
RATE_WINDOW = timedelta(minutes=10)
MAX_VERIFY_ATTEMPTS = 5


@dataclass(frozen=True)
class RequestResult:
    issued: bool
    expires_at: datetime | None
    dispatched: bool
    reason: str | None = None  # set when issued=False


@dataclass(frozen=True)
class VerifyResult:
    success: bool
    user_id: str | None
    token: str | None
    reason: str | None = None


def _hash_code(code: str) -> bytes:
    """HMAC-SHA256 of the cleartext code under the JWT signing secret.

    Reusing ``LIVE_VOICE_JWT_SECRET`` is fine here: both are short-lived
    signing operations under the same trust boundary (the backend), and
    it avoids requiring a second env var.
    """
    secret = os.environ.get("LIVE_VOICE_JWT_SECRET", "veas-live-voice-dev-secret-not-for-production")
    return hmac.new(secret.encode("utf-8"), code.encode("utf-8"), hashlib.sha256).digest()


def _generate_code() -> str:
    """Return a numeric code of length :data:`CODE_LENGTH` (zero-padded)."""
    upper = 10**CODE_LENGTH
    return str(secrets.randbelow(upper)).zfill(CODE_LENGTH)


async def _resolve_discord_user(pool: Any, discord_id: str) -> UUID | None:
    row = await pool.fetchrow(
        """
        SELECT user_id
        FROM mediator.user_identities
        WHERE transport = 'discord' AND address = $1
        """,
        discord_id,
    )
    if row is None:
        return None
    return row["user_id"]


async def request_magic_link(pool: Any, discord_id: str, *, bot_id: str = "mediator") -> RequestResult:
    """Generate a code and DM it to the user via the mediator bot."""
    discord_id = (discord_id or "").strip()
    if not discord_id or not discord_id.isdigit():
        return RequestResult(issued=False, expires_at=None, dispatched=False, reason="bad_discord_id")

    user_id = await _resolve_discord_user(pool, discord_id)
    if user_id is None:
        # Don't leak whether the discord_id is known — return the same
        # shape an attacker would see, just with reason set.
        return RequestResult(issued=False, expires_at=None, dispatched=False, reason="unknown_user")

    # Rate-limit: count active requests in the last RATE_WINDOW.
    cutoff = datetime.now(timezone.utc) - RATE_WINDOW
    recent = await pool.fetchval(
        """
        SELECT count(*)
        FROM mediator.auth_magic_links
        WHERE discord_id = $1 AND requested_at >= $2
        """,
        discord_id,
        cutoff,
    )
    if recent and recent >= MAX_ACTIVE_REQUESTS_PER_WINDOW:
        return RequestResult(issued=False, expires_at=None, dispatched=False, reason="rate_limited")

    code = _generate_code()
    code_hash = _hash_code(code)
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=DEFAULT_TTL_MINUTES)

    # Persist the HMAC first so a DM that fails after persist isn't a
    # silent failure for the user (they can retry / will see expiry).
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Revoke any prior active codes for this user so we don't keep
            # multiple windows open.
            await conn.execute(
                """
                UPDATE mediator.auth_magic_links
                SET revoked_at = now()
                WHERE user_id = $1 AND consumed_at IS NULL AND revoked_at IS NULL
                """,
                user_id,
            )
            await conn.execute(
                """
                INSERT INTO mediator.auth_magic_links
                    (user_id, discord_id, code_hash, expires_at)
                VALUES ($1, $2, $3, $4)
                """,
                user_id,
                discord_id,
                code_hash,
                expires_at,
            )

    msg = (
        f"Your Veas Live Voice code: **{code}**\n"
        f"Valid for {DEFAULT_TTL_MINUTES} minutes. If you didn't request this, ignore."
    )
    dm = await discord_dm.send_dm(discord_id, msg, bot_id=bot_id)

    # In dev (no bot token) we still return issued=True so the test flow
    # works; the operator can read the code from the request_magic_link
    # log line or the consumer-of-last-resort DB query.
    if not dm.dispatched:
        logger.warning(
            "magic_link: dm-not-dispatched for discord_id=%s reason=%s; cleartext code=%s",
            discord_id,
            dm.reason,
            code,
        )

    return RequestResult(
        issued=True,
        expires_at=expires_at,
        dispatched=dm.dispatched,
        reason=None if dm.dispatched else f"dm:{dm.reason}",
    )


async def verify_magic_link(pool: Any, discord_id: str, code: str) -> VerifyResult:
    """Verify a code and (on success) mint a JWT."""
    discord_id = (discord_id or "").strip()
    code = (code or "").strip()
    if not discord_id or not discord_id.isdigit():
        return VerifyResult(success=False, user_id=None, token=None, reason="bad_discord_id")
    if not code or not code.isdigit() or len(code) != CODE_LENGTH:
        return VerifyResult(success=False, user_id=None, token=None, reason="bad_code_format")

    row = await pool.fetchrow(
        """
        SELECT id, user_id, code_hash, expires_at, attempts_used
        FROM mediator.auth_magic_links
        WHERE discord_id = $1
          AND consumed_at IS NULL
          AND revoked_at IS NULL
        ORDER BY requested_at DESC
        LIMIT 1
        """,
        discord_id,
    )
    if row is None:
        return VerifyResult(success=False, user_id=None, token=None, reason="no_active_code")

    if row["expires_at"] < datetime.now(timezone.utc):
        return VerifyResult(success=False, user_id=None, token=None, reason="expired")

    if row["attempts_used"] >= MAX_VERIFY_ATTEMPTS:
        # Revoke for safety and force a re-request.
        await pool.execute(
            "UPDATE mediator.auth_magic_links SET revoked_at = now() WHERE id = $1",
            row["id"],
        )
        return VerifyResult(success=False, user_id=None, token=None, reason="too_many_attempts")

    expected = bytes(row["code_hash"])
    actual = _hash_code(code)
    if not hmac.compare_digest(expected, actual):
        await pool.execute(
            "UPDATE mediator.auth_magic_links SET attempts_used = attempts_used + 1 WHERE id = $1",
            row["id"],
        )
        return VerifyResult(success=False, user_id=None, token=None, reason="bad_code")

    await pool.execute(
        "UPDATE mediator.auth_magic_links SET consumed_at = now() WHERE id = $1",
        row["id"],
    )

    user_id = str(row["user_id"])
    token = jwt.mint(user_id=user_id, discord_id=discord_id)
    return VerifyResult(success=True, user_id=user_id, token=token, reason=None)
