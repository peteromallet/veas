"""Live DB tests for conversation artifacts (Sprint 1, Task T8).

Gated by DATABASE_URL / EVAL_DATABASE_URL — skipped gracefully when neither
is set.  Applies migrations to a scratch schema and verifies RLS policies,
CHECK constraints, cascade delete, index existence (via pg_indexes, NOT
EXPLAIN), down-migration round-trip, and add_artifact_link tombstone behavior.

Follows the pytest pattern from tests/test_live_migrations.py and
tests/test_live_artifacts.py.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import asyncpg

from app.services.live.artifacts import (
    add_artifact_link,
    create_artifact,
    get_current_artifact,
)

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


# ── helpers ──────────────────────────────────────────────────────────────────


def _read_migration(filename: str) -> str:
    return (MIGRATIONS_DIR / filename).read_text()


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _check_db_url() -> None:
    """Skip all DB-gated tests when no database URL is configured."""
    if not os.environ.get("DATABASE_URL") and not os.environ.get("EVAL_DATABASE_URL"):
        pytest.skip("DATABASE_URL / EVAL_DATABASE_URL not set")


@pytest.fixture(name="scratch_conn")
async def _scratch_conn_fixture() -> Any:
    """Yield an asyncpg Connection in a scratch schema with all migrations applied."""
    from evals.db import create_eval_pool, scratch_schema

    pool = await create_eval_pool()
    try:
        async with scratch_schema(
            pool, schema=f"eval_live_db_{uuid4().hex[:12]}"
        ) as scratch:
            async with pool.acquire() as conn:
                await conn.execute(
                    f'SET search_path TO "{scratch.schema}", public'
                )
                yield conn
    finally:
        await pool.close()


@pytest.fixture(name="seed_conversation")
async def _seed_conversation_fixture(scratch_conn: Any) -> tuple[str, str]:
    """Insert a users + conversations row and return (conversation_id, user_id)."""
    user_id = str(uuid4())
    conversation_id = str(uuid4())

    await scratch_conn.execute(
        "INSERT INTO mediator.users (id) VALUES ($1) ON CONFLICT DO NOTHING",
        user_id,
    )
    await scratch_conn.execute(
        """
        INSERT INTO mediator.conversations (id, user_id, partner_label, status)
        VALUES ($1, $2, 'test-partner', 'live')
        """,
        conversation_id,
        user_id,
    )
    return conversation_id, user_id


# ── Test 1: RLS + deny_anon + owner-scoped policies ─────────────────────────


class TestMigrationRlsPolicies:
    """Apply migration 0051 in scratch schema; verify RLS surface via pg_policies."""

    async def test_artifact_tables_have_rls_enabled_and_forced(
        self, scratch_conn: Any,
    ) -> None:
        """Both new tables must have relrowsecurity=true and relforcerowsecurity=true."""
        for table in ("conversation_artifacts", "artifact_links"):
            row = await scratch_conn.fetchrow(
                """
                SELECT c.relrowsecurity AS rls_enabled,
                       c.relforcerowsecurity AS rls_forced
                FROM pg_class c
                WHERE c.relname = $1
                  AND c.relkind = 'r'
                """,
                table,
            )
            assert row is not None, f"table {table} not found in pg_class"
            assert row["rls_enabled"], f"RLS not enabled on {table}"
            assert row["rls_forced"], f"RLS not forced on {table}"

    async def test_deny_anon_policies_present(self, scratch_conn: Any) -> None:
        """Both tables must have deny_anon policies via pg_policies."""
        for table in ("conversation_artifacts", "artifact_links"):
            row = await scratch_conn.fetchrow(
                """
                SELECT 1 FROM pg_policies
                WHERE tablename = $1 AND policyname = $2
                """,
                table,
                f"deny_anon_{table}",
            )
            assert row is not None, (
                f"deny_anon policy missing for {table}"
            )

    async def test_owner_scoped_policies_present(self, scratch_conn: Any) -> None:
        """Both tables must have owner_scoped policies via pg_policies."""
        for table in ("conversation_artifacts", "artifact_links"):
            row = await scratch_conn.fetchrow(
                """
                SELECT 1 FROM pg_policies
                WHERE tablename = $1 AND policyname = $2
                """,
                table,
                f"owner_scoped_{table}",
            )
            assert row is not None, (
                f"owner_scoped policy missing for {table}"
            )

    async def test_conversation_artifacts_policy_uses_conversations_hop(
        self, scratch_conn: Any,
    ) -> None:
        """The owner_scoped_conversation_artifacts policy must reference conversations."""
        row = await scratch_conn.fetchrow(
            """
            SELECT qual, with_check FROM pg_policies
            WHERE tablename = 'conversation_artifacts'
              AND policyname = 'owner_scoped_conversation_artifacts'
            """
        )
        assert row is not None
        qual = (row["qual"] or "") + (row["with_check"] or "")
        assert "conversations" in qual.lower(), (
            "owner_scoped policy must reference conversations table"
        )

    async def test_artifact_links_policy_uses_two_hop(
        self, scratch_conn: Any,
    ) -> None:
        """The owner_scoped_artifact_links policy uses two-hop join."""
        row = await scratch_conn.fetchrow(
            """
            SELECT qual, with_check FROM pg_policies
            WHERE tablename = 'artifact_links'
              AND policyname = 'owner_scoped_artifact_links'
            """
        )
        assert row is not None
        qual = (row["qual"] or "") + (row["with_check"] or "")
        assert "conversation_artifacts" in qual.lower(), (
            "two-hop policy must reference conversation_artifacts"
        )
        assert "conversations" in qual.lower(), (
            "two-hop policy must ultimately reference conversations"
        )


# ── Test 2: artifact + link insert to conversation_items and memories ────────


class TestArtifactLinkSuccess:
    async def test_insert_artifact_and_link_to_conv_items_and_memories(
        self, scratch_conn: Any, seed_conversation: tuple[str, str],
    ) -> None:
        """Insert an artifact and link to a conversation_items row and a
        memories row — both should succeed."""
        conversation_id, user_id = seed_conversation

        # We need a conversation_items row to link to.  Insert one.
        item_id = str(uuid4())
        await scratch_conn.execute(
            """
            INSERT INTO mediator.conversation_items
                (id, conversation_id, content, kind)
            VALUES ($1, $2, 'test content', 'message')
            """,
            item_id,
            conversation_id,
        )

        artifact = await create_artifact(
            scratch_conn,
            conversation_id=conversation_id,
            bot_id="mediator",
            user_id=user_id,
            artifact_type="live_prep_brief",
            payload={"summary": "hello"},
        )

        # Link to conversation_items.
        link1 = await add_artifact_link(
            scratch_conn,
            artifact_id=artifact.id,
            target_table="conversation_items",
            target_id=item_id,
            relation="summarized_from",
        )
        assert link1.artifact_id == artifact.id
        assert link1.target_table == "conversation_items"
        assert link1.target_id == item_id

        # Link to a memories row (just the ID — no FK enforced since
        # memories is in the durable set, not conversation-scoped).
        memory_id = str(uuid4())
        link2 = await add_artifact_link(
            scratch_conn,
            artifact_id=artifact.id,
            target_table="memories",
            target_id=memory_id,
            relation="extracted_memory",
        )
        assert link2.artifact_id == artifact.id
        assert link2.target_table == "memories"
        assert link2.target_id == memory_id


# ── Test 3: target_table='bot_turns' → CHECK violation ──────────────────────


class TestCheckViolationBotTurns:
    async def test_link_to_bot_turns_raises_check_violation(
        self, scratch_conn: Any, seed_conversation: tuple[str, str],
    ) -> None:
        """Inserting an artifact_link with target_table='bot_turns' must fail
        with a CHECK constraint violation."""
        conversation_id, user_id = seed_conversation
        artifact = await create_artifact(
            scratch_conn,
            conversation_id=conversation_id,
            bot_id="mediator",
            user_id=user_id,
            artifact_type="live_debrief",
            payload={},
        )
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await scratch_conn.execute(
                """
                INSERT INTO mediator.artifact_links
                    (artifact_id, target_table, target_id, relation)
                VALUES ($1, 'bot_turns', $2, 'logged_event')
                """,
                artifact.id,
                str(uuid4()),
            )


# ── Test 4: target_table='users' → CHECK violation ──────────────────────────


class TestCheckViolationUsers:
    async def test_link_to_users_raises_check_violation(
        self, scratch_conn: Any, seed_conversation: tuple[str, str],
    ) -> None:
        """Inserting an artifact_link with target_table='users' must fail
        with a CHECK constraint violation."""
        conversation_id, user_id = seed_conversation
        artifact = await create_artifact(
            scratch_conn,
            conversation_id=conversation_id,
            bot_id="mediator",
            user_id=user_id,
            artifact_type="live_debrief",
            payload={},
        )
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await scratch_conn.execute(
                """
                INSERT INTO mediator.artifact_links
                    (artifact_id, target_table, target_id, relation)
                VALUES ($1, 'users', $2, 'logged_event')
                """,
                artifact.id,
                str(uuid4()),
            )


# ── Test 5: unknown relation → CHECK violation ──────────────────────────────


class TestCheckViolationRelation:
    async def test_link_with_unknown_relation_raises_check_violation(
        self, scratch_conn: Any, seed_conversation: tuple[str, str],
    ) -> None:
        """Inserting an artifact_link with an unknown relation must fail
        with a CHECK constraint violation."""
        conversation_id, user_id = seed_conversation
        artifact = await create_artifact(
            scratch_conn,
            conversation_id=conversation_id,
            bot_id="mediator",
            user_id=user_id,
            artifact_type="live_debrief",
            payload={},
        )
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await scratch_conn.execute(
                """
                INSERT INTO mediator.artifact_links
                    (artifact_id, target_table, target_id, relation)
                VALUES ($1, 'memories', $2, 'nonexistent_relation_xyz')
                """,
                artifact.id,
                str(uuid4()),
            )


# ── Test 6: duplicate unique violation + idempotency ─────────────────────────


class TestDuplicateLinkAndIdempotency:
    async def test_duplicate_raw_sql_allowed_after_0054(
        self, scratch_conn: Any, seed_conversation: tuple[str, str],
    ) -> None:
        """After migration 0054 drops the UNIQUE constraint, duplicate raw
        INSERTs on artifact_links MUST succeed (insert-distinct by design)."""
        conversation_id, user_id = seed_conversation
        artifact = await create_artifact(
            scratch_conn,
            conversation_id=conversation_id,
            bot_id="mediator",
            user_id=user_id,
            artifact_type="live_prep_brief",
            payload={},
        )
        target_id = str(uuid4())
        # First insert succeeds.
        await scratch_conn.execute(
            """
            INSERT INTO mediator.artifact_links
                (artifact_id, target_table, target_id, relation)
            VALUES ($1, 'memories', $2, 'extracted_memory')
            """,
            artifact.id,
            target_id,
        )
        # Second insert with same key must ALSO succeed (no unique constraint).
        await scratch_conn.execute(
            """
            INSERT INTO mediator.artifact_links
                (artifact_id, target_table, target_id, relation)
            VALUES ($1, 'memories', $2, 'extracted_memory')
            """,
            artifact.id,
            target_id,
        )
        # Verify two rows exist.
        rows = await scratch_conn.fetch(
            """
            SELECT id FROM mediator.artifact_links
            WHERE artifact_id = $1 AND target_table = 'memories'
              AND target_id = $2 AND relation = 'extracted_memory'
              AND deleted_at IS NULL
            """,
            artifact.id,
            target_id,
        )
        assert len(rows) == 2, (
            f"Expected 2 rows after duplicate insert, got {len(rows)}"
        )

    async def test_add_artifact_link_idempotent_returns_same_row(
        self, scratch_conn: Any, seed_conversation: tuple[str, str],
    ) -> None:
        """Calling add_artifact_link twice with idempotent=True returns the same row."""
        conversation_id, user_id = seed_conversation
        artifact = await create_artifact(
            scratch_conn,
            conversation_id=conversation_id,
            bot_id="mediator",
            user_id=user_id,
            artifact_type="live_prep_brief",
            payload={},
        )
        target_id = str(uuid4())
        link1 = await add_artifact_link(
            scratch_conn,
            artifact_id=artifact.id,
            target_table="memories",
            target_id=target_id,
            relation="extracted_memory",
            idempotent=True,
        )
        link2 = await add_artifact_link(
            scratch_conn,
            artifact_id=artifact.id,
            target_table="memories",
            target_id=target_id,
            relation="extracted_memory",
            idempotent=True,
        )
        assert link1.id == link2.id, (
            f"Idempotent calls returned different rows: {link1.id} vs {link2.id}"
        )


# ── Test 7: revision_number beats created_at ────────────────────────────────


class TestRevisionNumberOrdering:
    async def test_get_current_artifact_uses_revision_number_not_created_at(
        self, scratch_conn: Any, seed_conversation: tuple[str, str],
    ) -> None:
        """Insert two artifacts of the same type where the second revision
        has an earlier created_at.  get_current_artifact must return the row
        with the higher revision_number, not the one with the later created_at."""
        conversation_id, user_id = seed_conversation

        # Create rev 1 with a "future" created_at.
        await scratch_conn.execute(
            """
            INSERT INTO mediator.conversation_artifacts
                (id, conversation_id, bot_id, user_id, artifact_type,
                 payload, revision_number, created_at)
            VALUES ($1, $2, $3, $4, $5, $6, 1, now() + interval '1 hour')
            """,
            str(uuid4()),
            conversation_id,
            "mediator",
            user_id,
            "review_summary",
            '{"v":1}',
        )

        # Create rev 2 with current created_at (earlier than rev 1's).
        rev2_id = str(uuid4())
        await scratch_conn.execute(
            """
            INSERT INTO mediator.conversation_artifacts
                (id, conversation_id, bot_id, user_id, artifact_type,
                 payload, revision_number, created_at)
            VALUES ($1, $2, $3, $4, $5, $6, 2, now())
            """,
            rev2_id,
            conversation_id,
            "mediator",
            user_id,
            "review_summary",
            '{"v":2}',
        )

        current = await get_current_artifact(
            scratch_conn,
            conversation_id=conversation_id,
            artifact_type="review_summary",
        )
        assert current is not None
        assert current.revision_number == 2, (
            f"Expected revision 2 (max revision_number), got {current.revision_number}"
        )
        assert current.payload == {"v": 2}


# ── Test 8: cascade delete ──────────────────────────────────────────────────


class TestCascadeDelete:
    async def test_delete_conversation_cascades_to_artifacts_and_links(
        self, scratch_conn: Any, seed_conversation: tuple[str, str],
    ) -> None:
        """Deleting a conversations row must cascade-delete its artifacts
        and their links."""
        conversation_id, user_id = seed_conversation

        artifact = await create_artifact(
            scratch_conn,
            conversation_id=conversation_id,
            bot_id="mediator",
            user_id=user_id,
            artifact_type="live_prep_brief",
            payload={"x": 1},
        )
        link = await add_artifact_link(
            scratch_conn,
            artifact_id=artifact.id,
            target_table="memories",
            target_id=str(uuid4()),
            relation="extracted_memory",
        )

        # Verify they exist.
        rows = await scratch_conn.fetch(
            "SELECT 1 FROM mediator.conversation_artifacts WHERE id = $1",
            artifact.id,
        )
        assert len(rows) == 1
        rows = await scratch_conn.fetch(
            "SELECT 1 FROM mediator.artifact_links WHERE id = $1",
            link.id,
        )
        assert len(rows) == 1

        # Delete the conversation.
        await scratch_conn.execute(
            "DELETE FROM mediator.conversations WHERE id = $1",
            conversation_id,
        )

        # Artifacts and links must be gone.
        rows = await scratch_conn.fetch(
            "SELECT 1 FROM mediator.conversation_artifacts WHERE id = $1",
            artifact.id,
        )
        assert len(rows) == 0, "artifact should be cascade-deleted"

        rows = await scratch_conn.fetch(
            "SELECT 1 FROM mediator.artifact_links WHERE id = $1",
            link.id,
        )
        assert len(rows) == 0, "artifact link should be cascade-deleted"


# ── Test 9: bot_turns queryability + index existence via pg_indexes ──────────


class TestBotTurnsQueryabilityAndIndexes:
    async def test_bot_turns_queryable_by_conversation_id_and_kind(
        self, scratch_conn: Any, seed_conversation: tuple[str, str],
    ) -> None:
        """Insert a bot_turns row via raw SQL with conversation_id and
        kind='live_prep'.  Verify it is queryable by both columns."""
        conversation_id, user_id = seed_conversation

        # We need a topic row (bot_turns.topic_id has a VALIDATED NOT NULL
        # CHECK from migrations 0026 → 0027).  Use an existing seeded topic
        # or create one.  The 'relationship' topic is seeded in 0020.
        topic_row = await scratch_conn.fetchrow(
            "SELECT id FROM mediator.topics WHERE slug = 'relationship' LIMIT 1"
        )
        topic_id = topic_row["id"] if topic_row else None
        if topic_id is None:
            # Fallback: create a scratch topic.
            topic_id = str(uuid4())
            await scratch_conn.execute(
                """
                INSERT INTO mediator.topics (id, slug, display_name, participants_shape)
                VALUES ($1, 'scratch-test', 'Scratch Test', 'solo')
                ON CONFLICT DO NOTHING
                """,
                topic_id,
            )

        # Insert a minimal bot_turns row.  We need all NOT NULL columns:
        #   system_prompt_version, model_version, prompt_snapshot,
        #   prompt_snapshot_encrypted, bot_id, topic_id,
        #   bot_spec_version, hot_context_builder_version, tool_schema_version.
        turn_id = str(uuid4())
        await scratch_conn.execute(
            """
            INSERT INTO mediator.bot_turns (
                id, user_in_context, system_prompt_version, model_version,
                prompt_snapshot, prompt_snapshot_encrypted, bot_id, topic_id,
                bot_spec_version, hot_context_builder_version,
                tool_schema_version, conversation_id, kind
            ) VALUES (
                $1, $2, 'v1', 'test-model',
                'test prompt', 'encrypted-test', 'mediator', $4,
                'v1', 'v1', 'v1', $3, 'live_prep'
            )
            """,
            turn_id,
            user_id,
            conversation_id,
            topic_id,
        )

        # Queryable by conversation_id.
        rows = await scratch_conn.fetch(
            "SELECT id FROM mediator.bot_turns WHERE conversation_id = $1",
            conversation_id,
        )
        assert len(rows) == 1
        assert rows[0]["id"] == turn_id

        # Queryable by kind.
        rows = await scratch_conn.fetch(
            "SELECT id FROM mediator.bot_turns WHERE kind = 'live_prep'",
        )
        assert len(rows) >= 1
        found = any(r["id"] == turn_id for r in rows)
        assert found, "Inserted turn not found when querying by kind='live_prep'"

    async def test_bot_turns_indexes_exist_in_pg_indexes(
        self, scratch_conn: Any,
    ) -> None:
        """Verify both partial indexes on bot_turns are present via pg_indexes
        catalog (NOT EXPLAIN)."""
        rows = await scratch_conn.fetch(
            """
            SELECT indexname FROM pg_indexes
            WHERE tablename = 'bot_turns'
              AND indexname IN (
                  'idx_bot_turns_conversation_id',
                  'idx_bot_turns_kind'
              )
            """
        )
        found = {r["indexname"] for r in rows}
        assert "idx_bot_turns_conversation_id" in found, (
            "idx_bot_turns_conversation_id index not found in pg_indexes"
        )
        assert "idx_bot_turns_kind" in found, (
            "idx_bot_turns_kind index not found in pg_indexes"
        )

    async def test_artifact_tables_indexes_exist_in_pg_indexes(
        self, scratch_conn: Any,
    ) -> None:
        """Verify the artifact-layer indexes exist via pg_indexes."""
        # conversation_artifacts indexes.
        rows = await scratch_conn.fetch(
            """
            SELECT indexname FROM pg_indexes
            WHERE tablename = 'conversation_artifacts'
              AND indexname IN (
                  'conversation_artifacts_conversation_id_artifact_type_revision_number_key',
                  'idx_conversation_artifacts_latest_rev',
                  'idx_conversation_artifacts_active'
              )
            """
        )
        found = {r["indexname"] for r in rows}
        assert "conversation_artifacts_conversation_id_artifact_type_revision_number_key" in found, (
            "UNIQUE constraint index not found for conversation_artifacts"
        )
        assert "idx_conversation_artifacts_latest_rev" in found, (
            "idx_conversation_artifacts_latest_rev not found in pg_indexes"
        )
        assert "idx_conversation_artifacts_active" in found, (
            "idx_conversation_artifacts_active not found in pg_indexes"
        )

        # artifact_links indexes.
        rows = await scratch_conn.fetch(
            """
            SELECT indexname FROM pg_indexes
            WHERE tablename = 'artifact_links'
              AND indexname IN (
                  'artifact_links_artifact_id_target_table_target_id_relation_key',
                  'idx_artifact_links_target'
              )
            """
        )
        found = {r["indexname"] for r in rows}
        assert "artifact_links_artifact_id_target_table_target_id_relation_key" in found, (
            "UNIQUE constraint index not found for artifact_links"
        )
        assert "idx_artifact_links_target" in found, (
            "idx_artifact_links_target not found in pg_indexes"
        )


# ── Test 10: down-migration round-trip ──────────────────────────────────────


class TestDownMigrationRoundTrip:
    async def test_apply_down_reapply_clean(
        self, scratch_conn: Any, seed_conversation: tuple[str, str],
    ) -> None:
        """Apply migration 0051, then apply the down migration, then re-apply
        the forward migration — assert the second apply succeeds cleanly."""
        conversation_id, user_id = seed_conversation

        # Sanity: artifact creation works (proof migration 0051 is applied).
        artifact = await create_artifact(
            scratch_conn,
            conversation_id=conversation_id,
            bot_id="mediator",
            user_id=user_id,
            artifact_type="live_prep_brief",
            payload={"test": True},
        )
        assert artifact.revision_number == 1

        # Apply the down migration.
        down_sql = _read_migration("0051_conversation_artifacts.down.sql")
        await scratch_conn.execute(down_sql)

        # Verify tables are gone.
        for table in ("conversation_artifacts", "artifact_links"):
            row = await scratch_conn.fetchrow(
                "SELECT 1 FROM pg_class WHERE relname = $1 AND relkind = 'r'",
                table,
            )
            assert row is None, f"table {table} should be dropped by down migration"

        # Verify bot_turns columns are gone.
        cols = await scratch_conn.fetch(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'bot_turns'
              AND column_name IN ('conversation_id', 'kind')
            """
        )
        assert len(cols) == 0, (
            f"bot_turns columns should be dropped: {[c['column_name'] for c in cols]}"
        )

        # Re-apply the forward migration.
        up_sql = _read_migration("0051_conversation_artifacts.sql")
        await scratch_conn.execute(up_sql)

        # Verify tables are back and functional.
        artifact2 = await create_artifact(
            scratch_conn,
            conversation_id=conversation_id,
            bot_id="mediator",
            user_id=user_id,
            artifact_type="live_prep_brief",
            payload={"reapplied": True},
        )
        assert artifact2.revision_number == 1, (
            "After re-apply, create_artifact should work with revision 1"
        )


