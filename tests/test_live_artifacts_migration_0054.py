"""Static migration tests for 0054_artifact_links_widen_checks.

Modeled after tests/test_live_artifacts_migration.py.  These tests run
without a DB connection — they assert against the migration SQL text directly.

Test classes:
- TestMigration0054FilesExist: up/down file presence
- TestMigration0054UpContent: forward migration content checks
- TestMigration0054DownContent: down migration content checks
- TestMigration0054TargetTables: target_table constraint coverage
- TestMigration0054Relations: relation constraint coverage
"""

from __future__ import annotations

from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

MIGRATION = "0054_artifact_links_widen_checks"


def _read_up() -> str:
    return (MIGRATIONS_DIR / f"{MIGRATION}.sql").read_text()


def _read_down() -> str:
    return (MIGRATIONS_DIR / f"{MIGRATION}.down.sql").read_text()


# -- A. File existence -------------------------------------------------------


class TestMigration0054FilesExist:
    def test_up_present(self) -> None:
        assert (MIGRATIONS_DIR / f"{MIGRATION}.sql").exists(), (
            f"forward migration {MIGRATION}.sql not found"
        )

    def test_down_present(self) -> None:
        assert (MIGRATIONS_DIR / f"{MIGRATION}.down.sql").exists(), (
            f"down migration {MIGRATION}.down.sql not found"
        )


# -- B. Forward migration content -------------------------------------------


class TestMigration0054UpContent:
    def test_drops_unique_constraint(self) -> None:
        """Up migration must drop the UNIQUE constraint on artifact_links."""
        sql = _read_up()
        assert (
            "DROP CONSTRAINT" in sql
            and "pg_constraint" in sql
            and "contype = 'u'" in sql
        ), "missing dynamic UNIQUE constraint drop"

    def test_adds_non_unique_forward_lookup_index(self) -> None:
        """Up migration must add idx_artifact_links_artifact_id."""
        sql = _read_up()
        assert "idx_artifact_links_artifact_id" in sql, (
            "missing idx_artifact_links_artifact_id index"
        )
        assert "CREATE INDEX" in sql, "missing CREATE INDEX"

    def test_widens_target_table_check(self) -> None:
        """Up migration must widen target_table CHECK to include themes, watch_items, out_of_bounds."""
        sql = _read_up()
        assert "artifact_links_target_table_check" in sql, (
            "missing named target_table CHECK constraint"
        )
        assert "'themes'" in sql, "target_table CHECK missing 'themes'"
        assert "'watch_items'" in sql, "target_table CHECK missing 'watch_items'"
        assert "'out_of_bounds'" in sql, "target_table CHECK missing 'out_of_bounds'"

    def test_widens_relation_check(self) -> None:
        """Up migration must widen relation CHECK to include all Sprint 4 relations."""
        sql = _read_up()
        assert "artifact_links_relation_check" in sql, (
            "missing named relation CHECK constraint"
        )
        for r in (
            "'extracted_theme'",
            "'closed_commitment'",
            "'updated_commitment'",
            "'updated_follow_up'",
            "'created_watch_item'",
            "'updated_watch_item'",
            "'addressed_watch_item'",
            "'created_oob'",
            "'updated_oob'",
            "'lifted_oob'",
        ):
            assert r in sql, f"relation CHECK missing {r}"

    def test_preserves_existing_reverse_lookup_index(self) -> None:
        """Up migration comment documents that idx_artifact_links_target is preserved."""
        sql = _read_up()
        assert "idx_artifact_links_target" in sql, (
            "missing reference to preserved idx_artifact_links_target"
        )

    def test_begin_commit_wrapping(self) -> None:
        sql = _read_up()
        assert "BEGIN" in sql, "forward migration must contain BEGIN"
        assert "COMMIT" in sql, "forward migration must contain COMMIT"


# -- C. Down migration content ----------------------------------------------


