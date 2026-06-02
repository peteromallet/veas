from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.config import get_settings
from app.bots.registry import get_relationship_topic_id
from app.models.user import User
from app.services.hot_context import HotContext, build_hot_context, render_hot_context
from app.services.retrieval import RetrievalResult

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
        self.conversation_notes = {}
        self.conversation_artifacts = {}
        self.bridge_candidates = []
        self.bot_turns = []
        self.feedback = []
        self.user_bot_state = {}

    async def fetchrow(self, sql, *args):
        compact = " ".join(sql.split())
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
        if "WITH ranked_sources AS" in compact and "JOIN mediator.v_searchable_content sc" in compact:
            source_types = args[-2]
            source_ids = args[-1]
            rows = []
            themes_by_id = {row["id"]: row for row in self.themes}
            for source_type, source_id in zip(source_types, source_ids, strict=True):
                if source_type == "conversation_note":
                    row = self.conversation_notes.get(source_id)
                    if row is None or not str(row.get("text") or "").strip():
                        continue
                    rows.append(
                        {
                            "source_type": "conversation_note",
                            "source_id": source_id,
                            "message_id": None,
                            "sent_at": row.get("created_at"),
                            "source_created_at": row.get("created_at"),
                            "source_updated_at": row.get("created_at"),
                            "sort_at": row.get("created_at"),
                            "content": row.get("text"),
                        }
                    )
                elif source_type == "theme":
                    row = themes_by_id.get(source_id)
                    if row is None or row.get("status", "active") != "active":
                        continue
                    content = "\n".join(
                        part for part in (row.get("title"), row.get("description")) if part
                    ).strip()
                    if not content:
                        continue
                    rows.append(
                        {
                            "source_type": "theme",
                            "source_id": source_id,
                            "message_id": None,
                            "sent_at": row.get("last_active_at"),
                            "source_created_at": row.get("first_seen_at")
                            or row.get("last_active_at"),
                            "source_updated_at": row.get("last_reinforced_at")
                            or row.get("last_active_at"),
                            "sort_at": row.get("last_active_at"),
                            "content": content,
                        }
                    )
                elif source_type == "artifact":
                    row = self.conversation_artifacts.get(source_id)
                    if row is None or row.get("deleted_at") is not None:
                        continue
                    payload = row.get("payload") or {}
                    content = "\n".join(
                        part.strip()
                        for part in (
                            payload.get("summary"),
                            payload.get("review_summary"),
                            payload.get("notes"),
                        )
                        if isinstance(part, str) and part.strip()
                    )
                    if not content:
                        continue
                    rows.append(
                        {
                            "source_type": "artifact",
                            "source_id": source_id,
                            "message_id": None,
                            "sent_at": row.get("created_at"),
                            "source_created_at": row.get("created_at"),
                            "source_updated_at": row.get("created_at"),
                            "sort_at": row.get("created_at"),
                            "content": content,
                        }
                    )
            return rows
        if "FROM bot_turns bt" in compact and "final_output_message_id IS NULL" in compact:
            # Silent-turns hot-context block. The real query joins
            # tool_calls; the fake pool doesn't track silent turns, so
            # default to no silent turns. Tests that exercise the block
            # should construct rows directly.
            return []
        if "FROM messages" in compact and " id = ANY(" in compact:
            ids = set(args[0])
            participant_ids = None
            bot_filter = args[1] if len(args) > 1 else None
            topic_filter = args[2] if len(args) > 2 else None
            if len(args) > 3 and isinstance(args[1], list):
                participant_ids = set(args[1])
                bot_filter = args[2]
                topic_filter = args[3]
            return [
                {
                    "id": row["id"],
                    "direction": row["direction"],
                    "sender_id": row["sender_id"],
                    "recipient_id": row["recipient_id"],
                    "charge": row["charge"],
                    "sent_at": row["sent_at"],
                    "content": row["content"],
                    "media_type": row.get("media_type"),
                    "media_duration_seconds": row.get("media_duration_seconds"),
                    "media_analysis": row.get("media_analysis"),
                    "handling_result": row.get("handling_result"),
                    "handled_at": row.get("handled_at"),
                    "processing_error": row.get("processing_error"),
                    "bot_id": row.get("bot_id"),
                    "topic_id": row.get("topic_id"),
                }
                for row in self.messages
                if row["id"] in ids
                and (
                    participant_ids is None
                    or row.get("sender_id") in participant_ids
                    or row.get("recipient_id") in participant_ids
                )
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
            # --- Topic-recent prior (T4): sent_at < $4 + configurable limit ---
            if "AND sent_at < $" in compact and "LIMIT $" in compact:
                sent_at_cutoff_str = args[3]
                limit = args[4] if len(args) > 4 else 20
                from datetime import datetime as dt

                cutoff = dt.fromisoformat(str(sent_at_cutoff_str))
                if cutoff.tzinfo is None:
                    cutoff = cutoff.replace(tzinfo=UTC)
                prior_rows = [
                    row
                    for row in scoped_rows
                    if row.get("deleted_at") is None
                    and row["sent_at"] < cutoff
                ]
                prior_rows.sort(key=lambda r: r["sent_at"], reverse=True)
                return list(reversed(prior_rows[:limit]))
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
    # relevant_prior populated by topic-recent selection (T4): messages
    # older than the hot-context window edge, capped at 5.
    assert len(hc.relevant_prior) == 5
    assert all(item.get("source") == "topic_recent" for item in hc.relevant_prior)
    assert all("id" in item for item in hc.relevant_prior)
    assert all("sent_at" in item for item in hc.relevant_prior)
    # All prior entries must be strictly older than the window edge.
    window_edge_sent_at = hc.trigger_metadata["hot_context_window_edge"]["sent_at"]
    for item in hc.relevant_prior:
        assert item["sent_at"] < window_edge_sent_at
    assert hc.trigger_metadata["messages"][0]["charge"] == "charged"
    assert hc.trigger_metadata["hot_context_window_edge"] == {
        "message_id": str(hc.recent_messages[0]["id"]),
        "sent_at": hc.recent_messages[0]["sent_at"],
    }
    assert (
        hc.trigger_metadata["hot_context_edge"]
        == hc.trigger_metadata["hot_context_window_edge"]
    )
    partner_rows = [
        item
        for item in hc.recent_messages
        if item["sender_id"] == partner.id or item["recipient_id"] == partner.id
    ]
    assert any(item.get("raw_content_hidden") for item in partner_rows)


async def test_topic_recent_prior_no_duplicate_with_recent_messages(
    hot_context_seed,
):
    """Prove topic-recent prior rows are disjoint from the last-20 window."""
    pool, user, partner = hot_context_seed

    hc = await build_hot_context(pool, user, partner, [pool.trigger_id])

    recent_ids = {item["id"] for item in hc.recent_messages}
    prior_ids = {item["id"] for item in hc.relevant_prior}

    assert len(recent_ids) == 20
    assert len(prior_ids) == 5
    assert recent_ids.isdisjoint(prior_ids), (
        "topic-recent prior must not duplicate any message already present "
        "in the last-20 recent-messages window"
    )

    # Confirm every prior entry is strictly older than the window edge.
    window_edge_sent_at = hc.trigger_metadata["hot_context_window_edge"]["sent_at"]
    for item in hc.relevant_prior:
        assert item["sent_at"] < window_edge_sent_at, (
            f"prior entry {item['id']} sent_at={item['sent_at']} is not older "
            f"than window edge {window_edge_sent_at}"
        )


async def test_topic_recent_prior_excludes_deleted_rows(hot_context_seed):
    """Prove messages with deleted_at set are excluded from topic-recent prior."""
    pool, user, partner = hot_context_seed

    # Add one deleted older-than-window message and one live older-than-window
    # message so we can prove the query is working yet the deleted row is absent.
    # The live message is placed just barely older than the window edge so it
    # displaces the oldest existing prior row within the 5-row cap.
    deleted_id = uuid4()
    live_id = uuid4()
    window_edge = pool.now - timedelta(seconds=20)
    pool.messages.append(
        {
            "id": deleted_id,
            "direction": "inbound",
            "sender_id": user.id,
            "recipient_id": partner.id,
            "content": "deleted older message",
            "sent_at": window_edge - timedelta(seconds=2),
            "charge": "routine",
            "bot_id": pool.bot_id,
            "topic_id": pool.topic_id,
            "deleted_at": pool.now - timedelta(seconds=1),
        }
    )
    pool.messages.append(
        {
            "id": live_id,
            "direction": "inbound",
            "sender_id": user.id,
            "recipient_id": partner.id,
            "content": "live older message",
            "sent_at": window_edge - timedelta(milliseconds=500),
            "charge": "routine",
            "bot_id": pool.bot_id,
            "topic_id": pool.topic_id,
        }
    )

    hc = await build_hot_context(pool, user, partner, [pool.trigger_id])

    prior_ids = {item["id"] for item in hc.relevant_prior}
    assert deleted_id not in prior_ids, (
        "deleted row must not appear in topic-recent prior"
    )
    assert live_id in prior_ids, (
        "non-deleted older-than-window row must appear in topic-recent prior"
    )


async def test_topic_recent_prior_redacts_partner_private_content(
    hot_context_seed,
):
    """Prove partner-private rows in topic-recent prior are redacted (content=None,
    raw_content_hidden=True) while user-owned rows are visible."""
    pool, user, partner = hot_context_seed

    hc = await build_hot_context(pool, user, partner, [pool.trigger_id])

    assert len(hc.relevant_prior) >= 2, (
        "need at least 2 prior entries to test partner vs user redaction"
    )

    # Thread ownership follows _message_thread_owner_id: for inbound messages
    # the sender owns the thread; for outbound the recipient owns the thread.
    # All older-than-window messages in the default fixture are inbound.
    partner_items = [
        item
        for item in hc.relevant_prior
        if item["direction"] == "inbound" and item["sender_id"] == partner.id
    ]
    user_items = [
        item
        for item in hc.relevant_prior
        if item["direction"] == "inbound" and item["sender_id"] == user.id
    ]

    # Partner has not opted in → all partner-thread rows must be redacted.
    assert len(partner_items) > 0, "expected at least one partner-row in prior"
    for item in partner_items:
        assert item.get("raw_content_hidden"), (
            f"partner message {item['id']} must have raw_content_hidden=True"
        )
        assert item.get("content") is None, (
            f"partner message {item['id']} must have content=None when not opted in"
        )

    # User-owned rows must be visible regardless of partner share.
    assert len(user_items) > 0, "expected at least one user-row in prior"
    for item in user_items:
        assert not item.get("raw_content_hidden"), (
            f"user message {item['id']} must have raw_content_hidden=False"
        )
        assert item.get("content") is not None, (
            f"user message {item['id']} must have visible content"
        )


async def test_topic_recent_prior_shows_partner_content_when_opted_in(
    hot_context_seed,
):
    """Prove partner messages in topic-recent prior are visible when the partner
    has opted into sharing."""
    pool, user, partner = hot_context_seed
    pool.user_bot_state[(partner.id, pool.bot_id)] = {"partner_share": "opt_in"}

    hc = await build_hot_context(pool, user, partner, [pool.trigger_id])

    # Thread ownership follows _message_thread_owner_id: for inbound messages
    # the sender owns the thread.
    partner_items = [
        item
        for item in hc.relevant_prior
        if item["direction"] == "inbound" and item["sender_id"] == partner.id
    ]
    assert len(partner_items) > 0, (
        "expected at least one partner-row in prior when partner opted in"
    )
    for item in partner_items:
        assert not item.get("raw_content_hidden"), (
            f"partner message {item['id']} must have raw_content_hidden=False "
            f"when partner_share=opt_in"
        )
        assert item.get("content") is not None, (
            f"partner message {item['id']} must have visible content "
            f"when partner_share=opt_in"
        )


async def test_semantic_prior_uses_visible_trigger_text_and_preserves_metadata(
    hot_context_seed, monkeypatch
):
    pool, user, partner = hot_context_seed
    topic_recent_dup_id = pool.messages[4]["id"]
    semantic_new_id = uuid4()
    recent_window_id = pool.messages[-2]["id"]
    seen_request = {}
    pool.messages.insert(
        0,
        {
            "id": semantic_new_id,
            "direction": "inbound",
            "sender_id": user.id,
            "recipient_id": partner.id,
            "content": "older semantic hit",
            "sent_at": pool.now - timedelta(seconds=60),
            "charge": "routine",
            "bot_id": pool.bot_id,
            "topic_id": pool.topic_id,
        },
    )

    async def fake_hybrid_search(_pool, request):
        seen_request["query"] = request.query
        seen_request["mode"] = request.mode
        seen_request["viewer_user_id"] = request.viewer_user_id
        seen_request["partner_user_id"] = request.partner_user_id
        seen_request["bot_id"] = request.bot_id
        seen_request["topic_id"] = request.topic_id
        seen_request["dyad_id"] = request.dyad_id
        return [
            RetrievalResult(
                message_id=topic_recent_dup_id,
                match_type="both",
                rrf_score=0.72,
                keyword_rank=1,
                semantic_rank=2,
                semantic_degraded=False,
                keyword_score=0.88,
            ),
            RetrievalResult(
                message_id=semantic_new_id,
                match_type="semantic",
                rrf_score=0.61,
                keyword_rank=None,
                semantic_rank=1,
                semantic_degraded=False,
                keyword_score=None,
            ),
            RetrievalResult(
                message_id=recent_window_id,
                match_type="exact",
                rrf_score=0.55,
                keyword_rank=2,
                semantic_rank=None,
                semantic_degraded=False,
                keyword_score=0.77,
            ),
        ]

    monkeypatch.setattr("app.services.hot_context.hybrid_search", fake_hybrid_search)

    hc = await build_hot_context(
        pool,
        user,
        partner,
        [pool.trigger_id],
        dyad_id=uuid4(),
    )

    assert seen_request["query"] == "message 24"
    assert seen_request["mode"] == "hybrid"
    assert seen_request["viewer_user_id"] == user.id
    assert seen_request["partner_user_id"] == partner.id
    assert seen_request["bot_id"] == pool.bot_id
    assert seen_request["topic_id"] == pool.topic_id
    assert seen_request["dyad_id"] is not None

    by_id = {item["id"]: item for item in hc.relevant_prior}
    assert recent_window_id not in by_id
    assert topic_recent_dup_id in by_id
    assert by_id[topic_recent_dup_id]["source"] == "topic_recent"
    assert by_id[topic_recent_dup_id]["retrieval"]["match_type"] == "both"
    assert by_id[topic_recent_dup_id]["retrieval"]["rrf_score"] == 0.72
    assert semantic_new_id not in {item["id"] for item in hc.recent_messages}


async def test_semantic_prior_skips_retrieval_without_visible_trigger_text(
    hot_context_seed, monkeypatch
):
    pool, user, partner = hot_context_seed
    pool.user_bot_state[(partner.id, pool.bot_id)] = {"partner_share": "opt_out"}
    pool.messages[-1]["sender_id"] = partner.id
    pool.messages[-1]["recipient_id"] = user.id
    called = False

    async def fake_hybrid_search(_pool, request):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr("app.services.hot_context.hybrid_search", fake_hybrid_search)

    hc = await build_hot_context(pool, user, partner, [pool.trigger_id])

    assert called is False
    assert all(item.get("source") == "topic_recent" for item in hc.relevant_prior)


async def test_semantic_prior_retrieval_errors_degrade_to_topic_recent_only(
    hot_context_seed, monkeypatch
):
    pool, user, partner = hot_context_seed

    async def fake_hybrid_search(_pool, request):
        raise TimeoutError("embed timeout")

    monkeypatch.setattr("app.services.hot_context.hybrid_search", fake_hybrid_search)

    hc = await build_hot_context(pool, user, partner, [pool.trigger_id])

    assert len(hc.relevant_prior) == 5
    assert all(item.get("source") == "topic_recent" for item in hc.relevant_prior)
    assert all("retrieval" not in item for item in hc.relevant_prior)


async def test_semantic_prior_skips_explicit_non_message_results(
    hot_context_seed, monkeypatch
):
    pool, user, partner = hot_context_seed
    memory_id = uuid4()
    # A dedicated-source (observation) result with a populated message_id.
    # It must be excluded by the _DEDICATED_SOURCE_TYPES filter even though
    # message_id is not None.  The message itself may appear via the
    # independent topic_recent path, but it will not carry retrieval
    # metadata because the semantic path was excluded.
    dedicated_msg_id = pool.messages[2]["id"]

    async def fake_hybrid_search(_pool, request):
        return [
            RetrievalResult(
                message_id=None,
                source_type="memory",
                source_id=memory_id,
                match_type="semantic",
                rrf_score=0.91,
                keyword_rank=None,
                semantic_rank=1,
                semantic_degraded=False,
            ),
            RetrievalResult(
                message_id=dedicated_msg_id,
                source_type="observation",
                source_id=uuid4(),
                match_type="semantic",
                rrf_score=0.81,
                keyword_rank=None,
                semantic_rank=2,
                semantic_degraded=False,
            ),
            RetrievalResult(
                message_id=pool.messages[0]["id"],
                match_type="semantic",
                rrf_score=0.74,
                keyword_rank=None,
                semantic_rank=3,
                semantic_degraded=False,
                keyword_score=None,
            ),
        ]

    monkeypatch.setattr("app.services.hot_context.hybrid_search", fake_hybrid_search)

    hc = await build_hot_context(pool, user, partner, [pool.trigger_id])

    # Dedicated source with null message_id: excluded (by null check).
    assert memory_id not in {item["id"] for item in hc.relevant_prior}

    # Dedicated source with populated message_id: excluded by source_type
    # filter.  The message may appear via topic_recent but without
    # retrieval metadata (the semantic path was excluded).
    by_id = {item["id"]: item for item in hc.relevant_prior}
    if dedicated_msg_id in by_id:
        assert by_id[dedicated_msg_id].get("retrieval") is None, (
            "dedicated source must not enrich with retrieval metadata"
        )

    # Normal message result still hydrates and merges with retrieval
    # metadata as before.
    normal_item = by_id.get(pool.messages[0]["id"])
    assert normal_item is not None, "normal message must appear in relevant_prior"
    assert normal_item["source"] == "topic_recent"
    assert normal_item.get("retrieval") is not None, (
        "normal message must carry retrieval metadata"
    )


async def test_semantic_prior_filters_null_message_ids_before_exclusion_and_hydration(
    hot_context_seed, monkeypatch
):
    pool, user, partner = hot_context_seed
    message_id = pool.messages[0]["id"]

    async def fake_hybrid_search(_pool, request):
        return [
            RetrievalResult(
                message_id=None,
                source_type="observation",
                source_id=uuid4(),
                match_type="semantic",
                rrf_score=0.91,
                keyword_rank=None,
                semantic_rank=1,
                semantic_degraded=False,
            ),
            RetrievalResult(
                message_id=message_id,
                match_type="semantic",
                rrf_score=0.74,
                keyword_rank=None,
                semantic_rank=2,
                semantic_degraded=False,
                keyword_score=None,
            ),
        ]

    monkeypatch.setattr("app.services.hot_context.hybrid_search", fake_hybrid_search)

    hc = await build_hot_context(pool, user, partner, [pool.trigger_id])

    assert None not in {item["id"] for item in hc.relevant_prior}
    assert all(item["id"] != message_id or item["source"] == "topic_recent" for item in hc.relevant_prior)


# ── T8: Focused semantic-prior unit tests ──────────────────────────────


async def test_semantic_prior_deterministic_merge(
    hot_context_seed, monkeypatch
):
    """Two calls with the same monkeypatched hybrid_search must produce
    identical relevant_prior lists (deterministic merge)."""
    pool, user, partner = hot_context_seed
    topic_recent_dup_id = pool.messages[3]["id"]
    semantic_new_id = uuid4()
    pool.messages.insert(
        0,
        {
            "id": semantic_new_id,
            "direction": "inbound",
            "sender_id": user.id,
            "recipient_id": partner.id,
            "content": "semantic-only message",
            "sent_at": pool.now - timedelta(seconds=65),
            "charge": "routine",
            "bot_id": pool.bot_id,
            "topic_id": pool.topic_id,
        },
    )

    async def fake_hybrid_search(_pool, request):
        return [
            RetrievalResult(
                message_id=topic_recent_dup_id,
                match_type="both",
                rrf_score=0.72,
                keyword_rank=1,
                semantic_rank=2,
                semantic_degraded=False,
                keyword_score=0.88,
            ),
            RetrievalResult(
                message_id=semantic_new_id,
                match_type="semantic",
                rrf_score=0.61,
                keyword_rank=None,
                semantic_rank=1,
                semantic_degraded=False,
                keyword_score=None,
            ),
        ]

    monkeypatch.setattr("app.services.hot_context.hybrid_search", fake_hybrid_search)

    hc1 = await build_hot_context(pool, user, partner, [pool.trigger_id])
    hc2 = await build_hot_context(pool, user, partner, [pool.trigger_id])

    # Same ids in same order.
    ids1 = [item["id"] for item in hc1.relevant_prior]
    ids2 = [item["id"] for item in hc2.relevant_prior]
    assert ids1 == ids2, f"deterministic merge failed: {ids1} != {ids2}"

    # Same sources for each item.
    sources1 = [item["source"] for item in hc1.relevant_prior]
    sources2 = [item["source"] for item in hc2.relevant_prior]
    assert sources1 == sources2

    # Same retrieval metadata on enriched items.
    for a, b in zip(hc1.relevant_prior, hc2.relevant_prior):
        assert a.get("retrieval") == b.get("retrieval")


async def test_semantic_prior_duplicates_merged_not_duplicated(
    hot_context_seed, monkeypatch
):
    """Duplicate message IDs between topic-recent prior and semantic results
    must be merged into a single entry (the topic-recent row enriched with
    retrieval metadata), never duplicated."""
    pool, user, partner = hot_context_seed
    # Pick a message that will appear in topic-recent prior (messages[0]
    # is the oldest, so it's definitely older than the window edge).
    dup_id = pool.messages[0]["id"]

    async def fake_hybrid_search(_pool, request):
        return [
            RetrievalResult(
                message_id=dup_id,
                match_type="exact",
                rrf_score=0.95,
                keyword_rank=1,
                semantic_rank=None,
                semantic_degraded=False,
                keyword_score=0.99,
            ),
        ]

    monkeypatch.setattr("app.services.hot_context.hybrid_search", fake_hybrid_search)

    hc = await build_hot_context(pool, user, partner, [pool.trigger_id])

    # dup_id must appear exactly once in relevant_prior.
    occurrences = [item for item in hc.relevant_prior if item["id"] == dup_id]
    assert len(occurrences) == 1, (
        f"duplicate ID {dup_id} appears {len(occurrences)} times, expected 1"
    )
    dup = occurrences[0]
    # The row kept must be the topic_recent source.
    assert dup["source"] == "topic_recent"
    # It must carry the retrieval metadata enrichment.
    assert dup.get("retrieval") is not None
    assert dup["retrieval"]["match_type"] == "exact"
    assert dup["retrieval"]["rrf_score"] == 0.95


async def test_semantic_prior_score_metadata_preserved_for_ordering(
    hot_context_seed, monkeypatch
):
    """Every item in relevant_prior that originated from semantic retrieval
    (or was enriched by it) must preserve its retrieval metadata with
    match_type, rrf_score, and keyword_rank / semantic_rank fields so
    downstream consumers can reason about ordering."""
    pool, user, partner = hot_context_seed
    semantic_new_1 = uuid4()
    semantic_new_2 = uuid4()
    pool.messages.insert(
        0,
        {
            "id": semantic_new_1,
            "direction": "inbound",
            "sender_id": user.id,
            "recipient_id": partner.id,
            "content": "first semantic hit",
            "sent_at": pool.now - timedelta(seconds=70),
            "charge": "routine",
            "bot_id": pool.bot_id,
            "topic_id": pool.topic_id,
        },
    )
    pool.messages.insert(
        0,
        {
            "id": semantic_new_2,
            "direction": "inbound",
            "sender_id": user.id,
            "recipient_id": partner.id,
            "content": "second semantic hit",
            "sent_at": pool.now - timedelta(seconds=80),
            "charge": "routine",
            "bot_id": pool.bot_id,
            "topic_id": pool.topic_id,
        },
    )

    async def fake_hybrid_search(_pool, request):
        return [
            RetrievalResult(
                message_id=semantic_new_1,
                match_type="semantic",
                rrf_score=0.82,
                keyword_rank=None,
                semantic_rank=1,
                semantic_degraded=False,
                keyword_score=None,
            ),
            RetrievalResult(
                message_id=semantic_new_2,
                match_type="both",
                rrf_score=0.67,
                keyword_rank=2,
                semantic_rank=3,
                semantic_degraded=False,
                keyword_score=0.76,
            ),
        ]

    monkeypatch.setattr("app.services.hot_context.hybrid_search", fake_hybrid_search)

    hc = await build_hot_context(pool, user, partner, [pool.trigger_id])

    by_id = {item["id"]: item for item in hc.relevant_prior}

    # semantic_new_1: net-new semantic row.
    assert semantic_new_1 in by_id, "semantic_new_1 must appear in relevant_prior"
    s1 = by_id[semantic_new_1]
    assert s1["source"] == "semantic"
    assert s1["retrieval"]["match_type"] == "semantic"
    assert s1["retrieval"]["rrf_score"] == 0.82
    assert s1["retrieval"]["semantic_rank"] == 1
    assert s1["retrieval"]["keyword_rank"] is None

    # semantic_new_2: net-new semantic row.
    assert semantic_new_2 in by_id, "semantic_new_2 must appear in relevant_prior"
    s2 = by_id[semantic_new_2]
    assert s2["source"] == "semantic"
    assert s2["retrieval"]["match_type"] == "both"
    assert s2["retrieval"]["rrf_score"] == 0.67
    assert s2["retrieval"]["keyword_score"] == 0.76

    # Topic-recent-only items must NOT carry retrieval metadata.
    topic_recent_only = [
        item
        for item in hc.relevant_prior
        if item["id"] not in {semantic_new_1, semantic_new_2}
        and item["source"] == "topic_recent"
    ]
    assert len(topic_recent_only) > 0, "expected at least one topic-recent-only item"
    for item in topic_recent_only:
        assert "retrieval" not in item, (
            f"topic-recent-only item {item['id']} must not carry retrieval metadata"
        )


async def test_semantic_prior_silent_turns_skip_retrieval(
    hot_context_seed, monkeypatch
):
    """When the triggering messages all have hidden content (e.g. partner
    messages with partner_share=opt_out), _semantic_query_text returns
    the empty string and hybrid_search is never called.  This simulates
    the silent-turn path where no user-visible trigger text exists."""
    pool, user, partner = hot_context_seed
    # Make the trigger message partner-owned with partner_share=opt_out.
    pool.user_bot_state[(partner.id, pool.bot_id)] = {"partner_share": "opt_out"}
    pool.messages[-1]["sender_id"] = partner.id
    pool.messages[-1]["recipient_id"] = user.id
    pool.messages[-1]["content"] = "partner message that should be hidden"

    call_count = 0

    async def fake_hybrid_search(_pool, request):
        nonlocal call_count
        call_count += 1
        raise AssertionError("hybrid_search must not be called for silent turns")

    monkeypatch.setattr("app.services.hot_context.hybrid_search", fake_hybrid_search)

    hc = await build_hot_context(pool, user, partner, [pool.trigger_id])

    assert call_count == 0, (
        f"hybrid_search was called {call_count} times for a silent turn; "
        f"expected 0 when no visible trigger text is available"
    )
    # relevant_prior must still be populated from topic-recent.
    assert len(hc.relevant_prior) <= 8
    assert all(item.get("source") == "topic_recent" for item in hc.relevant_prior)
    assert all("retrieval" not in item for item in hc.relevant_prior)


async def test_semantic_prior_hydration_error_degrades(
    hot_context_seed, monkeypatch
):
    """When hybrid_search succeeds but hydration of the returned message
    IDs fails (e.g. pool.fetch raises), the prior must still contain
    topic-recent rows — the error is non-fatal."""
    pool, user, partner = hot_context_seed
    semantic_new_id = uuid4()
    # Don't add the message to pool.messages — the hydration will fail
    # because the message ID won't be in the fake pool's messages list.

    async def fake_hybrid_search(_pool, request):
        return [
            RetrievalResult(
                message_id=semantic_new_id,
                match_type="semantic",
                rrf_score=0.55,
                keyword_rank=None,
                semantic_rank=1,
                semantic_degraded=False,
                keyword_score=None,
            ),
        ]

    monkeypatch.setattr("app.services.hot_context.hybrid_search", fake_hybrid_search)

    hc = await build_hot_context(pool, user, partner, [pool.trigger_id])

    # The semantic result couldn't be hydrated, so it must not appear.
    prior_ids = {item["id"] for item in hc.relevant_prior}
    assert semantic_new_id not in prior_ids, (
        "unhydrated semantic result must not appear in relevant_prior"
    )
    # relevant_prior is still populated from topic-recent.
    assert len(hc.relevant_prior) <= 8
    assert all(item.get("source") == "topic_recent" for item in hc.relevant_prior)


async def test_semantic_prior_hydrates_non_message_rows_without_losing_message_hits(
    hot_context_seed, monkeypatch
):
    pool, user, partner = hot_context_seed
    note_id = uuid4()
    theme_id = uuid4()
    artifact_id = uuid4()
    message_id = pool.messages[0]["id"]
    pool.conversation_notes[note_id] = {
        "id": note_id,
        "text": "Conversation note about the deployment rollback",
        "created_at": pool.now - timedelta(minutes=70),
    }
    pool.themes.append(
        {
            "id": theme_id,
            "title": "Deployment reliability",
            "description": "Need rollback drills and clearer incident comms",
            "status": "active",
            "sentiment": "mixed",
            "health": "tender",
            "first_seen_at": pool.now - timedelta(days=3),
            "last_reinforced_at": pool.now - timedelta(minutes=80),
            "last_active_at": pool.now - timedelta(minutes=80),
        }
    )
    pool.conversation_artifacts[artifact_id] = {
        "id": artifact_id,
        "artifact_type": "review_summary",
        "payload": {
            "summary": "Artifact summary about the deployment rollback timeline",
            "notes": "Include the rollback drill follow-up",
        },
        "created_at": pool.now - timedelta(minutes=75),
        "deleted_at": None,
    }

    async def fake_hybrid_search(_pool, request):
        return [
            RetrievalResult(
                message_id=None,
                source_type="conversation_note",
                source_id=note_id,
                match_type="semantic",
                rrf_score=0.91,
                keyword_rank=None,
                semantic_rank=1,
                semantic_degraded=False,
            ),
            RetrievalResult(
                message_id=None,
                source_type="theme",
                source_id=theme_id,
                match_type="semantic",
                rrf_score=0.82,
                keyword_rank=None,
                semantic_rank=2,
                semantic_degraded=False,
            ),
            RetrievalResult(
                message_id=None,
                source_type="artifact",
                source_id=artifact_id,
                match_type="semantic",
                rrf_score=0.77,
                keyword_rank=None,
                semantic_rank=3,
                semantic_degraded=False,
            ),
            RetrievalResult(
                message_id=message_id,
                source_type="message",
                source_id=message_id,
                match_type="semantic",
                rrf_score=0.73,
                keyword_rank=None,
                semantic_rank=4,
                semantic_degraded=False,
            ),
        ]

    monkeypatch.setattr("app.services.hot_context.hybrid_search", fake_hybrid_search)

    hc = await build_hot_context(pool, user, partner, [pool.trigger_id])

    by_id = {item["id"]: item for item in hc.relevant_prior}
    assert by_id[note_id]["source_type"] == "conversation_note"
    assert by_id[note_id]["content"] == "Conversation note about the deployment rollback"
    assert by_id[note_id]["retrieval"]["rrf_score"] == 0.91
    assert by_id[theme_id]["source_type"] == "theme"
    assert "Deployment reliability" in by_id[theme_id]["content"]
    assert by_id[theme_id]["retrieval"]["rrf_score"] == 0.82
    assert by_id[artifact_id]["source_type"] == "artifact"
    assert "deployment rollback timeline" in by_id[artifact_id]["content"]
    assert by_id[artifact_id]["retrieval"]["rrf_score"] == 0.77
    assert by_id[message_id]["source"] == "topic_recent"
    assert by_id[message_id]["retrieval"]["rrf_score"] == 0.73

    text = render_hot_context(hc)
    assert "[conversation_note] [semantic, rrf=0.9100]" in text
    assert "[theme] [semantic, rrf=0.8200]" in text
    assert "[artifact] [semantic, rrf=0.7700]" in text


async def test_semantic_prior_non_message_rows_stay_absent_when_not_returned_or_hidden(
    hot_context_seed, monkeypatch
):
    pool, user, partner = hot_context_seed
    returned_note_id = uuid4()
    absent_theme_id = uuid4()
    hidden_note_id = uuid4()
    hidden_theme_id = uuid4()
    hidden_artifact_id = uuid4()

    pool.conversation_notes[returned_note_id] = {
        "id": returned_note_id,
        "text": "Returned note stays in relevant prior",
        "created_at": pool.now - timedelta(minutes=65),
    }
    pool.conversation_notes[hidden_note_id] = {
        "id": hidden_note_id,
        "text": "   ",
        "created_at": pool.now - timedelta(minutes=64),
    }
    pool.themes.append(
        {
            "id": absent_theme_id,
            "title": "Not returned theme",
            "description": "Should stay absent because retrieval never surfaced it",
            "status": "active",
            "sentiment": "mixed",
            "health": "tender",
            "first_seen_at": pool.now - timedelta(days=4),
            "last_reinforced_at": pool.now - timedelta(minutes=90),
            "last_active_at": pool.now - timedelta(minutes=90),
        }
    )
    pool.themes.append(
        {
            "id": hidden_theme_id,
            "title": "Hidden theme",
            "description": "Dormant theme should fail hydration",
            "status": "dormant",
            "sentiment": "mixed",
            "health": "tender",
            "first_seen_at": pool.now - timedelta(days=4),
            "last_reinforced_at": pool.now - timedelta(minutes=95),
            "last_active_at": pool.now - timedelta(minutes=95),
        }
    )
    pool.conversation_artifacts[hidden_artifact_id] = {
        "id": hidden_artifact_id,
        "artifact_type": "review_summary",
        "payload": {"summary": "Deleted artifact should fail hydration"},
        "created_at": pool.now - timedelta(minutes=68),
        "deleted_at": pool.now - timedelta(minutes=1),
    }

    async def fake_hybrid_search(_pool, request):
        return [
            RetrievalResult(
                message_id=None,
                source_type="conversation_note",
                source_id=returned_note_id,
                match_type="semantic",
                rrf_score=0.88,
                keyword_rank=None,
                semantic_rank=1,
                semantic_degraded=False,
            ),
            RetrievalResult(
                message_id=None,
                source_type="conversation_note",
                source_id=hidden_note_id,
                match_type="semantic",
                rrf_score=0.71,
                keyword_rank=None,
                semantic_rank=2,
                semantic_degraded=False,
            ),
            RetrievalResult(
                message_id=None,
                source_type="theme",
                source_id=hidden_theme_id,
                match_type="semantic",
                rrf_score=0.67,
                keyword_rank=None,
                semantic_rank=3,
                semantic_degraded=False,
            ),
            RetrievalResult(
                message_id=None,
                source_type="artifact",
                source_id=hidden_artifact_id,
                match_type="semantic",
                rrf_score=0.63,
                keyword_rank=None,
                semantic_rank=4,
                semantic_degraded=False,
            ),
        ]

    monkeypatch.setattr("app.services.hot_context.hybrid_search", fake_hybrid_search)

    hc = await build_hot_context(pool, user, partner, [pool.trigger_id])

    prior_ids = {item["id"] for item in hc.relevant_prior}
    assert returned_note_id in prior_ids
    assert hidden_note_id not in prior_ids
    assert hidden_theme_id not in prior_ids
    assert hidden_artifact_id not in prior_ids
    assert absent_theme_id not in prior_ids

    text = render_hot_context(hc)
    assert "[conversation_note] [semantic, rrf=0.8800]" in text
    assert "Deleted artifact should fail hydration" not in text
    assert "Dormant theme should fail hydration" not in text
    assert "Not returned theme" not in text


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
    monkeypatch.setenv("HOT_CONTEXT_TOKEN_BUDGET", "280")
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

    assert len(text) // 4 <= 280
    assert "shareable" in text
    assert "OOB MUST REMAIN" not in text
    assert "core=" not in text
    assert "[truncated, 6 more]" in text
    assert text.index("## High-significance observations") < text.index(
        "## Recent messages"
    )
    # memories + observations use the priority-ranked marker; recent_messages
    # uses its own "older messages omitted" wording (truncated from the front).
    assert text.count("[truncated, 6 more]") == 2
    assert "[truncated, 6 older]" in text
    get_settings.cache_clear()


def test_render_hot_context_truncates_oldest_recent_messages_first(monkeypatch):
    # Regression: when the recent-messages list is over budget, the oldest
    # messages must be dropped so the most recent turns (the immediate context
    # the model needs to answer) survive. Previously the list was popped from
    # the tail, evicting the newest messages and keeping stale backlog.
    monkeypatch.setenv("HOT_CONTEXT_TOKEN_BUDGET", "500")
    get_settings.cache_clear()
    user_id = uuid4()
    partner_id = uuid4()
    base = datetime(2026, 5, 25, 12, 0, tzinfo=UTC)
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
            "total_count": 6,
            "inbound_count": 6,
            "outbound_count": 0,
        },
        active_oob=[],
        memories=[],
        active_themes=[],
        open_watch_items=[],
        observations=[],
        # recent_messages are ordered oldest -> newest, as build_hot_context emits.
        recent_messages=[
            {
                "id": uuid4(),
                "direction": "inbound",
                "sender_id": user_id,
                "recipient_id": partner_id,
                "content": f"unique-msg-{i:04d} " + "x" * 200,
                "sent_at": (base + timedelta(minutes=i)).isoformat(),
                "charge": "routine",
            }
            for i in range(6)
        ],
        time_since_last_message="1m",
        trigger_metadata={"triggering_message_ids": [uuid4()], "messages": []},
    )

    text = render_hot_context(hc)

    # Truncation must have fired and reported the dropped (older) count.
    assert "older]" in text
    # The newest message survives; the oldest is the one evicted.
    assert "unique-msg-0005" in text
    assert "unique-msg-0000" not in text
    # Whatever survives is a contiguous newest-first suffix: no surviving
    # message may be older than a dropped one.
    surviving = [i for i in range(6) if f"unique-msg-{i:04d}" in text]
    assert surviving == list(range(surviving[0], 6))
    get_settings.cache_clear()


