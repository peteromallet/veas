from __future__ import annotations

from pathlib import Path

import yaml


WORKFLOW_PATH = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "evals.yml"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


def test_eval_workflow_triggers_on_prompt_eval_sensitive_paths() -> None:
    workflow = _workflow()
    triggers = workflow.get("on") or workflow.get(True)
    pull_request_paths = set(triggers["pull_request"]["paths"])

    assert "app/services/prompts.py" in pull_request_paths
    assert "app/services/oob_check.py" in pull_request_paths
    assert "app/services/scoring.py" in pull_request_paths
    assert "app/services/charge.py" in pull_request_paths
    assert "app/services/tools/**" in pull_request_paths
    assert "tool_schemas.py" in pull_request_paths
    assert "evals/**" in pull_request_paths
    assert ".github/workflows/evals.yml" in pull_request_paths


def test_eval_workflow_runs_parser_units_and_secret_gated_live_evals() -> None:
    workflow = _workflow()
    job = workflow["jobs"]["evals"]
    steps = job["steps"]
    step_by_name = {step["name"]: step for step in steps}

    assert job["services"]["postgres"]["image"] == "postgres:16"
    assert job["env"]["EVAL_DATABASE_URL"].startswith("postgresql://postgres:postgres@localhost")

    parser_step = step_by_name["Parser and unit checks"]
    assert "python -m evals --help" in parser_step["run"]
    assert "tests/test_evals_scenario.py" in parser_step["run"]
    assert "tests/test_evals_assertions.py" in parser_step["run"]
    assert "tests/test_evals_db.py" in parser_step["run"]
    assert "tests/test_evals_capture.py" in parser_step["run"]
    assert "tests/test_evals_judge.py" in parser_step["run"]
    assert "tests/test_charge.py" in parser_step["run"]
    assert "tests/test_oob_check.py" in parser_step["run"]

    detector = step_by_name["Detect live eval secrets"]
    assert "secrets.ANTHROPIC_API_KEY" in detector["env"]["LIVE_ANTHROPIC_API_KEY"]
    assert "run_live=false" in detector["run"]
    assert "neutral coverage" in detector["run"]

    live_step = step_by_name["Live eval suite"]
    assert live_step["if"] == "steps.live-eval-secrets.outputs.run_live == 'true'"
    assert "python -m evals run --cost-cap-usd 5" in live_step["run"]
    assert "secrets.ANTHROPIC_API_KEY" in live_step["env"]["ANTHROPIC_API_KEY"]

    skipped_step = step_by_name["Missing live eval secrets are neutral"]
    assert skipped_step["if"] == "steps.live-eval-secrets.outputs.run_live != 'true'"
    assert "scenario regressions were not evaluated" in skipped_step["run"]
