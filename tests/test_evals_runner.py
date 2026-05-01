from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from app.models.user import User
from evals.execution import EvalTurnExecution
from evals.factories import ScenarioCapture, ScenarioSeed
from evals.results import create_eval_run
from evals.runner import format_report, run_scenario, run_scenarios
from evals.scenario import InboundMessage, Scenario, ScenarioExpectations
from evals.state import ScenarioSnapshot, StateDiff

pytestmark = pytest.mark.anyio


def _scenario(name: str, expectations: ScenarioExpectations | None = None) -> Scenario:
    return Scenario(
        name=name,
        description=f"{name} scenario",
        tags=[],
        setup={},
        inbound=[InboundMessage("hello")],
        expectations=expectations or ScenarioExpectations(),
        path=Path(f"{name}.md"),
    )


def _capture(
    scenario: Scenario,
    *,
    cost: str = "0",
    tool_calls: list[dict] | None = None,
    outbound_text: str = "",
) -> ScenarioCapture:
    user = User(uuid4(), "Maya", "15555550100", "UTC")
    partner = User(uuid4(), "Ben", "15555550101", "UTC")
    return ScenarioCapture(
        scenario=scenario,
        seed=ScenarioSeed(user=user, partner=partner, inbound_message_ids=[uuid4()], refs={}),
        before=ScenarioSnapshot(),
        after=ScenarioSnapshot(),
        diff=StateDiff(tables={}, cost_delta_usd=Decimal(cost)),
        execution=EvalTurnExecution(tool_calls=tool_calls or [], whatsapp_sends=[], oob_checks=[]),
        outbound_text=outbound_text,
        persisted_tool_calls=[],
        withheld_reviews=[],
        oob_outcome=None,
        classified_charges={},
        cost_delta_usd=cost,
    )


async def test_run_scenario_assembles_pass_and_failure_details(monkeypatch, fake_pool) -> None:
    scenario = _scenario("needs-tool", ScenarioExpectations(must_call_tools=["get_observations"]))

    async def fake_capture(pool, scenario_arg, *, prompt_version):
        assert scenario_arg is scenario
        assert prompt_version == "v1"
        return _capture(scenario, tool_calls=[])

    monkeypatch.setattr("evals.runner.capture_scenario_turn", fake_capture)

    result = await run_scenario(fake_pool, scenario, prompt_version="v1")

    assert result.status == "fail"
    assert result.assertion_report is not None
    assert result.assertion_report.failures[0].category == "tools"
    assert "required tools were not called" in result.failure_reason


async def test_run_scenarios_stops_after_cost_cap_and_formats_report(monkeypatch, fake_pool) -> None:
    first = _scenario("first")
    second = _scenario("second")

    async def fake_capture(pool, scenario_arg, *, prompt_version):
        return _capture(scenario_arg, cost="1")

    monkeypatch.setattr("evals.runner.capture_scenario_turn", fake_capture)

    report = await run_scenarios(fake_pool, [first, second], prompt_version="v1", cost_cap_usd="1")

    assert [result.status for result in report.results] == ["pass", "skipped"]
    assert report.total_cost_usd == Decimal("1")
    assert report.scenarios_passed == 1
    assert report.scenarios_skipped == 1
    rendered = format_report(report)
    assert "PASS first" in rendered
    assert "SKIPPED second" in rendered
    assert "cost cap already reached" in rendered


async def test_run_scenarios_can_persist_results(monkeypatch, fake_pool) -> None:
    scenario = _scenario("persisted", ScenarioExpectations(must_not_call_tools=["log_observation"]))
    run_id = await create_eval_run(fake_pool, prompt_version="v1")

    async def fake_capture(pool, scenario_arg, *, prompt_version):
        return _capture(scenario_arg, tool_calls=[{"tool_name": "log_observation"}])

    monkeypatch.setattr("evals.runner.capture_scenario_turn", fake_capture)

    report = await run_scenarios(
        fake_pool,
        [scenario],
        prompt_version="v1",
        cost_cap_usd="5",
        result_pool=fake_pool,
        run_id=run_id,
    )

    assert report.scenarios_failed == 1
    assert fake_pool.eval_runs[run_id]["scenarios_failed"] == 1
    result_row = next(iter(fake_pool.eval_results.values()))
    assert result_row["scenario_name"] == "persisted"
    assert result_row["status"] == "fail"
    assert "forbidden tools were called" in result_row["failure_reason"]


async def test_run_scenario_judges_outbound_assertions(monkeypatch, fake_pool) -> None:
    scenario = _scenario(
        "judge-outbound",
        ScenarioExpectations(outbound_assertions=["does not use clinical labels"]),
    )

    async def fake_capture(pool, scenario_arg, *, prompt_version):
        return _capture(scenario_arg, outbound_text="She is avoidant and attached anxiously.")

    async def fake_judge(pool, outbound_text, criteria, *, client=None):
        assert outbound_text == "She is avoidant and attached anxiously."
        assert criteria == ["does not use clinical labels"]
        return [
            {
                "criterion": "does not use clinical labels",
                "passes": False,
                "reason": "uses clinical labels",
                "judge_prompt_version": "rubric_judge_v1",
                "cost_usd": "0.001",
            }
        ]

    monkeypatch.setattr("evals.runner.capture_scenario_turn", fake_capture)
    monkeypatch.setattr("evals.runner.judge_outbound_assertions", fake_judge)

    result = await run_scenario(fake_pool, scenario, prompt_version="v1")

    assert result.status == "fail"
    assert result.judge_verdicts[0]["passes"] is False
    assert result.assertion_report is not None
    assert result.assertion_report.failures[0].category == "judge"
