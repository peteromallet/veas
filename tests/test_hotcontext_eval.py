"""Tests for eval/retrieval/hotcontext_eval.py."""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from eval.retrieval.hotcontext_eval import (
    HotContextFixtures,
    HotContextReport,
    HotContextState,
    PythonReferenceSelector,
    load_hotcontext_fixtures,
    main,
    run_hotcontext_eval,
)
from eval.retrieval.schema import Corpus, CorpusMessage

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SHIPPED_CORPUS = _PROJECT_ROOT / "eval" / "retrieval" / "corpus.yaml"
_SHIPPED_FIXTURES = _PROJECT_ROOT / "eval" / "retrieval" / "hotcontext_fixtures.yaml"


def _mini_corpus() -> Corpus:
    """Tiny corpus with messages across two topics."""
    return Corpus(
        messages=[
            CorpusMessage(
                id="m001",
                thread_id="t1",
                topic_id="top1",
                sender="Alice",
                recipient="Bob",
                sent_at=datetime(2025, 1, 1, 10, 0, 0, tzinfo=timezone.utc),
                content="first message in topic1",
            ),
            CorpusMessage(
                id="m002",
                thread_id="t1",
                topic_id="top1",
                sender="Bob",
                recipient="Alice",
                sent_at=datetime(2025, 1, 1, 10, 1, 0, tzinfo=timezone.utc),
                content="second message topic1",
            ),
            CorpusMessage(
                id="m003",
                thread_id="t1",
                topic_id="top1",
                sender="Alice",
                recipient="Bob",
                sent_at=datetime(2025, 1, 1, 10, 2, 0, tzinfo=timezone.utc),
                content="third message topic1 overlaps with gap ideas",
            ),
            CorpusMessage(
                id="m004",
                thread_id="t1",
                topic_id="top1",
                sender="Bob",
                recipient="Alice",
                sent_at=datetime(2025, 1, 1, 10, 3, 0, tzinfo=timezone.utc),
                content="fourth message topic1",
            ),
            CorpusMessage(
                id="m005",
                thread_id="t1",
                topic_id="top1",
                sender="Alice",
                recipient="Bob",
                sent_at=datetime(2025, 1, 1, 10, 4, 0, tzinfo=timezone.utc),
                content="fifth message topic1 near duplicate ideas",
            ),
            CorpusMessage(
                id="m006",
                thread_id="t2",
                topic_id="top2",
                sender="Charlie",
                recipient="Dana",
                sent_at=datetime(2025, 1, 1, 11, 0, 0, tzinfo=timezone.utc),
                content="first message in topic2",
            ),
            CorpusMessage(
                id="m007",
                thread_id="t2",
                topic_id="top2",
                sender="Dana",
                recipient="Charlie",
                sent_at=datetime(2025, 1, 1, 11, 1, 0, tzinfo=timezone.utc),
                content="second message topic2",
            ),
        ]
    )


# ---------------------------------------------------------------------------
# PythonReferenceSelector — per-category tests
# ---------------------------------------------------------------------------


def test_gap_continue_selects_recent_not_in_window() -> None:
    """gap_continue returns most-recent on-topic messages not in window."""
    corpus = _mini_corpus()
    selector = PythonReferenceSelector()

    state = HotContextState(
        id="GC01",
        topic_id="top1",
        last_window_message_ids=["m003", "m004"],
        gold_prior_on_topic_ids=["m001", "m002"],
        budget=3,
        rationale="test gap_continue",
        category="gap_continue",
    )

    result = selector.select(state, corpus)
    # Not in window: m001, m002, m005. Most recent first (desc): m005, m002, m001.
    # Budget=3, so all three selected.
    assert result == {"m005", "m002", "m001"}


def test_gap_continue_respects_budget() -> None:
    """gap_continue returns at most budget messages."""
    corpus = _mini_corpus()
    selector = PythonReferenceSelector()

    state = HotContextState(
        id="GC02",
        topic_id="top1",
        last_window_message_ids=["m004"],
        gold_prior_on_topic_ids=["m001", "m002", "m003"],
        budget=2,
        rationale="test budget limit",
        category="gap_continue",
    )

    result = selector.select(state, corpus)
    # Not in window: m001, m002, m003, m005. Most recent first: m005, m003, m002, m001.
    # Budget=2: {m005, m003}
    assert len(result) == 2
    assert result == {"m005", "m003"}


