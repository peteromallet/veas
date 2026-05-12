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
        self.distillations = []
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
        self.bridge_candidates = []
        self.bot_turns = []
        self.feedback = []

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
            "cross_thread_sharing_default": user.cross_thread_sharing_default,
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
        if "FROM distillations" in compact:
            return self.distillations
        if "FROM bridge_candidates" in compact:
            target_user_id, source_user_id = args
            return [
                row
                for row in self.bridge_candidates
                if row["target_user_id"] == target_user_id
                and row["source_user_id"] == source_user_id
                and row["status"] == "ready"
                and row.get("partner_path", "message_partner") == "message_partner"
            ][:5]
        if "FROM feedback" in compact:
            user_id, now_utc = args
            previous_completed_at = max(
                (
                    row["completed_at"]
                    for row in self.bot_turns
                    if row.get("user_in_context") == user_id and row.get("completed_at") is not None
                ),
                default=None,
            )
            if previous_completed_at is None:
                return []
            messages_by_id = {row["id"]: row for row in self.messages}
            rows = []
            for item in self.feedback:
                message = messages_by_id.get(item.get("target_id"))
                if message is None:
                    continue
                if item.get("from_user_id") != user_id:
                    continue
                if item.get("target_type") != "message" or item.get("source") != "reaction":
                    continue
                if message.get("direction") != "outbound" or message.get("recipient_id") != user_id:
                    continue
                if not previous_completed_at < item["created_at"] <= now_utc:
                    continue
                rows.append(
                    {
                        "id": item["id"],
                        "sentiment": item["sentiment"],
                        "content": item["content"],
                        "created_at": item["created_at"],
                        "message_id": message["id"],
                        "message_content": message["content"],
                        "message_sent_at": message["sent_at"],
                    }
                )
            rows.sort(key=lambda row: row["created_at"], reverse=True)
            return rows[:5]
        if "FROM messages" in compact and "WHERE id = ANY" in compact:
            ids = set(args[0])
            return [
                {
                    "id": row["id"],
                    "direction": row["direction"],
                    "sender_id": row["sender_id"],
                    "recipient_id": row["recipient_id"],
                    "charge": row["charge"],
                    "sent_at": row["sent_at"],
                    "content": row["content"],
                }
                for row in self.messages
                if row["id"] in ids
            ]
        if "FROM messages" in compact:
            if args and isinstance(args[0], list):
                allowed = set(args[0])
                rows = [
                    row
                    for row in self.messages
                    if row.get("sender_id") in allowed or row.get("recipient_id") in allowed
                ]
            else:
                user_id = args[0]
                rows = [
                    row
                    for row in self.messages
                    if row.get("sender_id") == user_id or row.get("recipient_id") == user_id
                ]
            return list(reversed(rows[-20:]))
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
    assert hc.recent_messages[-1]["sent_at_time"]["local_day_label"] == "today"
    assert "relative_to_now" in hc.recent_messages[-1]["sent_at_time"]
    assert any(item["direction"] == "outbound" for item in hc.recent_messages)
    assert hc.conversation_load["total_count"] == 25
    assert hc.conversation_load["inbound_count"] == 24
    assert hc.conversation_load["outbound_count"] == 1
    assert hc.recent_reactions == []
    assert hc.trigger_metadata["messages"][0]["charge"] == "charged"
    partner_rows = [item for item in hc.recent_messages if item["sender_id"] == partner.id or item["recipient_id"] == partner.id]
    assert any(item.get("raw_content_hidden") for item in partner_rows)


async def test_build_hot_context_surfaces_reactions_since_previous_turn(hot_context_seed):
    pool, user, partner = hot_context_seed
    outbound = next(row for row in pool.messages if row["direction"] == "outbound")
    outbound["sender_id"] = None
    outbound["recipient_id"] = user.id
    pool.bot_turns = [
        {
            "id": uuid4(),
            "user_in_context": user.id,
            "completed_at": pool.now - timedelta(minutes=10),
        }
    ]
    pool.feedback = [
        {
            "id": uuid4(),
            "from_user_id": user.id,
            "target_type": "message",
            "target_id": outbound["id"],
            "sentiment": "positive",
            "content": "👍",
            "source": "reaction",
            "created_at": pool.now - timedelta(minutes=3),
        },
        {
            "id": uuid4(),
            "from_user_id": user.id,
            "target_type": "message",
            "target_id": outbound["id"],
            "sentiment": "negative",
            "content": "👎",
            "source": "reaction",
            "created_at": pool.now - timedelta(minutes=20),
        },
    ]

    hc = await build_hot_context(pool, user, partner, [pool.trigger_id])
    text = render_hot_context(hc)

    assert len(hc.recent_reactions) == 1
    assert hc.recent_reactions[0]["content"] == "👍"
    assert hc.recent_reactions[0]["message_id"] == outbound["id"]
    assert "## New reactions since previous turn" in text
    assert "reaction=👍" in text
    assert "reaction=👎" not in text
    assert "passive feedback only" in text


