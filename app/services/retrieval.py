"""Production retrieval service.

M1 starts with the exact/keyword path.  Later tasks add query embeddings,
ANN, and RRF fusion on top of the request/result contract defined here.
"""

from __future__ import annotations

import asyncio
import time
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from app.config import Settings, get_settings
from app.services.embeddings import Embedder, embedder_from_settings, normalize_vector

SearchMode = Literal["exact", "hybrid"]
MatchType = Literal["exact", "semantic", "both"]


@dataclass(slots=True)
class _CachedQueryEmbedding:
    expires_at: float
    vector: list[float]


@dataclass(frozen=True, slots=True)
class _PreparedQueryEmbedding:
    model: str
    dimension: int
    vector: list[float]


_QUERY_EMBEDDING_CACHE: OrderedDict[tuple[str, str], _CachedQueryEmbedding] = OrderedDict()


@dataclass(frozen=True, slots=True)
class RetrievalQuery:
    """Inputs shared by exact and hybrid retrieval.

    ``thread_owner_user_id`` is the thread scope used by
    ``mediator.v_searchable_messages``.  ``dyad_id`` is accepted now so later
    visibility tasks can tighten cross-thread/cross-partner rules without
    changing the public call shape.
    """

    query: str
    viewer_user_id: UUID
    bot_id: str
    partner_user_id: UUID | None = None
    topic_id: UUID | None = None
    thread_owner_user_id: UUID | None = None
    dyad_id: UUID | None = None
    mode: SearchMode = "hybrid"
    limit: int = 10


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    message_id: UUID
    match_type: MatchType
    rrf_score: float | None
    keyword_rank: int | None
    semantic_rank: int | None
    semantic_degraded: bool
    keyword_score: float | None = None
    sent_at: Any | None = None


RRF_K = 60


async def hybrid_search(
    pool: Any,
    request: RetrievalQuery,
    *,
    embedder: Embedder | None = None,
    settings: Settings | None = None,
) -> list[RetrievalResult]:
    """Run production retrieval for *request*.

    Exact mode is deliberately keyword-only: it never attempts query embedding,
    never returns semantic-only hits, and therefore reports
    ``semantic_degraded=False``.
    """

    if not request.query.strip():
        return []

    if request.mode == "exact":
        return await _keyword_search(pool, request, semantic_degraded=False)

    prepared_embedding = await _prepare_query_embedding(
        request.query,
        embedder=embedder,
        settings=settings,
    )
    if prepared_embedding is None:
        return await _keyword_search(pool, request, semantic_degraded=True)
    keyword_rows = await _fetch_keyword_matches(pool, request, limit=_positive_limit(request.limit))
    semantic_rows = await _fetch_semantic_matches(
        pool,
        request,
        prepared_embedding=prepared_embedding,
        settings=settings,
        limit=_positive_limit(request.limit),
    )
    return _fuse_rrf_results(
        keyword_rows=keyword_rows,
        semantic_rows=semantic_rows,
        semantic_degraded=False,
        limit=_positive_limit(request.limit),
    )


async def _prepare_query_embedding(
    query: str,
    *,
    embedder: Embedder | None,
    settings: Settings | None,
) -> _PreparedQueryEmbedding | None:
    normalized_query = normalize_query_for_embedding(query)
    if not normalized_query:
        return _PreparedQueryEmbedding(model="", dimension=0, vector=[])

    settings = settings or get_settings()
    embedder = embedder or embedder_from_settings(settings)
    cache_key = (embedder.model_name, normalized_query)
    now = time.monotonic()

    cached = _QUERY_EMBEDDING_CACHE.get(cache_key)
    if cached is not None:
        if cached.expires_at >= now:
            _QUERY_EMBEDDING_CACHE.move_to_end(cache_key)
            return _PreparedQueryEmbedding(
                model=embedder.model_name,
                dimension=embedder.dimension,
                vector=cached.vector,
            )
        _QUERY_EMBEDDING_CACHE.pop(cache_key, None)

    try:
        vectors = await asyncio.wait_for(
            embedder.embed_texts([normalized_query]),
            timeout=settings.query_embed_timeout_s,
        )
        if len(vectors) != 1:
            raise ValueError(f"query embedder returned {len(vectors)} vectors for 1 query")
        vector = normalize_vector(vectors[0], dimension=embedder.dimension)
    except Exception:
        return None

    _cache_query_embedding(
        cache_key,
        vector,
        ttl_s=settings.query_embed_cache_ttl_s,
        max_entries=settings.query_embed_cache_max_entries,
    )
    return _PreparedQueryEmbedding(
        model=embedder.model_name,
        dimension=embedder.dimension,
        vector=vector,
    )


def normalize_query_for_embedding(query: str) -> str:
    """Return the stable query text used for provider calls and cache keys."""

    return " ".join(unicodedata.normalize("NFC", query).split())


