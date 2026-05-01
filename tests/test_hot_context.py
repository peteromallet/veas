from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.config import get_settings
from app.models.user import User
from app.services.hot_context import HotContext, build_hot_context, render_hot_context

pytestmark = pytest.mark.anyio


class HotContextPool:
    def __init__(self, user: User, partner: User) -> None:
        self.user = user
        self.partner = partner
        self.now = datetime.now(UTC)
        self.trigger_id = uuid4()
        self.oob = [
            {
                "id": uuid4(),
                "owner_id": partner.id,
                "sensitive_core": "must stay private",
                "shareable_context": "share carefully",
                "severity": "hard",
                "review_at": None,
            }
        ]
        self.memories = [
            {
                "id": uuid4(),
                "about_user_id": user.id,
                "content": f"memory {i}",
                "related_theme_ids": [],
                "last_referenced_at": None,
                "created_at": self.now - timedelta(days=i),
            }
            for i in range(3)
        ]
        self.themes = [
            {
                "id": uuid4(),
                "title": f"Theme {i}",
                "description": f"status line {i}",
                "status": "active",
                "sentiment": "mixed",
                "health": "tender",
                "last_reinforced_at": self.now - timedelta(hours=i),
                "last_active_at": self.now - timedelta(hours=i),
            }
            for i in range(12)
        ]
        self.watch_items = [
            {
                "id": uuid4(),
                "owner_user_id": user.id,
                "content": "watch the upcoming talk",
                "due_at": self.now + timedelta(days=1),
                "related_theme_ids": [],
            }
        ]
        self.observations = [
            {
                "id": uuid4(),
                "about_user_id": user.id,
                "content": "significant observation",
                "confidence": "medium",
                "significance": 3,
                "related_theme_ids": [],
                "last_reinforced_at": None,
                "created_at": self.now,
            },
            {
                "id": uuid4(),
                "about_user_id": user.id,
                "content": "null significance should not outrank scored rows",
                "confidence": "medium",
                "significance": None,
                "related_theme_ids": [],
                "last_reinforced_at": None,
                "created_at": self.now + timedelta(days=1),
            },
            {
                "id": uuid4(),
                "about_user_id": partner.id,
                "content": "low observation should be filtered by fake query",
                "confidence": "low",
                "significance": 2,
                "related_theme_ids": [],
                "last_reinforced_at": None,
                "created_at": self.now,
            },
        ]
        self.messages = [
            {
                "id": uuid4(),
                "direction": "outbound" if i == 10 else "inbound",
                "sender_id": user.id if i % 2 == 0 else partner.id,
                "recipient_id": partner.id if i % 2 == 0 else user.id,
                "content": f"message {i}",
                "sent_at": self.now - timedelta(minutes=25 - i),
                "charge": "routine",
            }
            for i in range(25)
        ]
        self.messages[-1]["id"] = self.trigger_id
        self.messages[-1]["charge"] = "charged"

    async def fetchrow(self, sql, *args):
        compact = " ".join(sql.split())
        if compact.startswith("WITH bounds AS"):
            user_id = args[0]
            period_start = self.now.replace(hour=0, minute=0, second=0, microsecond=0)
            period_end = period_start + timedelta(days=1)
            messages = [
                row
                for row in self.messages
                if row.get("deleted_at") is None
                and (row.get("sender_id") == user_id or row.get("recipient_id") == user_id)
                and period_start <= row["sent_at"] < period_end
            ]
            return {
                "period_start": period_start,
                "period_end": period_end,
                "inbound_count": sum(1 for row in messages if row["direction"] == "inbound"),
                "outbound_count": sum(1 for row in messages if row["direction"] == "outbound"),
                "total_count": len(messages),
            }
        user_id = args[0]
        user = self.user if user_id == self.user.id else self.partner
        return {
            "id": user.id,
            "name": user.name,
            "phone": user.phone,
            "timezone": user.timezone,
            "style_notes": f"{user.name} style",
            "onboarding_state": "welcomed" if user_id == self.user.id else "pending",
        }

    async def fetch(self, sql, *args):
        compact = " ".join(sql.split())
        if "FROM out_of_bounds" in compact:
            return self.oob
        if "FROM memories" in compact:
            return self.memories
        if "FROM themes" in compact:
            return self.themes[:10]
        if "FROM watch_items" in compact:
            return self.watch_items
        if "FROM observations" in compact:
            return [row for row in self.observations if row["significance"] is not None and row["significance"] >= 3]
        if "FROM messages" in compact and "WHERE id = ANY" in compact:
            ids = set(args[0])
            return [
                {"id": row["id"], "charge": row["charge"], "sent_at": row["sent_at"]}
                for row in self.messages
                if row["id"] in ids
            ]
        if "FROM messages" in compact:
            return list(reversed(self.messages[-20:]))
        raise AssertionError(compact)


@pytest.fixture
def hot_context_seed():
    user = User(uuid4(), "Maya", "15555550100", "UTC")
    partner = User(uuid4(), "Ben", "15555550101", "UTC")
    pool = HotContextPool(user, partner)
    return pool, user, partner


