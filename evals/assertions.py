from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from evals.factories import ScenarioCapture
from evals.scenario import PrimitiveWriteExpectation
from evals.state import TableDiff


KIND_TO_TABLE = {
    "memory": "memories",
    "theme": "themes",
    "watch_item": "watch_items",
    "observation": "observations",
    "style_note": "users",
    "oob_entry": "out_of_bounds",
}


@dataclass(frozen=True)
class ExpectationFailure:
    category: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def compact(self) -> str:
        if not self.details:
            return f"{self.category}: {self.message}"
        return f"{self.category}: {self.message} ({self.details})"


@dataclass(frozen=True)
class AssertionReport:
    failures: list[ExpectationFailure]

    @property
    def passed(self) -> bool:
        return not self.failures

    @property
    def failure_reason(self) -> str | None:
        if self.passed:
            return None
        return "; ".join(failure.compact() for failure in self.failures)


def check_expectations(
    capture: ScenarioCapture,
    *,
    judge_verdicts: list[dict[str, Any]] | None = None,
    cost_cap_usd: Decimal | float | str | None = None,
) -> AssertionReport:
    failures: list[ExpectationFailure] = []
    expectations = capture.scenario.expectations
    failures.extend(_check_tools(capture, expectations.must_call_tools, expectations.must_not_call_tools))
    for expected_write in expectations.must_write_primitives:
        failures.extend(_check_primitive_write(capture, expected_write))
    failures.extend(_check_oob(capture))
    failures.extend(_check_charge(capture))
    failures.extend(_check_judge_verdicts(judge_verdicts or []))
    if cost_cap_usd is not None:
        failures.extend(_check_cost(capture, Decimal(str(cost_cap_usd))))
    return AssertionReport(failures=failures)


def _check_tools(capture: ScenarioCapture, must_call: list[str], must_not_call: list[str]) -> list[ExpectationFailure]:
    called = [str(call.get("tool_name") or call.get("name") or "") for call in capture.execution.tool_calls]
    failures: list[ExpectationFailure] = []
    missing = [tool for tool in must_call if tool not in called]
    if missing:
        failures.append(
            ExpectationFailure(
                "tools",
                "required tools were not called",
                {"missing": missing, "called": called},
            )
        )
    forbidden = [tool for tool in must_not_call if tool in called]
    if forbidden:
        failures.append(
            ExpectationFailure(
                "tools",
                "forbidden tools were called",
                {"forbidden": forbidden, "called": called},
            )
        )
    return failures


def _check_primitive_write(capture: ScenarioCapture, expected: PrimitiveWriteExpectation) -> list[ExpectationFailure]:
    table_name = KIND_TO_TABLE[expected.kind]
    diff = capture.diff.tables.get(table_name, TableDiff())
    candidates = _operation_rows(diff, expected.operation)
    matches = [row for row in candidates if _matches_row(row, expected)]
    required_count = expected.count
    if required_count is None:
        if matches:
            return []
        return [
            ExpectationFailure(
                "writes",
                f"expected {expected.kind} write was not found",
                {
                    "kind": expected.kind,
                    "operation": expected.operation,
                    "content_matches": expected.content_matches,
                    "available": _summarize_rows(candidates),
                },
            )
        ]
    if len(matches) != required_count:
        return [
            ExpectationFailure(
                "writes",
                f"expected {required_count} matching {expected.kind} write(s), found {len(matches)}",
                {
                    "kind": expected.kind,
                    "operation": expected.operation,
                    "content_matches": expected.content_matches,
                    "available": _summarize_rows(candidates),
                },
            )
        ]
    return []


def _operation_rows(diff: TableDiff, operation: str | None) -> list[dict[str, Any]]:
    if operation == "insert":
        return diff.inserted
    if operation == "update":
        return diff.updated
    if operation == "supersede":
        return [
            row
            for row in [*diff.inserted, *diff.updated, *diff.deleted]
            if row.get("status") == "superseded" or row.get("supersedes_memory_id")
        ]
    return [*diff.inserted, *diff.updated, *diff.deleted]


