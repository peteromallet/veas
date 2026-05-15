"""Migration tests for the live-voice schema (0042).

Two layers:
1. Static text checks against the migration SQL (always run; no DB needed).
2. Live DB checks that apply the migration in a scratch schema and verify
   FORCE RLS + deny_anon policy + owner-scoped policy on every new table,
   plus the partner_user_id XOR partner_label CHECK. Skipped when
   DATABASE_URL is not set, following the convention in
   tests/test_pregnancy_migration.py.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

LIVE_TABLES = (
    "conversations",
    "conversation_items",
    "transcript_turns",
    "conversation_notes",
    "item_visits",
    "conversation_consent_events",
    "conversation_speakers",
)


def _read_up() -> str:
    return (MIGRATIONS_DIR / "0042_live_conversations.sql").read_text()


def _read_down() -> str:
    return (MIGRATIONS_DIR / "0042_live_conversations.down.sql").read_text()


class TestMigrationFilesExist:
    def test_up_present(self) -> None:
        assert (MIGRATIONS_DIR / "0042_live_conversations.sql").exists()

    def test_down_present(self) -> None:
        assert (MIGRATIONS_DIR / "0042_live_conversations.down.sql").exists()


class TestMigrationContent:
    def test_creates_all_seven_tables_in_mediator(self) -> None:
        sql = _read_up()
        for table in LIVE_TABLES:
            assert f"CREATE TABLE mediator.{table}" in sql, f"missing CREATE for {table}"

    def test_partner_xor_check(self) -> None:
        sql = _read_up()
        assert "conversations_partner_xor" in sql
        assert "partner_user_id IS NULL OR partner_label IS NULL" in sql

    def test_force_rls_on_every_table(self) -> None:
        sql = _read_up()
        for table in LIVE_TABLES:
            assert f"ALTER TABLE mediator.{table} ENABLE ROW LEVEL SECURITY" in sql
            assert f"ALTER TABLE mediator.{table} FORCE ROW LEVEL SECURITY" in sql
            assert f"REVOKE ALL ON TABLE mediator.{table} FROM anon" in sql

    def test_deny_anon_policy_on_every_table(self) -> None:
        sql = _read_up()
        for table in LIVE_TABLES:
            assert f"deny_anon_{table}" in sql, f"missing deny_anon policy for {table}"

    def test_owner_scoped_policy_on_every_table(self) -> None:
        sql = _read_up()
        for table in LIVE_TABLES:
            assert f"owner_scoped_{table}" in sql, f"missing owner_scoped policy for {table}"

    def test_status_enum_includes_required_states(self) -> None:
        sql = _read_up()
        for state in (
            "prepping", "ready", "live", "ended",
            "synthesizing", "review_pending", "synthesized",
            "discarded", "failed",
        ):
            assert f"'{state}'" in sql

    def test_down_reverses_cleanly(self) -> None:
        sql = _read_down()
        for table in LIVE_TABLES:
            assert (
                f"DROP TABLE IF EXISTS mediator.{table}" in sql
                or f"DROP TABLE mediator.{table}" in sql
            ), f"down migration must drop mediator.{table}"


# --------------------------------------------------------------------------- #
# Live DB checks — apply 0042 in a scratch schema and verify the surface.
# --------------------------------------------------------------------------- #


class TestMigrationDatabase:
    """Apply 0042 to a scratch schema and verify RLS surface."""

    @pytest.fixture(autouse=True)
    def _check_db_url(self) -> None:
        if not os.environ.get("DATABASE_URL") and not os.environ.get("EVAL_DATABASE_URL"):
            pytest.skip(
                "DATABASE_URL / EVAL_DATABASE_URL not set — skipping live migration test"
            )

    @pytest.mark.anyio
    async def test_apply_in_scratch_schema_and_check_rls(self) -> None:
        """Apply all migrations to a fresh schema; assert RLS + policies + CHECK."""
        from evals.db import create_eval_pool, scratch_schema

        pool = await create_eval_pool()
        try:
            async with scratch_schema(pool, schema="eval_live_voice") as scratch:
                # search_path is set to the scratch schema; mediator.X resolves
                # to the scratch's "mediator" namespace because migration 0042
                # qualifies tables as `mediator.X` and the eval harness applies
                # against a single schema. To verify RLS we instead probe the
                # pg_catalog using the schema name we set.
                schema = scratch.schema
                async with pool.acquire() as conn:
                    rows = await conn.fetch(
                        """
                        SELECT c.relname AS table_name,
                               c.relrowsecurity AS rls_enabled,
                               c.relforcerowsecurity AS rls_forced
                        FROM pg_class c
                        JOIN pg_namespace n ON n.oid = c.relnamespace
                        WHERE n.nspname = $1
                          AND c.relkind = 'r'
                          AND c.relname = ANY($2::text[])
                        """,
                        schema,
                        list(LIVE_TABLES),
                    )

                    by_name = {r["table_name"]: r for r in rows}
                    missing = [t for t in LIVE_TABLES if t not in by_name]
                    assert not missing, f"tables not created in scratch: {missing}"
                    for table in LIVE_TABLES:
                        row = by_name[table]
                        assert row["rls_enabled"], f"RLS not enabled on {table}"
                        assert row["rls_forced"], f"RLS not forced on {table}"

                    # Every new table must have the deny_anon catch-all.
                    policy_rows = await conn.fetch(
                        """
                        SELECT tablename, policyname
                        FROM pg_policies
                        WHERE schemaname = $1
                          AND tablename = ANY($2::text[])
                        """,
                        schema,
                        list(LIVE_TABLES),
                    )
                    by_table: dict[str, set[str]] = {}
                    for r in policy_rows:
                        by_table.setdefault(r["tablename"], set()).add(r["policyname"])
                    for table in LIVE_TABLES:
                        policies = by_table.get(table, set())
                        assert f"deny_anon_{table}" in policies, (
                            f"missing deny_anon policy on {table}; have={policies}"
                        )
                        assert f"owner_scoped_{table}" in policies, (
                            f"missing owner_scoped policy on {table}; have={policies}"
                        )

                    # partner_user_id XOR partner_label CHECK is wired.
                    check = await conn.fetchval(
                        """
                        SELECT 1
                        FROM pg_constraint c
                        JOIN pg_class t ON t.oid = c.conrelid
                        JOIN pg_namespace n ON n.oid = t.relnamespace
                        WHERE n.nspname = $1
                          AND t.relname = 'conversations'
                          AND c.conname = 'conversations_partner_xor'
                        """,
                        schema,
                    )
                    assert check == 1, "conversations_partner_xor CHECK constraint not found"
        finally:
            await pool.close()
