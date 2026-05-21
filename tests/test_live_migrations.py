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


# --------------------------------------------------------------------------- #
# Migration 0053 checks — live debrief statuses
# --------------------------------------------------------------------------- #


def _read_0053_up() -> str:
    return (MIGRATIONS_DIR / "0053_live_debrief_statuses.sql").read_text()


def _read_0053_down() -> str:
    return (MIGRATIONS_DIR / "0053_live_debrief_statuses.down.sql").read_text()


class TestMigration0053:
    def test_up_and_down_files_exist(self) -> None:
        assert (MIGRATIONS_DIR / "0053_live_debrief_statuses.sql").exists()
        assert (MIGRATIONS_DIR / "0053_live_debrief_statuses.down.sql").exists()

    def test_up_adds_debriefing_and_debrief_failed(self) -> None:
        sql = _read_0053_up()
        assert "'debriefing'" in sql, "up migration must add debriefing"
        assert "'debrief_failed'" in sql, "up migration must add debrief_failed"

    def test_up_creates_partial_index(self) -> None:
        sql = _read_0053_up()
        assert "idx_conversations_status_debrief_failed" in sql, (
            "up migration must create partial index for debrief_failed"
        )
        assert "WHERE status = 'debrief_failed'" in sql or (
            "status = 'debrief_failed'" in sql
        ), "partial index must filter on status = 'debrief_failed'"

    def test_down_restores_original_constraint(self) -> None:
        sql = _read_0053_down()
        assert "DROP INDEX" in sql or "drop index" in sql.lower(), (
            "down migration must drop the partial index"
        )
        # The down migration ADD CONSTRAINT block must NOT include
        # debriefing/debrief_failed. Extract only the ADD CONSTRAINT block.
        add_constraint_idx = sql.find("ADD CONSTRAINT conversations_status_check")
        if add_constraint_idx == -1:
            add_constraint_idx = sql.find("ADD CONSTRAINT")
        assert add_constraint_idx != -1, "down migration must have ADD CONSTRAINT"
        # Get everything from ADD CONSTRAINT to the next semicolon or end.
        constraint_block = sql[add_constraint_idx:]
        semicolon_idx = constraint_block.find(";")
        if semicolon_idx != -1:
            constraint_block = constraint_block[:semicolon_idx + 1]
        assert "'debriefing'" not in constraint_block, (
            f"down migration ADD CONSTRAINT must not include debriefing: {constraint_block[:200]}"
        )
        assert "'debrief_failed'" not in constraint_block, (
            f"down migration ADD CONSTRAINT must not include debrief_failed: {constraint_block[:200]}"
        )

    def test_debrief_statuses_not_in_active_session_indexes(self) -> None:
        """Confirm debriefing and debrief_failed are NOT in active-session
        indexes. The 0053 up migration should not modify idx_conversations_status_active
        or idx_conversations_spend_active."""
        sql = _read_0053_up()
        # Filter out comment lines to only check SQL statements.
        sql_lines = [
            line for line in sql.split("\n")
            if not line.strip().startswith("--")
        ]
        sql_body = "\n".join(sql_lines)
        assert "idx_conversations_status_active" not in sql_body, (
            "0053 must not modify active-session index"
        )
        assert "idx_conversations_spend_active" not in sql_body, (
            "0053 must not modify spend-active index"
        )


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


# --------------------------------------------------------------------------- #
# Migration 0055 checks — product statuses
# --------------------------------------------------------------------------- #


def _read_0055_up() -> str:
    return (MIGRATIONS_DIR / "0055_live_product_statuses.sql").read_text()


def _read_0055_down() -> str:
    return (MIGRATIONS_DIR / "0055_live_product_statuses.down.sql").read_text()


