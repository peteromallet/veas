"""Tests for eval/retrieval/adapters.py."""

from __future__ import annotations

from datetime import datetime, timezone

from eval.retrieval.adapters import IlikeBaselineRetriever, StubSemanticRetriever
from eval.retrieval.schema import Corpus, CorpusMessage


def _make_test_corpus() -> Corpus:
    """Build a small but realistic corpus for adapter tests."""
    return Corpus(
        messages=[
            CorpusMessage(
                id="m001",
                thread_id="thread_a",
                topic_id="topic_x",
                sender="Alice",
                recipient="Bob",
                sent_at=datetime(2025, 5, 1, 10, 0, 0, tzinfo=timezone.utc),
                content="The server is down in production",
            ),
            CorpusMessage(
                id="m002",
                thread_id="thread_a",
                topic_id="topic_x",
                sender="Bob",
                recipient="Alice",
                sent_at=datetime(2025, 5, 1, 10, 1, 0, tzinfo=timezone.utc),
                content="Looking into it now",
            ),
            CorpusMessage(
                id="m003",
                thread_id="thread_b",
                topic_id="topic_y",
                sender="Alice",
                recipient="Bob",
                sent_at=datetime(2025, 5, 1, 9, 0, 0, tzinfo=timezone.utc),
                content="Did you see the game last night?",
            ),
            CorpusMessage(
                id="m004",
                thread_id="thread_b",
                topic_id="topic_y",
                sender="Bob",
                recipient="Alice",
                sent_at=datetime(2025, 5, 1, 9, 1, 0, tzinfo=timezone.utc),
                content="Yeah what a finish",
            ),
            # Message with media_analysis signal (content is generic).
            CorpusMessage(
                id="m005",
                thread_id="thread_a",
                topic_id="topic_x",
                sender="Alice",
                recipient="Bob",
                sent_at=datetime(2025, 5, 2, 8, 0, 0, tzinfo=timezone.utc),
                content="Check this out",
                media_analysis={
                    "explanation": "The database connection pool is exhausting under load - we're seeing connection timeout errors at 200 concurrent users",
                },
            ),
            # Message with only media_analysis.description containing signal.
            CorpusMessage(
                id="m006",
                thread_id="thread_a",
                topic_id="topic_x",
                sender="Bob",
                recipient="Alice",
                sent_at=datetime(2025, 5, 2, 8, 5, 0, tzinfo=timezone.utc),
                content="Here is the report",
                media_analysis={
                    "description": "CPU utilization spiked to 98% on all four Kubernetes nodes after the load balancer failover",
                },
            ),
            # Message with only media_analysis.summary containing signal.
            CorpusMessage(
                id="m007",
                thread_id="thread_b",
                topic_id="topic_y",
                sender="Alice",
                recipient="Bob",
                sent_at=datetime(2025, 5, 2, 8, 10, 0, tzinfo=timezone.utc),
                content="Notes from the call",
                media_analysis={
                    "summary": "Decision made to push the deployment to next Tuesday instead of Friday due to the QA backlog",
                },
            ),
            # Two messages with same sent_at to test tiebreaker.
            CorpusMessage(
                id="m008",
                thread_id="thread_a",
                topic_id="topic_x",
                sender="Bob",
                recipient="Alice",
                sent_at=datetime(2025, 5, 3, 12, 0, 0, tzinfo=timezone.utc),
                content="first same-time message",
            ),
            CorpusMessage(
                id="m009",
                thread_id="thread_a",
                topic_id="topic_x",
                sender="Alice",
                recipient="Bob",
                sent_at=datetime(2025, 5, 3, 12, 0, 0, tzinfo=timezone.utc),
                content="second same-time message",
            ),
        ]
    )


# ---------------------------------------------------------------------------
# IlikeBaselineRetriever tests
# ---------------------------------------------------------------------------


def test_baseline_finds_verbatim_substring_case_insensitive():
    """Baseline should match case-insensitively on content."""
    corpus = _make_test_corpus()
    retriever = IlikeBaselineRetriever(corpus)

    # Exact case
    results = retriever.retrieve("server is down", scope="all", limit=50)
    assert "m001" in results

    # Different case
    results = retriever.retrieve("SERVER IS DOWN", scope="all", limit=50)
    assert "m001" in results

    # Mixed case
    results = retriever.retrieve("SeRvEr Is DoWn", scope="all", limit=50)
    assert "m001" in results


def test_baseline_respects_thread_scope():
    """Thread scope should filter to matching thread_id only."""
    corpus = _make_test_corpus()
    retriever = IlikeBaselineRetriever(corpus)

    # thread_a has m001 and m002 matching "server"
    results = retriever.retrieve("server", scope="thread", thread_id="thread_a", limit=50)
    assert "m001" in results
    assert all(
        corpus.messages[int(r[1:]) - 1].thread_id == "thread_a" for r in results
    )

    # thread_b should not contain "server"
    results_b = retriever.retrieve("server", scope="thread", thread_id="thread_b", limit=50)
    assert len(results_b) == 0


