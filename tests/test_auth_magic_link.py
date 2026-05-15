"""Tests for the Discord magic-link auth flow (Sprint 0 / R5).

These tests use a FakePool that mimics the asyncpg surface so we can
exercise the request + verify flow without a live DB.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import pytest

from app.services.auth import jwt
from app.services.auth.discord_dm import DmResult
from app.services.auth.magic_link import (
    DEFAULT_TTL_MINUTES,
    MAX_VERIFY_ATTEMPTS,
    request_magic_link,
    verify_magic_link,
)


# --------------------------------------------------------------------------- #
# JWT round-trip.
# --------------------------------------------------------------------------- #


def test_jwt_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIVE_VOICE_JWT_SECRET", "test-secret-not-for-prod")
    # Reset the lru_cache so the new env var is picked up.
    jwt._signing_secret.cache_clear()  # type: ignore[attr-defined]
    token = jwt.mint(user_id="00000000-0000-0000-0000-000000000001", discord_id="123456789012345678")
    claims = jwt.verify(token)
    assert claims.user_id == "00000000-0000-0000-0000-000000000001"
    assert claims.discord_id == "123456789012345678"


def test_jwt_rejects_tampered_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIVE_VOICE_JWT_SECRET", "test-secret-not-for-prod")
    jwt._signing_secret.cache_clear()  # type: ignore[attr-defined]
    token = jwt.mint(user_id="abc", discord_id="123456789012345678")
    header, payload, sig = token.split(".")
    # Swap a single character in the payload — base64 still decodes but
    # the HMAC won't match.
    tampered = payload[:-1] + ("A" if payload[-1] != "A" else "B")
    with pytest.raises(jwt.JWTError, match="bad signature"):
        jwt.verify(f"{header}.{tampered}.{sig}")


# --------------------------------------------------------------------------- #
# FakePool with just enough surface for magic_link.
# --------------------------------------------------------------------------- #


class _FakePool:
    def __init__(self) -> None:
        self.user_identities: dict[tuple[str, str], UUID] = {}
        self.magic_links: list[dict[str, Any]] = []
        self.user_rows: dict[UUID, dict[str, Any]] = {}

    # Public test helpers.
    def add_identity(self, transport: str, address: str, user_id: UUID) -> None:
        self.user_identities[(transport, address)] = user_id

    # asyncpg surface used by magic_link.
    def acquire(self) -> "_FakeAcquire":
        return _FakeAcquire(self)

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        compact = " ".join(sql.split())
        if "FROM mediator.user_identities" in compact:
            return self._lookup_identity(args[0])
        if "FROM mediator.auth_magic_links" in compact and "ORDER BY requested_at DESC" in compact:
            return self._latest_active(args[0])
        return None

    async def fetchval(self, sql: str, *args: Any) -> Any:
        compact = " ".join(sql.split())
        if "FROM mediator.auth_magic_links" in compact and "count(" in compact.lower():
            discord_id, cutoff = args
            return sum(
                1 for r in self.magic_links if r["discord_id"] == discord_id and r["requested_at"] >= cutoff
            )
        return None

    async def execute(self, sql: str, *args: Any) -> str:
        compact = " ".join(sql.split())
        if "SET attempts_used = attempts_used + 1" in compact:
            link_id = args[0]
            for r in self.magic_links:
                if r["id"] == link_id:
                    r["attempts_used"] += 1
        elif "SET consumed_at = now()" in compact:
            link_id = args[0]
            for r in self.magic_links:
                if r["id"] == link_id:
                    r["consumed_at"] = datetime.now(timezone.utc)
        elif "SET revoked_at = now()" in compact and "WHERE id = $1" in compact:
            link_id = args[0]
            for r in self.magic_links:
                if r["id"] == link_id:
                    r["revoked_at"] = datetime.now(timezone.utc)
        return "OK"

    def _lookup_identity(self, address: Any) -> dict[str, Any] | None:
        for (transport, addr), user_id in self.user_identities.items():
            if transport == "discord" and addr == address:
                return {"user_id": user_id}
        return None

    def _latest_active(self, discord_id: str) -> dict[str, Any] | None:
        active = [
            r for r in self.magic_links
            if r["discord_id"] == discord_id and r["consumed_at"] is None and r["revoked_at"] is None
        ]
        if not active:
            return None
        return max(active, key=lambda r: r["requested_at"])


class _FakeConn:
    def __init__(self, parent: _FakePool) -> None:
        self._parent = parent

    def transaction(self) -> "_FakeTxn":
        return _FakeTxn()

    async def execute(self, sql: str, *args: Any) -> str:
        compact = " ".join(sql.split())
        if "UPDATE mediator.auth_magic_links" in compact and "SET revoked_at = now()" in compact and "WHERE user_id = $1" in compact:
            user_id = args[0]
            now = datetime.now(timezone.utc)
            for r in self._parent.magic_links:
                if r["user_id"] == user_id and r["consumed_at"] is None and r["revoked_at"] is None:
                    r["revoked_at"] = now
        elif "INSERT INTO mediator.auth_magic_links" in compact:
            user_id, discord_id, code_hash, expires_at = args
            self._parent.magic_links.append({
                "id": f"link-{len(self._parent.magic_links) + 1}",
                "user_id": user_id,
                "discord_id": discord_id,
                "code_hash": code_hash,
                "expires_at": expires_at,
                "attempts_used": 0,
                "consumed_at": None,
                "revoked_at": None,
                "requested_at": datetime.now(timezone.utc),
            })
        return "OK"


class _FakeTxn:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _FakeAcquire:
    def __init__(self, parent: _FakePool) -> None:
        self._parent = parent

    async def __aenter__(self) -> _FakeConn:
        return _FakeConn(self._parent)

    async def __aexit__(self, *exc: Any) -> None:
        return None


# --------------------------------------------------------------------------- #
# Flow tests.
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _reset_jwt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIVE_VOICE_JWT_SECRET", "test-secret-not-for-prod")
    jwt._signing_secret.cache_clear()  # type: ignore[attr-defined]


@pytest.fixture
def _stub_dm(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Replace send_dm with a stub that records (discord_id, content)."""
    sent: list[tuple[str, str]] = []

    async def fake_send_dm(discord_id: str, content: str, *, bot_id: str = "mediator") -> DmResult:
        sent.append((discord_id, content))
        return DmResult(dispatched=True, dm_channel_id="chan-stub")

    monkeypatch.setattr("app.services.auth.magic_link.discord_dm.send_dm", fake_send_dm)
    return sent