def _matches_row(row: dict[str, Any], expected: PrimitiveWriteExpectation) -> bool:
    if expected.status is not None and row.get("status") != expected.status:
        return False
    significance = row.get("significance")
    if expected.significance_min is not None and not _score_at_least(significance, expected.significance_min):
        return False
    if expected.significance_max is not None and not _score_at_most(significance, expected.significance_max):
        return False
    if expected.content_matches is not None:
        text = _searchable_text(row)
        if re.search(expected.content_matches, text, re.IGNORECASE) is None:
            return False
    return True


def _searchable_text(row: dict[str, Any]) -> str:
    fields = ("content", "title", "description", "sensitive_core", "shareable_context", "style_notes", "addressing_note")
    return "\n".join(str(row.get(field) or "") for field in fields)


def _score_at_least(value: Any, threshold: int) -> bool:
    try:
        return int(value) >= threshold
    except (TypeError, ValueError):
        return False


def _score_at_most(value: Any, threshold: int) -> bool:
    try:
        return int(value) <= threshold
    except (TypeError, ValueError):
        return False


def _check_oob(capture: ScenarioCapture) -> list[ExpectationFailure]:
    expected = capture.scenario.expectations.expected_oob
    if expected is None and capture.scenario.expectations.must_pass_oob is not None:
        expected = "pass" if capture.scenario.expectations.must_pass_oob else "block"
    if expected is None:
        return []
    observed = _observed_oob(capture)
    if observed == expected:
        return []
    return [
        ExpectationFailure(
            "oob",
            "OOB outcome did not match",
            {
                "expected": expected,
                "observed": observed,
                "capture_oob_outcome": capture.oob_outcome,
                "check_oob_verdicts": [
                    _normalize_oob_verdict(record.verdict.get("verdict"))
                    for record in (capture.execution.oob_checks or [])
                ],
                "withheld_review_verdicts": [review.get("verdict") for review in capture.withheld_reviews],
            },
        )
    ]


def _observed_oob(capture: ScenarioCapture) -> str | None:
    verdicts = [_normalize_oob_verdict(record.verdict.get("verdict")) for record in (capture.execution.oob_checks or [])]
    review_verdicts = [_normalize_oob_verdict(review.get("verdict")) for review in capture.withheld_reviews]
    all_verdicts = [verdict for verdict in [*verdicts, *review_verdicts, _normalize_oob_verdict(capture.oob_outcome)] if verdict]
    if "block" in all_verdicts:
        return "block"
    if "rewrite" in all_verdicts:
        return "rewrite"
    if "pass" in all_verdicts:
        return "pass"
    return None


def _normalize_oob_verdict(value: Any) -> str | None:
    if value in {"pass", "ok"}:
        return "pass"
    if value in {"block", "rewrite"}:
        return str(value)
    return None


def _check_charge(capture: ScenarioCapture) -> list[ExpectationFailure]:
    expected = capture.scenario.expectations.expected_charge
    if expected is None:
        return []
    bad = {message_id: charge for message_id, charge in capture.classified_charges.items() if charge != expected}
    if not bad:
        return []
    return [
        ExpectationFailure(
            "charge",
            "classified charge did not match",
            {"expected": expected, "charges": capture.classified_charges},
        )
    ]


def _check_judge_verdicts(judge_verdicts: list[dict[str, Any]]) -> list[ExpectationFailure]:
    failures = []
    for index, verdict in enumerate(judge_verdicts):
        if verdict.get("passes") is False:
            failures.append(
                ExpectationFailure(
                    "judge",
                    "outbound assertion failed",
                    {
                        "index": index,
                        "criterion": verdict.get("criterion"),
                        "reason": verdict.get("reason"),
                    },
                )
            )
    return failures


def _check_cost(capture: ScenarioCapture, cap: Decimal) -> list[ExpectationFailure]:
    cost = Decimal(str(capture.cost_delta_usd))
    if cost <= cap:
        return []
    return [
        ExpectationFailure(
            "cost",
            "scenario exceeded cost cap",
            {"cost_delta_usd": str(cost), "cap_usd": str(cap)},
        )
    ]


def _summarize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary = []
    for row in rows[:5]:
        summary.append(
            {
                "id": row.get("id"),
                "content": row.get("content"),
                "title": row.get("title"),
                "status": row.get("status"),
                "significance": row.get("significance"),
            }
        )
    return summary
