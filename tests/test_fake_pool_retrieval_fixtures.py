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
    assert "JOIN mediator.v_searchable_messages m" in transactional[1][2]
