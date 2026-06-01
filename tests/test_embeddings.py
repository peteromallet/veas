from __future__ import annotations

import builtins
import math
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.services.embeddings import (
    DeterministicFakeEmbedder,
    EmbeddingError,
    LocalBgeSmallEmbedder,
    OpenAIEmbedder,
    canonical_artifact_embedding_text,
    canonical_content_hash,
    canonical_distillation_embedding_text,
    canonical_embedding_text,
    canonical_memory_embedding_text,
    canonical_observation_embedding_text,
    canonical_raw_content_text,
    content_hash,
    embedder_from_settings,
    validate_vectors,
    normalize_vector,
)


def test_canonical_embedding_text_matches_migration_field_order() -> None:
    text = canonical_embedding_text(
        "body",
        {
            "summary": "summary",
            "description": "description",
            "explanation": "explanation",
            "ignored": "ignored",
        },
    )

    assert text == "body\nexplanation\ndescription\nsummary"


def test_canonical_embedding_text_coalesces_missing_fields() -> None:
    assert canonical_embedding_text(None, None) == "\n\n\n"
    assert canonical_embedding_text("body", {"description": None}) == "body\n\n\n"


def test_canonical_embedding_text_matches_migration_sql_expression() -> None:
    sql = Path("migrations/0056_retrieval_index.sql").read_text(encoding="utf-8")

    expected = (
        "COALESCE(content, '') || E'\\n' ||\n"
        "            COALESCE(media_analysis->>'explanation', '') || E'\\n' ||\n"
        "            COALESCE(media_analysis->>'description', '') || E'\\n' ||\n"
        "            COALESCE(media_analysis->>'summary', '')"
    )

    assert expected in sql
    assert canonical_embedding_text(
        "body",
        {"explanation": "explanation", "description": "description", "summary": "summary"},
    ) == "body\nexplanation\ndescription\nsummary"


def test_content_hash_normalizes_line_endings_and_unicode() -> None:
    decomposed = "Cafe\u0301\r\nbody"
    composed = "Caf\u00e9\nbody"

    assert content_hash(decomposed) == content_hash(composed)
    assert canonical_content_hash("Caf\u00e9\r\nbody", {}) == content_hash(composed + "\n\n\n")


def test_content_hash_normalization_is_stable_for_whitespace_and_stringlike_fields() -> None:
    media_a = {"summary": 42, "description": "Line\r\nTwo", "explanation": None}
    media_b = {"description": "Line\nTwo", "summary": "42"}

    assert canonical_embedding_text("body", {"summary": 42}) == canonical_embedding_text(
        "body", {"summary": "42"}
    )
    assert canonical_content_hash("body", media_a) == canonical_content_hash("body", media_b)


def test_non_message_raw_content_builders_return_content_and_empty_text_for_missing_values() -> None:
    assert canonical_raw_content_text("memory text") == "memory text"
    assert canonical_raw_content_text(42) == "42"
    assert canonical_raw_content_text(None) == ""
    assert canonical_memory_embedding_text("memory text") == "memory text"
    assert canonical_observation_embedding_text("observation text") == "observation text"
    assert canonical_distillation_embedding_text("private synthesis") == "private synthesis"
    assert canonical_memory_embedding_text(None) == ""
    assert canonical_observation_embedding_text(None) == ""
    assert canonical_distillation_embedding_text(None) == ""


def test_message_canonical_text_behavior_is_preserved_with_non_message_builders() -> None:
    media = {"explanation": "why", "description": "what", "summary": "short"}

    assert canonical_embedding_text("body", media) == "body\nwhy\nwhat\nshort"
    assert canonical_memory_embedding_text("body") != canonical_embedding_text("body", media)


