from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.models.user import User
from app.services.retrieval import RetrievalResult
from app.services.tools import read_tools
from app.services.turn_context import TurnContext
from tool_schemas import (
    MessagesAfterInput,
    MessagesBeforeInput,
    OpenThreadInput,
    SearchInput,
    SearchMessagesInput,
    ScrollInput,
    TopicRecentInput,
)

pytestmark = pytest.mark.anyio


def _message_id(suffix: int) -> UUID:
    return UUID(f"00000000-0000-4000-8000-{suffix:012d}")


def _seed_message(
    pool,
    *,
    message_id: UUID,
    bot_id: str,
    topic_id: UUID,
    dyad_id: UUID,
    thread_owner_user_id: UUID,
    sender_id: UUID,
    recipient_id: UUID | None,
    sent_at: datetime,
    content: str,
    partner_share: str = "opt_in",
    charge: str = "routine",
    media_analysis: dict[str, object] | None = None,
    edited_at: datetime | None = None,
    edit_history: list[dict[str, object]] | None = None,
) -> None:
    pool.messages[message_id] = {
        "id": message_id,
        "bot_id": bot_id,
        "topic_id": topic_id,
        "dyad_id": dyad_id,
        "thread_owner_user_id": thread_owner_user_id,
        "thread_owner_partner_share": partner_share,
        "direction": "inbound",
        "sender_id": sender_id,
        "recipient_id": recipient_id,
        "sent_at": sent_at,
        "content": content,
        "charge": charge,
        "media_analysis": media_analysis,
        "edited_at": edited_at,
        "edit_history": edit_history,
        "deleted_at": None,
        "search_suppressed_at": None,
    }


@pytest.fixture
def fake_pool_ctx(fake_pool):
    user = User(uuid4(), "Maya", "15555550100", "UTC")
    partner = User(uuid4(), "Ben", "15555550101", "UTC")
    topic_id = uuid4()
    other_topic_id = uuid4()
    dyad_id = uuid4()
    turn_id = uuid4()
    fake_pool.bot_turns[turn_id] = {
        "id": turn_id,
        "reasoning": "",
        "completed_at": None,
        "failure_reason": None,
    }
    fake_pool.users[user.id] = {
        "id": user.id,
        "name": user.name,
        "phone": user.phone,
        "timezone": user.timezone,
    }
    fake_pool.users[partner.id] = {
        "id": partner.id,
        "name": partner.name,
        "phone": partner.phone,
        "timezone": partner.timezone,
    }
    ctx = TurnContext(
        turn_id,
        fake_pool,
        user,
        partner,
        [uuid4()],
        current_step="read",
        bot_id="mediator",
        user_id=user.id,
        primary_topic_id=topic_id,
        dyad_id=dyad_id,
        bot_spec=SimpleNamespace(display_name="Veas"),
        extras={
            "hot_context_edge": {
                "message_id": str(_message_id(3)),
                "sent_at": datetime(2026, 6, 1, 10, 1, tzinfo=UTC).isoformat(),
            }
        },
    )
    return fake_pool, ctx, {
        "user": user,
        "partner": partner,
        "topic_id": topic_id,
        "other_topic_id": other_topic_id,
        "dyad_id": dyad_id,
    }