def test_baseline_respects_topic_scope():
    """Topic scope should filter to matching topic_id only."""
    corpus = _make_test_corpus()
    retriever = IlikeBaselineRetriever(corpus)

    # topic_x has m001, m002 with "server"
    results = retriever.retrieve("server", scope="topic", topic_id="topic_x", limit=50)
    assert len(results) >= 1
    assert "m001" in results

    # topic_y should not contain "server"
    results_b = retriever.retrieve("server", scope="topic", topic_id="topic_y", limit=50)
    assert len(results_b) == 0


def test_baseline_respects_all_scope():
    """All scope should return matches across threads and topics."""
    corpus = _make_test_corpus()
    retriever = IlikeBaselineRetriever(corpus)

    results = retriever.retrieve("the", scope="all", limit=50)
    # "the" appears in m001, m003, m005, m006, m007
    assert len(results) >= 4


def test_baseline_media_analysis_explanation_fallback():
    """Should match on media_analysis.explanation when content is generic."""
    corpus = _make_test_corpus()
    retriever = IlikeBaselineRetriever(corpus)

    # "connection timeout" is only in m005's media_analysis.explanation
    results = retriever.retrieve("connection timeout", scope="all", limit=50)
    assert "m005" in results


def test_baseline_media_analysis_description_fallback():
    """Should match on media_analysis.description when content is generic."""
    corpus = _make_test_corpus()
    retriever = IlikeBaselineRetriever(corpus)

    # "kubernetes" is only in m006's media_analysis.description
    results = retriever.retrieve("kubernetes", scope="all", limit=50)
    assert "m006" in results


def test_baseline_media_analysis_summary_fallback():
    """Should match on media_analysis.summary when content is generic."""
    corpus = _make_test_corpus()
    retriever = IlikeBaselineRetriever(corpus)

    # "deployment to next tuesday" is only in m007's media_analysis.summary
    results = retriever.retrieve("deployment to next tuesday", scope="all", limit=50)
    assert "m007" in results


def test_baseline_paraphrase_only_query_returns_empty():
    """A query that is a paraphrase with no substring overlap returns []."""
    corpus = _make_test_corpus()
    retriever = IlikeBaselineRetriever(corpus)

    # "production outage" is a paraphrase of "server is down in production"
    # but the exact substring "production outage" does not appear anywhere.
    results = retriever.retrieve("production outage", scope="all", limit=50)
    assert results == []


def test_baseline_tiebreaker_ordering_is_stable():
    """Messages with same sent_at should be ordered by id DESC as tiebreaker."""
    corpus = _make_test_corpus()
    retriever = IlikeBaselineRetriever(corpus)

    # m008 and m009 share the same sent_at and both contain "message"
    results = retriever.retrieve("message", scope="all", limit=50)
    m008_idx = results.index("m008") if "m008" in results else -1
    m009_idx = results.index("m009") if "m009" in results else -1
    # m009 should come before m008 (id DESC tiebreaker)
    assert m009_idx < m008_idx


def test_baseline_limit_truncation():
    """Results should be truncated to the specified limit."""
    corpus = _make_test_corpus()
    retriever = IlikeBaselineRetriever(corpus)

    # Query matching many messages
    results = retriever.retrieve("the", scope="all", limit=2)
    assert len(results) <= 2


# ---------------------------------------------------------------------------
# StubSemanticRetriever tests
# ---------------------------------------------------------------------------


def test_stub_returns_empty_list():
    """Stub retriever should always return [] deterministically."""
    corpus = _make_test_corpus()
    retriever = StubSemanticRetriever(corpus)

    assert retriever.retrieve("anything", scope="all", limit=50) == []
    assert retriever.retrieve("server", scope="thread", thread_id="thread_a", limit=10) == []
    assert retriever.retrieve("", scope="topic", topic_id="topic_x", limit=1) == []
    # Multiple calls return same result
    assert retriever.retrieve("test", scope="all", limit=50) == []
    assert retriever.retrieve("test", scope="all", limit=50) == []


def test_stub_empty_corpus():
    """Stub works even with empty corpus."""
    corpus = Corpus(messages=[])
    retriever = StubSemanticRetriever(corpus)
    assert retriever.retrieve("test", scope="all", limit=50) == []


# ---------------------------------------------------------------------------
# No app.* imports check
# ---------------------------------------------------------------------------


def test_adapters_module_has_no_app_imports():
    """Verify adapters.py does not import anything from app.*"""
    import ast
    from pathlib import Path

    adapters_path = Path(__file__).parent.parent / "eval" / "retrieval" / "adapters.py"
    source = adapters_path.read_text()
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith(
                    "app."
                ), f"adapters.py imports app.*: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                assert not node.module.startswith(
                    "app."
                ), f"adapters.py imports from app.*: {node.module}"
