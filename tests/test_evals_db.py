from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest

from evals.db import EvalDatabaseSafetyError, apply_migrations, ensure_safe_database_url, scratch_schema
from evals.results import create_eval_run, list_eval_results, list_eval_runs, record_eval_result, update_eval_run_summary
from tests.conftest import FakePool


class EvalFakePool(FakePool):
    def __init__(self) -> None:
        super().__init__()
        self.ddl: list[str] = []

    async def execute(self, sql: str, *args):
        compact = " ".join(sql.split())
        if (
            compact.startswith("CREATE SCHEMA")
            or compact.startswith("DROP SCHEMA")
            or compact.startswith("SET search_path")
            or compact.startswith("CREATE TABLE")
        ):
            self.ddl.append(compact)
            return "OK"
        return await super().execute(sql, *args)


def test_database_safety_guard_rejects_production_like_urls() -> None:
    ensure_safe_database_url("postgresql://postgres:postgres@localhost:5432/mediator")
    ensure_safe_database_url("postgresql://user:pass@example.com:5432/mediator_eval")

    with pytest.raises(EvalDatabaseSafetyError, match="production-like managed database host"):
        ensure_safe_database_url("postgresql://postgres:secret@db.project.supabase.co:5432/postgres")

    with pytest.raises(EvalDatabaseSafetyError, match="prod/production"):
        ensure_safe_database_url("postgresql://postgres:secret@db.internal:5432/mediator_production")

    ensure_safe_database_url(
        "postgresql://postgres:secret@db.project.supabase.co:5432/postgres",
        allow_production=True,
    )


@pytest.mark.anyio
async def test_scratch_schema_applies_migrations_and_cleans_up(tmp_path) -> None:
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / "0001.sql").write_text("CREATE TABLE users (id uuid);", encoding="utf-8")

    pool = EvalFakePool()
    async with scratch_schema(pool, schema="eval_test", migrations_dir=migrations_dir) as scratch:
        assert scratch.schema == "eval_test"

    assert pool.ddl[0] == 'CREATE SCHEMA "eval_test"'
    assert 'SET search_path TO "eval_test", public' in pool.ddl
    assert "DROP SCHEMA IF EXISTS \"eval_test\" CASCADE" in pool.ddl


@pytest.mark.anyio
async def test_scratch_schema_can_be_kept_for_debugging(tmp_path) -> None:
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()

    pool = EvalFakePool()
    async with scratch_schema(pool, schema="eval_keep", migrations_dir=migrations_dir, keep_db=True):
        pass

    assert 'CREATE SCHEMA "eval_keep"' in pool.ddl
    assert "DROP SCHEMA IF EXISTS \"eval_keep\" CASCADE" not in pool.ddl


@pytest.mark.anyio
async def test_scratch_migrations_skip_durable_eval_result_tables(tmp_path) -> None:
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / "0001.sql").write_text("CREATE TABLE users (id uuid);", encoding="utf-8")
    (migrations_dir / "0006_plan7_eval_results.sql").write_text("CREATE TABLE public.eval_runs (id uuid);", encoding="utf-8")
    (migrations_dir / "0007_feature.down.sql").write_text("DROP TABLE users;", encoding="utf-8")

    pool = EvalFakePool()
    await apply_migrations(pool, "eval_skip_durable", migrations_dir=migrations_dir)

    assert any("CREATE TABLE users" in statement for statement in pool.ddl)
    assert not any("public.eval_runs" in statement for statement in pool.ddl)
    assert not any("DROP TABLE users" in statement for statement in pool.ddl)


@pytest.mark.anyio
async def test_eval_results_are_public_and_survive_scratch_cleanup(tmp_path) -> None:
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    pool = EvalFakePool()

    async with scratch_schema(pool, schema="eval_results_scratch", migrations_dir=migrations_dir):
        run_id = await create_eval_run(
            pool,
            prompt_version="v1",
            scenarios_passed=1,
            scenarios_failed=0,
            total_cost_usd=Decimal("0.12"),
            git_sha="abc123",
            notes="initial",
        )
        result_id = await record_eval_result(
            pool,
            run_id=run_id,
            scenario_name="worked-example-replay",
            status="pass",
            judge_verdicts=[{"passes": True, "reason": "ok"}],
            tool_calls=[{"tool_name": "get_observations"}],
        )
        await update_eval_run_summary(
            pool,
            run_id,
            scenarios_passed=2,
            scenarios_failed=1,
            total_cost_usd=Decimal("0.34"),
            notes="updated",
        )

    runs = await list_eval_runs(pool)
    results = await list_eval_results(pool, run_id)

    assert isinstance(run_id, UUID)
    assert isinstance(result_id, UUID)
    assert len(runs) == 1
    assert runs[0].prompt_version == "v1"
    assert runs[0].scenarios_passed == 2
    assert runs[0].scenarios_failed == 1
    assert runs[0].total_cost_usd == Decimal("0.34")
    assert runs[0].notes == "updated"
    assert len(results) == 1
    assert results[0].scenario_name == "worked-example-replay"
    assert results[0].judge_verdicts == [{"passes": True, "reason": "ok"}]
    assert results[0].tool_calls == [{"tool_name": "get_observations"}]
    assert "DROP SCHEMA IF EXISTS \"eval_results_scratch\" CASCADE" in pool.ddl
