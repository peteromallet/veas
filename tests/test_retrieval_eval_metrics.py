"""Tests for eval/retrieval/metrics.py and loader validation."""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from eval.retrieval.loader import load_corpus, load_golden_set
from eval.retrieval.metrics import (
    aggregate,
    aggregate_by_group,
    aggregate_by_query_type,
    aggregate_set_metrics,
    contiguous_boundary_ok,
    exact_ordered_match,
    recall_at_k,
    reciprocal_rank,
    set_precision,
    set_recall,
)
from eval.retrieval.schema import Corpus, CorpusMessage


# ---------------------------------------------------------------------------
# Metrics unit tests
# ---------------------------------------------------------------------------


def test_recall_at_k_perfect_hit_at_rank_1():
    """Perfect hit at rank 1 should give recall@1=1.0."""
    ranked = ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j"]
    expected = ["a"]
    assert recall_at_k(ranked, expected, k=1) == 1.0
    assert recall_at_k(ranked, expected, k=5) == 1.0
    assert recall_at_k(ranked, expected, k=10) == 1.0


def test_recall_at_k_hit_at_rank_7():
    """Hit at rank 7: recall@5=0, recall@10=1, RR=1/7."""
    ranked = ["x", "y", "z", "w", "v", "u", "target", "t", "s", "r"]
    expected = ["target"]

    assert recall_at_k(ranked, expected, k=1) == 0.0
    assert recall_at_k(ranked, expected, k=5) == 0.0
    assert recall_at_k(ranked, expected, k=10) == 1.0

    rr = reciprocal_rank(ranked, expected)
    assert rr == 1.0 / 7.0


def test_multi_expected_partial_recall():
    """Multiple expected ids, some found, some not."""
    ranked = ["a", "b", "c", "d", "e"]
    expected = ["a", "z", "c", "w"]  # a and c found, z and w not

    assert recall_at_k(ranked, expected, k=3) == 2.0 / 4.0  # a and c in top 3
    assert recall_at_k(ranked, expected, k=5) == 2.0 / 4.0  # still only a, c
    assert recall_at_k(ranked, expected, k=1) == 1.0 / 4.0  # only a

    # First expected hit is "a" at rank 1
    assert reciprocal_rank(ranked, expected) == 1.0


def test_empty_ranking_yields_zeros():
    """Empty ranking should produce all-zero metrics."""
    ranked: list[str] = []
    expected = ["a", "b"]

    assert recall_at_k(ranked, expected, k=5) == 0.0
    assert reciprocal_rank(ranked, expected) == 0.0


def test_recall_empty_expected():
    """Empty expected list returns 0.0 to avoid division by zero."""
    ranked = ["a", "b", "c"]
    expected: list[str] = []
    assert recall_at_k(ranked, expected, k=5) == 0.0
    assert reciprocal_rank(ranked, expected) == 0.0


def test_aggregate_empty():
    """Empty results list returns zeroed aggregate."""
    result = aggregate([])
    assert result == {"recall@1": 0.0, "recall@5": 0.0, "recall@10": 0.0, "mrr": 0.0, "n": 0}


def test_aggregate_basic():
    """Aggregate two simple cases."""
    results = [
        {"recall_at_1": 1.0, "recall_at_5": 1.0, "recall_at_10": 1.0, "reciprocal_rank": 1.0},
        {"recall_at_1": 0.0, "recall_at_5": 0.0, "recall_at_10": 1.0, "reciprocal_rank": 1.0 / 7.0},
    ]
    agg = aggregate(results)
    assert agg["n"] == 2
    assert agg["recall@1"] == 0.5
    assert agg["recall@5"] == 0.5
    assert agg["recall@10"] == 1.0
    assert agg["mrr"] == (1.0 + 1.0 / 7.0) / 2.0


def test_aggregate_by_query_type_grouping():
    """aggregate_by_query_type should group results by query_type correctly."""
    results = [
        {
            "recall_at_1": 1.0,
            "recall_at_5": 1.0,
            "recall_at_10": 1.0,
            "reciprocal_rank": 1.0,
            "query_type": "verbatim_quote",
        },
        {
            "recall_at_1": 0.0,
            "recall_at_5": 0.0,
            "recall_at_10": 0.0,
            "reciprocal_rank": 0.0,
            "query_type": "paraphrase",
        },
        {
            "recall_at_1": 0.0,
            "recall_at_5": 1.0,
            "recall_at_10": 1.0,
            "reciprocal_rank": 1.0 / 3.0,
            "query_type": "verbatim_quote",
        },
    ]
    grouped = aggregate_by_query_type(results)
    assert set(grouped.keys()) == {"verbatim_quote", "paraphrase"}

    vq = grouped["verbatim_quote"]
    assert vq["n"] == 2
    assert vq["recall@1"] == 0.5
    assert vq["recall@5"] == 1.0
    assert vq["recall@10"] == 1.0
    assert vq["mrr"] == (1.0 + 1.0 / 3.0) / 2.0

    pq = grouped["paraphrase"]
    assert pq["n"] == 1
    assert pq["recall@1"] == 0.0
    assert pq["mrr"] == 0.0


