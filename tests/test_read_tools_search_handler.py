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
from tool_schemas import SearchInput, SourceMessagesInput


class SearchPool:
    def __init__(
        self,
        rows: list[dict[str, object]],
        source_rows: list[dict[str, object]] | None = None,
    ) -> None:
        self.rows = {row["message_id"]: dict(row) for row in rows}
        self.source_rows = {
            (row["source_type"], row["source_id"]): dict(row)
            for row in (source_rows or [])
        }
        self.sql_calls: list[str] = []
        self.message_hydration_ids: list[list[UUID]] = []
        self.source_hydration_keys: list[list[tuple[str, UUID]]] = []

    async def fetch(self, sql: str, *args):
        compact = " ".join(sql.split())
        self.sql_calls.append(compact)
        if "FROM mediator.memories" in compact:
            source_ids = set(args[0])
            return [
                {
                    "source_type": "memory",
                    "source_id": row["source_id"],
                    "status": row.get("status", "active"),
                    "visibility": row.get("visibility", "private"),
                    "bot_id": row.get("bot_id", "mediator"),
                    "content": row.get("content"),
                }
                for row in self.source_rows.values()
                if row.get("source_type") == "memory" and row["source_id"] in source_ids
            ]
        if "FROM mediator.observations" in compact:
            source_ids = set(args[0])
            return [
                {
                    "source_type": "observation",
                    "source_id": row["source_id"],
                    "status": row.get("status", "active"),
                    "bot_id": row.get("bot_id", "mediator"),
                    "supporting_message_ids": row.get("supporting_message_ids", []),
                    "content": row.get("content"),
                }
                for row in self.source_rows.values()
                if row.get("source_type") == "observation" and row["source_id"] in source_ids
            ]
        if "FROM mediator.distillations" in compact:
            source_ids = set(args[0])
            return [
                {
                    "source_type": "distillation",
                    "source_id": row["source_id"],
                    "status": row.get("status", "active"),
                    "visibility": row.get("visibility", "private"),
                    "bot_id": row.get("bot_id", "mediator"),
                    "supporting_message_ids": row.get("supporting_message_ids", []),
                    "content": row.get("content"),
                }
                for row in self.source_rows.values()
                if row.get("source_type") == "distillation" and row["source_id"] in source_ids
            ]
        if "FROM mediator.conversation_artifacts" in compact:
            source_ids = set(args[0])
            return [
                {
                    "source_type": "artifact",
                    "source_id": row["source_id"],
                    "bot_id": row.get("bot_id", "mediator"),
                    "artifact_type": row.get("artifact_type", "live_prep_brief"),
                    "deleted_at": row.get("deleted_at"),
                }
                for row in self.source_rows.values()
                if row.get("source_type") == "artifact" and row["source_id"] in source_ids
            ]
        if "JOIN mediator.v_searchable_content sc" in compact:
            assert "WITH ranked_sources AS" in compact
            source_types = args[-2]
            source_ids = args[-1]
            keys = list(zip(source_types, source_ids, strict=True))
            self.source_hydration_keys.append(keys)
            out = []
            for key in keys:
                row = self.source_rows.get(key)
                if row is None:
                    continue
                if row.get("bot_id") != args[0] and not (
                    row.get("source_type") == "distillation" and row.get("bot_id") is None
                ):
                    continue
                if "sc.topic_id = $" in compact and row["topic_id"] != args[1]:
                    continue
                if (
                    "sc.thread_owner_user_id = $" in compact
                    and row["thread_owner_user_id"] != args[1]
                ):
                    continue
                out.append(dict(row))
            return out

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
        self.message_hydration_ids.append(list(ranked_ids))

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
    memory_id = uuid4()
    memory_row = {
        "source_type": "memory",
        "source_id": memory_id,
        "message_id": None,
        "sender_id": user.id,
        "recipient_id": None,
        "thread_owner_user_id": user.id,
        "thread_owner_partner_share": None,
        "bot_id": "mediator",
        "topic_id": topic_id,
        "dyad_id": None,
        "sent_at": datetime(2026, 6, 1, 12, 6, tzinfo=UTC),
        "source_created_at": datetime(2026, 6, 1, 12, 6, tzinfo=UTC),
        "source_updated_at": datetime(2026, 6, 1, 12, 7, tzinfo=UTC),
        "sort_at": datetime(2026, 6, 1, 12, 7, tzinfo=UTC),
        "content": "Memory says the repaired plan stuck.",
    }

    pool = SearchPool(
        [
            topic_both,
            topic_semantic,
            topic_media,
            user_thread_other_topic,
            hidden_partner,
            quoted_exact,
        ],
        source_rows=[memory_row],
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
        "hidden_partner": hidden_partner,
        "quoted_exact": quoted_exact,
        "memory": memory_row,
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


@pytest.mark.asyncio
async def test_search_semantic_hydrates_non_message_retrieval_results_by_source_key(
    search_ctx, monkeypatch
) -> None:
    ctx, rows = search_ctx
    memory_id = rows["memory"]["source_id"]

    async def fake_hybrid_search(pool, request, **_kwargs):
        assert request.mode == "hybrid"
        return [
            RetrievalResult(
                message_id=None,
                source_type="memory",
                source_id=memory_id,
                match_type="semantic",
                rrf_score=0.9,
                keyword_rank=None,
                semantic_rank=1,
                semantic_degraded=False,
            ),
            RetrievalResult(
                message_id=rows["topic_semantic"]["message_id"],
                match_type="semantic",
                rrf_score=0.8,
                keyword_rank=None,
                semantic_rank=2,
                semantic_degraded=False,
                sent_at=rows["topic_semantic"]["sent_at"],
            ),
        ]

    monkeypatch.setattr(read_tools, "hybrid_search", fake_hybrid_search)

    result = await read_tools.search(
        ctx, SearchInput(query="repair plan", mode="semantic", scope="topic", limit=5)
    )

    assert [(hit.source_type, hit.source_id, hit.message_id) for hit in result.hits] == [
        ("memory", memory_id, None),
        ("message", rows["topic_semantic"]["message_id"], rows["topic_semantic"]["message_id"]),
    ]
    assert result.hits[0].match_type == "semantic"
    assert result.hits[0].snippet == "Memory says the repaired plan stuck."
    assert any("mediator.v_searchable_content" in sql for sql in ctx.pool.sql_calls)
    assert any("mediator.v_searchable_messages" in sql for sql in ctx.pool.sql_calls)
    assert read_tools._decode_cursor(
        result.hits[0].cursor, expected_kind="search_source"
    )["source_type"] == "memory"
    assert ctx.pool.message_hydration_ids == [[rows["topic_semantic"]["message_id"]]]
    assert ctx.pool.source_hydration_keys == [[("memory", memory_id)]]
    assert result.next_cursor is None
    assert result.truncated is False


async def test_search_suppresses_unsafe_non_message_hits_and_keeps_rank_cursor(
    search_ctx, monkeypatch
) -> None:
    ctx, rows = search_ctx
    visible_observation_id = uuid4()
    shareable_memory_id = uuid4()
    hidden_distillation_id = uuid4()
    unknown_source_id = uuid4()
    ctx.pool.source_rows.update(
        {
            ("observation", visible_observation_id): {
                "source_type": "observation",
                "source_id": visible_observation_id,
                "message_id": None,
                "sender_id": rows["topic_both"]["sender_id"],
                "recipient_id": None,
                "thread_owner_user_id": rows["topic_both"]["thread_owner_user_id"],
                "thread_owner_partner_share": None,
                "bot_id": "mediator",
                "topic_id": rows["topic_both"]["topic_id"],
                "dyad_id": None,
                "sent_at": datetime(2026, 6, 1, 12, 8, tzinfo=UTC),
                "source_created_at": datetime(2026, 6, 1, 12, 8, tzinfo=UTC),
                "source_updated_at": datetime(2026, 6, 1, 12, 8, tzinfo=UTC),
                "sort_at": datetime(2026, 6, 1, 12, 8, tzinfo=UTC),
                "content": "Observation with visible support can mention the repair.",
                "supporting_message_ids": [rows["topic_both"]["message_id"]],
            },
            ("memory", shareable_memory_id): {
                "source_type": "memory",
                "source_id": shareable_memory_id,
                "message_id": None,
                "sender_id": rows["topic_both"]["sender_id"],
                "recipient_id": None,
                "thread_owner_user_id": rows["topic_both"]["thread_owner_user_id"],
                "thread_owner_partner_share": None,
                "bot_id": "mediator",
                "topic_id": rows["topic_both"]["topic_id"],
                "dyad_id": None,
                "sent_at": datetime(2026, 6, 1, 12, 9, tzinfo=UTC),
                "source_created_at": datetime(2026, 6, 1, 12, 9, tzinfo=UTC),
                "source_updated_at": datetime(2026, 6, 1, 12, 9, tzinfo=UTC),
                "sort_at": datetime(2026, 6, 1, 12, 9, tzinfo=UTC),
                "content": "Shareable memory should not render raw content.",
                "visibility": "dyad_shareable",
            },
            ("distillation", hidden_distillation_id): {
                "source_type": "distillation",
                "source_id": hidden_distillation_id,
                "message_id": None,
                "sender_id": rows["hidden_partner"]["sender_id"],
                "recipient_id": None,
                "thread_owner_user_id": rows["hidden_partner"]["thread_owner_user_id"],
                "thread_owner_partner_share": None,
                "bot_id": "mediator",
                "topic_id": rows["hidden_partner"]["topic_id"],
                "dyad_id": None,
                "sent_at": datetime(2026, 6, 1, 12, 10, tzinfo=UTC),
                "source_created_at": datetime(2026, 6, 1, 12, 10, tzinfo=UTC),
                "source_updated_at": datetime(2026, 6, 1, 12, 10, tzinfo=UTC),
                "sort_at": datetime(2026, 6, 1, 12, 10, tzinfo=UTC),
                "content": "Distillation with hidden support should not render.",
                "visibility": "private",
                "supporting_message_ids": [rows["hidden_partner"]["message_id"]],
            },
            ("unknown", unknown_source_id): {
                "source_type": "unknown",
                "source_id": unknown_source_id,
                "message_id": None,
                "sender_id": rows["topic_both"]["sender_id"],
                "recipient_id": None,
                "thread_owner_user_id": rows["topic_both"]["thread_owner_user_id"],
                "thread_owner_partner_share": None,
                "bot_id": "mediator",
                "topic_id": rows["topic_both"]["topic_id"],
                "dyad_id": None,
                "sent_at": datetime(2026, 6, 1, 12, 11, tzinfo=UTC),
                "source_created_at": datetime(2026, 6, 1, 12, 11, tzinfo=UTC),
                "source_updated_at": datetime(2026, 6, 1, 12, 11, tzinfo=UTC),
                "sort_at": datetime(2026, 6, 1, 12, 11, tzinfo=UTC),
                "content": "Unknown source should not render.",
            },
        }
    )

    async def fake_hybrid_search(pool, request, **_kwargs):
        return [
            RetrievalResult(
                message_id=None,
                source_type="memory",
                source_id=shareable_memory_id,
                match_type="semantic",
                rrf_score=1.0,
                keyword_rank=None,
                semantic_rank=1,
                semantic_degraded=False,
            ),
            RetrievalResult(
                message_id=None,
                source_type="observation",
                source_id=visible_observation_id,
                match_type="semantic",
                rrf_score=0.9,
                keyword_rank=None,
                semantic_rank=2,
                semantic_degraded=False,
            ),
            RetrievalResult(
                message_id=None,
                source_type="distillation",
                source_id=hidden_distillation_id,
                match_type="semantic",
                rrf_score=0.8,
                keyword_rank=None,
                semantic_rank=3,
                semantic_degraded=False,
            ),
            RetrievalResult(
                message_id=None,
                source_type="unknown",
                source_id=unknown_source_id,
                match_type="semantic",
                rrf_score=0.7,
                keyword_rank=None,
                semantic_rank=4,
                semantic_degraded=False,
            ),
            RetrievalResult(
                message_id=rows["topic_semantic"]["message_id"],
                match_type="semantic",
                rrf_score=0.6,
                keyword_rank=None,
                semantic_rank=5,
                semantic_degraded=False,
            ),
        ]

    monkeypatch.setattr(read_tools, "hybrid_search", fake_hybrid_search)

    result = await read_tools.search(
        ctx, SearchInput(query="repair", mode="semantic", scope="topic", limit=3)
    )

    assert [(hit.source_type, hit.source_id) for hit in result.hits] == [
        ("observation", visible_observation_id)
    ]
    assert result.hits[0].speaker.label == "Observation"
    assert "visible support" in result.hits[0].snippet
    assert result.truncated is True
    assert read_tools._decode_search_page_cursor(result.next_cursor)["rank_offset"] == 3
    assert any("mediator.v_searchable_content" in sql for sql in ctx.pool.sql_calls)
    assert any("mediator.v_searchable_messages" in sql for sql in ctx.pool.sql_calls)
    assert ctx.pool.source_hydration_keys == [
            [
                ("memory", shareable_memory_id),
                ("observation", visible_observation_id),
                ("distillation", hidden_distillation_id),
            ]
        ]
    assert ctx.pool.message_hydration_ids == [
        [
            rows["topic_both"]["message_id"],
            rows["hidden_partner"]["message_id"],
        ]
    ]


@pytest.mark.asyncio
async def test_search_renders_only_safe_source_hits_with_provenance_visibility(
    search_ctx, monkeypatch
) -> None:
    ctx, rows = search_ctx
    observation_id = uuid4()
    distillation_id = uuid4()
    unsafe_distillation_id = uuid4()
    unhydrated_memory_id = uuid4()
    artifact_id = uuid4()
    ctx.pool.source_rows.update(
        {
            ("distillation", distillation_id): {
                "source_type": "distillation",
                "source_id": distillation_id,
                "message_id": None,
                "sender_id": rows["topic_both"]["sender_id"],
                "recipient_id": None,
                "thread_owner_user_id": rows["topic_both"]["thread_owner_user_id"],
                "thread_owner_partner_share": None,
                "bot_id": "mediator",
                "topic_id": rows["topic_both"]["topic_id"],
                "dyad_id": None,
                "sent_at": datetime(2026, 6, 1, 12, 12, tzinfo=UTC),
                "source_created_at": datetime(2026, 6, 1, 12, 12, tzinfo=UTC),
                "source_updated_at": datetime(2026, 6, 1, 12, 12, tzinfo=UTC),
                "sort_at": datetime(2026, 6, 1, 12, 12, tzinfo=UTC),
                "content": "Distillation says repair follow-through is visible.",
                "visibility": "private",
                "supporting_message_ids": [rows["topic_both"]["message_id"]],
            },
            ("observation", observation_id): {
                "source_type": "observation",
                "source_id": observation_id,
                "message_id": None,
                "sender_id": rows["topic_both"]["sender_id"],
                "recipient_id": None,
                "thread_owner_user_id": rows["topic_both"]["thread_owner_user_id"],
                "thread_owner_partner_share": None,
                "bot_id": "mediator",
                "topic_id": rows["topic_both"]["topic_id"],
                "dyad_id": None,
                "sent_at": datetime(2026, 6, 1, 12, 13, tzinfo=UTC),
                "source_created_at": datetime(2026, 6, 1, 12, 13, tzinfo=UTC),
                "source_updated_at": datetime(2026, 6, 1, 12, 13, tzinfo=UTC),
                "sort_at": datetime(2026, 6, 1, 12, 13, tzinfo=UTC),
                "content": "Observation says repair follow-through is visible too.",
                "supporting_message_ids": [rows["topic_both"]["message_id"]],
            },
            ("distillation", unsafe_distillation_id): {
                "source_type": "distillation",
                "source_id": unsafe_distillation_id,
                "message_id": None,
                "sender_id": rows["hidden_partner"]["sender_id"],
                "recipient_id": None,
                "thread_owner_user_id": rows["hidden_partner"]["thread_owner_user_id"],
                "thread_owner_partner_share": None,
                "bot_id": "mediator",
                "topic_id": rows["hidden_partner"]["topic_id"],
                "dyad_id": None,
                "sent_at": datetime(2026, 6, 1, 12, 14, tzinfo=UTC),
                "source_created_at": datetime(2026, 6, 1, 12, 14, tzinfo=UTC),
                "source_updated_at": datetime(2026, 6, 1, 12, 14, tzinfo=UTC),
                "sort_at": datetime(2026, 6, 1, 12, 14, tzinfo=UTC),
                "content": "Hidden distillation must not leak.",
                "visibility": "private",
                "supporting_message_ids": [rows["hidden_partner"]["message_id"]],
            },
            ("artifact", artifact_id): {
                "source_type": "artifact",
                "source_id": artifact_id,
                "message_id": None,
                "sender_id": rows["topic_both"]["sender_id"],
                "recipient_id": None,
                "thread_owner_user_id": rows["topic_both"]["thread_owner_user_id"],
                "thread_owner_partner_share": None,
                "bot_id": "mediator",
                "topic_id": rows["topic_both"]["topic_id"],
                "dyad_id": None,
                "sent_at": datetime(2026, 6, 1, 12, 15, tzinfo=UTC),
                "source_created_at": datetime(2026, 6, 1, 12, 15, tzinfo=UTC),
                "source_updated_at": datetime(2026, 6, 1, 12, 15, tzinfo=UTC),
                "sort_at": datetime(2026, 6, 1, 12, 15, tzinfo=UTC),
                "content": "Artifact safely summarizes the repair plan.",
                "artifact_type": "live_prep_brief",
            },
        }
    )

    async def fake_hybrid_search(pool, request, **_kwargs):
        return [
            RetrievalResult(
                message_id=None,
                source_type="distillation",
                source_id=distillation_id,
                match_type="semantic",
                rrf_score=1.0,
                keyword_rank=None,
                semantic_rank=1,
                semantic_degraded=False,
            ),
            RetrievalResult(
                message_id=None,
                source_type="memory",
                source_id=unhydrated_memory_id,
                match_type="semantic",
                rrf_score=0.9,
                keyword_rank=None,
                semantic_rank=2,
                semantic_degraded=False,
            ),
            RetrievalResult(
                message_id=None,
                source_type="observation",
                source_id=observation_id,
                match_type="semantic",
                rrf_score=0.8,
                keyword_rank=None,
                semantic_rank=3,
                semantic_degraded=False,
            ),
            RetrievalResult(
                message_id=None,
                source_type="distillation",
                source_id=unsafe_distillation_id,
                match_type="semantic",
                rrf_score=0.7,
                keyword_rank=None,
                semantic_rank=4,
                semantic_degraded=False,
            ),
            RetrievalResult(
                message_id=None,
                source_type="artifact",
                source_id=artifact_id,
                match_type="semantic",
                rrf_score=0.6,
                keyword_rank=None,
                semantic_rank=5,
                semantic_degraded=False,
            ),
        ]

    monkeypatch.setattr(read_tools, "hybrid_search", fake_hybrid_search)

    result = await read_tools.search(
        ctx, SearchInput(query="repair", mode="semantic", scope="topic", limit=5)
    )

    assert [(hit.source_type, hit.source_id, hit.message_id) for hit in result.hits] == [
        ("distillation", distillation_id, None),
        ("observation", observation_id, None),
        ("artifact", artifact_id, None),
    ]
    assert [hit.speaker.label for hit in result.hits] == [
        "Distillation",
        "Observation",
        "Artifact",
    ]
    assert [hit.snippet for hit in result.hits] == [
        "Distillation says repair follow-through is visible.",
        "Observation says repair follow-through is visible too.",
        "Artifact safely summarizes the repair plan.",
    ]
    assert any("mediator.v_searchable_content" in sql for sql in ctx.pool.sql_calls)
    assert any("mediator.v_searchable_messages" in sql for sql in ctx.pool.sql_calls)
    assert ctx.pool.source_hydration_keys == [
        [
            ("distillation", distillation_id),
            ("memory", unhydrated_memory_id),
            ("observation", observation_id),
            ("distillation", unsafe_distillation_id),
            ("artifact", artifact_id),
        ]
    ]
    assert ctx.pool.message_hydration_ids == [
        [
            rows["topic_both"]["message_id"],
            rows["hidden_partner"]["message_id"],
        ]
    ]
    assert result.next_cursor is None
    assert result.truncated is False


@pytest.mark.asyncio
async def test_source_messages_returns_visible_supporting_messages(search_ctx) -> None:
    ctx, rows = search_ctx
    distillation_id = uuid4()
    ctx.pool.source_rows[("distillation", distillation_id)] = {
        "source_type": "distillation",
        "source_id": distillation_id,
        "message_id": None,
        "sender_id": rows["topic_both"]["sender_id"],
        "recipient_id": None,
        "thread_owner_user_id": rows["topic_both"]["thread_owner_user_id"],
        "thread_owner_partner_share": None,
        "bot_id": "mediator",
        "topic_id": rows["topic_both"]["topic_id"],
        "dyad_id": None,
        "sent_at": datetime(2026, 6, 1, 12, 16, tzinfo=UTC),
        "source_created_at": datetime(2026, 6, 1, 12, 16, tzinfo=UTC),
        "source_updated_at": datetime(2026, 6, 1, 12, 16, tzinfo=UTC),
        "sort_at": datetime(2026, 6, 1, 12, 16, tzinfo=UTC),
        "content": "Distillation linked to visible and hidden support.",
        "visibility": "private",
        "supporting_message_ids": [
            rows["topic_both"]["message_id"],
            rows["hidden_partner"]["message_id"],
        ],
    }

    result = await read_tools.source_messages(
        ctx,
        SourceMessagesInput(source_type="distillation", source_id=distillation_id),
    )

    assert result.status == "ok"
    assert [message.message_id for message in result.messages] == [
        rows["topic_both"]["message_id"]
    ]
    assert ctx.pool.source_hydration_keys == [[("distillation", distillation_id)]]
    assert ctx.pool.message_hydration_ids == [
        [
            rows["topic_both"]["message_id"],
            rows["hidden_partner"]["message_id"],
        ]
    ]


@pytest.mark.asyncio
async def test_source_messages_supports_observation_with_visibility_filtering(
    search_ctx,
) -> None:
    ctx, rows = search_ctx
    observation_id = uuid4()
    ctx.pool.source_rows[("observation", observation_id)] = {
        "source_type": "observation",
        "source_id": observation_id,
        "message_id": None,
        "sender_id": rows["topic_both"]["sender_id"],
        "recipient_id": None,
        "thread_owner_user_id": rows["topic_both"]["thread_owner_user_id"],
        "thread_owner_partner_share": None,
        "bot_id": "mediator",
        "topic_id": rows["topic_both"]["topic_id"],
        "dyad_id": None,
        "sent_at": datetime(2026, 6, 1, 12, 17, tzinfo=UTC),
        "source_created_at": datetime(2026, 6, 1, 12, 17, tzinfo=UTC),
        "source_updated_at": datetime(2026, 6, 1, 12, 17, tzinfo=UTC),
        "sort_at": datetime(2026, 6, 1, 12, 17, tzinfo=UTC),
        "content": "Observation linked to one visible and one hidden message.",
        "supporting_message_ids": [
            rows["topic_both"]["message_id"],
            rows["hidden_partner"]["message_id"],
        ],
    }

    result = await read_tools.source_messages(
        ctx,
        SourceMessagesInput(source_type="observation", source_id=observation_id),
    )

    assert result.status == "ok"
    assert [message.message_id for message in result.messages] == [
        rows["topic_both"]["message_id"]
    ]
    assert ctx.pool.source_hydration_keys == [[("observation", observation_id)]]
    assert ctx.pool.message_hydration_ids == [
        [
            rows["topic_both"]["message_id"],
            rows["hidden_partner"]["message_id"],
        ]
    ]


@pytest.mark.asyncio
async def test_source_messages_returns_explicit_unsupported_and_no_link(
    search_ctx,
) -> None:
    ctx, rows = search_ctx
    observation_id = uuid4()
    ctx.pool.source_rows[("observation", observation_id)] = {
        "source_type": "observation",
        "source_id": observation_id,
        "message_id": None,
        "sender_id": rows["topic_both"]["sender_id"],
        "recipient_id": None,
        "thread_owner_user_id": rows["topic_both"]["thread_owner_user_id"],
        "thread_owner_partner_share": None,
        "bot_id": "mediator",
        "topic_id": rows["topic_both"]["topic_id"],
        "dyad_id": None,
        "sent_at": datetime(2026, 6, 1, 12, 17, tzinfo=UTC),
        "source_created_at": datetime(2026, 6, 1, 12, 17, tzinfo=UTC),
        "source_updated_at": datetime(2026, 6, 1, 12, 17, tzinfo=UTC),
        "sort_at": datetime(2026, 6, 1, 12, 17, tzinfo=UTC),
        "content": "Observation without supporting messages.",
        "supporting_message_ids": [],
    }

    memory = await read_tools.source_messages(
        ctx,
        SourceMessagesInput(source_type="memory", source_id=rows["memory"]["source_id"]),
    )
    artifact = await read_tools.source_messages(
        ctx,
        SourceMessagesInput(source_type="artifact", source_id=uuid4()),
    )
    unknown = await read_tools.source_messages(
        ctx,
        SourceMessagesInput(source_type="unknown", source_id=uuid4()),
    )
    no_link = await read_tools.source_messages(
        ctx,
        SourceMessagesInput(source_type="observation", source_id=observation_id),
    )

    assert memory.status == "unsupported"
    assert memory.reason == "memory sources do not link to supporting messages."
    assert artifact.status == "unsupported"
    assert artifact.reason == "artifact sources do not link to supporting messages."
    assert unknown.status == "unsupported"
    assert unknown.reason == "source_type is not supported by source_messages."
    assert no_link.status == "no_link"
    assert no_link.messages == []