async def test_build_hot_context_returns_expected_fields(hot_context_seed):
    pool, user, partner = hot_context_seed

    hc = await build_hot_context(pool, user, partner, [pool.trigger_id])

    assert hc.current_user["id"] == user.id
    assert hc.current_user["onboarding_state"] == "welcomed"
    assert hc.partner_user["onboarding_state"] == "pending"
    assert hc.partner_user["id"] == partner.id
    assert hc.active_oob[0]["severity"] == "hard"
    assert hc.active_oob[0]["protected_summary"] == "share carefully"
    assert "sensitive_core" not in hc.active_oob[0]
    assert len(hc.active_themes) == 10
    assert hc.open_watch_items[0]["owner_user_id"] == user.id
    assert [item["significance"] for item in hc.observations] == [3]
    assert all(item["significance"] is not None for item in hc.observations)
    assert len(hc.recent_messages) == 20
    assert any(item["direction"] == "outbound" for item in hc.recent_messages)
    assert hc.conversation_load["total_count"] == 25
    assert hc.conversation_load["inbound_count"] == 24
    assert hc.conversation_load["outbound_count"] == 1
    assert hc.trigger_metadata["messages"][0]["charge"] == "charged"


async def test_render_hot_context_respects_default_token_budget(hot_context_seed):
    pool, user, partner = hot_context_seed
    hc = await build_hot_context(pool, user, partner, [pool.trigger_id])

    text = render_hot_context(hc)

    assert len(text) // 4 <= get_settings().hot_context_token_budget
    assert "## You" in text
    assert "onboarding_state: welcomed" in text
    assert "## Conversation load" in text
    assert "total_messages: 25" in text
    assert "share carefully" in text
    assert "must stay private" not in text
    assert "core=" not in text
    assert "## Trigger" in text


def test_render_hot_context_truncates_without_dropping_oob(monkeypatch):
    monkeypatch.setenv("HOT_CONTEXT_TOKEN_BUDGET", "170")
    get_settings.cache_clear()
    user_id = uuid4()
    partner_id = uuid4()
    hc = HotContext(
        current_user={"id": user_id, "name": "Maya", "phone": "1", "timezone": "UTC", "style_notes": "short", "onboarding_state": "welcomed"},
        partner_user={"id": partner_id, "name": "Ben", "phone": "2", "timezone": "UTC", "style_notes": "short", "onboarding_state": "pending"},
        conversation_load={"period": "today", "timezone": "UTC", "total_count": 24, "inbound_count": 13, "outbound_count": 11},
        active_oob=[
            {
                "id": uuid4(),
                "owner_id": partner_id,
                "severity": "hard",
                "shareable_context": "shareable",
                "protected_summary": "shareable",
            }
        ],
        memories=[{"id": uuid4(), "about_user_id": user_id, "content": "memory " + "m" * 300} for _ in range(6)],
        active_themes=[],
        open_watch_items=[],
        observations=[
            {"id": uuid4(), "about_user_id": user_id, "content": "observation " + "o" * 300, "confidence": "medium", "significance": 3}
            for _ in range(6)
        ],
        recent_messages=[
            {
                "id": uuid4(),
                "direction": "inbound",
                "sender_id": user_id,
                "recipient_id": partner_id,
                "content": "message " + "r" * 300,
                "sent_at": datetime.now(UTC).isoformat(),
                "charge": "routine",
            }
            for _ in range(6)
        ],
        time_since_last_message="1m",
        trigger_metadata={"triggering_message_ids": [uuid4()], "messages": []},
    )

    text = render_hot_context(hc)

    assert len(text) // 4 <= 170
    assert "shareable" in text
    assert "OOB MUST REMAIN" not in text
    assert "core=" not in text
    assert "[truncated, 6 more]" in text
    assert text.index("## High-significance observations") < text.index("## Recent messages")
    assert text.count("[truncated, 6 more]") == 3
    get_settings.cache_clear()


def test_render_hot_context_scrubs_internal_leaks_from_outbound_history(monkeypatch):
    monkeypatch.setenv("HOT_CONTEXT_TOKEN_BUDGET", "2000")
    get_settings.cache_clear()
    user_id = uuid4()
    partner_id = uuid4()
    hc = HotContext(
        current_user={"id": user_id, "name": "Maya", "phone": "1", "timezone": "UTC", "style_notes": "", "onboarding_state": "welcomed"},
        partner_user={"id": partner_id, "name": "Ben", "phone": "2", "timezone": "UTC", "style_notes": "", "onboarding_state": "pending"},
        conversation_load={"period": "today", "timezone": "UTC", "total_count": 1, "inbound_count": 0, "outbound_count": 1},
        active_oob=[],
        memories=[],
        active_themes=[],
        open_watch_items=[],
        observations=[],
        recent_messages=[
            {
                "id": uuid4(),
                "direction": "outbound",
                "sender_id": None,
                "recipient_id": user_id,
                "content": "The person's message is rich. No new tools needed; I have enough context.\n\n---\n\nThat's the real reply.",
                "sent_at": datetime.now(UTC).isoformat(),
                "charge": "routine",
            }
        ],
        time_since_last_message="1m",
        trigger_metadata={"triggering_message_ids": [uuid4()], "messages": []},
    )

    text = render_hot_context(hc)

    assert "No new tools needed" not in text
    assert "The person's message is rich" not in text
    assert "That's the real reply." in text
    get_settings.cache_clear()
