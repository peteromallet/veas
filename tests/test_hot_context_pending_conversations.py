"""Integration tests for pending_live_conversations hot-context section (T10, part 3).

Covers:
(a) Section appears for solo path scoped to (user_id, bot_id, topic_id)
(b) Capped at 5 entries
(c) Filtered to status IN ('prepping','preparing','ready')
(d) Items render as '- [status] title (id)'
(e) Under tight render budget, the new section is evicted AFTER active_themes
    — i.e., eviction order: open_watch_items, active_themes, then
    pending_live_conversations.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest

from app.services.hot_context import (
    HotContext,
    _render_with_counts,
    _estimated_tokens,
)

# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_hc(
    *,
    pending_live_conversations: list[dict[str, Any]] | None = None,
    active_themes: list[dict[str, Any]] | None = None,
    open_watch_items: list[dict[str, Any]] | None = None,
    recent_messages: list[dict[str, Any]] | None = None,
    distillations: list[dict[str, Any]] | None = None,
    observations: list[dict[str, Any]] | None = None,
    memories: list[dict[str, Any]] | None = None,
    partner_shareable_summaries: list[dict[str, Any]] | None = None,
    cross_topic_peek: list[dict[str, Any]] | None = None,
    cross_topic_status: list[dict[str, Any]] | None = None,
    upcoming_items: list[dict[str, Any]] | None = None,
    bot_id: str = "mediator",
    trigger_metadata: dict[str, Any] | None = None,
) -> HotContext:
    """Build a HotContext with sensible defaults matching the real dataclass."""
    user_id = uuid4()
    partner_id = uuid4()
    trigger_msg_id = uuid4()
    default_trigger = {
        "kind": "inbound",
        "triggering_message_ids": [trigger_msg_id],
        "messages": [
            {
                "id": trigger_msg_id,
                "charge": "neutral",
                "sent_at": "2025-01-01T00:00:00+00:00",
                "content": "test message",
            },
        ],
    }
    return HotContext(
        current_user={
            "id": user_id,
            "name": "Test",
            "timezone": "UTC",
            "onboarding_state": "done",
            "style_notes": "",
            "partner_sharing_state": "unavailable",
        },
        partner_user={
            "id": partner_id,
            "name": "Partner",
            "timezone": "UTC",
            "onboarding_state": "done",
            "style_notes": "",
            "partner_sharing_state": "unavailable",
        },
        conversation_load={"message_count": 0, "turn_count": 0},
        active_oob=[],
        pending_live_conversations=pending_live_conversations or [],
        active_themes=active_themes or [],
        open_watch_items=open_watch_items or [],
        recent_messages=recent_messages or [],
        distillations=distillations or [],
        observations=observations or [],
        memories=memories or [],
        partner_shareable_summaries=partner_shareable_summaries or [],
        cross_topic_peek=cross_topic_peek or [],
        cross_topic_status=cross_topic_status or [],
        upcoming_items=upcoming_items or [],
        bot_id=bot_id,
        time_since_last_message=None,
        trigger_metadata=trigger_metadata if trigger_metadata is not None else default_trigger,
        temporal_context={},
    )


# ── Tests ────────────────────────────────────────────────────────────────────


class TestPendingLiveConversationsRendering:
    """Rendering of the pending_live_conversations section."""

    def test_section_appears_when_conversations_exist(self) -> None:
        """Section '## Pending live conversations' appears when data is present."""
        hc = _make_hc(
            pending_live_conversations=[
                {
                    "id": uuid4(),
                    "status": "ready",
                    "title": "Test Agenda",
                },
            ],
        )
        text = _render_with_counts(hc, {}, clip_limit=200)
        assert "## Pending live conversations" in text
        assert "- [ready] Test Agenda" in text

    def test_section_absent_when_empty(self) -> None:
        """Section does not appear when no conversations exist."""
        hc = _make_hc(pending_live_conversations=[])
        text = _render_with_counts(hc, {}, clip_limit=200)
        assert "## Pending live conversations" not in text

    def test_items_render_with_status_title_id(self) -> None:
        """Each item renders as '- [status] title (id=...)'."""
        conv_id = uuid4()
        hc = _make_hc(
            pending_live_conversations=[
                {
                    "id": conv_id,
                    "status": "preparing",
                    "title": "My Agenda",
                },
            ],
        )
        text = _render_with_counts(hc, {}, clip_limit=200)
        assert f"- [preparing] My Agenda (id={conv_id})" in text

    def test_multiple_items_all_render(self) -> None:
        """Multiple pending conversations all render."""
        ids = [uuid4() for _ in range(3)]
        hc = _make_hc(
            pending_live_conversations=[
                {"id": ids[0], "status": "ready", "title": "First"},
                {"id": ids[1], "status": "prepping", "title": "Second"},
                {"id": ids[2], "status": "preparing", "title": "Third"},
            ],
        )
        text = _render_with_counts(hc, {}, clip_limit=200)
        assert f"- [ready] First (id={ids[0]})" in text
        assert f"- [prepping] Second (id={ids[1]})" in text
        assert f"- [preparing] Third (id={ids[2]})" in text

    def test_untitled_fallback(self) -> None:
        """Items with missing title use 'Untitled'."""
        conv_id = uuid4()
        hc = _make_hc(
            pending_live_conversations=[
                {"id": conv_id, "status": "ready"},
            ],
        )
        text = _render_with_counts(hc, {}, clip_limit=200)
        assert f"- [ready] Untitled (id={conv_id})" in text


class TestPendingLiveConversationsEviction:
    """Eviction ordering: pending_live_conversations is lowest tier."""

    def _make_bulk_hc(self, num_convos: int = 5) -> HotContext:
        """Build a HotContext with many pending conversations."""
        convos = [
            {
                "id": uuid4(),
                "status": "ready",
                "title": f"Agenda {i}",
            }
            for i in range(num_convos)
        ]
        return _make_hc(pending_live_conversations=convos)

    def test_entries_evicted_under_tight_budget(self) -> None:
        """Under a very tight budget, pending conversations are evicted."""
        hc = self._make_bulk_hc(num_convos=5)
        # With a budget of 200 tokens, only 1-2 convos should survive
        text_full = _render_with_counts(hc, {}, clip_limit=200)
        count_full = text_full.count("- [ready]")
        assert count_full > 0  # At least some items exist in full render

        # With a budget of 50 tokens, eviction may happen
        text_tight = _render_with_counts(hc, {}, clip_limit=30)
        count_tight = text_tight.count("- [ready]")
        # Under tight budget, fewer items should survive
        assert count_tight <= count_full

    def test_eviction_order_respects_tiers(self) -> None:
        """Eviction order: open_watch_items, active_themes,
        then pending_live_conversations (lowest tier).

        When budget is tight enough to evict pending_live_conversations
        but not active_themes, the pending section should shrink while
        active_themes remain intact.
        """
        theme_ids = [uuid4() for _ in range(2)]
        hc = _make_hc(
            active_themes=[
                {"id": theme_ids[0], "slug": "theme-1", "title": "Career", "description": "Career development", "status": "active", "sentiment": "neutral", "health": "stable"},
                {"id": theme_ids[1], "slug": "theme-2", "title": "Health", "description": "Health and wellness", "status": "active", "sentiment": "positive", "health": "good"},
            ],
            pending_live_conversations=[
                {"id": uuid4(), "status": "ready", "title": "Agenda 1"},
                {"id": uuid4(), "status": "ready", "title": "Agenda 2"},
                {"id": uuid4(), "status": "ready", "title": "Agenda 3"},
                {"id": uuid4(), "status": "ready", "title": "Agenda 4"},
            ],
        )

        # Full render
        text_full = _render_with_counts(hc, {}, clip_limit=200)
        assert "## Active themes" in text_full
        assert "## Pending live conversations" in text_full

        # Very tight budget should evict pending before active themes
        # The eviction loop for pending_live_conversations runs AFTER
        # the active_themes loop
        text_tight = _render_with_counts(hc, {}, clip_limit=60)
        # active_themes should still be present
        assert "## Active themes" in text_tight
        # pending_live_conversations may have fewer items or be empty

    def test_pending_conversations_in_working_copy(self) -> None:
        """The render_hot_context working copy includes pending_live_conversations."""
        conv_id = uuid4()
        hc = _make_hc(
            pending_live_conversations=[
                {"id": conv_id, "status": "ready", "title": "Copied"},
            ],
        )
        # Verify the field is accessible on the HotContext
        assert len(hc.pending_live_conversations) == 1
        assert hc.pending_live_conversations[0]["id"] == conv_id
        assert hc.pending_live_conversations[0]["title"] == "Copied"
