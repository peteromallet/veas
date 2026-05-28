from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.config import get_settings
from app.bots.registry import get_relationship_topic_id
from app.models.user import User
from app.services.hot_context import HotContext, build_hot_context, render_hot_context

pytestmark = pytest.mark.anyio


class HotContextPool:
    def __init__(self, user: User, partner: User) -> None:
        self.user = user
        self.partner = partner
        self.now = datetime.now(UTC)
        self.trigger_id = uuid4()
        self.bot_id = "mediator"
        self.topic_id = get_relationship_topic_id()
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
                "sent_at": self.now - timedelta(seconds=25 - i),
                "charge": "routine",
                "bot_id": self.bot_id,
                "topic_id": self.topic_id,
            }
            for i in range(25)
        ]
        self.messages[-1]["id"] = self.trigger_id
        self.messages[-1]["charge"] = "charged"
        self.bridge_candidates = []
        self.bot_turns = []
        self.feedback = []
        self.user_bot_state = {}

    async def fetchrow(self, sql, *args):
        compact = " ".join(sql.split())
        if compact.startswith("SELECT status, partner_path, shareable_summary, target_user_id FROM bridge_candidates WHERE id="):
            # By-id fetch for partner_nudge bridge context resolution (T6)
            candidate_id = args[0]
            for row in self.bridge_candidates:
                if row["id"] == candidate_id:
                    return {
                        "status": row["status"],
                        "partner_path": row.get("partner_path", "message_partner"),
                        "shareable_summary": row.get("shareable_summary", ""),
                        "target_user_id": row["target_user_id"],
                    }
            return None
        if compact.startswith("SELECT display_name FROM bots WHERE id"):
            names = {"tante_rosi": "Tante Rosi", "mediator": "Véas"}
            return {"display_name": names.get(args[0], args[0])}
        if compact.startswith("WITH bounds AS"):
            user_id = args[0]
            bot_filter = args[2] if len(args) > 2 else None
            topic_filter = args[3] if len(args) > 3 else None
            period_start = self.now.replace(hour=0, minute=0, second=0, microsecond=0)
            period_end = period_start + timedelta(days=1)
            messages = [
                row
                for row in self.messages
                if row.get("deleted_at") is None
                and (
                    row.get("sender_id") == user_id
                    or row.get("recipient_id") == user_id
                )
                and period_start <= row["sent_at"] < period_end
                and (bot_filter is None or row.get("bot_id") == bot_filter)
                and (topic_filter is None or row.get("topic_id") == topic_filter)
            ]
            return {
                "period_start": period_start,
                "period_end": period_end,
                "inbound_count": sum(
                    1 for row in messages if row["direction"] == "inbound"
                ),
                "outbound_count": sum(
                    1 for row in messages if row["direction"] == "outbound"
                ),
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

    async def fetchval(self, sql, *args):
        compact = " ".join(sql.split())
        if compact.startswith("SELECT partner_share FROM user_bot_state"):
            return self.user_bot_state.get((args[0], args[1]), {}).get("partner_share")
        raise AssertionError(compact)

    async def fetch(self, sql, *args):
        compact = " ".join(sql.split())
        if "FROM out_of_bounds" in compact:
            return self.oob
        if "WITH partner_rows AS" in compact:
            owner_user_id = args[0]
            limit = args[1]
            current_bot_id = args[2]
            rows = []
            for row in self.memories:
                if (
                    row.get("status", "active") == "active"
                    and row.get("visibility") == "dyad_shareable"
                    and row.get("shareable_summary")
                    and row.get("about_user_id") == owner_user_id
                    and row.get("recorded_by_bot_id") is not None
                    and row.get("recorded_by_bot_id") != current_bot_id
                    and self.user_bot_state.get(
                        (owner_user_id, row.get("recorded_by_bot_id")), {}
                    ).get("partner_share")
                    == "opt_in"
                ):
                    rows.append(
                        {
                            "kind": "memory",
                            "id": row["id"],
                            "bot_id": row.get("recorded_by_bot_id"),
                            "shareable_summary": row.get("shareable_summary"),
                            "occurred_at": row.get("last_referenced_at")
                            or row.get("created_at"),
                        }
                    )
            messages_by_id = {row["id"]: row for row in self.messages}
            for row in self.distillations:
                bot_id = row.get("recorded_by_bot_id") or messages_by_id.get(
                    row.get("triggering_message_id"), {}
                ).get("bot_id")
                if (
                    row.get("status", "active") == "active"
                    and row.get("visibility") == "dyad_shareable"
                    and row.get("shareable_summary")
                    and owner_user_id in row.get("source_user_ids", [])
                    and bot_id is not None
                    and bot_id != current_bot_id
                    and self.user_bot_state.get((owner_user_id, bot_id), {}).get(
                        "partner_share"
                    )
                    == "opt_in"
                ):
                    rows.append(
                        {
                            "kind": "distillation",
                            "id": row["id"],
                            "bot_id": bot_id,
                            "shareable_summary": row.get("shareable_summary"),
                            "occurred_at": row.get("updated_at")
                            or row.get("created_at"),
                        }
                    )
            rows.sort(key=lambda row: row["occurred_at"], reverse=True)
            return rows[:limit]
        if "FROM memories" in compact:
            about_user_id = args[0] if args else None
            return [
                row
                for row in self.memories
                if about_user_id is None
                or row.get("about_user_id") == about_user_id
                or row.get("about_user_id") is None
            ]
        if "FROM themes" in compact:
            return self.themes[:10]
        if "FROM watch_items" in compact:
            return self.watch_items
        if "FROM observations" in compact:
            return [
                row
                for row in self.observations
                if row["significance"] is not None and row["significance"] >= 3
            ]
        if "FROM distillations" in compact:
            messages_by_id = {row["id"]: row for row in self.messages}
            return [
                {
                    **row,
                    "recorded_by_bot_id": row.get("recorded_by_bot_id"),
                    "visibility_bot_id": row.get("recorded_by_bot_id")
                    or messages_by_id.get(row.get("triggering_message_id"), {}).get(
                        "bot_id"
                    ),
                }
                for row in self.distillations
            ]
        if "FROM bridge_candidates" in compact and "status IN (" in compact:
            # Source-side outgoing query: source_user_id=$1, target_user_id=$2
            source_user_id, target_user_id = args
            rows = [
                row
                for row in self.bridge_candidates
                if row["source_user_id"] == source_user_id
                and row["target_user_id"] == target_user_id
                and row["status"] in ("pending", "ready", "blocked")
            ]
            rows.sort(
                key=lambda r: (r.get("created_at"), r.get("id")), reverse=True
            )
            return rows[:3]
        if "FROM bridge_candidates" in compact:
            # Target-side incoming query: target_user_id=$1, source_user_id=$2
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
            user_id, now_utc = args[:2]
            bot_filter = args[2] if len(args) > 2 else None
            topic_filter = args[3] if len(args) > 3 else None
            previous_completed_at = max(
                (
                    row["completed_at"]
                    for row in self.bot_turns
                    if row.get("user_in_context") == user_id
                    and row.get("completed_at") is not None
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
                if (
                    item.get("target_type") != "message"
                    or item.get("source") != "reaction"
                ):
                    continue
                if (
                    message.get("direction") != "outbound"
                    or message.get("recipient_id") != user_id
                ):
                    continue
                if bot_filter is not None and message.get("bot_id") != bot_filter:
                    continue
                if topic_filter is not None and message.get("topic_id") != topic_filter:
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
        if "FROM bot_turns bt" in compact and "final_output_message_id IS NULL" in compact:
            # Silent-turns hot-context block. The real query joins
            # tool_calls; the fake pool doesn't track silent turns, so
            # default to no silent turns. Tests that exercise the block
            # should construct rows directly.
            return []
        if "FROM messages" in compact and "WHERE id = ANY" in compact:
            ids = set(args[0])
            bot_filter = args[1] if len(args) > 1 else None
            topic_filter = args[2] if len(args) > 2 else None
            return [
                {
                    "id": row["id"],
                    "direction": row["direction"],
                    "sender_id": row["sender_id"],
                    "recipient_id": row["recipient_id"],
                    "charge": row["charge"],
                    "sent_at": row["sent_at"],
                    "content": row["content"],
                    "bot_id": row.get("bot_id"),
                    "topic_id": row.get("topic_id"),
                }
                for row in self.messages
                if row["id"] in ids
                and (bot_filter is None or row.get("bot_id") == bot_filter)
                and (topic_filter is None or row.get("topic_id") == topic_filter)
            ]
        if "FROM messages" in compact:
            if args and isinstance(args[0], list):
                allowed = set(args[0])
                bot_filter = args[1] if len(args) > 1 else None
                topic_filter = args[2] if len(args) > 2 else None
                rows = [
                    row
                    for row in self.messages
                    if row.get("sender_id") in allowed
                    or row.get("recipient_id") in allowed
                ]
            else:
                user_id = args[0]
                bot_filter = None
                topic_filter = None
                rows = [
                    row
                    for row in self.messages
                    if row.get("sender_id") == user_id
                    or row.get("recipient_id") == user_id
                ]
            scoped_rows = [
                row
                for row in rows
                if (bot_filter is None or row.get("bot_id") == bot_filter)
                and (topic_filter is None or row.get("topic_id") == topic_filter)
            ]
            return list(reversed(scoped_rows[-20:]))
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
    assert hc.current_user["partner_sharing_state"] == "pending"
    assert hc.partner_user["partner_sharing_state"] == "pending"
    assert hc.recent_messages[-1]["sent_at_time"]["local_day_label"] == "today"
    assert "relative_to_now" in hc.recent_messages[-1]["sent_at_time"]
    assert any(item["direction"] == "outbound" for item in hc.recent_messages)
    assert hc.conversation_load["total_count"] == 25
    assert hc.conversation_load["inbound_count"] == 24
    assert hc.conversation_load["outbound_count"] == 1
    assert hc.recent_reactions == []
    assert hc.trigger_metadata["messages"][0]["charge"] == "charged"
    partner_rows = [
        item
        for item in hc.recent_messages
        if item["sender_id"] == partner.id or item["recipient_id"] == partner.id
    ]
    assert any(item.get("raw_content_hidden") for item in partner_rows)


async def test_build_hot_context_surfaces_reactions_since_previous_turn(
    hot_context_seed,
):
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


async def test_build_hot_context_shows_partner_raw_when_partner_opted_in(
    hot_context_seed,
):
    pool, user, partner = hot_context_seed
    pool.user_bot_state[(partner.id, pool.bot_id)] = {"partner_share": "opt_in"}

    hc = await build_hot_context(pool, user, partner, [pool.trigger_id])

    partner_items = [
        item
        for item in hc.recent_messages
        if item["sender_id"] == partner.id or item["recipient_id"] == partner.id
    ]
    assert partner_items
    assert any(item["content"] for item in partner_items)
    assert not all(item.get("raw_content_hidden") for item in partner_items)


async def test_build_hot_context_filters_cross_bot_and_legacy_raw_messages(
    hot_context_seed,
):
    pool, user, partner = hot_context_seed
    pool.user_bot_state[(partner.id, pool.bot_id)] = {"partner_share": "opt_in"}
    pool.messages.extend(
        [
            {
                "id": uuid4(),
                "direction": "inbound",
                "sender_id": partner.id,
                "recipient_id": user.id,
                "content": "rosi raw should stay out",
                "sent_at": pool.now + timedelta(minutes=1),
                "charge": "routine",
                "bot_id": "tante_rosi",
                "topic_id": pool.topic_id,
            },
            {
                "id": uuid4(),
                "direction": "inbound",
                "sender_id": partner.id,
                "recipient_id": user.id,
                "content": "legacy raw should stay out",
                "sent_at": pool.now + timedelta(minutes=2),
                "charge": "routine",
                "bot_id": None,
                "topic_id": pool.topic_id,
            },
        ]
    )

    hc = await build_hot_context(pool, user, partner, [pool.trigger_id])
    text = render_hot_context(hc)

    assert "rosi raw should stay out" not in text
    assert "legacy raw should stay out" not in text


async def test_build_hot_context_distillation_privacy_gates_partner_sources(
    hot_context_seed,
):
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
            "recorded_by_bot_id": pool.bot_id,
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
            "recorded_by_bot_id": pool.bot_id,
        },
    ]

    hc = await build_hot_context(pool, user, partner, [pool.trigger_id])
    text = render_hot_context(hc)

    assert hc.distillations == []
    assert "Safe reviewed summary" not in text
    assert "Partner-only private synthesis" not in text
    assert "Partner source full synthesis" not in text


async def test_build_hot_context_distillation_full_content_when_sources_visible(
    hot_context_seed,
):
    pool, user, partner = hot_context_seed
    pool.user_bot_state[(partner.id, pool.bot_id)] = {"partner_share": "opt_in"}
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
            "recorded_by_bot_id": pool.bot_id,
        }
    ]

    hc = await build_hot_context(pool, user, partner, [pool.trigger_id])
    text = render_hot_context(hc)

    assert hc.distillations[0]["display"] == "shareable_summary"
    assert "Summary should not replace full content" in text
    assert "Partner source full synthesis" not in text


