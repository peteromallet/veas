from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from uuid import UUID

from evals.assertions import AssertionReport, ExpectationFailure, check_expectations
from evals.factories import ScenarioCapture, capture_scenario_turn
from evals.judge import judge_outbound_assertions
from evals.results import record_eval_result, update_eval_run_summary
from evals.scenario import Scenario


@dataclass(frozen=True)
class ScenarioRunResult:
    scenario_name: str
    status: str
    failure_reason: str | None
    cost_usd: Decimal
    assertion_report: AssertionReport | None = None
    capture: ScenarioCapture | None = None
    judge_verdicts: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class EvalRunReport:
    prompt_version: str
    results: list[ScenarioRunResult]
    total_cost_usd: Decimal

    @property
    def scenarios_passed(self) -> int:
        return sum(1 for result in self.results if result.status == "pass")

    @property
    def scenarios_failed(self) -> int:
        return sum(1 for result in self.results if result.status == "fail")

    @property
    def scenarios_skipped(self) -> int:
        return sum(1 for result in self.results if result.status == "skipped")

    @property
    def passed(self) -> bool:
        return self.scenarios_failed == 0


async def run_scenario(
    pool: Any,
    scenario: Scenario,
    *,
    prompt_version: str,
    cost_cap_usd: Decimal | float | str | None = None,
    judge_verdicts: list[dict[str, Any]] | None = None,
    judge_client: Any | None = None,
) -> ScenarioRunResult:
    capture = await capture_scenario_turn(pool, scenario, prompt_version=prompt_version)
    if judge_verdicts is None and scenario.expectations.outbound_assertions:
        judge_verdicts = await judge_outbound_assertions(
            pool,
            capture.outbound_text,
            scenario.expectations.outbound_assertions,
            client=judge_client,
        )
    report = check_expectations(capture, judge_verdicts=judge_verdicts, cost_cap_usd=cost_cap_usd)
    status = "pass" if report.passed else "fail"
    return ScenarioRunResult(
        scenario_name=scenario.name,
        status=status,
        failure_reason=report.failure_reason,
        cost_usd=Decimal(str(capture.cost_delta_usd)),
        assertion_report=report,
        capture=capture,
        judge_verdicts=judge_verdicts or [],
    )


async def run_scenarios(
    pool: Any,
    scenarios: list[Scenario],
    *,
    prompt_version: str,
    cost_cap_usd: Decimal | float | str = Decimal("5"),
    result_pool: Any | None = None,
    run_id: UUID | None = None,
    judge_client: Any | None = None,
) -> EvalRunReport:
    cap = Decimal(str(cost_cap_usd))
    total = Decimal("0")
    results: list[ScenarioRunResult] = []
    for scenario in scenarios:
        if total >= cap:
            results.append(
                ScenarioRunResult(
                    scenario_name=scenario.name,
                    status="skipped",
                    failure_reason=f"cost cap already reached before scenario: total={total} cap={cap}",
                    cost_usd=Decimal("0"),
                    assertion_report=AssertionReport(
                        [
                            ExpectationFailure(
                                "cost",
                                "scenario skipped because cost cap was already reached",
                                {"total_cost_usd": str(total), "cap_usd": str(cap)},
                            )
                        ]
                    ),
                )
            )
            continue
        result = await run_scenario(
            pool,
            scenario,
            prompt_version=prompt_version,
            cost_cap_usd=cap,
            judge_client=judge_client,
        )
        total += result.cost_usd
        results.append(result)
    report = EvalRunReport(prompt_version=prompt_version, results=results, total_cost_usd=total)
    if result_pool is not None and run_id is not None:
        await persist_run_report(result_pool, run_id, report)
    return report


async def persist_run_report(pool: Any, run_id: UUID, report: EvalRunReport) -> None:
    for result in report.results:
        await record_eval_result(
            pool,
            run_id=run_id,
            scenario_name=result.scenario_name,
            status=result.status,
            judge_verdicts=result.judge_verdicts,
            tool_calls=result.capture.execution.tool_calls if result.capture is not None else [],
            failure_reason=result.failure_reason,
        )
    await update_eval_run_summary(
        pool,
        run_id,
        scenarios_passed=report.scenarios_passed,
        scenarios_failed=report.scenarios_failed,
        total_cost_usd=report.total_cost_usd,
    )


def format_report(report: EvalRunReport) -> str:
    lines = [
        (
            f"Eval run prompt_version={report.prompt_version} "
            f"passed={report.scenarios_passed} failed={report.scenarios_failed} "
            f"skipped={report.scenarios_skipped} cost=${report.total_cost_usd}"
        )
    ]
    for result in report.results:
        line = f"{result.status.upper()} {result.scenario_name}"
        if result.failure_reason:
            line += f": {result.failure_reason}"
        lines.append(line)
    return "\n".join(lines)
