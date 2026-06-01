from uuid import uuid4

import pytest

from pathlib import Path


MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
MIGRATION_NUMBER = "0058"
UP_PATH = MIGRATIONS_DIR / f"{MIGRATION_NUMBER}_content_embeddings_unified_index.sql"
DOWN_PATH = MIGRATIONS_DIR / f"{MIGRATION_NUMBER}_content_embeddings_unified_index.down.sql"
UP_SQL = UP_PATH.read_text()
DOWN_SQL = DOWN_PATH.read_text()


def _compact(sql: str) -> str:
    return " ".join(sql.lower().split())


def _database_dsn(admin_dsn: str, db_name: str) -> str:
    if "?" in admin_dsn:
        base, _, qs = admin_dsn.partition("?")
        head, _, _ = base.rpartition("/")
        return f"{head}/{db_name}?{qs}"
    head, _, _ = admin_dsn.rpartition("/")
    return f"{head}/{db_name}"


def test_0058_is_next_migration_number() -> None:
    numbered = sorted(
        path.name
        for path in MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql")
        if not path.name.endswith(".down.sql")
    )
    assert numbered[-1].startswith(f"{MIGRATION_NUMBER}_")
    assert sum(1 for name in numbered if name.startswith(f"{MIGRATION_NUMBER}_")) == 1
    assert UP_PATH.exists()
    assert DOWN_PATH.exists()


def test_0058_creates_generalized_content_embeddings() -> None:
    lowered = _compact(UP_SQL)
    assert "alter table mediator.message_embeddings rename to content_embeddings" in lowered
    assert "alter table mediator.content_embeddings rename column message_id to source_id" in lowered
    assert "add column source_type text" in lowered
    assert "set source_type = 'message'" in lowered
    assert "check (source_type in ('message','memory','observation','distillation','artifact'))" in lowered
    assert "primary key (source_type, source_id)" in lowered
    assert "drop constraint %i" in lowered
    assert "contype = 'f'" in lowered
    assert "idx_content_embeddings_model_dimension" in lowered
    assert "idx_content_embeddings_embedded_at" in lowered


def test_0058_generalizes_embed_jobs_without_dropping_message_id_compatibility() -> None:
    lowered = _compact(UP_SQL)
    assert "alter table mediator.embed_jobs" in lowered
    assert "add column if not exists source_type text" in lowered
    assert "add column if not exists source_id uuid" in lowered
    assert "deploy compatibility window" in lowered
    assert "message_id remains nullable compatibility metadata" in lowered
    assert "drop not null" in lowered
    assert "coalesce(source_type, 'message')" in lowered
    assert "set message_id = source_id" in lowered
    assert "alter column source_type set not null" in lowered
    assert "alter column source_id set not null" in lowered
    assert "embed_jobs_message_source_compat_check" in lowered
    assert "create or replace function mediator.populate_embed_job_source_identity" in lowered
    assert "before insert or update of message_id, source_type, source_id" in lowered
    assert "new.message_id := new.source_id" in lowered
    assert "idx_embed_jobs_source_status" in lowered
    assert "idx_embed_jobs_active_source_dedupe" in lowered


def test_0058_embed_jobs_active_dedupe_is_source_aware() -> None:
    lowered = _compact(UP_SQL)
    assert "drop index if exists mediator.idx_embed_jobs_active_dedupe" in lowered
    assert "create unique index if not exists idx_embed_jobs_active_source_dedupe" in lowered
    assert "on mediator.embed_jobs ( source_type, source_id, job_kind, coalesce(content_hash, '') )" in lowered
    assert "where status in ('pending','processing')" in lowered
    assert "on mediator.embed_jobs (message_id, job_kind, coalesce(content_hash, '')) where status in ('pending','processing')" not in lowered


