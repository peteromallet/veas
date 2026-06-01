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
    pool.link_topic("memories", memory_id, topic_id)
    pool.link_topic("observations", observation_id, topic_id)
    pool.link_topic("distillations", distillation_id, topic_id)

    rows = {
        source_type: pool._searchable_content_row(source_type, source_id)
        for source_type, source_id in [
            ("message", message_id),
            ("memory", memory_id),
            ("observation", observation_id),
            ("distillation", distillation_id),
            ("artifact", artifact_id),
        ]
    }

    assert set(rows) == {"message", "memory", "observation", "distillation", "artifact"}
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
