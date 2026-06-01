from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.models.user import User
from app.services.retrieval import RetrievalResult
from app.services.tools import read_tools
from app.services.tools.write_tools import ToolCallRejected
from app.services.turn_context import TurnContext
from tool_schemas import SearchInput


class SearchPool:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = {row["message_id"]: dict(row) for row in rows}
        self.sql_calls: list[str] = []

    async def fetch(self, sql: str, *args):
        compact = " ".join(sql.split())
        self.sql_calls.append(compact)
        assert "JOIN mediator.v_searchable_messages m" in compact
        assert "WITH ranked_ids AS" in compact

        bot_id = args[0]
        viewer_id = args[1]
        participant_ids = set(args[2])
        idx = 3
        topic_id = None
        thread_owner_user_id = None
        if "AND m.topic_id = $" in compact:
            topic_id = args[idx]
            idx += 1
        if "AND m.thread_owner_user_id = $" in compact:
            thread_owner_user_id = args[idx]
            idx += 1
        ranked_ids = args[idx]

        out = []
        for message_id in ranked_ids:
            row = self.rows.get(message_id)
            if row is None:
                continue
            if row["bot_id"] != bot_id:
                continue
            if row["thread_owner_user_id"] not in participant_ids:
                continue
            if (
                row["thread_owner_user_id"] != viewer_id
                and row["thread_owner_partner_share"] != "opt_in"
            ):
                continue
            if topic_id is not None and row["topic_id"] != topic_id:
                continue
            if (
                thread_owner_user_id is not None
                and row["thread_owner_user_id"] != thread_owner_user_id
            ):
                continue
            out.append(dict(row))
        return out


@pytest.fixture
def search_ctx():
    user = User(uuid4(), "Maya", "15555550100", "UTC")
    partner = User(uuid4(), "Ben", "15555550101", "UTC")
    topic_id = uuid4()
    other_topic_id = uuid4()

    def row(
        minute: int,
        *,
        thread_owner_user_id: UUID,
        sender_id: UUID | None,
        topic: UUID,
        content: str,
        message_id: UUID | None = None,
        partner_share: str = "opt_in",
        bot_id: str = "mediator",
        direction: str = "inbound",
        media_analysis: dict[str, object] | None = None,
    ) -> dict[str, object]:
        return {
            "message_id": message_id or uuid4(),
            "sender_id": sender_id,
            "recipient_id": None,
            "thread_owner_user_id": thread_owner_user_id,
            "thread_owner_partner_share": partner_share,
            "bot_id": bot_id,
            "topic_id": topic,
            "dyad_id": uuid4(),
            "direction": direction,
            "sent_at": datetime(2026, 6, 1, 12, minute, tzinfo=UTC),
            "content": content,
            "media_analysis": media_analysis,
            "charge": "routine",
            "edited_at": None,
            "edit_history": None,
        }

    topic_both = row(
        0,
        thread_owner_user_id=partner.id,
        sender_id=partner.id,
        topic=topic_id,
        content="We should repair the plan before Friday.",
    )
    topic_semantic = row(
        1,
        thread_owner_user_id=partner.id,
        sender_id=partner.id,
        topic=topic_id,
        content="Let's revisit the approach and smooth the rough edge.",
    )
    topic_media = row(
        2,
        thread_owner_user_id=partner.id,
        sender_id=partner.id,
        topic=topic_id,
        content="",
        media_analysis={"kind": "image", "explanation": "Screenshot of the repaired checklist."},
    )
    user_thread_other_topic = row(
        3,
        thread_owner_user_id=user.id,
        sender_id=user.id,
        topic=other_topic_id,
        content="I repaired the bike and the routine held.",
    )
    quoted_exact = row(
        5,
        thread_owner_user_id=user.id,
        sender_id=user.id,
        topic=other_topic_id,
        content='She said "repair the plan" before the call.',
    )
    hidden_partner = row(
        4,
        thread_owner_user_id=partner.id,
        sender_id=partner.id,
        topic=topic_id,
        content="hidden",
        partner_share="opt_out",
    )

    pool = SearchPool(
        [
            topic_both,
            topic_semantic,
            topic_media,
            user_thread_other_topic,
            hidden_partner,
            quoted_exact,
        ]
    )
    ctx = TurnContext(
        uuid4(),
        pool,
        user,
        partner,
        [uuid4()],
        current_step="read",
        bot_id="mediator",
        user_id=user.id,
        primary_topic_id=topic_id,
        dyad_id=uuid4(),
        bot_spec=SimpleNamespace(display_name="Veas"),
    )
    return ctx, {
        "topic_both": topic_both,
        "topic_semantic": topic_semantic,
        "topic_media": topic_media,
        "user_thread_other_topic": user_thread_other_topic,
        "quoted_exact": quoted_exact,
    }