async def test_fake_pool_nav_handlers_use_searchable_view_with_stable_cursor_order(
    fake_pool_ctx,
) -> None:
    fake_pool, ctx, ids = fake_pool_ctx
    shared_minute = datetime(2026, 6, 1, 10, 0, tzinfo=UTC)
    edited_at = datetime(2026, 6, 1, 10, 3, tzinfo=UTC)

    _seed_message(
        fake_pool,
        message_id=_message_id(1),
        bot_id="mediator",
        topic_id=ids["topic_id"],
        dyad_id=ids["dyad_id"],
        thread_owner_user_id=ids["partner"].id,
        sender_id=ids["partner"].id,
        recipient_id=ids["user"].id,
        sent_at=shared_minute,
        content="m1",
    )
    _seed_message(
        fake_pool,
        message_id=_message_id(2),
        bot_id="mediator",
        topic_id=ids["topic_id"],
        dyad_id=ids["dyad_id"],
        thread_owner_user_id=ids["partner"].id,
        sender_id=ids["partner"].id,
        recipient_id=ids["user"].id,
        sent_at=shared_minute,
        content="m2",
        charge="notable",
        edited_at=edited_at,
        edit_history=[{"content": "m2 original"}],
    )
    _seed_message(
        fake_pool,
        message_id=_message_id(3),
        bot_id="mediator",
        topic_id=ids["topic_id"],
        dyad_id=ids["dyad_id"],
        thread_owner_user_id=ids["partner"].id,
        sender_id=ids["partner"].id,
        recipient_id=ids["user"].id,
        sent_at=datetime(2026, 6, 1, 10, 1, tzinfo=UTC),
        content="m3",
    )
    _seed_message(
        fake_pool,
        message_id=_message_id(4),
        bot_id="mediator",
        topic_id=ids["other_topic_id"],
        dyad_id=ids["dyad_id"],
        thread_owner_user_id=ids["partner"].id,
        sender_id=ids["partner"].id,
        recipient_id=ids["user"].id,
        sent_at=datetime(2026, 6, 1, 10, 2, tzinfo=UTC),
        content="m4-other-topic",
    )
    _seed_message(
        fake_pool,
        message_id=_message_id(5),
        bot_id="mediator",
        topic_id=ids["topic_id"],
        dyad_id=ids["dyad_id"],
        thread_owner_user_id=ids["partner"].id,
        sender_id=ids["partner"].id,
        recipient_id=ids["user"].id,
        sent_at=datetime(2026, 6, 1, 10, 3, tzinfo=UTC),
        content="hidden",
        partner_share="opt_out",
    )
    _seed_message(
        fake_pool,
        message_id=_message_id(6),
        bot_id="coach",
        topic_id=ids["topic_id"],
        dyad_id=ids["dyad_id"],
        thread_owner_user_id=ids["partner"].id,
        sender_id=ids["partner"].id,
        recipient_id=ids["user"].id,
        sent_at=datetime(2026, 6, 1, 10, 4, tzinfo=UTC),
        content="wrong-bot",
    )

    before = await read_tools.messages_before(ctx, MessagesBeforeInput(anchor="current", n=2))
    assert [hit.content for hit in before.messages] == ["m1", "m2"]
    assert before.messages[-1].charge == "notable"
    assert before.messages[-1].edit_history_original == "m2 original"

    after = await read_tools.messages_after(
        ctx,
        MessagesAfterInput(anchor=_message_id(2), n=2),
    )
    assert [hit.content for hit in after.messages] == ["m3"]

    thread = await read_tools.open_thread(ctx, OpenThreadInput(around=_message_id(3), n=5))
    assert [hit.content for hit in thread.messages] == ["m1", "m2", "m3", "m4-other-topic"]

    recent = await read_tools.topic_recent(ctx, TopicRecentInput(n=3))
    assert [hit.content for hit in recent.messages] == ["m3", "m2", "m1"]

    older = await read_tools.scroll(
        ctx,
        ScrollInput(cursor=thread.cursor, direction="older", n=2),
    )
    assert [hit.content for hit in older.messages] == ["m1", "m2"]
    assert all("mediator.v_searchable_messages" in sql for sql in fake_pool.fetch_sqls)


