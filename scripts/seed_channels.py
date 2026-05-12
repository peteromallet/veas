#!/usr/bin/env python3
"""Seed channels from environment variables — idempotent, credentials-optional.

Post-migration script. Run after 0020_topics_bots_bindings.sql is applied.
Each transport block independently reads its env var; if absent, logs INFO and skips.
WhatsApp is optional (WHATSA_PHONE_NUMBER_ID may not be set).

Usage:
    python scripts/seed_channels.py

Requires:
    DISCORD_BOT_TOKEN (required for discord channel)
    DISCORD_BOT_USER_ID (optional — derived from token if unset)
    WHATSAPP_PHONE_NUMBER_ID (optional — skipped if absent)
    DATABASE_URL or PG* env vars for asyncpg connection
"""

from __future__ import annotations

import asyncio
import logging
import os

import asyncpg

from app.services.discord_id import _decode_discord_user_id

logger = logging.getLogger(__name__)


def _env(key: str) -> str | None:
    value = os.getenv(key)
    return value.strip() if value else None


async def _get_pool() -> asyncpg.Pool:
    # statement_cache_size=0 is required for Supabase's transaction-mode pooler
    # (port 6543). Safe to set unconditionally — only disables a local cache.
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


async def seed_discord(pool: asyncpg.Pool) -> bool:
    """Seed discord channel. Returns True if seeded, False if skipped."""
    bot_token = _env("DISCORD_BOT_TOKEN")
    if not bot_token:
        logger.info("DISCORD_BOT_TOKEN not set — skipping discord channel seed")
        return False

    bot_user_id = _env("DISCORD_BOT_USER_ID")
    if not bot_user_id:
        # Derive from token: the first segment is a base64url-encoded user id.
        bot_user_id = _decode_discord_user_id(bot_token)
        if bot_user_id is None or not bot_user_id.isdigit():
            logger.warning(
                "Could not decode DISCORD_BOT_USER_ID from token; "
                "set DISCORD_BOT_USER_ID explicitly or check token format"
            )
            return False

    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            INSERT INTO channels (bot_id, transport, address, guild_id, channel_id)
            VALUES ('mediator', 'discord', $1, NULL, NULL)
            ON CONFLICT (transport, address, COALESCE(guild_id, ''), COALESCE(channel_id, ''))
            DO NOTHING
            """,
            bot_user_id,
        )
    inserted = result != "INSERT 0 0"
    if inserted:
        logger.info("Seeded discord channel: address=%s", bot_user_id)
    else:
        logger.info("Discord channel already exists: address=%s", bot_user_id)
    return True


async def seed_whatsapp(pool: asyncpg.Pool) -> bool:
    """Seed whatsapp channel. Returns True if seeded, False if skipped."""
    phone_number_id = _env("WHATSAPP_PHONE_NUMBER_ID")
    if not phone_number_id:
        logger.info("WHATSAPP_PHONE_NUMBER_ID not set — skipping whatsapp channel seed")
        return False

    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            INSERT INTO channels (bot_id, transport, address, guild_id, channel_id)
            VALUES ('mediator', 'whatsapp', $1, NULL, NULL)
            ON CONFLICT (transport, address, COALESCE(guild_id, ''), COALESCE(channel_id, ''))
            DO NOTHING
            """,
            phone_number_id,
        )
    inserted = result != "INSERT 0 0"
    if inserted:
        logger.info("Seeded whatsapp channel: address=%s", phone_number_id)
    else:
        logger.info("Whatsapp channel already exists: address=%s", phone_number_id)
    return True


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    pool = await _get_pool()
    try:
        discord_ok = await seed_discord(pool)
        whatsapp_ok = await seed_whatsapp(pool)

        if not discord_ok and not whatsapp_ok:
            logger.warning(
                "No channels seeded — set DISCORD_BOT_TOKEN or WHATSAPP_PHONE_NUMBER_ID"
            )
        else:
            logger.info("Channel seeding complete")
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())