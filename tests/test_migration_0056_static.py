from pathlib import Path
from uuid import uuid4

import pytest


MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
UP_SQL = (MIGRATIONS_DIR / "0056_retrieval_index.sql").read_text()
DOWN_SQL = (MIGRATIONS_DIR / "0056_retrieval_index.down.sql").read_text()


def _inside_transaction(sql: str) -> str:
    begin = sql.index("BEGIN;")
    commit = sql.index("COMMIT;")
    return sql[begin:commit]


def test_0056_migration_exists_once() -> None:
    numbered = sorted(
        path.name
        for path in MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql")
        if not path.name.endswith(".down.sql")
    )
    assert sum(1 for name in numbered if name.startswith("0056_")) == 1


def test_pgvector_reversal_guardrails_documented() -> None:
    lowered = UP_SQL.lower()
    assert "explicit xen v1 m1 reversal" in lowered
    assert "human sign-off" in lowered
    assert "direct session-mode" in lowered
    assert "hnsw create index concurrently is deliberately absent" in lowered


def test_core_artifacts_are_created() -> None:
    lowered = UP_SQL.lower()
    assert "create extension if not exists vector" in lowered
    assert "add column if not exists search_suppressed_at timestamptz" in lowered
    assert "add column if not exists search_tsv tsvector" in lowered
    assert "create table if not exists mediator.message_embeddings" in lowered
    assert "embedding       vector(1536) not null" in lowered
    assert "content_hash    text not null" in lowered
    assert "create table if not exists mediator.embed_jobs" in lowered
    assert "create or replace view mediator.v_searchable_messages" in lowered
    assert "generated always as" in lowered
    assert "idx_messages_search_tsv" in lowered
    assert "idx_messages_searchable_scope_sent" in lowered
    assert "idx_embed_jobs_active_dedupe" in lowered


def test_canonical_text_uses_coalesced_agreed_fields_in_stable_order() -> None:
    content = UP_SQL.index("COALESCE(content, '')")
    explanation = UP_SQL.index("COALESCE(media_analysis->>'explanation', '')")
    description = UP_SQL.index("COALESCE(media_analysis->>'description', '')")
    summary = UP_SQL.index("COALESCE(media_analysis->>'summary', '')")
    assert content < explanation < description < summary
    assert "to_tsvector(" in UP_SQL


def test_view_excludes_deleted_and_suppressed_rows() -> None:
    view_start = UP_SQL.lower().index("create or replace view mediator.v_searchable_messages")
    view_sql = UP_SQL[view_start:].lower()
    assert "where m.deleted_at is null" in view_sql
    assert "and m.search_suppressed_at is null" in view_sql
    assert "thread_owner_user_id" in view_sql
    assert "thread_owner_partner_share" in view_sql
    assert "bb.dyad_id" in view_sql


def test_no_messages_content_hash_and_no_in_transaction_hnsw() -> None:
    lowered = UP_SQL.lower()
    assert "messages.content_hash" in lowered
    assert "add column if not exists content_hash" not in lowered
    assert "alter table mediator.messages" in lowered
    transactional = _inside_transaction(UP_SQL).lower()
    assert "hnsw" not in transactional.replace(
        "hnsw create index concurrently is deliberately absent", ""
    )
    assert "create index concurrently" not in transactional


def test_down_reverses_objects_and_guards_pgvector_drop() -> None:
    lowered = DOWN_SQL.lower()
    assert "drop view if exists mediator.v_searchable_messages" in lowered
    assert "drop table if exists mediator.embed_jobs" in lowered
    assert "drop table if exists mediator.message_embeddings" in lowered
    assert "drop column if exists search_tsv" in lowered
    assert "drop column if exists search_suppressed_at" in lowered
    assert "to_regtype('vector')" in lowered
    assert "drop extension if exists vector" in lowered