async def test_fake_pool_search_and_search_messages_honor_visibility_metadata_and_filters(
    fake_pool_ctx, monkeypatch
) -> None:
    fake_pool, ctx, ids = fake_pool_ctx
    fake_pool.fetch_sqls.clear()

    _seed_message(
        fake_pool,
        message_id=_message_id(10),
        bot_id="mediator",
        topic_id=ids["topic_id"],
        dyad_id=ids["dyad_id"],
        thread_owner_user_id=ids["partner"].id,
        sender_id=ids["partner"].id,
        recipient_id=ids["user"].id,
        sent_at=datetime(2026, 6, 1, 11, 0, tzinfo=UTC),
        content="Need to repair the plan before Friday.",
    )
    _seed_message(
        fake_pool,
        message_id=_message_id(11),
        bot_id="mediator",
        topic_id=ids["topic_id"],
        dyad_id=ids["dyad_id"],
        thread_owner_user_id=ids["partner"].id,
        sender_id=ids["partner"].id,
        recipient_id=ids["user"].id,
        sent_at=datetime(2026, 6, 1, 11, 5, tzinfo=UTC),
        content="",
        media_analysis={"explanation": "Screenshot of the repaired checklist."},
        charge="charged",
    )
    _seed_message(
        fake_pool,
        message_id=_message_id(12),
        bot_id="mediator",
        topic_id=ids["topic_id"],
        dyad_id=ids["dyad_id"],
        thread_owner_user_id=ids["partner"].id,
        sender_id=ids["partner"].id,
        recipient_id=ids["user"].id,
        sent_at=datetime(2026, 6, 1, 11, 10, tzinfo=UTC),
        content="hidden repair",
        partner_share="opt_out",
    )
    _seed_message(
        fake_pool,
        message_id=_message_id(13),
        bot_id="mediator",
        topic_id=ids["other_topic_id"],
        dyad_id=ids["dyad_id"],
        thread_owner_user_id=ids["partner"].id,
        sender_id=ids["partner"].id,
        recipient_id=ids["user"].id,
        sent_at=datetime(2026, 6, 1, 11, 15, tzinfo=UTC),
        content="other topic repair",
    )
    _seed_message(
        fake_pool,
        message_id=_message_id(14),
        bot_id="mediator",
        topic_id=ids["topic_id"],
        dyad_id=ids["dyad_id"],
        thread_owner_user_id=ids["user"].id,
        sender_id=ids["user"].id,
        recipient_id=ids["partner"].id,
        sent_at=datetime(2026, 6, 1, 11, 20, tzinfo=UTC),
        content='I wrote "repair checklist" on my side.',
    )
    _seed_message(
        fake_pool,
        message_id=_message_id(15),
        bot_id="mediator",
        topic_id=ids["topic_id"],
        dyad_id=ids["dyad_id"],
        thread_owner_user_id=ids["partner"].id,
        sender_id=ids["partner"].id,
        recipient_id=ids["user"].id,
        sent_at=datetime(2026, 6, 2, 11, 0, tzinfo=UTC),
        content="repair checklist tomorrow",
    )

    async def fake_hybrid_search(pool, request, **_kwargs):
        assert request.mode == "hybrid"
        assert request.topic_id == ids["topic_id"]
        return [
            RetrievalResult(
                message_id=_message_id(11),
                match_type="exact",
                rrf_score=0.8,
                keyword_rank=1,
                semantic_rank=None,
                semantic_degraded=False,
                sent_at=datetime(2026, 6, 1, 11, 5, tzinfo=UTC),
            ),
            RetrievalResult(
                message_id=_message_id(12),
                match_type="semantic",
                rrf_score=0.7,
                keyword_rank=None,
                semantic_rank=1,
                semantic_degraded=False,
                sent_at=datetime(2026, 6, 1, 11, 10, tzinfo=UTC),
            ),
        ]

    monkeypatch.setattr(read_tools, "hybrid_search", fake_hybrid_search)

    rich = await read_tools.search(
        ctx,
        SearchInput(query="repair checklist", mode="semantic", scope="topic", limit=5),
    )
    assert [hit.message_id for hit in rich.hits] == [_message_id(11)]
    assert rich.hits[0].snippet.startswith("[media] Screenshot")
    assert rich.hits[0].charge == "charged"

    legacy = await read_tools.search_messages(
        ctx,
        SearchMessagesInput(
            text_contains="checklist",
            partner_user_id=ids["partner"].id,
            local_day=datetime(2026, 6, 1, tzinfo=UTC).date(),
            limit=5,
        ),
    )
    assert [hit.id for hit in legacy.hits] == [_message_id(11)]
    assert legacy.hits[0].charge == "charged"

    current_scope = await read_tools.search_messages(
        ctx,
        SearchMessagesInput(
            text_contains="repair",
            date_range={
                "start": datetime(2026, 6, 1, 0, 0, tzinfo=UTC),
                "end": datetime(2026, 6, 2, 0, 0, tzinfo=UTC),
            },
            limit=10,
        ),
    )
    assert [hit.id for hit in current_scope.hits] == [
        _message_id(14),
        _message_id(11),
        _message_id(10),
    ]
    assert _message_id(12) not in {hit.id for hit in current_scope.hits}
    assert _message_id(13) not in {hit.id for hit in current_scope.hits}
    assert _message_id(15) not in {hit.id for hit in current_scope.hits}
    assert all("mediator.v_searchable_messages" in sql for sql in fake_pool.fetch_sqls)
    assert not any(
        "FROM mediator.messages" in sql and "deleted_at" not in sql
        for sql in fake_pool.fetch_sqls
    )
