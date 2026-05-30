"""Tests for eval/retrieval/runner.py."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from eval.retrieval.adapters import IlikeBaselineRetriever, StubSemanticRetriever
from eval.retrieval.loader import load_corpus, load_golden_set
from eval.retrieval.runner import (
    EvalReport,
    main,
    run_eval,
    write_json_report,
    write_markdown_report,
)
from eval.retrieval.schema import Corpus, CorpusMessage, GoldenCase, GoldenSet


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

_SHIPPED_CORPUS = _PROJECT_ROOT / "eval" / "retrieval" / "corpus.yaml"
_SHIPPED_GOLDEN = _PROJECT_ROOT / "eval" / "retrieval" / "golden_set.yaml"


def _mini_golden_set() -> GoldenSet:
    """A tiny golden set for unit-level runner tests."""
    return GoldenSet(
        cases=[
            GoldenCase(
                id="gc1",
                query="hello world",
                expected_message_ids=["m001", "m002"],
                scope="all",
                query_type="verbatim_quote",
            ),
            GoldenCase(
                id="gc2",
                query="paraphrase query",
                expected_message_ids=["m002"],
                scope="all",
                query_type="paraphrase",
            ),
        ]
    )


def _mini_corpus() -> Corpus:
    return Corpus(
        messages=[
            CorpusMessage(
                id="m001",
                thread_id="t1",
                topic_id="top1",
                sender="Alice",
                recipient="Bob",
                sent_at=datetime(2025, 1, 1, 10, 0, 0, tzinfo=timezone.utc),
                content="hello world this is a test",
            ),
            CorpusMessage(
                id="m002",
                thread_id="t1",
                topic_id="top1",
                sender="Bob",
                recipient="Alice",
                sent_at=datetime(2025, 1, 1, 10, 1, 0, tzinfo=timezone.utc),
                content="sure, fine.",
            ),
        ]
    )


# ---------------------------------------------------------------------------
# run_eval unit tests
# ---------------------------------------------------------------------------


def test_run_eval_baseline_mini() -> None:
    """Baseline run against mini corpus produces expected structure."""
    corpus = _mini_corpus()
    golden = _mini_golden_set()
    retriever = IlikeBaselineRetriever(corpus)

    report = run_eval(retriever, corpus, golden)

    assert isinstance(report, EvalReport)
    assert report.adapter_name == "IlikeBaselineRetriever"
    assert report.overall["n"] == 2
    assert "recall@1" in report.overall
    assert "recall@5" in report.overall
    assert "recall@10" in report.overall
    assert "mrr" in report.overall

    # Per-query-type keys should be present.
    assert "verbatim_quote" in report.by_query_type
    assert "paraphrase" in report.by_query_type

    # Per-case results should have two entries.
    assert len(report.per_case) == 2
    for case_result in report.per_case:
        assert "case_id" in case_result
        assert "recall_at_1" in case_result
        assert "recall_at_5" in case_result
        assert "recall_at_10" in case_result
        assert "reciprocal_rank" in case_result


def test_run_eval_stub_all_zero() -> None:
    """Stub retriever produces all-zero overall metrics."""
    corpus = _mini_corpus()
    golden = _mini_golden_set()
    retriever = StubSemanticRetriever(corpus)

    report = run_eval(retriever, corpus, golden)

    assert report.adapter_name == "StubSemanticRetriever"
    assert report.overall["recall@1"] == 0.0
    assert report.overall["recall@5"] == 0.0
    assert report.overall["recall@10"] == 0.0
    assert report.overall["mrr"] == 0.0
    assert report.overall["n"] == 2

    # All per-case recall values should be zero.
    for case_result in report.per_case:
        assert case_result["recall_at_1"] == 0.0
        assert case_result["reciprocal_rank"] == 0.0


def test_run_eval_empty_ks() -> None:
    """Empty ks tuple should skip metric computation defensively."""
    corpus = _mini_corpus()
    golden = _mini_golden_set()
    retriever = IlikeBaselineRetriever(corpus)

    report = run_eval(retriever, corpus, golden, ks=())

    # With empty ks, no metric keys should be present.
    assert len(report.per_case) == 2
    for case_result in report.per_case:
        assert "recall_at_1" not in case_result
        assert "recall_at_5" not in case_result
        assert "recall_at_10" not in case_result
        assert "reciprocal_rank" not in case_result

    # Overall should have n=2 and zeroed metrics.
    assert report.overall["n"] == 2
    assert report.overall["recall@1"] == 0.0
    assert report.overall["recall@5"] == 0.0
    assert report.overall["recall@10"] == 0.0


# ---------------------------------------------------------------------------
# End-to-end: shipped corpus + golden set
# ---------------------------------------------------------------------------


def test_e2e_baseline_against_shipped_golden() -> None:
    """Full E2E: baseline retriever vs shipped golden set produces valid report."""
    corpus = load_corpus(_SHIPPED_CORPUS)
    golden_set = load_golden_set(_SHIPPED_GOLDEN, corpus=corpus)
    retriever = IlikeBaselineRetriever(corpus)

    report = run_eval(retriever, corpus, golden_set)

    # All numeric metrics present.
    assert report.overall["n"] == len(golden_set.cases)
    assert isinstance(report.overall["recall@1"], float)
    assert isinstance(report.overall["recall@5"], float)
    assert isinstance(report.overall["recall@10"], float)
    assert isinstance(report.overall["mrr"], float)

    # Per-query-type keys present (all four types).
    assert "topic_recall" in report.by_query_type
    assert "verbatim_quote" in report.by_query_type
    assert "paraphrase" in report.by_query_type
    assert "cross_thread" in report.by_query_type

    # Every per-query-type aggregate has the same metric keys.
    for qt, agg in report.by_query_type.items():
        assert "recall@1" in agg
        assert "recall@5" in agg
        assert "recall@10" in agg
        assert "mrr" in agg
        assert "n" in agg
        assert agg["n"] > 0

    # Per-case results count matches golden set.
    assert len(report.per_case) == len(golden_set.cases)

    # Per-case results have required fields.
    for case_result in report.per_case:
        assert "case_id" in case_result
        assert "query_type" in case_result
        assert "recall_at_1" in case_result
        assert "recall_at_5" in case_result
        assert "recall_at_10" in case_result
        assert "reciprocal_rank" in case_result
        assert "ranked_ids" in case_result
        assert "expected_ids" in case_result


def test_e2e_json_roundtrip() -> None:
    """EvalReport round-trips through JSON serialization."""
    corpus = load_corpus(_SHIPPED_CORPUS)
    golden_set = load_golden_set(_SHIPPED_GOLDEN, corpus=corpus)
    retriever = IlikeBaselineRetriever(corpus)

    report = run_eval(retriever, corpus, golden_set)

    # Round-trip through json.dumps / json.loads.
    serialized = json.dumps(
        json.loads(report.model_dump_json()), sort_keys=True
    )
    deserialized = json.loads(serialized)

    # Check key structure survives round-trip.
    assert deserialized["adapter_name"] == report.adapter_name
    assert deserialized["overall"]["n"] == report.overall["n"]
    assert deserialized["overall"]["recall@1"] == report.overall["recall@1"]
    assert deserialized["overall"]["mrr"] == report.overall["mrr"]
    assert set(deserialized["by_query_type"].keys()) == set(
        report.by_query_type.keys()
    )
    assert len(deserialized["per_case"]) == len(report.per_case)


def test_e2e_stub_all_zero_overall() -> None:
    """Stub run against shipped golden yields all-zero overall metrics."""
    corpus = load_corpus(_SHIPPED_CORPUS)
    golden_set = load_golden_set(_SHIPPED_GOLDEN, corpus=corpus)
    retriever = StubSemanticRetriever(corpus)

    report = run_eval(retriever, corpus, golden_set)

    assert report.overall["recall@1"] == 0.0
    assert report.overall["recall@5"] == 0.0
    assert report.overall["recall@10"] == 0.0
    assert report.overall["mrr"] == 0.0
    assert report.overall["n"] == len(golden_set.cases)

    # All per-case recall values must be zero.
    for case_result in report.per_case:
        assert case_result["recall_at_1"] == 0.0
        assert case_result["recall_at_5"] == 0.0
        assert case_result["recall_at_10"] == 0.0
        assert case_result["reciprocal_rank"] == 0.0


# ---------------------------------------------------------------------------
# Report writers
# ---------------------------------------------------------------------------


def test_write_json_report_creates_parent_dir() -> None:
    """write_json_report creates parent directories if missing."""
    corpus = _mini_corpus()
    golden = _mini_golden_set()
    retriever = IlikeBaselineRetriever(corpus)
    report = run_eval(retriever, corpus, golden)

    with tempfile.TemporaryDirectory() as tmpdir:
        out_path = Path(tmpdir) / "subdir" / "nested" / "report.json"
        assert not out_path.parent.exists()

        write_json_report(report, out_path)

        assert out_path.exists()
        # Verify it's valid JSON.
        data = json.loads(out_path.read_text())
        assert data["adapter_name"] == "IlikeBaselineRetriever"


def test_write_markdown_report_creates_parent_dir() -> None:
    """write_markdown_report creates parent directories if missing."""
    corpus = _mini_corpus()
    golden = _mini_golden_set()
    retriever = IlikeBaselineRetriever(corpus)
    report = run_eval(retriever, corpus, golden)

    with tempfile.TemporaryDirectory() as tmpdir:
        out_path = Path(tmpdir) / "subdir" / "nested" / "report.md"
        assert not out_path.parent.exists()

        write_markdown_report(report, out_path)

        assert out_path.exists()
        content = out_path.read_text()
        assert len(content) > 0
        assert "# Retrieval Evaluation Report" in content
        assert "## Overall Metrics" in content
        assert "## Per Query-Type Metrics" in content


def test_write_markdown_report_stable_ordering() -> None:
    """Markdown report has sorted query-type keys and stable case ordering."""
    corpus = load_corpus(_SHIPPED_CORPUS)
    golden_set = load_golden_set(_SHIPPED_GOLDEN, corpus=corpus)
    retriever = IlikeBaselineRetriever(corpus)

    report = run_eval(retriever, corpus, golden_set)

    with tempfile.TemporaryDirectory() as tmpdir:
        out_path = Path(tmpdir) / "report.md"
        write_markdown_report(report, out_path)

        content = out_path.read_text()

        # Query types should appear in sorted order.
        qt_order: list[str] = []
        for line in content.splitlines():
            if line.startswith("### ") and not line.startswith("#### "):
                qt_order.append(line[4:].strip())
        assert qt_order == sorted(qt_order)

        # Every query type section should be present.
        for qt in ("cross_thread", "paraphrase", "topic_recall", "verbatim_quote"):
            assert f"### {qt}" in content


# ---------------------------------------------------------------------------
# CLI entrypoint tests
# ---------------------------------------------------------------------------


def test_cli_baseline_via_subprocess() -> None:
    """CLI invocation via subprocess produces markdown and json reports."""
    with tempfile.TemporaryDirectory() as tmpdir:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "eval.retrieval.runner",
                "--adapter",
                "baseline",
                "--corpus",
                str(_SHIPPED_CORPUS),
                "--golden",
                str(_SHIPPED_GOLDEN),
                "--out-dir",
                tmpdir,
            ],
            capture_output=True,
            text=True,
            cwd=str(_PROJECT_ROOT),
            timeout=30,
        )

        assert result.returncode == 0, f"CLI failed: {result.stderr}"

        # JSON report should exist and be valid.
        json_path = Path(tmpdir) / "baseline_report.json"
        assert json_path.exists(), f"JSON report not found at {json_path}"
        json_data = json.loads(json_path.read_text())
        assert json_data["adapter_name"] == "IlikeBaselineRetriever"
        assert "overall" in json_data
        assert "by_query_type" in json_data

        # Markdown report should exist and be non-empty.
        md_path = Path(tmpdir) / "baseline_report.md"
        assert md_path.exists(), f"Markdown report not found at {md_path}"
        md_content = md_path.read_text()
        assert len(md_content) > 0
        assert "# Retrieval Evaluation Report" in md_content


def test_cli_stub_via_subprocess() -> None:
    """CLI invocation with stub adapter produces markdown report."""
    with tempfile.TemporaryDirectory() as tmpdir:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "eval.retrieval.runner",
                "--adapter",
                "stub",
                "--corpus",
                str(_SHIPPED_CORPUS),
                "--golden",
                str(_SHIPPED_GOLDEN),
                "--out-dir",
                tmpdir,
            ],
            capture_output=True,
            text=True,
            cwd=str(_PROJECT_ROOT),
            timeout=30,
        )

        assert result.returncode == 0, f"CLI stub failed: {result.stderr}"

        # JSON report should exist.
        json_path = Path(tmpdir) / "stub_report.json"
        assert json_path.exists()
        json_data = json.loads(json_path.read_text())
        assert json_data["adapter_name"] == "StubSemanticRetriever"
        assert json_data["overall"]["recall@1"] == 0.0
        assert json_data["overall"]["mrr"] == 0.0

        # Markdown report should exist and be non-empty.
        md_path = Path(tmpdir) / "stub_report.md"
        assert md_path.exists()
        md_content = md_path.read_text()
        assert len(md_content) > 0


def test_cli_default_paths_direct_call() -> None:
    """CLI entrypoint works when called directly without path arguments."""
    report = main(
        [
            "--adapter",
            "baseline",
            "--corpus",
            str(_SHIPPED_CORPUS),
            "--golden",
            str(_SHIPPED_GOLDEN),
        ]
    )

    assert isinstance(report, EvalReport)
    assert report.adapter_name == "IlikeBaselineRetriever"
    assert report.overall["n"] == 28  # 28 golden cases
