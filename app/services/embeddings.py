"""Shared embedding contract for Xen M1 retrieval.

This module is the single source of truth for canonical message text,
content hashes, vector normalization, and provider-specific embedders.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from app.config import Settings, get_settings


DEFAULT_OPENAI_EMBEDDING_MODEL = "text-embedding-3-small"
DEFAULT_OPENAI_EMBEDDING_DIMENSION = 1536
LOCAL_BGE_SMALL_DIMENSION = 384

_CANONICAL_MEDIA_FIELDS = ("explanation", "description", "summary")
_ARTIFACT_AGENDA_ITEM_FIELDS = ("title", "intent", "ask", "done_when")


class EmbeddingError(RuntimeError):
    """Raised when an embedder cannot satisfy the shared embedding contract."""


@runtime_checkable
class Embedder(Protocol):
    """Async embedding provider interface used by workers and retrieval."""

    model_name: str
    dimension: int

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        """Return one L2-normalized vector per input text, preserving order."""


def _coerce_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _normalize_text_for_hash(text: str) -> str:
    return unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")


def canonical_embedding_text(
    content: str | None = None,
    media_analysis: Mapping[str, Any] | None = None,
) -> str:
    """Return the canonical text embedded and hashed for a message.

    Field order mirrors migration 0056:
    content, media_analysis.explanation, media_analysis.description,
    media_analysis.summary. Missing values are treated as empty strings and the
    four fields are joined with a single newline.
    """

    media = media_analysis or {}
    fields = [_coerce_text(content)]
    fields.extend(_coerce_text(media.get(field)) for field in _CANONICAL_MEDIA_FIELDS)
    return "\n".join(fields)


def canonical_raw_content_text(content: Any | None = None) -> str:
    """Return canonical text for raw-content non-message rows.

    M1 embeds eligible memory, observation, and private-only distillation rows
    from their raw ``content`` column. Missing content returns an empty string
    so callers can skip/drop without special-case exception handling.
    """

    return _coerce_text(content)


def canonical_memory_embedding_text(content: Any | None = None) -> str:
    """Return M1 canonical text for an eligible memory row."""

    return canonical_raw_content_text(content)


def canonical_observation_embedding_text(content: Any | None = None) -> str:
    """Return M1 canonical text for an eligible observation row."""

    return canonical_raw_content_text(content)


def canonical_distillation_embedding_text(content: Any | None = None) -> str:
    """Return M1 canonical text for an eligible private distillation row.

    Distillation eligibility is enforced by the SQL/search lifecycle: M1 only
    indexes active private distillations, not dyad-shareable summaries.
    """

    return canonical_raw_content_text(content)


def canonical_conversation_note_embedding_text(text: Any | None = None) -> str:
    """Return M4 canonical text for a conversation note row."""

    return canonical_raw_content_text(text)


def canonical_theme_embedding_text(
    title: Any | None = None,
    description: Any | None = None,
) -> str:
    """Return M4 canonical text for a theme row.

    This mirrors the SQL searchable-content contract exactly: only the human
    readable ``title`` + ``description`` fields participate, in that order.
    Theme status / sentiment / health stay out of the embedding payload.
    """

    return _join_artifact_fields([
        _coerce_text(title) if title is not None else None,
        _coerce_text(description) if description is not None else None,
    ])


def _payload_text_at(payload: Mapping[str, Any], path: Sequence[str]) -> str | None:
    value: Any = payload
    for key in path:
        if not isinstance(value, Mapping) or key not in value:
            return None
        value = value[key]
    if value is None:
        return None
    return _coerce_text(value)


def _payload_text_or_lines(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        lines = [_coerce_text(item) for item in value if item is not None]
        return "\n".join(lines) if lines else None
    return _coerce_text(value)


def _agenda_items_text(items: Any) -> str | None:
    if not isinstance(items, list):
        return None

    rendered: list[str] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        fields = [
            _coerce_text(item[field])
            for field in _ARTIFACT_AGENDA_ITEM_FIELDS
            if item.get(field) is not None
        ]
        if fields:
            rendered.append("\n".join(fields))
    return "\n".join(rendered) if rendered else None


def _join_artifact_fields(values: Sequence[str | None]) -> str:
    return "\n".join(value for value in values if value is not None).strip()


def canonical_artifact_embedding_text(
    artifact_type: str | None,
    payload: Mapping[str, Any] | None,
) -> str:
    """Return M1 canonical text for a conversation artifact payload.

    Extraction is intentionally type-aware and limited to human-readable fields.
    Structural fields such as ids, revision metadata, evidence references, and
    turn bookkeeping are ignored so artifact hashes track searchable prose.
    Unknown artifact types fall back to the same approved prose paths used by
    ``mediator.v_searchable_content``.
    """

    if not isinstance(payload, Mapping):
        return ""

    agenda = payload.get("agenda")
    agenda_payload = agenda if isinstance(agenda, Mapping) else {}
    fallback_values = [
        _payload_text_at(payload, ("summary",)),
        _payload_text_at(payload, ("title",)),
        _payload_text_at(payload, ("notes",)),
        _payload_text_at(payload, ("review_summary",)),
        _payload_text_at(payload, ("live_debrief", "review_summary")),
        _payload_text_at(payload, ("prep_summary",)),
        _payload_text_at(agenda_payload, ("prep_summary",)),
        _payload_text_or_lines(payload.get("transcript_reflection")),
        _payload_text_or_lines(payload.get("what_heard")),
        _payload_text_or_lines(payload.get("what_decided")),
        _payload_text_or_lines(payload.get("still_open")),
        _payload_text_or_lines(payload.get("what_to_remember")),
        _payload_text_or_lines(payload.get("durable_write_summary")),
        _payload_text_or_lines(payload.get("open_questions")),
        _agenda_items_text(agenda_payload.get("items")),
        _agenda_items_text(payload.get("items")),
    ]

    if artifact_type == "live_prep_brief":
        return _join_artifact_fields([
            _payload_text_at(agenda_payload, ("prep_summary",)),
            _payload_text_at(payload, ("notes",)),
            _agenda_items_text(agenda_payload.get("items")),
        ])
    if artifact_type == "live_debrief":
        return _join_artifact_fields([
            _payload_text_at(payload, ("review_summary",)),
            _payload_text_at(payload, ("live_debrief", "review_summary")),
            _payload_text_or_lines(payload.get("what_heard")),
            _payload_text_or_lines(payload.get("what_decided")),
            _payload_text_or_lines(payload.get("still_open")),
            _payload_text_or_lines(payload.get("what_to_remember")),
            _payload_text_or_lines(payload.get("durable_write_summary")),
            _payload_text_or_lines(payload.get("open_questions")),
        ])
    if artifact_type == "review_summary":
        return _join_artifact_fields([
            _payload_text_at(payload, ("review_summary",)),
            _payload_text_at(payload, ("summary",)),
            _payload_text_at(payload, ("live_debrief", "review_summary")),
            _payload_text_at(payload, ("review", "summary")),
        ])
    if artifact_type == "agenda_revision":
        return _join_artifact_fields([
            _payload_text_at(payload, ("prep_summary",)),
            _payload_text_at(agenda_payload, ("prep_summary",)),
            _payload_text_at(payload, ("summary",)),
            _payload_text_at(payload, ("notes",)),
            _agenda_items_text(agenda_payload.get("items")),
            _agenda_items_text(payload.get("items")),
        ])
    if artifact_type == "transcript_reflection":
        return _join_artifact_fields([
            _payload_text_or_lines(payload.get("transcript_reflection")),
            _payload_text_at(payload, ("summary",)),
            _payload_text_at(payload, ("notes",)),
        ])
    return _join_artifact_fields(fallback_values)


def content_hash(text: str) -> str:
    """Return the canonical SHA-256 hash for already-canonical embedding text."""

    return hashlib.sha256(_normalize_text_for_hash(text).encode("utf-8")).hexdigest()


def canonical_content_hash(
    content: str | None = None,
    media_analysis: Mapping[str, Any] | None = None,
) -> str:
    """Return the SHA-256 hash for ``canonical_embedding_text(...)``."""

    return content_hash(canonical_embedding_text(content, media_analysis))


def normalize_vector(vector: Sequence[float], *, dimension: int) -> list[float]:
    """Validate dimension and return an L2-normalized vector."""

    values = [float(value) for value in vector]
    if len(values) != dimension:
        raise ValueError(f"embedding dimension mismatch: expected {dimension}, got {len(values)}")
    if any(not math.isfinite(value) for value in values):
        raise ValueError("embedding vector contains non-finite values")
    norm = math.sqrt(sum(value * value for value in values))
    if norm == 0.0:
        raise ValueError("embedding vector must not be all zeros")
    return [value / norm for value in values]


def validate_vectors(vectors: Sequence[Sequence[float]], *, dimension: int) -> list[list[float]]:
    """Normalize and validate a batch of vectors."""

    return [normalize_vector(vector, dimension=dimension) for vector in vectors]


class OpenAIEmbedder:
    """Hosted async OpenAI embedder for ``text-embedding-3-small``."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model_name: str = DEFAULT_OPENAI_EMBEDDING_MODEL,
        dimension: int = DEFAULT_OPENAI_EMBEDDING_DIMENSION,
        timeout_s: float | None = None,
    ) -> None:
        self.model_name = model_name
        self.dimension = dimension
        self._api_key = api_key
        self._timeout_s = timeout_s
        self._client: Any | None = None

    def _ensure_client(self) -> Any:
        if self._client is None:
            from openai import AsyncOpenAI

            kwargs: dict[str, Any] = {}
            if self._api_key is not None:
                kwargs["api_key"] = self._api_key
            if self._timeout_s is not None:
                kwargs["timeout"] = self._timeout_s
            self._client = AsyncOpenAI(**kwargs)
        return self._client

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        client = self._ensure_client()
        response = await client.embeddings.create(
            model=self.model_name,
            input=list(texts),
            dimensions=self.dimension,
        )
        ordered = sorted(response.data, key=lambda item: item.index)
        vectors = [item.embedding for item in ordered]
        if len(vectors) != len(texts):
            raise EmbeddingError(f"OpenAI returned {len(vectors)} vectors for {len(texts)} inputs")
        return validate_vectors(vectors, dimension=self.dimension)


