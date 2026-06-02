from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.embed_jobs import enqueue_message_drop_embedding_job, enqueue_message_embed_job
from app.services.embed_worker import EmbedJobWorker
from app.services.embeddings import DeterministicFakeEmbedder, content_hash
from app.services.retrieval import RetrievalQuery, hybrid_search
from tests.conftest import FakePool

pytestmark = pytest.mark.anyio


def _settings(**overrides):
    base = {
        "embedding_worker_batch_size": 10,
        "embedding_worker_poll_interval_s": 0.01,
        "query_embed_timeout_s": 0.5,
        "query_embed_cache_ttl_s": 300,
        "query_embed_cache_max_entries": 1024,
        "retrieval_hnsw_ef_search": 32,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _message(
    *,
    message_id,
    sender_id,
    recipient_id,
    topic_id,
    dyad_id,
    text: str,
    suppressed=False,
    share="private",
    sent_at=None,
):
    return {
        "id": message_id,
        "direction": "inbound",
        "sender_id": sender_id,
        "recipient_id": recipient_id,
        "bot_id": "mediator",
        "topic_id": topic_id,
        "dyad_id": dyad_id,
        "thread_owner_user_id": sender_id,
        "thread_owner_partner_share": share,
        "content": text,
        "media_analysis": {"summary": "media summary"},
        "sent_at": sent_at or datetime(2026, 6, 1, 12, tzinfo=UTC),
        "deleted_at": None,
        "search_suppressed_at": (
            datetime(2026, 6, 1, 12, tzinfo=UTC) if suppressed else None
        ),
    }


async def test_fake_pool_supports_embed_jobs_and_worker_embedding_lifecycle():
    now = datetime(2026, 6, 1, 12, tzinfo=UTC)
    pool = FakePool()
    message_id = uuid4()
    user_id = uuid4()
    topic_id = uuid4()
    text = "Deploy crash\nmedia summary\n"
    pool.messages[message_id] = _message(
        message_id=message_id,
        sender_id=user_id,
        recipient_id=uuid4(),
        topic_id=topic_id,
        dyad_id=uuid4(),
        text="Deploy crash",
    )

    created = await enqueue_message_embed_job(
        pool,
        message_id=message_id,
        content_hash=content_hash(text),
        model="deterministic-fake",
        dimension=4,
        now=now,
    )

    result = await EmbedJobWorker(
        pool,
        settings=_settings(),
        embedder=DeterministicFakeEmbedder(dimension=4),
        worker_id="fixture-worker",
    ).run_once(now=now)

    assert created.action == "created"
    assert result.claimed == 1
    assert result.embedded == 1
    assert pool.embed_jobs[created.job.id]["status"] == "succeeded"
    assert pool.message_embeddings[message_id]["model"] == "deterministic-fake"
    assert pool.message_embeddings[message_id]["dimension"] == 4
    assert pool.message_embeddings[message_id]["content_hash"] == content_hash(text)


async def test_fake_pool_supports_drop_jobs_and_search_suppression_cleanup():
    now = datetime(2026, 6, 1, 12, tzinfo=UTC)
    pool = FakePool()
    message_id = uuid4()
    pool.messages[message_id] = _message(
        message_id=message_id,
        sender_id=uuid4(),
        recipient_id=uuid4(),
        topic_id=uuid4(),
        dyad_id=uuid4(),
        text="suppressed",
        suppressed=True,
    )
    pool.message_embeddings[message_id] = {"content_hash": "old"}

    job = await enqueue_message_drop_embedding_job(pool, message_id=message_id, now=now)
    result = await EmbedJobWorker(
        pool,
        settings=_settings(),
        embedder=DeterministicFakeEmbedder(dimension=4),
        worker_id="fixture-worker",
    ).run_once(now=now)

    assert result.dropped == 1
    assert pool.embed_jobs[job.job.id]["status"] == "succeeded"
    assert message_id not in pool.message_embeddings


async def test_fake_pool_retrieval_exact_filters_suppressed_and_visibility_rows():
    pool = FakePool()
    viewer_id = uuid4()
    partner_id = uuid4()
    topic_id = uuid4()
    dyad_id = uuid4()
    visible_id = uuid4()
    suppressed_id = uuid4()
    wrong_topic_id = uuid4()
    pool.messages[visible_id] = _message(
        message_id=visible_id,
        sender_id=viewer_id,
        recipient_id=partner_id,
        topic_id=topic_id,
        dyad_id=dyad_id,
        text="deploy crash root cause",
        sent_at=datetime(2026, 6, 1, 12, tzinfo=UTC),
    )
    pool.messages[suppressed_id] = _message(
        message_id=suppressed_id,
        sender_id=viewer_id,
        recipient_id=partner_id,
        topic_id=topic_id,
        dyad_id=dyad_id,
        text="deploy crash suppressed",
        suppressed=True,
        sent_at=datetime(2026, 6, 1, 13, tzinfo=UTC),
    )
    pool.messages[wrong_topic_id] = _message(
        message_id=wrong_topic_id,
        sender_id=viewer_id,
        recipient_id=partner_id,
        topic_id=uuid4(),
        dyad_id=dyad_id,
        text="deploy crash wrong topic",
        sent_at=datetime(2026, 6, 1, 14, tzinfo=UTC),
    )

    results = await hybrid_search(
        pool,
        RetrievalQuery(
            query="deploy crash",
            viewer_user_id=viewer_id,
            partner_user_id=partner_id,
            bot_id="mediator",
            topic_id=topic_id,
            dyad_id=dyad_id,
            mode="exact",
            limit=10,
        ),
        settings=_settings(),
    )

    assert [row.message_id for row in results] == [visible_id]


async def test_fake_pool_transaction_spy_records_hnsw_and_ann_fetch_together():
    pool = FakePool()
    viewer_id = uuid4()
    partner_id = uuid4()
    topic_id = uuid4()
    dyad_id = uuid4()
    message_id = uuid4()
    pool.messages[message_id] = _message(
        message_id=message_id,
        sender_id=viewer_id,
        recipient_id=partner_id,
        topic_id=topic_id,
        dyad_id=dyad_id,
        text="deploy crash semantic",
    )
    pool.message_embeddings[message_id] = {
        "message_id": message_id,
        "embedding": "[1,0,0,0]",
        "model": "deterministic-fake",
        "dimension": 4,
        "content_hash": "hash",
        "embedded_at": datetime(2026, 6, 1, 12, tzinfo=UTC),
    }

    await hybrid_search(
        pool,
        RetrievalQuery(
            query="deploy crash",
            viewer_user_id=viewer_id,
            partner_user_id=partner_id,
            bot_id="mediator",
            topic_id=topic_id,
            dyad_id=dyad_id,
            mode="hybrid",
            limit=10,
        ),
        embedder=DeterministicFakeEmbedder(dimension=4),
        settings=_settings(retrieval_hnsw_ef_search=77),
    )

    transactional = [
        event for event in pool.connection_events if event[1] > 0 and event[0] in {"execute", "fetch"}
    ]
    assert pool.transaction_entries == 1
    assert transactional[0][2] == "SET LOCAL hnsw.ef_search = 77"
    assert "FROM mediator.content_embeddings e" in transactional[1][2]
    assert "JOIN mediator.v_searchable_content sc" in transactional[1][2]
    assert "JOIN mediator.v_searchable_messages m" not in transactional[1][2]


async def test_fake_pool_searchable_content_projection_excludes_shareable_non_messages():
    pool = FakePool()
    private_memory_id = uuid4()
    shareable_memory_id = uuid4()
    private_distillation_id = uuid4()
    shareable_distillation_id = uuid4()

    pool.memories[private_memory_id] = {
        "id": private_memory_id,
        "content": "private memory",
        "status": "active",
        "visibility": "private",
    }
    pool.memories[shareable_memory_id] = {
        "id": shareable_memory_id,
        "content": "shareable memory",
        "status": "active",
        "visibility": "dyad_shareable",
    }
    pool.distillations[private_distillation_id] = {
        "id": private_distillation_id,
        "summary": "private distillation",
        "status": "active",
        "visibility": "private",
    }
    pool.distillations[shareable_distillation_id] = {
        "id": shareable_distillation_id,
        "summary": "shareable distillation",
        "status": "active",
        "visibility": "dyad_shareable",
    }

    sql = (
        "SELECT source_type, source_id, message_id, canonical_text "
        "FROM mediator.v_searchable_content WHERE source_type=$1 AND source_id=$2"
    )

    assert await pool.fetchrow(sql, "memory", private_memory_id) == {
        "source_type": "memory",
        "source_id": private_memory_id,
        "message_id": None,
        "canonical_text": "private memory",
    }
    assert await pool.fetchrow(sql, "distillation", private_distillation_id) == {
        "source_type": "distillation",
        "source_id": private_distillation_id,
        "message_id": None,
        "canonical_text": "private distillation",
    }
    assert await pool.fetchrow(sql, "memory", shareable_memory_id) is None
    assert await pool.fetchrow(sql, "distillation", shareable_distillation_id) is None


async def test_fake_pool_searchable_content_projection_matches_m1_source_shapes():
    pool = FakePool()
    now = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
    viewer_id = uuid4()
    recipient_id = uuid4()
    topic_id = uuid4()
    dyad_id = uuid4()
    message_id = uuid4()
    memory_id = uuid4()
    observation_id = uuid4()
    distillation_id = uuid4()
    artifact_id = uuid4()
    conversation_id = uuid4()
    note_id = uuid4()
    theme_id = uuid4()

    pool.messages[message_id] = _message(
        message_id=message_id,
        sender_id=viewer_id,
        recipient_id=recipient_id,
        topic_id=topic_id,
        dyad_id=dyad_id,
        text="source shape message",
        sent_at=now,
    )
    pool.memories[memory_id] = {
        "id": memory_id,
        "content": "source shape memory",
        "about_user_id": viewer_id,
        "recorded_by_bot_id": "mediator",
        "status": "active",
        "visibility": "private",
        "created_at": now - timedelta(minutes=4),
        "last_referenced_at": now - timedelta(minutes=1),
    }
    pool.observations[observation_id] = {
        "id": observation_id,
        "content": "source shape observation",
        "about_user_id": viewer_id,
        "recorded_by_bot_id": "mediator",
        "status": "active",
        "significance": 3,
        "created_at": now - timedelta(minutes=3),
        "last_reinforced_at": now - timedelta(minutes=2),
    }
    pool.distillations[distillation_id] = {
        "id": distillation_id,
        "content": "source shape distillation",
        "status": "active",
        "visibility": "private",
        "created_at": now - timedelta(minutes=2),
        "updated_at": now - timedelta(minutes=1),
    }
    pool.conversation_artifacts[artifact_id] = {
        "id": artifact_id,
        "conversation_id": conversation_id,
        "user_id": viewer_id,
        "bot_id": "mediator",
        "artifact_type": "live_prep_brief",
        "payload_version": 1,
        "revision_number": 1,
        "payload": {
            "agenda": {"prep_summary": "source shape artifact"},
            "notes": "artifact notes",
        },
        "created_at": now - timedelta(minutes=1),
        "deleted_at": None,
        "topic_id": topic_id,
    }
    pool.conversations[conversation_id] = {
        "id": conversation_id,
        "user_id": viewer_id,
        "partner_user_id": recipient_id,
        "bot_id": "mediator",
        "topic_id": topic_id,
        "dyad_id": dyad_id,
    }
    pool.conversation_notes[note_id] = {
        "id": note_id,
        "text": "source shape conversation note",
        "conversation_id": conversation_id,
        "topic_id": topic_id,
        "bot_id": "mediator",
        "user_id": viewer_id,
        "created_at": now - timedelta(minutes=2),
    }
    pool.themes[theme_id] = {
        "id": theme_id,
        "title": "source shape theme title",
        "description": "source shape theme description",
        "status": "active",
        "recorded_by_bot_id": "mediator",
        "about_user_id": viewer_id,
        "created_at": now - timedelta(minutes=3),
        "updated_at": now - timedelta(minutes=1),
    }
    pool.link_topic("memories", memory_id, topic_id)
    pool.link_topic("observations", observation_id, topic_id)
    pool.link_topic("distillations", distillation_id, topic_id)
    pool.link_topic("themes", theme_id, topic_id)

    rows = {
        source_type: pool._searchable_content_row(source_type, source_id)
        for source_type, source_id in [
            ("message", message_id),
            ("memory", memory_id),
            ("observation", observation_id),
            ("distillation", distillation_id),
            ("artifact", artifact_id),
            ("conversation_note", note_id),
            ("theme", theme_id),
        ]
    }

    assert set(rows) == {
        "message", "memory", "observation", "distillation", "artifact",
        "conversation_note", "theme",
    }
    for source_type, row in rows.items():
        assert row is not None
        assert row["source_type"] == source_type
        assert row["source_id"] is not None
        assert "source shape" in row["canonical_text"]
        assert row["source_created_at"] is not None
        assert row["source_updated_at"] is not None
        assert row["sort_at"] is not None

    assert rows["message"]["message_id"] == message_id
    assert rows["message"]["direction"] == "inbound"
    assert rows["message"]["dyad_id"] == dyad_id
    assert rows["memory"]["message_id"] is None
    assert rows["memory"]["recipient_id"] is None
    assert rows["memory"]["dyad_id"] is None
    assert rows["observation"]["message_id"] is None
    assert rows["observation"]["media_analysis"] is None
    assert rows["distillation"]["message_id"] is None
    assert rows["distillation"]["bot_id"] is None
    assert rows["distillation"]["thread_owner_user_id"] is None
    assert rows["artifact"]["message_id"] is None
    assert rows["artifact"]["media_analysis"]["artifact_type"] == "live_prep_brief"

    # conversation_note source-aware fields
    assert rows["conversation_note"]["message_id"] is None
    assert rows["conversation_note"]["direction"] is None
    assert rows["conversation_note"]["dyad_id"] == dyad_id
    assert rows["conversation_note"]["bot_id"] == "mediator"
    assert rows["conversation_note"]["topic_id"] == topic_id
    assert rows["conversation_note"]["sender_id"] == viewer_id
    assert rows["conversation_note"]["thread_owner_user_id"] == viewer_id

    # theme source-aware fields
    assert rows["theme"]["message_id"] is None
    assert rows["theme"]["direction"] is None
    assert rows["theme"]["dyad_id"] is None
    assert rows["theme"]["bot_id"] == "mediator"
    assert rows["theme"]["topic_id"] == topic_id
    assert rows["theme"]["sender_id"] == viewer_id
    assert rows["theme"]["thread_owner_user_id"] == viewer_id
    assert rows["theme"]["media_analysis"] is None


async def test_fake_pool_unified_keyword_ranking_filters_nullable_sources():
    pool = FakePool()
    now = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
    viewer_id = uuid4()
    partner_id = uuid4()
    topic_id = uuid4()
    dyad_id = uuid4()
    message_id = uuid4()
    memory_id = uuid4()
    observation_id = uuid4()
    distillation_id = uuid4()
    artifact_id = uuid4()

    pool.messages[message_id] = _message(
        message_id=message_id,
        sender_id=viewer_id,
        recipient_id=partner_id,
        topic_id=topic_id,
        dyad_id=dyad_id,
        text="needle message",
        sent_at=now - timedelta(minutes=5),
    )
    pool.memories[memory_id] = {
        "id": memory_id,
        "content": "needle memory",
        "about_user_id": viewer_id,
        "recorded_by_bot_id": "mediator",
        "status": "active",
        "visibility": "private",
        "created_at": now - timedelta(minutes=4),
    }
    pool.observations[observation_id] = {
        "id": observation_id,
        "content": "needle observation",
        "about_user_id": viewer_id,
        "recorded_by_bot_id": "mediator",
        "status": "active",
        "significance": 3,
        "created_at": now - timedelta(minutes=3),
    }
    pool.distillations[distillation_id] = {
        "id": distillation_id,
        "content": "needle distillation suppressed by null source identity",
        "status": "active",
        "visibility": "private",
        "created_at": now - timedelta(minutes=2),
    }
    pool.conversation_artifacts[artifact_id] = {
        "id": artifact_id,
        "conversation_id": uuid4(),
        "user_id": viewer_id,
        "bot_id": "mediator",
        "artifact_type": "review_summary",
        "payload": {"summary": "needle artifact"},
        "created_at": now - timedelta(minutes=1),
        "deleted_at": None,
        "topic_id": topic_id,
    }
    pool.link_topic("memories", memory_id, topic_id)
    pool.link_topic("observations", observation_id, topic_id)
    pool.link_topic("distillations", distillation_id, topic_id)

    results = await hybrid_search(
        pool,
        RetrievalQuery(
            query="needle",
            viewer_user_id=viewer_id,
            partner_user_id=partner_id,
            bot_id="mediator",
            topic_id=topic_id,
            dyad_id=dyad_id,
            mode="exact",
            limit=10,
        ),
    )

    assert {(result.source_type, result.source_id) for result in results} == {
        ("message", message_id),
        ("memory", memory_id),
        ("observation", observation_id),
        ("artifact", artifact_id),
    }
    assert all(result.message_id is not None for result in results if result.source_type == "message")
    assert all(result.message_id is None for result in results if result.source_type != "message")
    assert ("distillation", distillation_id) not in {
        (result.source_type, result.source_id) for result in results
    }


# ── Negative coverage: conversation_note empty text ──────────────────


async def test_fake_pool_searchable_content_excludes_empty_note_text():
    """Empty or whitespace-only conversation note text returns None."""
    pool = FakePool()
    note_id = uuid4()
    empty_id = uuid4()
    whitespace_id = uuid4()
    pool.conversation_notes[note_id] = {
        "id": note_id,
        "text": "real note",
        "conversation_id": uuid4(),
        "created_at": datetime(2026, 6, 1, 12, tzinfo=UTC),
    }
    pool.conversation_notes[empty_id] = {
        "id": empty_id,
        "text": "",
        "conversation_id": uuid4(),
        "created_at": datetime(2026, 6, 1, 12, tzinfo=UTC),
    }
    pool.conversation_notes[whitespace_id] = {
        "id": whitespace_id,
        "text": "   \n\t  ",
        "conversation_id": uuid4(),
        "created_at": datetime(2026, 6, 1, 12, tzinfo=UTC),
    }

    assert pool._searchable_content_row("conversation_note", note_id) is not None
    assert pool._searchable_content_row("conversation_note", empty_id) is None
    assert pool._searchable_content_row("conversation_note", whitespace_id) is None


async def test_fake_pool_searchable_content_excludes_none_note():
    """None (missing) conversation note returns None."""
    pool = FakePool()
    assert pool._searchable_content_row("conversation_note", uuid4()) is None


# ── Negative coverage: theme inactive status ─────────────────────────


async def test_fake_pool_searchable_content_excludes_inactive_theme():
    """Theme with status != 'active' returns None."""
    pool = FakePool()
    active_id = uuid4()
    dormant_id = uuid4()
    resolved_id = uuid4()
    pool.themes[active_id] = {
        "id": active_id,
        "title": "active theme",
        "status": "active",
        "created_at": datetime(2026, 6, 1, 12, tzinfo=UTC),
    }
    pool.themes[dormant_id] = {
        "id": dormant_id,
        "title": "dormant theme",
        "status": "dormant",
        "created_at": datetime(2026, 6, 1, 12, tzinfo=UTC),
    }
    pool.themes[resolved_id] = {
        "id": resolved_id,
        "title": "resolved theme",
        "status": "resolved",
        "created_at": datetime(2026, 6, 1, 12, tzinfo=UTC),
    }

    assert pool._searchable_content_row("theme", active_id) is not None
    assert pool._searchable_content_row("theme", dormant_id) is None
    assert pool._searchable_content_row("theme", resolved_id) is None


async def test_fake_pool_searchable_content_excludes_empty_theme_text():
    """Theme with empty canonical text (no title or description) returns None."""
    pool = FakePool()
    empty_id = uuid4()
    pool.themes[empty_id] = {
        "id": empty_id,
        "title": None,
        "description": None,
        "status": "active",
        "created_at": datetime(2026, 6, 1, 12, tzinfo=UTC),
    }

    assert pool._searchable_content_row("theme", empty_id) is None


# ── Negative coverage: hidden-topic theme ─────────────────────────────


async def test_fake_pool_searchable_content_theme_without_topic_still_exposed():
    """Theme without link_topic or row topic_id is still returned (topic
    scoping happens at the query level in production, not in the row builder)."""
    pool = FakePool()
    theme_id = uuid4()
    pool.themes[theme_id] = {
        "id": theme_id,
        "title": "untargeted theme",
        "status": "active",
        "created_at": datetime(2026, 6, 1, 12, tzinfo=UTC),
    }

    row = pool._searchable_content_row("theme", theme_id)
    assert row is not None
    assert row["source_type"] == "theme"
    assert row["source_id"] == theme_id
    assert row["topic_id"] is None
    assert row["topic_ids"] == []


async def test_fake_pool_searchable_content_theme_hidden_topic_visible():
    """Theme linked to a topic via link_topic is returned with that topic_id,
    even though the topic may be considered 'hidden' at query time.
    The row builder itself does not gate on topic visibility."""
    pool = FakePool()
    theme_id = uuid4()
    topic_id = uuid4()
    pool.themes[theme_id] = {
        "id": theme_id,
        "title": "hidden-topic theme",
        "status": "active",
        "created_at": datetime(2026, 6, 1, 12, tzinfo=UTC),
    }
    pool.link_topic("themes", theme_id, topic_id)

    row = pool._searchable_content_row("theme", theme_id)
    assert row is not None
    assert row["source_type"] == "theme"
    assert row["topic_id"] == topic_id
    assert row["topic_ids"] == [topic_id]


# ── fetchrow integration for conversation_note and theme ─────────────


async def test_fake_pool_fetchrow_v_searchable_content_returns_note_and_theme():
    """fetchrow against v_searchable_content returns conversation_note and
    theme rows with the expected projection columns."""
    pool = FakePool()
    note_id = uuid4()
    theme_id = uuid4()
    pool.conversation_notes[note_id] = {
        "id": note_id,
        "text": "a note",
        "created_at": datetime(2026, 6, 1, 12, tzinfo=UTC),
    }
    pool.themes[theme_id] = {
        "id": theme_id,
        "title": "a theme",
        "status": "active",
        "created_at": datetime(2026, 6, 1, 12, tzinfo=UTC),
    }

    sql = (
        "SELECT source_type, source_id, message_id, canonical_text "
        "FROM mediator.v_searchable_content WHERE source_type=$1 AND source_id=$2"
    )

    note_row = await pool.fetchrow(sql, "conversation_note", note_id)
    assert note_row == {
        "source_type": "conversation_note",
        "source_id": note_id,
        "message_id": None,
        "canonical_text": "a note",
    }

    theme_row = await pool.fetchrow(sql, "theme", theme_id)
    assert theme_row == {
        "source_type": "theme",
        "source_id": theme_id,
        "message_id": None,
        "canonical_text": "a theme",
    }

    # Negative: empty note returns None via fetchrow
    empty_note_id = uuid4()
    pool.conversation_notes[empty_note_id] = {
        "id": empty_note_id,
        "text": "",
        "created_at": datetime(2026, 6, 1, 12, tzinfo=UTC),
    }
    assert await pool.fetchrow(sql, "conversation_note", empty_note_id) is None

    # Negative: inactive theme returns None via fetchrow
    inactive_theme_id = uuid4()
    pool.themes[inactive_theme_id] = {
        "id": inactive_theme_id,
        "title": "dormant",
        "status": "dormant",
        "created_at": datetime(2026, 6, 1, 12, tzinfo=UTC),
    }
    assert await pool.fetchrow(sql, "theme", inactive_theme_id) is None
