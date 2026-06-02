"""Sweep candidate retrieval source-weight maps against a DB-backed golden set."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field

from eval.retrieval.adapters import DbBackedRetriever
from eval.retrieval.loader import load_corpus, load_golden_set
from eval.retrieval.runner import EvalReport, run_eval

SOURCE_TYPE_COUNTS_SQL = """
WITH searchable AS (
    SELECT source_type, count(*)::bigint AS searchable_count
    FROM mediator.v_searchable_content
    GROUP BY source_type
),
embeddings AS (
    SELECT source_type, count(*)::bigint AS embedding_count
    FROM mediator.content_embeddings
    GROUP BY source_type
)
SELECT
    COALESCE(searchable.source_type, embeddings.source_type) AS source_type,
    COALESCE(searchable.searchable_count, 0)::bigint AS searchable_count,
    COALESCE(embeddings.embedding_count, 0)::bigint AS embedding_count
FROM searchable
FULL OUTER JOIN embeddings USING (source_type)
ORDER BY source_type
"""

INTENT_GUARD_INTENTS = ("know_about", "exact_said")
DEFAULT_NO_REGRESSION_THRESHOLD = 0.02


class SweepCandidateSpec(BaseModel):
    name: str
    source_weight_map: dict[str, float] = Field(default_factory=dict)


class SweepSourceTypeCount(BaseModel):
    source_type: str
    searchable_count: int
    embedding_count: int


class IntentGuardResult(BaseModel):
    intent: str
    baseline_recall_at_10: float
    candidate_recall_at_10: float
    baseline_mrr: float
    candidate_mrr: float
    passed: bool
    failures: list[str] = Field(default_factory=list)


class SweepCandidateResult(BaseModel):
    name: str
    source_weight_map: dict[str, float] = Field(default_factory=dict)
    overall: dict[str, float | int]
    by_source_type: dict[str, dict[str, float | int]]
    by_intent: dict[str, dict[str, float | int]]
    no_regression_passed: bool
    intent_guard: list[IntentGuardResult]


class WeightSweepReport(BaseModel):
    corpus_path: str
    golden_set_path: str
    generated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    no_regression_threshold: float
    source_type_counts: list[SweepSourceTypeCount]
    expected_source_types: list[str]
    baseline: SweepCandidateResult
    candidates: list[SweepCandidateResult]


def _default_path(relative: str) -> Path:
    base = Path(__file__).resolve().parent.parent.parent
    return base / relative


def _default_golden_path() -> Path:
    real_golden = _default_path("eval/retrieval/real_golden_set.yaml")
    if real_golden.exists():
        return real_golden
    return _default_path("eval/retrieval/golden_set.yaml")


def _direct_database_url() -> str:
    db_url = (os.environ.get("DIRECT_DATABASE_URL") or "").strip()
    if not db_url:
        raise ValueError(
            "DIRECT_DATABASE_URL must be set to run retrieval weight sweep"
        )
    return db_url


async def _fetch_source_type_counts_async(database_url: str) -> list[SweepSourceTypeCount]:
    import asyncpg

    conn = await asyncpg.connect(database_url, statement_cache_size=0)
    try:
        rows = await conn.fetch(SOURCE_TYPE_COUNTS_SQL)
    finally:
        await conn.close()

    return [
        SweepSourceTypeCount(
            source_type=str(row["source_type"]),
            searchable_count=int(row["searchable_count"]),
            embedding_count=int(row["embedding_count"]),
        )
        for row in rows
    ]


def fetch_source_type_counts(database_url: str) -> list[SweepSourceTypeCount]:
    return asyncio.run(_fetch_source_type_counts_async(database_url))


def _expected_source_types(golden_set: object) -> list[str]:
    source_types = {
        key.source_type
        for case in golden_set.cases
        for key in case.expected_source_keys
    }
    return sorted(source_types)


def validate_source_type_counts(
    counts: list[SweepSourceTypeCount],
    *,
    expected_source_types: list[str],
) -> None:
    count_by_type = {row.source_type: row for row in counts}
    problems: list[str] = []

    for source_type in expected_source_types:
        row = count_by_type.get(
            source_type,
            SweepSourceTypeCount(
                source_type=source_type,
                searchable_count=0,
                embedding_count=0,
            ),
        )
        if row.searchable_count <= 0:
            problems.append(
                f"{source_type}: searchable_count=0 but the golden set expects this source type"
            )
        if row.embedding_count <= 0:
            problems.append(
                f"{source_type}: embedding_count=0 but the golden set expects this source type"
            )

    if problems:
        raise ValueError(
            "Source-type count precheck failed:\n- " + "\n- ".join(problems)
        )


def _candidate_result_from_report(
    name: str,
    source_weight_map: dict[str, float],
    report: EvalReport,
    *,
    baseline: EvalReport | None,
    threshold: float,
) -> SweepCandidateResult:
    intent_guard: list[IntentGuardResult] = []
    no_regression_passed = True

    if baseline is not None:
        for intent in INTENT_GUARD_INTENTS:
            baseline_metrics = baseline.by_intent.get(intent)
            candidate_metrics = report.by_intent.get(intent)
            if baseline_metrics is None or candidate_metrics is None:
                raise ValueError(
                    f"Intent guard requires by_intent metrics for {intent!r} in both baseline and candidate reports"
                )

            failures: list[str] = []
            baseline_recall = float(baseline_metrics.get("recall@10", 0.0))
            candidate_recall = float(candidate_metrics.get("recall@10", 0.0))
            baseline_mrr = float(baseline_metrics.get("mrr", 0.0))
            candidate_mrr = float(candidate_metrics.get("mrr", 0.0))

            if candidate_recall + threshold < baseline_recall:
                failures.append(
                    f"recall@10 regressed by {baseline_recall - candidate_recall:.4f}"
                )
            if candidate_mrr + threshold < baseline_mrr:
                failures.append(f"mrr regressed by {baseline_mrr - candidate_mrr:.4f}")

            passed = not failures
            no_regression_passed = no_regression_passed and passed
            intent_guard.append(
                IntentGuardResult(
                    intent=intent,
                    baseline_recall_at_10=baseline_recall,
                    candidate_recall_at_10=candidate_recall,
                    baseline_mrr=baseline_mrr,
                    candidate_mrr=candidate_mrr,
                    passed=passed,
                    failures=failures,
                )
            )

    return SweepCandidateResult(
        name=name,
        source_weight_map=source_weight_map,
        overall=report.overall,
        by_source_type=report.by_source_type,
        by_intent=report.by_intent,
        no_regression_passed=no_regression_passed,
        intent_guard=intent_guard,
    )


def _run_db_eval(
    corpus: object,
    golden_set: object,
    *,
    source_weight_map: dict[str, float] | None,
) -> EvalReport:
    retriever = DbBackedRetriever(corpus, source_weight_map=source_weight_map)
    try:
        return run_eval(retriever, corpus, golden_set)
    finally:
        retriever.close()


def run_weight_sweep(
    *,
    corpus_path: Path,
    golden_path: Path,
    candidates: list[SweepCandidateSpec],
    threshold: float = DEFAULT_NO_REGRESSION_THRESHOLD,
) -> WeightSweepReport:
    if not candidates:
        raise ValueError("At least one candidate source_weight_map is required")

    database_url = _direct_database_url()
    corpus = load_corpus(corpus_path)
    golden_set = load_golden_set(golden_path, corpus=corpus)

    counts = fetch_source_type_counts(database_url)
    expected_source_types = _expected_source_types(golden_set)
    validate_source_type_counts(counts, expected_source_types=expected_source_types)

    baseline_report = _run_db_eval(corpus, golden_set, source_weight_map=None)
    baseline = _candidate_result_from_report(
        "baseline",
        {},
        baseline_report,
        baseline=None,
        threshold=threshold,
    )

    candidate_results: list[SweepCandidateResult] = []
    for candidate in candidates:
        report = _run_db_eval(
            corpus,
            golden_set,
            source_weight_map=candidate.source_weight_map,
        )
        candidate_results.append(
            _candidate_result_from_report(
                candidate.name,
                candidate.source_weight_map,
                report,
                baseline=baseline_report,
                threshold=threshold,
            )
        )

    return WeightSweepReport(
        corpus_path=str(corpus_path),
        golden_set_path=str(golden_path),
        no_regression_threshold=threshold,
        source_type_counts=counts,
        expected_source_types=expected_source_types,
        baseline=baseline,
        candidates=candidate_results,
    )


def write_json_report(report: WeightSweepReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.model_dump_json(indent=2), encoding="utf-8")


def write_markdown_report(report: WeightSweepReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "# Retrieval Weight Sweep",
        "",
        f"- Corpus: `{report.corpus_path}`",
        f"- Golden set: `{report.golden_set_path}`",
        f"- Generated: `{report.generated_at}`",
        f"- No-regression threshold: `{report.no_regression_threshold:.4f}`",
        "",
        "## Source-Type Counts",
        "",
        "| Source Type | Searchable | Embedded |",
        "|---|---:|---:|",
    ]
    for row in report.source_type_counts:
        lines.append(
            f"| {row.source_type} | {row.searchable_count} | {row.embedding_count} |"
        )

    def append_candidate_block(candidate: SweepCandidateResult, *, title: str) -> None:
        lines.extend(
            [
                "",
                title,
                "",
                f"- name: `{candidate.name}`",
                f"- source_weight_map: `{json.dumps(candidate.source_weight_map, sort_keys=True)}`",
                f"- no_regression_passed: `{candidate.no_regression_passed}`",
                "",
                "### Overall",
                "",
                f"- recall@1: `{float(candidate.overall.get('recall@1', 0.0)):.4f}`",
                f"- recall@5: `{float(candidate.overall.get('recall@5', 0.0)):.4f}`",
                f"- recall@10: `{float(candidate.overall.get('recall@10', 0.0)):.4f}`",
                f"- mrr: `{float(candidate.overall.get('mrr', 0.0)):.4f}`",
                "",
                "### By Source Type",
                "",
                "| Source Type | Recall@10 | MRR | n |",
                "|---|---:|---:|---:|",
            ]
        )
        for source_type in sorted(candidate.by_source_type):
            metrics = candidate.by_source_type[source_type]
            lines.append(
                f"| {source_type} | {float(metrics.get('recall@10', 0.0)):.4f} | "
                f"{float(metrics.get('mrr', 0.0)):.4f} | {int(metrics.get('n', 0))} |"
            )

        lines.extend(
            [
                "",
                "### By Intent",
                "",
                "| Intent | Recall@10 | MRR | n |",
                "|---|---:|---:|---:|",
            ]
        )
        for intent in sorted(candidate.by_intent):
            metrics = candidate.by_intent[intent]
            lines.append(
                f"| {intent} | {float(metrics.get('recall@10', 0.0)):.4f} | "
                f"{float(metrics.get('mrr', 0.0)):.4f} | {int(metrics.get('n', 0))} |"
            )

        if candidate.intent_guard:
            lines.extend(
                [
                    "",
                    "### Intent Guard",
                    "",
                    "| Intent | Baseline Recall@10 | Candidate Recall@10 | Baseline MRR | Candidate MRR | Passed |",
                    "|---|---:|---:|---:|---:|---|",
                ]
            )
            for guard in candidate.intent_guard:
                lines.append(
                    f"| {guard.intent} | {guard.baseline_recall_at_10:.4f} | "
                    f"{guard.candidate_recall_at_10:.4f} | {guard.baseline_mrr:.4f} | "
                    f"{guard.candidate_mrr:.4f} | {guard.passed} |"
                )
                for failure in guard.failures:
                    lines.append(f"|  |  |  |  |  | failure: {failure} |")

    append_candidate_block(report.baseline, title="## Baseline")
    for candidate in report.candidates:
        append_candidate_block(candidate, title=f"## Candidate: {candidate.name}")

    path.write_text("\n".join(lines), encoding="utf-8")


def _parse_candidate_arg(raw: str) -> SweepCandidateSpec:
    if "=" not in raw:
        raise ValueError(
            "candidate must use NAME=JSON syntax, e.g. notes_bias={\"conversation_note\":1.2}"
        )
    name, payload = raw.split("=", 1)
    name = name.strip()
    if not name:
        raise ValueError("candidate name must be non-empty")
    parsed = json.loads(payload)
    if not isinstance(parsed, dict):
        raise ValueError("candidate JSON must decode to an object")
    normalized = {str(source_type): float(weight) for source_type, weight in parsed.items()}
    return SweepCandidateSpec(name=name, source_weight_map=normalized)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sweep candidate retrieval source-weight maps with the DB-backed eval adapter."
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
        help=(
            "Path to golden YAML (default: eval/retrieval/real_golden_set.yaml "
            "when present, else eval/retrieval/golden_set.yaml)."
        ),
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Directory for output reports (default: eval/retrieval/reports/).",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_NO_REGRESSION_THRESHOLD,
        help="Allowed absolute regression delta for know_about/exact_said recall@10 and MRR.",
    )
    parser.add_argument(
        "--candidate",
        action="append",
        default=[],
        help='Candidate override in NAME=JSON form, e.g. tune1={"theme":0.25}. Repeatable.',
    )
    return parser


def main(argv: list[str] | None = None) -> WeightSweepReport:
    parser = _build_parser()
    args = parser.parse_args(argv)

    corpus_path = args.corpus or _default_path("eval/retrieval/corpus.yaml")
    golden_path = args.golden or _default_golden_path()
    out_dir = args.out_dir or _default_path("eval/retrieval/reports")

    try:
        candidates = [_parse_candidate_arg(raw) for raw in args.candidate]
        report = run_weight_sweep(
            corpus_path=corpus_path,
            golden_path=golden_path,
            candidates=candidates,
            threshold=float(args.threshold),
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    json_path = out_dir / "weight_sweep_report.json"
    md_path = out_dir / "weight_sweep_report.md"
    write_json_report(report, json_path)
    write_markdown_report(report, md_path)

    print(f"Reports written to {out_dir.resolve()}")
    print(f"  JSON: {json_path.name}")
    print(f"  MD:   {md_path.name}")
    return report


if __name__ == "__main__":
    main()