class TestMigration0055:
    """Static text checks for the 0055 additive product-status migration."""

    def test_up_and_down_files_exist(self) -> None:
        assert (MIGRATIONS_DIR / "0055_live_product_statuses.sql").exists()
        assert (MIGRATIONS_DIR / "0055_live_product_statuses.down.sql").exists()

    def test_up_adds_canonical_statuses(self) -> None:
        """Canonical statuses preparing, active, completed must appear in the
        CHECK constraint."""
        sql = _read_0055_up()
        for canonical in ("'preparing'", "'active'", "'completed'"):
            assert canonical in sql, (
                f"up migration must include canonical status {canonical}"
            )

    def test_up_retains_all_legacy_statuses(self) -> None:
        """Legacy statuses must NOT be removed from the CHECK constraint."""
        sql = _read_0055_up()
        # Extract the ADD CONSTRAINT block so we don't match comments/docs.
        add_idx = sql.find("ADD CONSTRAINT conversations_status_check")
        assert add_idx != -1, "up migration must have ADD CONSTRAINT"
        constraint_block = sql[add_idx:]
        semicolon_idx = constraint_block.find(";")
        if semicolon_idx != -1:
            constraint_block = constraint_block[:semicolon_idx + 1]
        for legacy in (
            "'prepping'", "'live'", "'ended'", "'synthesizing'",
            "'synthesized'", "'discarded'", "'failed'",
        ):
            assert legacy in constraint_block, (
                f"legacy status {legacy} must be retained in CHECK"
            )

    def test_up_sets_default_to_preparing(self) -> None:
        sql = _read_0055_up()
        assert "SET DEFAULT 'preparing'" in sql, (
            "up migration must change DEFAULT to 'preparing'"
        )

    def test_up_updates_active_session_index_with_canonical(self) -> None:
        """idx_conversations_status_active must include canonical statuses
        while retaining legacy ones."""
        sql = _read_0055_up()
        # Find "CREATE INDEX ... idx_conversations_status_active".
        needle = "CREATE INDEX idx_conversations_status_active"
        idx_pos = sql.find(needle)
        assert idx_pos != -1, (
            f"must recreate active-session index; needle={needle!r} not found"
        )
        block = sql[idx_pos:]
        semicolon_idx = block.find(";")
        if semicolon_idx != -1:
            block = block[:semicolon_idx + 1]
        # Canonical statuses must be present
        for canonical in ("'preparing'", "'active'"):
            assert canonical in block, (
                f"active-session index must include {canonical}"
            )
        # Legacy statuses must be retained
        for legacy in ("'prepping'", "'live'", "'synthesizing'"):
            assert legacy in block, (
                f"active-session index must retain legacy {legacy}"
            )

    def test_up_updates_spend_active_index_with_canonical(self) -> None:
        """idx_conversations_spend_active must include canonical while
        retaining legacy."""
        sql = _read_0055_up()
        needle = "CREATE INDEX idx_conversations_spend_active"
        idx_pos = sql.find(needle)
        assert idx_pos != -1, (
            f"must recreate spend-active index; needle={needle!r} not found"
        )
        block = sql[idx_pos:]
        semicolon_idx = block.find(";")
        if semicolon_idx != -1:
            block = block[:semicolon_idx + 1]
        for canonical in ("'preparing'", "'active'"):
            assert canonical in block, (
                f"spend-active index must include {canonical}"
            )
        for legacy in ("'prepping'", "'live'"):
            assert legacy in block, (
                f"spend-active index must retain legacy {legacy}"
            )

    def test_down_removes_canonical_from_check(self) -> None:
        """Down migration CHECK must NOT include preparing/active/completed."""
        sql = _read_0055_down()
        add_idx = sql.find("ADD CONSTRAINT conversations_status_check")
        assert add_idx != -1, "down migration must have ADD CONSTRAINT"
        constraint_block = sql[add_idx:]
        semicolon_idx = constraint_block.find(";")
        if semicolon_idx != -1:
            constraint_block = constraint_block[:semicolon_idx + 1]
        for canonical in ("'preparing'", "'active'", "'completed'"):
            assert canonical not in constraint_block, (
                f"down CHECK must not include {canonical}"
            )

    def test_down_restores_default_prepping(self) -> None:
        sql = _read_0055_down()
        assert "SET DEFAULT 'prepping'" in sql, (
            "down migration must restore DEFAULT 'prepping'"
        )

    def test_down_restores_legacy_active_index(self) -> None:
        """Down migration must restore idx_conversations_status_active to
        the pre-0055 predicate."""
        sql = _read_0055_down()
        needle = "CREATE INDEX idx_conversations_status_active"
        idx_pos = sql.find(needle)
        assert idx_pos != -1, (
            f"down must recreate active-session index; needle={needle!r} not found"
        )
        block = sql[idx_pos:]
        semicolon_idx = block.find(";")
        if semicolon_idx != -1:
            block = block[:semicolon_idx + 1]
        # Canonical should NOT be in the down version of this index
        for canonical in ("'preparing'", "'active'"):
            assert canonical not in block, (
                f"down active-session index must not include {canonical}"
            )
        # Legacy must be present
        for legacy in ("'prepping'", "'live'", "'synthesizing'"):
            assert legacy in block, (
                f"down active-session index must include legacy {legacy}"
            )

    def test_down_restores_legacy_spend_index(self) -> None:
        """Down migration must restore idx_conversations_spend_active to the
        pre-0055 predicate."""
        sql = _read_0055_down()
        needle = "CREATE INDEX idx_conversations_spend_active"
        idx_pos = sql.find(needle)
        assert idx_pos != -1, (
            f"down must recreate spend-active index; needle={needle!r} not found"
        )
        block = sql[idx_pos:]
        semicolon_idx = block.find(";")
        if semicolon_idx != -1:
            block = block[:semicolon_idx + 1]
        for canonical in ("'preparing'", "'active'"):
            assert canonical not in block, (
                f"down spend-active index must not include {canonical}"
            )
        for legacy in ("'prepping'", "'live'"):
            assert legacy in block, (
                f"down spend-active index must include legacy {legacy}"
            )