def test_render_hot_context_previous_topic_section_has_navigation_cues(monkeypatch):
    monkeypatch.setenv("HOT_CONTEXT_TOKEN_BUDGET", "2000")
    get_settings.cache_clear()
    user_id = uuid4()
    partner_id = uuid4()
    recent_id = uuid4()
    prior_id = uuid4()
    hc = HotContext(
        current_user={
            "id": user_id,
            "name": "Maya",
            "phone": "1",
            "timezone": "UTC",
            "style_notes": "",
            "onboarding_state": "welcomed",
            "partner_share": "opt_in",
            "partner_sharing_state": "opt_in",
        },
        partner_user={
            "id": partner_id,
            "name": "Ben",
            "phone": "2",
            "timezone": "UTC",
            "style_notes": "",
            "onboarding_state": "pending",
            "partner_share": "opt_in",
            "partner_sharing_state": "opt_in",
        },
        conversation_load={
            "period": "today",
            "timezone": "UTC",
            "total_count": 2,
            "inbound_count": 1,
            "outbound_count": 1,
        },
        active_oob=[],
        memories=[],
        active_themes=[],
        open_watch_items=[],
        observations=[],
        recent_messages=[
            {
                "id": recent_id,
                "direction": "inbound",
                "sender_id": user_id,
                "recipient_id": partner_id,
                "content": "latest turn",
                "sent_at": "2026-05-25T12:05:00+00:00",
                "charge": "routine",
            }
        ],
        relevant_prior=[
            {
                "id": prior_id,
                "direction": "outbound",
                "sender_id": partner_id,
                "recipient_id": user_id,
                "content": "older topic context " + "x" * 260,
                "sent_at": "2026-05-25T11:45:00+00:00",
                "charge": "routine",
                "source": "semantic",
                "retrieval": {"match_type": "semantic", "rrf_score": 0.4321},
            }
        ],
        time_since_last_message="1m",
        trigger_metadata={"triggering_message_ids": [recent_id], "messages": []},
    )

    text = render_hot_context(hc)

    assert text.index("## Recent messages") < text.index("## Previous on this topic")
    assert text.index("## Previous on this topic") < text.index(
        "## Your silent turns since the user's last message"
    )
    assert "Use ids below as cursor anchors with read tools to explore further back." in text
    assert f"id={str(prior_id)[:14]}" in text
    assert "source=semantic [semantic, rrf=0.4321]" in text
    assert "older topic context " in text
    assert "x" * 240 not in text
    assert "..." in text
    get_settings.cache_clear()