def test_0058_defines_unified_searchable_content_and_message_compatibility_view() -> None:
    lowered = _compact(UP_SQL)
    assert "create or replace view mediator.v_searchable_content" in lowered
    assert "source_type" in lowered
    assert "'message'::text as source_type" in lowered
    assert "'memory'::text as source_type" in lowered
    assert "'observation'::text as source_type" in lowered
    assert "'distillation'::text as source_type" in lowered
    assert "'artifact'::text as source_type" in lowered
    assert "create or replace view mediator.v_searchable_messages" in lowered
    assert "from mediator.v_searchable_content sc" in lowered
    assert "where sc.source_type = 'message'" in lowered
    view_start = lowered.index("create or replace view mediator.v_searchable_messages")
    view_sql = lowered[view_start : lowered.index("comment on view mediator.v_searchable_messages")]
    for column in (
        "sc.message_id",
        "sc.direction",
        "sc.sender_id",
        "sc.recipient_id",
        "sc.thread_owner_user_id",
        "sc.sent_at",
        "sc.charge",
        "sc.edited_at",
        "sc.edit_history",
        "sc.content",
        "sc.media_type",
        "sc.media_analysis",
        "sc.bot_id",
        "sc.topic_id",
        "sc.dyad_id",
        "sc.thread_owner_partner_share",
        "sc.canonical_text",
        "sc.search_tsv",
    ):
        assert column in view_sql
    assert "sc.source_id" not in view_sql
    assert "sc.source_type" not in view_sql.replace("where sc.source_type = 'message'", "")


def test_0058_visibility_excludes_deleted_suppressed_and_dyad_shareable_non_messages() -> None:
    lowered = _compact(UP_SQL)
    assert "m.deleted_at is null" in lowered
    assert "m.search_suppressed_at is null" in lowered
    assert "mem.status = 'active'" in lowered
    assert "obs.status = 'active'" in lowered
    assert "d.status = 'active'" in lowered
    assert "ca.deleted_at is null" in lowered
    assert "coalesce(mem.visibility, 'private') = 'private'" in lowered
    assert "obs.significance >= 3" in lowered
    assert "coalesce(d.visibility, 'private') = 'private'" in lowered


def test_0058_memory_and_observation_topics_are_deterministic_and_aggregated() -> None:
    lowered = _compact(UP_SQL)
    assert "left join lateral" in lowered
    assert "array_agg(at.topic_id order by at.topic_id)" in lowered
    assert "(array_agg(at.topic_id order by at.topic_id))[1] as primary_topic_id" in lowered
    assert "coalesce(topics.topic_ids, array[]::uuid[]) as topic_ids" in lowered
    assert "at.artifact_table = 'memories'" in lowered
    assert "at.artifact_table = 'observations'" in lowered
    assert "and at.status = 'active'" in lowered
    assert "source_created_at" in lowered
    assert "source_updated_at" in lowered
    assert "null::uuid as message_id" in lowered


def test_0058_distillations_are_private_active_and_topic_aggregated() -> None:
    lowered = _compact(UP_SQL)
    assert "from mediator.distillations d left join lateral" in lowered
    assert "at.artifact_table = 'distillations'" in lowered
    assert "and at.status = 'active'" in lowered
    assert "d.status = 'active'" in lowered
    assert "coalesce(d.visibility, 'private') = 'private'" in lowered
    assert "coalesce(d.visibility, 'private') <> 'dyad_shareable'" not in lowered
    assert "coalesce(topics.topic_ids, array[]::uuid[]) as topic_ids" in lowered
    assert "d.content as canonical_text" in lowered


def test_0058_artifacts_filter_deleted_expired_and_extract_known_json_text_paths() -> None:
    lowered = _compact(UP_SQL)
    assert "from mediator.conversation_artifacts ca" in lowered
    assert "cross join lateral" in lowered
    assert "ca.deleted_at is null" in lowered
    assert "(ca.expires_at is null or ca.expires_at > now())" in lowered
    assert "artifact_text.canonical_text" in lowered
    assert "jsonb_build_object( 'artifact_type', ca.artifact_type" in lowered
    for path in (
        "ca.payload->>'prep_summary'",
        "ca.payload->>'review_summary'",
        "ca.payload->>'summary'",
        "ca.payload->>'notes'",
        "ca.payload#>>'{agenda,prep_summary}'",
        "ca.payload#>>'{live_debrief,review_summary}'",
        "ca.payload#>>'{review,summary}'",
        "jsonb_typeof(ca.payload->'what_heard') = 'array'",
        "jsonb_typeof(ca.payload->'what_decided') = 'array'",
        "jsonb_typeof(ca.payload->'still_open') = 'array'",
        "jsonb_typeof(ca.payload->'what_to_remember') = 'array'",
        "jsonb_typeof(ca.payload->'durable_write_summary') = 'array'",
        "jsonb_typeof(ca.payload->'open_questions') = 'array'",
        "jsonb_typeof(ca.payload#>'{agenda,items}') = 'array'",
        "jsonb_typeof(ca.payload->'items') = 'array'",
    ):
        assert path in lowered


