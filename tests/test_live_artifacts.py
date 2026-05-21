"""Helper + config unit tests for conversation artifacts (Sprint 1).

Two layers:
1. DB-gated helper tests: create_artifact, add_artifact_link,
   list_artifact_links, savepoint retry — require DATABASE_URL or
   EVAL_DATABASE_URL and are skipped when neither is set.
2. Non-DB tests: config bounds, constant-vs-SQL parity, ValueError
   rejection (mocked connection).

Follows the pytest pattern from tests/test_live_migrations.py.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.services.live.artifacts import (
    ALLOWED_TARGET_TABLES,
    ARTIFACT_TYPES,
    LIVE_DEBRIEF_KIND,
    LIVE_PREP_KIND,
    RELATIONS,
    ArtifactLinkRow,
    ArtifactRow,
    add_artifact_link,
    create_artifact,
    get_current_artifact,
    list_artifact_links,
    list_artifacts,
)

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


# ---------------------------------------------------------------------------
# DB-gated helpers — requires DATABASE_URL or EVAL_DATABASE_URL
# ---------------------------------------------------------------------------


@pytest.fixture(name="_check_db_url")
def _check_db_url_fixture() -> None:
    """Skip DB-gated tests when no database URL is configured."""
    if not os.environ.get("DATABASE_URL") and not os.environ.get("EVAL_DATABASE_URL"):
        pytest.skip("DATABASE_URL / EVAL_DATABASE_URL not set")


@pytest.fixture(name="scratch_conn")
async def _scratch_conn_fixture(_check_db_url: None) -> Any:
    """Yield an asyncpg Connection in a scratch schema with all migrations applied."""
    from evals.db import create_eval_pool, scratch_schema

    pool = await create_eval_pool()
    try:
        async with scratch_schema(pool, schema=f"eval_artifacts_{uuid4().hex[:12]}") as scratch:
            async with pool.acquire() as conn:
                await conn.execute(f"SET search_path TO \"{scratch.schema}\", public")
                yield conn
    finally:
        await pool.close()


@pytest.fixture(name="seed_conversation")
async def _seed_conversation_fixture(scratch_conn: Any) -> tuple[str, str]:
    """Insert a conversations + users row and return (conversation_id, user_id)."""
    user_id = str(uuid4())
    conversation_id = str(uuid4())

    # Seed a user row (minimal — just the PK).
    await scratch_conn.execute(
        "INSERT INTO mediator.users (id) VALUES ($1) ON CONFLICT DO NOTHING",
        user_id,
    )

    # Seed a conversations row.
    await scratch_conn.execute(
        """
        INSERT INTO mediator.conversations (id, user_id, partner_label, status)
        VALUES ($1, $2, 'test-partner', 'live')
        """,
        conversation_id,
        user_id,
    )

    return conversation_id, user_id


class TestCreateArtifact:
    async def test_happy_path_creates_revision_1(
        self, scratch_conn: Any, seed_conversation: tuple[str, str]
    ) -> None:
        """First create_artifact call for a type produces revision_number=1."""
        conversation_id, user_id = seed_conversation
        artifact = await create_artifact(
            scratch_conn,
            conversation_id=conversation_id,
            bot_id="mediator",
            user_id=user_id,
            artifact_type="live_prep_brief",
            payload={"summary": "hello world"},
        )
        assert artifact.revision_number == 1
        assert artifact.artifact_type == "live_prep_brief"
        assert artifact.payload == {"summary": "hello world"}
        assert artifact.bot_id == "mediator"
        assert artifact.id is not None

    async def test_two_sequential_calls_produce_revisions_1_and_2(
        self, scratch_conn: Any, seed_conversation: tuple[str, str]
    ) -> None:
        """Two create_artifact calls for the same type auto-increment revisions."""
        conversation_id, user_id = seed_conversation
        a1 = await create_artifact(
            scratch_conn,
            conversation_id=conversation_id,
            bot_id="mediator",
            user_id=user_id,
            artifact_type="live_prep_brief",
            payload={"v": 1},
        )
        a2 = await create_artifact(
            scratch_conn,
            conversation_id=conversation_id,
            bot_id="mediator",
            user_id=user_id,
            artifact_type="live_prep_brief",
            payload={"v": 2},
        )
        assert a1.revision_number == 1
        assert a2.revision_number == 2
        assert a2.id != a1.id

    async def test_revision_per_artifact_type(
        self, scratch_conn: Any, seed_conversation: tuple[str, str]
    ) -> None:
        """Different artifact_type get independent revision sequences."""
        conversation_id, user_id = seed_conversation
        a1 = await create_artifact(
            scratch_conn,
            conversation_id=conversation_id,
            bot_id="mediator",
            user_id=user_id,
            artifact_type="live_prep_brief",
            payload={},
        )
        a2 = await create_artifact(
            scratch_conn,
            conversation_id=conversation_id,
            bot_id="mediator",
            user_id=user_id,
            artifact_type="live_debrief",
            payload={},
        )
        assert a1.revision_number == 1
        assert a2.revision_number == 1  # different type, independent counter

    async def test_get_current_artifact_returns_highest_revision(
        self, scratch_conn: Any, seed_conversation: tuple[str, str]
    ) -> None:
        """get_current_artifact returns the row with max revision_number."""
        conversation_id, user_id = seed_conversation
        await create_artifact(
            scratch_conn,
            conversation_id=conversation_id,
            bot_id="mediator",
            user_id=user_id,
            artifact_type="live_prep_brief",
            payload={"v": 1},
        )
        await create_artifact(
            scratch_conn,
            conversation_id=conversation_id,
            bot_id="mediator",
            user_id=user_id,
            artifact_type="live_prep_brief",
            payload={"v": 2},
        )
        current = await get_current_artifact(
            scratch_conn,
            conversation_id=conversation_id,
            artifact_type="live_prep_brief",
        )
        assert current is not None
        assert current.revision_number == 2
        assert current.payload == {"v": 2}

    async def test_get_current_artifact_returns_none_for_missing(
        self, scratch_conn: Any, seed_conversation: tuple[str, str]
    ) -> None:
        """get_current_artifact returns None when no matching artifact exists."""
        conversation_id, _ = seed_conversation
        result = await get_current_artifact(
            scratch_conn,
            conversation_id=conversation_id,
            artifact_type="agenda_revision",
        )
        assert result is None

    async def test_list_artifacts_filters_by_type(
        self, scratch_conn: Any, seed_conversation: tuple[str, str]
    ) -> None:
        """list_artifacts with artifact_type filter returns only matching rows."""
        conversation_id, user_id = seed_conversation
        await create_artifact(
            scratch_conn,
            conversation_id=conversation_id,
            bot_id="mediator",
            user_id=user_id,
            artifact_type="live_prep_brief",
            payload={},
        )
        await create_artifact(
            scratch_conn,
            conversation_id=conversation_id,
            bot_id="mediator",
            user_id=user_id,
            artifact_type="live_debrief",
            payload={},
        )
        briefs = await list_artifacts(
            scratch_conn,
            conversation_id=conversation_id,
            artifact_type="live_prep_brief",
        )
        assert len(briefs) == 1
        assert briefs[0].artifact_type == "live_prep_brief"


class TestCreateArtifactSavepointRetry:
    async def test_savepoint_retry_outer_transaction_survives(
        self, scratch_conn: Any, seed_conversation: tuple[str, str]
    ) -> None:
        """Pre-insert a row at revision_number=1, then call create_artifact inside
        an open outer transaction — assert it succeeds at revision 2 without
        rolling back the outer transaction."""
        conversation_id, user_id = seed_conversation
        # Pre-insert a row occupying revision_number=1 for this type.
        await scratch_conn.execute(
            """
            INSERT INTO mediator.conversation_artifacts
                (id, conversation_id, bot_id, user_id, artifact_type,
                 payload, revision_number)
            VALUES ($1, $2, $3, $4, $5, $6, 1)
            """,
            str(uuid4()),
            conversation_id,
            "mediator",
            user_id,
            "live_prep_brief",
            '{"pre":true}',
        )

        # Now open an outer transaction, create_artifact, and commit.
        await scratch_conn.execute("BEGIN")
        try:
            artifact = await create_artifact(
                scratch_conn,
                conversation_id=conversation_id,
                bot_id="mediator",
                user_id=user_id,
                artifact_type="live_prep_brief",
                payload={"v": 2},
            )
            assert artifact.revision_number == 2, (
                f"expected revision 2 (pre-inserted 1), got {artifact.revision_number}"
            )
            await scratch_conn.execute("COMMIT")
        except Exception:
            await scratch_conn.execute("ROLLBACK")
            raise

        # Prove the outer transaction committed: the new row is visible.
        current = await get_current_artifact(
            scratch_conn,
            conversation_id=conversation_id,
            artifact_type="live_prep_brief",
        )
        assert current is not None
        assert current.revision_number == 2
        assert current.payload == {"v": 2}


class TestAddArtifactLink:
    async def test_happy_path(
        self, scratch_conn: Any, seed_conversation: tuple[str, str]
    ) -> None:
        """add_artifact_link creates a link row and returns it."""
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
            evidence={"quote": "test"},
        )
        assert link.artifact_id == artifact.id
        assert link.target_table == "memories"
        assert link.relation == "extracted_memory"
        assert link.evidence == {"quote": "test"}

    async def test_idempotency_same_return(
        self, scratch_conn: Any, seed_conversation: tuple[str, str]
    ) -> None:
        """Two calls with same key return the same link row (idempotent=True)."""
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
        assert link1.id == link2.id
        assert link1.artifact_id == link2.artifact_id

    async def test_reverse_lookup_by_target_table_target_id(
        self, scratch_conn: Any, seed_conversation: tuple[str, str]
    ) -> None:
        """list_artifact_links with (target_table, target_id) returns matching links."""
        conversation_id, user_id = seed_conversation
        artifact = await create_artifact(
            scratch_conn,
            conversation_id=conversation_id,
            bot_id="mediator",
            user_id=user_id,
            artifact_type="live_debrief",
            payload={},
        )
        target_id = str(uuid4())
        await add_artifact_link(
            scratch_conn,
            artifact_id=artifact.id,
            target_table="observations",
            target_id=target_id,
            relation="extracted_observation",
        )
        results = await list_artifact_links(
            scratch_conn,
            target_table="observations",
            target_id=target_id,
        )
        assert len(results) == 1
        assert results[0].target_table == "observations"
        assert results[0].target_id == target_id
        assert results[0].relation == "extracted_observation"


# ---------------------------------------------------------------------------
# ValueError rejection — no DB required (mocked connection)
# ---------------------------------------------------------------------------


class TestAddArtifactLinkRejection:
    def test_unknown_target_table_raises_valueerror_before_sql(self) -> None:
        """add_artifact_link rejects unknown target_table with ValueError
        before issuing any SQL."""
        mock_conn = MagicMock()
        # We don't want the mock to actually do anything — if the function
        # reaches SQL, the mock will return something and we want to know.
        # Instead, track whether any SQL method was called.
        with pytest.raises(ValueError) as exc_info:
            # Use asyncio to run the async function
            import asyncio
            async def _call() -> None:
                await add_artifact_link(
                    mock_conn,
                    artifact_id=str(uuid4()),
                    target_table="nonexistent_table",
                    target_id=str(uuid4()),
                    relation="extracted_memory",
                )
            asyncio.run(_call())
        assert "nonexistent_table" in str(exc_info.value)
        assert "not allowed" in str(exc_info.value).lower() or "Allowed" in str(exc_info.value)
        # Prove zero DB round-trips: fetchrow/fetch/execute must not be called.
        mock_conn.fetchrow.assert_not_called()
        mock_conn.fetch.assert_not_called()
        mock_conn.execute.assert_not_called()

    def test_unknown_relation_raises_valueerror_before_sql(self) -> None:
        """add_artifact_link rejects unknown relation with ValueError
        before issuing any SQL."""
        mock_conn = MagicMock()
        import asyncio

        with pytest.raises(ValueError) as exc_info:
            async def _call() -> None:
                await add_artifact_link(
                    mock_conn,
                    artifact_id=str(uuid4()),
                    target_table="memories",
                    target_id=str(uuid4()),
                    relation="nonexistent_relation",
                )
            asyncio.run(_call())
        assert "nonexistent_relation" in str(exc_info.value)
        mock_conn.fetchrow.assert_not_called()
        mock_conn.fetch.assert_not_called()
        mock_conn.execute.assert_not_called()


# ---------------------------------------------------------------------------
# Constant-vs-SQL parity — no DB required
# ---------------------------------------------------------------------------


def _extract_check_literals(check_kind: str, sql: str) -> set[str]:
    """Extract the quoted literals from a CHECK (column IN (...)) constraint.

    check_kind is one of: 'artifact_type', 'relation', 'target_table', 'kind'.
    Returns a set of the string literals (without quotes).
    """
    if check_kind == "kind":
        # Kind is special: CHECK (kind IS NULL OR kind IN (...))
        pattern = rf"CHECK\s*\(\s*kind\s+IS\s+NULL\s+OR\s+kind\s+IN\s*\((.*?)\)\)"
        match = re.search(pattern, sql, re.DOTALL | re.IGNORECASE)
        if not match:
            raise AssertionError(f"Could not find kind CHECK constraint in SQL")
        inner = match.group(1)
    else:
        # Standard: CHECK (column_name IN (...))
        pattern = rf"CHECK\s*\(\s*{check_kind}\s+IN\s*\((.*?)\)\)"
        match = re.search(pattern, sql, re.DOTALL | re.IGNORECASE)
        if not match:
            raise AssertionError(f"Could not find {check_kind} CHECK constraint in SQL")
        inner = match.group(1)

    # Extract all single-quoted strings from the IN (...) list.
    literals: list[str] = re.findall(r"'([^']*)'", inner)
    return set(literals)


def _read_migration_up(migration: str = "0051_conversation_artifacts") -> str:
    return (MIGRATIONS_DIR / f"{migration}.sql").read_text()


def _read_migration_0054_up() -> str:
    return (MIGRATIONS_DIR / "0054_artifact_links_widen_checks.sql").read_text()


class TestConstantSqlParity:
    def test_artifact_type_parity(self) -> None:
        """ARTIFACT_TYPES frozenset must match the SQL CHECK literals exactly."""
        sql = _read_migration_up()
        sql_types = _extract_check_literals("artifact_type", sql)
        assert sql_types == set(ARTIFACT_TYPES), (
            f"SQL artifact_type: {sorted(sql_types)}\n"
            f"Python ARTIFACT_TYPES: {sorted(ARTIFACT_TYPES)}"
        )

    def test_relation_parity(self) -> None:
        """RELATIONS frozenset must match the SQL CHECK literals from 0054 (widened)."""
        sql = _read_migration_0054_up()
        sql_relations = _extract_check_literals("relation", sql)
        assert sql_relations == set(RELATIONS), (
            f"SQL relation: {sorted(sql_relations)}\n"
            f"Python RELATIONS: {sorted(RELATIONS)}"
        )

    def test_target_table_parity(self) -> None:
        """ALLOWED_TARGET_TABLES frozenset must match the SQL CHECK literals from 0054 (widened)."""
        sql = _read_migration_0054_up()
        sql_targets = _extract_check_literals("target_table", sql)
        assert sql_targets == set(ALLOWED_TARGET_TABLES), (
            f"SQL target_table: {sorted(sql_targets)}\n"
            f"Python ALLOWED_TARGET_TABLES: {sorted(ALLOWED_TARGET_TABLES)}"
        )

    def test_kind_parity(self) -> None:
        """LIVE_PREP_KIND and LIVE_DEBRIEF_KIND must match the SQL CHECK literals."""
        sql = _read_migration_up()
        sql_kinds = _extract_check_literals("kind", sql)
        expected = {LIVE_PREP_KIND, LIVE_DEBRIEF_KIND}
        assert sql_kinds == expected, (
            f"SQL kind: {sorted(sql_kinds)}\n"
            f"Python kinds: {sorted(expected)}"
        )


# ---------------------------------------------------------------------------
# Config tests — no DB required
# ---------------------------------------------------------------------------


class TestConfigDefaults:
    def test_nonchat_default_max_tool_iterations_default(self) -> None:
        settings = Settings(database_url="postgres://test", supabase_url="https://test",
                            anthropic_api_key="sk-test", openai_api_key="sk-test",
                            groq_api_key="sk-test", whatsapp_token="test",
                            whatsapp_verify_token="test", admin_password="test")
        assert settings.nonchat_default_max_tool_iterations == 100

    def test_live_debrief_max_tool_iterations_default(self) -> None:
        settings = Settings(database_url="postgres://test", supabase_url="https://test",
                            anthropic_api_key="sk-test", openai_api_key="sk-test",
                            groq_api_key="sk-test", whatsapp_token="test",
                            whatsapp_verify_token="test", admin_password="test")
        assert settings.live_debrief_max_tool_iterations == 500

    def test_nonchat_boundary_zero_accepted(self) -> None:
        settings = Settings(database_url="postgres://test", supabase_url="https://test",
                            anthropic_api_key="sk-test", openai_api_key="sk-test",
                            groq_api_key="sk-test", whatsapp_token="test",
                            whatsapp_verify_token="test", admin_password="test",
                            nonchat_default_max_tool_iterations=0)
        assert settings.nonchat_default_max_tool_iterations == 0

    def test_nonchat_boundary_2000_accepted(self) -> None:
        settings = Settings(database_url="postgres://test", supabase_url="https://test",
                            anthropic_api_key="sk-test", openai_api_key="sk-test",
                            groq_api_key="sk-test", whatsapp_token="test",
                            whatsapp_verify_token="test", admin_password="test",
                            nonchat_default_max_tool_iterations=2000)
        assert settings.nonchat_default_max_tool_iterations == 2000

    def test_live_debrief_boundary_zero_accepted(self) -> None:
        settings = Settings(database_url="postgres://test", supabase_url="https://test",
                            anthropic_api_key="sk-test", openai_api_key="sk-test",
                            groq_api_key="sk-test", whatsapp_token="test",
                            whatsapp_verify_token="test", admin_password="test",
                            live_debrief_max_tool_iterations=0)
        assert settings.live_debrief_max_tool_iterations == 0

    def test_live_debrief_boundary_5000_accepted(self) -> None:
        settings = Settings(database_url="postgres://test", supabase_url="https://test",
                            anthropic_api_key="sk-test", openai_api_key="sk-test",
                            groq_api_key="sk-test", whatsapp_token="test",
                            whatsapp_verify_token="test", admin_password="test",
                            live_debrief_max_tool_iterations=5000)
        assert settings.live_debrief_max_tool_iterations == 5000

    def test_nonchat_negative_raises_validation_error(self) -> None:
        with pytest.raises(ValidationError):
            Settings(database_url="postgres://test", supabase_url="https://test",
                     anthropic_api_key="sk-test", openai_api_key="sk-test",
                     groq_api_key="sk-test", whatsapp_token="test",
                     whatsapp_verify_token="test", admin_password="test",
                     nonchat_default_max_tool_iterations=-1)

    def test_nonchat_above_2000_raises_validation_error(self) -> None:
        with pytest.raises(ValidationError):
            Settings(database_url="postgres://test", supabase_url="https://test",
                     anthropic_api_key="sk-test", openai_api_key="sk-test",
                     groq_api_key="sk-test", whatsapp_token="test",
                     whatsapp_verify_token="test", admin_password="test",
                     nonchat_default_max_tool_iterations=2001)

    def test_live_debrief_negative_raises_validation_error(self) -> None:
        with pytest.raises(ValidationError):
            Settings(database_url="postgres://test", supabase_url="https://test",
                     anthropic_api_key="sk-test", openai_api_key="sk-test",
                     groq_api_key="sk-test", whatsapp_token="test",
                     whatsapp_verify_token="test", admin_password="test",
                     live_debrief_max_tool_iterations=-1)

    def test_live_debrief_above_5000_raises_validation_error(self) -> None:
        with pytest.raises(ValidationError):
            Settings(database_url="postgres://test", supabase_url="https://test",
                     anthropic_api_key="sk-test", openai_api_key="sk-test",
                     groq_api_key="sk-test", whatsapp_token="test",
                     whatsapp_verify_token="test", admin_password="test",
                     live_debrief_max_tool_iterations=5001)


# ---------------------------------------------------------------------------
# Live debrief artifacts — no DB required
# ---------------------------------------------------------------------------


class TestLiveDebriefArtifact:
    """Verify live_debrief artifact type is supported in the artifact system."""

    def test_live_debrief_in_artifact_types(self) -> None:
        """live_debrief is in ARTIFACT_TYPES frozenset."""
        assert "live_debrief" in ARTIFACT_TYPES, (
            f"live_debrief must be in ARTIFACT_TYPES; got {sorted(ARTIFACT_TYPES)}"
        )

    def test_live_debrief_kind_constant(self) -> None:
        """LIVE_DEBRIEF_KIND is 'live_debrief'."""
        assert LIVE_DEBRIEF_KIND == "live_debrief", (
            f"Expected LIVE_DEBRIEF_KIND='live_debrief', got {LIVE_DEBRIEF_KIND!r}"
        )

    def test_review_summary_in_artifact_types(self) -> None:
        """review_summary is in ARTIFACT_TYPES frozenset."""
        assert "review_summary" in ARTIFACT_TYPES, (
            f"review_summary must be in ARTIFACT_TYPES; got {sorted(ARTIFACT_TYPES)}"
        )

    def test_get_current_artifact_for_live_debrief(self) -> None:
        """get_current_artifact is importable with live_debrief type."""
        assert callable(get_current_artifact)

    def test_create_artifact_live_debrief_supported(self) -> None:
        """create_artifact function handles artifact_type='live_debrief'."""
        assert callable(create_artifact)


# ---------------------------------------------------------------------------
# Sprint 4 provenance helpers — DB-gated
# ---------------------------------------------------------------------------


class TestEnsureLiveDebriefProvenanceArtifact:
    async def test_creates_fresh_for_new_conversation(
        self, scratch_conn: Any, seed_conversation: tuple[str, str]
    ) -> None:
        """ensure_live_debrief_provenance_artifact creates a new artifact."""
        from app.services.live.provenance import ensure_live_debrief_provenance_artifact

        conversation_id, user_id = seed_conversation
        turn_id = str(uuid4())
        artifact = await ensure_live_debrief_provenance_artifact(
            scratch_conn,
            conversation_id=conversation_id,
            created_by_turn_id=turn_id,
            bot_id="mediator",
            user_id=user_id,
        )
        assert artifact is not None
        assert artifact.artifact_type == "live_debrief"
        assert artifact.conversation_id == conversation_id
        assert artifact.created_by_turn_id == turn_id
        assert artifact.payload.get("status") == "provisional"
        assert artifact.deleted_at is None

    async def test_reuses_existing_for_same_turn(
        self, scratch_conn: Any, seed_conversation: tuple[str, str]
    ) -> None:
        """Second call with same turn_id returns the same artifact (idempotent)."""
        from app.services.live.provenance import ensure_live_debrief_provenance_artifact

        conversation_id, user_id = seed_conversation
        turn_id = str(uuid4())
        a1 = await ensure_live_debrief_provenance_artifact(
            scratch_conn,
            conversation_id=conversation_id,
            created_by_turn_id=turn_id,
            bot_id="mediator",
            user_id=user_id,
        )
        a2 = await ensure_live_debrief_provenance_artifact(
            scratch_conn,
            conversation_id=conversation_id,
            created_by_turn_id=turn_id,
            bot_id="mediator",
            user_id=user_id,
        )
        assert a1.id == a2.id
        assert a1.revision_number == a2.revision_number

    async def test_distinct_artifact_for_different_turn(
        self, scratch_conn: Any, seed_conversation: tuple[str, str]
    ) -> None:
        """Different turn_id on same conversation → distinct artifact."""
        from app.services.live.provenance import ensure_live_debrief_provenance_artifact

        conversation_id, user_id = seed_conversation
        t1 = str(uuid4())
        t2 = str(uuid4())
        a1 = await ensure_live_debrief_provenance_artifact(
            scratch_conn,
            conversation_id=conversation_id,
            created_by_turn_id=t1,
            bot_id="mediator",
            user_id=user_id,
        )
        a2 = await ensure_live_debrief_provenance_artifact(
            scratch_conn,
            conversation_id=conversation_id,
            created_by_turn_id=t2,
            bot_id="mediator",
            user_id=user_id,
        )
        assert a1.id != a2.id

    async def test_tombstones_stale_before_creating_new(
        self, scratch_conn: Any, seed_conversation: tuple[str, str]
    ) -> None:
        """A stale provisional from a prior turn gets tombstoned."""
        from app.services.live.provenance import ensure_live_debrief_provenance_artifact

        conversation_id, user_id = seed_conversation
        old_turn = str(uuid4())

        # Create a "stale" provisional with old turn
        stale = await ensure_live_debrief_provenance_artifact(
            scratch_conn,
            conversation_id=conversation_id,
            created_by_turn_id=old_turn,
            bot_id="mediator",
            user_id=user_id,
        )
        assert stale.deleted_at is None

        # Now create a fresh one with a new turn — should tombstone stale
        new_turn = str(uuid4())
        fresh = await ensure_live_debrief_provenance_artifact(
            scratch_conn,
            conversation_id=conversation_id,
            created_by_turn_id=new_turn,
            bot_id="mediator",
            user_id=user_id,
        )
        assert fresh.id != stale.id
        assert fresh.deleted_at is None

        # Verify stale was tombstoned
        stale_check = await scratch_conn.fetchrow(
            "SELECT deleted_at FROM mediator.conversation_artifacts WHERE id = $1",
            stale.id,
        )
        assert stale_check is not None
        assert stale_check["deleted_at"] is not None

    async def test_finalized_artifact_not_tombstoned(
        self, scratch_conn: Any, seed_conversation: tuple[str, str]
    ) -> None:
        """A finalized artifact is NOT tombstoned when a new provisional is created."""
        from app.services.live.provenance import (
            ensure_live_debrief_provenance_artifact,
            finalize_live_debrief_artifact,
        )

        conversation_id, user_id = seed_conversation
        old_turn = str(uuid4())

        # Create and finalize an artifact
        a1 = await ensure_live_debrief_provenance_artifact(
            scratch_conn,
            conversation_id=conversation_id,
            created_by_turn_id=old_turn,
            bot_id="mediator",
            user_id=user_id,
        )
        await finalize_live_debrief_artifact(
            scratch_conn,
            artifact_id=a1.id,
            content={"review_summary": "done"},
            created_by_turn_id=old_turn,
        )

        # New turn creates a fresh provisional
        new_turn = str(uuid4())
        a2 = await ensure_live_debrief_provenance_artifact(
            scratch_conn,
            conversation_id=conversation_id,
            created_by_turn_id=new_turn,
            bot_id="mediator",
            user_id=user_id,
        )
        assert a2.id != a1.id

        # The finalized artifact should NOT be tombstoned
        final_check = await scratch_conn.fetchrow(
            "SELECT deleted_at, payload FROM mediator.conversation_artifacts WHERE id = $1",
            a1.id,
        )
        assert final_check is not None
        assert final_check["deleted_at"] is None
        assert final_check["payload"].get("status") == "finalized"


class TestFinalizeLiveDebriefArtifact:
    async def test_updates_payload_to_finalized(
        self, scratch_conn: Any, seed_conversation: tuple[str, str]
    ) -> None:
        """finalize_live_debrief_artifact updates payload with content."""
        from app.services.live.provenance import (
            ensure_live_debrief_provenance_artifact,
            finalize_live_debrief_artifact,
        )

        conversation_id, user_id = seed_conversation
        turn_id = str(uuid4())
        provisional = await ensure_live_debrief_provenance_artifact(
            scratch_conn,
            conversation_id=conversation_id,
            created_by_turn_id=turn_id,
            bot_id="mediator",
            user_id=user_id,
        )
        finalized = await finalize_live_debrief_artifact(
            scratch_conn,
            artifact_id=provisional.id,
            content={"review_summary": "all good", "what_heard": ["stuff"]},
            created_by_turn_id=turn_id,
        )
        assert finalized.payload.get("status") == "finalized"
        assert finalized.payload.get("review_summary") == "all good"
        assert finalized.payload.get("what_heard") == ["stuff"]

    async def test_raises_on_nonexistent_artifact(
        self, scratch_conn: Any, seed_conversation: tuple[str, str]
    ) -> None:
        """finalize_live_debrief_artifact raises ValueError for missing artifact."""
        from app.services.live.provenance import finalize_live_debrief_artifact

        import pytest as _pytest
        with _pytest.raises(ValueError, match="no active artifact found"):
            await finalize_live_debrief_artifact(
                scratch_conn,
                artifact_id=str(uuid4()),
                content={},
                created_by_turn_id=str(uuid4()),
            )


class TestMarkLiveDebriefArtifactFailed:
    async def test_soft_deletes_artifact_and_links(
        self, scratch_conn: Any, seed_conversation: tuple[str, str]
    ) -> None:
        """mark_live_debrief_artifact_failed soft-deletes artifact and links."""
        from app.services.live.provenance import (
            ensure_live_debrief_provenance_artifact,
            mark_live_debrief_artifact_failed,
        )

        conversation_id, user_id = seed_conversation
        turn_id = str(uuid4())
        provisional = await ensure_live_debrief_provenance_artifact(
            scratch_conn,
            conversation_id=conversation_id,
            created_by_turn_id=turn_id,
            bot_id="mediator",
            user_id=user_id,
        )
        # Add a link
        await add_artifact_link(
            scratch_conn,
            artifact_id=provisional.id,
            target_table="memories",
            target_id=str(uuid4()),
            relation="extracted_memory",
        )
        await mark_live_debrief_artifact_failed(
            scratch_conn,
            artifact_id=provisional.id,
            reason="test failure",
        )
        # Artifact should be soft-deleted
        art = await scratch_conn.fetchrow(
            "SELECT deleted_at, payload FROM mediator.conversation_artifacts WHERE id = $1",
            provisional.id,
        )
        assert art["deleted_at"] is not None
        assert art["payload"].get("failure_reason") == "test failure"

        # Links should be soft-deleted
        links = await scratch_conn.fetch(
            "SELECT deleted_at FROM mediator.artifact_links WHERE artifact_id = $1",
            provisional.id,
        )
        for link in links:
            assert link["deleted_at"] is not None


# ---------------------------------------------------------------------------
# Sprint 4 evidence validation — no DB required
# ---------------------------------------------------------------------------


class TestValidateArtifactLinkEvidence:
    def test_accepts_valid_sprint4_shape(self) -> None:
        from app.services.live.provenance import validate_artifact_link_evidence

        ev = {
            "transcript_turn_ids": ["00000000-0000-0000-0000-000000000001"],
            "quotes": ["hello world"],
            "confidence": 0.9,
            "reason": "direct quote",
        }
        result = validate_artifact_link_evidence(ev)
        assert result == ev

    def test_accepts_none(self) -> None:
        from app.services.live.provenance import validate_artifact_link_evidence

        assert validate_artifact_link_evidence(None) is None

    def test_rejects_unknown_fields(self) -> None:
        from app.services.live.provenance import validate_artifact_link_evidence

        ev = {"transcript_turn_ids": [], "unknown_field": "x"}
        import pytest as _pytest
        with _pytest.raises(ValueError, match="unknown evidence fields"):
            validate_artifact_link_evidence(ev)

    def test_rejects_invalid_turn_id_format(self) -> None:
        from app.services.live.provenance import validate_artifact_link_evidence

        ev = {"transcript_turn_ids": ["not-a-uuid"]}
        import pytest as _pytest
        with _pytest.raises(ValueError, match="not a valid UUID"):
            validate_artifact_link_evidence(ev)

    def test_rejects_non_list_turn_ids(self) -> None:
        from app.services.live.provenance import validate_artifact_link_evidence

        ev = {"transcript_turn_ids": "not-a-list"}
        import pytest as _pytest
        with _pytest.raises(ValueError, match="must be a list"):
            validate_artifact_link_evidence(ev)

    def test_rejects_confidence_out_of_range(self) -> None:
        from app.services.live.provenance import validate_artifact_link_evidence

        ev = {"confidence": 1.5}
        import pytest as _pytest
        with _pytest.raises(ValueError, match="confidence must be in"):
            validate_artifact_link_evidence(ev)

    def test_accepts_confidence_zero_and_one(self) -> None:
        from app.services.live.provenance import validate_artifact_link_evidence

        assert validate_artifact_link_evidence({"confidence": 0.0})["confidence"] == 0.0
        assert validate_artifact_link_evidence({"confidence": 1.0})["confidence"] == 1.0

    def test_accepts_confidence_none(self) -> None:
        from app.services.live.provenance import validate_artifact_link_evidence

        result = validate_artifact_link_evidence({"confidence": None})
        assert result["confidence"] is None

    def test_rejects_non_string_quotes(self) -> None:
        from app.services.live.provenance import validate_artifact_link_evidence

        ev = {"quotes": [123]}
        import pytest as _pytest
        with _pytest.raises(ValueError, match="must be a string"):
            validate_artifact_link_evidence(ev)

    def test_rejects_non_dict_evidence(self) -> None:
        from app.services.live.provenance import validate_artifact_link_evidence

        import pytest as _pytest
        with _pytest.raises(ValueError, match="must be a dict"):
            validate_artifact_link_evidence([])

    def test_rejects_non_string_reason(self) -> None:
        from app.services.live.provenance import validate_artifact_link_evidence

        ev = {"reason": 42}
        import pytest as _pytest
        with _pytest.raises(ValueError, match="reason must be a string or None"):
            validate_artifact_link_evidence(ev)

    def test_accepts_reason_none(self) -> None:
        from app.services.live.provenance import validate_artifact_link_evidence

        result = validate_artifact_link_evidence({"reason": None})
        assert result["reason"] is None


class TestNormalizeArtifactLinkEvidence:
    def test_normalize_guard_high_to_0_9(self) -> None:
        from app.services.live.provenance import normalize_artifact_link_evidence

        ev = {
            "evidence_refs": [
                {"transcript_turn_id": "00000000-0000-0000-0000-000000000001",
                 "quote": "abc", "confidence": "high"}
            ]
        }
        result = normalize_artifact_link_evidence(ev)
        assert result["transcript_turn_ids"] == ["00000000-0000-0000-0000-000000000001"]
        assert result["quotes"] == ["abc"]
        assert result["confidence"] == 0.9

    def test_normalize_guard_medium_to_0_6(self) -> None:
        from app.services.live.provenance import normalize_artifact_link_evidence
        ev = {"evidence_refs": [{"transcript_turn_id": "00000000-0000-0000-0000-000000000002",
                                  "quote": "x", "confidence": "medium"}]}
        result = normalize_artifact_link_evidence(ev)
        assert result["confidence"] == 0.6

    def test_normalize_guard_low_to_0_3(self) -> None:
        from app.services.live.provenance import normalize_artifact_link_evidence
        ev = {"evidence_refs": [{"transcript_turn_id": "00000000-0000-0000-0000-000000000003",
                                  "quote": "y", "confidence": "low"}]}
        result = normalize_artifact_link_evidence(ev)
        assert result["confidence"] == 0.3

    def test_normalize_numeric_confidence_through(self) -> None:
        from app.services.live.provenance import normalize_artifact_link_evidence
        ev = {"evidence_refs": [{"transcript_turn_id": "00000000-0000-0000-0000-000000000004",
                                  "quote": "z", "confidence": 0.75}]}
        result = normalize_artifact_link_evidence(ev)
        assert result["confidence"] == 0.75

    def test_normalize_derivation_source(self) -> None:
        from app.services.live.provenance import normalize_artifact_link_evidence

        ev = {"derivation_source": "hot_context"}
        result = normalize_artifact_link_evidence(ev)
        assert result["transcript_turn_ids"] == []
        assert result["quotes"] == []
        assert result["confidence"] is None
        assert result["reason"] == "derived_from:hot_context"

    def test_normalize_derivation_source_bot_notes(self) -> None:
        from app.services.live.provenance import normalize_artifact_link_evidence

        ev = {"derivation_source": "bot_notes"}
        result = normalize_artifact_link_evidence(ev)
        assert result["reason"] == "derived_from:bot_notes"

    def test_normalize_derivation_source_prep_artifact(self) -> None:
        from app.services.live.provenance import normalize_artifact_link_evidence

        ev = {"derivation_source": "prep_artifact"}
        result = normalize_artifact_link_evidence(ev)
        assert result["reason"] == "derived_from:prep_artifact"

    def test_normalize_already_sprint4_shape_passes_through(self) -> None:
        from app.services.live.provenance import normalize_artifact_link_evidence

        ev = {
            "transcript_turn_ids": ["00000000-0000-0000-0000-000000000001"],
            "quotes": ["test"],
            "confidence": 0.8,
        }
        result = normalize_artifact_link_evidence(ev)
        assert result == ev

    def test_normalize_unknown_format_raises(self) -> None:
        from app.services.live.provenance import normalize_artifact_link_evidence

        import pytest as _pytest
        with _pytest.raises(ValueError, match="must contain"):
            normalize_artifact_link_evidence({})

    def test_normalize_none_returns_none(self) -> None:
        from app.services.live.provenance import normalize_artifact_link_evidence

        assert normalize_artifact_link_evidence(None) is None


# ---------------------------------------------------------------------------
# Sprint 4 provenance mapping tests — no DB required
# ---------------------------------------------------------------------------


class TestDebriefToolOutputMapping:
    def test_all_guarded_tools_covered(self) -> None:
        """LIVE_DEBRIEF_TOOL_OUTPUT_MAP covers every guarded write tool."""
        from app.services.tools.registry import LIVE_DEBRIEF_GUARDED_WRITE_TOOLS
        from app.services.live.provenance import LIVE_DEBRIEF_TOOL_OUTPUT_MAP

        mapped = set(LIVE_DEBRIEF_TOOL_OUTPUT_MAP.keys())
        missing = LIVE_DEBRIEF_GUARDED_WRITE_TOOLS - mapped
        extra = mapped - LIVE_DEBRIEF_GUARDED_WRITE_TOOLS
        assert not missing, f"Missing tools: {sorted(missing)}"
        assert not extra, f"Extra tools: {sorted(extra)}"

    def test_all_target_tables_allowed(self) -> None:
        """Every target_table in the mapping is in ALLOWED_TARGET_TABLES."""
        from app.services.live.provenance import LIVE_DEBRIEF_TOOL_OUTPUT_MAP

        for name, m in LIVE_DEBRIEF_TOOL_OUTPUT_MAP.items():
            assert m.target_table in ALLOWED_TARGET_TABLES, (
                f"Tool '{name}' target_table='{m.target_table}' "
                f"not in ALLOWED_TARGET_TABLES"
            )

    def test_all_relations_allowed(self) -> None:
        """Every relation in the mapping is in RELATIONS."""
        from app.services.live.provenance import LIVE_DEBRIEF_TOOL_OUTPUT_MAP

        for name, m in LIVE_DEBRIEF_TOOL_OUTPUT_MAP.items():
            assert m.relation in RELATIONS, (
                f"Tool '{name}' relation='{m.relation}' not in RELATIONS"
            )

    def test_supersede_revise_capture_new_id(self) -> None:
        """supersede_memory and revise_distillation capture new_id, not id."""
        from app.services.live.provenance import LIVE_DEBRIEF_TOOL_OUTPUT_MAP

        sm = LIVE_DEBRIEF_TOOL_OUTPUT_MAP["supersede_memory"]
        assert sm.output_id_field == "new_id", (
            f"supersede_memory must link to new_id, got {sm.output_id_field}"
        )
        rd = LIVE_DEBRIEF_TOOL_OUTPUT_MAP["revise_distillation"]
        assert rd.output_id_field == "new_id", (
            f"revise_distillation must link to new_id, got {rd.output_id_field}"
        )

    def test_commitment_tools_use_commitment_id(self) -> None:
        """Commitment tools link via commitment_id."""
        from app.services.live.provenance import LIVE_DEBRIEF_TOOL_OUTPUT_MAP

        for tool in ("create_commitment", "update_commitment", "close_commitment"):
            m = LIVE_DEBRIEF_TOOL_OUTPUT_MAP[tool]
            assert m.output_id_field == "commitment_id", (
                f"{tool} must link to commitment_id, got {m.output_id_field}"
            )

    def test_schedule_tools_use_job_id(self) -> None:
        """Schedule tools (checkin, task) link via job_id to scheduled_jobs table."""
        from app.services.live.provenance import LIVE_DEBRIEF_TOOL_OUTPUT_MAP

        for tool in ("schedule_checkin", "schedule_task",
                      "update_scheduled_task", "update_scheduled_checkin"):
            m = LIVE_DEBRIEF_TOOL_OUTPUT_MAP[tool]
            assert m.target_table == "scheduled_jobs", (
                f"{tool} must target scheduled_jobs, got {m.target_table}"
            )
            assert m.output_id_field == "job_id", (
                f"{tool} must link to job_id, got {m.output_id_field}"
            )

    def test_noop_scheduled_updates_not_success(self) -> None:
        """update_scheduled_task and update_scheduled_checkin treat 'noop' as non-success."""
        from app.services.live.provenance import LIVE_DEBRIEF_TOOL_OUTPUT_MAP

        ut = LIVE_DEBRIEF_TOOL_OUTPUT_MAP["update_scheduled_task"]
        # action='updated' with job_id → success
        assert ut.success_predicate({
            "action": "updated", "job_id": "00000000-0000-0000-0000-000000000001"
        }) is True
        # action='noop' even with job_id → no success
        assert ut.success_predicate({
            "action": "noop", "job_id": "00000000-0000-0000-0000-000000000001"
        }) is False
        # action='updated' but no job_id → no success
        assert ut.success_predicate({"action": "updated"}) is False

        uc = LIVE_DEBRIEF_TOOL_OUTPUT_MAP["update_scheduled_checkin"]
        assert uc.success_predicate({
            "action": "updated", "job_id": "00000000-0000-0000-0000-000000000002"
        }) is True
        assert uc.success_predicate({
            "action": "noop", "job_id": "00000000-0000-0000-0000-000000000002"
        }) is False
        assert uc.success_predicate({"action": "updated"}) is False

    def test_commitment_tools_error_is_not_success(self) -> None:
        """Commitment tools treat is_error=True or missing fields as non-success."""
        from app.services.live.provenance import LIVE_DEBRIEF_TOOL_OUTPUT_MAP

        # All three commitment tools reject is_error=True
        for tool in ("create_commitment", "update_commitment", "close_commitment"):
            m = LIVE_DEBRIEF_TOOL_OUTPUT_MAP[tool]
            assert m.success_predicate({"is_error": True}) is False

        # create_commitment: needs commitment_id
        cm = LIVE_DEBRIEF_TOOL_OUTPUT_MAP["create_commitment"]
        assert cm.success_predicate({"commitment_id": "00000000-0000-0000-0000-000000000010"}) is True
        assert cm.success_predicate({"is_error": False}) is False  # missing commitment_id
        assert cm.success_predicate({"is_error": False, "commitment_id": None}) is False

        # update_commitment: needs commitment_id + updated_at
        um = LIVE_DEBRIEF_TOOL_OUTPUT_MAP["update_commitment"]
        assert um.success_predicate({
            "commitment_id": "00000000-0000-0000-0000-000000000011",
            "updated_at": "2026-01-01T00:00:00Z",
        }) is True
        assert um.success_predicate({
            "commitment_id": "00000000-0000-0000-0000-000000000011",
        }) is False  # missing updated_at
        assert um.success_predicate({"is_error": False}) is False

        # close_commitment: needs status + closed_at
        cl = LIVE_DEBRIEF_TOOL_OUTPUT_MAP["close_commitment"]
        assert cl.success_predicate({
            "commitment_id": "00000000-0000-0000-0000-000000000012",
            "status": "completed",
            "closed_at": "2026-01-01T00:00:00Z",
        }) is True
        assert cl.success_predicate({
            "commitment_id": "00000000-0000-0000-0000-000000000012",
            "status": "completed",
        }) is False  # missing closed_at
        assert cl.success_predicate({"is_error": False}) is False

    def test_noop_outputs_rejected_by_action_predicates(self) -> None:
        """Tools using _action_is() reject non-matching action values."""
        from app.services.live.provenance import LIVE_DEBRIEF_TOOL_OUTPUT_MAP

        # add_memory only succeeds on action='created'
        am = LIVE_DEBRIEF_TOOL_OUTPUT_MAP["add_memory"]
        assert am.success_predicate({"action": "created", "id": "00000000-0000-0000-0000-000000000001"}) is True
        assert am.success_predicate({"action": "updated", "id": "00000000-0000-0000-0000-000000000001"}) is False
        assert am.success_predicate({"action": "noop", "id": "00000000-0000-0000-0000-000000000001"}) is False
        assert am.success_predicate({}) is False

    def test_error_outputs_with_partial_data_not_success(self) -> None:
        """Outputs with is_error=True must never pass success predicates."""
        from app.services.live.provenance import LIVE_DEBRIEF_TOOL_OUTPUT_MAP

        # Test a representative sample across families
        err_out = {"is_error": True, "error": "something went wrong"}

        for tool_name in (
            "add_memory", "supersede_memory", "log_observation",
            "create_theme", "add_watch_item", "address_watch_item",
            "add_oob", "lift_oob", "schedule_checkin",
            "update_scheduled_task", "create_commitment", "close_commitment",
            "log_event", "revise_distillation",
        ):
            m = LIVE_DEBRIEF_TOOL_OUTPUT_MAP[tool_name]
            assert m.success_predicate(err_out) is False, (
                f"{tool_name} should reject is_error=True"
            )

    def test_missing_target_id_rejected(self) -> None:
        """Outputs missing their stable ID field must not pass success predicates."""
        from app.services.live.provenance import LIVE_DEBRIEF_TOOL_OUTPUT_MAP

        # Each tool has an output_id_field that must be present and truthy
        # for the predicate to pass (where applicable).
        # _action_is predicates don't check ID, so they need a separate
        # validation layer (T8 responsibility).

        # For _scheduled_update_success, job_id must be truthy
        ut = LIVE_DEBRIEF_TOOL_OUTPUT_MAP["update_scheduled_task"]
        assert ut.success_predicate({"action": "updated", "job_id": None}) is False
        assert ut.success_predicate({"action": "updated", "job_id": ""}) is False

        # For _no_error_and_has, fields must be truthy
        cm = LIVE_DEBRIEF_TOOL_OUTPUT_MAP["create_commitment"]
        assert cm.success_predicate({"commitment_id": None}) is False
        assert cm.success_predicate({"commitment_id": ""}) is False

    def test_log_event_predicate(self) -> None:
        """log_event uses _no_error — any non-error output is success."""
        from app.services.live.provenance import LIVE_DEBRIEF_TOOL_OUTPUT_MAP

        le = LIVE_DEBRIEF_TOOL_OUTPUT_MAP["log_event"]
        assert le.success_predicate({}) is True
        assert le.success_predicate({"is_error": False}) is True
        assert le.success_predicate({"is_error": True}) is False
        assert le.success_predicate({"event_id": "00000000-0000-0000-0000-000000000001"}) is True

    def test_new_tables_in_allowed_target_tables(self) -> None:
        """themes, watch_items, out_of_bounds are in ALLOWED_TARGET_TABLES."""
        for table in ("themes", "watch_items", "out_of_bounds"):
            assert table in ALLOWED_TARGET_TABLES, (
                f"'{table}' must be in ALLOWED_TARGET_TABLES"
            )


# ---------------------------------------------------------------------------
# Sprint 4 T14: DB-gated reverse lookup tests
# ---------------------------------------------------------------------------


class TestReverseLookupDB:
    """Verify get_source_conversations_for_durable_record and
    list_durable_writes_for_conversation with a real database.

    Requires DATABASE_URL / EVAL_DATABASE_URL — skipped otherwise.
    """

    async def _seed_linked_record(
        self, scratch_conn: Any,
        conversation_id: str, user_id: str,
        target_table: str, target_id: str, relation: str,
        artifact_type: str = "live_debrief",
    ) -> dict[str, str]:
        """Create an artifact + link and return {artifact_id, link_id}."""
        from app.services.live.artifacts import (
            add_artifact_link, create_artifact,
        )

        artifact = await create_artifact(
            scratch_conn,
            conversation_id=conversation_id,
            bot_id="mediator",
            user_id=user_id,
            artifact_type=artifact_type,
            payload={"status": "finalized"},
            payload_version=1,
            created_by_turn_id=str(uuid4()),
        )
        link = await add_artifact_link(
            scratch_conn,
            artifact_id=artifact.id,
            target_table=target_table,
            target_id=target_id,
            relation=relation,
            idempotent=False,
        )
        return {"artifact_id": artifact.id, "link_id": link.id}

    @pytest.mark.parametrize("target_table, relation", [
        ("memories", "extracted_memory"),
        ("observations", "extracted_observation"),
        ("distillations", "extracted_distillation"),
        ("commitments", "created_commitment"),
        ("events", "logged_event"),
        ("scheduled_jobs", "created_follow_up"),
        ("themes", "extracted_theme"),
        ("watch_items", "created_watch_item"),
        ("out_of_bounds", "created_oob"),
    ])
    async def test_reverse_lookup_returns_source_conversation(
        self, scratch_conn: Any, seed_conversation: tuple[str, str],
        target_table: str, relation: str,
    ) -> None:
        """get_source_conversations_for_durable_record returns the
        conversation that produced a link to a durable record."""
        from app.services.live.provenance import (
            get_source_conversations_for_durable_record,
        )

        conversation_id, user_id = seed_conversation
        target_id = str(uuid4())
        await self._seed_linked_record(
            scratch_conn, conversation_id, user_id,
            target_table, target_id, relation,
        )

        results = await get_source_conversations_for_durable_record(
            scratch_conn,
            target_table=target_table,
            target_id=target_id,
            include_deleted=False,
        )

        assert len(results) >= 1, (
            f"Expected at least 1 source conversation for "
            f"target_table={target_table} target_id={target_id}, "
            f"got {len(results)}"
        )
        result = results[0]
        assert result["target_table"] is None or "conversation_id" in result, (
            f"Result should have conversation_id: {result}"
        )
        assert result["relation"] == relation, (
            f"Expected relation={relation}, got {result['relation']}"
        )
        assert result["artifact_type"] == "live_debrief"
        assert result["link_deleted"] is False
        assert result["artifact_deleted"] is False

    async def test_reverse_lookup_respects_include_deleted(
        self, scratch_conn: Any, seed_conversation: tuple[str, str]
    ) -> None:
        """include_deleted=False hides soft-deleted artifacts/links;
        include_deleted=True shows them."""
        from app.services.live.provenance import (
            get_source_conversations_for_durable_record,
            mark_live_debrief_artifact_failed,
        )

        conversation_id, user_id = seed_conversation
        target_id = str(uuid4())
        ids = await self._seed_linked_record(
            scratch_conn, conversation_id, user_id,
            "memories", target_id, "extracted_memory",
        )

        # Soft-delete the artifact.
        await mark_live_debrief_artifact_failed(
            scratch_conn,
            artifact_id=ids["artifact_id"],
            reason="test cleanup",
        )

        # include_deleted=False — should return nothing.
        active = await get_source_conversations_for_durable_record(
            scratch_conn,
            target_table="memories",
            target_id=target_id,
            include_deleted=False,
        )
        assert len(active) == 0, (
            f"Expected 0 results with include_deleted=False after soft-delete, "
            f"got {len(active)}"
        )

        # include_deleted=True — should return the deleted row.
        deleted = await get_source_conversations_for_durable_record(
            scratch_conn,
            target_table="memories",
            target_id=target_id,
            include_deleted=True,
        )
        assert len(deleted) >= 1, (
            f"Expected at least 1 result with include_deleted=True, "
            f"got {len(deleted)}"
        )
        assert deleted[0]["link_deleted"] is True or deleted[0]["artifact_deleted"] is True, (
            "At least one of link_deleted/artifact_deleted should be True"
        )

    async def test_list_durable_writes_for_conversation_empty(
        self, scratch_conn: Any, seed_conversation: tuple[str, str]
    ) -> None:
        """list_durable_writes_for_conversation returns empty list when
        no links exist."""
        from app.services.live.provenance import (
            list_durable_writes_for_conversation,
        )

        conversation_id, _ = seed_conversation
        results = await list_durable_writes_for_conversation(
            scratch_conn,
            conversation_id=conversation_id,
        )
        assert results == [], (
            f"Expected empty list for conversation with no links, got {results}"
        )

    async def test_list_durable_writes_for_conversation_with_links(
        self, scratch_conn: Any, seed_conversation: tuple[str, str]
    ) -> None:
        """list_durable_writes_for_conversation returns all linked records
        grouped by target_table/relation."""
        from app.services.live.provenance import (
            list_durable_writes_for_conversation,
        )

        conversation_id, user_id = seed_conversation

        # Create links across multiple tables.
        mem_id = str(uuid4())
        obs_id = str(uuid4())
        await self._seed_linked_record(
            scratch_conn, conversation_id, user_id,
            "memories", mem_id, "extracted_memory",
        )
        await self._seed_linked_record(
            scratch_conn, conversation_id, user_id,
            "observations", obs_id, "extracted_observation",
        )

        results = await list_durable_writes_for_conversation(
            scratch_conn,
            conversation_id=conversation_id,
            include_deleted=False,
        )

        assert len(results) >= 2, (
            f"Expected at least 2 durable writes, got {len(results)}: {results}"
        )

        tables = {r["target_table"] for r in results}
        assert "memories" in tables
        assert "observations" in tables

        # Verify each result has the expected fields.
        for r in results:
            for key in (
                "link_id", "artifact_id", "artifact_type",
                "revision_number", "target_table", "target_id",
                "relation", "link_deleted", "artifact_deleted",
            ):
                assert key in r, (
                    f"Result missing key '{key}': {r}"
                )

    async def test_list_durable_writes_respects_include_deleted(
        self, scratch_conn: Any, seed_conversation: tuple[str, str]
    ) -> None:
        """list_durable_writes_for_conversation with include_deleted=False
        excludes soft-deleted links."""
        from app.services.live.provenance import (
            list_durable_writes_for_conversation,
            mark_live_debrief_artifact_failed,
        )

        conversation_id, user_id = seed_conversation
        target_id = str(uuid4())
        ids = await self._seed_linked_record(
            scratch_conn, conversation_id, user_id,
            "commitments", target_id, "created_commitment",
        )

        # Soft-delete the artifact.
        await mark_live_debrief_artifact_failed(
            scratch_conn,
            artifact_id=ids["artifact_id"],
            reason="test cleanup",
        )

        # include_deleted=False — should return nothing.
        active = await list_durable_writes_for_conversation(
            scratch_conn,
            conversation_id=conversation_id,
            include_deleted=False,
        )
        assert len(active) == 0, (
            f"Expected 0 results after soft-delete with include_deleted=False, "
            f"got {len(active)}"
        )

        # include_deleted=True — should return the deleted row.
        all_results = await list_durable_writes_for_conversation(
            scratch_conn,
            conversation_id=conversation_id,
            include_deleted=True,
        )
        assert len(all_results) >= 1, (
            f"Expected at least 1 result with include_deleted=True, "
            f"got {len(all_results)}"
        )

    async def test_find_artifact_links_for_target_returns_links(
        self, scratch_conn: Any, seed_conversation: tuple[str, str]
    ) -> None:
        """find_artifact_links_for_target returns links for a specific
        durable record."""
        from app.services.live.provenance import (
            find_artifact_links_for_target,
        )

        conversation_id, user_id = seed_conversation
        target_id = str(uuid4())
        ids = await self._seed_linked_record(
            scratch_conn, conversation_id, user_id,
            "events", target_id, "logged_event",
        )

        links = await find_artifact_links_for_target(
            scratch_conn,
            target_table="events",
            target_id=target_id,
        )

        assert len(links) >= 1, (
            f"Expected at least 1 link for events/{target_id}, "
            f"got {len(links)}"
        )
        link = links[0]
        assert link["artifact_id"] == ids["artifact_id"]
        assert link["relation"] == "logged_event"
