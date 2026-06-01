from pathlib import Path
from uuid import uuid4

import pytest


MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
UP_SQL = (MIGRATIONS_DIR / "0057_searchable_messages_render_metadata.sql").read_text()
DOWN_SQL = (MIGRATIONS_DIR / "0057_searchable_messages_render_metadata.down.sql").read_text()


def test_0057_is_next_migration_number() -> None:
    numbered = sorted(
        path.name
        for path in MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql")
        if not path.name.endswith(".down.sql")
    )
    assert numbered[-1].startswith("0057_")
    assert sum(1 for name in numbered if name.startswith("0057_")) == 1


def test_0057_view_preserves_visibility_and_adds_render_metadata() -> None:
    lowered = UP_SQL.lower()
    assert "create or replace view mediator.v_searchable_messages" in lowered
    assert "where m.deleted_at is null" in lowered
    assert "and m.search_suppressed_at is null" in lowered
    assert "coalesce(m.charge, 'routine') as charge" in lowered
    assert "m.edited_at" in lowered
    assert "m.edit_history" in lowered


def test_0057_comments_clarify_metadata_not_visibility_predicates() -> None:
    lowered = UP_SQL.lower()
    assert "render metadata only" in lowered
    assert "not visibility predicates" in lowered


def test_0057_down_reverts_added_view_columns() -> None:
    lowered = DOWN_SQL.lower()
    assert "create or replace view mediator.v_searchable_messages" in lowered
    assert "coalesce(m.charge, 'routine') as charge" not in lowered
    assert "m.edited_at" not in lowered
    assert "m.edit_history" not in lowered


@pytest.mark.postgres
@pytest.mark.anyio
async def test_0057_catalog_shape_and_visibility_hold_after_recreate() -> None:
    import os

    asyncpg = pytest.importorskip("asyncpg")

    admin_dsn = os.environ.get("TEST_DATABASE_URL")
    if not admin_dsn:
        pytest.skip("TEST_DATABASE_URL unset; migration validation requires it")

    admin_conn = await asyncpg.connect(admin_dsn, statement_cache_size=0)
    db_name = f"veas_0057_{uuid4().hex[:12]}"
    test_dsn = _database_dsn(admin_dsn, db_name)
    try:
        from tests.fixtures.postgres import _apply_migrations

        for role in ("anon", "authenticated", "service_role"):
            await admin_conn.execute(
                f"DO $$ BEGIN CREATE ROLE {role}; "
                f"EXCEPTION WHEN duplicate_object THEN NULL; END $$;"
            )
        await admin_conn.execute(f'DROP DATABASE IF EXISTS "{db_name}";')
        await admin_conn.execute(f'CREATE DATABASE "{db_name}";')
        await _apply_migrations(test_dsn, db_name)

        conn = await asyncpg.connect(test_dsn, statement_cache_size=0)
        try:
            await _assert_0057_catalog_objects(conn)
            await _assert_0057_visibility(conn)
            await conn.execute(DOWN_SQL)
            columns = await conn.fetch(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'mediator'
                  AND table_name = 'v_searchable_messages'
                ORDER BY ordinal_position;
                """
            )
            column_names = [row["column_name"] for row in columns]
            assert "charge" not in column_names
            assert "edited_at" not in column_names
            assert "edit_history" not in column_names
        finally:
            await conn.close()
    finally:
        if admin_conn.is_closed():
            admin_conn = await asyncpg.connect(admin_dsn, statement_cache_size=0)
        try:
            await admin_conn.execute(
                "SELECT pg_terminate_backend(pid) "
                "FROM pg_stat_activity WHERE datname = $1 AND pid <> pg_backend_pid();",
                db_name,
            )
            await admin_conn.execute(f'DROP DATABASE IF EXISTS "{db_name}";')
        finally:
            await admin_conn.close()


def _database_dsn(admin_dsn: str, db_name: str) -> str:
    if "?" in admin_dsn:
        base, _, qs = admin_dsn.partition("?")
        head, _, _ = base.rpartition("/")
        return f"{head}/{db_name}?{qs}"
    head, _, _ = admin_dsn.rpartition("/")
    return f"{head}/{db_name}"


async def _assert_0057_catalog_objects(conn) -> None:
    columns = await conn.fetch(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'mediator'
          AND table_name = 'v_searchable_messages'
        ORDER BY ordinal_position;
        """
    )
    column_names = [row["column_name"] for row in columns]
    assert "charge" in column_names
    assert "edited_at" in column_names
    assert "edit_history" in column_names


async def _assert_0057_visibility(conn) -> None:
    topic_id = await conn.fetchval(
        "SELECT id FROM mediator.topics WHERE slug = 'relationship';"
    )
    sender_id = await conn.fetchval("SELECT id FROM mediator.users ORDER BY created_at LIMIT 1;")
    assert topic_id is not None
    assert sender_id is not None

    visible_id = await conn.fetchval(
        """
        INSERT INTO mediator.messages
            (direction, sender_id, content, charge, edit_history, edited_at, bot_id, topic_id, sent_at)
        VALUES
            ('inbound', $1, 'visible row', NULL, '[{"content":"old text"}]'::jsonb, now(),
             'mediator', $2, now())
        RETURNING id;
        """,
        sender_id,
        topic_id,
    )
    deleted_id = await conn.fetchval(
        """
        INSERT INTO mediator.messages
            (direction, sender_id, content, charge, bot_id, topic_id, sent_at, deleted_at)
        VALUES
            ('inbound', $1, 'deleted row', 'charged', 'mediator', $2, now(), now())
        RETURNING id;
        """,
        sender_id,
        topic_id,
    )
    suppressed_id = await conn.fetchval(
        """
        INSERT INTO mediator.messages
            (direction, sender_id, content, charge, bot_id, topic_id, sent_at, search_suppressed_at)
        VALUES
            ('inbound', $1, 'suppressed row', 'charged', 'mediator', $2, now(), now())
        RETURNING id;
        """,
        sender_id,
        topic_id,
    )

    row = await conn.fetchrow(
        """
        SELECT message_id, charge, edited_at, edit_history
        FROM mediator.v_searchable_messages
        WHERE message_id = $1;
        """,
        visible_id,
    )
    assert row is not None
    assert row["message_id"] == visible_id
    assert row["charge"] == "routine"
    assert row["edited_at"] is not None
    assert row["edit_history"][0]["content"] == "old text"

    deleted_visible = await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM mediator.v_searchable_messages WHERE message_id = $1);",
        deleted_id,
    )
    suppressed_visible = await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM mediator.v_searchable_messages WHERE message_id = $1);",
        suppressed_id,
    )
    assert deleted_visible is False
    assert suppressed_visible is False