# ── Test 11: tombstone behavior ─────────────────────────────────────────────


class TestTombstoneBehavior:
    async def test_add_artifact_link_after_soft_delete_creates_new_row(
        self, scratch_conn: Any, seed_conversation: tuple[str, str],
    ) -> None:
        """Soft-delete a link, then call add_artifact_link again with the
        same key.  Assert a NEW row is created (different id), and the
        returned row is the new one, not the tombstoned one."""
        conversation_id, user_id = seed_conversation
        artifact = await create_artifact(
            scratch_conn,
            conversation_id=conversation_id,
            bot_id="mediator",
            user_id=user_id,
            artifact_type="live_prep_brief",
            payload={},
        )
        target_id = str(uuid4())

        # Create initial link.
        link1 = await add_artifact_link(
            scratch_conn,
            artifact_id=artifact.id,
            target_table="memories",
            target_id=target_id,
            relation="extracted_memory",
        )

        # Soft-delete it.
        await scratch_conn.execute(
            """
            UPDATE mediator.artifact_links
            SET deleted_at = now()
            WHERE id = $1
            """,
            link1.id,
        )

        # Call add_artifact_link again — must create a NEW row.
        link2 = await add_artifact_link(
            scratch_conn,
            artifact_id=artifact.id,
            target_table="memories",
            target_id=target_id,
            relation="extracted_memory",
        )

        # Assert it's a different row.
        assert link2.id != link1.id, (
            "add_artifact_link after soft-delete must create a new row, "
            f"not return the tombstoned one (got same id: {link2.id})"
        )

        # Assert the returned row is not tombstoned.
        assert link2.deleted_at is None, (
            "Returned link row must not be tombstoned"
        )

        # Verify the tombstoned row still exists but is separate.
        rows = await scratch_conn.fetch(
            """
            SELECT id, deleted_at FROM mediator.artifact_links
            WHERE artifact_id = $1 AND target_table = $2
              AND target_id = $3 AND relation = $4
            ORDER BY created_at
            """,
            artifact.id,
            "memories",
            target_id,
            "extracted_memory",
        )
        assert len(rows) == 2, f"Expected 2 rows (1 tombstoned + 1 fresh), got {len(rows)}"
        ids = [r["id"] for r in rows]
        assert link1.id in ids, "Tombstoned row should still exist"
        assert link2.id in ids, "Fresh row should exist"
        assert link2.id != link1.id, "Fresh row must have different id"
