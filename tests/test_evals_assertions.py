from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from app.models.user import User
from evals.assertions import check_expectations
from evals.execution import EvalTurnExecution, OobCheckRecord
from evals.factories import ScenarioCapture, ScenarioSeed
from evals.scenario import InboundMessage, PrimitiveWriteExpectation, Scenario, ScenarioExpectations
from evals.state import ScenarioSnapshot, StateDiff, TableDiff


def _capture(
    expectations: ScenarioExpectations,
    *,
    tool_calls: list[dict] | None = None,
    table_diffs: dict[str, TableDiff] | None = None,
    oob_checks: list[OobCheckRecord] | None = None,
    oob_outcome: str | None = None,
    withheld_reviews: list[dict] | None = None,
    charges: dict[str, str | None] | None = None,
    cost_delta_usd: str = "0",
) -> ScenarioCapture:
    user = User(uuid4(), "Maya", "15555550100", "UTC")
    partner = User(uuid4(), "Ben", "15555550101", "UTC")
    scenario = Scenario(
        name="assertion-smoke",
        description="Assertion smoke",
        tags=[],
        setup={},
        inbound=[InboundMessage("hello")],
        expectations=expectations,
        path=Path("assertion-smoke.md"),
    )
    return ScenarioCapture(
        scenario=scenario,
        seed=ScenarioSeed(user=user, partner=partner, inbound_message_ids=[uuid4()], refs={}),
        before=ScenarioSnapshot(),
        after=ScenarioSnapshot(),
        diff=StateDiff(tables=table_diffs or {}, cost_delta_usd=Decimal(cost_delta_usd)),
        execution=EvalTurnExecution(tool_calls=tool_calls or [], whatsapp_sends=[], oob_checks=oob_checks or []),
        outbound_text="",
        persisted_tool_calls=[],
        withheld_reviews=withheld_reviews or [],
        oob_outcome=oob_outcome,
        classified_charges=charges or {},
        cost_delta_usd=cost_delta_usd,
    )


def test_tool_expectations_report_missing_and_forbidden_tools() -> None:
    capture = _capture(
        ScenarioExpectations(must_call_tools=["get_observations"], must_not_call_tools=["log_observation"]),
        tool_calls=[{"tool_name": "log_observation"}],
    )

    report = check_expectations(capture)

    assert not report.passed
    assert "required tools were not called" in report.failure_reason
    assert "forbidden tools were called" in report.failure_reason


def test_tool_expectations_use_observer_transcript_not_persisted_write_rows() -> None:
    capture = _capture(
        ScenarioExpectations(must_call_tools=["get_observations"], must_not_call_tools=["log_observation"]),
        tool_calls=[{"tool_name": "get_observations", "phase": "read"}],
    )

    assert capture.persisted_tool_calls == []
    assert check_expectations(capture).passed


def test_primitive_write_matchers_cover_insert_update_regex_status_and_significance() -> None:
    capture = _capture(
        ScenarioExpectations(
            must_write_primitives=[
                PrimitiveWriteExpectation(
                    kind="observation",
                    operation="update",
                    content_matches="asked.*day",
                    significance_min=4,
                    status="active",
                ),
                PrimitiveWriteExpectation(kind="watch_item", operation="insert", content_matches="repair", count=1),
            ]
        ),
        table_diffs={
            "observations": TableDiff(
                updated=[
                    {
                        "id": "obs-1",
                        "content": "Maya notices when Ben asked about her day.",
                        "significance": 5,
                        "status": "active",
                    }
                ]
            ),
            "watch_items": TableDiff(
                inserted=[
                    {
                        "id": "watch-1",
                        "content": "Check whether the repair conversation happened.",
                        "status": "open",
                    }
                ]
            ),
        },
    )

    assert check_expectations(capture).passed


def test_primitive_write_failure_is_localized() -> None:
    capture = _capture(
        ScenarioExpectations(
            must_write_primitives=[
                PrimitiveWriteExpectation(kind="theme", operation="insert", content_matches="weekend planning")
            ]
        ),
        table_diffs={"themes": TableDiff(inserted=[])},
    )

    report = check_expectations(capture)

    assert not report.passed
    assert report.failures[0].category == "writes"
    assert "expected theme write was not found" in report.failures[0].message


def test_oob_expectations_use_checker_and_withheld_review_evidence() -> None:
    rewrite_capture = _capture(
        ScenarioExpectations(expected_oob="rewrite"),
        oob_checks=[OobCheckRecord("draft", "recipient", {"verdict": "rewrite"})],
        withheld_reviews=[{"verdict": "rewrite"}],
        oob_outcome="rewrite",
    )
    pass_capture = _capture(
        ScenarioExpectations(must_pass_oob=True),
        oob_checks=[OobCheckRecord("draft", "recipient", {"verdict": "ok"})],
        oob_outcome="pass",
    )

    assert check_expectations(rewrite_capture).passed
    assert check_expectations(pass_capture).passed


def test_charge_judge_and_cost_failures_are_distinct() -> None:
    capture = _capture(
        ScenarioExpectations(expected_charge="charged"),
        charges={"message-1": "routine"},
        cost_delta_usd="7",
    )

    report = check_expectations(
        capture,
        judge_verdicts=[{"criterion": "no clinical language", "passes": False, "reason": "uses avoidant"}],
        cost_cap_usd="5",
    )

    assert [failure.category for failure in report.failures] == ["charge", "judge", "cost"]
    assert "classified charge did not match" in report.failure_reason
    assert "outbound assertion failed" in report.failure_reason
    assert "scenario exceeded cost cap" in report.failure_reason