def test_0058_message_delete_cleanup_handles_generalized_embeddings_and_jobs() -> None:
    lowered = _compact(UP_SQL)
    assert "create or replace function mediator.cleanup_message_content_embedding" in lowered
    assert "delete from mediator.content_embeddings" in lowered
    assert "source_type = 'message'" in lowered
    assert "source_id = old.id" in lowered
    assert "update mediator.embed_jobs" in lowered
    assert "where source_type = 'message'" in lowered
    assert "create trigger trg_messages_cleanup_content_embedding" in lowered
    assert "after delete on mediator.messages" in lowered


def test_0058_down_teardown_order_and_comments_are_explicit() -> None:
    lowered = _compact(DOWN_SQL)
    assert "reverse 0058_content_embeddings_unified_index" in lowered
    assert "drop views before columns and tables" in lowered
    assert lowered.index("drop view if exists mediator.v_searchable_messages") < lowered.index(
        "drop view if exists mediator.v_searchable_content"
    )
    assert lowered.index("drop trigger if exists trg_messages_cleanup_content_embedding") < lowered.index(
        "drop function if exists mediator.cleanup_message_content_embedding"
    )
    assert lowered.index("drop trigger if exists trg_embed_jobs_populate_source_identity") < lowered.index(
        "drop function if exists mediator.populate_embed_job_source_identity"
    )
    assert lowered.index("delete from mediator.embed_jobs") < lowered.index(
        "alter table mediator.embed_jobs"
    )
    assert "deploy compatibility window reversal" in lowered
    assert "where source_type <> 'message'" in lowered
    assert "set message_id = source_id" in lowered
    assert "create unique index if not exists idx_embed_jobs_active_dedupe" in lowered
    assert "on mediator.embed_jobs (message_id, job_kind, coalesce(content_hash, ''))" in lowered
    assert lowered.index("delete from mediator.content_embeddings") < lowered.index(
        "alter table mediator.content_embeddings rename column source_id to message_id"
    )
    assert "alter table mediator.content_embeddings rename to message_embeddings" in lowered
    assert "foreign key (message_id) references mediator.messages(id) on delete cascade" in lowered
    assert "message_id compatibility column is preserved" in lowered


