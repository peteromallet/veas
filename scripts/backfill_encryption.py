"""Backfill *_encrypted columns for sensitive content.

Idempotent: only encrypts rows where the encrypted column is currently NULL.
Safe to interrupt and re-run.

Usage:
    DATA_ENCRYPTION_KEY=<base64-32-byte-key> DATABASE_URL=... \
        python -m scripts.backfill_encryption

After this completes and you have verified ciphertext is present in every row
(and that the application reads it back correctly), you can schedule a
follow-up migration to DROP the plaintext columns.
"""

from __future__ import annotations

import argparse
import asyncio
import os

import asyncpg

from app.services.crypto import encrypt_value, is_configured


# (table, plaintext_col, encrypted_col)
TARGETS = (
    ("out_of_bounds", "sensitive_core", "sensitive_core_encrypted"),
    ("messages", "content", "content_encrypted"),
    ("memories", "content", "content_encrypted"),
    ("bot_turns", "reasoning", "reasoning_encrypted"),
    ("observations", "content", "content_encrypted"),
)


async def _backfill_one(conn: asyncpg.Connection, table: str, plain: str, enc: str, batch_size: int) -> int:
    """Encrypt rows in chunks of ``batch_size``. Returns total rows updated."""
    total = 0
    while True:
        rows = await conn.fetch(
            f"SELECT id, {plain} FROM {table} "
            f"WHERE {enc} IS NULL AND {plain} IS NOT NULL "
            f"LIMIT {batch_size}"
        )
        if not rows:
            break
        async with conn.transaction():
            for row in rows:
                ct = encrypt_value(row[plain])
                await conn.execute(
                    f"UPDATE {table} SET {enc} = $1 WHERE id = $2 AND {enc} IS NULL",
                    ct,
                    row["id"],
                )
        total += len(rows)
        print(f"  {table}: {total} rows backfilled", flush=True)
    return total


async def main_async(batch_size: int) -> int:
    if not is_configured():
        print(
            "DATA_ENCRYPTION_KEY is not set; refusing to backfill (rows would store plaintext bytes).",
            flush=True,
        )
        return 2
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is required", flush=True)
        return 2
    conn = await asyncpg.connect(database_url)
    try:
        for table, plain, enc in TARGETS:
            print(f"backfilling {table}.{enc}…", flush=True)
            count = await _backfill_one(conn, table, plain, enc, batch_size)
            print(f"  {table}: done ({count} rows total)", flush=True)
    finally:
        await conn.close()
    print("backfill complete", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=200)
    args = parser.parse_args()
    return asyncio.run(main_async(args.batch_size))


if __name__ == "__main__":
    raise SystemExit(main())
