"""Dry-run staging utilities."""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.config import get_settings
from app.db import db_lifespan
from app.models.user import User, fetch_user_by_id
from app.services.hot_context import build_hot_context, render_hot_context
from app.services.prompts import render_system_prompt
from app.services.turn_context import partner_of


@dataclass
class _App:
    state: Any


class _State:
    pool: Any = None


async def _replay(pool: Any, prompt_version: str, since: str, user_id: str) -> None:
    user = await fetch_user_by_id(pool, UUID(user_id))
    partner = await partner_of(pool, user)
    rows = await pool.fetch(
        """
        SELECT id, content, sent_at
        FROM messages
        WHERE direction='inbound'
          AND sender_id=$1
          AND sent_at >= $2::timestamptz
        ORDER BY sent_at ASC
        """,
        user.id,
        since,
    )
    settings = get_settings()
    for row in rows:
        hot_context = await build_hot_context(pool, user, partner, [row["id"]], {"kind": "staging_replay"})
        system_prompt = render_system_prompt(settings.assistant_name, user.name, partner.name)
        rendered = render_hot_context(hot_context)
        candidate = (
            f"[dry-run:{prompt_version}] Would answer {user.name} after message {row['id']}: "
            f"{str(row.get('content') or '').strip()[:160]}"
        )
        print(json.dumps({
            "message_id": str(row["id"]),
            "sent_at": str(row["sent_at"]),
            "prompt_version": prompt_version,
            "prompt_preview": f"{system_prompt}\n\n{rendered}"[:1000],
            "would_send": candidate,
            "would_write": [
                {"table": "bot_turns", "action": "insert"},
                {"table": "messages", "action": "insert_outbound"},
                {"table": "tool_calls", "action": "dry_run_record_only"},
            ],
        }, default=str))


async def _main_async(args: argparse.Namespace) -> None:
    app = _App(_State())
    async with db_lifespan(app):
        await _replay(app.state.pool, args.prompt_version, args.since, args.user)


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m app.staging")
    sub = parser.add_subparsers(dest="command", required=True)
    replay = sub.add_parser("replay")
    replay.add_argument("--prompt-version", required=True)
    replay.add_argument("--since", required=True)
    replay.add_argument("--user", required=True)
    args = parser.parse_args()
    if args.command == "replay":
        asyncio.run(_main_async(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
