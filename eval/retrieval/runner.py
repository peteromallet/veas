"""
Retrieval evaluation runner.

Wires the loader, metrics, and retriever adapters into a single evaluation
pipeline, producing structured JSON and Markdown reports.

Usage:
    python -m eval.retrieval.runner --adapter {baseline,stub}
        [--corpus PATH] [--golden PATH] [--out-dir PATH]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from eval.retrieval.adapters import IlikeBaselineRetriever, Retriever, StubSemanticRetriever
from eval.retrieval.loader import load_corpus, load_golden_set
from eval.retrieval.metrics import (
    aggregate,
    aggregate_by_difficulty,
    aggregate_by_fairness,
    aggregate_by_intent,
    aggregate_by_query_type,
    aggregate_by_source_type,
    recall_at_k,
    reciprocal_rank,
)
from eval.retrieval.schema import Corpus, GoldenSet


def _source_key_id(source_type: str, source_id: str) -> str:
    return f"{source_type}:{source_id}"


def _expected_source_type_bucket(expected_source_key_ids: list[dict[str, str]]) -> str:
    source_types = {item["source_type"] for item in expected_source_key_ids}
    if not source_types:
        return "unknown"
    if len(source_types) == 1:
        return next(iter(source_types))
    return "mixed"


# ---------------------------------------------------------------------------
# Report model
# ---------------------------------------------------------------------------


class EvalReport(BaseModel):
    """Structured evaluation report for a single adapter run."""

    adapter_name: str
    corpus_path: str
    golden_set_path: str
    overall: dict[str, float | int]
    by_query_type: dict[str, dict[str, float | int]]
    by_source_type: dict[str, dict[str, float | int]]
    by_intent: dict[str, dict[str, float | int]]
    by_fairness: dict[str, dict[str, float | int]] | None = None
    by_difficulty: dict[str, dict[str, float | int]] | None = None
    per_case: list[dict[str, Any]]
    generated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ---------------------------------------------------------------------------
# Core runner
# ---------------------------------------------------------------------------


def run_eval(
    retriever: Retriever,
    corpus: Corpus,
    golden_set: GoldenSet,
    *,
    ks: tuple[int, ...] = (1, 5, 10),
) -> EvalReport:
    """Run a full evaluation of a retriever against a golden set.

    Args:
        retriever: Any object satisfying the Retriever protocol.
        corpus: The corpus of messages to search over.
        golden_set: The golden test cases.
        ks: Recall cutoffs to compute. If empty, limit=0 and no metrics
            are computed (per-case results will have no recall keys).

    Returns:
        EvalReport with overall and per-query-type aggregates.
    """
    # Defensive: handle empty ks (callers-2).
    limit = max(ks) if ks else 0

    per_case_results: list[dict[str, Any]] = []

    for case in golden_set.cases:
        extra_scope = dict(case.extra_scope or {})
        extra_scope.setdefault("query_type", case.query_type)
        ranked_source_keys = retriever.retrieve(
            query=case.query,
            scope=case.scope,
            thread_id=case.thread_id,
            topic_id=case.topic_id,
            limit=limit,
            **extra_scope,
        )
        ranked_ids = [
            result.source_id
            for result in ranked_source_keys
            if result.source_type == "message"
        ]
        ranked_source_key_ids = [
            _source_key_id(result.source_type, result.source_id)
            for result in ranked_source_keys
        ]
        expected_source_key_ids = [
            _source_key_id(key.source_type, key.source_id)
            for key in case.expected_source_keys
        ]
        expected_source_keys = [
            key.model_dump(mode="json") for key in case.expected_source_keys
        ]
        source_type_bucket = _expected_source_type_bucket(expected_source_keys)
        intent_bucket = case.intent or "unlabeled"

        case_result: dict[str, Any] = {
            "case_id": case.id,
            "query": case.query,
            "query_type": case.query_type,
            "source_type": source_type_bucket,
            "intent": intent_bucket,
            "scope": case.scope,
            "fairness": case.fairness or "unlabeled",
            "difficulty": case.difficulty or "unlabeled",
            "expected_count": len(case.expected_source_keys),
            "retrieved_count": len(ranked_source_keys),
            "ranked_source_keys": [
                result.model_dump(mode="json") for result in ranked_source_keys
            ],
            "expected_source_keys": expected_source_keys,
            "ranked_ids": ranked_ids,
            "expected_ids": case.expected_message_ids,
            "notes": case.notes,
        }

        # Compute metrics only if ks is non-empty.
        if ks:
            for k in ks:
                case_result[f"recall_at_{k}"] = recall_at_k(
                    ranked_source_key_ids, expected_source_key_ids, k
                )
            case_result["reciprocal_rank"] = reciprocal_rank(
                ranked_source_key_ids, expected_source_key_ids
            )

        per_case_results.append(case_result)

    if ks and per_case_results:
        overall = aggregate(per_case_results)
        by_query_type = aggregate_by_query_type(per_case_results)
        by_source_type = aggregate_by_source_type(per_case_results)
        by_intent = aggregate_by_intent(per_case_results)
        by_fairness = aggregate_by_fairness(per_case_results) or None
        by_difficulty = aggregate_by_difficulty(per_case_results) or None
    else:
        overall = {
            "recall@1": 0.0,
            "recall@5": 0.0,
            "recall@10": 0.0,
            "mrr": 0.0,
            "n": len(golden_set.cases),
        }
        by_query_type = {}
        by_source_type = {}
        by_intent = {}
        by_fairness = None
        by_difficulty = None

    return EvalReport(
        adapter_name=getattr(retriever, "__class__", type(retriever)).__name__,
        corpus_path="",  # Filled by CLI wrapper or caller
        golden_set_path="",  # Filled by CLI wrapper or caller
        overall=overall,
        by_query_type=by_query_type,
        by_source_type=by_source_type,
        by_intent=by_intent,
        by_fairness=by_fairness,
        by_difficulty=by_difficulty,
        per_case=per_case_results,
    )


# ---------------------------------------------------------------------------
# Report writers
# ---------------------------------------------------------------------------


def write_json_report(report: EvalReport, path: Path) -> None:
    """Write the evaluation report as JSON.

    Per SD5 / all_locations-2: creates parent directories if missing.

    Args:
        report: The EvalReport to serialize.
        path: Destination file path.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.model_dump_json(indent=2), encoding="utf-8")