def test_render_hot_context_truncates_prior_before_recent_messages(monkeypatch):
    monkeypatch.setenv("HOT_CONTEXT_TOKEN_BUDGET", "560")
    get_settings.cache_clear()
    user_id = uuid4()
    partner_id = uuid4()
    recent_id = uuid4()
    base = datetime(2026, 5, 25, 12, 0, tzinfo=UTC)
    hc = HotContext(
        current_user={
            "id": user_id,
            "name": "Maya",
            "phone": "1",
            "timezone": "UTC",
            "style_notes": "",
            "onboarding_state": "welcomed",
            "partner_share": "opt_in",
            "partner_sharing_state": "opt_in",
        },
        partner_user={
            "id": partner_id,
            "name": "Ben",
            "phone": "2",
            "timezone": "UTC",
            "style_notes": "",
            "onboarding_state": "pending",
            "partner_share": "opt_in",
            "partner_sharing_state": "opt_in",
        },
        conversation_load={
            "period": "today",
            "timezone": "UTC",
            "total_count": 7,
            "inbound_count": 6,
            "outbound_count": 1,
        },
        active_oob=[],
        memories=[],
        active_themes=[],
        open_watch_items=[],
        observations=[],
        recent_messages=[
            {
                "id": recent_id,
                "direction": "inbound",
                "sender_id": user_id,
                "recipient_id": partner_id,
                "content": "keep-this-immediate-context",
                "sent_at": "2026-05-25T12:05:00+00:00",
                "charge": "routine",
            }
        ],
        relevant_prior=[
            {
                "id": uuid4(),
                "direction": "outbound",
                "sender_id": partner_id,
                "recipient_id": user_id,
                "content": f"prior-{i} " + "y" * 220,
                "sent_at": (base + timedelta(minutes=i)).isoformat(),
                "charge": "routine",
                "source": "topic_recent",
            }
            for i in range(6)
        ],
        time_since_last_message="1m",
        trigger_metadata={"triggering_message_ids": [recent_id], "messages": []},
    )

    text = render_hot_context(hc)

    assert "## Previous on this topic" in text
    assert "+4 more" in text
    assert "search() for older relevant context" in text
    assert "keep-this-immediate-context" in text
    assert "prior-0" not in text
    assert "prior-5" in text
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


