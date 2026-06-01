from pathlib import Path
from uuid import UUID

import pytest

from scripts.backup_dump import sha256_file
from scripts.backfill_embeddings import (
    COVERAGE_SQL,
    HNSW_INDEX_SQL,
    SELECT_CANDIDATES_SQL,
    UPSERT_EMBEDDING_SQL,
    backfill_embeddings,
    build_hnsw_index_concurrently,
    direct_database_url_from_env,
)
from app.services.embeddings import content_hash

pytestmark = pytest.mark.anyio


def test_sha256_file_matches_known_content(tmp_path: Path) -> None:
    path = tmp_path / "dump"
    path.write_bytes(b"veas backup")

    assert sha256_file(path) == "bdc1a887db21bbb0f413ef3d6ed2056e4689cb09cb652fb00cdee3b50a4dd25e"


def test_backfill_embeddings_requires_direct_non_pooler_url() -> None:
    assert (
        direct_database_url_from_env(
            {"DIRECT_DATABASE_URL": "postgresql://user:pass@db.example.com:5432/postgres"}
        )
        == "postgresql://user:pass@db.example.com:5432/postgres"
    )

    for url in (
        "",
        "postgresql://user:pass@aws-0-eu.pooler.supabase.com:5432/postgres",
        "postgresql://user:pass@db.example.com:6543/postgres",
        "postgresql://user:pass@pgbouncer.example.com:5432/postgres",
    ):
        try:
            direct_database_url_from_env({"DIRECT_DATABASE_URL": url})
        except ValueError as exc:
            assert "DIRECT_DATABASE_URL" in str(exc) or "refusing" in str(exc)
        else:  # pragma: no cover - defensive assertion branch
            raise AssertionError(f"accepted unsafe url: {url}")


def test_backfill_embeddings_source_safety_contract() -> None:
    source = Path("scripts/backfill_embeddings.py").read_text()

    assert "DIRECT_DATABASE_URL" in source
    assert "DATABASE_URL" not in source.replace("DIRECT_DATABASE_URL", "")
    assert "mediator.v_searchable_content" in SELECT_CANDIDATES_SQL
    assert "LEFT JOIN mediator.content_embeddings" in SELECT_CANDIDATES_SQL
    assert "source_type" in SELECT_CANDIDATES_SQL
    assert "source_id" in SELECT_CANDIDATES_SQL
    assert "GROUP BY source_type" in COVERAGE_SQL
    assert "content_hash(" in source
    assert "$3::vector" in UPSERT_EMBEDDING_SQL
    assert "ON CONFLICT (source_type, source_id)" in UPSERT_EMBEDDING_SQL
    assert "CREATE INDEX CONCURRENTLY" in HNSW_INDEX_SQL
    assert "asyncio.create_task" not in source
    assert "embedding_worker_enabled" not in source


class FakeBackfillEmbedder:
    model_name = "text-embedding-3-small"
    dimension = 3

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def embed_texts(self, texts):
        self.calls.append(list(texts))
        return [[1.0, 0.0, 0.0] for _ in texts]


class FakeBackfillConn:
    def __init__(self, rows):
        self.rows = sorted(rows, key=lambda row: (row["source_type"], str(row["source_id"])))
        self.fetch_calls = []
        self.execute_calls = []
        self.upserts = {}

    async def fetch(self, sql: str, *args):
        self.fetch_calls.append((sql, args))
        cursor_type, cursor_id, limit = args
        rows = [
            row
            for row in self.rows
            if (row["source_type"], str(row["source_id"])) > (cursor_type, str(cursor_id))
        ]
        return rows[:limit]

    async def execute(self, sql: str, *args):
        self.execute_calls.append((sql, args))
        if sql == HNSW_INDEX_SQL:
            return "CREATE INDEX"
        if sql == UPSERT_EMBEDDING_SQL:
            source_type, source_id, vector, model, dimension, hash_value, embedded_at = args
            self.upserts[(source_type, source_id)] = {
                "vector": vector,
                "model": model,
                "dimension": dimension,
                "content_hash": hash_value,
                "embedded_at": embedded_at,
            }
            return "INSERT 0 1"
        raise AssertionError(f"unexpected execute: {sql}")


