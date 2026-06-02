"""End-to-end tests for the SemanticRetriever and HybridRetriever adapters.

These tests use a tiny deterministic FakeEmbedder (a hashed bag-of-words
projection) so they run with NO network and NO model download. They verify:

  1. SemanticRetriever and HybridRetriever satisfy the Retriever protocol and
     run end-to-end through the harness runner (run_eval) producing valid
     EvalReport metrics.
  2. Scope filtering (thread / topic / all) is respected, matching the
     baseline's scope semantics.
  3. RRF hybrid fusion surfaces both keyword and semantic hits.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import numpy as np

from eval.retrieval.adapters import (
    HybridRetriever,
    IlikeBaselineRetriever,
    SemanticRetriever,
)
from eval.retrieval.runner import run_eval
from eval.retrieval.schema import Corpus, CorpusMessage, GoldenCase, GoldenSet


class FakeEmbedder:
    """Deterministic hashed bag-of-words embedder. No network, no model.

    Each token is hashed into one of `dim` buckets; the vector counts token
    bucket hits. Texts sharing words land near each other in cosine space,
    which is enough to exercise the ranking + fusion logic deterministically.
    """

    name = "fake-hashed-bow"
    is_real_embedding = False  # not a real embedding; test-only

    def __init__(self, dim: int = 64) -> None:
        self._dim = dim

    def _embed(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self._dim), dtype=np.float32)
        for i, t in enumerate(texts):
            for tok in t.lower().split():
                h = int(hashlib.sha256(tok.encode()).hexdigest(), 16)
                out[i, h % self._dim] += 1.0
        # L2-normalize so dot product == cosine, matching MiniLMEmbedder's
        # contract (SemanticRetriever scores via plain dot product).
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return out / norms

    # SemanticRetriever's embedder interface: embed_corpus + embed_query.
    def embed_corpus(self, texts: list[str]) -> np.ndarray:
        return self._embed(texts)

    def embed_query(self, text: str) -> np.ndarray:
        return self._embed([text])[0]


def _corpus() -> Corpus:
    return Corpus(
        messages=[
            CorpusMessage(
                id="m001", thread_id="t_a", topic_id="top_x",
                sender="Alice", recipient="Bob",
                sent_at=datetime(2025, 5, 1, 10, 0, tzinfo=timezone.utc),
                content="the production server crashed during deploy",
            ),
            CorpusMessage(
                id="m002", thread_id="t_a", topic_id="top_x",
                sender="Bob", recipient="Alice",
                sent_at=datetime(2025, 5, 1, 10, 1, tzinfo=timezone.utc),
                content="rolling back the release now",
            ),
            CorpusMessage(
                id="m003", thread_id="t_b", topic_id="top_x",
                sender="Alice", recipient="Bob",
                sent_at=datetime(2025, 5, 1, 11, 0, tzinfo=timezone.utc),
                content="server crashed again on the other host",
            ),
            CorpusMessage(
                id="m004", thread_id="t_b", topic_id="top_y",
                sender="Bob", recipient="Alice",
                sent_at=datetime(2025, 5, 1, 12, 0, tzinfo=timezone.utc),
                content="lunch plans for saturday",
            ),
        ]
    )


def _golden() -> GoldenSet:
    return GoldenSet(
        cases=[
            GoldenCase(
                id="GC1", query="server crashed", expected_message_ids=["m001", "m003"],
                scope="all", query_type="verbatim_quote",
            ),
            GoldenCase(
                id="GC2", query="server crashed", expected_message_ids=["m001"],
                scope="thread", query_type="verbatim_quote", thread_id="t_a",
            ),
            GoldenCase(
                id="GC3", query="server crashed", expected_message_ids=["m001", "m003"],
                scope="topic", query_type="cross_thread", topic_id="top_x",
            ),
        ]
    )


def _make_semantic(corpus: Corpus) -> SemanticRetriever:
    return SemanticRetriever(corpus, embedder=FakeEmbedder())


def _make_hybrid(corpus: Corpus) -> HybridRetriever:
    sem = _make_semantic(corpus)
    return HybridRetriever(corpus, semantic=sem)


def _source_ids(ranked):
    """Extract source_id strings from a list of RankedSourceKey objects."""
    return [r.source_id for r in ranked]


def test_semantic_runs_end_to_end_through_harness():
    corpus = _corpus()
    report = run_eval(_make_semantic(corpus), corpus, _golden())
    assert report.overall["n"] == 3
    # Semantic should find both server-crash messages in the 'all' case.
    gc1 = next(c for c in report.per_case if c["case_id"] == "GC1")
    assert "m001" in gc1["ranked_ids"]
    assert "m003" in gc1["ranked_ids"]
    assert gc1["recall_at_10"] == 1.0


def test_hybrid_runs_end_to_end_through_harness():
    corpus = _corpus()
    report = run_eval(_make_hybrid(corpus), corpus, _golden())
    assert report.overall["n"] == 3
    gc1 = next(c for c in report.per_case if c["case_id"] == "GC1")
    assert gc1["recall_at_10"] == 1.0
    assert report.overall["recall@10"] > 0.0


def test_semantic_respects_thread_scope():
    corpus = _corpus()
    sem = _make_semantic(corpus)
    ids = _source_ids(sem.retrieve("server crashed", "thread", thread_id="t_a", limit=10))
    # m003 lives in t_b and must be excluded under thread scope.
    assert "m003" not in ids
    assert "m001" in ids


def test_semantic_respects_topic_scope_cross_thread():
    corpus = _corpus()
    sem = _make_semantic(corpus)
    ids = _source_ids(sem.retrieve("server crashed", "topic", topic_id="top_x", limit=10))
    # Both threads share top_x: cross-thread recall must include both.
    assert "m001" in ids
    assert "m003" in ids
    assert "m004" not in ids  # top_y, different topic


def test_hybrid_fuses_keyword_and_semantic():
    corpus = _corpus()
    baseline = IlikeBaselineRetriever(corpus)
    hybrid = _make_hybrid(corpus)
    # Keyword baseline finds exact substring "server crashed" (m001, m003).
    kw = _source_ids(baseline.retrieve("server crashed", "all", limit=10))
    assert set(kw) == {"m001", "m003"}
    fused = _source_ids(hybrid.retrieve("server crashed", "all", limit=10))
    # RRF must surface the keyword hits at the top.
    assert set(fused[:2]) == {"m001", "m003"}


def test_semantic_retrieve_truncates_to_limit():
    corpus = _corpus()
    sem = _make_semantic(corpus)
    ids = sem.retrieve("server", "all", limit=2)
    assert len(ids) == 2