# ---------------------------------------------------------------------------
# T7 — Render-focused dedicated-section tests (memories / observations /
# distillations).  Prove type labels, handles, resolved names, clipped
# snippets, and no raw dyad UUIDs in those section lines.  Also add a
# selection-parity assertion using the fake pool's known selected IDs
# without snapshotting old render text.
# ---------------------------------------------------------------------------


async def test_dedicated_section_selection_parity_matches_fake_pool(hot_context_seed):
    """`hc.memories`, `hc.observations`, and `hc.distillations` IDs match the
    fake pool's known selected rows without snapshotting old render text."""
    pool, user, partner = hot_context_seed

    # Add distillations so the fake pool returns them via fetch().
    dist_id = uuid4()
    pool.distillations = [
        {
            "id": dist_id,
            "about_user_id": user.id,
            "content": "distilled insight from conversation history",
            "shareable_summary": None,
            "display": "synthesized",
            "confidence": "high",
            "sensitivity": "medium",
            "visibility": "dyad_shareable",
            "source_user_ids": [user.id],
            "revision_count": 1,
            "related_memory_ids": [],
            "related_observation_ids": [],
            "related_theme_ids": [],
            "supporting_message_ids": [],
            "updated_at": pool.now,
            "created_at": pool.now,
            "status": "active",
            "recorded_by_bot_id": "mediator",
            "triggering_message_id": pool.messages[0]["id"],
        }
    ]

    hc = await build_hot_context(pool, user, partner, [pool.trigger_id])

    # --- Selection parity: memories ---
    # The fake pool returns all memories (no significance filter), all 3.
    expected_memory_ids = {row["id"] for row in pool.memories}
    actual_memory_ids = {item["id"] for item in hc.memories}
    assert actual_memory_ids == expected_memory_ids, (
        f"memory selection drifted: expected {expected_memory_ids}, "
        f"got {actual_memory_ids}"
    )

    # --- Selection parity: observations ---
    # The fake pool only returns observations with significance >= 3.
    expected_obs_ids = {
        row["id"]
        for row in pool.observations
        if row["significance"] is not None and row["significance"] >= 3
    }
    actual_obs_ids = {item["id"] for item in hc.observations}
    assert actual_obs_ids == expected_obs_ids, (
        f"observation selection drifted: expected {expected_obs_ids}, "
        f"got {actual_obs_ids}"
    )

    # --- Selection parity: distillations ---
    expected_dist_ids = {row["id"] for row in pool.distillations}
    actual_dist_ids = {item["id"] for item in hc.distillations}
    assert actual_dist_ids == expected_dist_ids, (
        f"distillation selection drifted: expected {expected_dist_ids}, "
        f"got {actual_dist_ids}"
    )