async def _no_sleep(_seconds: float) -> None:
    return None


async def test_backfill_embeddings_dry_run_scans_view_without_embedding(anyio_backend) -> None:
    message_id = UUID("00000000-0000-0000-0000-000000000001")
    conn = FakeBackfillConn(
        [
            {
                "source_type": "message",
                "source_id": message_id,
                "message_id": message_id,
                "canonical_text": "hello\n\n\n",
                "existing_content_hash": None,
                "existing_model": None,
                "existing_dimension": None,
            }
        ]
    )
    embedder = FakeBackfillEmbedder()

    totals = await backfill_embeddings(
        conn,
        embedder=embedder,
        batch_size=10,
        rate_limit_per_min=60,
        dry_run=True,
        sleep=_no_sleep,
    )

    assert totals.scanned == 1
    assert totals.dry_run_pending == 1
    assert totals.embedded == 0
    assert embedder.calls == []
    assert conn.upserts == {}


async def test_backfill_embeddings_upserts_only_missing_or_stale_rows(anyio_backend) -> None:
    current_id = UUID("00000000-0000-0000-0000-000000000001")
    stale_id = UUID("00000000-0000-0000-0000-000000000002")
    missing_id = UUID("00000000-0000-0000-0000-000000000003")
    memory_id = UUID("00000000-0000-0000-0000-000000000004")
    current_text = "already current\n\n\n"
    stale_text = "edited\n\n\n"
    memory_text = "private memory"
    conn = FakeBackfillConn(
        [
            {
                "source_type": "message",
                "source_id": current_id,
                "message_id": current_id,
                "canonical_text": current_text,
                "existing_content_hash": content_hash(current_text),
                "existing_model": "text-embedding-3-small",
                "existing_dimension": 3,
            },
            {
                "source_type": "message",
                "source_id": stale_id,
                "message_id": stale_id,
                "canonical_text": stale_text,
                "existing_content_hash": "0" * 64,
                "existing_model": "text-embedding-3-small",
                "existing_dimension": 3,
            },
            {
                "source_type": "message",
                "source_id": missing_id,
                "message_id": missing_id,
                "canonical_text": None,
                "existing_content_hash": None,
                "existing_model": None,
                "existing_dimension": None,
            },
            {
                "source_type": "memory",
                "source_id": memory_id,
                "message_id": None,
                "canonical_text": memory_text,
                "existing_content_hash": None,
                "existing_model": None,
                "existing_dimension": None,
            },
        ]
    )
    embedder = FakeBackfillEmbedder()

    totals = await backfill_embeddings(
        conn,
        embedder=embedder,
        batch_size=2,
        rate_limit_per_min=100000,
        dry_run=False,
        sleep=_no_sleep,
    )

    assert totals.scanned == 4
    assert totals.skipped_current == 1
    assert totals.embedded == 3
    assert embedder.calls == [[memory_text], [stale_text, ""]]
    assert set(conn.upserts) == {
        ("message", stale_id),
        ("message", missing_id),
        ("memory", memory_id),
    }
    assert conn.upserts[("message", stale_id)]["content_hash"] == content_hash(stale_text)
    assert conn.upserts[("message", missing_id)]["content_hash"] == content_hash("")
    assert conn.upserts[("memory", memory_id)]["content_hash"] == content_hash(memory_text)


async def test_backfill_hnsw_index_build_uses_concurrent_statement_without_transaction(anyio_backend) -> None:
    conn = FakeBackfillConn([])

    await build_hnsw_index_concurrently(conn)

    assert conn.execute_calls == [(HNSW_INDEX_SQL, ())]
    assert "CREATE INDEX CONCURRENTLY" in conn.execute_calls[0][0]