@pytest.mark.asyncio
async def test_search_semantic_topic_paginates_and_hydrates_in_rank_order(
    search_ctx, monkeypatch
) -> None:
    ctx, rows = search_ctx
    calls: list[tuple[str, int, UUID | None, UUID | None]] = []

    async def fake_hybrid_search(pool, request, **_kwargs):
        calls.append(
            (request.mode, request.limit, request.topic_id, request.thread_owner_user_id)
        )
        if request.limit == 3:
            return [
                RetrievalResult(
                    message_id=rows["topic_semantic"]["message_id"],
                    match_type="semantic",
                    rrf_score=0.8,
                    keyword_rank=None,
                    semantic_rank=1,
                    semantic_degraded=False,
                    sent_at=rows["topic_semantic"]["sent_at"],
                ),
                RetrievalResult(
                    message_id=rows["topic_both"]["message_id"],
                    match_type="both",
                    rrf_score=0.7,
                    keyword_rank=1,
                    semantic_rank=2,
                    semantic_degraded=False,
                    sent_at=rows["topic_both"]["sent_at"],
                ),
                RetrievalResult(
                    message_id=rows["topic_media"]["message_id"],
                    match_type="exact",
                    rrf_score=0.6,
                    keyword_rank=2,
                    semantic_rank=None,
                    semantic_degraded=False,
                    sent_at=rows["topic_media"]["sent_at"],
                ),
            ]
        assert request.limit == 5
        return [
            RetrievalResult(
                message_id=rows["topic_semantic"]["message_id"],
                match_type="semantic",
                rrf_score=0.8,
                keyword_rank=None,
                semantic_rank=1,
                semantic_degraded=False,
                sent_at=rows["topic_semantic"]["sent_at"],
            ),
            RetrievalResult(
                message_id=rows["topic_both"]["message_id"],
                match_type="both",
                rrf_score=0.7,
                keyword_rank=1,
                semantic_rank=2,
                semantic_degraded=False,
                sent_at=rows["topic_both"]["sent_at"],
            ),
            RetrievalResult(
                message_id=rows["topic_media"]["message_id"],
                match_type="exact",
                rrf_score=0.6,
                keyword_rank=2,
                semantic_rank=None,
                semantic_degraded=False,
                sent_at=rows["topic_media"]["sent_at"],
            ),
        ]

    monkeypatch.setattr(read_tools, "hybrid_search", fake_hybrid_search)

    first = await read_tools.search(
        ctx, SearchInput(query="repair plan", mode="semantic", scope="topic", limit=2)
    )

    assert [hit.message_id for hit in first.hits] == [
        rows["topic_semantic"]["message_id"],
        rows["topic_both"]["message_id"],
    ]
    assert [hit.match_type for hit in first.hits] == ["semantic", "both"]
    assert first.hits[0].why_matched == "Matched semantically related wording."
    assert "repair the plan" in first.hits[1].snippet
    assert read_tools._decode_nav_cursor(first.hits[0].cursor)["scope"] == "topic"
    assert first.next_cursor is not None
    decoded_page = read_tools._decode_search_page_cursor(first.next_cursor)
    assert decoded_page["rank_offset"] == 2
    assert decoded_page["scope"] == "topic"

    second = await read_tools.search(
        ctx,
        SearchInput(
            query="repair plan",
            mode="semantic",
            scope="topic",
            limit=2,
            cursor=first.next_cursor,
        ),
    )

    assert [hit.message_id for hit in second.hits] == [rows["topic_media"]["message_id"]]
    assert second.hits[0].snippet.startswith("[image] Screenshot")
    assert second.hits[0].match_type == "exact"
    assert second.next_cursor is None
    assert {
        hit.message_id for hit in first.hits
    }.isdisjoint({hit.message_id for hit in second.hits})
    assert first.truncated is True
    assert second.truncated is False
    assert calls == [
        ("hybrid", 3, ctx.primary_topic_id, None),
        ("hybrid", 5, ctx.primary_topic_id, None),
    ]
    assert all("mediator.v_searchable_messages" in sql for sql in ctx.pool.sql_calls)