async def test_build_hot_context_shows_partner_raw_when_partner_opted_in(hot_context_seed):
    pool, user, partner = hot_context_seed
    opted_in_partner = User(
        partner.id,
        partner.name,
        partner.phone,
        partner.timezone,
        cross_thread_sharing_default="opt_in",
    )

    hc = await build_hot_context(pool, user, opted_in_partner, [pool.trigger_id])

    partner_items = [
        item
        for item in hc.recent_messages
        if item["sender_id"] == partner.id or item["recipient_id"] == partner.id
    ]
    assert partner_items
    assert any(item["content"] for item in partner_items)
    assert not all(item.get("raw_content_hidden") for item in partner_items)


async def test_build_hot_context_distillation_privacy_gates_partner_sources(hot_context_seed):
    pool, user, partner = hot_context_seed
    pool.distillations = [
        {
            "id": uuid4(),
            "content": "Partner-only private synthesis",
            "shareable_summary": None,
            "confidence": "medium",
            "status": "active",
            "sensitivity": "medium",
            "visibility": "private",
            "source_user_ids": [partner.id],
            "related_memory_ids": [],
            "related_observation_ids": [uuid4()],
            "related_theme_ids": [],
            "supporting_message_ids": [],
            "revision_count": 0,
            "updated_at": pool.now,
            "created_at": pool.now,
        },
        {
            "id": uuid4(),
            "content": "Partner source full synthesis",
            "shareable_summary": "Safe reviewed summary",
            "confidence": "medium",
            "status": "active",
            "sensitivity": "medium",
            "visibility": "dyad_shareable",
            "source_user_ids": [partner.id],
            "related_memory_ids": [],
            "related_observation_ids": [uuid4()],
            "related_theme_ids": [],
            "supporting_message_ids": [],
            "revision_count": 0,
            "updated_at": pool.now,
            "created_at": pool.now,
        },
    ]

    hc = await build_hot_context(pool, user, partner, [pool.trigger_id])
    text = render_hot_context(hc)

    assert [item["display"] for item in hc.distillations] == ["shareable_summary"]
    assert "Safe reviewed summary" in text
    assert "Partner-only private synthesis" not in text
    assert "Partner source full synthesis" not in text


async def test_build_hot_context_distillation_full_content_when_sources_visible(hot_context_seed):
    pool, user, partner = hot_context_seed
    opted_in_partner = User(
        partner.id,
        partner.name,
        partner.phone,
        partner.timezone,
        cross_thread_sharing_default="opt_in",
    )
    pool.partner = opted_in_partner
    pool.distillations = [
        {
            "id": uuid4(),
            "content": "Partner source full synthesis",
            "shareable_summary": "Summary should not replace full content",
            "confidence": "medium",
            "status": "active",
            "sensitivity": "medium",
            "visibility": "dyad_shareable",
            "source_user_ids": [partner.id],
            "related_memory_ids": [],
            "related_observation_ids": [uuid4()],
            "related_theme_ids": [],
            "supporting_message_ids": [],
            "revision_count": 0,
            "updated_at": pool.now,
            "created_at": pool.now,
        }
    ]

    hc = await build_hot_context(pool, user, opted_in_partner, [pool.trigger_id])
    text = render_hot_context(hc)

    assert hc.distillations[0]["display"] == "full_content"
    assert "Partner source full synthesis" in text


