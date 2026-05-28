"""Solo hot context construction (Sprint 5).

Mirrors hot_context.py but for a single-user bot: single about-user bucket,
no partner content. When a dyad partner resolves, outgoing mediated bridge
visibility (source_user_id = this user) is included read-only; solo bots
have no bridge tools.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from app.config import get_settings
from app.models.user import User
from app.services.text_safety import (
    clean_user_facing_text,
    looks_like_internal_process_text,
)
from app.services.time_context import (
    add_calendar_months,
    temporal_reference,
    timezone_or_utc,
)
from app.services.tools.common import media_analysis_text
from app.services.hot_context import peek_other_topics
from app.services.open_asks import _get_bot_asks, render_open_asks
from app.services.partner_sharing import (
    get_partner_share,
    has_dyad_partner,
    resolve_dyad_partner,
)
from app.services.cross_thread_privacy import bridge_candidate_visible_to_target
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
    outgoing_mediated_issues: list[dict[str, Any]] = field(default_factory=list)
    resolved_bridge_context: dict | None = None
    recent_reactions: list[dict[str, Any]] = field(default_factory=list)
    silent_turns: list[dict[str, Any]] = field(default_factory=list)
    topic_status: dict[str, Any] | None = None
    cross_topic_peek: list[dict[str, Any]] = field(default_factory=list)
    pregnancy_state: str | None = None
    partner_pregnancy_state: str | None = None
    fitness_block: str | None = None
    upcoming_items: list[dict[str, Any]] = field(default_factory=list)
    bot_id: str = "coach"


def _partner_sharing_state(partner_share: str | None, *, has_partner: bool) -> str:
    if not has_partner:
        return "unavailable"
    if partner_share is None:
        return "pending"
    return partner_share


def _row_dict(row: Any) -> dict[str, Any]:
    return dict(row)


def _clean_list(value: Any) -> list[Any]:
    return list(value or [])


def _iso(value: Any) -> str | None:
    return (
        value.isoformat() if value is not None and hasattr(value, "isoformat") else None
    )


def _temporal_context_solo(
    timezone_name: str | None, now_utc: datetime | None = None
) -> dict[str, Any]:
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


def _time_context(
    value: datetime | None, timezone_name: str | None, now_utc: datetime
) -> dict[str, str] | None:
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
        content = (
            cleaned
            if cleaned or looks_like_internal_process_text(raw_content)
            else content
        )
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


def _queue_outcome_label(item: dict[str, Any]) -> str:
    """Return a short label explaining queue outcome for non-replied inbound messages."""
    qo = item.get("queue_outcome")
    if not qo:
        return ""
    result = qo.get("handling_result", "")
    if result == "silent":
        return " [bot intentionally silent]"
    if result == "withheld_newer_inbound":
        return " [stale response withheld, newer message arrived]"
    if result == "failed":
        err = qo.get("processing_error") or ""
        err_short = err[:80] + "..." if len(err) > 80 else err
        return f" [processing failed: {err_short}]" if err_short else " [processing failed]"
    if result == "expired":
        return " [expired, not processed]"
    if result == "no_action":
        return " [bot chose no action]"
    return f" [outcome: {result}]"


def _message_content(item: dict[str, Any], clip_limit: int) -> str:
    return f"{_media_label(item)}: {_history_content(item)}"


def _clip_id(value: Any, clip_limit: int) -> str:
    return _clip(value, 14 if clip_limit < 60 else clip_limit)


def _format_partner_pregnancy_state(
    partner_user: dict[str, Any],
    *,
    partner_name: str | None,
    today: Any,
) -> str | None:
    from app.services.pregnancy import format_pregnancy_state

    pregnancy_edd = partner_user.get("pregnancy_edd")
    if pregnancy_edd is None:
        return None
    user = User(
        id=partner_user.get("id"),
        name=partner_name or partner_user.get("name") or "Partner",
        phone="",
        timezone=partner_user.get("timezone") or "UTC",
        pregnancy_edd=pregnancy_edd,
        pregnancy_dating_basis=partner_user.get("pregnancy_dating_basis"),
        pregnancy_lmp_date=partner_user.get("pregnancy_lmp_date"),
        pregnancy_scan_date=partner_user.get("pregnancy_scan_date"),
        pregnancy_scan_corrected_at=partner_user.get("pregnancy_scan_corrected_at"),
        pregnancy_started_at=partner_user.get("pregnancy_started_at"),
        pregnancy_ended_at=partner_user.get("pregnancy_ended_at"),
        pregnancy_outcome=partner_user.get("pregnancy_outcome"),
    )
    state = format_pregnancy_state(user, today=today)
    if state is None:
        return None
    label = partner_name or partner_user.get("name") or "partner"
    return f"- subject: {label}\n{state}"


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


async def _fetch_upcoming_items(
    pool: Any,
    *,
    user_id: UUID,
    bot_id: str,
    topic_id: UUID,
    now_utc: datetime,
    tz_name: str | None,
    max_total: int = 5,
) -> list[dict[str, Any]]:
    """Return pending scheduled jobs for (user, bot, topic), trimmed.

    Selection rule: include every pending job whose scheduled_for falls on
    the user's local "today" (so the bot sees the full set of items due
    today), then pad with the earliest future jobs until total == max_total.
    """
    rows = await pool.fetch(
        """\
        SELECT id, job_type, scheduled_for, context, topic_id
        FROM scheduled_jobs
        WHERE user_id = $1
          AND bot_id = $2
          AND topic_id = $3
          AND status = 'pending'
          AND scheduled_for >= $4
        ORDER BY scheduled_for ASC
        LIMIT 50
        """,
        user_id,
        bot_id,
        topic_id,
        now_utc,
    )
    if not rows:
        return []

    tz = timezone_or_utc(tz_name)
    today_local = now_utc.astimezone(tz).date()

    today_items: list[dict[str, Any]] = []
    later_items: list[dict[str, Any]] = []
    for row in rows:
        sched_utc = row["scheduled_for"]
        if sched_utc.tzinfo is None:
            sched_utc = sched_utc.replace(tzinfo=UTC)
        ref = temporal_reference(sched_utc, tz_name, now=now_utc) or {}
        context = row.get("context") or {}
        if isinstance(context, str):
            try:
                import json as _json
                context = _json.loads(context)
            except Exception:
                context = {}
        brief = (
            (context.get("brief") if isinstance(context, dict) else None)
            or (context.get("about_what") if isinstance(context, dict) else None)
            or (context.get("reason") if isinstance(context, dict) else None)
            or (context.get("kind") if isinstance(context, dict) else None)
        )
        item = {
            "id": str(row["id"]),
            "job_type": row["job_type"],
            "scheduled_for_utc": sched_utc.isoformat(),
            "local_day_label": ref.get("local_day_label"),
            "local_time": ref.get("local_time"),
            "relative_to_now": ref.get("relative_to_now"),
            "brief": brief,
        }
        if sched_utc.astimezone(tz).date() == today_local:
            today_items.append(item)
        else:
            later_items.append(item)

    pad_budget = max(0, max_total - len(today_items))
    return today_items + later_items[:pad_budget]


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
    # Resolve dyad partner identity ONCE for both partner_sharing_state and
    # the `## Your Partner` block. Identity-only — name/id/timezone plus
    # the partner's per-bot partner_share for THIS bot. NO content (no
    # memories, themes, observations, distillations, messages, pregnancy
    # facts). Invariant 1.
    partner_share = await get_partner_share(pool, user_id=user.id, bot_id=bot_id)
    dyad_partner = await resolve_dyad_partner(pool, user.id)
    current_user["partner_share"] = partner_share
    current_user["partner_sharing_state"] = _partner_sharing_state(
        partner_share,
        has_partner=dyad_partner is not None,
    )
    partner_user: dict[str, Any] = {}
    if dyad_partner is not None:
        partner_row = await pool.fetchrow(
            """\
            SELECT id, name, phone, timezone, onboarding_state, pacing_preferences,
                   pregnancy_edd, pregnancy_dating_basis, pregnancy_lmp_date, pregnancy_scan_date,
                   pregnancy_scan_corrected_at, pregnancy_started_at, pregnancy_ended_at, pregnancy_outcome
            FROM users
            WHERE id = $1
            """,
            dyad_partner.partner_user_id,
        )
        partner_recipient_share = await get_partner_share(
            pool, user_id=dyad_partner.partner_user_id, bot_id=bot_id
        )
        if partner_row is not None:
            partner_user = {
                "id": partner_row["id"],
                "name": partner_row["name"],
                "timezone": partner_row.get("timezone")
                if isinstance(partner_row, dict)
                else partner_row["timezone"],
                "pregnancy_edd": partner_row.get("pregnancy_edd"),
                "pregnancy_dating_basis": partner_row.get("pregnancy_dating_basis"),
                "pregnancy_lmp_date": partner_row.get("pregnancy_lmp_date"),
                "pregnancy_scan_date": partner_row.get("pregnancy_scan_date"),
                "pregnancy_scan_corrected_at": partner_row.get(
                    "pregnancy_scan_corrected_at"
                ),
                "pregnancy_started_at": partner_row.get("pregnancy_started_at"),
                "pregnancy_ended_at": partner_row.get("pregnancy_ended_at"),
                "pregnancy_outcome": partner_row.get("pregnancy_outcome"),
                # Recipient-side per-bot sharing state, normalized to one of
                # {opt_in, opt_out, pending}. Used by the partner-nudge tool
                # to decide whether scheduling is allowed.
                "partner_sharing_state_recipient_side": (
                    partner_recipient_share or "pending"
                ),
            }
        else:
            partner_user = {
                "id": dyad_partner.partner_user_id,
                "name": None,
                "timezone": None,
                "partner_sharing_state_recipient_side": (
                    partner_recipient_share or "pending"
                ),
            }
    # Outgoing mediated bridge visibility: source-side unresolved bridges.
    # Read-only; solo bots have no bridge tools. Skip when no dyad partner.
    outgoing_mediated_issues: list[dict[str, Any]] = []
    if dyad_partner is not None:
        outgoing_rows = await pool.fetch(
            """\
            SELECT id, kind, status, partner_path, shareable_summary, created_at
            FROM bridge_candidates
            WHERE source_user_id=$1 AND target_user_id=$2
              AND status IN ('pending','ready','blocked')
            ORDER BY created_at DESC, id DESC
            LIMIT 3
            """,
            user.id,
            dyad_partner.partner_user_id,
        )
        outgoing_mediated_issues = [dict(row) for row in outgoing_rows]

    # Resolve bridge context for partner_nudge triggers at fire time.
    # Same gate/fetch/visibility pattern as dyadic hot_context.py.
    resolved_bridge_context: dict | None = None
    _trigger_ctx = (trigger_metadata or {}).get("context")
    if (
        trigger_metadata is not None
        and isinstance(_trigger_ctx, dict)
        and _trigger_ctx.get("kind") == "partner_nudge"
        and _trigger_ctx.get("bridge_candidate_id") is not None
    ):
        _bc_row = await pool.fetchrow(
            """
            SELECT status, partner_path, shareable_summary, target_user_id
            FROM bridge_candidates
            WHERE id=$1
            """,
            _trigger_ctx["bridge_candidate_id"],
        )
        if _bc_row is not None and bridge_candidate_visible_to_target(
            _bc_row, target_user_id=user.id
        ):
            resolved_bridge_context = {
                "shareable_summary": _bc_row["shareable_summary"],
                "status": _bc_row["status"],
            }
        else:
            resolved_bridge_context = {"stale": True}

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
        "period_start": (
            _iso(conversation_load_row["period_start"])
            if conversation_load_row
            else None
        ),
        "period_end": (
            _iso(conversation_load_row["period_end"]) if conversation_load_row else None
        ),
        "inbound_count": (
            int(conversation_load_row["inbound_count"] or 0)
            if conversation_load_row
            else 0
        ),
        "outbound_count": (
            int(conversation_load_row["outbound_count"] or 0)
            if conversation_load_row
            else 0
        ),
        "total_count": (
            int(conversation_load_row["total_count"] or 0)
            if conversation_load_row
            else 0
        ),
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
            "last_referenced_at_time": _time_context(
                row["last_referenced_at"], user_timezone, now_utc
            ),
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
            "last_reinforced_at_time": _time_context(
                row["last_reinforced_at"], user_timezone, now_utc
            ),
            "last_active_at_time": _time_context(
                row["last_active_at"], user_timezone, now_utc
            ),
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
            "last_reinforced_at_time": _time_context(
                row["last_reinforced_at"], user_timezone, now_utc
            ),
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
               media_analysis, sent_at, COALESCE(charge, 'routine') AS charge,
               processing_state, handling_result, handled_at, processing_error
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
                "updated_at_time": _time_context(
                    row["updated_at"], user_timezone, now_utc
                ),
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
            "media_duration_seconds": (
                row["media_duration_seconds"]
                if "media_duration_seconds" in row
                else None
            ),
            "media_analysis": (
                row["media_analysis"] if "media_analysis" in row else None
            ),
            "raw_content_hidden": False,
            "sent_at": _iso(row["sent_at"]),
            "sent_at_time": _time_context(row["sent_at"], user_timezone, now_utc),
            "charge": row["charge"],
            **(
                {
                    "queue_outcome": {
                        "handling_result": row["handling_result"],
                        "handled_at": _iso(row["handled_at"]),
                        "processing_error": row.get("processing_error"),
                    }
                }
                if row.get("direction") == "inbound"
                and row.get("handling_result") is not None
                and row["handling_result"]
                not in (None, "replied")
                else {}
            ),
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
            "message_sent_at_time": _time_context(
                row["message_sent_at"], user_timezone, now_utc
            ),
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

    # Silent turns since the user's last inbound message — turns where the
    # agent did something (e.g. fired a scheduled task) but sent no outbound
    # message, so the message timeline has no record. Without this section
    # the agent has no way to know it already ran a check this morning.
    silent_turns_rows = await pool.fetch(
        """\
        WITH last_inbound AS (
            SELECT MAX(sent_at) AS sent_at
            FROM messages
            WHERE direction = 'inbound'
              AND sender_id = $1
        ),
        floor_ts AS (
            SELECT COALESCE(
                (SELECT sent_at FROM last_inbound),
                $2::timestamptz - INTERVAL '24 hours'
            ) AS ts
        )
        SELECT bt.id AS turn_id,
               bt.started_at,
               bt.completed_at,
               bt.bot_id,
               bt.reasoning,
               COALESCE(
                 jsonb_agg(
                   jsonb_build_object(
                     'id', tc.id,
                     'tool_name', tc.tool_name,
                     'kind', tc.kind,
                     'summary', tc.summary,
                     'called_at', tc.called_at
                   )
                   ORDER BY tc.called_at
                 ) FILTER (WHERE tc.id IS NOT NULL),
                 '[]'::jsonb
               ) AS tool_calls
        FROM bot_turns bt
        LEFT JOIN tool_calls tc ON tc.turn_id = bt.id
        WHERE bt.user_in_context = $1
          AND bt.completed_at IS NOT NULL
          AND bt.completed_at > (SELECT ts FROM floor_ts)
          AND bt.completed_at <= $2
          AND bt.final_output_message_id IS NULL
          AND bt.failure_reason IS NULL
        GROUP BY bt.id
        ORDER BY bt.completed_at DESC
        LIMIT 5
        """,
        user.id,
        now_utc,
    )
    silent_turns = [
        {
            "turn_id": str(row["turn_id"]),
            "started_at": _iso(row["started_at"]),
            "started_at_time": _time_context(
                row["started_at"], user_timezone, now_utc
            ),
            "completed_at": _iso(row["completed_at"]),
            "bot_id": row["bot_id"],
            "reasoning": row["reasoning"],
            "tool_calls": list(row["tool_calls"] or []),
        }
        for row in silent_turns_rows
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
    partner_pregnancy_state: str | None = None
    if bot_id == "tante_rosi":
        from app.services.pregnancy import format_pregnancy_state

        pregnancy_today = now_utc.astimezone(timezone_or_utc(user_timezone)).date()
        pregnancy_state = format_pregnancy_state(user, today=pregnancy_today)
        if pregnancy_state is None and partner_user:
            partner_pregnancy_state = _format_partner_pregnancy_state(
                partner_user,
                partner_name=partner_user.get("name"),
                today=pregnancy_today,
            )

    # ── Upcoming items (pending scheduled jobs) ────────────────────────
    try:
        upcoming_items = await _fetch_upcoming_items(
            pool,
            user_id=user.id,
            bot_id=bot_id,
            topic_id=primary_topic_id,
            now_utc=now_utc,
            tz_name=user_timezone,
        )
    except Exception:
        upcoming_items = []

    # ── Fitness adherence (Hector only) ────────────────────────────────
    fitness_block: str | None = None
    if bot_id == "hector":
        try:
            fitness_block = await _format_fitness_block(
                user_id=user.id,
                topic_id=primary_topic_id,
                today=now_utc,
                tz_name=user_timezone or "UTC",
                conn=pool,
            )
        except Exception:
            # Failure isolation: non-Hector bots and any DB errors
            # result in None (no fitness block)
            pass

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
        bridge_candidates=[],  # no bridge tools for solo
        outgoing_mediated_issues=outgoing_mediated_issues,
        resolved_bridge_context=resolved_bridge_context,
        recent_reactions=recent_reactions,
        silent_turns=silent_turns,
        recent_messages=recent_messages,
        topic_status=topic_status,
        cross_topic_peek=cross_topic_peek,
        pregnancy_state=pregnancy_state,
        partner_pregnancy_state=partner_pregnancy_state,
        fitness_block=fitness_block,
        upcoming_items=upcoming_items,
        bot_id=bot_id,
        time_since_last_message=_duration_since(latest_sent_at),
        trigger_metadata={
            **(trigger_metadata or {}),
            "triggering_message_ids": triggering_message_ids,
            "messages": [
                {
                    "id": row["id"],
                    "charge": row["charge"],
                    "sent_at": _iso(row["sent_at"]),
                    "sent_at_time": _time_context(
                        row["sent_at"], user_timezone, now_utc
                    ),
                    "content": row["content"] if "content" in row else None,
                    "media_type": row["media_type"] if "media_type" in row else None,
                    "media_duration_seconds": (
                        row["media_duration_seconds"]
                        if "media_duration_seconds" in row
                        else None
                    ),
                    "media_analysis": (
                        row["media_analysis"] if "media_analysis" in row else None
                    ),
                }
                for row in trigger_rows
            ],
        },
    )

async def _format_fitness_block(
    user_id: UUID,
    topic_id: UUID,
    today: datetime,
    tz_name: str,
    conn: Any,
) -> str | None:
    """Query active commitments and recent events, compute adherence, and
    return a pre-formatted '## Fitness' block string for solo hot context.

    Returns None when there are no active commitments.
    """
    from app.services.adherence import compute_adherence, summarize_board
    from zoneinfo import ZoneInfo

    try:
        _zone = ZoneInfo(tz_name)
    except Exception:
        _zone = ZoneInfo("UTC")

    _today = today.astimezone(_zone).date()

    # Fetch active commitments (max 5)
    crows = await conn.fetch(
        """
        SELECT id, label, cadence, days_of_week, target_count,
               start_date, end_date, schedule_rule, pressure_style
        FROM mediator.commitments
        WHERE user_id = $1
          AND bot_id = 'hector'
          AND topic_id = $2
          AND status = 'active'
        ORDER BY created_at
        LIMIT 5
        """,
        user_id,
        topic_id,
    )

    if not crows:
        return None

    active_commitments: list[dict[str, Any]] = []
    for crow in crows:
        cdict = dict(crow)
        cdict["id"] = str(cdict["id"])
        cdict.setdefault("pressure_style", "low_key")
        active_commitments.append(cdict)

    # Fetch recent events from last 14 days (max 10 for display)
    since = today - timedelta(days=14)
    erows = await conn.fetch(
        """
        SELECT id, commitment_id, metric_key, adherence_status,
               value_numeric, value_text, unit, observed_at, note
        FROM mediator.events
        WHERE user_id = $1
          AND bot_id = 'hector'
          AND topic_id = $2
          AND observed_at >= $3
        ORDER BY observed_at DESC
        LIMIT 10
        """,
        user_id,
        topic_id,
        since,
    )

    events: list[dict[str, Any]] = [dict(r) for r in erows]

    # Build per-commitment adherence summaries
    events_by_cid: dict[str, list[dict[str, Any]]] = {}
    for evt in events:
        cid_key = str(evt["commitment_id"]) if evt.get("commitment_id") else "_none"
        events_by_cid.setdefault(cid_key, []).append(evt)

    per_commitment_status: dict[str, str] = {}
    for cdict in active_commitments:
        cid_str = cdict["id"]
        c_evts = events_by_cid.get(cid_str, [])
        board = compute_adherence(cdict, c_evts, _today, _zone)
        per_commitment_status[board.label] = summarize_board(board)

    # Build the formatted block
    lines: list[str] = []

    # Current focus
    focus = active_commitments[0]["label"] if active_commitments else "fitness"
    lines.append(f"Current focus: {focus}")

    # Active commitments
    lines.append("Active commitments:")
    for c in active_commitments:
        ps = c.get("pressure_style", "low_key")
        lines.append(f"  - {c['label']} (pressure={ps})")

    # This week per-commitment per-day status
    if per_commitment_status:
        lines.append("This week:")
        for label, status in per_commitment_status.items():
            lines.append(f"  - {label}: {status}")

    # Recent events (~5)
    if events:
        lines.append("Recent events:")
        for e in events[:5]:
            obs = e.get("observed_at")
            if obs is not None and hasattr(obs, "isoformat"):
                obs_str = obs.isoformat()[:10]
            elif obs is not None:
                obs_str = str(obs)[:10]
            else:
                obs_str = "?"
            metric = e.get("metric_key", "")
            status = e.get("adherence_status", "")
            note = (e.get("note") or "")[:80]
            parts = [obs_str, metric, status]
            if note:
                parts.append(note)
            lines.append(f"  - {' '.join(p for p in parts if p)}")

    return "\n".join(lines)



def _line(prefix: str, value: Any) -> str:
    return f"- {prefix}: {_clip(value)}"


def _render_solo_with_counts(
    hc: HotContextSolo, truncations: dict[str, int], clip_limit: int = 240
) -> str:
    lines: list[str] = []
    lines += [
        "## You",
        f"- id: {_clip(hc.current_user['id'], clip_limit)}",
        f"- name: {_clip(hc.current_user['name'], clip_limit)}",
        f"- timezone: {_clip(hc.current_user['timezone'], clip_limit)}",
        f"- onboarding_state: {_clip(hc.current_user.get('onboarding_state', 'pending'), clip_limit)}",
        f"- partner_share: {_clip(hc.current_user.get('partner_share'), clip_limit)}",
        f"- partner_sharing_state: {_clip(hc.current_user.get('partner_sharing_state', 'unavailable'), clip_limit)}",
        f"- style_notes: {_clip(hc.current_user.get('style_notes', ''), clip_limit)}",
    ]
    open_asks = render_open_asks(
        _get_bot_asks(hc.bot_id),
        {
            "pregnancy_edd": hc.current_user.get("pregnancy_edd")
            or (hc.partner_user or {}).get("pregnancy_edd"),
            "partner_share": hc.current_user.get("partner_share"),
            "has_partner": bool(hc.partner_user),
            "partner_name": hc.partner_user.get("name") if hc.partner_user else None,
        },
    )
    if open_asks:
        lines += ["", open_asks]
    # ── Partner identity block (S1) ────────────────────────────────────
    # Identity-only by invariant 1: name, id, timezone, plus the
    # recipient-side per-bot partner_share. No memories, themes,
    # observations, distillations, messages, or pregnancy facts.
    if hc.partner_user:
        lines += [
            "",
            "## Your Partner",
            f"- name: {_clip(hc.partner_user.get('name'), clip_limit)}",
            f"- id: {_clip(hc.partner_user.get('id'), clip_limit)}",
            f"- timezone: {_clip(hc.partner_user.get('timezone'), clip_limit)}",
            f"- partner_sharing_state_for_this_bot: {_clip(hc.partner_user.get('partner_sharing_state_recipient_side', 'pending'), clip_limit)}",
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
        ts_iso = (
            ts_updated.isoformat() if hasattr(ts_updated, "isoformat") else ts_updated
        )
        lines += [
            "",
            "## Topic status",
            f"- headline: {_clip(hc.topic_status.get('headline'), clip_limit)}",
        ]
        body_text = hc.topic_status.get("body") or ""
        if body_text:
            lines.append(f"- body: {_clip(body_text, clip_limit)}")
        lines.append(f"- last_updated_at: {_clip(ts_iso, clip_limit)}")

    if hc.upcoming_items:
        lines += ["", "## Upcoming reminders"]
        for item in hc.upcoming_items:
            day = item.get("local_day_label") or ""
            t = item.get("local_time") or ""
            rel = item.get("relative_to_now") or ""
            job_type = item.get("job_type") or ""
            item_id = item.get("id") or ""
            brief = item.get("brief") or ""
            when = f"{day} {t}".strip()
            if rel:
                when = f"{when} ({rel})" if when else rel
            label = f"[{job_type}] [id={item_id}]"
            line = f"- {when} {label}".rstrip()
            if brief:
                line = f"{line} — {_clip(brief, clip_limit)}"
            lines.append(line)

    # ── Pregnancy state (Tante Rosi only) ──────────────────────────
    if hc.pregnancy_state is not None:
        lines += ["", "## Pregnancy", hc.pregnancy_state]
    elif hc.partner_pregnancy_state is not None:
        lines += ["", "## Partner pregnancy", hc.partner_pregnancy_state]

    # ── Fitness adherence (Hector only) ─────────────────────────────
    if hc.fitness_block:
        lines += ["", "## Fitness", hc.fitness_block]

    # Cross-topic peek for solo
    if hc.cross_topic_peek:
        lines += ["", "## Cross-topic activity (peek)"]
        for item in hc.cross_topic_peek:
            last_active = item.get("last_active_at")
            last_iso = (
                last_active.isoformat()
                if hasattr(last_active, "isoformat")
                else last_active
            )
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
        lines.append(
            "- use get_distillations before adding or revising synthesized explanations."
        )
    else:
        lines.append("- none")
    if truncations.get("distillations"):
        lines.append(f"- [truncated, {truncations['distillations']} more]")
    if hc.outgoing_mediated_issues:
        lines += ["", "## Outgoing mediated issues"]
        for item in hc.outgoing_mediated_issues:
            summary = _clip(item.get("shareable_summary") or "", clip_limit)
            line = (
                f"- id={_clip_id(item['id'], clip_limit)}"
                f" status={item['status']}"
                f" partner_path={item.get('partner_path')}"
                f" kind={item.get('kind')}"
            )
            if summary:
                line += f" summary={summary}"
            lines.append(line)
        lines.append(
            '- use list_bridge_candidates(scope="dyad") for full lifecycle including sent/addressed/declined.'
        )
        if truncations.get("outgoing_mediated_issues"):
            lines.append(f"- [truncated, {truncations['outgoing_mediated_issues']} more]")
    lines += ["", "## Recent messages"]
    lines.extend(
        f"- {_time_label(item, 'sent_at') or item['sent_at']} {item['direction']} charge={item['charge']} sender={item['sender_id']} recipient={item['recipient_id']}{_message_content(item, clip_limit)}{_queue_outcome_label(item)}"
        for item in hc.recent_messages
    )
    if truncations.get("recent_messages"):
        lines.append(f"- [truncated, {truncations['recent_messages']} more]")
    # Silent agent turns since the user's last message — work the agent
    # already did that did NOT produce an outbound message (e.g. a fired
    # scheduled_task that decided to stay quiet). Without this section,
    # the message timeline gives the agent no record of these turns.
    lines += ["", "## Your silent turns since the user's last message"]
    if hc.silent_turns:
        for turn in hc.silent_turns:
            started_label = _time_label(turn, "started_at") or turn.get("started_at")
            reasoning = _clip(
                (turn.get("reasoning") or "").strip().splitlines()[0]
                if turn.get("reasoning")
                else "",
                clip_limit,
            )
            lines.append(
                f"- {started_label} turn_id={turn['turn_id']}"
                f"{' bot=' + turn['bot_id'] if turn.get('bot_id') else ''}"
                f" — {reasoning or '[no reasoning recorded]'}"
            )
            for tc in turn.get("tool_calls") or []:
                summary = tc.get("summary") or f"{tc.get('tool_name')} (no summary)"
                lines.append(
                    f"    · tool_call_id={tc.get('id')} {_clip(summary, clip_limit)}"
                )
        lines.append(
            "- Use these to answer 'did you do X this morning?' truthfully."
            " Drill into a specific tool call with get_tool_call(tool_call_id)"
            " when the summary isn't enough."
        )
    else:
        lines.append("- none")
    lines += ["", "## New reactions since previous turn"]
    if hc.recent_reactions:
        lines.extend(
            f"- {_time_label(item, 'created_at') or item['created_at']} sentiment={item['sentiment']} reaction={_clip(item['content'], clip_limit)} on_message={_clip_id(item['message_id'], clip_limit)} sent={_clip(_time_label(item, 'message_sent_at') or item['message_sent_at'], clip_limit)}: {_clip(clean_user_facing_text(str(item.get('message_content') or '')) or item.get('message_content') or '[no text]', clip_limit)}"
            for item in hc.recent_reactions
        )
        lines.append(
            "- Treat these as passive feedback only; do not mention them unless naturally relevant to the user's new message."
        )
    else:
        lines.append("- none")
    lines += [
        "",
        "## Trigger",
        f"- kind: {_clip(hc.trigger_metadata.get('kind', 'inbound'), clip_limit)}",
        f"- triggering_message_ids: {_clip(', '.join(str(mid) for mid in hc.trigger_metadata['triggering_message_ids']), clip_limit)}",
        f"- time_since_last_message: {_clip(hc.time_since_last_message, clip_limit)}",
    ]
    trigger_context = hc.trigger_metadata.get("context")
    is_partner_nudge = (
        hc.trigger_metadata.get("kind") == "scheduled_task"
        and isinstance(trigger_context, dict)
        and trigger_context.get("kind") == "partner_nudge"
    )
    # Suppress the raw `- context: ...` dump for partner_nudge triggers
    # so the audit-only `reason` field cannot leak into the prompt
    # (invariant 4). The curated `## Incoming nudge from your partner`
    # block below replaces it. This branch is narrow on purpose.
    if trigger_context is not None and not is_partner_nudge:
        lines.append(f"- context: {_clip(trigger_context, clip_limit)}")
    lines.extend(
        f"- trigger_message id={msg['id']} charge={msg['charge']} sent_at={_time_label(msg, 'sent_at') or msg['sent_at']}{_message_content(msg, clip_limit)}"
        for msg in hc.trigger_metadata["messages"]
    )
    if is_partner_nudge:
        # Curated render: originator name + nudge_note ONLY. `reason` is
        # audit-only and must never appear in the rendered prompt.
        originator_name = (
            (hc.partner_user or {}).get("name")
            or trigger_context.get("originating_user_name")
            or "your partner"
        )
        nudge_note = trigger_context.get("nudge_note")
        if not nudge_note:
            nudge_note = (
                f"{originator_name} asked me to check in with you"
            )
        scheduled_for_iso = trigger_context.get("scheduled_for")
        lines += [
            "",
            "## Incoming nudge from your partner",
            f"- from: {_clip(originator_name, clip_limit)}",
            f"- note: {_clip(nudge_note, clip_limit)}",
        ]
        if scheduled_for_iso:
            lines.append(f"- scheduled_for: {_clip(scheduled_for_iso, clip_limit)}")
        rc = hc.resolved_bridge_context
        if rc is not None and not rc.get("stale"):
            lines.append(f"- about: {_clip(rc['shareable_summary'], clip_limit)}")
        elif rc is not None:
            lines.append("- about: a previously raised issue (since updated or resolved)")
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
        outgoing_mediated_issues=list(hc.outgoing_mediated_issues),
        resolved_bridge_context=hc.resolved_bridge_context,
        recent_reactions=list(hc.recent_reactions),
        silent_turns=list(hc.silent_turns),
        recent_messages=list(hc.recent_messages),
        topic_status=hc.topic_status,
        cross_topic_peek=list(hc.cross_topic_peek),
        pregnancy_state=hc.pregnancy_state,
        partner_pregnancy_state=hc.partner_pregnancy_state,
        fitness_block=hc.fitness_block,
        upcoming_items=hc.upcoming_items,
        bot_id=hc.bot_id,
        time_since_last_message=hc.time_since_last_message,
        trigger_metadata=hc.trigger_metadata,
    )
    truncations = {
        "distillations": 0,
        "observations": 0,
        "memories": 0,
        "recent_messages": 0,
        "outgoing_mediated_issues": 0,
        "conversation_load": 0,
    }
    clip_limit = 240
    text = _render_solo_with_counts(working, truncations, clip_limit)
    for name in ("distillations", "observations", "memories", "recent_messages", "outgoing_mediated_issues"):
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
