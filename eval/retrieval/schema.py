from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class CorpusMessage(BaseModel):
    """A single message in the synthetic corpus."""

    id: str
    thread_id: str
    topic_id: str
    sender: str
    recipient: str
    sent_at: datetime
    content: str
    media_analysis: dict | None = None


QueryType = Literal["topic_recall", "verbatim_quote", "paraphrase", "cross_thread"]

Scope = Literal["thread", "topic", "all"]


class GoldenCase(BaseModel):
    """A single golden test case against which retrievers are evaluated."""

    id: str
    query: str
    expected_message_ids: list[str]
    scope: Scope
    query_type: QueryType
    difficulty: Literal["easy", "medium", "hard"] | None = None
    fairness: (
        Literal["keyword_favored", "semantic_favored", "either", "adversarial"] | None
    ) = None
    thread_id: str | None = None
    topic_id: str | None = None
    notes: str | None = None
    extra_scope: dict[str, Any] | None = None


class GoldenSet(BaseModel):
    """A collection of golden cases."""

    cases: list[GoldenCase]


class Corpus(BaseModel):
    """A collection of corpus messages."""

    messages: list[CorpusMessage]