@pytest.mark.postgres
@pytest.mark.anyio
async def test_0056_catalog_objects_and_down_cleanup_with_pgvector() -> None:
    """Apply migrations in a disposable TEST_DATABASE_URL database.

    This is intentionally gated on TEST_DATABASE_URL, not the Docker fallback:
    the default postgres:16 image used by the shared fixture does not include
    pgvector. A caller that wants this integration path must provide a
    pgvector-capable test cluster.
    """
    import os

    asyncpg = pytest.importorskip("asyncpg")

    admin_dsn = os.environ.get("TEST_DATABASE_URL")
    if not admin_dsn:
        pytest.skip("TEST_DATABASE_URL unset; pgvector migration validation requires it")

    admin_conn = await asyncpg.connect(admin_dsn, statement_cache_size=0)
    db_name = f"veas_pgvector_{uuid4().hex[:12]}"
    test_dsn = _database_dsn(admin_dsn, db_name)
    try:
        has_vector = await admin_conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'vector')"
        )
        if not has_vector:
            pytest.skip("TEST_DATABASE_URL cluster does not have pgvector available")

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
            await _assert_0056_catalog_objects(conn)
            await _assert_search_tsv_non_null_for_null_content(conn)
            await conn.execute(DOWN_SQL)
            await _assert_0056_down_cleanup(conn)
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


async def _assert_0056_catalog_objects(conn) -> None:
    extension = await conn.fetchval(
        "SELECT extname FROM pg_extension WHERE extname = 'vector';"
    )
    assert extension == "vector"

    columns = await conn.fetch(
        """
        SELECT column_name, is_generated, data_type
        FROM information_schema.columns
        WHERE table_schema = 'mediator'
          AND table_name = 'messages'
          AND column_name IN ('search_tsv', 'search_suppressed_at')
        ORDER BY column_name;
        """
    )
    by_name = {row["column_name"]: row for row in columns}
    assert by_name["search_suppressed_at"]["data_type"] == "timestamp with time zone"
    assert by_name["search_tsv"]["data_type"] == "tsvector"
    assert by_name["search_tsv"]["is_generated"] == "ALWAYS"

    embedding_type = await conn.fetchval(
        """
        SELECT format_type(a.atttypid, a.atttypmod)
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'mediator'
          AND c.relname = 'message_embeddings'
          AND a.attname = 'embedding'
          AND NOT a.attisdropped;
        """
    )
    assert embedding_type == "vector(1536)"

    view_exists = await conn.fetchval(
        "SELECT to_regclass('mediator.v_searchable_messages') IS NOT NULL;"
    )
    assert view_exists


async def _assert_search_tsv_non_null_for_null_content(conn) -> None:
    topic_id = await conn.fetchval(
        "SELECT id FROM mediator.topics WHERE slug = 'relationship';"
    )
    sender_id = await conn.fetchval("SELECT id FROM mediator.users ORDER BY created_at LIMIT 1;")
    assert topic_id is not None
    assert sender_id is not None

    message_id = await conn.fetchval(
        """
        INSERT INTO mediator.messages
            (direction, sender_id, content, media_analysis, bot_id, topic_id, sent_at)
        VALUES
            ('inbound', $1, NULL, '{"summary": "fallback summary"}'::jsonb,
             'mediator', $2, now())
        RETURNING id;
        """,
        sender_id,
        topic_id,
    )

    row = await conn.fetchrow(
        """
        SELECT m.search_tsv IS NOT NULL AS has_search_tsv,
               v.canonical_text,
               v.message_id IS NOT NULL AS visible_before_suppression
        FROM mediator.messages m
        LEFT JOIN mediator.v_searchable_messages v ON v.message_id = m.id
        WHERE m.id = $1;
        """,
        message_id,
    )
    assert row["has_search_tsv"] is True
    assert "fallback summary" in row["canonical_text"]
    assert row["visible_before_suppression"] is True

    await conn.execute(
        "UPDATE mediator.messages SET search_suppressed_at = now() WHERE id = $1;",
        message_id,
    )
    visible_after_suppression = await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM mediator.v_searchable_messages WHERE message_id = $1);",
        message_id,
    )
    assert visible_after_suppression is False


async def _assert_0056_down_cleanup(conn) -> None:
    view_exists = await conn.fetchval(
        "SELECT to_regclass('mediator.v_searchable_messages') IS NOT NULL;"
    )
    assert view_exists is False

    for table_name in ("message_embeddings", "embed_jobs"):
        exists = await conn.fetchval(
            "SELECT to_regclass($1) IS NOT NULL;",
            f"mediator.{table_name}",
        )
        assert exists is False

    remaining_columns = await conn.fetch(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'mediator'
          AND table_name = 'messages'
          AND column_name IN ('search_tsv', 'search_suppressed_at');
        """
    )
    assert remaining_columns == []

    extension_exists = await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector');"
    )
    assert extension_exists is False