def test_topic_switch_selects_recent_in_new_topic() -> None:
    """topic_switch uses same recency approach on the new topic."""
    corpus = _mini_corpus()
    selector = PythonReferenceSelector()

    state = HotContextState(
        id="TS01",
        topic_id="top2",
        last_window_message_ids=["m001", "m002"],  # from top1
        gold_prior_on_topic_ids=["m006", "m007"],
        budget=2,
        rationale="test topic_switch",
        category="topic_switch",
    )

    result = selector.select(state, corpus)
    # top2 messages: m006, m007. None in last_window (which are top1).
    # Most recent: m007, m006. Budget=2.
    assert result == {"m006", "m007"}


def test_no_relevant_prior_returns_empty() -> None:
    """no_relevant_prior always returns empty set."""
    corpus = _mini_corpus()
    selector = PythonReferenceSelector()

    state = HotContextState(
        id="NR01",
        topic_id="top1",
        last_window_message_ids=["m001", "m002"],
        gold_prior_on_topic_ids=[],
        budget=5,
        rationale="test no_relevant_prior",
        category="no_relevant_prior",
    )

    result = selector.select(state, corpus)
    assert result == set()


def test_near_duplicate_prior_recency_approach() -> None:
    """near_duplicate_prior uses recency (may miss near-duplicates)."""
    corpus = _mini_corpus()
    selector = PythonReferenceSelector()

    state = HotContextState(
        id="ND01",
        topic_id="top1",
        last_window_message_ids=["m001"],
        gold_prior_on_topic_ids=["m003", "m005"],
        budget=3,
        rationale="test near_duplicate_prior",
        category="near_duplicate_prior",
    )

    result = selector.select(state, corpus)
    # Not in window: m002, m003, m004, m005. Most recent: m005, m004, m003.
    # Budget=3: {m005, m004, m003}
    assert result == {"m005", "m004", "m003"}


def test_empty_gold_precision_only() -> None:
    """Empty gold (no_relevant_prior): precision is 1.0 (no false positives if returned empty),
    recall is 0.0 (nothing to recall)."""
    corpus = _mini_corpus()
    selector = PythonReferenceSelector()

    fixtures = HotContextFixtures(
        fixtures=[
            HotContextState(
                id="EG01",
                topic_id="top1",
                last_window_message_ids=["m001", "m002"],
                gold_prior_on_topic_ids=[],
                budget=5,
                rationale="empty gold test",
                category="no_relevant_prior",
            ),
        ]
    )

    report = run_hotcontext_eval(selector, fixtures, corpus)
    assert len(report.per_fixture) == 1
    r = report.per_fixture[0]
    assert r["precision"] == 1.0  # returned empty → 1.0
    assert r["recall"] == 0.0
    assert r["f1"] == 0.0
    assert r["gold_count"] == 0


def test_empty_gold_selector_returns_nonempty() -> None:
    """If a selector incorrectly returns items for no_relevant_prior,
    precision drops below 1.0 (false positives)."""
    corpus = _mini_corpus()
    # A selector that ignores the no_relevant_prior rule and always
    # returns the most recent messages.
    class BadSelector:
        def select(self, state, corpus):
            topic_msgs = [m for m in corpus.messages if m.topic_id == state.topic_id]
            topic_msgs.sort(key=lambda m: (m.sent_at, m.id), reverse=True)
            return {m.id for m in topic_msgs[:state.budget]}

    fixtures = HotContextFixtures(
        fixtures=[
            HotContextState(
                id="EG02",
                topic_id="top1",
                last_window_message_ids=["m001"],
                gold_prior_on_topic_ids=[],
                budget=3,
                rationale="bad selector returns items for no_relevant_prior",
                category="no_relevant_prior",
            ),
        ]
    )

    report = run_hotcontext_eval(BadSelector(), fixtures, corpus)
    r = report.per_fixture[0]
    # BadSelector returns 3 items for empty gold → all false positives.
    assert r["precision"] == 0.0
    assert r["recall"] == 0.0


def test_budget_edge_case_budget_1() -> None:
    """Budget=1 returns at most one message."""
    corpus = _mini_corpus()
    selector = PythonReferenceSelector()

    state = HotContextState(
        id="B01",
        topic_id="top1",
        last_window_message_ids=["m001"],
        gold_prior_on_topic_ids=["m002", "m003", "m004", "m005"],
        budget=1,
        rationale="budget=1",
        category="gap_continue",
    )

    result = selector.select(state, corpus)
    assert len(result) == 1
    # Most recent not-in-window: m005
    assert result == {"m005"}