@pytest.mark.asyncio
async def test_search_exact_thread_validates_cursor_and_uses_user_thread_scope(
    search_ctx, monkeypatch
) -> None:
    ctx, rows = search_ctx

    async def fake_hybrid_search(pool, request, **_kwargs):
        assert request.mode == "exact"
        assert request.topic_id is None
        assert request.thread_owner_user_id == ctx.user.id
        return [
            RetrievalResult(
                message_id=rows["user_thread_other_topic"]["message_id"],
                match_type="exact",
                rrf_score=None,
                keyword_rank=1,
                semantic_rank=None,
                semantic_degraded=False,
                sent_at=rows["user_thread_other_topic"]["sent_at"],
            ),
            RetrievalResult(
                message_id=rows["topic_both"]["message_id"],
                match_type="exact",
                rrf_score=None,
                keyword_rank=2,
                semantic_rank=None,
                semantic_degraded=False,
                sent_at=rows["topic_both"]["sent_at"],
            ),
        ]

    monkeypatch.setattr(read_tools, "hybrid_search", fake_hybrid_search)

    result = await read_tools.search(
        ctx, SearchInput(query="repair", mode="exact", scope="thread", limit=1)
    )

    assert [hit.message_id for hit in result.hits] == [
        rows["user_thread_other_topic"]["message_id"]
    ]
    assert result.next_cursor is not None
    assert read_tools._decode_nav_cursor(result.hits[0].cursor)["thread_owner_user_id"] == str(
        ctx.user.id
    )

    with pytest.raises(ToolCallRejected) as exc_info:
        await read_tools.search(
            ctx,
            SearchInput(
                query="different repair",
                mode="exact",
                scope="thread",
                limit=1,
                cursor=result.next_cursor,
            ),
        )
    assert exc_info.value.result["error_code"] == "search_cursor_mismatch"


@pytest.mark.asyncio
async def test_search_exact_preserves_quote_safe_snippets_and_partial_last_page(
    search_ctx, monkeypatch
) -> None:
    ctx, rows = search_ctx

    async def fake_hybrid_search(pool, request, **_kwargs):
        assert request.mode == "exact"
        return [
            RetrievalResult(
                message_id=rows["quoted_exact"]["message_id"],
                match_type="exact",
                rrf_score=None,
                keyword_rank=1,
                semantic_rank=None,
                semantic_degraded=False,
                sent_at=rows["quoted_exact"]["sent_at"],
            )
        ]

    monkeypatch.setattr(read_tools, "hybrid_search", fake_hybrid_search)

    result = await read_tools.search(
        ctx,
        SearchInput(query="repair the plan", mode="exact", scope="thread", limit=5),
    )

    assert [hit.message_id for hit in result.hits] == [rows["quoted_exact"]["message_id"]]
    assert result.hits[0].snippet == 'She said "repair the plan" before the call.'
    assert result.hits[0].why_matched == "Matched exact words in the message text."
    assert result.next_cursor is None
    assert result.truncated is False