@pytest.mark.postgres
@pytest.mark.anyio
async def test_0058_split_apply_preserves_message_vectors_visibility_and_delete_cleanup() -> None:
    """Apply through 0057, seed legacy message rows, then apply 0058.

    Gated on TEST_DATABASE_URL because the migration path requires pgvector.
    """
    import os

    asyncpg = pytest.importorskip("asyncpg")

    admin_dsn = os.environ.get("TEST_DATABASE_URL")
    if not admin_dsn:
        pytest.skip("TEST_DATABASE_URL unset; pgvector migration validation requires it")

    admin_conn = await asyncpg.connect(admin_dsn, statement_cache_size=0)
    db_name = f"veas_0058_split_{uuid4().hex[:12]}"
    test_dsn = _database_dsn(admin_dsn, db_name)
    try:
        has_vector = await admin_conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'vector')"
        )
        if not has_vector:
            pytest.skip("TEST_DATABASE_URL cluster does not have pgvector available")

        for role in ("anon", "authenticated", "service_role"):
            await admin_conn.execute(
                f"DO $$ BEGIN CREATE ROLE {role}; "
                f"EXCEPTION WHEN duplicate_object THEN NULL; END $$;"
            )
        await admin_conn.execute(f'DROP DATABASE IF EXISTS "{db_name}";')
        await admin_conn.execute(f'CREATE DATABASE "{db_name}";')

        conn = await asyncpg.connect(test_dsn, statement_cache_size=0)
        try:
            await conn.execute("CREATE SCHEMA IF NOT EXISTS mediator;")
            await conn.execute("CREATE SCHEMA IF NOT EXISTS auth;")
            await conn.execute(
                """
                CREATE OR REPLACE FUNCTION auth.uid()
                RETURNS uuid
                LANGUAGE sql
                STABLE
                AS $$ SELECT NULL::uuid $$;
                """
            )
            await conn.execute(f'ALTER DATABASE "{db_name}" SET search_path TO mediator, public;')
            await conn.execute("SET search_path TO mediator, public;")

            from tests.fixtures.postgres import _SEED_BEFORE_0025, _migration_files

            for path in _migration_files():
                if path.name > "0057_searchable_messages_render_metadata.sql":
                    break
                if path.name == "0025_backfill_legacy_scope_columns.sql":
                    await conn.execute(_SEED_BEFORE_0025)
                await conn.execute(path.read_text())

            topic_id = await conn.fetchval(
                "SELECT id FROM mediator.topics WHERE slug = 'relationship';"
            )
            sender_id = await conn.fetchval(
                "SELECT id FROM mediator.users ORDER BY created_at LIMIT 1;"
            )
            visible_id = await conn.fetchval(
                """
                INSERT INTO mediator.messages
                    (direction, sender_id, content, bot_id, topic_id, sent_at)
                VALUES ('inbound', $1, 'visible vector row', 'mediator', $2, now())
                RETURNING id;
                """,
                sender_id,
                topic_id,
            )
            deleted_id = await conn.fetchval(
                """
                INSERT INTO mediator.messages
                    (direction, sender_id, content, bot_id, topic_id, sent_at, deleted_at)
                VALUES ('inbound', $1, 'deleted vector row', 'mediator', $2, now(), now())
                RETURNING id;
                """,
                sender_id,
                topic_id,
            )
            suppressed_id = await conn.fetchval(
                """
                INSERT INTO mediator.messages
                    (direction, sender_id, content, bot_id, topic_id, sent_at, search_suppressed_at)
                VALUES ('inbound', $1, 'suppressed vector row', 'mediator', $2, now(), now())
                RETURNING id;
                """,
                sender_id,
                topic_id,
            )
            await conn.execute(
                """
                INSERT INTO mediator.message_embeddings
                    (message_id, embedding, model, dimension, content_hash, embedded_at)
                VALUES
                    ($1, $4::vector, 'model-a', 1536, 'hash-visible', '2026-01-01T00:00:00Z'),
                    ($2, $4::vector, 'model-a', 1536, 'hash-deleted', '2026-01-02T00:00:00Z'),
                    ($3, $4::vector, 'model-a', 1536, 'hash-suppressed', '2026-01-03T00:00:00Z');
                """,
                visible_id,
                deleted_id,
                suppressed_id,
                "[" + ",".join(["0.001"] * 1536) + "]",
            )
            await conn.execute(
                """
                INSERT INTO mediator.embed_jobs (message_id, job_kind, content_hash, status)
                VALUES
                    ($1, 'reembed', 'hash-visible-next', 'pending'),
                    ($2, 'drop', NULL, 'processing'),
                    ($3, 'embed', 'hash-suppressed-next', 'pending');
                """,
                visible_id,
                deleted_id,
                suppressed_id,
            )

            await conn.execute(UP_SQL)

            preserved = await conn.fetch(
                """
                SELECT source_type, source_id, model, dimension, content_hash, embedded_at
                FROM mediator.content_embeddings
                ORDER BY content_hash;
                """
            )
            assert [(row["source_type"], row["source_id"], row["content_hash"]) for row in preserved] == [
                ("message", deleted_id, "hash-deleted"),
                ("message", suppressed_id, "hash-suppressed"),
                ("message", visible_id, "hash-visible"),
            ]
            assert {row["model"] for row in preserved} == {"model-a"}
            assert {row["dimension"] for row in preserved} == {1536}

            searchable_content_ids = await conn.fetch(
                "SELECT source_id FROM mediator.v_searchable_content WHERE source_type = 'message';"
            )
            searchable_message_ids = await conn.fetch(
                "SELECT message_id FROM mediator.v_searchable_messages;"
            )
            assert {row["source_id"] for row in searchable_content_ids} == {visible_id}
            assert {row["message_id"] for row in searchable_message_ids} == {visible_id}

            jobs = await conn.fetch(
                """
                SELECT message_id, source_type, source_id, job_kind, status
                FROM mediator.embed_jobs
                ORDER BY source_id, job_kind;
                """
            )
            assert {(row["message_id"], row["source_type"], row["source_id"]) for row in jobs} == {
                (visible_id, "message", visible_id),
                (deleted_id, "message", deleted_id),
                (suppressed_id, "message", suppressed_id),
            }

            await conn.execute("DELETE FROM mediator.messages WHERE id = $1;", visible_id)
            assert await conn.fetchval(
                """
                SELECT COUNT(*) FROM mediator.content_embeddings
                WHERE source_type = 'message' AND source_id = $1;
                """,
                visible_id,
            ) == 0
            assert await conn.fetchval(
                """
                SELECT COUNT(*) FROM mediator.embed_jobs
                WHERE source_type = 'message' AND source_id = $1
                  AND status IN ('pending','processing');
                """,
                visible_id,
            ) == 0
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