def _cache_query_embedding(
    cache_key: tuple[str, str],
    vector: list[float],
    *,
    ttl_s: int,
    max_entries: int,
) -> None:
    if ttl_s <= 0 or max_entries <= 0:
        return
    _QUERY_EMBEDDING_CACHE[cache_key] = _CachedQueryEmbedding(
        expires_at=time.monotonic() + ttl_s,
        vector=vector,
    )
    _QUERY_EMBEDDING_CACHE.move_to_end(cache_key)
    while len(_QUERY_EMBEDDING_CACHE) > max_entries:
        _QUERY_EMBEDDING_CACHE.popitem(last=False)


async def _keyword_search(
    pool: Any,
    request: RetrievalQuery,
    *,
    semantic_degraded: bool,
) -> list[RetrievalResult]:
    limit = _positive_limit(request.limit)
    rows = await _fetch_keyword_matches(pool, request, limit=limit)
    return [
        RetrievalResult(
            message_id=row["message_id"],
            match_type="exact",
            rrf_score=None,
            keyword_rank=row["keyword_rank"],
            semantic_rank=None,
            semantic_degraded=semantic_degraded,
            keyword_score=float(row["keyword_score"]),
            sent_at=row["sent_at"],
        )
        for row in rows
    ]


async def _fetch_keyword_matches(
    pool: Any,
    request: RetrievalQuery,
    *,
    limit: int,
) -> list[Any]:
    filters, params, next_param = _visibility_filters(
        request,
        initial_params=[request.query],
        next_param=2,
    )

    limit_param = next_param
    params.append(limit)

    where_clause = "\n          AND ".join(filters)
    sql = f"""
        WITH query AS (
            SELECT websearch_to_tsquery('simple'::regconfig, $1) AS tsq
        ),
        keyword_matches AS (
            SELECT
                m.message_id,
                m.sent_at,
                ts_rank(m.search_tsv, query.tsq, 32) AS keyword_score,
                row_number() OVER (
                    ORDER BY
                        ts_rank(m.search_tsv, query.tsq, 32) DESC,
                        m.sent_at DESC,
                        m.message_id DESC
                ) AS keyword_rank
            FROM mediator.v_searchable_messages m
            CROSS JOIN query
            WHERE m.search_tsv @@ query.tsq
              AND {where_clause}
        )
        SELECT
            message_id,
            sent_at,
            keyword_score,
            keyword_rank
        FROM keyword_matches
        ORDER BY keyword_rank ASC
        LIMIT ${limit_param}
    """

    rows = await pool.fetch(sql, *params)
    return list(rows)


async def _semantic_search(
    pool: Any,
    request: RetrievalQuery,
    *,
    prepared_embedding: _PreparedQueryEmbedding,
    settings: Settings | None,
) -> list[RetrievalResult]:
    limit = _positive_limit(request.limit)
    rows = await _fetch_semantic_matches(
        pool,
        request,
        prepared_embedding=prepared_embedding,
        settings=settings,
        limit=limit,
    )
    return [
        RetrievalResult(
            message_id=row["message_id"],
            match_type="semantic",
            rrf_score=None,
            keyword_rank=None,
            semantic_rank=row["semantic_rank"],
            semantic_degraded=False,
            keyword_score=None,
            sent_at=row["sent_at"],
        )
        for row in rows
    ]


async def _fetch_semantic_matches(
    pool: Any,
    request: RetrievalQuery,
    *,
    prepared_embedding: _PreparedQueryEmbedding,
    settings: Settings | None,
    limit: int,
) -> list[Any]:
    settings = settings or get_settings()
    filters = [
        "e.model = $2",
        "e.dimension = $3",
    ]
    params: list[Any] = [
        prepared_embedding.vector,
        prepared_embedding.model,
        prepared_embedding.dimension,
    ]
    visibility_filters, params, next_param = _visibility_filters(
        request,
        initial_params=params,
        next_param=4,
    )
    filters.extend(visibility_filters)

    limit_param = next_param
    params.append(limit)

    where_clause = "\n              AND ".join(filters)
    sql = f"""
        WITH semantic_matches AS (
            SELECT
                m.message_id,
                m.sent_at,
                e.embedding <=> $1 AS cosine_distance,
                row_number() OVER (
                    ORDER BY
                        e.embedding <=> $1 ASC,
                        m.sent_at DESC,
                        m.message_id DESC
                ) AS semantic_rank
            FROM mediator.content_embeddings e
            JOIN mediator.v_searchable_content sc
              ON sc.source_type = e.source_type
             AND sc.source_id = e.source_id
             AND sc.source_type = 'message'
            JOIN mediator.v_searchable_messages m
              ON m.message_id = sc.message_id
            WHERE {where_clause}
        )
        SELECT
            message_id,
            sent_at,
            cosine_distance,
            semantic_rank
        FROM semantic_matches
        ORDER BY semantic_rank ASC
        LIMIT ${limit_param}
    """

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                f"SET LOCAL hnsw.ef_search = {int(settings.retrieval_hnsw_ef_search)}"
            )
            rows = await conn.fetch(sql, *params)

    return list(rows)