def test_dedicated_section_memory_lines_show_type_label_and_resolved_names(
    monkeypatch,
):
    """Memory lines must carry the 'memory' type label, resolve about_user_id
    to a human name (not a raw UUID), and clip long content."""
    monkeypatch.setenv("HOT_CONTEXT_TOKEN_BUDGET", "2000")
    get_settings.cache_clear()
    user_id = uuid4()
    partner_id = uuid4()
    memory_id = uuid4()
    hc = HotContext(
        current_user={
            "id": user_id,
            "name": "Maya",
            "phone": "1",
            "timezone": "UTC",
            "style_notes": "",
            "onboarding_state": "welcomed",
            "partner_share": "opt_in",
            "partner_sharing_state": "opt_in",
        },
        partner_user={
            "id": partner_id,
            "name": "Ben",
            "phone": "2",
            "timezone": "UTC",
            "style_notes": "",
            "onboarding_state": "pending",
            "partner_share": "opt_in",
            "partner_sharing_state": "opt_in",
        },
        conversation_load={
            "period": "today",
            "timezone": "UTC",
            "total_count": 1,
            "inbound_count": 1,
            "outbound_count": 0,
        },
        active_oob=[],
        memories=[
            {
                "id": memory_id,
                "about_user_id": partner_id,
                "content": "Ben prefers direct communication " + "x" * 300,
                "created_at": datetime(2026, 5, 20, tzinfo=UTC),
                "last_referenced_at": datetime(2026, 5, 25, tzinfo=UTC),
            }
        ],
        active_themes=[],
        open_watch_items=[],
        observations=[],
        recent_messages=[
            {
                "id": uuid4(),
                "direction": "inbound",
                "sender_id": user_id,
                "recipient_id": partner_id,
                "content": "hello",
                "sent_at": datetime.now(UTC).isoformat(),
                "charge": "routine",
            }
        ],
        time_since_last_message="1m",
        trigger_metadata={"triggering_message_ids": [uuid4()], "messages": []},
    )

    text = render_hot_context(hc)

    # Extract the ## Memories section
    assert "## Memories" in text
    mem_start = text.index("## Memories")
    next_section = text.find("##", mem_start + len("## Memories"))
    memory_section = text[mem_start:next_section] if next_section != -1 else text[mem_start:]

    # Type label
    assert "memory" in memory_section
    # Resolved name (not raw UUID)
    assert "Ben" in memory_section
    assert str(partner_id) not in memory_section
    # Clipped content snippet
    assert "x" * 240 not in memory_section
    assert "..." in memory_section
    # ID is present (clipped)
    assert str(memory_id)[:14] in memory_section

    get_settings.cache_clear()