class DeterministicFakeEmbedder:
    """Deterministic async test embedder with no network or model dependency."""

    model_name = "deterministic-fake"

    def __init__(self, *, dimension: int = 64) -> None:
        if dimension < 1:
            raise ValueError("dimension must be positive")
        self.dimension = dimension

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        canonical = _normalize_text_for_hash(text).casefold()
        tokens = canonical.split() or [canonical]
        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            bucket = int.from_bytes(digest[:8], "big") % self.dimension
            sign = 1.0 if digest[8] % 2 == 0 else -1.0
            vector[bucket] += sign
        return normalize_vector(vector, dimension=self.dimension)


class LocalBgeSmallEmbedder:
    """Lazy local bge-small embedder.

    ``sentence_transformers`` is imported only when this provider is used, so
    normal test and hosted OpenAI paths do not pull the local model dependency.
    """

    def __init__(
        self,
        *,
        model_name: str = "BAAI/bge-small-en-v1.5",
        dimension: int = LOCAL_BGE_SMALL_DIMENSION,
    ) -> None:
        self.model_name = model_name
        self.dimension = dimension
        self._model: Any | None = None

    def _ensure_model(self) -> Any:
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:  # pragma: no cover - depends on optional extra
                raise EmbeddingError(
                    "Local bge-small embeddings require the optional "
                    "`sentence-transformers` dependency"
                ) from exc
            self._model = SentenceTransformer(self.model_name, device="cpu")
            self._model.eval()
        return self._model

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        return await asyncio.to_thread(self._embed_sync, list(texts))

    def _embed_sync(self, texts: list[str]) -> list[list[float]]:
        model = self._ensure_model()
        vectors = model.encode(
            texts,
            batch_size=64,
            convert_to_numpy=False,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return validate_vectors(vectors, dimension=self.dimension)


def embedder_from_settings(settings: Settings | None = None) -> Embedder:
    """Create the configured embedder without touching optional providers early."""

    settings = settings or get_settings()
    provider = settings.embedding_provider
    model = settings.embedding_model
    dimension = settings.embedding_dimension
    if provider == "openai":
        return OpenAIEmbedder(
            api_key=settings.openai_api_key.get_secret_value(),
            model_name=model,
            dimension=dimension,
            timeout_s=settings.query_embed_timeout_s,
        )
    if provider == "local":
        return LocalBgeSmallEmbedder(model_name=model, dimension=dimension)
    raise ValueError(
        "No built-in embedder is registered for "
        f"provider={provider!r}; inject a custom Embedder for this provider"
    )