@pytest.mark.anyio
async def test_request_and_verify_happy_path(_stub_dm: list[tuple[str, str]]) -> None:
    user_id = UUID("00000000-0000-0000-0000-000000000001")
    pool = _FakePool()
    pool.add_identity("discord", "123456789012345678", user_id)

    req = await request_magic_link(pool, "123456789012345678")
    assert req.issued is True
    assert req.dispatched is True
    assert len(_stub_dm) == 1

    # Extract the cleartext code from the DM body — it's the 6-digit token.
    sent_content = _stub_dm[0][1]
    import re
    match = re.search(r"\b(\d{6})\b", sent_content)
    assert match, sent_content
    code = match.group(1)

    res = await verify_magic_link(pool, "123456789012345678", code)
    assert res.success is True
    assert res.user_id == str(user_id)
    assert res.token

    # The JWT must verify under the test secret.
    claims = jwt.verify(res.token)
    assert claims.user_id == str(user_id)
    assert claims.discord_id == "123456789012345678"


@pytest.mark.anyio
async def test_verify_wrong_code_increments_attempts(_stub_dm: list[tuple[str, str]]) -> None:
    user_id = UUID("00000000-0000-0000-0000-000000000001")
    pool = _FakePool()
    pool.add_identity("discord", "123456789012345678", user_id)

    await request_magic_link(pool, "123456789012345678")
    res = await verify_magic_link(pool, "123456789012345678", "000000")
    assert res.success is False
    assert res.reason == "bad_code"
    assert pool.magic_links[-1]["attempts_used"] == 1


@pytest.mark.anyio
async def test_verify_too_many_attempts_revokes(_stub_dm: list[tuple[str, str]]) -> None:
    user_id = UUID("00000000-0000-0000-0000-000000000001")
    pool = _FakePool()
    pool.add_identity("discord", "123456789012345678", user_id)
    await request_magic_link(pool, "123456789012345678")

    for _ in range(MAX_VERIFY_ATTEMPTS):
        await verify_magic_link(pool, "123456789012345678", "000000")

    res = await verify_magic_link(pool, "123456789012345678", "000000")
    assert res.success is False
    assert res.reason == "too_many_attempts"
    assert pool.magic_links[-1]["revoked_at"] is not None


@pytest.mark.anyio
async def test_request_unknown_discord_id(_stub_dm: list[tuple[str, str]]) -> None:
    pool = _FakePool()  # no identity
    res = await request_magic_link(pool, "123456789012345678")
    assert res.issued is False
    assert res.reason == "unknown_user"
    assert _stub_dm == []  # no DM sent


@pytest.mark.anyio
async def test_request_rate_limit(_stub_dm: list[tuple[str, str]]) -> None:
    user_id = UUID("00000000-0000-0000-0000-000000000001")
    pool = _FakePool()
    pool.add_identity("discord", "123456789012345678", user_id)

    for _ in range(3):
        await request_magic_link(pool, "123456789012345678")
    res = await request_magic_link(pool, "123456789012345678")
    assert res.issued is False
    assert res.reason == "rate_limited"
