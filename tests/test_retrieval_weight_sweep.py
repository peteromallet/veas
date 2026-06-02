"""Tests for eval/retrieval/weight_sweep.py."""

from __future__ import annotations

from pathlib import Path

import pytest

from eval.retrieval.runner import EvalReport
from eval.retrieval.weight_sweep import (
    SweepCandidateSpec,
    SweepSourceTypeCount,
    _parse_candidate_arg,
    run_weight_sweep,
    validate_source_type_counts,
)


def _write_source_aware_fixture_files(tmp_path: Path) -> tuple[Path, Path]:
    corpus_path = tmp_path / "corpus.yaml"
    corpus_path.write_text(
        """
messages:
  - id: m001
    thread_id: thread-1
    topic_id: topic-1
    sender: Alice
    recipient: Bob
    sent_at: 2025-01-01T00:00:00Z
    content: hello there
""".strip()
        + "\n",
        encoding="utf-8",
    )

    golden_path = tmp_path / "golden.yaml"
    golden_path.write_text(
        """
cases:
  - id: gc1
    query: deployment rollback
    expected_message_ids: [m001]
    expected_source_keys:
      - source_type: conversation_note
        source_id: note001
    scope: all
    query_type: knowledge_recall
    intent: know_about
    extra_scope: {}
  - id: gc2
    query: relationship pattern
    expected_message_ids: [m001]
    expected_source_keys:
      - source_type: theme
        source_id: thm001
    scope: all
    query_type: exact_source_quote
    intent: exact_said
    extra_scope: {}
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return corpus_path, golden_path


def _report(*, know_about_r10: float, know_about_mrr: float, exact_r10: float, exact_mrr: float) -> EvalReport:
    return EvalReport(
        adapter_name="DbBackedRetriever",
        corpus_path="",
        golden_set_path="",
        overall={"recall@1": 0.1, "recall@5": 0.2, "recall@10": 0.3, "mrr": 0.4, "n": 2},
        by_query_type={},
        by_source_type={
            "conversation_note": {"recall@10": know_about_r10, "mrr": know_about_mrr, "n": 1},
            "theme": {"recall@10": exact_r10, "mrr": exact_mrr, "n": 1},
        },
        by_intent={
            "know_about": {
                "recall@1": 0.1,
                "recall@5": 0.2,
                "recall@10": know_about_r10,
                "mrr": know_about_mrr,
                "n": 1,
            },
            "exact_said": {
                "recall@1": 0.1,
                "recall@5": 0.2,
                "recall@10": exact_r10,
                "mrr": exact_mrr,
                "n": 1,
            },
        },
        by_fairness=None,
        by_difficulty=None,
        per_case=[],
    )


def test_parse_candidate_arg_requires_name_equals_json() -> None:
    candidate = _parse_candidate_arg('notes_bias={"conversation_note":1.2,"theme":0.25}')

    assert candidate.name == "notes_bias"
    assert candidate.source_weight_map == {
        "conversation_note": 1.2,
        "theme": 0.25,
    }


def test_validate_source_type_counts_fails_when_expected_non_message_counts_are_missing() -> None:
    counts = [
        SweepSourceTypeCount(source_type="message", searchable_count=10, embedding_count=10),
        SweepSourceTypeCount(source_type="conversation_note", searchable_count=0, embedding_count=5),
        SweepSourceTypeCount(source_type="theme", searchable_count=4, embedding_count=0),
    ]

    with pytest.raises(ValueError) as exc_info:
        validate_source_type_counts(
            counts,
            expected_source_types=["message", "conversation_note", "theme"],
        )

    message = str(exc_info.value)
    assert "conversation_note: searchable_count=0" in message
    assert "theme: embedding_count=0" in message


def test_run_weight_sweep_records_counts_and_no_regression_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus_path, golden_path = _write_source_aware_fixture_files(tmp_path)
    monkeypatch.setenv("DIRECT_DATABASE_URL", "postgresql://direct.example/db")

    monkeypatch.setattr(
        "eval.retrieval.weight_sweep.fetch_source_type_counts",
        lambda _database_url: [
            SweepSourceTypeCount(
                source_type="message", searchable_count=12, embedding_count=12
            ),
            SweepSourceTypeCount(
                source_type="conversation_note", searchable_count=3, embedding_count=3
            ),
            SweepSourceTypeCount(
                source_type="theme", searchable_count=2, embedding_count=2
            ),
        ],
    )

    baseline = _report(
        know_about_r10=0.80,
        know_about_mrr=0.70,
        exact_r10=0.90,
        exact_mrr=0.85,
    )
    passing = _report(
        know_about_r10=0.79,
        know_about_mrr=0.69,
        exact_r10=0.89,
        exact_mrr=0.84,
    )
    failing = _report(
        know_about_r10=0.60,
        know_about_mrr=0.69,
        exact_r10=0.89,
        exact_mrr=0.80,
    )

    def fake_run_db_eval(_corpus: object, _golden_set: object, *, source_weight_map: dict[str, float] | None):
        if source_weight_map is None:
            return baseline
        if source_weight_map == {"conversation_note": 1.2, "theme": 0.25}:
            return passing
        if source_weight_map == {"conversation_note": 0.7, "theme": 0.1}:
            return failing
        raise AssertionError(f"unexpected candidate map: {source_weight_map}")

    monkeypatch.setattr("eval.retrieval.weight_sweep._run_db_eval", fake_run_db_eval)

    report = run_weight_sweep(
        corpus_path=corpus_path,
        golden_path=golden_path,
        candidates=[
            SweepCandidateSpec(
                name="pass",
                source_weight_map={"conversation_note": 1.2, "theme": 0.25},
            ),
            SweepCandidateSpec(
                name="fail",
                source_weight_map={"conversation_note": 0.7, "theme": 0.1},
            ),
        ],
        threshold=0.02,
    )

    assert report.expected_source_types == ["conversation_note", "message", "theme"]
    assert [row.source_type for row in report.source_type_counts] == [
        "message",
        "conversation_note",
        "theme",
    ]
    assert report.baseline.name == "baseline"
    assert report.baseline.no_regression_passed is True
    assert [candidate.name for candidate in report.candidates] == ["pass", "fail"]

    passing_candidate = report.candidates[0]
    assert passing_candidate.no_regression_passed is True
    assert all(guard.passed for guard in passing_candidate.intent_guard)

    failing_candidate = report.candidates[1]
    assert failing_candidate.no_regression_passed is False
    know_about_guard = next(
        guard for guard in failing_candidate.intent_guard if guard.intent == "know_about"
    )
    assert know_about_guard.passed is False
    assert "recall@10 regressed" in know_about_guard.failures[0]
    exact_guard = next(
        guard for guard in failing_candidate.intent_guard if guard.intent == "exact_said"
    )
    assert exact_guard.passed is False
    assert any("mrr regressed" in failure for failure in exact_guard.failures)