def test_artifact_canonical_text_matches_representative_sql_view_fixtures() -> None:
    fixtures = [
        (
            "live_prep_brief",
            {
                "agenda": {
                    "prep_summary": "Focus on repair.",
                    "items": [
                        {
                            "id": "skip-structural-id",
                            "title": "Name the tension",
                            "intent": "Surface what happened",
                            "ask": "What felt unresolved?",
                            "done_when": "Both people answer",
                            "priority": "skip-structural-priority",
                        }
                    ],
                },
                "notes": "Use the calmer first item.",
                "turn_id": "skip-structural-turn",
            },
            "Focus on repair.\nUse the calmer first item.\n"
            "Name the tension\nSurface what happened\nWhat felt unresolved?\nBoth people answer",
        ),
        (
            "live_debrief",
            {
                "review_summary": "They found a next step.",
                "what_heard": ["Timing is hard", "Both want clarity"],
                "what_decided": "Try a Sunday check-in",
                "still_open": "",
                "what_to_remember": "Avoid late-night planning",
                "durable_write_summary": "Created one memory",
                "open_questions": "Who owns the calendar invite?",
                "references": [{"transcript_turn_id": "skip-reference-id"}],
            },
            "They found a next step.\nTiming is hard\nBoth want clarity\n"
            "Try a Sunday check-in\n\nAvoid late-night planning\n"
            "Created one memory\nWho owns the calendar invite?",
        ),
        (
            "review_summary",
            {
                "review_summary": "Short review.",
                "conversation_id": "skip-conversation-id",
                "source_artifact_id": "skip-source-id",
            },
            "Short review.",
        ),
        (
            "agenda_revision",
            {
                "summary": "Revision after user edit.",
                "notes": "Move logistics later.",
                "items": [
                    {
                        "id": "skip-root-item-id",
                        "title": "Start with feelings",
                        "intent": "Lower defensiveness",
                    }
                ],
            },
            "Revision after user edit.\nMove logistics later.\nStart with feelings\nLower defensiveness",
        ),
        (
            "transcript_reflection",
            {
                "transcript_reflection": ["Partner sounded tired", "Primary softened"],
                "summary": "Tone shifted.",
                "transcript_turn_id": "skip-turn-id",
            },
            "Partner sounded tired\nPrimary softened\nTone shifted.",
        ),
        (
            "unknown_future_artifact",
            {
                "title": "Fallback title",
                "summary": "Fallback summary",
                "what_heard": ["One", "Two"],
                "id": "skip-structural-id",
            },
            "Fallback summary\nFallback title\nOne\nTwo",
        ),
    ]

    for artifact_type, payload, expected in fixtures:
        assert canonical_artifact_embedding_text(artifact_type, payload) == expected


def test_artifact_python_builder_stays_in_parity_with_sql_contract() -> None:
    sql = Path("migrations/0058_content_embeddings_unified_index.sql").read_text(encoding="utf-8")

    for artifact_type in (
        "live_prep_brief",
        "live_debrief",
        "review_summary",
        "agenda_revision",
        "transcript_reflection",
    ):
        assert f"WHEN '{artifact_type}'" in sql

    for structural_field in ("turn_id", "id", "references", "created_by_turn_id"):
        assert f"ca.payload->>'{structural_field}'" not in sql

    assert "ELSE btrim(concat_ws" in sql
    assert "jsonb_typeof(ca.payload->'what_heard') = 'array'" in sql
    assert "value->>'title'" in sql
    assert "value->>'intent'" in sql
    assert "value->>'ask'" in sql
    assert "value->>'done_when'" in sql


def test_normalize_vector_validates_dimension_finiteness_and_zero_norm() -> None:
    vector = normalize_vector([3, 4], dimension=2)

    assert vector == [0.6, 0.8]
    with pytest.raises(ValueError, match="expected 3, got 2"):
        normalize_vector([1, 2], dimension=3)
    with pytest.raises(ValueError, match="non-finite"):
        normalize_vector([1, math.inf], dimension=2)
    with pytest.raises(ValueError, match="all zeros"):
        normalize_vector([0, 0], dimension=2)


def test_validate_vectors_normalizes_a_batch_and_surfaces_dimension_errors() -> None:
    vectors = validate_vectors([[3, 4], [8, 15]], dimension=2)

    assert vectors == [[0.6, 0.8], [8 / 17, 15 / 17]]
    with pytest.raises(ValueError, match="expected 2, got 1"):
        validate_vectors([[1]], dimension=2)


async def test_deterministic_fake_embedder_is_async_deterministic_and_normalized() -> None:
    embedder = DeterministicFakeEmbedder(dimension=16)

    first = await embedder.embed_texts(["deploy failed", "deploy failed", "other text"])
    second = await embedder.embed_texts(["deploy failed", "deploy failed", "other text"])

    assert first == second
    assert first[0] == first[1]
    assert first[0] != first[2]
    assert all(len(vector) == 16 for vector in first)
    assert all(math.isclose(math.sqrt(sum(x * x for x in vector)), 1.0) for vector in first)


