#!/usr/bin/env python3
"""Backfill unified content embeddings for Xen M1 retrieval.

This is a human-run operational script.  It deliberately requires
``DIRECT_DATABASE_URL`` and refuses common transaction-pooler endpoints because
pgvector backfill and ``CREATE INDEX CONCURRENTLY`` need a direct session-mode
connection.  It is not imported or launched from application startup.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from app.config import Settings, get_settings
from app.services.embeddings import Embedder, content_hash, embedder_from_settings
from app.services.embed_worker import _vector_literal

logger = logging.getLogger("backfill_embeddings")

DEFAULT_BATCH_SIZE = 64
DEFAULT_RATE_LIMIT_PER_MIN = 60
DEFAULT_COVERAGE_THRESHOLD = 0.95


SELECT_CANDIDATES_SQL = """
SELECT
    v.source_type,
    v.source_id,
    v.message_id,
    v.canonical_text,
    e.content_hash AS existing_content_hash,
    e.model AS existing_model,
    e.dimension AS existing_dimension
FROM mediator.v_searchable_content v
LEFT JOIN mediator.content_embeddings e
  ON e.source_type = v.source_type
 AND e.source_id = v.source_id
WHERE (v.source_type, v.source_id) > ($1, $2::uuid)
ORDER BY v.source_type ASC, v.source_id ASC
LIMIT $3
"""

UPSERT_EMBEDDING_SQL = """
INSERT INTO mediator.content_embeddings (
    source_type, source_id, embedding, model, dimension, content_hash, embedded_at
)
VALUES ($1, $2, $3::vector, $4, $5, $6, $7)
ON CONFLICT (source_type, source_id) DO UPDATE
SET embedding = EXCLUDED.embedding,
    model = EXCLUDED.model,
    dimension = EXCLUDED.dimension,
    content_hash = EXCLUDED.content_hash,
    embedded_at = EXCLUDED.embedded_at
"""

COVERAGE_SQL = """
WITH searchable AS (
    SELECT source_type, count(*)::bigint AS total
    FROM mediator.v_searchable_content
    GROUP BY source_type
),
covered AS (
    SELECT v.source_type, count(*)::bigint AS covered
    FROM mediator.v_searchable_content v
    JOIN mediator.content_embeddings e
      ON e.source_type = v.source_type
     AND e.source_id = v.source_id
     AND e.model = $1
     AND e.dimension = $2
    GROUP BY v.source_type
)
SELECT searchable.source_type, searchable.total, coalesce(covered.covered, 0)::bigint AS covered
FROM searchable
LEFT JOIN covered USING (source_type)
ORDER BY searchable.source_type
"""

SOURCE_TYPE_COUNTS_SQL = """
WITH searchable AS (
    SELECT source_type, count(*)::bigint AS searchable_count
    FROM mediator.v_searchable_content
    GROUP BY source_type
),
embeddings AS (
    SELECT source_type, count(*)::bigint AS embedding_count
    FROM mediator.content_embeddings
    GROUP BY source_type
)
SELECT
    COALESCE(searchable.source_type, embeddings.source_type) AS source_type,
    COALESCE(searchable.searchable_count, 0)::bigint AS searchable_count,
    COALESCE(embeddings.embedding_count, 0)::bigint AS embedding_count
