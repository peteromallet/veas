from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.bots.registry import get_relationship_topic_id
from app.models.user import User
from app.services.tools import read_tools
from app.services.turn_context import TurnContext
from app.services.tools.write_tools import ToolCallRejected


@pytest.fixture
def helper_ctx(fake_pool):
    user = User(uuid4(), "Maya", "15555550100", "UTC")
    partner = User(uuid4(), "Ben", "15555550101", "UTC")
    turn_id = uuid4()
    fake_pool.bot_turns[turn_id] = {"id": turn_id, "reasoning": ""}
    return TurnContext(
        turn_id,
        fake_pool,
        user,
        partner,
        [uuid4()],
        current_step="read",
        bot_id="mediator",
        user_id=user.id,
        primary_topic_id=get_relationship_topic_id(),
        dyad_id=uuid4(),
        bot_spec=SimpleNamespace(display_name="Veas"),
    )


def test_cursor_kind_roundtrip_and_rejection() -> None:
    anchor_id = uuid4()
    topic_id = uuid4()
    nav_cursor = read_tools._encode_nav_cursor(
        anchor_sent_at=datetime(2026, 5, 6, 22, 15, tzinfo=UTC),
        anchor_id=anchor_id,
        scope="topic",
        topic_id=topic_id,
    )
    assert read_tools._decode_nav_cursor(nav_cursor) == {
        "anchor_id": str(anchor_id),
        "anchor_sent_at": "2026-05-06T22:15:00+00:00",
        "kind": "nav",
        "scope": "topic",
        "topic_id": str(topic_id),
    }

    page_cursor = read_tools._encode_search_page_cursor(
        query_hash="abc123",
        rank_offset=20,
        scope="thread",
    )
    assert read_tools._decode_search_page_cursor(page_cursor) == {
        "kind": "search_page",
        "query_hash": "abc123",
        "rank_offset": 20,
        "scope": "thread",
    }

    with pytest.raises(ToolCallRejected) as exc_info:
        read_tools._decode_search_page_cursor(nav_cursor)
    assert exc_info.value.result["error_code"] == "wrong_cursor_kind"
    assert exc_info.value.result["field"] == "cursor"


def test_invalid_cursor_is_structured() -> None:
    with pytest.raises(ToolCallRejected) as exc_info:
        read_tools._decode_nav_cursor("not-base64")
    assert exc_info.value.result["error_code"] == "invalid_cursor"
    assert exc_info.value.result["retryable"] is True


def test_searchable_scope_filters_mirror_retrieval_contract(helper_ctx) -> None:
    topic_id = uuid4()
    thread_owner_user_id = helper_ctx.partner.id
    filters, params, next_param = read_tools._searchable_view_scope_filters(
        helper_ctx,
        topic_id=topic_id,
        thread_owner_user_id=thread_owner_user_id,
        dyad_id=helper_ctx.dyad_id,
    )

    compact = " ".join(filters)
    assert "m.bot_id = $1" in compact
    assert "m.thread_owner_user_id = ANY($3::uuid[])" in compact
    assert "m.thread_owner_partner_share = 'opt_in'" in compact
    assert "m.topic_id = $4" in compact
    assert "m.thread_owner_user_id = $5" in compact
    assert "m.dyad_id = $6" in compact
    assert params == [
        "mediator",
        helper_ctx.user.id,
        [helper_ctx.user.id, helper_ctx.partner.id],
        topic_id,
        thread_owner_user_id,
        helper_ctx.dyad_id,
    ]
    assert next_param == 7


def test_local_day_helper_builds_utc_half_open_bounds(helper_ctx) -> None:
    helper_ctx.user = User(
        helper_ctx.user.id,
        helper_ctx.user.name,
        helper_ctx.user.phone,
        "Europe/Berlin",
    )
    helper_ctx.turn_started_at = datetime(2026, 5, 6, 22, 30, tzinfo=UTC)
    clauses: list[str] = []
    params: list[object] = []

    read_tools._add_searchable_sent_at_filters(
        helper_ctx,
        clauses,
        params,
        local_day="today",
        column="m.sent_at",
    )

    assert clauses == ["m.sent_at >= $1", "m.sent_at < $2"]
    assert params == [
        datetime(2026, 5, 6, 22, 0, tzinfo=UTC),
        datetime(2026, 5, 7, 22, 0, tzinfo=UTC),
    ]


def test_local_day_and_date_range_conflict_raises(helper_ctx) -> None:
    with pytest.raises(ValueError, match="Use either local_day or date_range"):
        read_tools._add_searchable_sent_at_filters(
            helper_ctx,
            [],
            [],
            local_day=date(2026, 5, 7),
            date_range=object(),
        )


def test_row_renderer_sets_speaker_header_cursor_and_edit_metadata(helper_ctx) -> None:
    helper_ctx.user = User(
        helper_ctx.user.id,
        helper_ctx.user.name,
        helper_ctx.user.phone,
        "Europe/Berlin",
    )
    helper_ctx.turn_started_at = datetime(2026, 5, 6, 22, 30, tzinfo=UTC)
    message_id = uuid4()
    row = {
        "message_id": message_id,
        "direction": "inbound",
        "sender_id": helper_ctx.partner.id,
        "recipient_id": None,
        "sent_at": datetime(2026, 5, 6, 22, 15, tzinfo=UTC),
        "content": "We should revisit the plan tomorrow.",
        "charge": "notable",
        "edited_at": datetime(2026, 5, 6, 22, 20, tzinfo=UTC),
        "edit_history": [{"content": "We should revisit the idea tomorrow."}],
    }

    hit = read_tools._render_message_nav_hit(
        row,
        helper_ctx,
        scope="thread",
        thread_owner_user_id=helper_ctx.partner.id,
        timezone="Europe/Berlin",
    )

    assert hit.speaker.label == "Ben"
    assert hit.sent_at_time.display == "today 00:15 Berlin"
    assert hit.header == "Ben, today 12:15 AM Berlin:"
    assert hit.edit_history_original == "We should revisit the idea tomorrow."
    assert "0000" not in hit.header
    decoded_cursor = read_tools._decode_nav_cursor(hit.cursor)
    assert decoded_cursor["scope"] == "thread"
    assert decoded_cursor["anchor_id"] == str(message_id)
    assert decoded_cursor["thread_owner_user_id"] == str(helper_ctx.partner.id)


def test_search_hit_renderer_reuses_nav_rendering(helper_ctx) -> None:
    row = {
        "message_id": uuid4(),
        "direction": "outbound",
        "sender_id": None,
        "recipient_id": helper_ctx.partner.id,
        "sent_at": datetime(2026, 5, 6, 22, 15, tzinfo=UTC),
        "content": "I can help you say that more gently.",
        "charge": "routine",
        "edited_at": None,
        "edit_history": None,
    }

    hit = read_tools._render_search_hit(
        row,
        helper_ctx,
        scope="topic",
        topic_id=helper_ctx.primary_topic_id,
        match_type=read_tools.SearchMatchType.exact,
        snippet="help you say that",
        why_matched="Exact phrase match.",
    )

    assert hit.speaker.label == "Veas"
    assert hit.header.startswith("Veas,")
    assert hit.match_type == read_tools.SearchMatchType.exact
    assert hit.why_matched == "Exact phrase match."
