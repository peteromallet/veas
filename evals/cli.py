from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Sequence

from app.db import db_lifespan
from .db import eval_database
from .replay import replay_history
from .results import create_eval_run
from .runner import format_report, run_scenarios
from .scenario import ScenarioError, load_scenarios


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m evals",
        description="Run mediator-bot prompt eval scenarios.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="Run scripted eval scenarios.")
    run.add_argument("--scenario", help="Run only the named scenario.")
    run.add_argument("--tag", help="Run scenarios with this tag.")
    run.add_argument("--prompt-version", default="v1", help="Prompt version to evaluate.")
    run.add_argument("--cost-cap-usd", default="5", help="Maximum eval cost for the run.")
    run.add_argument("--database-url", help="Non-production eval database URL. Defaults to EVAL_DATABASE_URL/DATABASE_URL.")
    run.add_argument("--keep-db", action="store_true", help="Keep the scratch schema after the run for debugging.")
    run.add_argument(
        "--scenarios-dir",
        type=Path,
        default=Path(__file__).with_name("scenarios"),
        help="Directory containing markdown scenario files.",
    )
    run.set_defaults(func=_run)

    replay = subparsers.add_parser("replay", help="Replay historical messages in eval mode.")
    replay.add_argument("--since", required=True, help="Inclusive start date or timestamp.")
    replay.add_argument("--user", required=True, help="User id to replay.")
    replay.add_argument("--prompt-version", default="v1", help="Prompt version to evaluate.")
    replay.add_argument("--database-url", help="Non-production eval database URL. Defaults to EVAL_DATABASE_URL/DATABASE_URL.")
    replay.add_argument("--keep-db", action="store_true", help="Keep the scratch schema after replay for debugging.")
    replay.set_defaults(func=_replay)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ScenarioError as exc:
        parser.exit(2, f"evals: error: {exc}\n")


def _run(args: argparse.Namespace) -> int:
    scenarios = load_scenarios(args.scenarios_dir, scenario_name=args.scenario, tag=args.tag)
    if not scenarios:
        print(f"Loaded 0 scenario(s) for prompt version {args.prompt_version}.")
        return 0
    return asyncio.run(_run_async(args, scenarios))


async def _run_async(args: argparse.Namespace, scenarios: list) -> int:
    async with eval_database(args.database_url, keep_db=args.keep_db) as scratch:
        run_id = await create_eval_run(scratch.pool, prompt_version=args.prompt_version)
        report = await run_scenarios(
            scratch.pool,
            scenarios,
            prompt_version=args.prompt_version,
            cost_cap_usd=args.cost_cap_usd,
            result_pool=scratch.pool,
            run_id=run_id,
        )
    print(format_report(report))
    return 0 if report.passed else 1


def _replay(args: argparse.Namespace) -> int:
    asyncio.run(_replay_async(args))
    return 0


async def _replay_async(args: argparse.Namespace) -> None:
    app = _App(_State())
    async with db_lifespan(app):
        async with eval_database(args.database_url, keep_db=args.keep_db) as scratch:
            await replay_history(
                app.state.pool,
                scratch.pool,
                since=args.since,
                user_id=args.user,
                prompt_version=args.prompt_version,
                output=sys.stdout,
            )


class _State:
    pool = None


class _App:
    def __init__(self, state: _State) -> None:
        self.state = state
