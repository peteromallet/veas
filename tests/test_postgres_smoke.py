"""Smoke test for the Project B.1 real-Postgres fixture.

Verifies:
  - The fixture provisions a Postgres instance and applies every forward
    migration in ``migrations/*.sql`` (excluding ``teardown.sql`` and
    ``*.down.sql``).
  - The ``mediator.messages`` table is present with the recovery-v2
    lifecycle columns added by A1 (``next_retry_at``, ``failure_class``).
  - The ``messages_failure_class_check`` constraint rejects bogus values.

Tagged ``postgres`` so it can be selected via ``pytest -m postgres``.  The
fixture auto-skips when Docker is unavailable and ``TEST_DATABASE_URL`` is
not set, so this file is safe to include in the default test run.
"""

from __future__ import annotations

from pathlib import Path

import pytest


pytestmark = pytest.mark.postgres


REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = REPO_ROOT / "migrations"


def _expected_migration_count() -> int:
    count = 0
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if path.name == "teardown.sql":
            continue
        if path.name.endswith(".down.sql"):
            continue
        count += 1
    return count


async def test_pg_pool_connects_and_runs_select(pg_pool) -> None:
    """The fixture yields a working asyncpg pool against the test DB."""
    value = await pg_pool.fetchval("SELECT 1;")
    assert value == 1


async def test_mediator_schema_has_expected_tables(pg_pool) -> None:
    """Every migration created tables in the ``mediator`` schema."""
    rows = await pg_pool.fetch(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'mediator' ORDER BY table_name;"
    )
    table_names = {row["table_name"] for row in rows}

    # We don't pin the exact set (it changes per migration), but we assert
    # a sane lower bound and that the load-bearing tables for B.2 exist.
    assert len(table_names) >= 25, (
        f"Expected ≥25 mediator tables; got {len(table_names)}: {sorted(table_names)}"
    )
    for required in (
        "messages",
        "bot_turns",
        "topics",
        "bots",
        "dyads",
        "feedback",
    ):
        assert required in table_names, (
            f"Missing required table mediator.{required}; "
            f"have: {sorted(table_names)}"
        )


async def test_messages_table_has_recovery_v2_columns(pg_pool) -> None:
    """A1 added ``next_retry_at`` + ``failure_class`` in migration 0042."""
    rows = await pg_pool.fetch(
        "SELECT column_name, data_type "
        "FROM information_schema.columns "
        "WHERE table_schema = 'mediator' "
        "  AND table_name = 'messages' "
        "  AND column_name IN ('next_retry_at', 'failure_class');"
    )
    by_name = {row["column_name"]: row["data_type"] for row in rows}

    assert "next_retry_at" in by_name, (
        "mediator.messages.next_retry_at is missing — "
        "did migration 0046_message_lifecycle_columns.sql run?"
    )
    assert by_name["next_retry_at"] == "timestamp with time zone"

    assert "failure_class" in by_name, (
        "mediator.messages.failure_class is missing — "
        "did migration 0046_message_lifecycle_columns.sql run?"
    )
    assert by_name["failure_class"] == "text"


async def test_failure_class_check_constraint_rejects_bogus_values(pg_pool) -> None:
    """0042's CHECK constraint should reject failure_class not in the enum."""
    import asyncpg

    async with pg_pool.acquire() as conn:
        # NOT NULL constraints from later migrations require bot_id + topic_id.
        # Fetch the seeded mediator bot and relationship topic so we can
        # satisfy those before the CHECK constraint can fire.
        topic_id = await conn.fetchval(
            "SELECT id FROM mediator.topics WHERE slug = 'relationship';"
        )
        assert topic_id is not None, "0020 should seed the relationship topic"

        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await conn.execute(
                """
                INSERT INTO mediator.messages
                  (direction, processing_state, content, sent_at,
                   processing_attempts, bot_id, topic_id, failure_class)
                VALUES
                  ('inbound', 'failed', 'smoke test', now(), 1,
                   'mediator', $1, 'not-a-real-class');
                """,
                topic_id,
            )

        # And the happy path: a valid enum value is accepted.
        result = await conn.fetchval(
            """
            INSERT INTO mediator.messages
              (direction, processing_state, content, sent_at,
               processing_attempts, bot_id, topic_id, failure_class)
            VALUES
              ('inbound', 'failed', 'smoke test', now(), 1,
               'mediator', $1, 'retryable_pre_send')
            RETURNING id;
            """,
            topic_id,
        )
        assert result is not None
        # Clean up so re-running the smoke test on a shared DB is idempotent.
        await conn.execute("DELETE FROM mediator.messages WHERE id = $1;", result)


async def test_all_forward_migrations_were_applied(pg_pool) -> None:
    """Sanity check: the fixture really did walk every forward migration.

    We can't query a migrations table (there isn't one), but we can sample a
    couple of post-0001 artifacts.  ``processing_attempts`` is added by a
    later migration; ``bridge_candidates`` is added by 0013.
    """
    expected = _expected_migration_count()
    assert expected >= 40, (
        f"Fixture should be applying ≥40 forward migrations; "
        f"counted {expected} on disk."
    )

    has_bridge = await pg_pool.fetchval(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='mediator' AND table_name='bridge_candidates');"
    )
    assert has_bridge, "bridge_candidates table missing (0013 not applied?)"

    has_processing_attempts = await pg_pool.fetchval(
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
        "WHERE table_schema='mediator' AND table_name='messages' "
        "AND column_name='processing_attempts');"
    )
    assert has_processing_attempts, (
        "messages.processing_attempts missing — late-migration not applied"
    )