class TestMigration0054DownContent:
    def test_drops_forward_lookup_index(self) -> None:
        """Down migration must drop idx_artifact_links_artifact_id."""
        sql = _read_down()
        assert "DROP INDEX IF EXISTS mediator.idx_artifact_links_artifact_id" in sql, (
            "missing DROP INDEX for idx_artifact_links_artifact_id"
        )

    def test_handles_duplicate_rows(self) -> None:
        """Down migration must remove duplicates before restoring UNIQUE."""
        sql = _read_down()
        assert "ROW_NUMBER()" in sql, (
            "missing duplicate removal via ROW_NUMBER()"
        )
        assert "PARTITION BY artifact_id, target_table, target_id, relation" in sql, (
            "missing PARTITION BY on the unique key columns"
        )
        assert "RAISE WARNING" in sql, (
            "missing RAISE WARNING for duplicate data loss"
        )

    def test_restores_unique_constraint(self) -> None:
        """Down migration must restore the UNIQUE constraint."""
        sql = _read_down()
        assert "ADD UNIQUE (artifact_id, target_table, target_id, relation)" in sql, (
            "missing UNIQUE constraint restoration"
        )

    def test_reverts_target_table_check_to_0051_values(self) -> None:
        """Down migration must revert target_table CHECK to 0051 values (no themes/watch/oob)."""
        sql = _read_down()
        # Find the down migration's target_table CHECK block.
        assert "artifact_links_target_table_check" in sql, (
            "missing down target_table CHECK"
        )
        # Should restore to 0051 values (without themes/watch_items/out_of_bounds).
        # Check that the down migration explicitly lists the 0051 values.
        assert "'topic_status'" in sql, "down target_table CHECK missing 'topic_status'"
        # Verify themes/watch/oob are NOT in the down CHECK block.
        # The down migration contains two CHECK blocks - the down one and the
        # original one in the comment header.  Check the down-specific block.
        down_tt_pos = sql.rfind("artifact_links_target_table_check")
        down_tt_block = sql[down_tt_pos:]
        assert "'themes'" not in down_tt_block, (
            "down target_table CHECK must NOT include 'themes'"
        )
        assert "'watch_items'" not in down_tt_block, (
            "down target_table CHECK must NOT include 'watch_items'"
        )
        assert "'out_of_bounds'" not in down_tt_block, (
            "down target_table CHECK must NOT include 'out_of_bounds'"
        )

    def test_reverts_relation_check_to_0051_values(self) -> None:
        """Down migration must revert relation CHECK to 0051 values."""
        sql = _read_down()
        assert "artifact_links_relation_check" in sql, (
            "missing down relation CHECK"
        )
        # The down migration's relation block should have the 0051 values.
        down_rel_pos = sql.rfind("artifact_links_relation_check")
        down_rel_block = sql[down_rel_pos:]
        for r in (
            "'created_watch_item'",
            "'updated_watch_item'",
            "'addressed_watch_item'",
            "'created_oob'",
            "'updated_oob'",
            "'lifted_oob'",
            "'extracted_theme'",
            "'closed_commitment'",
            "'updated_commitment'",
            "'updated_follow_up'",
        ):
            assert r not in down_rel_block, (
                f"down relation CHECK must NOT include {r}"
            )

    def test_documents_data_loss(self) -> None:
        """Down migration header must document that duplicate removal is lossy."""
        sql = _read_down()
        assert "lossy" in sql.lower() or "data loss" in sql.lower() or (
            "cannot be recovered" in sql.lower()
        ), "down migration must document lossy duplicate removal"

    def test_begin_commit_wrapping(self) -> None:
        sql = _read_down()
        assert "BEGIN" in sql, "down migration must contain BEGIN"
        assert "COMMIT" in sql, "down migration must contain COMMIT"


# -- D. Target table constraint coverage ------------------------------------


class TestMigration0054TargetTables:
    """Verify all Sprint 4 target tables are in the widened CHECK constraint."""

    def test_all_0051_tables_preserved(self) -> None:
        """All 0051 target_table values must be in the widened CHECK."""
        sql = _read_up()
        for t in (
            "'conversations'", "'conversation_items'", "'transcript_turns'",
            "'conversation_notes'", "'messages'", "'memories'",
            "'observations'", "'distillations'", "'commitments'",
            "'events'", "'scheduled_jobs'", "'topic_status'",
        ):
            assert t in sql, f"target_table CHECK missing pre-existing {t}"

    def test_themes_added(self) -> None:
        sql = _read_up()
        assert "'themes'" in sql

    def test_watch_items_added(self) -> None:
        sql = _read_up()
        assert "'watch_items'" in sql

    def test_out_of_bounds_added(self) -> None:
        sql = _read_up()
        assert "'out_of_bounds'" in sql

    def test_bot_turns_still_excluded(self) -> None:
        """bot_turns must NOT appear in the 0054 target_table CHECK — provenance via FK."""
        sql = _read_up()
        assert "'bot_turns'" not in sql, (
            "bot_turns must not be in target_table CHECK"
        )

    def test_pregnancy_state_still_excluded(self) -> None:
        """pregnancy_state must NOT appear in the 0054 target_table CHECK."""
        sql = _read_up()
        assert "'pregnancy_state'" not in sql, (
            "pregnancy_state must not be in target_table CHECK"
        )


# -- E. Relation constraint coverage ----------------------------------------


class TestMigration0054Relations:
    """Verify all Sprint 4 relations are in the widened CHECK constraint."""

    def test_all_0051_relations_preserved(self) -> None:
        """All 0051 relation values must be in the widened CHECK."""
        sql = _read_up()
        for r in (
            "'planned_item'", "'summarized_from'", "'evidence_quote'",
            "'extracted_memory'", "'extracted_observation'",
            "'extracted_distillation'", "'created_commitment'",
            "'logged_event'", "'created_follow_up'",
            "'updated_topic_status'",
        ):
            assert r in sql, f"relation CHECK missing pre-existing {r}"

    def test_extracted_theme_added(self) -> None:
        sql = _read_up()
        assert "'extracted_theme'" in sql

    def test_updated_commitment_added(self) -> None:
        sql = _read_up()
        assert "'updated_commitment'" in sql

    def test_closed_commitment_added(self) -> None:
        sql = _read_up()
        assert "'closed_commitment'" in sql

    def test_updated_follow_up_added(self) -> None:
        sql = _read_up()
        assert "'updated_follow_up'" in sql

    def test_watch_item_relations_added(self) -> None:
        sql = _read_up()
        assert "'created_watch_item'" in sql
        assert "'updated_watch_item'" in sql
        assert "'addressed_watch_item'" in sql

    def test_oob_relations_added(self) -> None:
        sql = _read_up()
        assert "'created_oob'" in sql
        assert "'updated_oob'" in sql
        assert "'lifted_oob'" in sql