# ---------------------------------------------------------------------------
# New metrics unit tests (T4)
# ---------------------------------------------------------------------------


def test_exact_ordered_match_identity():
    """Identical lists should match."""
    assert exact_ordered_match(["a", "b", "c"], ["a", "b", "c"]) is True


def test_exact_ordered_match_different_order():
    """Same elements, different order → False."""
    assert exact_ordered_match(["a", "b", "c"], ["c", "b", "a"]) is False


def test_exact_ordered_match_different_elements():
    """Different elements → False."""
    assert exact_ordered_match(["a", "b"], ["a", "c"]) is False


def test_exact_ordered_match_different_lengths():
    """Different lengths → False."""
    assert exact_ordered_match(["a", "b"], ["a", "b", "c"]) is False


def test_exact_ordered_match_empty():
    """Two empty lists are strictly equal."""
    assert exact_ordered_match([], []) is True


def test_exact_ordered_match_empty_vs_nonempty():
    """Empty vs non-empty → False."""
    assert exact_ordered_match([], ["a"]) is False
    assert exact_ordered_match(["a"], []) is False


def test_contiguous_boundary_ok_exact_match_in_corpus():
    """Returned is contiguous in corpus_order with matching boundaries."""
    corpus_order = ["a", "b", "c", "d", "e"]
    returned = ["b", "c", "d"]
    expected = ["b", "X", "d"]  # interior ignored, only boundaries checked
    assert contiguous_boundary_ok(returned, expected, corpus_order) is True


def test_contiguous_boundary_ok_not_contiguous():
    """Returned is not a contiguous slice of corpus_order → False."""
    corpus_order = ["a", "b", "c", "d", "e"]
    returned = ["b", "d"]  # skipped "c"
    expected = ["b", "d"]
    assert contiguous_boundary_ok(returned, expected, corpus_order) is False


def test_contiguous_boundary_ok_wrong_first():
    """First element mismatch → False."""
    corpus_order = ["a", "b", "c"]
    returned = ["b", "c"]
    expected = ["a", "c"]
    assert contiguous_boundary_ok(returned, expected, corpus_order) is False


def test_contiguous_boundary_ok_wrong_last():
    """Last element mismatch → False."""
    corpus_order = ["a", "b", "c"]
    returned = ["a", "b"]
    expected = ["a", "c"]
    assert contiguous_boundary_ok(returned, expected, corpus_order) is False


def test_contiguous_boundary_ok_empty():
    """Empty returned or expected → False."""
    corpus_order = ["a", "b"]
    assert contiguous_boundary_ok([], ["a"], corpus_order) is False
    assert contiguous_boundary_ok(["a"], [], corpus_order) is False
    assert contiguous_boundary_ok([], [], corpus_order) is False


def test_contiguous_boundary_ok_single_element():
    """Single-element lists: returned==expected and contiguous in corpus."""
    corpus_order = ["x", "a", "y"]
    returned = ["a"]
    expected = ["a"]
    assert contiguous_boundary_ok(returned, expected, corpus_order) is True


def test_set_precision_perfect():
    """All returned items are expected → 1.0."""
    assert set_precision(["a", "b"], ["a", "b", "c"]) == 1.0


def test_set_precision_partial():
    """Some returned items are expected → fractional."""
    assert set_precision(["a", "x"], ["a", "b", "c"]) == 0.5


def test_set_precision_empty_returned():
    """Empty returned → 1.0 by convention."""
    assert set_precision([], ["a", "b"]) == 1.0


def test_set_precision_empty_expected():
    """No overlap possible if expected is empty."""
    assert set_precision(["a"], []) == 0.0


def test_set_precision_no_overlap():
    """Complete mismatch → 0.0."""
    assert set_precision(["x", "y"], ["a", "b"]) == 0.0


def test_set_recall_perfect():
    """All expected items are returned → 1.0."""
    assert set_recall(["a", "b", "c"], ["a", "c"]) == 1.0