def _bridge_candidate_row(pool, user, partner, *, status="ready", partner_path="message_partner", summary="Bridge summary"):
    return {
        "id": uuid4(),
        "source_user_id": partner.id,
        "target_user_id": user.id,
        "kind": "repair",
        "status": status,
        "sensitivity": "low",
        "partner_path": partner_path,
        "shareable_summary": summary,
        "created_at": pool.now,
    }


async def test_build_hot_context_surfaces_multiple_ready_message_partner_bridges(hot_context_seed):
    pool, user, partner = hot_context_seed
    pool.bridge_candidates = [
        _bridge_candidate_row(
            pool,
            user,
            partner,
            summary=f"Ready partner bridge {index}",
        )
        for index in range(6)
    ]

    hc = await build_hot_context(pool, user, partner, [pool.trigger_id])
    text = render_hot_context(hc)

    assert len(hc.bridge_candidates) == 5
    assert all(item["status"] == "ready" for item in hc.bridge_candidates)
    assert all(item["partner_path"] == "message_partner" for item in hc.bridge_candidates)
    assert "Ready partner bridge 0" in text
    assert "partner_path=message_partner" in text


async def test_build_hot_context_excludes_sent_addressed_and_non_message_partner_bridges(hot_context_seed):
    pool, user, partner = hot_context_seed
    visible = _bridge_candidate_row(pool, user, partner, summary="Visible ready message partner")
    sent = _bridge_candidate_row(pool, user, partner, status="sent", summary="Sent bridge should be absent")
    addressed = _bridge_candidate_row(
        pool,
        user,
        partner,
        status="addressed",
        summary="Addressed bridge should be absent",
    )
    hold = _bridge_candidate_row(
        pool,
        user,
        partner,
        partner_path="hold_for_context",
        summary="Held bridge should be absent",
    )
    pool.bridge_candidates = [visible, sent, addressed, hold]

    hc = await build_hot_context(pool, user, partner, [pool.trigger_id])
    text = render_hot_context(hc)

    assert [item["id"] for item in hc.bridge_candidates] == [visible["id"]]
    assert "Visible ready message partner" in text
    assert "Sent bridge should be absent" not in text
    assert "Addressed bridge should be absent" not in text
    assert "Held bridge should be absent" not in text


async def test_render_hot_context_respects_default_token_budget(hot_context_seed):
    pool, user, partner = hot_context_seed
    hc = await build_hot_context(pool, user, partner, [pool.trigger_id])

    text = render_hot_context(hc)

    assert len(text) // 4 <= get_settings().hot_context_token_budget
    assert "## You" in text
    assert "onboarding_state: welcomed" in text
    assert "## Conversation load" in text
    assert "## Sharing defaults" in text
    assert "## URGENT ACTION NEEDED" in text
    assert "Ask them to pick opt_in or opt_out" in text
    assert "total_messages: 25" in text
    assert "share carefully" in text
    assert "must stay private" not in text
    assert "core=" not in text
    assert "## Trigger" in text
    assert "; utc=" in text
    assert "one_month_from_now:" in text
    assert "[raw partner content hidden by sharing_default]" in text


