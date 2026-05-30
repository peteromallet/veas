"""Retriever adapters for the retrieval evaluation harness.

Defines the Retriever protocol and two implementations:
- IlikeBaselineRetriever: Pure-python re-implementation of the ILIKE shape
  from the production search_messages (case-insensitive substring match across
  content and media_analysis fields).
- StubSemanticRetriever: Returns empty list deterministically.

MUST NOT import anything from app.* — this module is a pure-python
re-implementation from documentation only.
"""

from __future__ import annotations

from typing import Protocol

from eval.retrieval.schema import Corpus, Scope


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
    ) -> list[str]:
        """Retrieve ranked message ids for a query.

        Args:
            query: The search query string.
            scope: Filter scope ('thread', 'topic', or 'all').
            thread_id: Required for scope=='thread', ignored otherwise.
            topic_id: Required for scope=='topic', ignored otherwise.
            limit: Maximum number of results to return.

        Returns:
            Ordered list of message ids (rank 1 = index 0), truncated to limit.
        """
        ...


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
    ) -> list[str]:
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
        return [m.id for m in matches[:limit]]


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
    ) -> list[str]:
        return []
