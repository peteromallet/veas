"""Solo hot context construction (Sprint 5).

Mirrors hot_context.py but for a single-user bot: single about-user bucket,
no partner, no bridge candidates, no cross-thread sharing defaults.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from app.config import get_settings
from app.models.user import User
from app.services.text_safety import clean_user_facing_text, looks_like_internal_process_text
from app.services.time_context import add_calendar_months, temporal_reference, timezone_or_utc
from app.services.tools.common import media_analysis_text
from app.services.hot_context import peek_other_topics
from app.services.topic_filter import join_artifact_topics


@dataclass
class HotContextSolo:
    current_user: dict[str, Any]
    partner_user: dict[str, Any]  # always empty dict for solo
    conversation_load: dict[str, Any]
    active_oob: list[dict[str, Any]]
    memories: list[dict[str, Any]]
    active_themes: list[dict[str, Any]]
    open_watch_items: list[dict[str, Any]]
    observations: list[dict[str, Any]]
    recent_messages: list[dict[str, Any]]
    time_since_last_message: str | None
    trigger_metadata: dict[str, Any]
    temporal_context: dict[str, Any] = field(default_factory=dict)
    distillations: list[dict[str, Any]] = field(default_factory=list)
    bridge_candidates: list[dict[str, Any]] = field(default_factory=list)
    recent_reactions: list[dict[str, Any]] = field(default_factory=list)
    topic_status: dict[str, Any] | None = None
    cross_topic_peek: list[dict[str, Any]] = field(default_factory=list)
    pregnancy_state: str | None = None


def _row_dict(row: Any) -> dict[str, Any]:
    return dict(row)


def _clean_list(value: Any) -> list[Any]:
    return list(value or [])


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None and hasattr(value, "isoformat") else None


def _temporal_context_solo(timezone_name: str | None, now_utc: datetime | None = None) -> dict[str, Any]:
    now = now_utc or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    now = now.astimezone(UTC)
    tz = timezone_or_utc(timezone_name)
    now_local = now.astimezone(tz)
    local_day_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    local_day_end = local_day_start + timedelta(days=1)
    one_month_from_now_local = add_calendar_months(now_local, 1)
    one_month_from_today_local_date = add_calendar_months(now_local.date(), 1)
    return {
        "now_utc": now.isoformat(),
        "now_local": now_local.isoformat(),
        "timezone": timezone_name or "UTC",
        "local_date": now_local.date().isoformat(),
        "local_time": now_local.strftime("%H:%M:%S"),
        "local_weekday": now_local.strftime("%A"),
        "local_day_start": local_day_start.isoformat(),
        "local_day_end": local_day_end.isoformat(),
        "local_day_start_utc": local_day_start.astimezone(UTC).isoformat(),
        "local_day_end_utc": local_day_end.astimezone(UTC).isoformat(),
        "one_month_from_now_local": one_month_from_now_local.isoformat(),
        "one_month_from_now_utc": one_month_from_now_local.astimezone(UTC).isoformat(),
        "one_month_from_today_local_date": one_month_from_today_local_date.isoformat(),
    }


def _time_context(value: datetime | None, timezone_name: str | None, now_utc: datetime) -> dict[str, str] | None:
    return temporal_reference(value, timezone_name, now=now_utc)


def _time_label(item: dict[str, Any], key: str) -> str | None:
    ref = item.get(f"{key}_time")
    if isinstance(ref, dict):
        exact = ref.get("utc")
        suffix = f"; utc={exact}" if exact else ""
        return f"{ref.get('display')} ({ref.get('relative_to_now')}{suffix})"
    return item.get(key)


def _duration_since(value: datetime | None) -> str | None:
    if value is None:
        return None
    now = datetime.now(UTC)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    seconds = max(0, int((now - value).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h"
    return f"{hours // 24}d"


def _clip(text: Any, limit: int = 240) -> str:
    value = "" if text is None else str(text)
    return value if len(value) <= limit else value[: limit - 3] + "..."


def _history_content(item: dict[str, Any]) -> str:
    content = item.get("content") or media_analysis_text(item)
    if item.get("direction") == "outbound":
        raw_content = str(content or "")
        cleaned = clean_user_facing_text(raw_content)
        content = cleaned if cleaned or looks_like_internal_process_text(raw_content) else content
    return "" if content is None else str(content)


def _media_label(item: dict[str, Any]) -> str:
    media_type = item.get("media_type")
    if not media_type:
        return ""
    duration = item.get("media_duration_seconds")
    duration_text = f", {duration}s" if duration is not None else ""
    if media_type == "voice":
        return f" [voice transcript{duration_text}]"
    if media_type == "image":
        return " [image analysis]"
    return f" [{media_type}{duration_text}]"


def _message_content(item: dict[str, Any], clip_limit: int) -> str:
    return f"{_media_label(item)}: {_history_content(item)}"


def _clip_id(value: Any, clip_limit: int) -> str:
    return _clip(value, 14 if clip_limit < 60 else clip_limit)


async def _user_profile_solo(pool: Any, user: User) -> dict[str, Any]:
    row = await pool.fetchrow(
        """\
        SELECT id, name, phone, timezone, COALESCE(style_notes, '') AS style_notes,
               COALESCE(onboarding_state, 'pending') AS onboarding_state,
               pregnancy_edd, pregnancy_dating_basis, pregnancy_lmp_date, pregnancy_scan_date,
               pregnancy_scan_corrected_at, pregnancy_started_at, pregnancy_ended_at, pregnancy_outcome
        FROM users
        WHERE id = $1
        """,
        user.id,
    )
    if row is None:
        return {
            "id": user.id,
            "name": user.name,
            "phone": user.phone,
            "timezone": user.timezone,
            "style_notes": "",
            "onboarding_state": "pending",
            "pregnancy_edd": None,
            "pregnancy_dating_basis": None,
            "pregnancy_lmp_date": None,
            "pregnancy_scan_date": None,
            "pregnancy_scan_corrected_at": None,
            "pregnancy_started_at": None,
            "pregnancy_ended_at": None,
            "pregnancy_outcome": None,
        }
    # §16.3 wi 7: surface the canonical user_identities address when present.
    from app.services.user_identity import resolve_user_address
    resolved = await resolve_user_address(pool, user.id)
    profile = _row_dict(row)
    if resolved is not None:
        profile["phone"] = resolved
    return profile


async def _fetch_topic_status_solo(
    pool: Any,
    *,
    topic_id: UUID,
    user_id: UUID,
) -> dict[str, Any] | None:
    """Fetch the topic_status row for (topic, user). No dyad_id for solo."""
    row = await pool.fetchrow(
        """\
        SELECT id, headline, body, last_updated_at
        FROM topic_status
        WHERE topic_id = $1 AND user_id = $2
        """,
        topic_id,
        user_id,
    )
    return dict(row) if row is not None else None


async def build_hot_context_solo(
    pool: Any,
    user: User,
    triggering_message_ids: list[UUID],
    trigger_metadata: dict[str, Any] | None = None,
    *,
    primary_topic_id: UUID,
    bot_id: str,
    allow_cross_topic_peek: bool = False,
) -> HotContextSolo:
    """Build hot context for a solo bot turn.

    Raises:
        ValueError: if primary_topic_id is None (no defensive fallback, lesson #6).
    """
    if primary_topic_id is None:
        raise ValueError("build_hot_context_solo: primary_topic_id must not be None")

    topic_status = await _fetch_topic_status_solo(
        pool, topic_id=primary_topic_id, user_id=user.id
    )
    current_user = await _user_profile_solo(pool, user)
    partner_user: dict[str, Any] = {}
    now_utc = datetime.now(UTC)
    user_timezone = timezone_or_utc(current_user.get("timezone") or user.timezone).key

    # Conversation load for this user only
    conversation_load_row = await pool.fetchrow(
        """\
        WITH bounds AS (
            SELECT
                date_trunc('day', now() AT TIME ZONE $2) AT TIME ZONE $2 AS period_start,
                (date_trunc('day', now() AT TIME ZONE $2) + interval '1 day') AT TIME ZONE $2 AS period_end
        )
        SELECT
            bounds.period_start,
            bounds.period_end,
            COUNT(*) FILTER (WHERE m.direction = 'inbound') AS inbound_count,
            COUNT(*) FILTER (WHERE m.direction = 'outbound') AS outbound_count,
            COUNT(m.id) AS total_count
        FROM bounds
        LEFT JOIN messages m
            ON m.deleted_at IS NULL
           AND (m.sender_id = $1 OR m.recipient_id = $1)
           AND m.sent_at >= bounds.period_start
           AND m.sent_at < bounds.period_end
        GROUP BY bounds.period_start, bounds.period_end
        """,
        user.id,
        user_timezone,
    )
    conversation_load = {
        "period": "today",
        "timezone": user_timezone,
        "period_start": _iso(conversation_load_row["period_start"]) if conversation_load_row else None,
        "period_end": _iso(conversation_load_row["period_end"]) if conversation_load_row else None,
        "inbound_count": int(conversation_load_row["inbound_count"] or 0) if conversation_load_row else 0,
        "outbound_count": int(conversation_load_row["outbound_count"] or 0) if conversation_load_row else 0,
        "total_count": int(conversation_load_row["total_count"] or 0) if conversation_load_row else 0,
    }

    # OOB: only for the user (no partner)
    active_oob = [
        {
            "id": row["id"],
            "owner_id": row["owner_id"],
            "severity": row["severity"],
            "shareable_context": row["shareable_context"],
            "protected_summary": row["shareable_context"] or "[protected]",
            "review_at": _iso(row["review_at"]),
            "review_at_time": _time_context(row["review_at"], user_timezone, now_utc),
        }
        for row in await pool.fetch(
            f"""\
            SELECT x.id, x.owner_id, x.shareable_context, x.severity, x.review_at
            FROM out_of_bounds x
            {join_artifact_topics('x', '$2')}
            WHERE x.status = 'active' AND x.owner_id = $1
            ORDER BY CASE x.severity WHEN 'hard' THEN 1 WHEN 'firm' THEN 2 ELSE 3 END, x.created_at DESC
            """,
            user.id,
            primary_topic_id,
        )
    ]

    # Memories: only about the user, scoped to primary topic
    memories = [
        {
            "id": row["id"],
            "about_user_id": row["about_user_id"],
            "content": row["content"],
            "related_theme_ids": _clean_list(row["related_theme_ids"]),
            "last_referenced_at": _iso(row["last_referenced_at"]),
            "created_at": _iso(row["created_at"]),
            "last_referenced_at_time": _time_context(row["last_referenced_at"], user_timezone, now_utc),
            "created_at_time": _time_context(row["created_at"], user_timezone, now_utc),
        }
        for row in await pool.fetch(
            f"""\
            SELECT m.id, m.about_user_id, m.content, COALESCE(m.related_theme_ids, '{{}}'::uuid[]) AS related_theme_ids,
                   m.last_referenced_at, m.created_at
            FROM memories m
            {join_artifact_topics('m', '$2')}
            WHERE m.status = 'active' AND m.about_user_id = $1
            ORDER BY COALESCE(m.last_referenced_at, m.created_at) DESC
            LIMIT 80
            """,
            user.id,
            primary_topic_id,
        )
    ]

    # Active themes: scoped to primary topic
    active_themes = [
        {
            "id": row["id"],
            "title": row["title"],
            "status": row["status"],
            "sentiment": row["sentiment"],
            "health": row["health"],
            "description": row["description"],
            "last_reinforced_at": _iso(row["last_reinforced_at"]),
            "last_active_at": _iso(row["last_active_at"]),
            "last_reinforced_at_time": _time_context(row["last_reinforced_at"], user_timezone, now_utc),
            "last_active_at_time": _time_context(row["last_active_at"], user_timezone, now_utc),
        }
        for row in await pool.fetch(
            f"""\
            SELECT t.id, t.title, t.description, t.status, t.sentiment, t.health, t.last_reinforced_at, t.last_active_at
            FROM themes t
            {join_artifact_topics('t', '$1')}
            WHERE t.status = 'active'
            ORDER BY COALESCE(t.last_reinforced_at, t.first_seen_at) DESC
            LIMIT 10
            """,
            primary_topic_id,
        )
    ]

    # Open watch items: owner = user, scoped to primary topic
    open_watch_items = [
        {
            "id": row["id"],
            "owner_user_id": row["owner_user_id"],
            "content": row["content"],
            "due_at": _iso(row["due_at"]),
            "due_at_time": _time_context(row["due_at"], user_timezone, now_utc),
            "related_theme_ids": _clean_list(row["related_theme_ids"]),
        }
        for row in await pool.fetch(
            f"""\
            SELECT w.id, w.owner_user_id, w.content, w.due_at, COALESCE(w.related_theme_ids, '{{}}'::uuid[]) AS related_theme_ids
            FROM watch_items w
            {join_artifact_topics('w', '$2')}
            WHERE w.status = 'open' AND w.owner_user_id = $1
            ORDER BY COALESCE(w.due_at, w.created_at) ASC
            """,
            user.id,
            primary_topic_id,
        )
    ]

    # Observations: scoped to primary topic
    observations = [
        {
            "id": row["id"],
            "about_user_id": row["about_user_id"],
            "content": row["content"],
            "confidence": row["confidence"],
            "significance": row["significance"],
            "related_theme_ids": _clean_list(row["related_theme_ids"]),
            "last_reinforced_at": _iso(row["last_reinforced_at"]),
            "created_at": _iso(row["created_at"]),
            "last_reinforced_at_time": _time_context(row["last_reinforced_at"], user_timezone, now_utc),
            "created_at_time": _time_context(row["created_at"], user_timezone, now_utc),
        }
        for row in await pool.fetch(
            f"""\
            SELECT o.id, o.about_user_id, o.content, o.confidence, o.significance,
                   COALESCE(o.related_theme_ids, '{{}}'::uuid[]) AS related_theme_ids,
                   o.last_reinforced_at, o.created_at
            FROM observations o
            {join_artifact_topics('o', '$1')}
            WHERE o.status = 'active' AND o.significance >= 3
            ORDER BY recency_weighted_score(o.significance, o.last_reinforced_at, o.created_at) DESC NULLS LAST,
                     COALESCE(o.last_reinforced_at, o.created_at) DESC
            LIMIT 80
            """,
            primary_topic_id,
        )
    ]

    # Messages: sender or recipient is the user, scoped to this bot+topic
    message_rows = await pool.fetch(
        """\
        SELECT id, direction, sender_id, recipient_id, content, media_type, media_duration_seconds,
               media_analysis, sent_at, COALESCE(charge, 'routine') AS charge
        FROM messages
        WHERE deleted_at IS NULL
          AND (sender_id = $1 OR recipient_id = $1)
          AND bot_id = $2
          AND topic_id = $3
        ORDER BY sent_at DESC
        LIMIT 20
        """,
        user.id,
        bot_id,
        primary_topic_id,
    )

    # Distillations: scoped to primary topic, source includes user
    distillation_rows = await pool.fetch(
        f"""\
        SELECT d.id, d.content, d.confidence, d.status, d.sensitivity, d.visibility, d.shareable_summary,
               COALESCE(d.source_user_ids, '{{}}'::uuid[]) AS source_user_ids,
               COALESCE(d.related_memory_ids, '{{}}'::uuid[]) AS related_memory_ids,
               COALESCE(d.related_observation_ids, '{{}}'::uuid[]) AS related_observation_ids,
               COALESCE(d.related_theme_ids, '{{}}'::uuid[]) AS related_theme_ids,
               COALESCE(d.supporting_message_ids, '{{}}'::uuid[]) AS supporting_message_ids,
               d.revision_note, d.revision_count, d.updated_at, d.created_at
        FROM distillations d
        {join_artifact_topics('d', '$2')}
        WHERE d.status = 'active'
          AND d.source_user_ids && $1::uuid[]
        ORDER BY d.updated_at DESC, d.created_at DESC
        LIMIT 12
        """,
        [user.id],
        primary_topic_id,
    )
    distillations: list[dict[str, Any]] = []
    for row in distillation_rows:
        source_user_ids = _clean_list(row["source_user_ids"])
        # For solo, all source material is from this user — always visible
        if row["visibility"] == "dyad_shareable" and row["shareable_summary"]:
            content = row["shareable_summary"]
            display = "shareable_summary"
        else:
            content = row["content"]
            display = "full_content"
        distillations.append(
            {
                "id": row["id"],
                "content": content,
                "display": display,
                "source_user_ids": source_user_ids,
                "confidence": row["confidence"],
                "sensitivity": row["sensitivity"],
                "visibility": row["visibility"],
                "revision_count": row["revision_count"],
                "related_memory_ids": _clean_list(row["related_memory_ids"]),
                "related_observation_ids": _clean_list(row["related_observation_ids"]),
                "related_theme_ids": _clean_list(row["related_theme_ids"]),
                "supporting_message_ids": _clean_list(row["supporting_message_ids"]),
                "updated_at": _iso(row["updated_at"]),
                "updated_at_time": _time_context(row["updated_at"], user_timezone, now_utc),
            }
        )

    # Recent messages: only this user's messages, no sharing-default filtering needed
    recent_messages = [
        {
            "id": row["id"],
            "direction": row["direction"],
            "sender_id": row["sender_id"],
            "recipient_id": row["recipient_id"],
            "content": row["content"],
            "media_type": row["media_type"] if "media_type" in row else None,
            "media_duration_seconds": row["media_duration_seconds"] if "media_duration_seconds" in row else None,
            "media_analysis": row["media_analysis"] if "media_analysis" in row else None,
            "raw_content_hidden": False,
            "sent_at": _iso(row["sent_at"]),
            "sent_at_time": _time_context(row["sent_at"], user_timezone, now_utc),
            "charge": row["charge"],
        }
        for row in reversed(message_rows)
    ]

    latest_sent_at = max((row["sent_at"] for row in message_rows), default=None)

    # Trigger messages
    trigger_rows = await pool.fetch(
        """\
        SELECT id, direction, sender_id, recipient_id, COALESCE(charge, 'routine') AS charge,
               sent_at, content, media_type, media_duration_seconds, media_analysis
        FROM messages
        WHERE id = ANY($1::uuid[])
        ORDER BY sent_at ASC
        """,
        triggering_message_ids,
    )

    # Recent reactions (feedback from user about bot's messages)
    recent_reactions = [
        {
            "id": row["id"],
            "sentiment": row["sentiment"],
            "content": row["content"],
            "created_at": _iso(row["created_at"]),
            "created_at_time": _time_context(row["created_at"], user_timezone, now_utc),
            "message_id": row["message_id"],
            "message_content": row["message_content"],
            "message_sent_at": _iso(row["message_sent_at"]),
            "message_sent_at_time": _time_context(row["message_sent_at"], user_timezone, now_utc),
        }
        for row in reversed(
            await pool.fetch(
                """\
                WITH previous_turn AS (
                    SELECT completed_at
                    FROM bot_turns
                    WHERE user_in_context = $1
                      AND completed_at IS NOT NULL
                    ORDER BY completed_at DESC
                    LIMIT 1
                )
                SELECT f.id, f.sentiment, f.content, f.created_at,
                       m.id AS message_id, m.content AS message_content, m.sent_at AS message_sent_at
                FROM feedback f
                JOIN messages m ON m.id = f.target_id
                WHERE EXISTS (SELECT 1 FROM previous_turn)
                  AND f.from_user_id = $1
                  AND f.target_type = 'message'
                  AND f.source = 'reaction'
                  AND m.direction = 'outbound'
                  AND m.recipient_id = $1
                  AND f.created_at > (SELECT completed_at FROM previous_turn)
                  AND f.created_at <= $2
                ORDER BY f.created_at DESC
                LIMIT 5
                """,
                user.id,
                now_utc,
            )
        )
    ]

    cross_topic_peek: list[dict[str, Any]] = []
    if allow_cross_topic_peek:
        peek_since = now_utc - timedelta(days=14)
        cross_topic_peek = await peek_other_topics(
            pool,
            dyad_id=None,
            user_id=user.id,
            exclude_topic_id=primary_topic_id,
            since=peek_since,
        )

    # ── Pregnancy state (Tante Rosi only) ──────────────────────────────
    pregnancy_state: str | None = None
    if bot_id == "tante_rosi":
        from app.services.pregnancy import format_pregnancy_state

        pregnancy_state = format_pregnancy_state(user)

    return HotContextSolo(
        current_user=current_user,
        partner_user=partner_user,
        temporal_context=_temporal_context_solo(user_timezone, now_utc),
        conversation_load=conversation_load,
        active_oob=active_oob,
        memories=memories,
        active_themes=active_themes,
        open_watch_items=open_watch_items,
        observations=observations,
        distillations=distillations,
        bridge_candidates=[],  # no bridge candidates for solo
        recent_reactions=recent_reactions,
        recent_messages=recent_messages,
        topic_status=topic_status,
        cross_topic_peek=cross_topic_peek,
        pregnancy_state=pregnancy_state,
        time_since_last_message=_duration_since(latest_sent_at),
        trigger_metadata={
            **(trigger_metadata or {}),
            "triggering_message_ids": triggering_message_ids,
            "messages": [
                {
                    "id": row["id"],
                    "charge": row["charge"],
                    "sent_at": _iso(row["sent_at"]),
                    "sent_at_time": _time_context(row["sent_at"], user_timezone, now_utc),
                    "content": row["content"] if "content" in row else None,
                    "media_type": row["media_type"] if "media_type" in row else None,
                    "media_duration_seconds": row["media_duration_seconds"] if "media_duration_seconds" in row else None,
                    "media_analysis": row["media_analysis"] if "media_analysis" in row else None,
                }
                for row in trigger_rows
            ],
        },
    )


def _line(prefix: str, value: Any) -> str:
    return f"- {prefix}: {_clip(value)}"


def _render_solo_with_counts(hc: HotContextSolo, truncations: dict[str, int], clip_limit: int = 240) -> str:
    lines: list[str] = []
    lines += [
        "## You",
        f"- id: {_clip(hc.current_user['id'], clip_limit)}",
        f"- name: {_clip(hc.current_user['name'], clip_limit)}",
        f"- timezone: {_clip(hc.current_user['timezone'], clip_limit)}",
        f"- onboarding_state: {_clip(hc.current_user.get('onboarding_state', 'pending'), clip_limit)}",
        f"- style_notes: {_clip(hc.current_user.get('style_notes', ''), clip_limit)}",
    ]
    if hc.temporal_context:
        lines += [
            "",
            "## Current time",
            f"- now_utc: {_clip(hc.temporal_context.get('now_utc'), clip_limit)}",
            f"- now_local: {_clip(hc.temporal_context.get('now_local'), clip_limit)}",
            f"- timezone: {_clip(hc.temporal_context.get('timezone'), clip_limit)}",
            f"- local_date: {_clip(hc.temporal_context.get('local_date'), clip_limit)}",
            f"- local_time: {_clip(hc.temporal_context.get('local_time'), clip_limit)}",
            f"- local_weekday: {_clip(hc.temporal_context.get('local_weekday'), clip_limit)}",
            f"- local_day_bounds: {_clip(hc.temporal_context.get('local_day_start'), clip_limit)} to {_clip(hc.temporal_context.get('local_day_end'), clip_limit)} (UTC {_clip(hc.temporal_context.get('local_day_start_utc'), clip_limit)} to {_clip(hc.temporal_context.get('local_day_end_utc'), clip_limit)})",
            f"- one_month_from_now: local={_clip(hc.temporal_context.get('one_month_from_now_local'), clip_limit)} utc={_clip(hc.temporal_context.get('one_month_from_now_utc'), clip_limit)} local_date={_clip(hc.temporal_context.get('one_month_from_today_local_date'), clip_limit)}",
            "- scheduling_note: Default to scheduling tool delay fields for simple duration phrases like 'in two hours', 'in 10 hours', or 'in two days'. Use local_when for concrete local clock phrases like '9pm tonight' or 'Monday at 8'. Use absolute when only for exact timezone-aware instants. For phrases like 'for the next month', use the one_month_from_now/local_date anchors rather than guessing.",
        ]
    if not truncations.get("conversation_load"):
        lines += [
            "",
            "## Conversation load",
            f"- period: {_clip(hc.conversation_load.get('period', 'today'), clip_limit)}",
            f"- timezone: {_clip(hc.conversation_load.get('timezone'), clip_limit)}",
            f"- local_period_bounds: {_clip(hc.temporal_context.get('local_day_start') if hc.temporal_context else None, clip_limit)} to {_clip(hc.temporal_context.get('local_day_end') if hc.temporal_context else None, clip_limit)}",
            f"- utc_period_bounds: {_clip(hc.conversation_load.get('period_start'), clip_limit)} to {_clip(hc.conversation_load.get('period_end'), clip_limit)}",
            f"- total_messages: {_clip(hc.conversation_load.get('total_count', 0), clip_limit)}",
            f"- inbound_messages: {_clip(hc.conversation_load.get('inbound_count', 0), clip_limit)}",
            f"- outbound_messages: {_clip(hc.conversation_load.get('outbound_count', 0), clip_limit)}",
        ]
    if hc.topic_status:
        ts_updated = hc.topic_status.get("last_updated_at")
        ts_iso = ts_updated.isoformat() if hasattr(ts_updated, "isoformat") else ts_updated
        lines += [
            "",
            "## Topic status",
            f"- headline: {_clip(hc.topic_status.get('headline'), clip_limit)}",
        ]
        body_text = hc.topic_status.get("body") or ""
        if body_text:
            lines.append(f"- body: {_clip(body_text, clip_limit)}")
        lines.append(f"- last_updated_at: {_clip(ts_iso, clip_limit)}")

    # ── Pregnancy state (Tante Rosi only) ──────────────────────────
    if hc.pregnancy_state is not None:
        lines += ["", "## Pregnancy", hc.pregnancy_state]

    # Cross-topic peek for solo
    if hc.cross_topic_peek:
        lines += ["", "## Cross-topic activity (peek)"]
        for item in hc.cross_topic_peek:
            last_active = item.get("last_active_at")
            last_iso = last_active.isoformat() if hasattr(last_active, "isoformat") else last_active
            lines.append(
                f"- {_clip(item.get('slug'), clip_limit)} ({_clip(item.get('display_name'), clip_limit)}): last_active={_clip(last_iso, clip_limit)}"
            )
    else:
        lines += [
            "",
            "## Peek (other topics)",
            "- (none — solo bot, single topic)",
        ]

    lines += [
        "",
        "## Active OOB (severity)",
    ]
    if hc.active_oob:
        for item in hc.active_oob:
            lines.append(
                f"- id={_clip_id(item['id'], clip_limit)} {item['severity']} owner={_clip_id(item['owner_id'], clip_limit)} review={_clip(_time_label(item, 'review_at') or 'none', clip_limit)} context={_clip(item.get('protected_summary') or item.get('shareable_context') or '[protected]', clip_limit)}"
            )
    else:
        lines.append("- none")
    lines += ["", "## Active themes"]
    lines.extend(
        f"- id={_clip_id(theme['id'], clip_limit)} last={_clip(_time_label(theme, 'last_reinforced_at') or _time_label(theme, 'last_active_at') or 'unknown', clip_limit)} {_clip(theme['title'], clip_limit)} ({theme['status']}, {theme['sentiment']}, {theme['health']}): {_clip(theme['description'], clip_limit)}"
        for theme in hc.active_themes
    )
    lines += ["", "## Memories"]
    lines.extend(
        f"- id={_clip_id(item['id'], clip_limit)} time={_clip(_time_label(item, 'last_referenced_at') or _time_label(item, 'created_at') or 'unknown', clip_limit)} about={_clip_id(item['about_user_id'], clip_limit)}: {_clip(item['content'], clip_limit)}"
        for item in hc.memories
    )
    if truncations.get("memories"):
        lines.append(f"- [truncated, {truncations['memories']} more]")
    lines += ["", "## Open watch items"]
    lines.extend(
        f"- id={_clip_id(item['id'], clip_limit)} due={_clip(_time_label(item, 'due_at') or 'none', clip_limit)} {_clip(item['content'], clip_limit)}"
        for item in hc.open_watch_items
    )
    lines += ["", "## High-significance observations"]
    lines.extend(
        f"- id={_clip_id(item['id'], clip_limit)} time={_clip(_time_label(item, 'last_reinforced_at') or _time_label(item, 'created_at') or 'unknown', clip_limit)} sig={item['significance']} confidence={item['confidence']} about={_clip_id(item['about_user_id'], clip_limit)}: {_clip(item['content'], clip_limit)}"
        for item in hc.observations
    )
    if truncations.get("observations"):
        lines.append(f"- [truncated, {truncations['observations']} more]")
    lines += ["", "## Distillations"]
    if hc.distillations:
        lines.extend(
            f"- id={_clip_id(item['id'], clip_limit)} time={_clip(_time_label(item, 'updated_at') or 'unknown', clip_limit)} display={item['display']} confidence={item['confidence']} sensitivity={item['sensitivity']} visibility={item['visibility']} sources={_clip(', '.join(str(source) for source in item['source_user_ids']), clip_limit)}: {_clip(item['content'], clip_limit)}"
            for item in hc.distillations
        )
        lines.append("- use get_distillations before adding or revising synthesized explanations.")
    else:
        lines.append("- none")
    if truncations.get("distillations"):
        lines.append(f"- [truncated, {truncations['distillations']} more]")
    lines += ["", "## Recent messages"]
    lines.extend(
        f"- {_time_label(item, 'sent_at') or item['sent_at']} {item['direction']} charge={item['charge']} sender={item['sender_id']} recipient={item['recipient_id']}{_message_content(item, clip_limit)}"
        for item in hc.recent_messages
    )
    if truncations.get("recent_messages"):
        lines.append(f"- [truncated, {truncations['recent_messages']} more]")
    lines += ["", "## New reactions since previous turn"]
    if hc.recent_reactions:
        lines.extend(
            f"- {_time_label(item, 'created_at') or item['created_at']} sentiment={item['sentiment']} reaction={_clip(item['content'], clip_limit)} on_message={_clip_id(item['message_id'], clip_limit)} sent={_clip(_time_label(item, 'message_sent_at') or item['message_sent_at'], clip_limit)}: {_clip(clean_user_facing_text(str(item.get('message_content') or '')) or item.get('message_content') or '[no text]', clip_limit)}"
            for item in hc.recent_reactions
        )
        lines.append("- Treat these as passive feedback only; do not mention them unless naturally relevant to the user's new message.")
    else:
        lines.append("- none")
    lines += [
        "",
        "## Trigger",
        f"- kind: {_clip(hc.trigger_metadata.get('kind', 'inbound'), clip_limit)}",
        f"- triggering_message_ids: {_clip(', '.join(str(mid) for mid in hc.trigger_metadata['triggering_message_ids']), clip_limit)}",
        f"- time_since_last_message: {_clip(hc.time_since_last_message, clip_limit)}",
    ]
    if hc.trigger_metadata.get("context") is not None:
        lines.append(f"- context: {_clip(hc.trigger_metadata['context'], clip_limit)}")
    lines.extend(
        f"- trigger_message id={msg['id']} charge={msg['charge']} sent_at={_time_label(msg, 'sent_at') or msg['sent_at']}{_message_content(msg, clip_limit)}"
        for msg in hc.trigger_metadata["messages"]
    )
    return "\n".join(lines).strip()


def _estimated_tokens(text: str) -> int:
    return len(text) // 4


def render_hot_context_solo(hc: HotContextSolo) -> str:
    """Render a solo HotContextSolo as a string, respecting the token budget.

    Mirrors render_hot_context but skips all partner/bridge/sharing-default
    sections.
    """
    budget = get_settings().hot_context_token_budget
    working = HotContextSolo(
        current_user=hc.current_user,
        partner_user=hc.partner_user,
        temporal_context=hc.temporal_context,
        conversation_load=hc.conversation_load,
        active_oob=hc.active_oob,
        memories=list(hc.memories),
        active_themes=hc.active_themes,
        open_watch_items=hc.open_watch_items,
        observations=list(hc.observations),
        distillations=list(hc.distillations),
        bridge_candidates=[],
        recent_reactions=list(hc.recent_reactions),
        recent_messages=list(hc.recent_messages),
        topic_status=hc.topic_status,
        cross_topic_peek=list(hc.cross_topic_peek),
        pregnancy_state=hc.pregnancy_state,
        time_since_last_message=hc.time_since_last_message,
        trigger_metadata=hc.trigger_metadata,
    )
    truncations = {"distillations": 0, "observations": 0, "memories": 0, "recent_messages": 0, "conversation_load": 0}
    clip_limit = 240
    text = _render_solo_with_counts(working, truncations, clip_limit)
    for name in ("distillations", "observations", "memories", "recent_messages"):
        items = getattr(working, name)
        while _estimated_tokens(text) > budget and items:
            items.pop()
            truncations[name] += 1
            text = _render_solo_with_counts(working, truncations, clip_limit)
    for clip_limit in (160, 100, 60, 30):
        if _estimated_tokens(text) <= budget:
            break
        text = _render_solo_with_counts(working, truncations, clip_limit)
    for name in ("open_watch_items", "active_themes"):
        items = getattr(working, name)
        while _estimated_tokens(text) > budget and items:
            items.pop()
            text = _render_solo_with_counts(working, truncations, clip_limit)
    if _estimated_tokens(text) > budget and not truncations["conversation_load"]:
        truncations["conversation_load"] = 1
        text = _render_solo_with_counts(working, truncations, clip_limit)
    return text