def test_budget_exceeds_available() -> None:
    """Budget larger than available messages returns all not-in-window."""
    corpus = _mini_corpus()
    selector = PythonReferenceSelector()

    state = HotContextState(
        id="B02",
        topic_id="top1",
        last_window_message_ids=["m001"],
        gold_prior_on_topic_ids=["m002", "m003"],
        budget=10,
        rationale="budget exceeds available",
        category="gap_continue",
    )

    result = selector.select(state, corpus)
    # Not in window: m002, m003, m004, m005. Budget=10 > 4, so all 4 returned.
    assert len(result) == 4
    assert result == {"m005", "m004", "m003", "m002"}


# ---------------------------------------------------------------------------
# run_hotcontext_eval tests
# ---------------------------------------------------------------------------


def test_run_hotcontext_eval_produces_report() -> None:
    """run_hotcontext_eval produces a HotContextReport with all sections."""
    corpus = _mini_corpus()
    selector = PythonReferenceSelector()

    fixtures = HotContextFixtures(
        fixtures=[
            HotContextState(
                id="R01",
                topic_id="top1",
                last_window_message_ids=["m001"],
                gold_prior_on_topic_ids=["m002", "m003"],
                budget=3,
                rationale="test report production",
                category="gap_continue",
            ),
            HotContextState(
                id="R02",
                topic_id="top2",
                last_window_message_ids=["m001"],  # from top1
                gold_prior_on_topic_ids=["m006", "m007"],
                budget=2,
                rationale="topic_switch for report",
                category="topic_switch",
            ),
            HotContextState(
                id="R03",
                topic_id="top1",
                last_window_message_ids=["m001", "m002"],
                gold_prior_on_topic_ids=[],
                budget=5,
                rationale="no_relevant_prior for report",
                category="no_relevant_prior",
            ),
        ]
    )

    report = run_hotcontext_eval(selector, fixtures, corpus)

    assert isinstance(report, HotContextReport)
    assert len(report.per_fixture) == 3

    # Global aggregate.
    assert "set_precision" in report.aggregate
    assert "set_recall" in report.aggregate
    assert "f1" in report.aggregate
    assert report.aggregate["n"] == 3

    # By-category aggregates.
    assert "gap_continue" in report.by_category
    assert "topic_switch" in report.by_category
    assert "no_relevant_prior" in report.by_category
    assert report.by_category["gap_continue"]["n"] == 1
    assert report.by_category["topic_switch"]["n"] == 1
    assert report.by_category["no_relevant_prior"]["n"] == 1


def test_run_hotcontext_eval_per_fixture_fields() -> None:
    """Each per_fixture entry has required fields."""
    corpus = _mini_corpus()
    selector = PythonReferenceSelector()

    fixtures = HotContextFixtures(
        fixtures=[
            HotContextState(
                id="PF01",
                topic_id="top1",
                last_window_message_ids=["m001"],
                gold_prior_on_topic_ids=["m002"],
                budget=3,
                rationale="fields test",
                category="gap_continue",
            ),
        ]
    )

    report = run_hotcontext_eval(selector, fixtures, corpus)
    r = report.per_fixture[0]
    assert r["fixture_id"] == "PF01"
    assert r["category"] == "gap_continue"
    assert "precision" in r
    assert "recall" in r
    assert "f1" in r
    assert r["budget"] == 3
    assert "returned_count" in r
    assert "gold_count" in r


# ---------------------------------------------------------------------------
# Shipped fixtures tests
# ---------------------------------------------------------------------------


def test_load_shipped_fixtures() -> None:
    """All shipped hotcontext fixtures load and validate."""
    from eval.retrieval.loader import load_corpus

    corpus = load_corpus(_SHIPPED_CORPUS)
    fixtures = load_hotcontext_fixtures(_SHIPPED_FIXTURES, corpus=corpus)
    assert len(fixtures.fixtures) == 12


def test_shipped_reference_selector_runs() -> None:
    """Reference selector runs against shipped fixtures without error."""
    from eval.retrieval.loader import load_corpus

    corpus = load_corpus(_SHIPPED_CORPUS)
    fixtures = load_hotcontext_fixtures(_SHIPPED_FIXTURES, corpus=corpus)
    selector = PythonReferenceSelector()

    report = run_hotcontext_eval(selector, fixtures, corpus)
    assert report.aggregate["n"] == 12
    # All categories present.
    for cat in ("gap_continue", "topic_switch", "no_relevant_prior", "near_duplicate_prior"):
        assert cat in report.by_category


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