FROM searchable
FULL OUTER JOIN embeddings USING (source_type)
ORDER BY source_type
"""

HNSW_INDEX_SQL = """
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_content_embeddings_hnsw_embedding
ON mediator.content_embeddings
USING hnsw (embedding vector_cosine_ops)
"""


@dataclass(frozen=True)
class BackfillTotals:
    scanned: int = 0
    embedded: int = 0
    skipped_current: int = 0
    failed: int = 0
    dry_run_pending: int = 0
    batches: int = 0

    def add(self, other: "BackfillTotals") -> "BackfillTotals":
        return BackfillTotals(
            scanned=self.scanned + other.scanned,
            embedded=self.embedded + other.embedded,
            skipped_current=self.skipped_current + other.skipped_current,
            failed=self.failed + other.failed,
            dry_run_pending=self.dry_run_pending + other.dry_run_pending,
            batches=self.batches + other.batches,
        )


@dataclass(frozen=True)
class Coverage:
    source_type: str
    total: int
    covered: int

    @property
    def ratio(self) -> float:
        if self.total == 0:
            return 1.0
        return self.covered / self.total


@dataclass(frozen=True)
class SourceTypeCount:
    source_type: str
    searchable_count: int
    embedding_count: int


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _is_current(row: Any, *, model: str, dimension: int, hash_value: str) -> bool:
    return (
        row["existing_content_hash"] == hash_value
        and row["existing_model"] == model
        and row["existing_dimension"] == dimension
    )


def _refuses_pooler_url(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.port == 6543:
        return "port 6543 is a Supabase pooler endpoint"
    host = (parsed.hostname or "").casefold()
    if "pooler" in host or "pgbouncer" in host:
        return f"host {parsed.hostname!r} looks like a pooler endpoint"
    return None


def direct_database_url_from_env(env: dict[str, str] | None = None) -> str:
    env = env or os.environ
    url = (env.get("DIRECT_DATABASE_URL") or "").strip()
    if not url:
        raise ValueError("DIRECT_DATABASE_URL is required for embedding backfill")
    refusal = _refuses_pooler_url(url)
    if refusal:
        raise ValueError(f"refusing DIRECT_DATABASE_URL: {refusal}; use a direct session-mode URL")
    return url


@asynccontextmanager
async def _direct_connection(database_url: str) -> AsyncIterator[Any]:
    import asyncpg

    conn = await asyncpg.connect(database_url, statement_cache_size=0)
    try:
        yield conn
    finally:
        await conn.close()


async def fetch_coverage(conn: Any, *, model: str, dimension: int) -> list[Coverage]:
    rows = await conn.fetch(COVERAGE_SQL, model, dimension)
    return [
        Coverage(
            source_type=str(row["source_type"]),
            total=int(row["total"]),
            covered=int(row["covered"]),
        )
        for row in rows
    ]


async def fetch_source_type_counts(conn: Any) -> list[SourceTypeCount]:
    rows = await conn.fetch(SOURCE_TYPE_COUNTS_SQL)
    return [
        SourceTypeCount(
            source_type=str(row["source_type"]),
            searchable_count=int(row["searchable_count"]),
            embedding_count=int(row["embedding_count"]),
        )
        for row in rows
    ]


def _overall_coverage(rows: Sequence[Coverage]) -> Coverage:
    total = sum(row.total for row in rows)
    covered = sum(row.covered for row in rows)
    return Coverage(source_type="all", total=total, covered=covered)


async def build_hnsw_index_concurrently(conn: Any) -> None:
    """Build the ANN index without opening an explicit transaction."""

    await conn.execute(HNSW_INDEX_SQL)


async def _sleep_for_rate_limit(
    *,
    embedded_count: int,
    rate_limit_per_min: int,
    sleep: Any,
) -> None:
    if embedded_count <= 0 or rate_limit_per_min <= 0:
        return
    await sleep((60.0 / rate_limit_per_min) * embedded_count)


async def backfill_embeddings(
    conn: Any,
    *,
    embedder: Embedder,
    batch_size: int,
    rate_limit_per_min: int,
    dry_run: bool,
    sleep: Any = asyncio.sleep,
    now_factory: Any = _utc_now,
) -> BackfillTotals:
    """Backfill embeddings by keyset-scanning ``v_searchable_content``."""

    cursor_type = ""
    cursor_id = UUID("00000000-0000-0000-0000-000000000000")
    totals = BackfillTotals()
    while True:
        rows = await conn.fetch(SELECT_CANDIDATES_SQL, cursor_type, cursor_id, batch_size)
        if not rows:
            return totals
        cursor_type = rows[-1]["source_type"]
        cursor_id = rows[-1]["source_id"]
        pending: list[tuple[str, Any, str, str]] = []
        skipped_current = 0
        for row in rows:
            canonical_text = row["canonical_text"] or ""
            hash_value = content_hash(canonical_text)
            if _is_current(
                row,
                model=embedder.model_name,
                dimension=embedder.dimension,
                hash_value=hash_value,
            ):
                skipped_current += 1
            else:
                pending.append((row["source_type"], row["source_id"], canonical_text, hash_value))

        if dry_run:
            batch_totals = BackfillTotals(
                scanned=len(rows),
                skipped_current=skipped_current,
                dry_run_pending=len(pending),
                batches=1,
            )
            totals = totals.add(batch_totals)
            logger.info(
                "dry-run batch scanned=%d pending=%d current=%d cursor=%s",
                len(rows),
                len(pending),
                skipped_current,
                f"{cursor_type}:{cursor_id}",
            )
            continue

        embedded = 0
        failed = 0
        if pending:
            try:
                vectors = await embedder.embed_texts([item[2] for item in pending])
            except Exception:
                logger.exception("embedding provider batch failed at cursor=%s:%s", cursor_type, cursor_id)
                failed += len(pending)
                vectors = []
            for (source_type, source_id, _text, hash_value), vector in zip(pending, vectors, strict=False):
                try:
                    await conn.execute(
                        UPSERT_EMBEDDING_SQL,
                        source_type,
                        source_id,
                        _vector_literal(vector),
                        embedder.model_name,
                        embedder.dimension,
                        hash_value,
                        now_factory(),
                    )
                except Exception:
                    failed += 1
                    logger.exception("embedding upsert failed for source_type=%s source_id=%s", source_type, source_id)
                else:
                    embedded += 1
            if len(vectors) < len(pending):
                failed += len(pending) - len(vectors)

        totals = totals.add(
            BackfillTotals(
                scanned=len(rows),
                embedded=embedded,
                skipped_current=skipped_current,
                failed=failed,
                batches=1,
            )
        )
        logger.info(
            "batch scanned=%d embedded=%d failed=%d current=%d cursor=%s",
            len(rows),
            embedded,
            failed,
            skipped_current,
            f"{cursor_type}:{cursor_id}",
        )
        await _sleep_for_rate_limit(
            embedded_count=embedded,
            rate_limit_per_min=rate_limit_per_min,
            sleep=sleep,
        )


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill mediator.content_embeddings from mediator.v_searchable_content. "
            "Requires DIRECT_DATABASE_URL and refuses pooler endpoints."
        )
    )
    parser.add_argument("--dry-run", action="store_true", help="Report pending work without writes.")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Required for writes and concurrent index creation.",
    )
    parser.add_argument(
        "--build-index-concurrently",
        action="store_true",
        help="Build the pgvector HNSW index with CREATE INDEX CONCURRENTLY after backfill.",
    )
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument(
        "--rate-limit-per-min",
        type=int,
        default=DEFAULT_RATE_LIMIT_PER_MIN,
    )
    parser.add_argument(
        "--coverage-threshold",
        type=float,
        default=DEFAULT_COVERAGE_THRESHOLD,
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace, *, settings: Settings | None = None) -> int:
    settings = settings or get_settings()
    if args.batch_size < 1:
        print("error: --batch-size must be >= 1", file=sys.stderr)
        return 2
    if args.rate_limit_per_min < 1:
        print("error: --rate-limit-per-min must be >= 1", file=sys.stderr)
        return 2
    if not 0.0 <= args.coverage_threshold <= 1.0:
        print("error: --coverage-threshold must be between 0 and 1", file=sys.stderr)
        return 2
    if not args.dry_run and not args.yes:
        print("error: writes require --yes; use --dry-run to inspect only", file=sys.stderr)
        return 2
    if args.build_index_concurrently and not args.yes:
        print("error: --build-index-concurrently requires --yes", file=sys.stderr)
        return 2

    try:
        database_url = direct_database_url_from_env()
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    embedder = embedder_from_settings(settings)
    async with _direct_connection(database_url) as conn:
        before_source_counts = await fetch_source_type_counts(conn)
        for count in before_source_counts:
            logger.info(
                "source counts before source_type=%s searchable=%d embeddings=%d",
                count.source_type,
                count.searchable_count,
                count.embedding_count,
            )
        before_rows = await fetch_coverage(conn, model=embedder.model_name, dimension=embedder.dimension)
        for coverage in before_rows:
            logger.info(
                "coverage before source_type=%s: covered=%d total=%d ratio=%.4f",
                coverage.source_type,
                coverage.covered,
                coverage.total,
                coverage.ratio,
            )
        before = _overall_coverage(before_rows)
        logger.info("coverage before all: covered=%d total=%d ratio=%.4f", before.covered, before.total, before.ratio)
        totals = await backfill_embeddings(
            conn,
            embedder=embedder,
            batch_size=args.batch_size,
            rate_limit_per_min=args.rate_limit_per_min,
            dry_run=args.dry_run,
        )
        after_rows = await fetch_coverage(conn, model=embedder.model_name, dimension=embedder.dimension)
        after_source_counts = await fetch_source_type_counts(conn)
        logger.info(
            "backfill done dry_run=%s scanned=%d embedded=%d pending=%d current=%d failed=%d",
            args.dry_run,
            totals.scanned,
            totals.embedded,
            totals.dry_run_pending,
            totals.skipped_current,
            totals.failed,
        )
        for coverage in after_rows:
            logger.info(
                "coverage after source_type=%s: covered=%d total=%d ratio=%.4f threshold=%.4f",
                coverage.source_type,
                coverage.covered,
                coverage.total,
                coverage.ratio,
                args.coverage_threshold,
            )
        after = _overall_coverage(after_rows)
        logger.info(
            "coverage after all: covered=%d total=%d ratio=%.4f threshold=%.4f",
            after.covered,
            after.total,
            after.ratio,
            args.coverage_threshold,
        )
        for count in after_source_counts:
            logger.info(
                "source counts after source_type=%s searchable=%d embeddings=%d",
                count.source_type,
                count.searchable_count,
                count.embedding_count,
            )
        if args.build_index_concurrently:
            logger.info("building HNSW index concurrently outside an explicit transaction")
            await build_hnsw_index_concurrently(conn)
        if not args.dry_run and after.ratio < args.coverage_threshold:
            logger.error(
                "coverage threshold not met: ratio=%.4f threshold=%.4f",
                after.ratio,
                args.coverage_threshold,
            )
            return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