async def test_build_hot_context_pulls_opted_in_cross_bot_summaries_with_cap(
    hot_context_seed,
):
    pool, user, partner = hot_context_seed
    pool.user_bot_state[(partner.id, "tante_rosi")] = {
        "partner_share": "opt_in",
    }
    pool.user_bot_state[(partner.id, "coach")] = {
        "partner_share": "opt_out",
    }
    pool.memories.extend(
        [
            {
                "id": uuid4(),
                "about_user_id": partner.id,
                "content": "private pregnancy detail should stay hidden",
                "shareable_summary": f"Rosi memory summary {index}",
                "visibility": "dyad_shareable",
                "status": "active",
                "recorded_by_bot_id": "tante_rosi",
                "related_theme_ids": [],
                "last_referenced_at": None,
                "created_at": pool.now + timedelta(minutes=index),
            }
            for index in range(14)
        ]
    )
    pool.memories.append(
        {
            "id": uuid4(),
            "about_user_id": partner.id,
            "content": "coach private detail should stay hidden",
            "shareable_summary": "Coach summary should be gated out",
            "visibility": "dyad_shareable",
            "status": "active",
            "recorded_by_bot_id": "coach",
            "related_theme_ids": [],
            "last_referenced_at": None,
            "created_at": pool.now + timedelta(hours=1),
        }
    )
    pool.distillations.append(
        {
            "id": uuid4(),
            "content": "Rosi distillation private content should stay hidden",
            "shareable_summary": "Rosi distillation summary",
            "confidence": "medium",
            "status": "active",
            "sensitivity": "low",
            "visibility": "dyad_shareable",
            "source_user_ids": [partner.id],
            "related_memory_ids": [],
            "related_observation_ids": [],
            "related_theme_ids": [],
            "supporting_message_ids": [],
            "revision_count": 0,
            "updated_at": pool.now + timedelta(hours=2),
            "created_at": pool.now + timedelta(hours=2),
            "recorded_by_bot_id": "tante_rosi",
        }
    )

    hc = await build_hot_context(pool, user, partner, [pool.trigger_id])
    text = render_hot_context(hc)

    assert len(hc.partner_shareable_summaries) == 12
    assert hc.partner_shareable_summaries[0]["shareable_summary"] == (
        "Rosi distillation summary"
    )
    assert all(
        item["provenance"] == "from Tante Rosi:"
        for item in hc.partner_shareable_summaries
    )
    assert "## Partner shareable summaries" in text
    assert "from Tante Rosi:" in text
    assert "Rosi distillation summary" in text
    assert "Rosi distillation private content should stay hidden" not in text
    assert "private pregnancy detail should stay hidden" not in text
    assert "Coach summary should be gated out" not in text