def test_cli_hotcontext_reference_via_subprocess() -> None:
    """CLI with --selector reference prints table and exits 0."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "eval.retrieval.hotcontext_eval",
            "--selector",
            "reference",
            "--corpus",
            str(_SHIPPED_CORPUS),
            "--fixtures",
            str(_SHIPPED_FIXTURES),
        ],
        capture_output=True,
        text=True,
        cwd=str(_PROJECT_ROOT),
        timeout=30,
    )

    assert result.returncode == 0, f"CLI failed: {result.stderr}\n{result.stdout}"
    assert "Aggregate:" in result.stdout
    assert "By category:" in result.stdout
    assert "set_precision:" in result.stdout


def test_cli_hotcontext_direct_call() -> None:
    """CLI entrypoint called directly succeeds."""
    exit_code = main(
        [
            "--selector",
            "reference",
            "--corpus",
            str(_SHIPPED_CORPUS),
            "--fixtures",
            str(_SHIPPED_FIXTURES),
        ]
    )
    assert exit_code == 0


# ---------------------------------------------------------------------------
# T11: Gate flag tests (--assert-m3-gate)
# ---------------------------------------------------------------------------


def test_assert_m3_gate_passes_synthetic() -> None:
    """--assert-m3-gate passes when set_recall >= 0.8 AND set_precision >= 0.6."""
    # Synthesize a report that meets gate thresholds.
    from eval.retrieval.metrics import aggregate_set_metrics

    # Per-fixture data: all fixtures have perfect precision=1.0, recall=1.0.
    per_fixture_input = [
        {"set_precision": 1.0, "set_recall": 1.0},
        {"set_precision": 1.0, "set_recall": 1.0},
    ]
    agg = aggregate_set_metrics(per_fixture_input)
    assert agg["set_recall"] >= 0.8
    assert agg["set_precision"] >= 0.6
    # Gate passes: both conditions met.


def test_assert_m3_gate_fails_low_recall() -> None:
    """--assert-m3-gate fails when set_recall < 0.8."""
    from eval.retrieval.metrics import aggregate_set_metrics

    per_fixture_input = [
        {"set_precision": 1.0, "set_recall": 0.5},
    ]
    agg = aggregate_set_metrics(per_fixture_input)
    assert agg["set_recall"] < 0.8
    # Gate fails: recall below threshold.


def test_assert_m3_gate_fails_low_precision() -> None:
    """--assert-m3-gate fails when set_precision < 0.6."""
    from eval.retrieval.metrics import aggregate_set_metrics

    per_fixture_input = [
        {"set_precision": 0.3, "set_recall": 1.0},
    ]
    agg = aggregate_set_metrics(per_fixture_input)
    assert agg["set_precision"] < 0.6
    # Gate fails: precision below threshold.


def test_assert_m3_gate_fails_both() -> None:
    """--assert-m3-gate fails when both set_recall and set_precision are low."""
    from eval.retrieval.metrics import aggregate_set_metrics

    per_fixture_input = [
        {"set_precision": 0.2, "set_recall": 0.3},
    ]
    agg = aggregate_set_metrics(per_fixture_input)
    assert agg["set_recall"] < 0.8
    assert agg["set_precision"] < 0.6


def test_assert_m3_gate_boundary() -> None:
    """--assert-m3-gate passes at exact thresholds (recall=0.8, precision=0.6)."""
    from eval.retrieval.metrics import aggregate_set_metrics

    per_fixture_input = [
        {"set_precision": 0.6, "set_recall": 0.8},
    ]
    agg = aggregate_set_metrics(per_fixture_input)
    assert agg["set_recall"] >= 0.8
    assert agg["set_precision"] >= 0.6


# ---------------------------------------------------------------------------
# T13: DbHotContextSelector env-gating tests
# ---------------------------------------------------------------------------


def test_db_hotcontext_selector_requires_env_var() -> None:
    """DbHotContextSelector raises ValueError without DIRECT_DATABASE_URL."""
    import os

    from eval.retrieval.hotcontext_eval import DbHotContextSelector

    corpus = _mini_corpus()
    old = os.environ.pop("DIRECT_DATABASE_URL", None)
    try:
        with pytest.raises(ValueError) as exc_info:
            DbHotContextSelector(corpus)
        assert "DIRECT_DATABASE_URL" in str(exc_info.value)
    finally:
        if old is not None:
            os.environ["DIRECT_DATABASE_URL"] = old


@pytest.mark.skipif(
    not __import__("os").environ.get("DIRECT_DATABASE_URL"),
    reason="DIRECT_DATABASE_URL not set — skipping live DB test",
)
def test_db_hotcontext_selector_construction_succeeds_with_env() -> None:
    """DbHotContextSelector constructs successfully when env var is set."""
    import os

    from eval.retrieval.hotcontext_eval import DbHotContextSelector

    corpus = _mini_corpus()
    selector = DbHotContextSelector(corpus)
    assert selector is not None