def test_set_recall_partial():
    """Some expected items are returned → fractional."""
    assert set_recall(["a", "x"], ["a", "b", "c"]) == 1.0 / 3.0


def test_set_recall_empty_returned():
    """No returned items → 0.0."""
    assert set_recall([], ["a", "b"]) == 0.0


def test_set_recall_empty_expected():
    """Empty expected → 0.0 by convention."""
    assert set_recall(["a", "b"], []) == 0.0


def test_set_recall_no_overlap():
    """Complete mismatch → 0.0."""
    assert set_recall(["x", "y"], ["a", "b"]) == 0.0


def test_aggregate_set_metrics_empty():
    """Empty per_case returns zeros."""
    result = aggregate_set_metrics([])
    assert result == {"set_precision": 0.0, "set_recall": 0.0, "f1": 0.0, "n": 0}


def test_aggregate_set_metrics_basic():
    """Two cases, macro-averaged."""
    per_case = [
        {"set_precision": 1.0, "set_recall": 0.5},
        {"set_precision": 0.5, "set_recall": 1.0},
    ]
    result = aggregate_set_metrics(per_case)
    assert result["n"] == 2
    assert result["set_precision"] == 0.75  # (1.0 + 0.5) / 2
    assert result["set_recall"] == 0.75  # (0.5 + 1.0) / 2
    # f1 = 2 * 0.75 * 0.75 / (0.75 + 0.75) = 0.75
    assert result["f1"] == 0.75


def test_aggregate_set_metrics_all_zero():
    """All zeros → f1 is 0.0."""
    per_case = [
        {"set_precision": 0.0, "set_recall": 0.0},
        {"set_precision": 0.0, "set_recall": 0.0},
    ]
    result = aggregate_set_metrics(per_case)
    assert result["set_precision"] == 0.0
    assert result["set_recall"] == 0.0
    assert result["f1"] == 0.0
    assert result["n"] == 2


def test_aggregate_by_group_generic():
    """aggregate_by_group works with arbitrary group keys."""
    results = [
        {
            "recall_at_1": 1.0,
            "recall_at_5": 1.0,
            "recall_at_10": 1.0,
            "reciprocal_rank": 1.0,
            "difficulty": "easy",
        },
        {
            "recall_at_1": 0.0,
            "recall_at_5": 0.0,
            "recall_at_10": 0.0,
            "reciprocal_rank": 0.0,
            "difficulty": "hard",
        },
        {
            "recall_at_1": 0.0,
            "recall_at_5": 1.0,
            "recall_at_10": 1.0,
            "reciprocal_rank": 1.0 / 3.0,
            "difficulty": "easy",
        },
    ]
    grouped = aggregate_by_group(results, "difficulty")
    assert set(grouped.keys()) == {"easy", "hard"}
    assert grouped["easy"]["n"] == 2
    assert grouped["easy"]["recall@1"] == 0.5
    assert grouped["hard"]["n"] == 1
    assert grouped["hard"]["recall@1"] == 0.0


def test_aggregate_by_group_empty():
    """Empty results → empty dict."""
    assert aggregate_by_group([], "any_key") == {}


def test_aggregate_by_query_type_delegates_to_aggregate_by_group():
    """aggregate_by_query_type still works and produces same output shape."""
    results = [
        {
            "recall_at_1": 1.0,
            "recall_at_5": 1.0,
            "recall_at_10": 1.0,
            "reciprocal_rank": 1.0,
            "query_type": "verbatim_quote",
        },
        {
            "recall_at_1": 0.0,
            "recall_at_5": 0.0,
            "recall_at_10": 0.0,
            "reciprocal_rank": 0.0,
            "query_type": "paraphrase",
        },
        {
            "recall_at_1": 0.0,
            "recall_at_5": 1.0,
            "recall_at_10": 1.0,
            "reciprocal_rank": 1.0 / 3.0,
            "query_type": "verbatim_quote",
        },
    ]
    grouped = aggregate_by_query_type(results)
    assert set(grouped.keys()) == {"verbatim_quote", "paraphrase"}
    vq = grouped["verbatim_quote"]
    assert vq["n"] == 2
    assert vq["recall@1"] == 0.5
    assert vq["recall@5"] == 1.0
    assert vq["recall@10"] == 1.0
    assert vq["mrr"] == (1.0 + 1.0 / 3.0) / 2.0
    pq = grouped["paraphrase"]
    assert pq["n"] == 1
    assert pq["recall@1"] == 0.0
    assert pq["mrr"] == 0.0


