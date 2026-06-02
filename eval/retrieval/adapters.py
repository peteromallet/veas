"""Retriever adapters for the retrieval evaluation harness.

Defines the Retriever protocol and its implementations:
- IlikeBaselineRetriever: Pure-python re-implementation of the ILIKE shape
  from the production search_messages (case-insensitive substring match across
  content and media_analysis fields).
- StubSemanticRetriever: Returns empty list deterministically.
- SemanticRetriever: Cosine similarity over local MiniLM dense embeddings,
  with the same scope filtering and deterministic tiebreaker as the baseline.
- HybridRetriever: Reciprocal Rank Fusion (RRF) of the baseline and semantic
  rankings.
- DbBackedRetriever: Sync eval adapter over the production async retriever.

Offline adapters MUST NOT import anything from app.*. DbBackedRetriever is the
only exception: it lazy-imports app.services.retrieval inside the adapter so no
M1 tool consumer depends on production retrieval beyond eval.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import threading
from collections.abc import Mapping
from concurrent.futures import Future
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

from eval.retrieval.schema import Corpus, CorpusMessage, RankedSourceKey, Scope

if TYPE_CHECKING:  # pragma: no cover - typing only
    from eval.retrieval.embeddings import MiniLMEmbedder


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def message_text(msg: CorpusMessage) -> str:
    """Return the full searchable text for a message.

    Concatenates content with any media_analysis explanation/description/summary
    so semantic scoring sees the same signal the ILIKE baseline can match on.
    Deterministic field order: content, explanation, description, summary.
    """
    parts: list[str] = [msg.content]
    ma = msg.media_analysis
    if ma is not None:
        for field in ("explanation", "description", "summary"):
            val = ma.get(field)
            if isinstance(val, str) and val:
                parts.append(val)
    return " ".join(parts)


def _scope_filter(
    messages: list[CorpusMessage],
    scope: Scope,
    thread_id: str | None,
    topic_id: str | None,
) -> list[CorpusMessage]:
    if scope == "thread":
        return [m for m in messages if m.thread_id == thread_id]
    if scope == "topic":
        return [m for m in messages if m.topic_id == topic_id]
    return list(messages)


class Retriever(Protocol):
    """Protocol for retrieval adapters.

    All retrievers must implement this interface so the runner can swap
    between baseline and semantic implementations.
    """

    def retrieve(
        self,
        query: str,
        scope: Scope,
        *,
        thread_id: str | None,
        topic_id: str | None,
        limit: int,
        **extra_scope: Any,
    ) -> list[RankedSourceKey]:
        """Retrieve ranked source keys for a query.

        Args:
            query: The search query string.
            scope: Filter scope ('thread', 'topic', or 'all').
            thread_id: Required for scope=='thread', ignored otherwise.
            topic_id: Required for scope=='topic', ignored otherwise.
            limit: Maximum number of results to return.
            **extra_scope: Additional scope filters (bot_id, participant,
                partner_share, date, etc.) used by DbBackedRetriever.
                Ignored by offline adapters.

        Returns:
            Ordered list of ranked source keys (rank 1 = index 0), truncated to
            limit. Message-only adapters still return ``source_type='message'``
            keys so legacy eval fixtures preserve their prior behavior.
        """
        ...


def _ranked_message_keys(message_ids: list[str]) -> list[RankedSourceKey]:
    return [
        RankedSourceKey(source_type="message", source_id=message_id, rank=rank)
        for rank, message_id in enumerate(message_ids, start=1)
    ]


class IlikeBaselineRetriever:
    """Pure-python re-implementation of production ILIKE search semantics.

    Matches case-insensitive substrings against:
        1. message.content
        2. media_analysis.explanation (if present)
        3. media_analysis.description (if present)
        4. media_analysis.summary (if present)

    Applies scope filter:
        - 'thread': Only messages with matching thread_id.
        - 'topic': Only messages with matching topic_id.
        - 'all': No filter.

    Results are ordered by (sent_at DESC, id DESC) for deterministic ranking
    with tiebreaker per SD3 / callers-3.
    """

    def __init__(self, corpus: Corpus) -> None:
        self._corpus = corpus

    def retrieve(
        self,
        query: str,
        scope: Scope,
        *,
        thread_id: str | None = None,
        topic_id: str | None = None,
        limit: int = 50,
        **extra_scope: Any,
    ) -> list[RankedSourceKey]:
        query_lower = query.lower()

        # Apply scope filter first.
        candidates = self._corpus.messages
        if scope == "thread":
            candidates = [m for m in candidates if m.thread_id == thread_id]
        elif scope == "topic":
            candidates = [m for m in candidates if m.topic_id == topic_id]
        # scope == 'all': no filter

        # Case-insensitive substring match against content and media_analysis.
        matches = []
        for msg in candidates:
            if query_lower in msg.content.lower():
                matches.append(msg)
                continue

            ma = msg.media_analysis
            if ma is not None:
                # Check each media_analysis field.
                explanation = ma.get("explanation")
                if isinstance(explanation, str) and query_lower in explanation.lower():
                    matches.append(msg)
                    continue

                description = ma.get("description")
                if isinstance(description, str) and query_lower in description.lower():
                    matches.append(msg)
                    continue

                summary = ma.get("summary")
                if isinstance(summary, str) and query_lower in summary.lower():
                    matches.append(msg)
                    continue

        # Order by (sent_at DESC, id DESC) per SD3.
        matches.sort(key=lambda m: (m.sent_at, m.id), reverse=True)

        # Slice to limit.
        return _ranked_message_keys([m.id for m in matches[:limit]])


class StubSemanticRetriever:
    """Deterministic stub retriever that always returns an empty list.

    Used as a placeholder for the semantic retriever implementation.
    Returns [] for every query, scope, and limit combination.
    """

    def __init__(self, corpus: Corpus) -> None:
        self._corpus = corpus

    def retrieve(
        self,
        query: str,
        scope: Scope,
        *,
        thread_id: str | None = None,
        topic_id: str | None = None,
        limit: int = 50,
        **extra_scope: Any,
    ) -> list[RankedSourceKey]:
        return []


class SemanticRetriever:
    """Dense-embedding retriever using cosine similarity over MiniLM vectors.

    Builds an L2-normalized embedding matrix over the full corpus text
    (content + media_analysis) once, then for each query embeds it and ranks
    candidates by cosine similarity (= dot product, since vectors are
    normalized). Applies the SAME scope filter as the baseline so the only
    difference being measured is lexical-vs-semantic matching, not scoping.

    Ranking: primary key cosine score DESC; deterministic tiebreaker
    (sent_at DESC, id DESC) matches the baseline so equal-score ties are
    resolved identically across adapters.
    """

    def __init__(self, corpus: Corpus, embedder: "MiniLMEmbedder | None" = None) -> None:
        from eval.retrieval.embeddings import MiniLMEmbedder

        self._corpus = corpus
        self._embedder = embedder or MiniLMEmbedder()
        self._messages = list(corpus.messages)
        texts = [message_text(m) for m in self._messages]
        self._matrix = self._embedder.embed_corpus(texts)  # (N, 384)
        self._index_by_id = {m.id: i for i, m in enumerate(self._messages)}

    def retrieve(
        self,
        query: str,
        scope: Scope,
        *,
        thread_id: str | None = None,
        topic_id: str | None = None,
        limit: int = 50,
        **extra_scope: Any,
    ) -> list[RankedSourceKey]:
        candidates = _scope_filter(self._messages, scope, thread_id, topic_id)
        if not candidates:
            return []

        qvec = self._embedder.embed_query(query)
        scored: list[tuple[float, CorpusMessage]] = []
        for msg in candidates:
            row = self._matrix[self._index_by_id[msg.id]]
            score = float(row @ qvec)  # cosine, vectors are normalized
            scored.append((score, msg))

        # Sort by (score DESC, sent_at DESC, id DESC) deterministically.
        scored.sort(key=lambda t: (t[0], t[1].sent_at, t[1].id), reverse=True)
        return _ranked_message_keys([m.id for _, m in scored[:limit]])


class HybridRetriever:
    """Reciprocal Rank Fusion (RRF) of the baseline and semantic rankings.

    For each candidate, RRF score = sum over rankers of 1 / (k + rank), where
    rank is 1-indexed and k=60 (Cormack et al. default). A document missing
    from a ranker simply contributes nothing from that ranker. This rewards
    documents ranked highly by *either* retriever and is robust to score-scale
    differences between lexical and semantic scorers.

    Both sub-rankers are queried over the full candidate set (limit large)
    before fusion so the fusion sees complete rankings, then the fused list is
    truncated to `limit`. Deterministic tiebreaker (sent_at DESC, id DESC).
    """

    RRF_K = 60

    def __init__(
        self,
        corpus: Corpus,
        embedder: "MiniLMEmbedder | None" = None,
        *,
        baseline: IlikeBaselineRetriever | None = None,
        semantic: SemanticRetriever | None = None,
    ) -> None:
        self._corpus = corpus
        self._baseline = baseline or IlikeBaselineRetriever(corpus)
        self._semantic = semantic or SemanticRetriever(corpus, embedder)
        self._msg_by_id = {m.id: m for m in corpus.messages}

    def retrieve(
        self,
        query: str,
        scope: Scope,
        *,
        thread_id: str | None = None,
        topic_id: str | None = None,
        limit: int = 50,
        **extra_scope: Any,
    ) -> list[RankedSourceKey]:
        full = len(self._corpus.messages)
        kwargs = dict(scope=scope, thread_id=thread_id, topic_id=topic_id, limit=full)
        lex = self._baseline.retrieve(query, **kwargs)
        sem = self._semantic.retrieve(query, **kwargs)

        rrf: dict[str, float] = {}
        for ranking in (lex, sem):
            for item in ranking:
                rrf[item.source_id] = rrf.get(item.source_id, 0.0) + 1.0 / (self.RRF_K + item.rank)

        def sort_key(mid: str) -> tuple[float, object, str]:
            msg = self._msg_by_id[mid]
            return (rrf[mid], msg.sent_at, msg.id)

        fused = sorted(rrf.keys(), key=sort_key, reverse=True)
        return _ranked_message_keys(fused[:limit])


# ---------------------------------------------------------------------------
# DbBackedRetriever — production retrieval service adapter
# ---------------------------------------------------------------------------


class DbBackedRetriever:
    """Retriever that delegates to the production async hybrid_search service.

    This is intentionally the only eval adapter allowed to import from app.*.
    It reads DIRECT_DATABASE_URL directly from os.environ, owns a background
    event loop so the public retrieve(...) method stays synchronous, and closes
    both the async pool and loop via close().

    Golden-set query types drive production mode selection:
      - verbatim_quote -> exact
      - paraphrase -> hybrid
      - all other types default to hybrid
    """

    def __init__(
        self,
        corpus: Corpus,
        *,
        source_weight_map: Mapping[str, float] | None = None,
    ) -> None:
        self._corpus = corpus  # kept for interface compatibility

        # Intentionally read from os.environ here instead of app Settings: eval
        # DB usage is opt-in and must point at a direct session-mode URL.
        db_url = os.environ.get("DIRECT_DATABASE_URL")
        if not db_url:
            raise ValueError(
                "DIRECT_DATABASE_URL must be set to use DbBackedRetriever"
            )

        self._db_url = db_url
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever,
            name="eval-db-retriever",
            daemon=True,
        )
        self._thread.start()
        self._pool: Any | None = None
        self._closed = False
        self._source_weight_map = (
            {str(source_type): float(weight) for source_type, weight in source_weight_map.items()}
            if source_weight_map is not None
            else None
        )

    def retrieve(
        self,
        query: str,
        scope: Scope,
        *,
        thread_id: str | None = None,
        topic_id: str | None = None,
        limit: int = 50,
        **extra_scope: Any,
    ) -> list[RankedSourceKey]:
        return self._run(
            self._retrieve_async(
                query=query,
                scope=scope,
                thread_id=thread_id,
                topic_id=topic_id,
                limit=limit,
                extra_scope=extra_scope,
            )
        )

    async def _retrieve_async(
        self,
        *,
        query: str,
        scope: Scope,
        thread_id: str | None,
        topic_id: str | None,
        limit: int,
        extra_scope: dict[str, Any],
    ) -> list[RankedSourceKey]:
        retrieval = importlib.import_module("app.services.retrieval")
        pool = await self._get_pool()
        request = retrieval.RetrievalQuery(
            query=query,
            viewer_user_id=self._required_uuid(extra_scope, "viewer_user_id"),
            bot_id=str(extra_scope.get("bot_id") or "mediator"),
            partner_user_id=self._optional_uuid(extra_scope.get("partner_user_id")),
            topic_id=self._optional_uuid(extra_scope.get("topic_id") or topic_id),
            thread_owner_user_id=self._optional_uuid(
                extra_scope.get("thread_owner_user_id")
                or (thread_id if scope == "thread" else None)
            ),
            dyad_id=self._optional_uuid(extra_scope.get("dyad_id")),
            mode=self._mode_for_query_type(extra_scope.get("query_type")),
            limit=limit,
        )
        results = await retrieval.hybrid_search(
            pool,
            request,
            source_weight_map=self._source_weight_map,
        )
        return [
            RankedSourceKey(
                source_type=getattr(result, "source_type", "message"),
                source_id=str(getattr(result, "source_id", result.message_id)),
                rank=rank,
            )
            for rank, result in enumerate(results, start=1)
            if getattr(result, "source_id", result.message_id) is not None
        ]

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool

        asyncpg = importlib.import_module("asyncpg")

        async def init_connection(conn: Any) -> None:
            try:
                pgvector_asyncpg = importlib.import_module("pgvector.asyncpg")
            except ImportError:
                return
            await pgvector_asyncpg.register_vector(conn)

        self._pool = await asyncpg.create_pool(
            self._db_url,
            min_size=1,
            max_size=4,
            statement_cache_size=0,
            init=init_connection,
        )
        return self._pool

    def close(self) -> None:
        """Close the adapter-owned async pool and background event loop."""

        if self._closed:
            return
        if self._pool is not None:
            self._run(self._pool.close())
            self._pool = None
        self._closed = True
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        self._loop.close()

    def __enter__(self) -> "DbBackedRetriever":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def __del__(self) -> None:
        if getattr(self, "_closed", True):
            return
        try:
            self.close()
        except Exception:
            pass

    def _run(self, coro: Any) -> Any:
        if self._closed:
            raise RuntimeError("DbBackedRetriever is closed")
        future: Future[Any] = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result()

    @staticmethod
    def _mode_for_query_type(query_type: Any) -> str:
        if query_type == "verbatim_quote":
            return "exact"
        return "hybrid"

    @staticmethod
    def _optional_uuid(value: Any) -> UUID | None:
        if value in (None, ""):
            return None
        if isinstance(value, UUID):
            return value
        return UUID(str(value))

    @classmethod
    def _required_uuid(cls, extra_scope: dict[str, Any], name: str) -> UUID:
        value = extra_scope.get(name)
        if value in (None, ""):
            raise ValueError(f"{name} is required for DbBackedRetriever")
        return cls._optional_uuid(value)  # type: ignore[return-value]