def write_markdown_report(report: EvalReport, path: Path) -> None:
    """Write the evaluation report as Markdown.

    Contains an overall table and a per-query-type table with sorted keys
    and stable case ordering.

    Per SD5 / all_locations-2: creates parent directories if missing.

    Args:
        report: The EvalReport to serialize.
        path: Destination file path.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []

    lines.append(f"# Retrieval Evaluation Report")
    lines.append(f"")
    lines.append(f"- **Adapter:** {report.adapter_name}")
    lines.append(f"- **Corpus:** {report.corpus_path}")
    lines.append(f"- **Golden Set:** {report.golden_set_path}")
    lines.append(f"- **Generated:** {report.generated_at}")
    lines.append(f"- **Cases:** {report.overall.get('n', 0)}")
    lines.append(f"")

    # Overall table
    lines.append(f"## Overall Metrics")
    lines.append(f"")
    lines.append(f"| Metric    | Value |")
    lines.append(f"|-----------|-------|")
    metric_keys = sorted(k for k in report.overall if k != "n")
    for key in metric_keys:
        val = report.overall[key]
        lines.append(f"| {key} | {val:.4f} |")
    lines.append(f"| n         | {report.overall.get('n', 0)} |")
    lines.append(f"")

    # Per query-type tables (sorted keys, stable case ordering)
    if report.by_query_type:
        lines.append(f"## Per Query-Type Metrics")
        lines.append(f"")
        for qt in sorted(report.by_query_type.keys()):
            qt_data = report.by_query_type[qt]
            lines.append(f"### {qt}")
            lines.append(f"")
            lines.append(f"| Metric    | Value |")
            lines.append(f"|-----------|-------|")
            for mkey in sorted(k for k in qt_data if k != "n"):
                lines.append(f"| {mkey} | {qt_data[mkey]:.4f} |")
            lines.append(f"| n         | {qt_data.get('n', 0)} |")
            lines.append(f"")

            # Case listing for this query type
            matching_cases = [c for c in report.per_case if c.get("query_type") == qt]
            if matching_cases:
                lines.append(f"#### Cases")
                lines.append(f"")
                lines.append(
                    f"| Case ID | Query | Scope | Expected | Retrieved | "
                    f"Recall@1 | Recall@5 | Recall@10 | MRR |"
                )
                lines.append(
                    f"|---------|-------|-------|----------|-----------|"
                    f"----------|----------|-----------|-----|"
                )
                for c in matching_cases:
                    lines.append(
                        f"| {c.get('case_id', '')} "
                        f"| {c.get('query', '')} "
                        f"| {c.get('scope', '')} "
                        f"| {c.get('expected_count', 0)} "
                        f"| {c.get('retrieved_count', 0)} "
                        f"| {c.get('recall_at_1', 0):.4f} "
                        f"| {c.get('recall_at_5', 0):.4f} "
                        f"| {c.get('recall_at_10', 0):.4f} "
                        f"| {c.get('reciprocal_rank', 0):.4f} |"
                    )
                lines.append(f"")

    if report.by_source_type:
        lines.append(f"## Per Source-Type Metrics")
        lines.append(f"")
        for source_type in sorted(report.by_source_type.keys()):
            source_data = report.by_source_type[source_type]
            lines.append(f"### {source_type}")
            lines.append(f"")
            lines.append(f"| Metric    | Value |")
            lines.append(f"|-----------|-------|")
            for mkey in sorted(k for k in source_data if k != "n"):
                lines.append(f"| {mkey} | {source_data[mkey]:.4f} |")
            lines.append(f"| n         | {source_data.get('n', 0)} |")
            lines.append(f"")

    if report.by_intent:
        lines.append(f"## Per Intent Metrics")
        lines.append(f"")
        for intent in sorted(report.by_intent.keys()):
            intent_data = report.by_intent[intent]
            lines.append(f"### {intent}")
            lines.append(f"")
            lines.append(f"| Metric    | Value |")
            lines.append(f"|-----------|-------|")
            for mkey in sorted(k for k in intent_data if k != "n"):
                lines.append(f"| {mkey} | {intent_data[mkey]:.4f} |")
            lines.append(f"| n         | {intent_data.get('n', 0)} |")
            lines.append(f"")

    # Fairness breakdown table (only when non-None and non-empty).
    if report.by_fairness:
        lines.append(f"## Per Fairness Metrics")
        lines.append(f"")
        for fl in sorted(report.by_fairness.keys()):
            fl_data = report.by_fairness[fl]
            lines.append(f"### {fl}")
            lines.append(f"")
            lines.append(f"| Metric    | Value |")
            lines.append(f"|-----------|-------|")
            for mkey in sorted(k for k in fl_data if k != "n"):
                lines.append(f"| {mkey} | {fl_data[mkey]:.4f} |")
            lines.append(f"| n         | {fl_data.get('n', 0)} |")
            lines.append(f"")

    # Difficulty breakdown table (only when non-None and non-empty).
    if report.by_difficulty:
        lines.append(f"## Per Difficulty Metrics")
        lines.append(f"")
        for dl in sorted(report.by_difficulty.keys()):
            dl_data = report.by_difficulty[dl]
            lines.append(f"### {dl}")
            lines.append(f"")
            lines.append(f"| Metric    | Value |")
            lines.append(f"|-----------|-------|")
            for mkey in sorted(k for k in dl_data if k != "n"):
                lines.append(f"| {mkey} | {dl_data[mkey]:.4f} |")
            lines.append(f"| n         | {dl_data.get('n', 0)} |")
            lines.append(f"")

    path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run retrieval evaluation harness against an adapter."
    )
    parser.add_argument(
        "--adapter",
        choices=["baseline", "stub", "semantic", "hybrid", "openai", "hybrid-openai", "db"],
        required=True,
        help="Retriever adapter to evaluate.",
    )
    parser.add_argument(
        "--corpus",
        type=Path,
        default=None,
        help="Path to corpus YAML (default: eval/retrieval/corpus.yaml).",
    )
    parser.add_argument(
        "--golden",
        type=Path,
        default=None,
        help="Path to golden set YAML (default: eval/retrieval/golden_set.yaml).",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Directory for output reports (default: eval/retrieval/reports/).",
    )
    return parser


def _default_path(relative: str) -> Path:
    """Resolve a relative path against the project root (parent of eval/)."""
    base = Path(__file__).resolve().parent.parent.parent
    return base / relative


def main(argv: list[str] | None = None) -> EvalReport:
    """CLI entrypoint.

    Args:
        argv: Command-line arguments (defaults to sys.argv[1:]).

    Returns:
        The resulting EvalReport (also written to disk).
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Resolve default paths relative to project root.
    corpus_path: Path = args.corpus or _default_path("eval/retrieval/corpus.yaml")
    golden_path: Path = args.golden or _default_path("eval/retrieval/golden_set.yaml")
    out_dir: Path = args.out_dir or _default_path("eval/retrieval/reports/")

    # Load data.
    corpus = load_corpus(corpus_path)
    golden_set = load_golden_set(golden_path, corpus=corpus)

    # Build retriever.
    adapter_name = args.adapter
    retriever: Retriever
    if adapter_name == "baseline":
        retriever = IlikeBaselineRetriever(corpus)
    elif adapter_name == "stub":
        retriever = StubSemanticRetriever(corpus)
    elif adapter_name == "semantic":
        from eval.retrieval.adapters import SemanticRetriever

        retriever = SemanticRetriever(corpus)
    elif adapter_name == "hybrid":
        from eval.retrieval.adapters import HybridRetriever

        retriever = HybridRetriever(corpus)
    elif adapter_name == "openai":
        from eval.retrieval.adapters import SemanticRetriever
        from eval.retrieval.embeddings import OpenAIEmbedder

        retriever = SemanticRetriever(corpus, OpenAIEmbedder())
    elif adapter_name == "hybrid-openai":
        from eval.retrieval.adapters import HybridRetriever
        from eval.retrieval.embeddings import OpenAIEmbedder

        retriever = HybridRetriever(corpus, embedder=OpenAIEmbedder())
    elif adapter_name == "db":
        from eval.retrieval.adapters import DbBackedRetriever

        try:
            retriever = DbBackedRetriever(corpus)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        raise ValueError(f"Unknown adapter: {adapter_name}")

    # Run evaluation.
    report = run_eval(retriever, corpus, golden_set)

    # Stamp paths into report.
    report.corpus_path = str(corpus_path)
    report.golden_set_path = str(golden_path)

    # Write reports.
    json_path = out_dir / f"{adapter_name}_report.json"
    md_path = out_dir / f"{adapter_name}_report.md"
    write_json_report(report, json_path)
    write_markdown_report(report, md_path)

    print(f"Reports written to {out_dir.resolve()}")
    print(f"  JSON: {json_path.name}")
    print(f"  MD:   {md_path.name}")

    return report


if __name__ == "__main__":
    main()