async def test_cross_bot_summaries_filter_opt_in_before_global_cap(
    hot_context_seed,
):
    pool, user, partner = hot_context_seed
    pool.user_bot_state[(partner.id, "tante_rosi")] = {
        "partner_share": "opt_in",
    }
    pool.user_bot_state[(partner.id, "coach")] = {
        "partner_share": "opt_out",
    }
    pool.memories.extend(
        [
            {
                "id": uuid4(),
                "about_user_id": partner.id,
                "content": "newer coach content should stay hidden",
                "shareable_summary": f"Newer coach summary {index}",
                "visibility": "dyad_shareable",
                "status": "active",
                "recorded_by_bot_id": "coach",
                "related_theme_ids": [],
                "last_referenced_at": None,
                "created_at": pool.now + timedelta(hours=2, minutes=index),
            }
            for index in range(60)
        ]
    )
    pool.memories.append(
        {
            "id": uuid4(),
            "about_user_id": partner.id,
            "content": "older Rosi full content should stay hidden",
            "shareable_summary": "Older opted-in Rosi summary",
            "visibility": "dyad_shareable",
            "status": "active",
            "recorded_by_bot_id": "tante_rosi",
            "related_theme_ids": [],
            "last_referenced_at": None,
            "created_at": pool.now,
        }
    )

    hc = await build_hot_context(pool, user, partner, [pool.trigger_id])
    text = render_hot_context(hc)

    assert [item["shareable_summary"] for item in hc.partner_shareable_summaries] == [
        "Older opted-in Rosi summary"
    ]
    assert "Older opted-in Rosi summary" in text
    assert "older Rosi full content should stay hidden" not in text
    assert "Newer coach summary" not in text