def test_dedicated_section_observation_lines_show_type_label_and_resolved_names(
    monkeypatch,
):
    """Observation lines must carry the 'observation' type label, resolve
    about_user_id to a human name, show confidence/significance, and clip."""
    monkeypatch.setenv("HOT_CONTEXT_TOKEN_BUDGET", "2000")
    get_settings.cache_clear()
    user_id = uuid4()
    partner_id = uuid4()
    obs_id = uuid4()
    hc = HotContext(
        current_user={
            "id": user_id,
            "name": "Maya",
            "phone": "1",
            "timezone": "UTC",
            "style_notes": "",
            "onboarding_state": "welcomed",
            "partner_share": "opt_in",
            "partner_sharing_state": "opt_in",
        },
        partner_user={
            "id": partner_id,
            "name": "Ben",
            "phone": "2",
            "timezone": "UTC",
            "style_notes": "",
            "onboarding_state": "pending",
            "partner_share": "opt_in",
            "partner_sharing_state": "opt_in",
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
        observations=[
            {
                "id": obs_id,
                "about_user_id": user_id,
                "content": "Maya shows increased engagement " + "y" * 300,
                "confidence": "high",
                "significance": 4,
                "created_at": datetime(2026, 5, 20, tzinfo=UTC),
                "last_reinforced_at": None,
            }
        ],
        recent_messages=[
            {
                "id": uuid4(),
                "direction": "inbound",
                "sender_id": user_id,
                "recipient_id": partner_id,
                "content": "hello",
                "sent_at": datetime.now(UTC).isoformat(),
                "charge": "routine",
            }
        ],
        time_since_last_message="1m",
        trigger_metadata={"triggering_message_ids": [uuid4()], "messages": []},
    )

    text = render_hot_context(hc)

    assert "## High-significance observations" in text
    obs_start = text.index("## High-significance observations")
    next_section = text.find(
        "##", obs_start + len("## High-significance observations")
    )
    obs_section = (
        text[obs_start:next_section] if next_section != -1 else text[obs_start:]
    )

    # Type label
    assert "observation" in obs_section
    # Confidence + significance shown
    assert "confidence=high" in obs_section
    assert "sig=4" in obs_section
    # Resolved name (not raw UUID)
    assert "Maya" in obs_section
    assert str(user_id) not in obs_section
    # Clipped content snippet
    assert "y" * 240 not in obs_section
    assert "..." in obs_section
    # ID is present (clipped)
    assert str(obs_id)[:14] in obs_section

    get_settings.cache_clear()


def test_dedicated_section_distillation_lines_show_type_label_and_metadata(
    monkeypatch,
):
    """Distillation lines must carry the 'distillation' type label, show
    display/confidence/sensitivity/visibility, and clip content."""
    monkeypatch.setenv("HOT_CONTEXT_TOKEN_BUDGET", "2000")
    get_settings.cache_clear()
    user_id = uuid4()
    partner_id = uuid4()
    dist_id = uuid4()
    hc = HotContext(
        current_user={
            "id": user_id,
            "name": "Maya",
            "phone": "1",
            "timezone": "UTC",
            "style_notes": "",
            "onboarding_state": "welcomed",
            "partner_share": "opt_in",
            "partner_sharing_state": "opt_in",
        },
        partner_user={
            "id": partner_id,
            "name": "Ben",
            "phone": "2",
            "timezone": "UTC",
            "style_notes": "",
            "onboarding_state": "pending",
            "partner_share": "opt_in",
            "partner_sharing_state": "opt_in",
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
        distillations=[
            {
                "id": dist_id,
                "content": "Key insight about communication patterns " + "z" * 300,
                "display": "synthesized",
                "confidence": "high",
                "sensitivity": "medium",
                "visibility": "dyad_shareable",
                "source_user_ids": [user_id],
                "updated_at": datetime(2026, 5, 25, tzinfo=UTC),
                "created_at": datetime(2026, 5, 20, tzinfo=UTC),
            }
        ],
        recent_messages=[
            {
                "id": uuid4(),
                "direction": "inbound",
                "sender_id": user_id,
                "recipient_id": partner_id,
                "content": "hello",
                "sent_at": datetime.now(UTC).isoformat(),
                "charge": "routine",
            }
        ],
        time_since_last_message="1m",
        trigger_metadata={"triggering_message_ids": [uuid4()], "messages": []},
    )

    text = render_hot_context(hc)

    assert "## Distillations" in text
    dist_start = text.index("## Distillations")
    next_section = text.find("##", dist_start + len("## Distillations"))
    dist_section = (
        text[dist_start:next_section] if next_section != -1 else text[dist_start:]
    )

    # Type label
    assert "distillation" in dist_section
    # Display + metadata
    assert "display=synthesized" in dist_section
    assert "confidence=high" in dist_section
    assert "sensitivity=medium" in dist_section
    assert "visibility=dyad_shareable" in dist_section
    # Clipped content snippet
    assert "z" * 240 not in dist_section
    assert "..." in dist_section
    # ID is present (clipped)
    assert str(dist_id)[:14] in dist_section
    # Follow-up instruction preserved
    assert "use get_distillations before adding" in dist_section.lower()

    get_settings.cache_clear()


def test_dedicated_section_lines_resolve_about_user_id_to_names_not_raw_uuids(
    monkeypatch,
):
    """All three dedicated sections — memories, observations, distillations —
    must resolve about_user_id to human names (Maya / Ben) in the ``about=``
    position instead of leaking raw dyad UUIDs there.

    Note: distillation ``source_user_ids`` are rendered as raw UUID strings
    by the current helper (provenance display).  This test only enforces
    name resolution for the ``about_user_id`` field, which is the
    user-facing identity in all three dedicated section lines.
    """
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
            "partner_share": "opt_in",
            "partner_sharing_state": "opt_in",
        },
        partner_user={
            "id": partner_id,
            "name": "Ben",
            "phone": "2",
            "timezone": "UTC",
            "style_notes": "",
            "onboarding_state": "pending",
            "partner_share": "opt_in",
            "partner_sharing_state": "opt_in",
        },
        conversation_load={
            "period": "today",
            "timezone": "UTC",
            "total_count": 3,
            "inbound_count": 2,
            "outbound_count": 1,
        },
        active_oob=[],
        memories=[
            {
                "id": uuid4(),
                "about_user_id": user_id,
                "content": "memory about Maya",
                "created_at": datetime(2026, 5, 20, tzinfo=UTC),
                "last_referenced_at": None,
            },
            {
                "id": uuid4(),
                "about_user_id": partner_id,
                "content": "memory about Ben",
                "created_at": datetime(2026, 5, 21, tzinfo=UTC),
                "last_referenced_at": None,
            },
        ],
        active_themes=[],
        open_watch_items=[],
        observations=[
            {
                "id": uuid4(),
                "about_user_id": user_id,
                "content": "observation about Maya",
                "confidence": "medium",
                "significance": 3,
                "created_at": datetime(2026, 5, 20, tzinfo=UTC),
                "last_reinforced_at": None,
            },
            {
                "id": uuid4(),
                "about_user_id": partner_id,
                "content": "observation about Ben",
                "confidence": "high",
                "significance": 4,
                "created_at": datetime(2026, 5, 21, tzinfo=UTC),
                "last_reinforced_at": None,
            },
        ],
        distillations=[
            {
                "id": uuid4(),
                "content": "distillation about both",
                "display": "synthesized",
                "confidence": "high",
                "sensitivity": "low",
                "visibility": "dyad_shareable",
                "source_user_ids": [user_id, partner_id],
                "updated_at": datetime(2026, 5, 25, tzinfo=UTC),
                "created_at": datetime(2026, 5, 20, tzinfo=UTC),
            }
        ],
        recent_messages=[
            {
                "id": uuid4(),
                "direction": "inbound",
                "sender_id": user_id,
                "recipient_id": partner_id,
                "content": "hello",
                "sent_at": datetime.now(UTC).isoformat(),
                "charge": "routine",
            }
        ],
        time_since_last_message="1m",
        trigger_metadata={"triggering_message_ids": [uuid4()], "messages": []},
    )

    text = render_hot_context(hc)

    # Extract the three dedicated sections.
    sections_of_interest: list[tuple[str, str]] = []
    for header in (
        "## Memories",
        "## High-significance observations",
        "## Distillations",
    ):
        if header in text:
            start = text.index(header)
            rest = text[start + len(header) :]
            next_marker = rest.find("## ")
            section_body = rest[:next_marker] if next_marker != -1 else rest
            sections_of_interest.append((header, section_body))

    assert len(sections_of_interest) == 3, (
        f"expected 3 dedicated sections, found {len(sections_of_interest)}"
    )

    raw_user_id = str(user_id)
    raw_partner_id = str(partner_id)

    for header, body in sections_of_interest:
        if header == "## Distillations":
            # Distillations do not carry an about= field — they use
            # source_user_ids for provenance (rendered as raw UUIDs by
            # the current helper).  Verify the distillation section is
            # present and has the expected type label; name resolution
            # is enforced only on memories + observations below.
            assert "distillation" in body, (
                f"distillation type label missing from {header}"
            )
            continue

        # Memories and observations: about_user_id must resolve to names.
        assert "Maya" in body, f"resolved name 'Maya' missing from {header}"
        assert "Ben" in body, f"resolved name 'Ben' missing from {header}"

        # The about_user_id field must NOT appear as a raw UUID
        # (it is resolved via _user_label → name_map).
        assert f"about={raw_user_id}" not in body, (
            f"raw user UUID in about= position leaked into {header}"
        )
        assert f"about={raw_partner_id}" not in body, (
            f"raw partner UUID in about= position leaked into {header}"
        )

        # Memories and observations do not have a source_user_ids field,
        # so their dedicated-section bodies must be completely free of
        # raw dyad UUIDs.
        assert raw_user_id not in body, (
            f"raw user UUID leaked into {header} section"
        )
        assert raw_partner_id not in body, (
            f"raw partner UUID leaked into {header} section"
        )

    get_settings.cache_clear()
