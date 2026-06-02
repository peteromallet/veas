from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.services import retrieval
from app.services.retrieval import RetrievalQuery, RetrievalResult, hybrid_search


class RecordingPool:
    def __init__(self, rows: list[dict] | list[list[dict]] | None = None) -> None:
        self.rows = rows or []
        self.sql: str | None = None
        self.args: tuple[object, ...] | None = None
        self.fetch_sqls: list[str] = []
        self.fetch_args: list[tuple[object, ...]] = []
        self.fetch_calls = 0
        self.execute_calls: list[tuple[str, tuple[object, ...]]] = []
        self.transaction_entries = 0

    async def fetch(self, sql: str, *args):
        self.fetch_calls += 1
        self.sql = sql
        self.args = args
        self.fetch_sqls.append(sql)
        self.fetch_args.append(args)
        if self.rows and isinstance(self.rows[0], list):
            return self.rows[self.fetch_calls - 1]
        return self.rows

    async def execute(self, sql: str, *args):
        self.execute_calls.append((sql, args))
        return "OK"

    def acquire(self):
        return _RecordingAcquire(self)

    def transaction(self):
        return _RecordingTransaction(self)


class TransactionSpyPool:
    def __init__(
        self,
        rows: list[dict] | None = None,
        *,
        keyword_rows: list[dict] | None = None,
    ) -> None:
        self.rows = rows or []
        self.keyword_rows = keyword_rows or []
        self.events: list[tuple[str, int, int | None, str]] = []
        self.connection_count = 0

    async def fetch(self, sql: str, *args):
        self.events.append(("fetch", 0, None, " ".join(sql.split())))
        return self.keyword_rows

    def acquire(self):
        self.connection_count += 1
        return _TransactionSpyAcquire(self, self.connection_count)


class _TransactionSpyConnection:
    def __init__(self, pool: TransactionSpyPool, connection_id: int) -> None:
        self.pool = pool
        self.connection_id = connection_id
        self.transaction_depth = 0
        self.sql: str | None = None
        self.args: tuple[object, ...] | None = None

    def transaction(self):
        return _TransactionSpyTransaction(self)

    async def execute(self, sql: str, *args):
        self.pool.events.append(
            ("execute", self.connection_id, self.transaction_depth, " ".join(sql.split()))
        )
        return "OK"

    async def fetch(self, sql: str, *args):
        self.sql = sql
        self.args = args
        self.pool.events.append(
            ("fetch", self.connection_id, self.transaction_depth, " ".join(sql.split()))
        )
        return self.pool.rows


class _TransactionSpyAcquire:
    def __init__(self, pool: TransactionSpyPool, connection_id: int) -> None:
        self.conn = _TransactionSpyConnection(pool, connection_id)

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _TransactionSpyTransaction:
    def __init__(self, conn: _TransactionSpyConnection) -> None:
        self.conn = conn

    async def __aenter__(self):
        self.conn.transaction_depth += 1
        self.conn.pool.events.append(
            ("transaction_enter", self.conn.connection_id, self.conn.transaction_depth, "")
        )

    async def __aexit__(self, exc_type, exc, tb):
        self.conn.pool.events.append(
            ("transaction_exit", self.conn.connection_id, self.conn.transaction_depth, "")
        )
        self.conn.transaction_depth -= 1
        return False


class _RecordingAcquire:
    def __init__(self, pool: RecordingPool) -> None:
        self.pool = pool

    async def __aenter__(self):
        return self.pool

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _RecordingTransaction:
    def __init__(self, pool: RecordingPool) -> None:
        self.pool = pool

    async def __aenter__(self):
        self.pool.transaction_entries += 1

    async def __aexit__(self, exc_type, exc, tb):
        return False


class RecordingEmbedder:
    model_name = "test-model"
    dimension = 3

    def __init__(self, vector: list[float] | None = None) -> None:
        self.vector = vector or [3.0, 4.0, 0.0]
        self.calls: list[list[str]] = []

    async def embed_texts(self, texts):
        self.calls.append(list(texts))
        return [self.vector for _ in texts]


class FailingEmbedder:
    model_name = "failing-model"
    dimension = 3

    async def embed_texts(self, texts):
        raise RuntimeError("provider unavailable")


class SlowEmbedder:
    model_name = "slow-model"
    dimension = 3

    async def embed_texts(self, texts):
        await asyncio.sleep(0.05)
        return [[1.0, 0.0, 0.0] for _ in texts]


def _visibility_contract_row_visible(
    row: dict[str, object],
    *,
    viewer_id: UUID,
    partner_id: UUID,
    bot_id: str,
    topic_id: UUID,
    thread_owner_user_id: UUID | None,
    dyad_id: UUID,
) -> bool:
    """Mirror the SQL visibility contract for table-driven regression cases."""

    if row.get("deleted_at") is not None or row.get("search_suppressed_at") is not None:
        return False
    if row["bot_id"] != bot_id:
        return False
    if row["topic_id"] != topic_id:
        return False
    if row["dyad_id"] != dyad_id and not (
        row.get("source_type") != "message" and row["dyad_id"] is None
    ):
        return False
    if thread_owner_user_id is not None and row["thread_owner_user_id"] != thread_owner_user_id:
        return False
    if row["thread_owner_user_id"] not in {viewer_id, partner_id}:
        return False
    if row["sender_id"] not in {viewer_id, partner_id} and row["recipient_id"] not in {
        viewer_id,
        partner_id,
    }:
        return False
    if row["thread_owner_user_id"] != viewer_id and row["thread_owner_partner_share"] != "opt_in":
        return False
    if row.get("source_type") in {"memory", "distillation"} and (
        row["thread_owner_partner_share"] is not None
    ):
        return False
    if row.get("active_oob_severity") in {"firm", "hard"}:
        return False
    return True


def _assert_retrieval_visibility_sql(sql: str) -> None:
    compact = " ".join(sql.split())
    assert "mediator.v_searchable_content sc" in compact
    assert "mediator.v_searchable_messages m" not in compact
    assert "FROM mediator.messages" not in compact
    assert "sc.bot_id IS NOT NULL" in compact
    assert "sc.bot_id =" in compact
    assert "sc.primary_topic_id =" in compact
    assert "ANY(sc.topic_ids)" in compact
    assert "sc.dyad_id IS NOT NULL" not in compact
    assert "sc.dyad_id =" in compact
    assert "sc.source_type <> 'message' AND sc.dyad_id IS NULL" in compact
    assert "sc.thread_owner_user_id IS NOT NULL" in compact
    assert "sc.thread_owner_user_id =" in compact
    assert "sc.thread_owner_user_id = ANY(" in compact
    assert "(sc.sender_id IS NOT NULL AND sc.sender_id = ANY(" in compact
    assert "OR (sc.recipient_id IS NOT NULL AND sc.recipient_id = ANY(" in compact
    assert "sc.thread_owner_partner_share = 'opt_in'" in compact
    assert "sc.source_type NOT IN ('memory', 'distillation') OR sc.thread_owner_partner_share IS NULL" in compact
    assert "FROM mediator.out_of_bounds x" in compact
    assert "x.owner_id = sc.thread_owner_user_id" in compact
    assert "x.status = 'active'" in compact
    assert "x.severity IN ('firm', 'hard')" in compact