def _bridge_candidate_row(
    pool,
    user,
    partner,
    *,
    status="ready",
    partner_path="message_partner",
    summary="Bridge summary",
):
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


async def test_build_hot_context_surfaces_multiple_ready_message_partner_bridges(
    hot_context_seed,
):
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
    assert all(
        item["partner_path"] == "message_partner" for item in hc.bridge_candidates
    )
    assert "Ready partner bridge 0" in text
    assert "partner_path=message_partner" in text


async def test_build_hot_context_excludes_sent_addressed_and_non_message_partner_bridges(
    hot_context_seed,
):
    pool, user, partner = hot_context_seed
    visible = _bridge_candidate_row(
        pool, user, partner, summary="Visible ready message partner"
    )
    sent = _bridge_candidate_row(
        pool, user, partner, status="sent", summary="Sent bridge should be absent"
    )
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
    assert "## Partner sharing" in text
    assert "partner_sharing_state: pending" in text
    assert "## URGENT ACTION NEEDED" not in text
    assert "Ask them to pick opt_in or opt_out" not in text
    assert "total_messages: 25" in text
    assert "share carefully" in text
    assert "must stay private" not in text
    assert "core=" not in text
    assert "## Trigger" in text
    assert "; utc=" in text
    assert "one_month_from_now:" in text
    assert "[raw partner content hidden by partner_share]" in text