# ---------------------------------------------------------------------------
# Loader validation unit tests
# ---------------------------------------------------------------------------

def _make_corpus() -> Corpus:
    """Build a minimal corpus for golden-set validation tests."""
    return Corpus(
        messages=[
            CorpusMessage(
                id="m001",
                thread_id="t1",
                topic_id="top1",
                sender="Alice",
                recipient="Bob",
                sent_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
                content="hello",
            ),
            CorpusMessage(
                id="m002",
                thread_id="t1",
                topic_id="top1",
                sender="Bob",
                recipient="Alice",
                sent_at=datetime(2025, 1, 1, 0, 1, tzinfo=timezone.utc),
                content="hi",
            ),
            CorpusMessage(
                id="m003",
                thread_id="t2",
                topic_id="top2",
                sender="Alice",
                recipient="Bob",
                sent_at=datetime(2025, 1, 2, tzinfo=timezone.utc),
                content="test",
            ),
        ]
    )


def test_loader_dangling_ref_raises():
    """GoldenCase referencing a non-existent corpus id raises ValueError."""
    corpus = _make_corpus()
    golden_data = {
        "cases": [
            {
                "id": "g1",
                "query": "test",
                "expected_message_ids": ["m001", "m999"],  # m999 not in corpus
                "scope": "all",
                "query_type": "verbatim_quote",
            }
        ]
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(golden_data, f)
        tmp_path = Path(f.name)

    try:
        with pytest.raises(ValueError, match="m999"):
            load_golden_set(tmp_path, corpus=corpus)
    finally:
        tmp_path.unlink()


def test_loader_empty_expected_raises():
    """GoldenCase with empty expected_message_ids raises ValueError."""
    corpus = _make_corpus()
    golden_data = {
        "cases": [
            {
                "id": "g1",
                "query": "test",
                "expected_message_ids": [],
                "scope": "all",
                "query_type": "verbatim_quote",
            }
        ]
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(golden_data, f)
        tmp_path = Path(f.name)

    try:
        with pytest.raises(ValueError, match="empty expected_message_ids"):
            load_golden_set(tmp_path, corpus=corpus)
    finally:
        tmp_path.unlink()


def test_loader_thread_scope_without_thread_id_raises():
    """GoldenCase with scope='thread' but thread_id=None raises ValueError."""
    corpus = _make_corpus()
    golden_data = {
        "cases": [
            {
                "id": "g1",
                "query": "test",
                "expected_message_ids": ["m001"],
                "scope": "thread",
                "query_type": "verbatim_quote",
                "thread_id": None,
            }
        ]
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(golden_data, f)
        tmp_path = Path(f.name)

    try:
        with pytest.raises(ValueError, match="scope='thread' but thread_id is None"):
            load_golden_set(tmp_path, corpus=corpus)
    finally:
        tmp_path.unlink()


def test_loader_topic_scope_without_topic_id_raises():
    """GoldenCase with scope='topic' but topic_id=None raises ValueError."""
    corpus = _make_corpus()
    golden_data = {
        "cases": [
            {
                "id": "g1",
                "query": "test",
                "expected_message_ids": ["m001"],
                "scope": "topic",
                "query_type": "verbatim_quote",
                "topic_id": None,
            }
        ]
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(golden_data, f)
        tmp_path = Path(f.name)

    try:
        with pytest.raises(ValueError, match="scope='topic' but topic_id is None"):
            load_golden_set(tmp_path, corpus=corpus)
    finally:
        tmp_path.unlink()


def test_loader_valid_golden_set():
    """A valid golden set loads without error."""
    corpus = _make_corpus()
    golden_data = {
        "cases": [
            {
                "id": "g1",
                "query": "find hello",
                "expected_message_ids": ["m001"],
                "scope": "all",
                "query_type": "verbatim_quote",
            },
            {
                "id": "g2",
                "query": "find in thread",
                "expected_message_ids": ["m001", "m002"],
                "scope": "thread",
                "query_type": "verbatim_quote",
                "thread_id": "t1",
            },
            {
                "id": "g3",
                "query": "find in topic",
                "expected_message_ids": ["m003"],
                "scope": "topic",
                "query_type": "topic_recall",
                "topic_id": "top2",
            },
        ]
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(golden_data, f)
        tmp_path = Path(f.name)

    try:
        gs = load_golden_set(tmp_path, corpus=corpus)
        assert len(gs.cases) == 3
    finally:
        tmp_path.unlink()
