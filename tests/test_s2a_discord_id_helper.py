"""Discord bot user-id helper tests.

Verifies:
- discord_bot_user_id() returns env var when set and digit-only
- Falls back to decoded token when DISCORD_BOT_USER_ID is not set
- Returns None when both are empty/unavailable
- Confirms parity with S1 seeder (same decode logic)
"""

from __future__ import annotations

import os
import pytest

from app.services.discord_id import (
    discord_bot_user_id,
    _decode_discord_user_id,
    _DISCORD_BOT_USER_ID_RE,
)


# ---------------------------------------------------------------------------
# _decode_discord_user_id
# ---------------------------------------------------------------------------

class TestDecodeDiscordUserID:
    """Tests for _decode_discord_user_id."""

    def test_decodes_valid_token(self):
        """A valid bot token prefix decodes to the numeric user id."""
        # "1245222614276898866" encodes to "MTI0NTIyMjYxNDI3Njg5ODg2Ng" (no padding)
        # base64url: M T I 0 N T I y M j Y x N D I 3 N j g 5 O D g 2 N g
        token = "MTI0NTIyMjYxNDI3Njg5ODg2Ng.abc.def"
        result = _decode_discord_user_id(token)
        assert result == "1245222614276898866", f"Got {result}"

    def test_decodes_token_needs_padding(self):
        """Token with segment that needs padding still works."""
        # "1234567890" -> "MTIzNDU2Nzg5MA" (len 16, no padding needed already)
        token = "MTIzNDU2Nzg5MA.abc.def"
        result = _decode_discord_user_id(token)
        assert result == "1234567890", f"Got {result}"

    def test_returns_none_for_non_numeric_decode(self):
        """Non-numeric decode result returns None."""
        # "hello" -> "aGVsbG8"
        token = "aGVsbG8.abc.def"
        result = _decode_discord_user_id(token)
        assert result is None, f"Non-numeric decode should return None, got {result}"

    def test_returns_none_for_empty_prefix(self):
        """Empty prefix returns None."""
        token = ".abc.def"
        result = _decode_discord_user_id(token)
        assert result is None

    def test_returns_none_for_garbage_token(self):
        """Garbage token returns None."""
        result = _decode_discord_user_id("not-a-token")
        assert result is None

    def test_returns_none_for_invalid_base64(self):
        """Invalid base64 in prefix returns None."""
        result = _decode_discord_user_id("!!!.abc.def")
        assert result is None


# ---------------------------------------------------------------------------
# discord_bot_user_id
# ---------------------------------------------------------------------------

class TestDiscordBotUserID:
    """Tests for discord_bot_user_id."""

    def test_returns_env_var_when_set(self, monkeypatch):
        """DISCORD_BOT_USER_ID env var takes priority."""
        monkeypatch.setenv("DISCORD_BOT_USER_ID", "999888777666555444")
        result = discord_bot_user_id()
        assert result == "999888777666555444"

    def test_env_var_must_be_digit_only(self, monkeypatch):
        """Non-digit DISCORD_BOT_USER_ID is rejected (falls back to token decode)."""
        monkeypatch.setenv("DISCORD_BOT_USER_ID", "not-digits")
        monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
        result = discord_bot_user_id()
        assert result is None, f"Non-digit env var should be rejected, got {result}"

    def test_falls_back_to_token_decode(self, monkeypatch):
        """When DISCORD_BOT_USER_ID is not set, falls back to DISCORD_BOT_TOKEN."""
        monkeypatch.delenv("DISCORD_BOT_USER_ID", raising=False)
        # Use a valid token segment that decodes to all digits
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "MTIzNDU2Nzg5MA.sig.hmac")
        result = discord_bot_user_id()
        assert result == "1234567890", f"Got {result}"

    def test_returns_none_when_both_empty(self, monkeypatch):
        """Returns None when neither env var is available."""
        monkeypatch.delenv("DISCORD_BOT_USER_ID", raising=False)
        monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
        result = discord_bot_user_id()
        assert result is None

    def test_returns_none_when_token_is_empty_string(self, monkeypatch):
        """Empty DISCORD_BOT_TOKEN returns None."""
        monkeypatch.delenv("DISCORD_BOT_USER_ID", raising=False)
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "")
        result = discord_bot_user_id()
        assert result is None


# ---------------------------------------------------------------------------
# Parity with S1 seeder
# ---------------------------------------------------------------------------

class TestS1SeederParity:
    """Confirm parity with S1 seeder logic."""

    def test_regex_matches_s1_pattern(self):
        """_DISCORD_BOT_USER_ID_RE matches the same strings as S1 seeder."""
        assert _DISCORD_BOT_USER_ID_RE.match("1245222614276898866")
        assert not _DISCORD_BOT_USER_ID_RE.match("abc123")
        assert not _DISCORD_BOT_USER_ID_RE.match("")
        assert not _DISCORD_BOT_USER_ID_RE.match("12.34")

    def test_seed_channels_imports_from_shared_module(self):
        """seed_channels.py imports from app.services.discord_id."""
        content = open("scripts/seed_channels.py").read()
        assert "from app.services.discord_id import" in content, (
            "seed_channels.py must import from the shared discord_id module"
        )

    def test_seed_channels_no_local_duplicate(self):
        """seed_channels.py has no local _decode_discord_user_id definition."""
        content = open("scripts/seed_channels.py").read()
        assert "def _decode_discord_user_id" not in content, (
            "seed_channels.py must NOT have a local _decode_discord_user_id — single source of truth"
        )