def test_render_hot_context_truncates_without_dropping_oob(monkeypatch):
    monkeypatch.setenv("HOT_CONTEXT_TOKEN_BUDGET", "260")
    get_settings.cache_clear()
    user_id = uuid4()
    partner_id = uuid4()
    hc = HotContext(
        current_user={
            "id": user_id,
            "name": "Maya",
            "phone": "1",
            "timezone": "UTC",
            "style_notes": "short",
            "onboarding_state": "welcomed",
            "partner_share": "opt_in",
            "partner_sharing_state": "opt_in",
        },
        partner_user={
            "id": partner_id,
            "name": "Ben",
            "phone": "2",
            "timezone": "UTC",
            "style_notes": "short",
            "onboarding_state": "pending",
            "partner_share": "opt_in",
            "partner_sharing_state": "opt_in",
        },
        conversation_load={
            "period": "today",
            "timezone": "UTC",
            "total_count": 24,
            "inbound_count": 13,
            "outbound_count": 11,
        },
        active_oob=[
            {
                "id": uuid4(),
                "owner_id": partner_id,
                "severity": "hard",
                "shareable_context": "shareable",
                "protected_summary": "shareable",
            }
        ],
        memories=[
            {"id": uuid4(), "about_user_id": user_id, "content": "memory " + "m" * 300}
            for _ in range(6)
        ],
        active_themes=[],
        open_watch_items=[],
        observations=[
            {
                "id": uuid4(),
                "about_user_id": user_id,
                "content": "observation " + "o" * 300,
                "confidence": "medium",
                "significance": 3,
            }
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

    assert len(text) // 4 <= 260
    assert "shareable" in text
    assert "OOB MUST REMAIN" not in text
    assert "core=" not in text
    assert "[truncated, 6 more]" in text
    assert text.index("## High-significance observations") < text.index(
        "## Recent messages"
    )
    assert text.count("[truncated, 6 more]") == 3
    get_settings.cache_clear()


def test_render_hot_context_labels_voice_transcripts(monkeypatch):
    monkeypatch.setenv("HOT_CONTEXT_TOKEN_BUDGET", "2000")
    get_settings.cache_clear()
    user_id = uuid4()
    partner_id = uuid4()
    message_id = uuid4()
    hc = HotContext(
        current_user={
            "id": user_id,
            "name": "Maya",
            "phone": "1",
            "timezone": "UTC",
            "style_notes": "",
            "onboarding_state": "welcomed",
        },
        partner_user={
            "id": partner_id,
            "name": "Ben",
            "phone": "2",
            "timezone": "UTC",
            "style_notes": "",
            "onboarding_state": "pending",
        },
        conversation_load={
            "period": "today",
            "timezone": "UTC",
            "total_count": 1,
            "inbound_count": 1,
            "outbound_count": 0,
        },
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
    assert (
        "[voice transcript, 7s]: Can you hear this? Or can you understand it?" in text
    )
    assert "trigger_message" in text
    assert (
        "[voice transcript, 7s]: Can you hear this? Or can you understand it?" in text
    )
    get_settings.cache_clear()


def test_render_hot_context_includes_current_time_for_relative_scheduling(monkeypatch):
    monkeypatch.setenv("HOT_CONTEXT_TOKEN_BUDGET", "2000")
    get_settings.cache_clear()
    user_id = uuid4()
    partner_id = uuid4()
    hc = HotContext(
        current_user={
            "id": user_id,
            "name": "Maya",
            "phone": "1",
            "timezone": "Europe/Berlin",
            "style_notes": "",
            "onboarding_state": "welcomed",
        },
        partner_user={
            "id": partner_id,
            "name": "Ben",
            "phone": "2",
            "timezone": "UTC",
            "style_notes": "",
            "onboarding_state": "pending",
        },
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
        conversation_load={
            "period": "today",
            "timezone": "Europe/Berlin",
            "total_count": 1,
            "inbound_count": 1,
            "outbound_count": 0,
        },
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
    assert (
        "local_day_bounds: 2026-05-06T00:00:00+02:00 to 2026-05-07T00:00:00+02:00"
        in text
    )
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
        current_user={
            "id": user_id,
            "name": "Maya",
            "phone": "1",
            "timezone": "Europe/Berlin",
            "style_notes": "",
            "onboarding_state": "welcomed",
        },
        partner_user={
            "id": partner_id,
            "name": "Ben",
            "phone": "2",
            "timezone": "Europe/Berlin",
            "style_notes": "",
            "onboarding_state": "pending",
        },
        conversation_load={
            "period": "today",
            "timezone": "Europe/Berlin",
            "period_start": "2026-05-06T00:00:00+02:00",
            "period_end": "2026-05-07T00:00:00+02:00",
            "total_count": 1,
            "inbound_count": 1,
            "outbound_count": 0,
        },
        temporal_context={
            "local_day_start": "2026-05-06T00:00:00+02:00",
            "local_day_end": "2026-05-07T00:00:00+02:00",
        },
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

    assert (
        "today 23:03 Berlin (about 4 minutes ago; utc=2026-05-06T21:03:00+00:00) inbound"
        in text
    )
    assert (
        "local_period_bounds: 2026-05-06T00:00:00+02:00 to 2026-05-07T00:00:00+02:00"
        in text
    )
    assert (
        "utc_period_bounds: 2026-05-06T00:00:00+02:00 to 2026-05-07T00:00:00+02:00"
        in text
    )
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
        current_user={
            "id": user_id,
            "name": "Maya",
            "phone": "1",
            "timezone": "UTC",
            "style_notes": "",
            "onboarding_state": "welcomed",
        },
        partner_user={
            "id": partner_id,
            "name": "Ben",
            "phone": "2",
            "timezone": "UTC",
            "style_notes": "",
            "onboarding_state": "pending",
        },
        conversation_load={
            "period": "today",
            "timezone": "UTC",
            "total_count": 1,
            "inbound_count": 1,
            "outbound_count": 0,
        },
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
        current_user={
            "id": user_id,
            "name": "Maya",
            "phone": "1",
            "timezone": "UTC",
            "style_notes": "",
            "onboarding_state": "welcomed",
        },
        partner_user={
            "id": partner_id,
            "name": "Ben",
            "phone": "2",
            "timezone": "UTC",
            "style_notes": "",
            "onboarding_state": "pending",
        },
        conversation_load={
            "period": "today",
            "timezone": "UTC",
            "total_count": 1,
            "inbound_count": 0,
            "outbound_count": 1,
        },
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


def _outgoing_bridge_row(pool, user, partner, *, status="ready", summary="Outgoing issue", created_at=None):
    return {
        "id": uuid4(),
        "source_user_id": user.id,
        "target_user_id": partner.id,
        "kind": "repair",
        "status": status,
        "sensitivity": "low",
        "partner_path": "message_partner",
        "shareable_summary": summary,
        "created_at": created_at if created_at is not None else pool.now,
    }


async def test_outgoing_mediated_issues_render_for_unresolved_statuses(hot_context_seed):
    pool, user, partner = hot_context_seed
    pool.bridge_candidates = [
        _outgoing_bridge_row(pool, user, partner, status="pending", summary="Pending issue"),
        _outgoing_bridge_row(pool, user, partner, status="ready", summary="Ready issue"),
        _outgoing_bridge_row(pool, user, partner, status="blocked", summary="Blocked issue"),
    ]

    hc = await build_hot_context(pool, user, partner, [pool.trigger_id])
    text = render_hot_context(hc)

    assert len(hc.outgoing_mediated_issues) == 3
    assert "## Outgoing mediated issues" in text
    assert "Pending issue" in text
    assert "Ready issue" in text
    assert "Blocked issue" in text


async def test_outgoing_mediated_issues_excludes_terminal_statuses(hot_context_seed):
    pool, user, partner = hot_context_seed
    pool.bridge_candidates = [
        _outgoing_bridge_row(pool, user, partner, status="sent", summary="Sent should be absent"),
        _outgoing_bridge_row(pool, user, partner, status="addressed", summary="Addressed should be absent"),
        _outgoing_bridge_row(pool, user, partner, status="declined", summary="Declined should be absent"),
        _outgoing_bridge_row(pool, user, partner, status="expired", summary="Expired should be absent"),
    ]

    hc = await build_hot_context(pool, user, partner, [pool.trigger_id])
    text = render_hot_context(hc)

    assert len(hc.outgoing_mediated_issues) == 0
    assert "Sent should be absent" not in text
    assert "Addressed should be absent" not in text
    assert "Declined should be absent" not in text
    assert "Expired should be absent" not in text


async def test_outgoing_mediated_issues_cap_and_order(hot_context_seed):
    pool, user, partner = hot_context_seed
    base = pool.now
    pool.bridge_candidates = [
        _outgoing_bridge_row(pool, user, partner, status="ready", summary="Oldest", created_at=base - timedelta(hours=3)),
        _outgoing_bridge_row(pool, user, partner, status="ready", summary="Middle", created_at=base - timedelta(hours=2)),
        _outgoing_bridge_row(pool, user, partner, status="ready", summary="Second newest", created_at=base - timedelta(hours=1)),
        _outgoing_bridge_row(pool, user, partner, status="ready", summary="Newest", created_at=base),
    ]

    hc = await build_hot_context(pool, user, partner, [pool.trigger_id])

    assert len(hc.outgoing_mediated_issues) == 3
    summaries = [item["shareable_summary"] for item in hc.outgoing_mediated_issues]
    assert summaries[0] == "Newest"
    assert summaries[1] == "Second newest"
    assert summaries[2] == "Middle"
    assert "Oldest" not in summaries