def _query(**overrides) -> RetrievalQuery:
    base = {
        "query": "deploy crash",
        "viewer_user_id": uuid4(),
        "bot_id": "mediator",
        "mode": "exact",
        "limit": 5,
    }
    base.update(overrides)
    return RetrievalQuery(**base)


def _settings(**overrides):
    base = {
        "query_embed_timeout_s": 0.5,
        "query_embed_cache_ttl_s": 300,
        "query_embed_cache_max_entries": 1024,
        "retrieval_hnsw_ef_search": 80,
        "retrieval_source_weight_map": {"message": 1.0},
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def clear_query_embedding_cache():
    retrieval._QUERY_EMBEDDING_CACHE.clear()
    yield
    retrieval._QUERY_EMBEDDING_CACHE.clear()


def test_retrieval_result_defaults_message_source_identity_for_legacy_callers():
    message_id = uuid4()

    result = RetrievalResult(
        message_id=message_id,
        match_type="exact",
        rrf_score=None,
        keyword_rank=1,
        semantic_rank=None,
        semantic_degraded=False,
    )

    assert result.message_id == message_id
    assert result.source_type == "message"
    assert result.source_id == message_id


def test_retrieval_result_allows_explicit_non_message_source_identity():
    source_id = uuid4()

    result = RetrievalResult(
        message_id=None,
        source_type="memory",
        source_id=source_id,
        match_type="semantic",
        rrf_score=0.25,
        keyword_rank=None,
        semantic_rank=1,
        semantic_degraded=False,
    )

    assert result.message_id is None
    assert result.source_type == "memory"
    assert result.source_id == source_id


@pytest.mark.anyio
async def test_exact_mode_runs_keyword_only_and_returns_exact_results():
    message_id = uuid4()
    sent_at = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
    pool = RecordingPool(
        [
            {
                "message_id": message_id,
                "sent_at": sent_at,
                "keyword_score": 0.625,
                "keyword_rank": 1,
            }
        ]
    )

    embedder = FailingEmbedder()

    results = await hybrid_search(pool, _query(), embedder=embedder, settings=_settings())

    assert [result.message_id for result in results] == [message_id]
    assert results[0].match_type == "exact"
    assert results[0].keyword_rank == 1
    assert results[0].semantic_rank is None
    assert results[0].semantic_degraded is False
    assert results[0].rrf_score is None
    assert results[0].keyword_score == pytest.approx(0.625)


@pytest.mark.anyio
async def test_exact_mode_skips_blank_queries_without_hitting_database():
    pool = RecordingPool()

    results = await hybrid_search(pool, _query(query="   "))

    assert results == []
    assert pool.fetch_calls == 0


@pytest.mark.anyio
async def test_exact_mode_sql_uses_searchable_view_normalized_rank_and_stable_ordering():
    pool = RecordingPool()
    viewer_id = UUID("00000000-0000-4000-8000-000000000020")
    partner_id = UUID("00000000-0000-4000-8000-000000000021")

    await hybrid_search(
        pool,
        _query(
            viewer_user_id=viewer_id,
            partner_user_id=partner_id,
            topic_id=UUID("00000000-0000-4000-8000-000000000010"),
            thread_owner_user_id=UUID("00000000-0000-4000-8000-000000000011"),
            dyad_id=UUID("00000000-0000-4000-8000-000000000012"),
            limit=7,
        ),
    )

    assert pool.sql is not None
    compact = " ".join(pool.sql.split())
    assert "FROM mediator.v_searchable_content sc" in pool.sql
    assert "websearch_to_tsquery('simple'::regconfig, $1)" in pool.sql
    assert "ts_rank(sc.search_tsv, query.tsq, 32)" in pool.sql
    assert "sc.canonical_text AS source_text" in pool.sql
    assert "sc.source_created_at" in pool.sql
    assert "sc.source_updated_at" in pool.sql
    assert "row_number() OVER" in pool.sql
    assert "sc.sort_at DESC" in pool.sql
    assert "sc.source_id DESC" in pool.sql
    assert "sc.search_tsv @@ query.tsq" in pool.sql
    assert "sc.content" not in compact
    assert compact.count("sc.canonical_text") == 1
    assert "semantic_rank" not in compact
    assert "rrf_score" not in compact
    assert "sc.thread_owner_user_id = ANY($4::uuid[])" in pool.sql
    assert (
        "((sc.sender_id IS NOT NULL AND sc.sender_id = ANY($4::uuid[])) "
        "OR (sc.recipient_id IS NOT NULL AND sc.recipient_id = ANY($4::uuid[])))"
    ) in compact
    assert "(sc.thread_owner_user_id = $3 OR sc.thread_owner_partner_share = 'opt_in')" in compact
    assert "FROM mediator.out_of_bounds x" in pool.sql
    assert "x.status = 'active'" in pool.sql
    assert "x.severity IN ('firm', 'hard')" in pool.sql
    assert pool.args == (
        "deploy crash",
        "mediator",
        viewer_id,
        [viewer_id, partner_id],
        UUID("00000000-0000-4000-8000-000000000010"),
        UUID("00000000-0000-4000-8000-000000000011"),
        UUID("00000000-0000-4000-8000-000000000012"),
        7,
    )


@pytest.mark.anyio
async def test_exact_mode_sql_handles_null_or_empty_source_text_via_search_tsv_only():
    pool = RecordingPool()

    await hybrid_search(pool, _query(query="image transcript"))

    assert pool.sql is not None
    compact = " ".join(pool.sql.split())
    assert "sc.search_tsv @@ query.tsq" in compact
    assert "coalesce(" not in compact
    assert "ILIKE" not in compact
    assert "media_analysis" not in compact


@pytest.mark.anyio
async def test_hybrid_mode_normalizes_query_and_caches_embedding_by_model_and_query():
    pool = RecordingPool()
    embedder = RecordingEmbedder()
    request = _query(mode="hybrid", query="  Cafe\u0301   deploy\tcrash  ")

    first = await hybrid_search(pool, request, embedder=embedder, settings=_settings())
    second = await hybrid_search(pool, request, embedder=embedder, settings=_settings())

    assert first == []
    assert second == []
    assert embedder.calls == [["Café deploy crash"]]
    assert ("test-model", "Café deploy crash") in retrieval._QUERY_EMBEDDING_CACHE
    assert pool.fetch_calls == 4


@pytest.mark.anyio
async def test_hybrid_query_embedding_cache_hits_for_normalized_repeats_without_duplicate_calls():
    pool = RecordingPool()
    embedder = RecordingEmbedder()

    await hybrid_search(
        pool,
        _query(mode="hybrid", query="  Café   deploy\tcrash  "),
        embedder=embedder,
        settings=_settings(),
    )
    await hybrid_search(
        pool,
        _query(mode="hybrid", query="Café deploy crash"),
        embedder=embedder,
        settings=_settings(),
    )

    assert embedder.calls == [["Café deploy crash"]]
    assert list(retrieval._QUERY_EMBEDDING_CACHE) == [("test-model", "Café deploy crash")]


@pytest.mark.anyio
async def test_hybrid_query_embedding_cache_preserves_case_and_misses_for_case_distinct_queries():
    pool = RecordingPool()
    embedder = RecordingEmbedder()

    await hybrid_search(
        pool,
        _query(mode="hybrid", query="Deploy crash"),
        embedder=embedder,
        settings=_settings(),
    )
    await hybrid_search(
        pool,
        _query(mode="hybrid", query="deploy crash"),
        embedder=embedder,
        settings=_settings(),
    )

    assert embedder.calls == [["Deploy crash"], ["deploy crash"]]
    assert ("test-model", "Deploy crash") in retrieval._QUERY_EMBEDDING_CACHE
    assert ("test-model", "deploy crash") in retrieval._QUERY_EMBEDDING_CACHE


@pytest.mark.anyio
async def test_hybrid_query_embedding_cache_expires_after_ttl(monkeypatch: pytest.MonkeyPatch):
    clock = {"now": 100.0}
    monkeypatch.setattr(retrieval.time, "monotonic", lambda: clock["now"])

    pool = RecordingPool()
    embedder = RecordingEmbedder()
    settings = _settings(query_embed_cache_ttl_s=5, query_embed_cache_max_entries=16)

    await hybrid_search(pool, _query(mode="hybrid"), embedder=embedder, settings=settings)
    clock["now"] = 104.0
    await hybrid_search(pool, _query(mode="hybrid"), embedder=embedder, settings=settings)
    clock["now"] = 106.0
    await hybrid_search(pool, _query(mode="hybrid"), embedder=embedder, settings=settings)

    assert embedder.calls == [
        ["deploy crash"],
        ["deploy crash"],
    ]


@pytest.mark.anyio
async def test_hybrid_query_embedding_cache_evicts_lru_entry_when_full():
    pool = RecordingPool()
    embedder = RecordingEmbedder()
    settings = _settings(query_embed_cache_ttl_s=300, query_embed_cache_max_entries=2)

    await hybrid_search(
        pool,
        _query(mode="hybrid", query="first query"),
        embedder=embedder,
        settings=settings,
    )
    await hybrid_search(
        pool,
        _query(mode="hybrid", query="second query"),
        embedder=embedder,
        settings=settings,
    )
    await hybrid_search(
        pool,
        _query(mode="hybrid", query="first query"),
        embedder=embedder,
        settings=settings,
    )
    await hybrid_search(
        pool,
        _query(mode="hybrid", query="third query"),
        embedder=embedder,
        settings=settings,
    )
    await hybrid_search(
        pool,
        _query(mode="hybrid", query="second query"),
        embedder=embedder,
        settings=settings,
    )

    assert embedder.calls == [
        ["first query"],
        ["second query"],
        ["third query"],
        ["second query"],
    ]
    assert list(retrieval._QUERY_EMBEDDING_CACHE) == [
        ("test-model", "third query"),
        ("test-model", "second query"),
    ]


@pytest.mark.anyio
async def test_hybrid_query_embedding_cache_is_partitioned_by_model():
    pool = RecordingPool()
    first_embedder = RecordingEmbedder()
    second_embedder = RecordingEmbedder()
    second_embedder.model_name = "second-model"
    request = _query(mode="hybrid", query="deploy crash")

    await hybrid_search(pool, request, embedder=first_embedder, settings=_settings())
    await hybrid_search(pool, request, embedder=second_embedder, settings=_settings())

    assert first_embedder.calls == [["deploy crash"]]
    assert second_embedder.calls == [["deploy crash"]]
    assert ("test-model", "deploy crash") in retrieval._QUERY_EMBEDDING_CACHE
    assert ("second-model", "deploy crash") in retrieval._QUERY_EMBEDDING_CACHE


@pytest.mark.anyio
async def test_hybrid_mode_runs_semantic_ann_with_model_dimension_filters_and_hnsw_transaction():
    message_id = uuid4()
    sent_at = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
    pool = RecordingPool(
        [
            [],
            [
                {
                    "message_id": message_id,
                    "sent_at": sent_at,
                    "cosine_distance": 0.125,
                    "semantic_rank": 1,
                }
            ],
        ]
    )
    embedder = RecordingEmbedder(vector=[3.0, 4.0, 0.0])
    viewer_id = UUID("00000000-0000-4000-8000-000000000020")
    partner_id = UUID("00000000-0000-4000-8000-000000000021")

    results = await hybrid_search(
        pool,
        _query(
            mode="hybrid",
            viewer_user_id=viewer_id,
            partner_user_id=partner_id,
            topic_id=UUID("00000000-0000-4000-8000-000000000010"),
            thread_owner_user_id=UUID("00000000-0000-4000-8000-000000000011"),
            dyad_id=UUID("00000000-0000-4000-8000-000000000012"),
            limit=7,
        ),
        embedder=embedder,
        settings=_settings(retrieval_hnsw_ef_search=96),
    )

    assert [result.message_id for result in results] == [message_id]
    assert results[0].match_type == "semantic"
    assert results[0].rrf_score == pytest.approx(1 / 61)
    assert results[0].keyword_rank is None
    assert results[0].semantic_rank == 1
    assert results[0].semantic_degraded is False
    assert pool.transaction_entries == 1
    assert pool.execute_calls == [("SET LOCAL hnsw.ef_search = 96", ())]
    assert pool.args == (
        [0.6, 0.8, 0.0],
        "test-model",
        3,
        "mediator",
        viewer_id,
        [viewer_id, partner_id],
        UUID("00000000-0000-4000-8000-000000000010"),
        UUID("00000000-0000-4000-8000-000000000011"),
        UUID("00000000-0000-4000-8000-000000000012"),
        7,
    )


@pytest.mark.anyio
async def test_hybrid_semantic_ann_sets_hnsw_and_fetches_ann_in_same_transaction():
    message_id = uuid4()
    sent_at = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
    pool = TransactionSpyPool(
        [
            {
                "message_id": message_id,
                "sent_at": sent_at,
                "cosine_distance": 0.125,
                "semantic_rank": 1,
            }
        ]
    )

    results = await hybrid_search(
        pool,
        _query(mode="hybrid"),
        embedder=RecordingEmbedder(),
        settings=_settings(retrieval_hnsw_ef_search=123),
    )

    assert [result.message_id for result in results] == [message_id]
    assert pool.connection_count == 1
    assert [event[0] for event in pool.events] == [
        "fetch",
        "transaction_enter",
        "execute",
        "fetch",
        "transaction_exit",
    ]

    execute_event = pool.events[2]
    fetch_event = pool.events[3]
    assert execute_event[:3] == ("execute", 1, 1)
    assert execute_event[3] == "SET LOCAL hnsw.ef_search = 123"
    assert fetch_event[:3] == ("fetch", 1, 1)
    assert "FROM mediator.content_embeddings e" in fetch_event[3]
    assert "JOIN mediator.v_searchable_content sc" in fetch_event[3]
    assert "sc.source_type = 'message'" not in fetch_event[3]
    assert "JOIN mediator.v_searchable_messages m" not in fetch_event[3]
    assert "e.embedding <=> $1" in fetch_event[3]


@pytest.mark.anyio
async def test_hybrid_semantic_sql_joins_searchable_view_and_orders_by_cosine_with_tie_breakers():
    pool = RecordingPool()

    await hybrid_search(
        pool,
        _query(mode="hybrid"),
        embedder=RecordingEmbedder(),
        settings=_settings(retrieval_hnsw_ef_search=80),
    )

    assert pool.sql is not None
    compact = " ".join(pool.sql.split())
    assert "FROM mediator.content_embeddings e" in pool.sql
    assert "JOIN mediator.v_searchable_content sc" in pool.sql
    assert "JOIN mediator.v_searchable_messages m" not in pool.sql
    assert "sc.source_type = 'message'" not in pool.sql
    assert "e.model = $2" in pool.sql
    assert "e.dimension = $3" in pool.sql
    assert "e.embedding <=> $1 AS cosine_distance" in pool.sql
    assert "e.embedding <=> $1 ASC" in pool.sql
    assert "sc.sort_at DESC" in pool.sql
    assert "sc.source_id DESC" in pool.sql
    assert "sc.thread_owner_user_id = ANY($6::uuid[])" in pool.sql
    assert "(sc.thread_owner_user_id = $5 OR sc.thread_owner_partner_share = 'opt_in')" in compact
    assert "FROM mediator.out_of_bounds x" in pool.sql
    assert "row_number() OVER" in pool.sql
    assert "ORDER BY semantic_rank ASC" in pool.sql
    assert "semantic_matches" in compact


@pytest.mark.anyio
async def test_hybrid_mode_fuses_keyword_and_semantic_results_with_rrf_metadata_and_limit():
    both_id = UUID("00000000-0000-4000-8000-000000000001")
    keyword_only_id = UUID("00000000-0000-4000-8000-000000000002")
    semantic_only_id = UUID("00000000-0000-4000-8000-000000000003")
    sent_at = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
    pool = RecordingPool(
        [
            [
                {
                    "message_id": both_id,
                    "sent_at": sent_at,
                    "keyword_score": 0.8,
                    "keyword_rank": 1,
                },
                {
                    "message_id": keyword_only_id,
                    "sent_at": sent_at,
                    "keyword_score": 0.4,
                    "keyword_rank": 2,
                },
            ],
            [
                {
                    "message_id": semantic_only_id,
                    "sent_at": sent_at,
                    "cosine_distance": 0.1,
                    "semantic_rank": 1,
                },
                {
                    "message_id": both_id,
                    "sent_at": sent_at,
                    "cosine_distance": 0.2,
                    "semantic_rank": 2,
                },
            ],
        ]
    )

    results = await hybrid_search(
        pool,
        _query(mode="hybrid", limit=2),
        embedder=RecordingEmbedder(),
        settings=_settings(),
    )

    assert [result.message_id for result in results] == [both_id, semantic_only_id]
    assert results[0].match_type == "both"
    assert results[0].keyword_rank == 1
    assert results[0].semantic_rank == 2
    assert results[0].rrf_score == pytest.approx((1 / 61) + (1 / 62))
    assert results[0].semantic_degraded is False
    assert results[1].match_type == "semantic"
    assert results[1].keyword_rank is None
    assert results[1].semantic_rank == 1
    assert results[1].rrf_score == pytest.approx(1 / 61)
    assert pool.fetch_calls == 2


@pytest.mark.anyio
async def test_exact_mode_ranks_unified_keyword_sources_without_semantic_work():
    message_id = UUID("00000000-0000-4000-8000-000000000041")
    memory_id = UUID("00000000-0000-4000-8000-000000000042")
    sent_at = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
    pool = RecordingPool(
        [
            {
                "source_type": "memory",
                "source_id": memory_id,
                "message_id": None,
                "sent_at": sent_at,
                "keyword_score": 0.9,
                "keyword_rank": 1,
            },
            {
                "source_type": "message",
                "source_id": message_id,
                "message_id": message_id,
                "sent_at": sent_at,
                "keyword_score": 0.8,
                "keyword_rank": 2,
            },
        ]
    )

    results = await hybrid_search(pool, _query(mode="exact", limit=5), settings=_settings())

    assert [(result.source_type, result.source_id, result.message_id) for result in results] == [
        ("memory", memory_id, None),
        ("message", message_id, message_id),
    ]
    assert [result.match_type for result in results] == ["exact", "exact"]
    assert all(result.rrf_score is None for result in results)
    assert pool.fetch_calls == 1


@pytest.mark.anyio
async def test_hybrid_mode_ranks_unified_semantic_only_sources():
    memory_id = UUID("00000000-0000-4000-8000-000000000043")
    observation_id = UUID("00000000-0000-4000-8000-000000000044")
    sent_at = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
    pool = RecordingPool(
        [
            [],
            [
                {
                    "source_type": "memory",
                    "source_id": memory_id,
                    "message_id": None,
                    "sent_at": sent_at,
                    "cosine_distance": 0.1,
                    "semantic_rank": 1,
                },
                {
                    "source_type": "observation",
                    "source_id": observation_id,
                    "message_id": None,
                    "sent_at": sent_at,
                    "cosine_distance": 0.2,
                    "semantic_rank": 2,
                },
            ],
        ]
    )

    results = await hybrid_search(
        pool,
        _query(mode="hybrid", limit=5),
        embedder=RecordingEmbedder(),
        settings=_settings(),
    )

    assert [(result.source_type, result.source_id) for result in results] == [
        ("memory", memory_id),
        ("observation", observation_id),
    ]
    assert [result.match_type for result in results] == ["semantic", "semantic"]
    assert [result.semantic_rank for result in results] == [1, 2]
    assert all(result.message_id is None for result in results)


@pytest.mark.anyio
async def test_hybrid_mode_uses_configured_source_weights_without_public_request_fields():
    message_id = UUID("00000000-0000-4000-8000-000000000011")
    memory_id = UUID("00000000-0000-4000-8000-000000000012")
    sent_at = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
    pool = RecordingPool(
        [
            [],
            [
                {
                    "source_type": "message",
                    "source_id": message_id,
                    "message_id": message_id,
                    "sent_at": sent_at,
                    "cosine_distance": 0.1,
                    "semantic_rank": 1,
                },
                {
                    "source_type": "memory",
                    "source_id": memory_id,
                    "message_id": None,
                    "sent_at": sent_at,
                    "cosine_distance": 0.2,
                    "semantic_rank": 2,
                },
            ],
        ]
    )

    results = await hybrid_search(
        pool,
        _query(mode="hybrid", limit=2),
        embedder=RecordingEmbedder(),
        settings=_settings(retrieval_source_weight_map={"message": 1.0, "memory": 2.0}),
    )

    assert [result.source_type for result in results] == ["memory", "message"]
    assert results[0].message_id is None
    assert results[0].source_id == memory_id
    assert results[0].rrf_score == pytest.approx(2.0 / 62)
    assert not hasattr(RetrievalQuery, "source_weight_map")


@pytest.mark.anyio
async def test_source_weight_map_flips_order_and_unmapped_sources_default_to_one():
    message_id = UUID("00000000-0000-4000-8000-000000000045")
    observation_id = UUID("00000000-0000-4000-8000-000000000046")
    artifact_id = UUID("00000000-0000-4000-8000-000000000047")
    sent_at = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
    semantic_rows = [
        {
            "source_type": "message",
            "source_id": message_id,
            "message_id": message_id,
            "sent_at": sent_at,
            "semantic_rank": 1,
        },
        {
            "source_type": "observation",
            "source_id": observation_id,
            "message_id": None,
            "sent_at": sent_at,
            "semantic_rank": 2,
        },
        {
            "source_type": "artifact",
            "source_id": artifact_id,
            "message_id": None,
            "sent_at": sent_at,
            "semantic_rank": 3,
        },
    ]

    unweighted = retrieval._fuse_rrf_results(
        keyword_rows=[],
        semantic_rows=semantic_rows,
        semantic_degraded=False,
        limit=3,
        source_weight_map={"message": 1.0},
    )
    weighted = retrieval._fuse_rrf_results(
        keyword_rows=[],
        semantic_rows=semantic_rows,
        semantic_degraded=False,
        limit=3,
        source_weight_map={"message": 1.0, "observation": 2.0},
    )

    assert [result.source_type for result in unweighted] == [
        "message",
        "observation",
        "artifact",
    ]
    assert [result.source_type for result in weighted] == [
        "observation",
        "message",
        "artifact",
    ]
    assert weighted[2].rrf_score == pytest.approx(1.0 / 63)


def test_source_weight_map_defaults_conversation_note_and_theme_to_one_when_unmapped():
    sent_at = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
    message_id = UUID("00000000-0000-4000-8000-000000000048")
    note_id = UUID("00000000-0000-4000-8000-000000000049")
    theme_id = UUID("00000000-0000-4000-8000-000000000050")

    results = retrieval._fuse_rrf_results(
        keyword_rows=[],
        semantic_rows=[
            {
                "source_type": "message",
                "source_id": message_id,
                "message_id": message_id,
                "sent_at": sent_at,
                "semantic_rank": 1,
            },
            {
                "source_type": "conversation_note",
                "source_id": note_id,
                "message_id": None,
                "sent_at": sent_at,
                "semantic_rank": 2,
            },
            {
                "source_type": "theme",
                "source_id": theme_id,
                "message_id": None,
                "sent_at": sent_at,
                "semantic_rank": 3,
            },
        ],
        semantic_degraded=False,
        limit=3,
        source_weight_map={"message": 1.0},
    )

    assert [result.source_type for result in results] == [
        "message",
        "conversation_note",
        "theme",
    ]
    assert results[1].rrf_score == pytest.approx(1.0 / 62)
    assert results[2].rrf_score == pytest.approx(1.0 / 63)


def test_source_weight_map_applies_to_conversation_note_and_theme_when_mapped():
    sent_at = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
    message_id = UUID("00000000-0000-4000-8000-000000000051")
    note_id = UUID("00000000-0000-4000-8000-000000000052")
    theme_id = UUID("00000000-0000-4000-8000-000000000053")

    results = retrieval._fuse_rrf_results(
        keyword_rows=[],
        semantic_rows=[
            {
                "source_type": "message",
                "source_id": message_id,
                "message_id": message_id,
                "sent_at": sent_at,
                "semantic_rank": 1,
            },
            {
                "source_type": "conversation_note",
                "source_id": note_id,
                "message_id": None,
                "sent_at": sent_at,
                "semantic_rank": 2,
            },
            {
                "source_type": "theme",
                "source_id": theme_id,
                "message_id": None,
                "sent_at": sent_at,
                "semantic_rank": 3,
            },
        ],
        semantic_degraded=False,
        limit=3,
        source_weight_map={
            "message": 1.0,
            "conversation_note": 2.0,
            "theme": 4.0,
        },
    )

    assert [result.source_type for result in results] == [
        "theme",
        "conversation_note",
        "message",
    ]
    assert results[0].rrf_score == pytest.approx(4.0 / 63)
    assert results[1].rrf_score == pytest.approx(2.0 / 62)


@pytest.mark.anyio
async def test_hybrid_mode_internal_source_weight_override_wins_over_config():
    message_id = UUID("00000000-0000-4000-8000-000000000021")
    memory_id = UUID("00000000-0000-4000-8000-000000000022")
    sent_at = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
    pool = RecordingPool(
        [
            [],
            [
                {
                    "source_type": "message",
                    "source_id": message_id,
                    "message_id": message_id,
                    "sent_at": sent_at,
                    "cosine_distance": 0.1,
                    "semantic_rank": 1,
                },
                {
                    "source_type": "memory",
                    "source_id": memory_id,
                    "message_id": None,
                    "sent_at": sent_at,
                    "cosine_distance": 0.2,
                    "semantic_rank": 2,
                },
            ],
        ]
    )

    results = await hybrid_search(
        pool,
        _query(mode="hybrid", limit=2),
        embedder=RecordingEmbedder(),
        settings=_settings(retrieval_source_weight_map={"message": 1.0, "memory": 5.0}),
        source_weight_map={"message": 1.0, "memory": 0.5},
    )

    assert [result.source_type for result in results] == ["message", "memory"]
    assert results[1].rrf_score == pytest.approx(0.5 / 62)


@pytest.mark.parametrize("mode", ["exact", "hybrid"])
@pytest.mark.anyio
async def test_retrieval_ranks_null_message_distillation_and_observation_sources(mode: str):
    observation_id = UUID("00000000-0000-4000-8000-000000000031")
    distillation_id = UUID("00000000-0000-4000-8000-000000000032")
    sent_at = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
    keyword_rows = [
        {
            "source_type": "observation",
            "source_id": observation_id,
            "message_id": None,
            "sent_at": sent_at,
            "keyword_score": 0.8,
            "keyword_rank": 1,
        },
        {
            "source_type": "distillation",
            "source_id": distillation_id,
            "message_id": None,
            "sent_at": sent_at,
            "keyword_score": 0.7,
            "keyword_rank": 2,
        },
    ]
    semantic_rows = [
        {
            "source_type": "observation",
            "source_id": observation_id,
            "message_id": None,
            "sent_at": sent_at,
            "cosine_distance": 0.1,
            "semantic_rank": 1,
        },
        {
            "source_type": "distillation",
            "source_id": distillation_id,
            "message_id": None,
            "sent_at": sent_at,
            "cosine_distance": 0.2,
            "semantic_rank": 2,
        },
    ]
    pool = RecordingPool(keyword_rows if mode == "exact" else [keyword_rows, semantic_rows])

    results = await hybrid_search(
        pool,
        _query(
            mode=mode,
            viewer_user_id=UUID("00000000-0000-4000-8000-000000000020"),
            partner_user_id=UUID("00000000-0000-4000-8000-000000000021"),
            topic_id=UUID("00000000-0000-4000-8000-000000000010"),
            dyad_id=UUID("00000000-0000-4000-8000-000000000012"),
            limit=2,
        ),
        embedder=RecordingEmbedder(),
        settings=_settings(),
    )

    assert [(result.source_type, result.source_id) for result in results] == [
        ("observation", observation_id),
        ("distillation", distillation_id),
    ]
    assert [result.message_id for result in results] == [None, None]
    assert pool.fetch_sqls
    for sql in pool.fetch_sqls:
        compact = " ".join(sql.split())
        assert "(sc.dyad_id =" in compact
        assert "OR (sc.source_type <> 'message' AND sc.dyad_id IS NULL)" in compact
        assert "x.severity IN ('firm', 'hard')" in compact


@pytest.mark.parametrize("mode", ["exact", "hybrid"])
@pytest.mark.anyio
async def test_retriever_visibility_denied_and_control_rows_are_gated_in_every_mode(mode: str):
    viewer_id = UUID("00000000-0000-4000-8000-000000000020")
    partner_id = UUID("00000000-0000-4000-8000-000000000021")
    topic_id = UUID("00000000-0000-4000-8000-000000000010")
    request_thread_owner_user_id = None
    dyad_id = UUID("00000000-0000-4000-8000-000000000012")
    other_id = UUID("00000000-0000-4000-8000-000000000099")
    base_row = {
        "bot_id": "mediator",
        "source_type": "message",
        "topic_id": topic_id,
        "dyad_id": dyad_id,
        "thread_owner_user_id": viewer_id,
        "thread_owner_partner_share": "unset",
        "sender_id": viewer_id,
        "recipient_id": partner_id,
        "deleted_at": None,
        "search_suppressed_at": None,
        "active_oob_severity": None,
    }
    scenarios = [
        ("wrong bot", {**base_row, "bot_id": "coach"}, False),
        ("wrong topic", {**base_row, "topic_id": uuid4()}, False),
        ("wrong dyad", {**base_row, "dyad_id": uuid4()}, False),
        (
            "partner-private",
            {
                **base_row,
                "thread_owner_user_id": partner_id,
                "thread_owner_partner_share": "opt_out",
            },
            False,
        ),
        (
            "raw-message-hidden",
            {**base_row, "sender_id": other_id, "recipient_id": other_id},
            False,
        ),
        ("OOB-disallowed firm", {**base_row, "active_oob_severity": "firm"}, False),
        ("OOB-disallowed hard", {**base_row, "active_oob_severity": "hard"}, False),
        ("deleted", {**base_row, "deleted_at": datetime(2025, 6, 1, tzinfo=UTC)}, False),
        (
            "suppressed",
            {**base_row, "search_suppressed_at": datetime(2025, 6, 1, tzinfo=UTC)},
            False,
        ),
        ("allowed own-thread control", base_row, True),
        ("allowed soft-OOB control", {**base_row, "active_oob_severity": "soft"}, True),
        (
            "allowed observation with null dyad",
            {**base_row, "source_type": "observation", "dyad_id": None},
            True,
        ),
        (
            "allowed distillation with null dyad",
            {
                **base_row,
                "source_type": "distillation",
                "dyad_id": None,
                "thread_owner_partner_share": None,
            },
            True,
        ),
        (
            "firm OOB message remains denied",
            {**base_row, "source_type": "message", "active_oob_severity": "firm"},
            False,
        ),
        (
            "hard OOB message remains denied",
            {**base_row, "source_type": "message", "active_oob_severity": "hard"},
            False,
        ),
        (
            "allowed partner opt-in control",
            {
                **base_row,
                "thread_owner_user_id": partner_id,
                "thread_owner_partner_share": "opt_in",
            },
            True,
        ),
    ]

    observed = {
        label: _visibility_contract_row_visible(
            row,
            viewer_id=viewer_id,
            partner_id=partner_id,
            bot_id="mediator",
            topic_id=topic_id,
            thread_owner_user_id=request_thread_owner_user_id,
            dyad_id=dyad_id,
        )
        for label, row, _expected_visible in scenarios
    }

    assert observed == {
        label: expected_visible for label, _row, expected_visible in scenarios
    }

    pool = RecordingPool()
    await hybrid_search(
        pool,
        _query(
            mode=mode,
            viewer_user_id=viewer_id,
            partner_user_id=partner_id,
            topic_id=topic_id,
            thread_owner_user_id=None,
            dyad_id=dyad_id,
        ),
        embedder=RecordingEmbedder(),
        settings=_settings(),
    )

    assert pool.fetch_sqls
    assert len(pool.fetch_sqls) == (1 if mode == "exact" else 2)
    for sql in pool.fetch_sqls:
        _assert_retrieval_visibility_sql(sql)

    if mode == "exact":
        assert pool.fetch_args == [
            ("deploy crash", "mediator", viewer_id, [viewer_id, partner_id], topic_id, dyad_id, 5)
        ]
    else:
        assert pool.fetch_args[0] == (
            "deploy crash",
            "mediator",
            viewer_id,
            [viewer_id, partner_id],
            topic_id,
            dyad_id,
            5,
        )
        assert pool.fetch_args[1] == (
            [0.6, 0.8, 0.0],
            "test-model",
            3,
            "mediator",
            viewer_id,
            [viewer_id, partner_id],
            topic_id,
            dyad_id,
            5,
        )


def test_rrf_fusion_computes_scores_and_match_metadata_for_exact_semantic_and_both_hits():
    sent_at = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
    both_id = UUID("00000000-0000-4000-8000-000000000101")
    exact_only_id = UUID("00000000-0000-4000-8000-000000000102")
    semantic_only_id = UUID("00000000-0000-4000-8000-000000000103")

    results = retrieval._fuse_rrf_results(
        keyword_rows=[
            {
                "source_type": "message",
                "source_id": both_id,
                "message_id": both_id,
                "sent_at": sent_at,
                "keyword_score": 0.9,
                "keyword_rank": 1,
            },
            {
                "source_type": "message",
                "source_id": exact_only_id,
                "message_id": exact_only_id,
                "sent_at": sent_at,
                "keyword_score": 0.5,
                "keyword_rank": 3,
            },
        ],
        semantic_rows=[
            {
                "source_type": "message",
                "source_id": semantic_only_id,
                "message_id": semantic_only_id,
                "sent_at": sent_at,
                "semantic_rank": 1,
            },
            {
                "source_type": "message",
                "source_id": both_id,
                "message_id": both_id,
                "sent_at": sent_at,
                "semantic_rank": 4,
            },
        ],
        semantic_degraded=False,
        limit=10,
    )

    assert [result.message_id for result in results] == [both_id, semantic_only_id, exact_only_id]
    assert results[0].match_type == "both"
    assert results[0].keyword_rank == 1
    assert results[0].semantic_rank == 4
    assert results[0].rrf_score == pytest.approx((1 / 61) + (1 / 64))
    assert results[1].match_type == "semantic"
    assert results[1].keyword_rank is None
    assert results[1].semantic_rank == 1
    assert results[1].rrf_score == pytest.approx(1 / 61)
    assert results[2].match_type == "exact"
    assert results[2].keyword_rank == 3
    assert results[2].semantic_rank is None
    assert results[2].rrf_score == pytest.approx(1 / 63)


def test_rrf_fusion_applies_limit_after_sorting_and_propagates_semantic_degraded():
    sent_at = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
    newest_id = UUID("00000000-0000-4000-8000-000000000201")
    oldest_id = UUID("00000000-0000-4000-8000-000000000202")
    trimmed_id = UUID("00000000-0000-4000-8000-000000000203")

    results = retrieval._fuse_rrf_results(
        keyword_rows=[
            {
                "message_id": newest_id,
                "sent_at": sent_at,
                "keyword_score": 0.8,
                "keyword_rank": 1,
            },
            {
                "message_id": oldest_id,
                "sent_at": datetime(2025, 6, 1, 11, 0, tzinfo=UTC),
                "keyword_score": 0.7,
                "keyword_rank": 1,
            },
            {
                "message_id": trimmed_id,
                "sent_at": sent_at,
                "keyword_score": 0.6,
                "keyword_rank": 2,
            },
        ],
        semantic_rows=[],
        semantic_degraded=True,
        limit=2,
    )

    assert [result.message_id for result in results] == [newest_id, oldest_id]
    assert all(result.semantic_degraded is True for result in results)
    assert all(result.match_type == "exact" for result in results)
    assert all(result.semantic_rank is None for result in results)


def test_rrf_fusion_keeps_message_only_tie_break_order_stable():
    sent_at = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
    newer_message_id = UUID("00000000-0000-4000-8000-000000000211")
    older_message_id = UUID("00000000-0000-4000-8000-000000000210")

    results = retrieval._fuse_rrf_results(
        keyword_rows=[
            {
                "message_id": older_message_id,
                "sent_at": sent_at,
                "keyword_score": 0.8,
                "keyword_rank": 1,
            },
            {
                "message_id": newer_message_id,
                "sent_at": sent_at,
                "keyword_score": 0.8,
                "keyword_rank": 1,
            },
        ],
        semantic_rows=[],
        semantic_degraded=False,
        limit=10,
    )

    assert [result.message_id for result in results] == [newer_message_id, older_message_id]


def test_rrf_fusion_uses_source_identity_tie_break_for_non_message_hits():
    sent_at = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
    shared_identity = UUID("00000000-0000-4000-8000-000000000221")

    results = retrieval._fuse_rrf_results(
        keyword_rows=[
            {
                "source_type": "artifact",
                "source_id": shared_identity,
                "message_id": None,
                "sent_at": sent_at,
                "keyword_score": 0.8,
                "keyword_rank": 1,
            },
            {
                "source_type": "observation",
                "source_id": shared_identity,
                "message_id": None,
                "sent_at": sent_at,
                "keyword_score": 0.8,
                "keyword_rank": 1,
            },
            {
                "source_type": "message",
                "source_id": shared_identity,
                "message_id": shared_identity,
                "sent_at": sent_at,
                "keyword_score": 0.8,
                "keyword_rank": 1,
            },
        ],
        semantic_rows=[],
        semantic_degraded=False,
        limit=10,
    )

    assert [(result.source_type, result.source_id) for result in results] == [
        ("artifact", shared_identity),
        ("message", shared_identity),
        ("observation", shared_identity),
    ]


@pytest.mark.anyio
async def test_hybrid_mode_degrades_to_keyword_only_on_provider_error():
    message_id = uuid4()
    pool = RecordingPool(
        [
            {
                "message_id": message_id,
                "sent_at": datetime(2025, 6, 1, 12, 0, tzinfo=UTC),
                "keyword_score": 0.5,
                "keyword_rank": 1,
            }
        ]
    )

    results = await hybrid_search(
        pool,
        _query(mode="hybrid"),
        embedder=FailingEmbedder(),
        settings=_settings(),
    )

    assert [result.message_id for result in results] == [message_id]
    assert results[0].semantic_degraded is True
    compact = " ".join((pool.sql or "").split())
    assert "FROM mediator.v_searchable_content sc" in pool.sql
    assert "semantic_rank" not in compact
    assert "rrf_score" not in compact


@pytest.mark.anyio
async def test_hybrid_mode_degrades_to_keyword_only_on_provider_timeout():
    pool = RecordingPool()

    results = await hybrid_search(
        pool,
        _query(mode="hybrid"),
        embedder=SlowEmbedder(),
        settings=_settings(query_embed_timeout_s=0.001),
    )

    assert results == []
    assert pool.fetch_calls == 1


@pytest.mark.anyio
async def test_hybrid_mode_provider_failures_do_not_escape_callers():
    pool = RecordingPool()

    timeout_results = await hybrid_search(
        pool,
        _query(mode="hybrid", query="timeout query"),
        embedder=SlowEmbedder(),
        settings=_settings(query_embed_timeout_s=0.001),
    )
    error_results = await hybrid_search(
        pool,
        _query(mode="hybrid", query="error query"),
        embedder=FailingEmbedder(),
        settings=_settings(),
    )

    assert timeout_results == []
    assert error_results == []
    assert pool.fetch_calls == 2


@pytest.mark.anyio
async def test_exact_mode_excludes_semantic_only_hits_by_skipping_semantic_ann():
    message_id = UUID("00000000-0000-4000-8000-000000000301")
    pool = RecordingPool(
        [
            {
                "message_id": message_id,
                "sent_at": datetime(2025, 6, 1, 12, 0, tzinfo=UTC),
                "keyword_score": 0.5,
                "keyword_rank": 1,
            }
        ]
    )
    calls: list[str] = []

    async def unexpected_prepare_query_embedding(*args, **kwargs):
        calls.append("prepare")
        raise AssertionError("exact mode should not prepare semantic embeddings")

    original = retrieval._prepare_query_embedding
    retrieval._prepare_query_embedding = unexpected_prepare_query_embedding
    try:
        results = await hybrid_search(
            pool,
            _query(mode="exact"),
            embedder=RecordingEmbedder(),
            settings=_settings(),
        )
    finally:
        retrieval._prepare_query_embedding = original

    assert calls == []
    assert [result.message_id for result in results] == [message_id]
    assert all(result.match_type == "exact" for result in results)


@pytest.mark.postgres
@pytest.mark.anyio
async def test_hybrid_semantic_ann_runs_against_pgvector_when_available():
    admin_dsn = os.environ.get("TEST_DATABASE_URL")
    if not admin_dsn:
        pytest.skip("TEST_DATABASE_URL unset; pgvector ANN validation requires it")

    asyncpg = pytest.importorskip("asyncpg")

    admin_conn = await asyncpg.connect(admin_dsn)
    db_name = f"veas_ann_{uuid4().hex[:12]}"
    try:
        vector_available = await admin_conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'vector')"
        )
        if not vector_available:
            pytest.skip("TEST_DATABASE_URL cluster does not have pgvector available")
        await admin_conn.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        await admin_conn.close()

    db_dsn = admin_dsn.rsplit("/", 1)[0] + f"/{db_name}"
    conn = await asyncpg.connect(db_dsn)
    try:
        await conn.execute("CREATE EXTENSION vector")
        await conn.execute("CREATE SCHEMA mediator")
        await conn.execute(
            """
            CREATE TABLE mediator.content_embeddings (
                source_type text NOT NULL,
                source_id uuid NOT NULL,
                model text NOT NULL,
                dimension integer NOT NULL,
                embedding vector(3) NOT NULL,
                PRIMARY KEY (source_type, source_id)
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE mediator.v_searchable_content (
                source_type text NOT NULL,
                source_id uuid NOT NULL,
                message_id uuid,
                bot_id text NOT NULL,
                sender_id uuid,
                recipient_id uuid,
                topic_id uuid,
                thread_owner_user_id uuid,
                thread_owner_partner_share text,
                dyad_id uuid,
                sent_at timestamptz NOT NULL,
                canonical_text text NOT NULL,
                search_tsv tsvector NOT NULL,
                sort_at timestamptz NOT NULL,
                primary_topic_id uuid,
                topic_ids uuid[] NOT NULL DEFAULT ARRAY[]::uuid[],
                source_created_at timestamptz NOT NULL,
                source_updated_at timestamptz NOT NULL
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE mediator.out_of_bounds (
                id uuid PRIMARY KEY,
                owner_id uuid NOT NULL,
                severity text NOT NULL,
                status text NOT NULL
            )
            """
        )
        viewer_id = uuid4()
        message_id = uuid4()
        older_message_id = uuid4()
        await conn.execute(
            """
            INSERT INTO mediator.v_searchable_content (
                source_type, source_id, message_id, bot_id, sender_id, recipient_id,
                thread_owner_user_id, thread_owner_partner_share, sent_at,
                canonical_text, search_tsv, sort_at, source_created_at, source_updated_at
            )
            VALUES (
                'message', $1, $1, 'mediator', $3, $3, $3, 'unset',
                '2025-06-01T12:00:00Z', 'deploy crash',
                to_tsvector('simple'::regconfig, 'deploy crash'),
                '2025-06-01T12:00:00Z', '2025-06-01T12:00:00Z', '2025-06-01T12:00:00Z'
            ),
            (
                'message', $2, $2, 'mediator', $3, $3, $3, 'unset',
                '2025-06-01T11:00:00Z', 'older deployment note',
                to_tsvector('simple'::regconfig, 'older deployment note'),
                '2025-06-01T11:00:00Z', '2025-06-01T11:00:00Z', '2025-06-01T11:00:00Z'
            )
            """,
            message_id,
            older_message_id,
            viewer_id,
        )
        await conn.execute(
            """
            INSERT INTO mediator.content_embeddings (
                source_type, source_id, model, dimension, embedding
            )
            VALUES ('message', $1, 'test-model', 3, '[0.6,0.8,0]'::vector),
                   ('message', $2, 'test-model', 3, '[1,0,0]'::vector)
            """,
            message_id,
            older_message_id,
        )
        pool = await asyncpg.create_pool(db_dsn, min_size=1, max_size=1)
        try:
            results = await hybrid_search(
                pool,
                _query(mode="hybrid", viewer_user_id=viewer_id, limit=1),
                embedder=RecordingEmbedder(vector=[3.0, 4.0, 0.0]),
                settings=_settings(retrieval_hnsw_ef_search=16),
            )
        finally:
            await pool.close()

        assert [result.message_id for result in results] == [message_id]
        assert results[0].semantic_rank == 1
        assert results[0].match_type == "semantic"
    finally:
        await conn.close()
        admin_conn = await asyncpg.connect(admin_dsn)
        try:
            await admin_conn.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
        finally:
            await admin_conn.close()
