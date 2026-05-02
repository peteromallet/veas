"""Small user model helpers shared by ingestion, debouncing, and recovery."""

import json
from dataclasses import dataclass, field
from typing import Any, Mapping
from uuid import UUID

from app.config import Settings, get_settings


@dataclass(frozen=True)
class User:
    id: UUID
    name: str
    phone: str
    timezone: str
    onboarding_state: str = "pending"
    pacing_preferences: dict[str, Any] = field(default_factory=dict)
    cross_thread_sharing_default: str | None = None


def _clamp_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return min(max(number, minimum), maximum)


def _clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return min(max(number, minimum), maximum)


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return default


def resolve_pacing_preferences(
    raw_preferences: Mapping[str, Any] | None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Return a bounded pacing preference snapshot safe for timing decisions."""
    settings = settings or get_settings()
    raw = dict(raw_preferences or {})
    max_wait_s = _clamp_float(
        raw.get("max_wait_s"),
        settings.discord_pacing_max_wait_s,
        max(settings.discord_pacing_min_wait_s, 1.0),
        60.0,
    )
    resolved = {
        "enabled": _coerce_bool(raw.get("enabled"), settings.discord_pacing_enabled),
        "burst_window_s": _clamp_float(
            raw.get("burst_window_s"),
            settings.discord_pacing_burst_window_s,
            0.25,
            min(max_wait_s, 15.0),
        ),
        "min_wait_s": _clamp_float(
            raw.get("min_wait_s"),
            settings.discord_pacing_min_wait_s,
            0.0,
            min(settings.discord_pacing_max_wait_s, 10.0),
        ),
        "max_wait_s": max_wait_s,
        "typing_grace_s": _clamp_float(
            raw.get("typing_grace_s"),
            settings.discord_pacing_typing_grace_s,
            0.5,
            30.0,
        ),
        "max_typing_wait_s": _clamp_float(
            raw.get("max_typing_wait_s"),
            settings.discord_pacing_max_typing_wait_s,
            1.0,
            90.0,
        ),
        "answer_typing_min_s": _clamp_float(
            raw.get("answer_typing_min_s"),
            settings.discord_pacing_answer_typing_min_s,
            0.0,
            20.0,
        ),
        "answer_typing_max_s": _clamp_float(
            raw.get("answer_typing_max_s"),
            settings.discord_pacing_answer_typing_max_s,
            0.5,
            45.0,
        ),
        "answer_chars_per_s": _clamp_float(
            raw.get("answer_chars_per_s"),
            settings.discord_pacing_answer_chars_per_s,
            4.0,
            80.0,
        ),
        "reactions_enabled": _coerce_bool(
            raw.get("reactions_enabled"),
            settings.discord_pacing_reactions_enabled,
        ),
        "reaction_daily_limit": _clamp_int(
            raw.get("reaction_daily_limit"),
            settings.discord_pacing_reaction_daily_limit,
            0,
            100,
        ),
    }
    if resolved["min_wait_s"] > resolved["max_wait_s"]:
        resolved["min_wait_s"] = resolved["max_wait_s"]
    if resolved["answer_typing_min_s"] > resolved["answer_typing_max_s"]:
        resolved["answer_typing_min_s"] = resolved["answer_typing_max_s"]
    return resolved


def _normalize_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        if isinstance(decoded, Mapping):
            return dict(decoded)
    return {}


def _row_to_user(row: Any) -> User:
    onboarding_state = row["onboarding_state"] if "onboarding_state" in row else "pending"
    pacing_preferences = row["pacing_preferences"] if "pacing_preferences" in row else {}
    cross_thread_sharing_default = row["cross_thread_sharing_default"] if "cross_thread_sharing_default" in row else None
    return User(
        id=row["id"],
        name=row["name"],
        phone=row["phone"],
        timezone=row["timezone"],
        onboarding_state=onboarding_state,
        pacing_preferences=_normalize_json_object(pacing_preferences),
        cross_thread_sharing_default=cross_thread_sharing_default,
    )


async def fetch_user_by_id(pool: Any, user_id: UUID) -> User:
    row = await pool.fetchrow(
        """
        SELECT id, name, phone, timezone, onboarding_state, pacing_preferences, cross_thread_sharing_default
        FROM users
        WHERE id = $1
        """,
        user_id,
    )
    return _row_to_user(row)


async def upsert_user(pool: Any, name: str, phone: str, default_tz: str) -> User:
    row = await pool.fetchrow(
        """
        INSERT INTO users (name, phone, timezone)
        VALUES ($1, $2, $3)
        ON CONFLICT (phone) DO UPDATE SET name = EXCLUDED.name
        RETURNING id, name, phone, timezone, onboarding_state, pacing_preferences, cross_thread_sharing_default
        """,
        name,
        phone,
        default_tz,
    )
    return _row_to_user(row)


async def fetch_user_pacing_preferences(pool: Any, user_id: UUID) -> dict[str, Any]:
    row = await pool.fetchrow(
        "SELECT pacing_preferences FROM users WHERE id = $1",
        user_id,
    )
    raw = row["pacing_preferences"] if row is not None else {}
    return resolve_pacing_preferences(_normalize_json_object(raw))


async def update_user_pacing_preferences(
    pool: Any,
    user_id: UUID,
    preferences: Mapping[str, Any],
) -> dict[str, Any]:
    bounded = resolve_pacing_preferences(preferences)
    row = await pool.fetchrow(
        """
        UPDATE users
        SET pacing_preferences = $2::jsonb
        WHERE id = $1
        RETURNING pacing_preferences
        """,
        user_id,
        json.dumps(bounded, default=str),
    )
    raw = row["pacing_preferences"] if row is not None else bounded
    return resolve_pacing_preferences(_normalize_json_object(raw))


async def record_pacing_event(
    pool: Any,
    *,
    user_id: UUID,
    message_ids: list[UUID],
    source: str,
    decision: str,
    reason: str,
    signal_snapshot: Mapping[str, Any] | None = None,
    preference_snapshot: Mapping[str, Any] | None = None,
    wait_ms: int | None = None,
    reaction: str | None = None,
    llm_judgement: Mapping[str, Any] | None = None,
) -> UUID:
    row = await pool.fetchrow(
        """
        INSERT INTO pacing_events (
            user_id,
            message_ids,
            source,
            decision,
            reason,
            signal_snapshot,
            preference_snapshot,
            wait_ms,
            reaction,
            llm_judgement
        )
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb, $8, $9, $10::jsonb)
        RETURNING id
        """,
        user_id,
        message_ids,
        source,
        decision,
        reason,
        json.dumps(dict(signal_snapshot or {}), default=str),
        json.dumps(dict(preference_snapshot or {}), default=str),
        wait_ms,
        reaction,
        json.dumps(dict(llm_judgement or {}), default=str) if llm_judgement is not None else None,
    )
    return row["id"]


async def claim_onboarding_welcome(pool: Any, user_id: UUID) -> bool:
    row = await pool.fetchrow(
        """
        UPDATE users
        SET onboarding_state='welcomed'
        WHERE id=$1 AND onboarding_state='pending'
        RETURNING id
        """,
        user_id,
    )
    return row is not None