async def test_openai_embedder_batches_preserves_input_order_and_normalizes(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeEmbeddingsClient:
        async def create(self, *, model, input, dimensions):
            captured["model"] = model
            captured["input"] = list(input)
            captured["dimensions"] = dimensions
            return SimpleNamespace(
                data=[
                    SimpleNamespace(index=1, embedding=[0.0, 5.0]),
                    SimpleNamespace(index=0, embedding=[3.0, 4.0]),
                ]
            )

    embedder = OpenAIEmbedder(api_key="test", model_name="text-embedding-3-small", dimension=2)
    monkeypatch.setattr(embedder, "_ensure_client", lambda: SimpleNamespace(embeddings=FakeEmbeddingsClient()))

    vectors = await embedder.embed_texts(["first", "second"])

    assert captured == {
        "model": "text-embedding-3-small",
        "input": ["first", "second"],
        "dimensions": 2,
    }
    assert vectors == [[0.6, 0.8], [0.0, 1.0]]


async def test_openai_embedder_rejects_wrong_vector_count_and_dimension(monkeypatch) -> None:
    class WrongCountClient:
        async def create(self, *, model, input, dimensions):
            return SimpleNamespace(data=[SimpleNamespace(index=0, embedding=[1.0, 0.0])])

    count_embedder = OpenAIEmbedder(api_key="test", dimension=2)
    monkeypatch.setattr(count_embedder, "_ensure_client", lambda: SimpleNamespace(embeddings=WrongCountClient()))

    with pytest.raises(EmbeddingError, match="returned 1 vectors for 2 inputs"):
        await count_embedder.embed_texts(["a", "b"])

    class WrongDimensionClient:
        async def create(self, *, model, input, dimensions):
            return SimpleNamespace(data=[SimpleNamespace(index=0, embedding=[1.0])])

    dimension_embedder = OpenAIEmbedder(api_key="test", dimension=2)
    monkeypatch.setattr(
        dimension_embedder,
        "_ensure_client",
        lambda: SimpleNamespace(embeddings=WrongDimensionClient()),
    )

    with pytest.raises(ValueError, match="expected 2, got 1"):
        await dimension_embedder.embed_texts(["a"])


def test_embedder_factory_uses_openai_without_importing_local_dependency(app_env, monkeypatch) -> None:
    monkeypatch.setenv("EMBEDDING_PROVIDER", "openai")
    monkeypatch.setenv("EMBEDDING_MODEL", "text-embedding-3-small")
    monkeypatch.setenv("EMBEDDING_DIMENSION", "1536")

    settings = Settings()
    embedder = embedder_from_settings(settings)

    assert isinstance(embedder, OpenAIEmbedder)
    assert embedder.model_name == "text-embedding-3-small"
    assert embedder.dimension == 1536
    assert "sentence_transformers" not in sys.modules


def test_embedder_factory_local_is_lazy(app_env, monkeypatch) -> None:
    monkeypatch.setenv("EMBEDDING_PROVIDER", "local")
    monkeypatch.setenv("EMBEDDING_MODEL", "bge-small")
    monkeypatch.setenv("EMBEDDING_DIMENSION", "384")

    settings = Settings()
    embedder = embedder_from_settings(settings)

    assert isinstance(embedder, LocalBgeSmallEmbedder)
    assert embedder.model_name == "bge-small"
    assert embedder.dimension == 384
    assert "sentence_transformers" not in sys.modules


def test_local_embedder_import_failure_is_lazy_until_first_call(monkeypatch) -> None:
    original_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "sentence_transformers":
            raise ImportError("missing optional dependency")
        return original_import(name, globals, locals, fromlist, level)

    embedder = LocalBgeSmallEmbedder(model_name="bge-small", dimension=384)

    assert embedder._model is None
    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(EmbeddingError, match="optional `sentence-transformers` dependency"):
        embedder._ensure_model()


def test_embedder_factory_rejects_unregistered_custom_provider(app_env, monkeypatch) -> None:
    monkeypatch.setenv("EMBEDDING_PROVIDER", "registered-test")
    monkeypatch.setenv("EMBEDDING_MODEL", "custom-v1")
    monkeypatch.setenv("EMBEDDING_DIMENSION", "8")

    settings = Settings()
    with pytest.raises(ValueError, match="inject a custom Embedder"):
        embedder_from_settings(settings)


def test_openai_embedder_client_is_lazy() -> None:
    embedder = OpenAIEmbedder(api_key="test", dimension=1536)

    assert embedder._client is None


def test_protocol_shape_is_simple_to_fake() -> None:
    class TinyFake:
        model_name = "tiny"
        dimension = 2

        async def embed_texts(self, texts):
            return [[1.0, 0.0] for _ in texts]

    assert TinyFake.model_name == "tiny"
    assert TinyFake.dimension == 2
