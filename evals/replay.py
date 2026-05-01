from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TextIO
from uuid import UUID

from app.models.user import User, fetch_user_by_id
from app.services.turn_context import partner_of
from evals.execution import run_eval_turn
from evals.state import (
    diff_snapshots,
    outbound_text,
    snapshot_state,
    oob_outcome,
)


@dataclass(frozen=True)
class ReplayRecord:
    message_id: str
    prompt_version: str
    would_send: str
    would_write: dict[str, Any]
    tool_transcript: list[dict[str, Any]]
    oob_outcome: str | None
    charge: str | None
    cost_usd: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "prompt_version": self.prompt_version,
            "would_send": self.would_send,
            "would_write": self.would_write,
            "tool_transcript": self.tool_transcript,
            "oob_outcome": self.oob_outcome,
            "charge": self.charge,
            "cost_usd": self.cost_usd,
        }


async def replay_history(
    source_pool: Any,
    scratch_pool: Any,
    *,
    since: str,
    user_id: str,
    prompt_version: str,
    output: TextIO,
) -> list[ReplayRecord]:
    source_user = await fetch_user_by_id(source_pool, UUID(user_id))
    source_partner = await partner_of(source_pool, source_user)
    rows = await _history_rows(source_pool, source_user.id, since)
    records: list[ReplayRecord] = []
    for row in rows:
        user = await _copy_user(scratch_pool, source_user)
        await _copy_user(scratch_pool, source_partner)
        message_id = await _copy_message(scratch_pool, row, user.id, target=True)
        before = await snapshot_state(scratch_pool)
        execution = await run_eval_turn(scratch_pool, [message_id], user, prompt_version=prompt_version)
        after = await snapshot_state(scratch_pool)
        diff = diff_snapshots(before, after)
        record = ReplayRecord(
            message_id=str(row["id"]),
            prompt_version=prompt_version,
            would_send=outbound_text(after),
            would_write=_primitive_summary(diff),
            tool_transcript=execution.tool_calls,
            oob_outcome=oob_outcome(after),
            charge=after.tables.get("messages", {}).get(str(message_id), {}).get("charge"),
            cost_usd=str(diff.cost_delta_usd),
        )
        output.write(json.dumps(record.as_dict(), default=str) + "\n")
        records.append(record)
    return records


async def _history_rows(pool: Any, user_id: UUID, since: str) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        """
        SELECT id, content, sent_at, charge, whatsapp_message_id, media_type, media_url,
               media_duration_seconds, media_analysis
        FROM messages
        WHERE direction='inbound'
          AND sender_id=$1
          AND sent_at >= $2::timestamptz
        ORDER BY sent_at ASC
        """,
        user_id,
        since,
    )
    return [dict(row) for row in rows]


async def _copy_user(pool: Any, user: User) -> User:
    row = {
        "id": user.id,
        "name": user.name,
        "phone": user.phone,
        "timezone": user.timezone,
        "onboarding_state": "welcomed",
    }
    if hasattr(pool, "users"):
        existing = pool.users.get(user.id)
        pool.users[user.id] = {**row, **({"style_notes": existing.get("style_notes", "")} if existing else {})}
    else:
        await pool.fetchrow(
            """
            INSERT INTO users (id, name, phone, timezone, onboarding_state)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (phone) DO UPDATE
            SET name=EXCLUDED.name, timezone=EXCLUDED.timezone, onboarding_state=EXCLUDED.onboarding_state
            RETURNING id
            """,
            row["id"],
            row["name"],
            row["phone"],
            row["timezone"],
            row["onboarding_state"],
        )
    return User(user.id, user.name, user.phone, user.timezone, "welcomed")


async def _copy_message(pool: Any, row: dict[str, Any], user_id: UUID, *, target: bool) -> UUID:
    message_id = row["id"]
    data = {
        "id": message_id,
        "direction": "inbound",
        "sender_id": user_id,
        "recipient_id": None,
        "content": row.get("content"),
        "processing_state": "raw" if target else "processed",
        "sent_at": row.get("sent_at") or datetime.now(UTC),
        "charge": row.get("charge") or "routine",
        "whatsapp_message_id": row.get("whatsapp_message_id") or f"replay-{message_id}",
        "media_type": row.get("media_type"),
        "media_url": row.get("media_url"),
        "media_duration_seconds": row.get("media_duration_seconds"),
        "media_analysis": row.get("media_analysis"),
        "edit_history": None,
        "edited_at": None,
        "deleted_at": None,
    }
    if hasattr(pool, "messages"):
        pool.messages[message_id] = data
        return message_id
    await pool.fetchrow(
        """
        INSERT INTO messages (
            id, direction, sender_id, content, processing_state, whatsapp_message_id, sent_at,
            charge, media_type, media_url, media_duration_seconds, media_analysis
        )
        VALUES ($1, 'inbound', $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        ON CONFLICT (id) DO UPDATE
        SET content=EXCLUDED.content,
            processing_state=EXCLUDED.processing_state,
            charge=EXCLUDED.charge
        RETURNING id
        """,
        data["id"],
        data["sender_id"],
        data["content"],
        data["processing_state"],
        data["whatsapp_message_id"],
        data["sent_at"],
        data["charge"],
        data["media_type"],
        data["media_url"],
        data["media_duration_seconds"],
        data["media_analysis"],
    )
    return message_id


def _primitive_summary(diff: Any) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for table, table_diff in diff.primitive_tables.items():
        changed = {
            "inserted": len(table_diff.inserted),
            "updated": len(table_diff.updated),
            "deleted": len(table_diff.deleted),
        }
        if any(changed.values()):
            summary[table] = changed
    tool_calls = persisted_tool_calls_from_diff(diff)
    if tool_calls:
        summary["tool_calls"] = tool_calls
    return summary


def persisted_tool_calls_from_diff(diff: Any) -> list[dict[str, Any]]:
    rows = diff.tables.get("tool_calls")
    if rows is None:
        return []
    return [
        {
            "tool_name": row.get("tool_name"),
            "arguments": row.get("arguments"),
        }
        for row in rows.inserted
    ]