def test_render_hot_context_truncates_without_dropping_oob(monkeypatch):
    monkeypatch.setenv("HOT_CONTEXT_TOKEN_BUDGET", "230")
    get_settings.cache_clear()
    user_id = uuid4()
    partner_id = uuid4()
    hc = HotContext(
        current_user={"id": user_id, "name": "Maya", "phone": "1", "timezone": "UTC", "style_notes": "short", "onboarding_state": "welcomed", "cross_thread_sharing_default": "opt_in"},
        partner_user={"id": partner_id, "name": "Ben", "phone": "2", "timezone": "UTC", "style_notes": "short", "onboarding_state": "pending", "cross_thread_sharing_default": "opt_in"},
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

    assert len(text) // 4 <= 230
    assert "shareable" in text
    assert "OOB MUST REMAIN" not in text
    assert "core=" not in text
    assert "[truncated, 6 more]" in text
    assert text.index("## High-significance observations") < text.index("## Recent messages")
    assert text.count("[truncated, 6 more]") == 3
    get_settings.cache_clear()


def test_render_hot_context_labels_voice_transcripts(monkeypatch):
    monkeypatch.setenv("HOT_CONTEXT_TOKEN_BUDGET", "2000")
    get_settings.cache_clear()
    user_id = uuid4()
    partner_id = uuid4()
    message_id = uuid4()
    hc = HotContext(
        current_user={"id": user_id, "name": "Maya", "phone": "1", "timezone": "UTC", "style_notes": "", "onboarding_state": "welcomed"},
        partner_user={"id": partner_id, "name": "Ben", "phone": "2", "timezone": "UTC", "style_notes": "", "onboarding_state": "pending"},
        conversation_load={"period": "today", "timezone": "UTC", "total_count": 1, "inbound_count": 1, "outbound_count": 0},
        active_oob=[],
        memories=[],
        active_themes=[],
        open_watch_items=[],
        observations=[],
        recent_messages=[
            {
                "id": message_id,
                "direction": "inbound",
                "sender_id": user_id,
                "recipient_id": None,
                "content": "Can you hear this? Or can you understand it?",
                "media_type": "voice",
                "media_duration_seconds": 7,
                "media_analysis": None,
                "sent_at": datetime.now(UTC).isoformat(),
                "charge": "routine",
            }
        ],
        time_since_last_message="5s",
        trigger_metadata={
            "triggering_message_ids": [message_id],
            "messages": [
                {
                    "id": message_id,
                    "charge": "routine",
                    "sent_at": datetime.now(UTC).isoformat(),
                    "content": "Can you hear this? Or can you understand it?",
                    "media_type": "voice",
                    "media_duration_seconds": 7,
                    "media_analysis": None,
                }
            ],
        },
    )

    text = render_hot_context(hc)

    assert "inbound charge=routine" in text
    assert "[voice transcript, 7s]: Can you hear this? Or can you understand it?" in text
    assert "trigger_message" in text
    assert "[voice transcript, 7s]: Can you hear this? Or can you understand it?" in text
    get_settings.cache_clear()


def test_render_hot_context_includes_current_time_for_relative_scheduling(monkeypatch):
    monkeypatch.setenv("HOT_CONTEXT_TOKEN_BUDGET", "2000")
    get_settings.cache_clear()
    user_id = uuid4()
    partner_id = uuid4()
    hc = HotContext(
        current_user={"id": user_id, "name": "Maya", "phone": "1", "timezone": "Europe/Berlin", "style_notes": "", "onboarding_state": "welcomed"},
        partner_user={"id": partner_id, "name": "Ben", "phone": "2", "timezone": "UTC", "style_notes": "", "onboarding_state": "pending"},
        temporal_context={
            "now_utc": "2026-05-06T10:00:00+00:00",
            "now_local": "2026-05-06T12:00:00+02:00",
            "timezone": "Europe/Berlin",
            "local_date": "2026-05-06",
            "local_time": "12:00:00",
            "local_weekday": "Wednesday",
            "local_day_start": "2026-05-06T00:00:00+02:00",
            "local_day_end": "2026-05-07T00:00:00+02:00",
            "local_day_start_utc": "2026-05-05T22:00:00+00:00",
            "local_day_end_utc": "2026-05-06T22:00:00+00:00",
        },
        conversation_load={"period": "today", "timezone": "Europe/Berlin", "total_count": 1, "inbound_count": 1, "outbound_count": 0},
        active_oob=[],
        memories=[],
        active_themes=[],
        open_watch_items=[],
        observations=[],
        recent_messages=[],
        time_since_last_message="5s",
        trigger_metadata={"triggering_message_ids": [uuid4()], "messages": []},
    )

    text = render_hot_context(hc)

    assert "## Current time" in text
    assert "now_utc: 2026-05-06T10:00:00+00:00" in text
    assert "now_local: 2026-05-06T12:00:00+02:00" in text
    assert "local_day_bounds: 2026-05-06T00:00:00+02:00 to 2026-05-07T00:00:00+02:00" in text
    assert "Default to scheduling tool delay fields" in text
    assert "Use local_when for concrete local clock phrases" in text
    get_settings.cache_clear()


def test_render_hot_context_uses_relative_local_message_time(monkeypatch):
    monkeypatch.setenv("HOT_CONTEXT_TOKEN_BUDGET", "2000")
    get_settings.cache_clear()
    user_id = uuid4()
    partner_id = uuid4()
    message_id = uuid4()
    hc = HotContext(
        current_user={"id": user_id, "name": "Maya", "phone": "1", "timezone": "Europe/Berlin", "style_notes": "", "onboarding_state": "welcomed"},
        partner_user={"id": partner_id, "name": "Ben", "phone": "2", "timezone": "Europe/Berlin", "style_notes": "", "onboarding_state": "pending"},
        conversation_load={"period": "today", "timezone": "Europe/Berlin", "period_start": "2026-05-06T00:00:00+02:00", "period_end": "2026-05-07T00:00:00+02:00", "total_count": 1, "inbound_count": 1, "outbound_count": 0},
        temporal_context={"local_day_start": "2026-05-06T00:00:00+02:00", "local_day_end": "2026-05-07T00:00:00+02:00"},
        active_oob=[],
        memories=[],
        active_themes=[],
        open_watch_items=[],
        observations=[],
        recent_messages=[
            {
                "id": message_id,
                "direction": "inbound",
                "sender_id": user_id,
                "recipient_id": partner_id,
                "content": "Yeah, I'm all good.",
                "sent_at": "2026-05-06T21:03:00+00:00",
                "sent_at_time": {
                    "utc": "2026-05-06T21:03:00+00:00",
                    "local": "2026-05-06T23:03:00+02:00",
                    "timezone": "Europe/Berlin",
                    "local_date": "2026-05-06",
                    "local_time": "23:03",
                    "local_weekday": "Wednesday",
                    "local_day_label": "today",
                    "relative_to_now": "about 4 minutes ago",
                    "display": "today 23:03 Berlin",
                },
                "charge": "routine",
            }
        ],
        time_since_last_message="4m",
        trigger_metadata={"triggering_message_ids": [message_id], "messages": []},
    )

    text = render_hot_context(hc)

    assert "today 23:03 Berlin (about 4 minutes ago; utc=2026-05-06T21:03:00+00:00) inbound" in text
    assert "local_period_bounds: 2026-05-06T00:00:00+02:00 to 2026-05-07T00:00:00+02:00" in text
    assert "utc_period_bounds: 2026-05-06T00:00:00+02:00 to 2026-05-07T00:00:00+02:00" in text
    get_settings.cache_clear()


def test_render_hot_context_does_not_clip_trigger_voice_transcript(monkeypatch):
    monkeypatch.setenv("HOT_CONTEXT_TOKEN_BUDGET", "2000")
    get_settings.cache_clear()
    user_id = uuid4()
    partner_id = uuid4()
    message_id = uuid4()
    transcript = (
        "So one thing I was thinking about, like we discussed before, kind of the plan around therapy. "
        + "middle " * 80
        + "The weekly thing helps us bring that into the real world, and the monthly therapist handles the harder stuff."
    )
    hc = HotContext(
        current_user={"id": user_id, "name": "Maya", "phone": "1", "timezone": "UTC", "style_notes": "", "onboarding_state": "welcomed"},
        partner_user={"id": partner_id, "name": "Ben", "phone": "2", "timezone": "UTC", "style_notes": "", "onboarding_state": "pending"},
        conversation_load={"period": "today", "timezone": "UTC", "total_count": 1, "inbound_count": 1, "outbound_count": 0},
        active_oob=[],
        memories=[],
        active_themes=[],
        open_watch_items=[],
        observations=[],
        recent_messages=[
            {
                "id": message_id,
                "direction": "inbound",
                "sender_id": user_id,
                "recipient_id": None,
                "content": transcript,
                "media_type": "voice",
                "media_duration_seconds": 119,
                "media_analysis": None,
                "sent_at": datetime.now(UTC).isoformat(),
                "charge": "routine",
            }
        ],
        time_since_last_message="5s",
        trigger_metadata={
            "triggering_message_ids": [message_id],
            "messages": [
                {
                    "id": message_id,
                    "charge": "routine",
                    "sent_at": datetime.now(UTC).isoformat(),
                    "content": transcript,
                    "media_type": "voice",
                    "media_duration_seconds": 119,
                    "media_analysis": None,
                }
            ],
        },
    )

    text = render_hot_context(hc)

    recent_line, trigger_line = [
        line for line in text.splitlines() if "[voice transcript, 119s]" in line
    ]
    assert "The weekly thing helps us bring that into the real world" in recent_line
    assert "The weekly thing helps us bring that into the real world" in trigger_line
    assert trigger_line.endswith("harder stuff.")
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