@pytest.mark.postgres
@pytest.mark.anyio
async def test_0058_split_apply_enforces_memory_observation_distillation_visibility_and_topics() -> None:
    import os

    asyncpg = pytest.importorskip("asyncpg")

    admin_dsn = os.environ.get("TEST_DATABASE_URL")
    if not admin_dsn:
        pytest.skip("TEST_DATABASE_URL unset; pgvector migration validation requires it")

    admin_conn = await asyncpg.connect(admin_dsn, statement_cache_size=0)
    db_name = f"veas_0058_visibility_{uuid4().hex[:12]}"
    test_dsn = _database_dsn(admin_dsn, db_name)
    try:
        has_vector = await admin_conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'vector')"
        )
        if not has_vector:
            pytest.skip("TEST_DATABASE_URL cluster does not have pgvector available")

        for role in ("anon", "authenticated", "service_role"):
            await admin_conn.execute(
                f"DO $$ BEGIN CREATE ROLE {role}; "
                f"EXCEPTION WHEN duplicate_object THEN NULL; END $$;"
            )
        await admin_conn.execute(f'DROP DATABASE IF EXISTS "{db_name}";')
        await admin_conn.execute(f'CREATE DATABASE "{db_name}";')

        conn = await asyncpg.connect(test_dsn, statement_cache_size=0)
        try:
            await conn.execute("CREATE SCHEMA IF NOT EXISTS mediator;")
            await conn.execute("CREATE SCHEMA IF NOT EXISTS auth;")
            await conn.execute(
                """
                CREATE OR REPLACE FUNCTION auth.uid()
                RETURNS uuid
                LANGUAGE sql
                STABLE
                AS $$ SELECT NULL::uuid $$;
                """
            )
            await conn.execute(f'ALTER DATABASE "{db_name}" SET search_path TO mediator, public;')
            await conn.execute("SET search_path TO mediator, public;")

            from tests.fixtures.postgres import _SEED_BEFORE_0025, _migration_files

            for path in _migration_files():
                if path.name > "0057_searchable_messages_render_metadata.sql":
                    break
                if path.name == "0025_backfill_legacy_scope_columns.sql":
                    await conn.execute(_SEED_BEFORE_0025)
                await conn.execute(path.read_text())

            primary_topic_id = await conn.fetchval(
                "SELECT id FROM mediator.topics WHERE slug = 'relationship';"
            )
            secondary_topic_id = await conn.fetchval(
                """
                INSERT INTO mediator.topics (slug, title)
                VALUES ('embedding-secondary', 'Embedding Secondary')
                RETURNING id;
                """
            )
            user_id = await conn.fetchval(
                "SELECT id FROM mediator.users ORDER BY created_at LIMIT 1;"
            )

            visible_memory_id = await conn.fetchval(
                """
                INSERT INTO mediator.memories (about_user_id, content, status, visibility)
                VALUES ($1, 'private active memory', 'active', 'private')
                RETURNING id;
                """,
                user_id,
            )
            await conn.execute(
                """
                INSERT INTO mediator.artifact_topics
                    (artifact_table, artifact_id, topic_id, tagged_by_bot_id, status, reason)
                VALUES
                    ('memories', $1, $2, 'mediator', 'active', 'primary memory topic'),
                    ('memories', $1, $3, 'mediator', 'inactive', 'old memory topic');
                """,
                visible_memory_id,
                primary_topic_id,
                secondary_topic_id,
            )
            await conn.execute(
                """
                INSERT INTO mediator.memories (about_user_id, content, status, visibility)
                VALUES
                    ($1, 'shared memory', 'active', 'dyad_shareable'),
                    ($1, 'inactive memory', 'superseded', 'private');
                """,
                user_id,
            )

            visible_observation_id = await conn.fetchval(
                """
                INSERT INTO mediator.observations
                    (content, about_user_id, confidence, significance, status, scoring_prompt_version)
                VALUES ('significant observation', $1, 'high', 4, 'active', 'prompt-v1')
                RETURNING id;
                """,
                user_id,
            )
            await conn.execute(
                """
                INSERT INTO mediator.artifact_topics
                    (artifact_table, artifact_id, topic_id, tagged_by_bot_id, status, reason)
                VALUES
                    ('observations', $1, $2, 'mediator', 'active', 'primary observation topic'),
                    ('observations', $1, $3, 'mediator', 'inactive', 'old observation topic');
                """,
                visible_observation_id,
                secondary_topic_id,
                primary_topic_id,
            )
            await conn.execute(
                """
                INSERT INTO mediator.observations
                    (content, about_user_id, confidence, significance, status, scoring_prompt_version)
                VALUES
                    ('low significance observation', $1, 'medium', 2, 'active', 'prompt-v1'),
                    ('stale observation', $1, 'medium', 5, 'stale', 'prompt-v1');
                """,
                user_id,
            )

            visible_distillation_id = await conn.fetchval(
                """
                INSERT INTO mediator.distillations
                    (content, confidence, status, sensitivity, visibility, source_user_ids, supporting_message_ids)
                VALUES ('private active distillation', 'medium', 'active', 'medium', 'private', ARRAY[$1]::uuid[], ARRAY[]::uuid[])
                RETURNING id;
                """,
                user_id,
            )
            await conn.execute(
                """
                INSERT INTO mediator.artifact_topics
                    (artifact_table, artifact_id, topic_id, tagged_by_bot_id, status, reason)
                VALUES
                    ('distillations', $1, $2, 'mediator', 'active', 'primary distillation topic'),
                    ('distillations', $1, $3, 'mediator', 'inactive', 'old distillation topic');
                """,
                visible_distillation_id,
                primary_topic_id,
                secondary_topic_id,
            )
            await conn.execute(
                """
                INSERT INTO mediator.distillations
                    (content, confidence, status, sensitivity, visibility, shareable_summary, source_user_ids, supporting_message_ids)
                VALUES
                    ('shared distillation', 'medium', 'active', 'medium', 'dyad_shareable', 'shareable', ARRAY[$1]::uuid[], ARRAY[]::uuid[]),
                    ('retired distillation', 'medium', 'retired', 'medium', 'private', NULL, ARRAY[$1]::uuid[], ARRAY[]::uuid[]);
                """,
                user_id,
            )

            await conn.execute(UP_SQL)

            searchable = await conn.fetch(
                """
                SELECT source_type, source_id, primary_topic_id, topic_ids
                FROM mediator.v_searchable_content
                WHERE source_type IN ('memory', 'observation', 'distillation')
                ORDER BY source_type, source_id;
                """
            )
            assert [(row["source_type"], row["source_id"]) for row in searchable] == [
                ("distillation", visible_distillation_id),
                ("memory", visible_memory_id),
                ("observation", visible_observation_id),
            ]
            assert {
                (row["source_type"], row["primary_topic_id"], row["topic_ids"])
                for row in searchable
            } == {
                ("memory", primary_topic_id, [primary_topic_id]),
                ("observation", secondary_topic_id, [secondary_topic_id]),
                ("distillation", primary_topic_id, [primary_topic_id]),
            }
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