def _visibility_filters(
    request: RetrievalQuery,
    *,
    initial_params: list[Any],
    next_param: int,
) -> tuple[list[str], list[Any], int]:
    """Return SQL predicates equivalent to the legacy raw-message read gates.

    Deleted/search-suppressed rows are intentionally delegated to
    ``mediator.v_searchable_messages``.  M1 also excludes any active firm/hard
    OOB owned by the message thread owner.  Product policy for finer-grained
    OOB-scoped retrieval is not settled yet, so this conservative rule prevents
    semantic retrieval from surfacing protected raw material until that policy
    is clarified.
    """

    params = list(initial_params)
    participant_ids = [request.viewer_user_id]
    if request.partner_user_id is not None:
        participant_ids.append(request.partner_user_id)

    bot_param = next_param
    params.append(request.bot_id)
    next_param += 1

    viewer_param = next_param
    params.append(request.viewer_user_id)
    next_param += 1

    participants_param = next_param
    params.append(participant_ids)
    next_param += 1

    filters = [
        f"m.bot_id = ${bot_param}",
        f"m.thread_owner_user_id = ANY(${participants_param}::uuid[])",
        (
            f"(m.sender_id = ANY(${participants_param}::uuid[]) "
            f"OR m.recipient_id = ANY(${participants_param}::uuid[]))"
        ),
        (
            f"(m.thread_owner_user_id = ${viewer_param} "
            "OR m.thread_owner_partner_share = 'opt_in')"
        ),
        """
        NOT EXISTS (
            SELECT 1
            FROM mediator.out_of_bounds x
            WHERE x.owner_id = m.thread_owner_user_id
              AND x.status = 'active'
              AND x.severity IN ('firm', 'hard')
        )
        """,
    ]

    if request.topic_id is not None:
        filters.append(f"m.topic_id = ${next_param}")
        params.append(request.topic_id)
        next_param += 1

    if request.thread_owner_user_id is not None:
        filters.append(f"m.thread_owner_user_id = ${next_param}")
        params.append(request.thread_owner_user_id)
        next_param += 1

    if request.dyad_id is not None:
        filters.append(f"m.dyad_id = ${next_param}")
        params.append(request.dyad_id)
        next_param += 1

    return filters, params, next_param


def _fuse_rrf_results(
    *,
    keyword_rows: list[Any],
    semantic_rows: list[Any],
    semantic_degraded: bool,
    limit: int,
) -> list[RetrievalResult]:
    by_message_id: dict[UUID, dict[str, Any]] = {}

    for row in keyword_rows:
        message_id = row["message_id"]
        by_message_id[message_id] = {
            "message_id": message_id,
            "sent_at": row["sent_at"],
            "keyword_score": float(row["keyword_score"]),
            "keyword_rank": int(row["keyword_rank"]),
            "semantic_rank": None,
        }

    for row in semantic_rows:
        message_id = row["message_id"]
        existing = by_message_id.setdefault(
            message_id,
            {
                "message_id": message_id,
                "sent_at": row["sent_at"],
                "keyword_score": None,
                "keyword_rank": None,
                "semantic_rank": None,
            },
        )
        existing["semantic_rank"] = int(row["semantic_rank"])
        if existing["sent_at"] is None:
            existing["sent_at"] = row["sent_at"]

    fused = []
    for item in by_message_id.values():
        keyword_rank = item["keyword_rank"]
        semantic_rank = item["semantic_rank"]
        rrf_score = 0.0
        if keyword_rank is not None:
            rrf_score += 1.0 / (RRF_K + keyword_rank)
        if semantic_rank is not None:
            rrf_score += 1.0 / (RRF_K + semantic_rank)
        match_type: MatchType
        if keyword_rank is not None and semantic_rank is not None:
            match_type = "both"
        elif keyword_rank is not None:
            match_type = "exact"
        else:
            match_type = "semantic"
        fused.append(
            RetrievalResult(
                message_id=item["message_id"],
                match_type=match_type,
                rrf_score=rrf_score,
                keyword_rank=keyword_rank,
                semantic_rank=semantic_rank,
                semantic_degraded=semantic_degraded,
                keyword_score=item["keyword_score"],
                sent_at=item["sent_at"],
            )
        )

    return sorted(
        fused,
        key=lambda result: (
            -(result.rrf_score or 0.0),
            -_sort_timestamp(result.sent_at),
            -result.message_id.int,
        ),
    )[:limit]


def _sort_timestamp(value: Any) -> float:
    if hasattr(value, "timestamp"):
        return float(value.timestamp())
    return 0.0


def _positive_limit(limit: int) -> int:
    if limit < 1:
        raise ValueError("retrieval limit must be positive")
    return limit
