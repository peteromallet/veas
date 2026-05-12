"""Durable, privacy-conscious diagnostic events for bot turns."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Any, Mapping
from uuid import UUID

from app.services.crypto import encrypt_value

logger = logging.getLogger(__name__)

_SENSITIVE_KEY_PARTS = (
    "content",
    "text",
    "prompt",
    "reasoning",
    "raw",
    "sensitive",
    "secret",
    "argument",
    "result",
    "response",
    "message_body",
)


def _hash_text(value: str) -> dict[str, Any]:
    return {
        "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
        "length": len(value),
    }


def _safe_value(key: str, value: Any) -> Any:
    lowered = key.lower()
    if any(part in lowered for part in _SENSITIVE_KEY_PARTS):
        return _hash_text(json.dumps(value, default=str, sort_keys=True) if not isinstance(value, str) else value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, str):
        return value if len(value) <= 160 else _hash_text(value)
    if isinstance(value, Mapping):
        return {str(child_key): _safe_value(str(child_key), child_value) for child_key, child_value in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_safe_value(key, item) for item in list(value)[:20]]
    return str(value)


def safe_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    if not metadata:
        return {}
    return {str(key): _safe_value(str(key), value) for key, value in metadata.items()}


async def record_turn_event(
    pool: Any,
    turn_id: UUID | None,
    event_type: str,
    *,
    step: str | None = None,
    severity: str = "info",
    actor: str = "system",
    message: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    sensitive_metadata: Mapping[str, Any] | None = None,
    occurred_at: datetime | None = None,
    duration_ms: int | None = None,
) -> None:
    """Insert one diagnostic event without making turn execution depend on audit writes."""

    if pool is None or turn_id is None:
        return
    safe = safe_metadata(metadata)
    encrypted_sensitive = None
    if sensitive_metadata:
        encrypted_sensitive = encrypt_value(json.dumps(sensitive_metadata, default=str, sort_keys=True))
    try:
        await pool.fetchrow(
            """
            INSERT INTO turn_audit_events (
                turn_id, event_seq, event_type, step, severity, occurred_at,
                duration_ms, actor, message, metadata, sensitive_metadata_encrypted
            )
            SELECT $1,
                   COALESCE(MAX(event_seq), 0) + 1,
                   $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10
            FROM turn_audit_events
            WHERE turn_id = $1
            RETURNING id, event_seq
            """,
            turn_id,
            event_type,
            step,
            severity,
            occurred_at or datetime.now(UTC),
            duration_ms,
            actor,
            message,
            json.dumps(safe, default=str, sort_keys=True),
            encrypted_sensitive,
        )
    except Exception:
        # obs N/A: audit fallback
        logger.exception("failed to record turn audit event turn_id=%s event_type=%s", turn_id, event_type)
