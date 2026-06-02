from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


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


class CorpusMemory(BaseModel):
    """A synthetic memory entry for eval retrieval."""

    id: str
    topic_id: str
    content: str
    visibility: str = "private"
    created_at: datetime | None = None


class CorpusObservation(BaseModel):
    """A synthetic observation entry for eval retrieval."""

    id: str
    topic_id: str
    content: str
    confidence: str = "medium"
    significance: int = 3
    created_at: datetime | None = None


class CorpusDistillation(BaseModel):
    """A synthetic distillation entry for eval retrieval."""

    id: str
    topic_id: str
    content: str
    visibility: str = "private"
    created_at: datetime | None = None


class CorpusArtifact(BaseModel):
    """A synthetic artifact entry for eval retrieval."""

    id: str
    topic_id: str
    title: str
    summary: str
    artifact_type: str = "review_summary"
    created_at: datetime | None = None


class CorpusConversationNote(BaseModel):
    """A synthetic conversation note entry for eval retrieval."""

    id: str
    topic_id: str
    text: str
    created_at: datetime | None = None


class CorpusTheme(BaseModel):
    """A synthetic theme entry for eval retrieval."""

    id: str
    topic_id: str
    title: str
    description: str | None = None
    status: str = "active"
    created_at: datetime | None = None


SourceType = Literal[
    "message",
    "memory",
    "observation",
    "distillation",
    "artifact",
    "conversation_note",
    "theme",
]


class SourceKey(BaseModel):
    """Stable identifier for a retrieval source row."""

    source_type: SourceType
    source_id: str


class RankedSourceKey(SourceKey):
    """Source key plus 1-indexed rank for scored retrieval outputs."""

    rank: int


QueryType = Literal[
    "topic_recall",
    "verbatim_quote",
    "paraphrase",
    "cross_thread",
    "knowledge_recall",
    "exact_source_quote",
]

Scope = Literal["thread", "topic", "all"]


class GoldenCase(BaseModel):
    """A single golden test case against which retrievers are evaluated."""

    id: str
    query: str
    expected_source_keys: list[SourceKey] = Field(default_factory=list)
    expected_message_ids: list[str] = Field(default_factory=list)
    scope: Scope
    query_type: QueryType
    intent: Literal["know_about", "exact_said"] | None = None
    difficulty: Literal["easy", "medium", "hard"] | None = None
    fairness: (
        Literal["keyword_favored", "semantic_favored", "either", "adversarial"] | None
    ) = None
    thread_id: str | None = None
    topic_id: str | None = None
    notes: str | None = None
    extra_scope: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _normalize_expected_keys(self) -> "GoldenCase":
        normalized: list[SourceKey] = []
        seen: set[tuple[str, str]] = set()

        for key in self.expected_source_keys:
            pair = (key.source_type, key.source_id)
            if pair not in seen:
                normalized.append(key)
                seen.add(pair)

        for message_id in self.expected_message_ids:
            pair = ("message", message_id)
            if pair not in seen:
                normalized.append(SourceKey(source_type="message", source_id=message_id))
                seen.add(pair)

        self.expected_source_keys = normalized
        self.expected_message_ids = [
            key.source_id for key in normalized if key.source_type == "message"
        ]
        return self


class GoldenSet(BaseModel):
    """A collection of golden cases."""

    cases: list[GoldenCase]


class Corpus(BaseModel):
    """A collection of corpus messages and non-message source entries.

    Message-only adapters (IlikeBaselineRetriever, SemanticRetriever, etc.)
    consume only ``messages`` and ignore other collections. Non-message
    collections default to empty lists so existing corpus.yaml files remain
    valid without change.
    """

    messages: list[CorpusMessage] = Field(default_factory=list)
    memories: list[CorpusMemory] = Field(default_factory=list)
    observations: list[CorpusObservation] = Field(default_factory=list)
    distillations: list[CorpusDistillation] = Field(default_factory=list)
    artifacts: list[CorpusArtifact] = Field(default_factory=list)
    conversation_notes: list[CorpusConversationNote] = Field(default_factory=list)
    themes: list[CorpusTheme] = Field(default_factory=list)
