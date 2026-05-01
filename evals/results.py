from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID


EvalStatus = Literal["pass", "fail", "skipped"]


@dataclass(frozen=True)
class EvalRun:
    id: UUID
    run_at: datetime
    prompt_version: str
    scenarios_passed: int
    scenarios_failed: int
    total_cost_usd: Decimal
    git_sha: str | None
    notes: str | None


@dataclass(frozen=True)
class EvalResult:
    id: UUID
    run_id: UUID
    scenario_name: str
    status: EvalStatus
    judge_verdicts: list[dict[str, Any]]
    tool_calls: list[dict[str, Any]]
    failure_reason: str | None


async def create_eval_run(
    pool: Any,
    *,
    prompt_version: str,
    scenarios_passed: int = 0,
    scenarios_failed: int = 0,
    total_cost_usd: Decimal | float | str = Decimal("0"),
    git_sha: str | None = None,
    notes: str | None = None,
) -> UUID:
    row = await pool.fetchrow(
        """
        INSERT INTO public.eval_runs (
            prompt_version, scenarios_passed, scenarios_failed, total_cost_usd, git_sha, notes
        )
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING id
        """,
        prompt_version,
        scenarios_passed,
        scenarios_failed,
        Decimal(str(total_cost_usd)),
        git_sha,
        notes,
    )
    return row["id"]


async def update_eval_run_summary(
    pool: Any,
    run_id: UUID,
    *,
    scenarios_passed: int,
    scenarios_failed: int,
    total_cost_usd: Decimal | float | str,
    notes: str | None = None,
) -> None:
    await pool.execute(
        """
        UPDATE public.eval_runs
        SET scenarios_passed = $1,
            scenarios_failed = $2,
            total_cost_usd = $3,
            notes = COALESCE($4, notes)
        WHERE id = $5
        """,
        scenarios_passed,
        scenarios_failed,
        Decimal(str(total_cost_usd)),
        notes,
        run_id,
    )


async def record_eval_result(
    pool: Any,
    *,
    run_id: UUID,
    scenario_name: str,
    status: EvalStatus,
    judge_verdicts: list[dict[str, Any]] | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    failure_reason: str | None = None,
) -> UUID:
    if status not in {"pass", "fail", "skipped"}:
        raise ValueError("status must be one of: pass, fail, skipped")
    row = await pool.fetchrow(
        """
        INSERT INTO public.eval_results (
            run_id, scenario_name, status, judge_verdicts, tool_calls, failure_reason
        )
        VALUES ($1, $2, $3, $4::jsonb, $5::jsonb, $6)
        RETURNING id
        """,
        run_id,
        scenario_name,
        status,
        json.dumps(judge_verdicts or []),
        json.dumps(tool_calls or []),
        failure_reason,
    )
    return row["id"]


async def list_eval_runs(pool: Any, *, limit: int = 25) -> list[EvalRun]:
    rows = await pool.fetch(
        """
        SELECT id, run_at, prompt_version, scenarios_passed, scenarios_failed,
               total_cost_usd, git_sha, notes
        FROM public.eval_runs
        ORDER BY run_at DESC
        LIMIT $1
        """,
        limit,
    )
    return [_eval_run_from_row(row) for row in rows]


async def list_eval_results(pool: Any, run_id: UUID) -> list[EvalResult]:
    rows = await pool.fetch(
        """
        SELECT id, run_id, scenario_name, status, judge_verdicts, tool_calls, failure_reason
        FROM public.eval_results
        WHERE run_id = $1
        ORDER BY scenario_name ASC
        """,
        run_id,
    )
    return [_eval_result_from_row(row) for row in rows]


def _eval_run_from_row(row: Any) -> EvalRun:
    return EvalRun(
        id=row["id"],
        run_at=row["run_at"],
        prompt_version=row["prompt_version"],
        scenarios_passed=row["scenarios_passed"],
        scenarios_failed=row["scenarios_failed"],
        total_cost_usd=Decimal(str(row["total_cost_usd"])),
        git_sha=row.get("git_sha") if hasattr(row, "get") else row["git_sha"],
        notes=row.get("notes") if hasattr(row, "get") else row["notes"],
    )


def _eval_result_from_row(row: Any) -> EvalResult:
    return EvalResult(
        id=row["id"],
        run_id=row["run_id"],
        scenario_name=row["scenario_name"],
        status=row["status"],
        judge_verdicts=_jsonb_list(row["judge_verdicts"]),
        tool_calls=_jsonb_list(row["tool_calls"]),
        failure_reason=row.get("failure_reason") if hasattr(row, "get") else row["failure_reason"],
    )


def _jsonb_list(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, str):
        loaded = json.loads(value)
    else:
        loaded = value
    if not isinstance(loaded, list):
        raise ValueError("jsonb value must decode to a list")
    return loaded
