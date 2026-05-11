#!/usr/bin/env python3
"""Resumable backfill: populate artifact_topics for every existing artifact row.

Three-phase per table (memories, themes, observations, watch_items,
out_of_bounds, distillations):

  Phase A  – record backfill_started_at in migration_progress.
  Phase B  – cursor-keyed batch loop (id > last_id ORDER BY id LIMIT).
  Phase C  – catch-up pass for rows created during Phase B that were
             missed because UUID ordering is non-chronological.

Every existing out_of_bounds row is classified as relationship-topic-scoped
per the locked S1 decision (§16.8).

Usage:
    DATABASE_URL=... python scripts/backfill_artifact_topics.py

The script is idempotent: tables with completed_at set are skipped.
Interrupt and re-run safely — it resumes from migration_progress.last_id.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import os
import sys
from typing import Optional

import asyncpg

logger = logging.getLogger(__name__)

BATCH_SIZE = 10_000

# Each entry: (table_name, artifact_table_label, timestamp_col)
# artifact_table_label is the literal stored in artifact_topics.artifact_table.
# timestamp_col is used for Phase C catch-up.
ARTIFACT_TABLES: list[tuple[str, str, str]] = [
    ("memories", "memories", "created_at"),
    ("themes", "themes", "first_seen_at"),
    ("observations", "observations", "created_at"),
    ("watch_items", "watch_items", "created_at"),
    ("out_of_bounds", "out_of_bounds", "created_at"),
    ("distillations", "distillations", "created_at"),
]

# Lock IDs — one per table so concurrent runs don't collide on the same table.
# Use sha1 (not Python's hash()) because hash() is salted per-process since
# Python 3.3, so the same table name would get a different lock id across runs.
def _lock_id(table_name: str) -> int:
    digest = hashlib.sha1(f"s1_artifact_topics.{table_name}".encode()).digest()
    return int.from_bytes(digest[:4], "big") % (2**31 - 1)


def _env(key: str) -> Optional[str]:
    value = os.getenv(key)
    return value.strip() if value else None


async def _get_pool() -> asyncpg.Pool:
    # statement_cache_size=0 is required for Supabase's transaction-mode pooler
    # (port 6543): asyncpg's prepared-statement cache breaks because the same
    # underlying connection serves multiple transactions. Safe to set
    # unconditionally — it just disables a cache.
    database_url = _env("DATABASE_URL")
    if database_url:
        return await asyncpg.create_pool(
            dsn=database_url, min_size=1, max_size=2, statement_cache_size=0
        )
    return await asyncpg.create_pool(
        host=_env("PGHOST") or "localhost",
        port=int(_env("PGPORT") or "5432"),
        user=_env("PGUSER") or "postgres",
        password=_env("PGPASSWORD") or "",
        database=_env("PGDATABASE") or "postgres",
        min_size=1,
        max_size=2,
        statement_cache_size=0,
    )


async def _get_relationship_topic_id(pool: asyncpg.Pool) -> str:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM topics WHERE slug = 'relationship'"
        )
    if row is None:
        raise RuntimeError(
            "Relationship topic not found in topics table. "
            "Ensure migration 0020 has been applied."
        )
    return str(row["id"])


# ---------------------------------------------------------------------------
# Phase A
# ---------------------------------------------------------------------------


async def _phase_a_record_start(
    conn: asyncpg.Connection, table_name: str
) -> None:
    """Insert or update backfill_started_at in migration_progress."""
    await conn.execute(
        """
        INSERT INTO migration_progress (table_name, backfill_started_at)
        VALUES ($1, now())
        ON CONFLICT (table_name) DO UPDATE
            SET backfill_started_at = EXCLUDED.backfill_started_at
        """,
        table_name,
    )


# ---------------------------------------------------------------------------
# Phase B — cursor-keyed batch loop
# ---------------------------------------------------------------------------


_CTE_BATCH_SQL = """
-- WARNING: ins and src CTEs must stay aligned — drift silently mis-advances cursor.
-- The WHERE / ORDER BY / LIMIT clauses MUST be identical in both CTEs.
WITH
src AS (
    SELECT id
    FROM {table}
    WHERE id > $1
    ORDER BY id
    LIMIT $2
),
ins AS (
    INSERT INTO artifact_topics
        (artifact_table, artifact_id, topic_id, tagged_by_bot_id, reason)
    SELECT
        $3,
        src.id,
        $4,
        'mediator',
        'S1 backfill: existing row assigned to relationship topic'
    FROM src
    ON CONFLICT (artifact_table, artifact_id, topic_id) DO NOTHING
    RETURNING artifact_id
)
-- Postgres has no max(uuid) aggregate; the src CTE is already ORDER BY id,
-- so the last id in that ordered set is the max we want.
SELECT COALESCE((SELECT id FROM src ORDER BY id DESC LIMIT 1), $1) AS new_last_id
"""


async def _phase_b_batch_loop(
    conn: asyncpg.Connection,
    table_name: str,
    artifact_label: str,
    topic_id: str,
) -> int:
    """Cursor-keyed batch loop. Returns total rows scanned."""
    sql = _CTE_BATCH_SQL.format(table=table_name)
    total_scanned = 0

    # Read current cursor position
    row = await conn.fetchrow(
        "SELECT last_id FROM migration_progress WHERE table_name = $1",
        table_name,
    )
    last_id = row["last_id"] if row and row["last_id"] else None
    if last_id is None:
        last_id = "00000000-0000-0000-0000-000000000000"

    while True:
        async with conn.transaction():
            result = await conn.fetchval(
                sql,
                last_id,
                BATCH_SIZE,
                artifact_label,
                topic_id,
            )
            new_last_id = result if result else last_id

            if str(new_last_id) == str(last_id):
                # No rows returned — done
                break

            last_id = str(new_last_id)
            total_scanned += 1  # batch counter

            # Update cursor
            await conn.execute(
                """
                UPDATE migration_progress
                SET last_id = $2
                WHERE table_name = $1
                """,
                table_name,
                last_id,
            )

        logger.info(
            "  %s: cursor %s (%d batches so far)",
            table_name,
            last_id[:8],
            total_scanned,
        )

    return total_scanned


# ---------------------------------------------------------------------------
# Phase C — catch-up
# ---------------------------------------------------------------------------


async def _phase_c_catchup(
    conn: asyncpg.Connection,
    table_name: str,
    artifact_label: str,
    timestamp_col: str,
    topic_id: str,
) -> int:
    """Catch up on rows created during Phase B whose UUID fell behind the cursor."""
    # Read start_time from migration_progress
    row = await conn.fetchrow(
        "SELECT backfill_started_at FROM migration_progress WHERE table_name = $1",
        table_name,
    )
    start_time = row["backfill_started_at"] if row else None
    if start_time is None:
        logger.warning(
            "  %s: no backfill_started_at — skipping Phase C", table_name
        )
        return 0

    result = await conn.execute(
        f"""
        INSERT INTO artifact_topics
            (artifact_table, artifact_id, topic_id, tagged_by_bot_id, reason)
        SELECT
            $1,
            t.id,
            $2,
            'mediator',
            'S1 backfill: Phase C catch-up'
        FROM {table_name} t
        WHERE t.{timestamp_col} >= $3
          AND NOT EXISTS (
              SELECT 1 FROM artifact_topics at2
              WHERE at2.artifact_table = $1
                AND at2.artifact_id = t.id
          )
        ON CONFLICT (artifact_table, artifact_id, topic_id) DO NOTHING
        """,
        artifact_label,
        topic_id,
        start_time,
    )

    count_str = result.split()[-1] if result else "0"
    try:
        inserted = int(count_str)
    except ValueError:
        inserted = 0

    if inserted:
        logger.info("  %s: Phase C caught up %d rows", table_name, inserted)
    return inserted


# ---------------------------------------------------------------------------
# Per-table runner
# ---------------------------------------------------------------------------


async def _backfill_table(
    pool: asyncpg.Pool,
    table_name: str,
    artifact_label: str,
    timestamp_col: str,
    topic_id: str,
) -> None:
    lock_id = _lock_id(table_name)

    # Skip if already completed
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT completed_at FROM migration_progress WHERE table_name = $1",
            table_name,
        )
        if row and row["completed_at"] is not None:
            logger.info(
                "%s: already completed at %s — skipping", table_name, row["completed_at"]
            )
            return

    logger.info("%s: acquiring advisory lock %s", table_name, lock_id)
    async with pool.acquire() as conn:
        await conn.execute("SELECT pg_advisory_lock($1)", lock_id)
        try:
            await _backfill_table_locked(
                conn, table_name, artifact_label, timestamp_col, topic_id
            )
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", lock_id)
            logger.info("%s: released advisory lock %s", table_name, lock_id)


async def _backfill_table_locked(
    conn: asyncpg.Connection,
    table_name: str,
    artifact_label: str,
    timestamp_col: str,
    topic_id: str,
) -> None:
    # Phase A
    logger.info("%s: Phase A — recording start time", table_name)
    await _phase_a_record_start(conn, table_name)

    # Phase B
    logger.info("%s: Phase B — cursor-keyed batch loop", table_name)
    batches = await _phase_b_batch_loop(
        conn, table_name, artifact_label, topic_id
    )
    logger.info("%s: Phase B complete (%d batches)", table_name, batches)

    # Phase C
    logger.info("%s: Phase C — catch-up on rows created during backfill", table_name)
    caught = await _phase_c_catchup(
        conn, table_name, artifact_label, timestamp_col, topic_id
    )
    logger.info("%s: Phase C complete (%d rows caught up)", table_name, caught)

    # Mark completed
    await conn.execute(
        """
        UPDATE migration_progress
        SET completed_at = now()
        WHERE table_name = $1
        """,
        table_name,
    )
    logger.info("%s: marked completed", table_name)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main_async() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    pool = await _get_pool()
    try:
        topic_id = await _get_relationship_topic_id(pool)
        logger.info("Relationship topic id: %s", topic_id)

        for table_name, artifact_label, ts_col in ARTIFACT_TABLES:
            logger.info("=" * 60)
            logger.info("Processing: %s", table_name)
            await _backfill_table(
                pool, table_name, artifact_label, ts_col, topic_id
            )

        logger.info("=" * 60)
        logger.info("Backfill complete for all tables")
    finally:
        await pool.close()

    return 0


def main() -> int:
    global BATCH_SIZE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--batch-size",
        type=int,
        default=BATCH_SIZE,
        help=f"Rows per cursor batch (default: {BATCH_SIZE})",
    )
    args = parser.parse_args()
    BATCH_SIZE = args.batch_size
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())