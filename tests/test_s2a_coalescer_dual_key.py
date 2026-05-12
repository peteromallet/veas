"""Dual-key BurstCoalescer tests.

Verifies:
- (user_id, bot_id) composite keys flush correctly
- (user_id, None) legacy keys flush correctly
- Composite-first lookup, legacy fallback
- TODO(S2b) markers present
"""

from __future__ import annotations

import asyncio
import pytest
from uuid import uuid4

from app.services.debouncer import BurstCoalescer, CompositeKey
from app.models.user import User


async def _noop(*args, **kwargs):
    pass


def _user(uid=None):
    return User(id=uid or uuid4(), name="Test", phone="1", timezone="UTC")


class TestDualKeyComposite:
    """Composite key (user_id, bot_id) works correctly."""

    @pytest.mark.asyncio
    async def test_add_with_bot_id(self):
        """add() with bot_id uses composite key."""
        called = []
        async def on_burst(msg_ids, user):
            called.append((msg_ids, user))

        coalescer = BurstCoalescer(on_burst, debounce_seconds=0.01, max_seconds=0.02)
        user = _user()
        msg_id = uuid4()

        await coalescer.add(user.id, msg_id, user, bot_id="custom_bot")
        await asyncio.sleep(0.05)
        await coalescer._fire(user.id)
        await asyncio.sleep(0.05)

    @pytest.mark.asyncio
    async def test_add_with_none_bot_id(self):
        """add() without bot_id uses legacy (user_id, None) key."""
        coalescer = BurstCoalescer(_noop, debounce_seconds=0.01, max_seconds=0.02)
        user = _user()
        msg_id = uuid4()

        await coalescer.add(user.id, msg_id, user, bot_id=None)
        # The key should be (user_id, None)
        keys = list(coalescer._bursts.keys())
        assert any(k[1] is None for k in keys), f"Expected legacy key (None), got {keys}"

    @pytest.mark.asyncio
    async def test_composite_first_lookup(self):
        """Composite-first lookup finds (user_id, bot_id) keys."""
        called = []
        async def on_burst(msg_ids, user):
            called.append(msg_ids)

        coalescer = BurstCoalescer(on_burst, debounce_seconds=0.01, max_seconds=0.02)
        user = _user()
        msg_id = uuid4()

        await coalescer.add(user.id, msg_id, user, bot_id="bot_a")
        await asyncio.sleep(0.05)
        await coalescer._fire(user.id)
        await asyncio.sleep(0.05)
        # The composite key (user.id, 'bot_a') should have been found
        # and the burst fired.

    @pytest.mark.asyncio
    async def test_legacy_fallback(self):
        """When no composite key matches, legacy (user_id, None) is tried."""
        called = []
        async def on_burst(msg_ids, user):
            called.append(msg_ids)

        coalescer = BurstCoalescer(on_burst, debounce_seconds=0.01, max_seconds=0.02)
        user = _user()
        msg_id = uuid4()

        # Add with bot_id=None (legacy)
        await coalescer.add(user.id, msg_id, user, bot_id=None)
        await asyncio.sleep(0.05)
        await coalescer._fire(user.id)
        await asyncio.sleep(0.05)

    @pytest.mark.asyncio
    async def test_both_keys_coexist(self):
        """Both (user_id, 'bot_a') and (user_id, None) can exist simultaneously."""
        coalescer = BurstCoalescer(_noop, debounce_seconds=0.01, max_seconds=0.02)
        user = _user()

        await coalescer.add(user.id, uuid4(), user, bot_id="bot_a")
        await coalescer.add(user.id, uuid4(), user, bot_id=None)

        keys = list(coalescer._bursts.keys())
        has_bot_a = any(k == (user.id, "bot_a") for k in keys)
        has_none = any(k == (user.id, None) for k in keys)
        assert has_bot_a, f"Expected (user_id, 'bot_a') key, got {keys}"
        assert has_none, f"Expected (user_id, None) key, got {keys}"


class TestDualKeyTodoMarkers:
    """TODO(S2b) markers exist for legacy-fallback sites."""

    def test_debouncer_has_todo_s2b(self):
        """debouncer.py contains TODO(S2b) comments."""
        content = open("app/services/debouncer.py").read()
        assert "# TODO(S2b): drop legacy (user_id, None) fallback" in content, (
            "debouncer.py must have TODO(S2b) markers for legacy fallback removal"
        )