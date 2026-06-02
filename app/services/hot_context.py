"""Hot context construction for the agentic loop."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from app.bots.ids import MEDIATOR_BOT_ID
from app.bots.registry import get_relationship_topic_id
from app.config import get_settings
from app.models.user import User
from app.services.cross_thread_privacy import (
    bridge_candidate_visible_to_target,
    raw_message_visibility,
)
from app.services.partner_sharing import get_partner_share, provenance_prefix
from app.services.open_asks import _get_bot_asks, render_open_asks
from app.services.text_safety import (
    clean_user_facing_text,
    looks_like_internal_process_text,
)
from app.services.retrieval import RetrievalQuery, RetrievalResult, hybrid_search
from app.services.time_context import (
    add_calendar_months,
    temporal_reference,
    timezone_or_utc,
)
from app.services.tools.common import media_analysis_text
from app.services.pregnancy import gestational_age as _ga
from app.services.topic_filter import join_artifact_topics


CROSS_BOT_SHAREABLE_SUMMARY_CAP = 12
HOT_CONTEXT_TOPIC_RECENT_PRIOR_CAP = 5
HOT_CONTEXT_SEMANTIC_PRIOR_CAP = 5
HOT_CONTEXT_RELEVANT_PRIOR_CAP = 8
HOT_CONTEXT_LINE_CLIP_LIMIT = 240

# Source types that produce dedicated hot-context sections (memories,
# observations, distillations).  Semantic retrieval candidates with these
# source types are excluded from relevant_prior so they do not double-
# surface in both their dedicated section and the prior block.
_DEDICATED_SOURCE_TYPES: frozenset[str] = frozenset(
    {"memory", "observation", "distillation"}
)


@dataclass
class HotContext:
    current_user: dict[str, Any]
    partner_user: dict[str, Any]
    conversation_load: dict[str, Any]
    active_oob: list[dict[str, Any]]
    memories: list[dict[str, Any]]
    active_themes: list[dict[str, Any]]
    open_watch_items: list[dict[str, Any]]
    observations: list[dict[str, Any]]
    recent_messages: list[dict[str, Any]]
    time_since_last_message: str | None
    trigger_metadata: dict[str, Any]
    relevant_prior: list[dict[str, Any]] = field(default_factory=list)
    temporal_context: dict[str, Any] = field(default_factory=dict)
    distillations: list[dict[str, Any]] = field(default_factory=list)
    bridge_candidates: list[dict[str, Any]] = field(default_factory=list)
    recent_reactions: list[dict[str, Any]] = field(default_factory=list)
    silent_turns: list[dict[str, Any]] = field(default_factory=list)
    topic_status: dict[str, Any] | None = None
    cross_topic_peek: list[dict[str, Any]] = field(default_factory=list)
    cross_topic_status: list[dict[str, Any]] = field(default_factory=list)
    partner_shareable_summaries: list[dict[str, Any]] = field(default_factory=list)
    upcoming_items: list[dict[str, Any]] = field(default_factory=list)
    pending_live_conversations: list[dict[str, Any]] = field(default_factory=list)
    bot_id: str = MEDIATOR_BOT_ID


def _row_dict(row: Any) -> dict[str, Any]:
    return dict(row)


def _clean_list(value: Any) -> list[Any]:
    return list(value or [])


def _iso(value: Any) -> str | None:
    return (
        value.isoformat() if value is not None and hasattr(value, "isoformat") else None
    )


def _temporal_context(
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
    if item.get("raw_content_hidden"):
        return "[raw partner content hidden by partner_share]"
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


def _hot_context_message_from_row(
    row: dict[str, Any],
    *,
    viewer_user_id: UUID,
    partner_share_by_user: dict[UUID, str],
    timezone_name: str | None,
    now_utc: datetime,
    source: str | None = None,
    retrieval: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    thread_owner_user_id = _message_thread_owner_id(row)
    if thread_owner_user_id not in partner_share_by_user:
        return None
    visible = raw_message_visibility(
        viewer_user_id=viewer_user_id,
        thread_owner_user_id=thread_owner_user_id,
        thread_owner_partner_share=partner_share_by_user.get(thread_owner_user_id),
    ).visible
    item = {
        "id": row["id"],
        "direction": row["direction"],
        "sender_id": row["sender_id"],
        "recipient_id": row["recipient_id"],
        "content": row["content"] if visible else None,
        "media_type": row["media_type"] if "media_type" in row else None,
        "media_duration_seconds": (
            row["media_duration_seconds"] if "media_duration_seconds" in row else None
        ),
        "media_analysis": row["media_analysis"] if "media_analysis" in row else None,
        "raw_content_hidden": not visible,
        "sent_at": _iso(row["sent_at"]),
        "sent_at_time": _time_context(row["sent_at"], timezone_name, now_utc),
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
            and row["handling_result"] not in (None, "replied")
            else {}
        ),
    }
    if source is not None:
        item["source"] = source
    if retrieval is not None:
        item["retrieval"] = retrieval
    return item


def _semantic_prior_metadata(result: RetrievalResult) -> dict[str, Any]:
    return {
        "match_type": result.match_type,
        "rrf_score": result.rrf_score,
        "keyword_rank": result.keyword_rank,
        "semantic_rank": result.semantic_rank,
        "semantic_degraded": result.semantic_degraded,
        "keyword_score": result.keyword_score,
    }


def _hot_context_source_item_from_row(
    row: dict[str, Any],
    *,
    timezone_name: str | None,
    now_utc: datetime,
    source: str | None = None,
    retrieval: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sent_at = (
        row.get("sent_at")
        or row.get("source_updated_at")
        or row.get("source_created_at")
        or row.get("sort_at")
    )
    item = {
        "id": row["source_id"],
        "source_type": row["source_type"],
        "source_id": row["source_id"],
        "message_id": row.get("message_id"),
        "content": row.get("content") or row.get("canonical_text"),
        "sent_at": _iso(sent_at),
        "sent_at_time": _time_context(sent_at, timezone_name, now_utc),
        "created_at": _iso(row.get("source_created_at")),
        "updated_at": _iso(row.get("source_updated_at")),
    }
    if source is not None:
        item["source"] = source
    if retrieval is not None:
        item["retrieval"] = retrieval
    return item


def _semantic_query_text(
    trigger_rows: list[dict[str, Any]],
    *,
    viewer_user_id: UUID,
    partner_share_by_user: dict[UUID, str],
    timezone_name: str | None,
    now_utc: datetime,
) -> str:
    parts: list[str] = []
    for row in trigger_rows:
        item = _hot_context_message_from_row(
            row,
            viewer_user_id=viewer_user_id,
            partner_share_by_user=partner_share_by_user,
            timezone_name=timezone_name,
            now_utc=now_utc,
        )
        if item is None:
            continue
        if item.get("raw_content_hidden"):
            continue
        content = _history_content(item).strip()
        if content:
            parts.append(content)
    return "\n".join(parts).strip()


async def _fetch_semantic_prior(
    pool: Any,
    *,
    user: User,
    partner: User,
    bot_id: str,
    primary_topic: UUID,
    dyad_id: UUID | None,
    trigger_rows: list[dict[str, Any]],
    partner_share_by_user: dict[UUID, str],
    user_timezone: str,
    now_utc: datetime,
    excluded_message_ids: set[UUID],
) -> list[dict[str, Any]]:
    query_text = _semantic_query_text(
        trigger_rows,
        viewer_user_id=user.id,
        partner_share_by_user=partner_share_by_user,
        timezone_name=user_timezone,
        now_utc=now_utc,
    )
    if not query_text:
        return []
    try:
        retrieval_results = await hybrid_search(
            pool,
            RetrievalQuery(
                query=query_text,
                viewer_user_id=user.id,
                partner_user_id=partner.id,
                bot_id=bot_id,
                topic_id=primary_topic,
                dyad_id=dyad_id,
                mode="hybrid",
                limit=HOT_CONTEXT_SEMANTIC_PRIOR_CAP,
            ),
        )
    except Exception:
        return []
    ranked_results = [
        result
        for result in retrieval_results
        if result.source_type not in _DEDICATED_SOURCE_TYPES
        and (
            result.message_id is None
            or result.message_id not in excluded_message_ids
        )
    ][:HOT_CONTEXT_SEMANTIC_PRIOR_CAP]
    if not ranked_results:
        return []
    try:
        message_results = [result for result in ranked_results if result.message_id is not None]
        source_results = [result for result in ranked_results if result.message_id is None]
        rows_by_id: dict[UUID, dict[str, Any]] = {}
        if message_results:
            hydrated_rows = await pool.fetch(
                """
                SELECT id, direction, sender_id, recipient_id, content, media_type,
                       media_duration_seconds, media_analysis, sent_at,
                       COALESCE(charge, 'routine') AS charge, bot_id, topic_id,
                       processing_state, handling_result, handled_at, processing_error
                FROM messages
                WHERE deleted_at IS NULL
                  AND id = ANY($1::uuid[])
                  AND (sender_id = ANY($2::uuid[]) OR recipient_id = ANY($2::uuid[]))
                  AND bot_id = $3
                  AND topic_id = $4
                """,
                [result.message_id for result in message_results],
                [user.id, partner.id],
                bot_id,
                primary_topic,
            )
            rows_by_id = {row["id"]: dict(row) for row in hydrated_rows}

        rows_by_source: dict[tuple[str, UUID], dict[str, Any]] = {}
        if source_results:
            params: list[Any] = [bot_id, primary_topic]
            dyad_clause = ""
            if dyad_id is not None:
                dyad_clause = "AND (sc.dyad_id = $3 OR sc.dyad_id IS NULL)"
                params.append(dyad_id)
            source_types_param = len(params) + 1
            params.append([result.source_type for result in source_results])
            source_ids_param = len(params) + 1
            params.append([result.source_id for result in source_results])
            hydrated_source_rows = await pool.fetch(
                f"""
                WITH ranked_sources AS (
                    SELECT ranked.source_type, ranked.source_id, ranked.ordinality
                    FROM unnest(
                        ${source_types_param}::text[],
                        ${source_ids_param}::uuid[]
                    ) WITH ORDINALITY AS ranked(source_type, source_id, ordinality)
                )
                SELECT
                    sc.source_type,
                    sc.source_id,
                    sc.message_id,
                    sc.sent_at,
                    sc.source_created_at,
                    sc.source_updated_at,
                    sc.sort_at,
                    sc.canonical_text AS content
                FROM ranked_sources
                JOIN mediator.v_searchable_content sc
                  ON sc.source_type = ranked_sources.source_type
                 AND sc.source_id = ranked_sources.source_id
                WHERE sc.source_type <> 'message'
                  AND sc.bot_id = $1
                  AND sc.topic_id = $2
                  {dyad_clause}
                ORDER BY ranked_sources.ordinality ASC
                """,
                *params,
            )
            rows_by_source = {
                (str(row["source_type"]), row["source_id"]): dict(row)
                for row in hydrated_source_rows
            }
        semantic_prior: list[dict[str, Any]] = []
        for result in ranked_results:
            if result.message_id is not None:
                row = rows_by_id.get(result.message_id)
                if row is None:
                    continue
                item = _hot_context_message_from_row(
                    row,
                    viewer_user_id=user.id,
                    partner_share_by_user=partner_share_by_user,
                    timezone_name=user_timezone,
                    now_utc=now_utc,
                    source="semantic",
                    retrieval=_semantic_prior_metadata(result),
                )
            else:
                row = rows_by_source.get((result.source_type, result.source_id))
                if row is None:
                    continue
                item = _hot_context_source_item_from_row(
                    row,
                    timezone_name=user_timezone,
                    now_utc=now_utc,
                    source="semantic",
                    retrieval=_semantic_prior_metadata(result),
                )
            if item is not None:
                semantic_prior.append(item)
        return semantic_prior
    except Exception:
        return []


def _clip_id(value: Any, clip_limit: int) -> str:
    return _clip(value, 14 if clip_limit < 60 else clip_limit)


def _profile_partner_share(profile: dict[str, Any]) -> Any:
    return profile.get("partner_share")


def _partner_sharing_state(partner_share: str | None, *, has_partner: bool) -> str:
    if partner_share in {"opt_in", "opt_out"}:
        return partner_share
    return "pending" if has_partner else "unavailable"


async def _user_profile(pool: Any, user: User) -> dict[str, Any]:
    row = await pool.fetchrow(
        """
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
            "partner_share": None,
            "pregnancy_edd": None,
            "pregnancy_dating_basis": None,
            "pregnancy_lmp_date": None,
            "pregnancy_scan_date": None,
            "pregnancy_scan_corrected_at": None,
            "pregnancy_started_at": None,
            "pregnancy_ended_at": None,
            "pregnancy_outcome": None,
        }
    profile = _row_dict(row)
    profile["partner_share"] = None
    return profile


async def _get_partner_share_for_hot_context(
    pool: Any, *, user: User, bot_id: str
) -> str | None:
    if hasattr(pool, "fetchval"):
        return await get_partner_share(pool, user_id=user.id, bot_id=bot_id)
    return None


async def _fetch_partner_shareable_summaries(
    pool: Any,
    *,
    owner_user_id: UUID,
    viewer_timezone: str,
    now_utc: datetime,
    current_bot_id: str,
) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        """
        WITH partner_rows AS (
            SELECT
                'memory'::text AS kind,
                m.id,
                m.recorded_by_bot_id AS bot_id,
                m.shareable_summary,
                COALESCE(m.last_referenced_at, m.created_at) AS occurred_at
            FROM memories m
            JOIN user_bot_state ubs
              ON ubs.user_id = $1
             AND ubs.bot_id = m.recorded_by_bot_id
             AND ubs.partner_share = 'opt_in'
            /* cross-bot partner sharing intentionally spans bot domains; no join_artifact_topics filter */
            WHERE m.status = 'active'
              AND m.visibility = 'dyad_shareable'
              AND m.shareable_summary IS NOT NULL
              AND length(btrim(m.shareable_summary)) > 0
              AND m.about_user_id = $1
              AND m.recorded_by_bot_id IS NOT NULL
              AND m.recorded_by_bot_id <> $3

            UNION ALL

            SELECT
                'distillation'::text AS kind,
                d.id,
                COALESCE(d.recorded_by_bot_id, tm.bot_id) AS bot_id,
                d.shareable_summary,
                COALESCE(d.updated_at, d.created_at) AS occurred_at
            FROM distillations d
            LEFT JOIN messages tm ON tm.id = d.triggering_message_id
            JOIN user_bot_state ubs
              ON ubs.user_id = $1
             AND ubs.bot_id = COALESCE(d.recorded_by_bot_id, tm.bot_id)
             AND ubs.partner_share = 'opt_in'
            /* cross-bot partner sharing intentionally spans bot domains; no join_artifact_topics filter */
            WHERE d.status = 'active'
              AND d.visibility = 'dyad_shareable'
              AND d.shareable_summary IS NOT NULL
              AND length(btrim(d.shareable_summary)) > 0
              AND d.source_user_ids && ARRAY[$1]::uuid[]
              AND COALESCE(d.recorded_by_bot_id, tm.bot_id) IS NOT NULL
              AND COALESCE(d.recorded_by_bot_id, tm.bot_id) <> $3
        )
        SELECT kind, id, bot_id, shareable_summary, occurred_at
        FROM partner_rows
        ORDER BY occurred_at DESC NULLS LAST
        LIMIT $2
        """,
        owner_user_id,
        CROSS_BOT_SHAREABLE_SUMMARY_CAP,
        current_bot_id,
    )
    prefix_by_bot: dict[str, str] = {}
    summaries: list[dict[str, Any]] = []
    # Cross-bot summaries are opt-in gated in SQL first, then globally
    # recency-ordered and capped so non-opted-in bots cannot starve valid rows.
    for row in rows:
        row_bot_id = row["bot_id"]
        if row_bot_id is None:
            continue
        if row_bot_id not in prefix_by_bot:
            prefix_by_bot[row_bot_id] = await provenance_prefix(pool, row_bot_id)
        summaries.append(
            {
                "kind": row["kind"],
                "id": row["id"],
                "bot_id": row_bot_id,
                "provenance": prefix_by_bot[row_bot_id],
                "shareable_summary": row["shareable_summary"],
                "occurred_at": _iso(row["occurred_at"]),
                "occurred_at_time": _time_context(
                    row["occurred_at"], viewer_timezone, now_utc
                ),
            }
        )
    return summaries


async def fetch_cross_topic_status(
    pool: Any,
    *,
    dyad_id: UUID | None,
    user_id: UUID,
    exclude_topic_id: UUID,
    cap: int = 5,
) -> list[dict[str, Any]]:
    """Fetch the most-recently-updated topic_status rows from OTHER topics.

    Per §16.5 lock decision D: cap N=5. Used by allow_cross_topic_status_injection.
    With one topic in play, this returns []; no header is rendered.
    """
    if dyad_id is not None:
        rows = await pool.fetch(
            """
            SELECT id, topic_id, headline, body, last_updated_at
            FROM topic_status
            WHERE dyad_id = $1 AND topic_id <> $2
            ORDER BY last_updated_at DESC
            LIMIT $3
            """,
            dyad_id,
            exclude_topic_id,
            cap,
        )
    else:
        rows = await pool.fetch(
            """
            SELECT id, topic_id, headline, body, last_updated_at
            FROM topic_status
            WHERE user_id = $1 AND topic_id <> $2
            ORDER BY last_updated_at DESC
            LIMIT $3
            """,
            user_id,
            exclude_topic_id,
            cap,
        )
    return [dict(row) for row in rows]


async def peek_other_topics(
    pool: Any,
    *,
    dyad_id: UUID | None,
    user_id: UUID,
    exclude_topic_id: UUID,
    since: datetime,
    cap: int = 5,
) -> list[dict[str, Any]]:
    """Fetch recently-active OTHER topics for the dyad/user (peek window).

    Per §16.5 lock decision A: 14-day window (caller passes `since`).
    Per §16.5 lock decision D: cap N=5.
    Returns [] with one topic in play.
    """
    if dyad_id is not None:
        rows = await pool.fetch(
            """
            SELECT t.id AS topic_id, t.slug, t.display_name, MAX(ts.last_updated_at) AS last_active_at
            FROM topics t
            JOIN topic_status ts ON ts.topic_id = t.id
            WHERE ts.dyad_id = $1
              AND ts.topic_id <> $2
              AND ts.last_updated_at >= $3
            GROUP BY t.id, t.slug, t.display_name
            ORDER BY MAX(ts.last_updated_at) DESC
            LIMIT $4
            """,
            dyad_id,
            exclude_topic_id,
            since,
            cap,
        )
    else:
        rows = await pool.fetch(
            """
            SELECT t.id AS topic_id, t.slug, t.display_name, MAX(ts.last_updated_at) AS last_active_at
            FROM topics t
            JOIN topic_status ts ON ts.topic_id = t.id
            WHERE ts.user_id = $1
              AND ts.topic_id <> $2
              AND ts.last_updated_at >= $3
            GROUP BY t.id, t.slug, t.display_name
            ORDER BY MAX(ts.last_updated_at) DESC
            LIMIT $4
            """,
            user_id,
            exclude_topic_id,
            since,
            cap,
        )
    return [dict(row) for row in rows]


async def _fetch_topic_status(
    pool: Any,
    *,
    topic_id: UUID,
    user_id: UUID,
    dyad_id: UUID | None,
) -> dict[str, Any] | None:
    """Fetch the topic_status row for this scope; dyad row wins when dyad_id set."""
    if dyad_id is not None:
        row = await pool.fetchrow(
            """
            SELECT id, headline, body, last_updated_at
            FROM topic_status
            WHERE topic_id = $1 AND dyad_id = $2
            """,
            topic_id,
            dyad_id,
        )
        if row is not None:
            return dict(row)
    row = await pool.fetchrow(
        """
        SELECT id, headline, body, last_updated_at
        FROM topic_status
        WHERE topic_id = $1 AND user_id = $2
        """,
        topic_id,
        user_id,
    )
    return dict(row) if row is not None else None


async def _fetch_pending_live_conversations(
    pool: Any,
    *,
    user_id: UUID,
    bot_id: str,
    topic_id: UUID,
) -> list[dict[str, Any]]:
    """Return pending live-voice conversations for (user, bot, topic).

    Includes statuses 'prepping', 'preparing', and 'ready' per gate warning
    (migration 0055 adds 'preparing' as a valid DB value and the web router
    writes it).  Each dict carries id, status, current_item_id, started_at,
    and a derived title from the first conversation_item ordered by order_hint.
    """
    rows = await pool.fetch(
        """\
        SELECT c.id, c.status, c.current_item_id, c.started_at
        FROM mediator.conversations c
        WHERE c.user_id = $1
          AND c.bot_id = $2
          AND c.topic_id = $3
          AND c.status IN ('prepping', 'preparing', 'ready')
        ORDER BY c.started_at DESC
        LIMIT 5
        """,
        user_id,
        bot_id,
        topic_id,
    )
    if not rows:
        return []

    result: list[dict[str, Any]] = []
    for row in rows:
        title = "Untitled"
        item_rows = await pool.fetch(
            """\
            SELECT ci.title
            FROM mediator.conversation_items ci
            WHERE ci.conversation_id = $1
              AND ci.kind = 'planned'
            ORDER BY ci.order_hint
            LIMIT 1
            """,
            row["id"],
        )
        if item_rows and item_rows[0]["title"]:
            title = item_rows[0]["title"]
        result.append(
            {
                "id": str(row["id"]),
                "status": row["status"],
                "current_item_id": (
                    str(row["current_item_id"]) if row["current_item_id"] else None
                ),
                "started_at": _iso(row["started_at"]),
                "title": title,
            }
        )
    return result


async def build_hot_context(
    pool: Any,
    user: User,
    partner: User,
    triggering_message_ids: list[UUID],
    trigger_metadata: dict[str, Any] | None = None,
    *,
    primary_topic_id: UUID | None = None,
    dyad_id: UUID | None = None,
    allow_cross_topic_peek: bool = False,
    allow_cross_topic_status_injection: bool = False,
    bot_id: str = MEDIATOR_BOT_ID,
) -> HotContext:
    primary_topic = primary_topic_id or get_relationship_topic_id()
    if primary_topic is None:
        raise RuntimeError(
            "build_hot_context: no primary_topic_id provided and relationship topic not available"
        )
    topic_status = await _fetch_topic_status(
        pool, topic_id=primary_topic, user_id=user.id, dyad_id=dyad_id
    )
    current_user = await _user_profile(pool, user)
    partner_user = await _user_profile(pool, partner)
    current_user["partner_share"] = await _get_partner_share_for_hot_context(
        pool, user=user, bot_id=bot_id
    )
    partner_user["partner_share"] = await _get_partner_share_for_hot_context(
        pool, user=partner, bot_id=bot_id
    )
    current_user["partner_share"] = _profile_partner_share(current_user)
    partner_user["partner_share"] = _profile_partner_share(partner_user)
    current_user["partner_sharing_state"] = _partner_sharing_state(
        current_user["partner_share"], has_partner=True
    )
    partner_user["partner_sharing_state"] = _partner_sharing_state(
        partner_user["partner_share"], has_partner=True
    )
    now_utc = datetime.now(UTC)
    user_timezone = timezone_or_utc(current_user.get("timezone") or user.timezone).key
    conversation_load_row = await pool.fetchrow(
        """
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
           AND m.bot_id = $3
           AND m.topic_id = $4
        GROUP BY bounds.period_start, bounds.period_end
        """,
        user.id,
        user_timezone,
        bot_id,
        primary_topic,
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
            f"""
            SELECT x.id, x.owner_id, x.shareable_context, x.severity, x.review_at
            FROM out_of_bounds x
            {join_artifact_topics('x', '$2')}
            WHERE x.status = 'active' AND x.owner_id = ANY($1::uuid[])
            ORDER BY CASE x.severity WHEN 'hard' THEN 1 WHEN 'firm' THEN 2 ELSE 3 END, x.created_at DESC
            """,
            [user.id, partner.id],
            primary_topic,
        )
    ]
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
            f"""
            SELECT m.id, m.about_user_id, m.content, COALESCE(m.related_theme_ids, '{{}}'::uuid[]) AS related_theme_ids,
                   m.last_referenced_at, m.created_at
            FROM memories m
            {join_artifact_topics('m', '$2')}
            WHERE m.status = 'active' AND (m.about_user_id = $1 OR m.about_user_id IS NULL)
            ORDER BY COALESCE(m.last_referenced_at, m.created_at) DESC
            LIMIT 80
            """,
            user.id,
            primary_topic,
        )
    ]
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
            f"""
            SELECT t.id, t.title, t.description, t.status, t.sentiment, t.health, t.last_reinforced_at, t.last_active_at
            FROM themes t
            {join_artifact_topics('t', '$1')}
            WHERE t.status = 'active'
            ORDER BY COALESCE(t.last_reinforced_at, t.first_seen_at) DESC
            LIMIT 10
            """,
            primary_topic,
        )
    ]
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
            f"""
            SELECT w.id, w.owner_user_id, w.content, w.due_at, COALESCE(w.related_theme_ids, '{{}}'::uuid[]) AS related_theme_ids
            FROM watch_items w
            {join_artifact_topics('w', '$2')}
            WHERE w.status = 'open' AND w.owner_user_id = $1
            ORDER BY COALESCE(w.due_at, w.created_at) ASC
            """,
            user.id,
            primary_topic,
        )
    ]
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
            f"""
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
            primary_topic,
        )
    ]
    message_rows = await pool.fetch(
        """
        SELECT id, direction, sender_id, recipient_id, content, media_type, media_duration_seconds,
               media_analysis, sent_at, COALESCE(charge, 'routine') AS charge, bot_id, topic_id,
               processing_state, handling_result, handled_at, processing_error
        FROM messages
        WHERE deleted_at IS NULL
          AND (sender_id = ANY($1::uuid[]) OR recipient_id = ANY($1::uuid[]))
          AND bot_id = $2
          AND topic_id = $3
        ORDER BY sent_at DESC
        LIMIT 20
        """,
        [user.id, partner.id],
        bot_id,
        primary_topic,
    )
    partner_share_by_user = {
        user.id: current_user.get("partner_share") or "unset",
        partner.id: partner_user.get("partner_share") or "unset",
    }
    distillation_rows = await pool.fetch(
        f"""
        SELECT d.id, d.content, d.confidence, d.status, d.sensitivity, d.visibility, d.shareable_summary,
               COALESCE(d.source_user_ids, '{{}}'::uuid[]) AS source_user_ids,
               COALESCE(d.related_memory_ids, '{{}}'::uuid[]) AS related_memory_ids,
               COALESCE(d.related_observation_ids, '{{}}'::uuid[]) AS related_observation_ids,
               COALESCE(d.related_theme_ids, '{{}}'::uuid[]) AS related_theme_ids,
               COALESCE(d.supporting_message_ids, '{{}}'::uuid[]) AS supporting_message_ids,
               d.revision_note, d.revision_count, d.updated_at, d.created_at,
               d.recorded_by_bot_id, COALESCE(d.recorded_by_bot_id, tm.bot_id) AS visibility_bot_id
        FROM distillations d
        LEFT JOIN messages tm ON tm.id = d.triggering_message_id
        {join_artifact_topics('d', '$2')}
        WHERE d.status = 'active'
          AND d.source_user_ids && $1::uuid[]
        ORDER BY d.updated_at DESC, d.created_at DESC
        LIMIT 12
        """,
        [user.id, partner.id],
        primary_topic,
    )
    distillations: list[dict[str, Any]] = []
    partner_share_by_owner_bot: dict[tuple[Any, str], str] = {}
    for row in distillation_rows:
        source_user_ids = _clean_list(row["source_user_ids"])
        visibility_bot_id = (
            row["visibility_bot_id"] if "visibility_bot_id" in row else None
        )
        full_visible = bool(source_user_ids)
        summary_visible = bool(source_user_ids)
        for source_user_id in source_user_ids:
            if source_user_id == user.id:
                owner_partner_share = "opt_in"
            elif visibility_bot_id is None:
                owner_partner_share = "unset"
            elif (
                visibility_bot_id == bot_id and source_user_id in partner_share_by_user
            ):
                owner_partner_share = partner_share_by_user[source_user_id]
            else:
                cache_key = (source_user_id, visibility_bot_id)
                if cache_key not in partner_share_by_owner_bot:
                    partner_share_by_owner_bot[cache_key] = (
                        await get_partner_share(
                            pool, user_id=source_user_id, bot_id=visibility_bot_id
                        )
                        or "unset"
                    )
                owner_partner_share = partner_share_by_owner_bot[cache_key]
            if not raw_message_visibility(
                viewer_user_id=user.id,
                thread_owner_user_id=source_user_id,
                thread_owner_partner_share=owner_partner_share,
            ).visible:
                full_visible = False
            if source_user_id != user.id and owner_partner_share != "opt_in":
                summary_visible = False
        if full_visible and all(
            source_user_id == user.id for source_user_id in source_user_ids
        ):
            content = row["content"]
            display = "full_content"
        elif (
            summary_visible
            and row["visibility"] == "dyad_shareable"
            and row["shareable_summary"]
        ):
            content = row["shareable_summary"]
            display = "shareable_summary"
        else:
            continue
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
    recent_messages = [
        {
            "id": row["id"],
            "direction": row["direction"],
            "sender_id": row["sender_id"],
            "recipient_id": row["recipient_id"],
            "content": (
                row["content"]
                if raw_message_visibility(
                    viewer_user_id=user.id,
                    thread_owner_user_id=_message_thread_owner_id(row),
                    thread_owner_partner_share=partner_share_by_user.get(
                        _message_thread_owner_id(row)
                    ),
                ).visible
                else None
            ),
            "media_type": row["media_type"] if "media_type" in row else None,
            "media_duration_seconds": (
                row["media_duration_seconds"]
                if "media_duration_seconds" in row
                else None
            ),
            "media_analysis": (
                row["media_analysis"] if "media_analysis" in row else None
            ),
            "raw_content_hidden": not raw_message_visibility(
                viewer_user_id=user.id,
                thread_owner_user_id=_message_thread_owner_id(row),
                thread_owner_partner_share=partner_share_by_user.get(
                    _message_thread_owner_id(row)
                ),
            ).visible,
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
        if _message_thread_owner_id(row) in partner_share_by_user
    ]
    hot_context_window_edge = (
        {
            "message_id": str(recent_messages[0]["id"]),
            "sent_at": recent_messages[0]["sent_at"],
        }
        if recent_messages
        else None
    )

    # ── Topic-recent prior older than the hot-context window edge ──────
    # Same bot, same topic, same participant scope, undeleted messages
    # strictly older than the window edge (or older than now if no edge).
    # Transformed with the identical visibility / redaction logic as the
    # recent-message window.
    topic_recent_edge_sent_at = (
        hot_context_window_edge["sent_at"]
        if hot_context_window_edge is not None
        else now_utc.isoformat()
    )
    topic_recent_prior_rows = await pool.fetch(
        """\
        SELECT id, direction, sender_id, recipient_id, content, media_type,
               media_duration_seconds, media_analysis, sent_at,
               COALESCE(charge, 'routine') AS charge, bot_id, topic_id,
               processing_state, handling_result, handled_at, processing_error
        FROM messages
        WHERE deleted_at IS NULL
          AND (sender_id = ANY($1::uuid[]) OR recipient_id = ANY($1::uuid[]))
          AND bot_id = $2
          AND topic_id = $3
          AND sent_at < $4::timestamptz
        ORDER BY sent_at DESC, id DESC
        LIMIT $5
        """,
        [user.id, partner.id],
        bot_id,
        primary_topic,
        topic_recent_edge_sent_at,
        HOT_CONTEXT_TOPIC_RECENT_PRIOR_CAP,
    )
    relevant_prior = [
        item
        for row in reversed(topic_recent_prior_rows)
        if (
            item := _hot_context_message_from_row(
                dict(row),
                viewer_user_id=user.id,
                partner_share_by_user=partner_share_by_user,
                timezone_name=user_timezone,
                now_utc=now_utc,
                source="topic_recent",
            )
        )
        is not None
    ]

    bridge_candidate_rows = await pool.fetch(
        """
        SELECT id, source_user_id, target_user_id, kind, status, sensitivity, partner_path,
               shareable_summary, created_at
        FROM bridge_candidates
        WHERE target_user_id=$1
          AND source_user_id=$2
          AND status='ready'
          AND partner_path='message_partner'
        ORDER BY created_at DESC
        LIMIT 5
        """,
        user.id,
        partner.id,
    )
    bridge_candidates = [
        {
            "id": row["id"],
            "source_user_id": row["source_user_id"],
            "target_user_id": row["target_user_id"],
            "kind": row["kind"],
            "status": row["status"],
            "sensitivity": row["sensitivity"],
            "partner_path": row["partner_path"],
            "shareable_summary": row["shareable_summary"],
        }
        for row in bridge_candidate_rows
        if bridge_candidate_visible_to_target(row, target_user_id=user.id)
    ]
    latest_sent_at = max((row["sent_at"] for row in message_rows), default=None)
    trigger_rows = await pool.fetch(
        """
        SELECT id, direction, sender_id, recipient_id, COALESCE(charge, 'routine') AS charge,
               sent_at, content, media_type, media_duration_seconds, media_analysis, bot_id, topic_id
        FROM messages
        WHERE id = ANY($1::uuid[])
          AND bot_id = $2
          AND topic_id = $3
        ORDER BY sent_at ASC
        """,
        triggering_message_ids,
        bot_id,
        primary_topic,
    )
    excluded_prior_ids = {
        item["id"] for item in recent_messages if item.get("id") is not None
    }
    semantic_prior = await _fetch_semantic_prior(
        pool,
        user=user,
        partner=partner,
        bot_id=bot_id,
        primary_topic=primary_topic,
        dyad_id=dyad_id,
        trigger_rows=[dict(row) for row in trigger_rows],
        partner_share_by_user=partner_share_by_user,
        user_timezone=user_timezone,
        now_utc=now_utc,
        excluded_message_ids=excluded_prior_ids,
    )
    # ── Merge: topic-recent floor + semantic net-new ────────────────────
    # Build a combined relevant_prior.  Invariants:
    #   • Exclude recent-window IDs (already handled: semantic retrieval
    #     filters excluded_prior_ids; topic-recent query filters by
    #     sent_at < window edge).
    #   • Prefer topic-recent records on duplicate message IDs — the
    #     topic_recent item stays as the canonical row while semantic
    #     retrieval metadata is carried over as enrichment.
    #   • Keep up to HOT_CONTEXT_TOPIC_RECENT_PRIOR_CAP (5) topic-recent
    #     rows plus up to 3 net-new semantic rows, final cap at
    #     HOT_CONTEXT_RELEVANT_PRIOR_CAP (8).
    #   • Sort the merged list chronologically (oldest first) so the
    #     agent sees a readable, time-ordered context block.
    if semantic_prior:
        topic_recent_items = list(relevant_prior[:HOT_CONTEXT_TOPIC_RECENT_PRIOR_CAP])
        topic_recent_by_id: dict[Any, dict[str, Any]] = {
            item["id"]: item for item in topic_recent_items if item.get("id") is not None
        }
        semantic_items: list[dict[str, Any]] = []
        for item in semantic_prior:
            existing = topic_recent_by_id.get(item["id"])
            if existing is not None:
                # Duplicate: keep the topic_recent item but carry over
                # semantic retrieval metadata (match_type, rrf_score,
                # keyword_rank, semantic_rank, semantic_degraded,
                # keyword_score).
                existing["retrieval"] = item.get("retrieval")
                continue
            semantic_items.append(item)
        net_new_semantic = semantic_items[
            : max(0, HOT_CONTEXT_RELEVANT_PRIOR_CAP - len(topic_recent_items))
        ]
        relevant_prior = sorted(
            topic_recent_items + net_new_semantic,
            key=lambda item: (item["sent_at"] or "", str(item["id"])),
        )[:HOT_CONTEXT_RELEVANT_PRIOR_CAP]
    else:
        # No semantic results: cap topic-recent at the overall cap.
        relevant_prior = relevant_prior[:HOT_CONTEXT_RELEVANT_PRIOR_CAP]
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
                """
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
                  AND m.bot_id = $3
                  AND m.topic_id = $4
                  AND f.created_at > (SELECT completed_at FROM previous_turn)
                  AND f.created_at <= $2
                ORDER BY f.created_at DESC
                LIMIT 5
                """,
                user.id,
                now_utc,
                bot_id,
                primary_topic,
            )
        )
    ]
    # Silent agent turns since the user's last message in this dyad/bot —
    # turns that did work (e.g. fired a scheduled task) but produced no
    # outbound message. The message timeline can't show these.
    silent_turns_rows = await pool.fetch(
        """\
        WITH last_inbound AS (
            SELECT MAX(sent_at) AS sent_at
            FROM messages
            WHERE direction = 'inbound'
              AND sender_id = $1
              AND bot_id = $3
              AND topic_id = $4
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
          AND bt.bot_id = $3
          AND bt.topic_id = $4
        GROUP BY bt.id
        ORDER BY bt.completed_at DESC
        LIMIT 5
        """,
        user.id,
        now_utc,
        bot_id,
        primary_topic,
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
            dyad_id=dyad_id,
            user_id=user.id,
            exclude_topic_id=primary_topic,
            since=peek_since,
        )
    cross_topic_status: list[dict[str, Any]] = []
    if allow_cross_topic_status_injection:
        cross_topic_status = await fetch_cross_topic_status(
            pool,
            dyad_id=dyad_id,
            user_id=user.id,
            exclude_topic_id=primary_topic,
        )
    partner_shareable_summaries = await _fetch_partner_shareable_summaries(
        pool,
        owner_user_id=partner.id,
        viewer_timezone=user_timezone,
        now_utc=now_utc,
        current_bot_id=bot_id,
    )
    try:
        from app.services.hot_context_solo import _fetch_upcoming_items as _fetch_upcoming
        upcoming_items = await _fetch_upcoming(
            pool,
            user_id=user.id,
            bot_id=bot_id,
            topic_id=primary_topic_id,
            now_utc=now_utc,
            tz_name=user_timezone,
        )
    except Exception:
        upcoming_items = []

    try:
        pending_live_conversations = await _fetch_pending_live_conversations(
            pool,
            user_id=user.id,
            bot_id=bot_id,
            topic_id=primary_topic_id,
        )
    except Exception:
        pending_live_conversations = []

    return HotContext(
        current_user=current_user,
        partner_user=partner_user,
        temporal_context=_temporal_context(user_timezone, now_utc),
        conversation_load=conversation_load,
        active_oob=active_oob,
        memories=memories,
        active_themes=active_themes,
        open_watch_items=open_watch_items,
        observations=observations,
        distillations=distillations,
        bridge_candidates=bridge_candidates,
        recent_reactions=recent_reactions,
        silent_turns=silent_turns,
        recent_messages=recent_messages,
        relevant_prior=relevant_prior,
        topic_status=topic_status,
        cross_topic_peek=cross_topic_peek,
        cross_topic_status=cross_topic_status,
        partner_shareable_summaries=partner_shareable_summaries,
        upcoming_items=upcoming_items,
        pending_live_conversations=pending_live_conversations,
        bot_id=bot_id,
        time_since_last_message=_duration_since(latest_sent_at),
        trigger_metadata={
            **(trigger_metadata or {}),
            **(
                {
                    "hot_context_window_edge": hot_context_window_edge,
                    "hot_context_edge": hot_context_window_edge,
                }
                if hot_context_window_edge is not None
                else {}
            ),
            "triggering_message_ids": triggering_message_ids,
            "messages": [
                {
                    "id": row["id"],
                    "charge": row["charge"],
                    "sent_at": _iso(row["sent_at"]),
                    "sent_at_time": _time_context(
                        row["sent_at"], user_timezone, now_utc
                    ),
                    "content": (
                        row["content"]
                        if "content" in row
                        and raw_message_visibility(
                            viewer_user_id=user.id,
                            thread_owner_user_id=_message_thread_owner_id(row),
                            thread_owner_partner_share=partner_share_by_user.get(
                                _message_thread_owner_id(row)
                            ),
                        ).visible
                        else None
                    ),
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


def _line(prefix: str, value: Any) -> str:
    return f"- {prefix}: {_clip(value)}"


def _message_thread_owner_id(row: Any) -> Any:
    direction = row["direction"] if "direction" in row else None
    sender_id = row["sender_id"] if "sender_id" in row else None
    recipient_id = row["recipient_id"] if "recipient_id" in row else None
    if direction == "inbound" and sender_id is not None:
        return sender_id
    if direction == "outbound" and recipient_id is not None:
        return recipient_id
    return sender_id or recipient_id


def _render_partner_pregnancy_state(
    partner_user: dict[str, Any], partner_name: str, clip_limit: int = 240
) -> str | None:
    """Render the one-line partner pregnancy summary for dyad hot context.

    Per §4.1: this is the ONLY pregnancy data surfaced to the mediator.
    Never auto-bridges symptoms, themes, weight, or observations.

    Returns None when there is nothing to render (no pregnancy, ended >90d,
    or data-corruption).
    """
    from datetime import date

    pregnancy_edd = partner_user.get("pregnancy_edd")
    if pregnancy_edd is None:
        return None

    pregnancy_ended_at = partner_user.get("pregnancy_ended_at")

    # --- Ended pregnancy -------------------------------------------------
    if pregnancy_ended_at is not None:
        pregnancy_outcome = partner_user.get("pregnancy_outcome")
        if pregnancy_outcome is None or pregnancy_outcome not in (
            "loss",
            "termination",
        ):
            return None

        # Compute days since ended_at.
        if hasattr(pregnancy_ended_at, "date"):
            ended_date = pregnancy_ended_at.date()
        elif isinstance(pregnancy_ended_at, date):
            ended_date = pregnancy_ended_at
        else:
            return None

        _today = date.today()
        days_ago = (_today - ended_date).days
        if days_ago > 90:
            return None

        partner_label = _clip(partner_name, clip_limit)
        return (
            f"- {partner_label}'s pregnancy ended recently "
            f"(loss, {days_ago} days ago). Handle with care."
        )

    # --- Active pregnancy ------------------------------------------------
    pregnancy_dating_basis = partner_user.get("pregnancy_dating_basis")
    if pregnancy_dating_basis is None:
        return None

    try:
        weeks, days = _ga(pregnancy_edd)
    except (ValueError, TypeError):
        return None

    edd_str = (
        pregnancy_edd.isoformat()
        if hasattr(pregnancy_edd, "isoformat")
        else str(pregnancy_edd)
    )
    partner_label = _clip(partner_name, clip_limit)

    return f"- {partner_label} is currently {weeks}w{days}d pregnant (EDD {edd_str})."


# ---------------------------------------------------------------------------
# Render-only helper functions (shared vocabulary across prior + dedicated
# sections).  These are render-only, deterministic, and free of database
# access / selection-side effects.  Name resolution uses only hc.current_user
# and hc.partner_user (no additional queries).
# ---------------------------------------------------------------------------


def _hot_context_name_map(hc: "HotContext") -> dict[str, str]:
    """Build a user-id → name lookup from hc.current_user / hc.partner_user.

    No database queries — the two in-memory user dicts are the only
    resolution sources.  Unknown ids fall back to str(id) downstream.
    """
    name_map: dict[str, str] = {}
    for user in (hc.current_user, hc.partner_user):
        if isinstance(user, dict):
            uid = user.get("id")
            if uid is not None:
                name_map[str(uid)] = user.get("name") or str(uid)
    return name_map


def _user_label(user_id: object, name_map: dict[str, str]) -> str:
    """Return a human-readable label for *user_id*.

    Resolves through *name_map* (built by ``_hot_context_name_map``);
    falls back to ``str(user_id)`` when no entry exists.
    """
    key = str(user_id) if user_id is not None else "unknown"
    return name_map.get(key, key)


def _source_handle(
    item: dict[str, object], name_map: dict[str, str] | None = None
) -> str:
    """Build a compact source-type "handle" for a prior / knowledge item.

    Dedicated types (memory / observation / distillation) return just the
    type name.  Message items produce ``source=… [match, rrf=…]`` when
    retrieval metadata is present, falling back to direction + charge.
    """
    source_type = str(item.get("source_type", ""))

    # ---- dedicated types ---------------------------------------------------
    if source_type in ("memory", "observation", "distillation"):
        return source_type
    # ---- non-message knowledge items (conversation_note / theme / artifact) -
    # Produce a readable type label enriched with any retrieval metadata.
    if source_type in ("conversation_note", "theme", "artifact"):
        label = f"[{source_type}]"
        retrieval = item.get("retrieval")
        if isinstance(retrieval, dict):
            match = str(retrieval.get("match_type", ""))
            score = retrieval.get("rrf_score")
            retrieval_parts: list[str] = []
            if match:
                retrieval_parts.append(match)
            if score is not None:
                retrieval_parts.append(f"rrf={float(score):.4f}")
            if retrieval_parts:
                label = f"{label} [{', '.join(retrieval_parts)}]"
        return label

    # ---- message-like items ------------------------------------------------
    parts: list[str] = []
    if source_type and source_type != "message":
        parts.append(source_type)
    source = str(item.get("source", ""))
    if source:
        parts.append(f"source={source}")

    retrieval = item.get("retrieval")
    if isinstance(retrieval, dict):
        match = str(retrieval.get("match_type", ""))
        score = retrieval.get("rrf_score")
        retrieval_parts: list[str] = []
        if match:
            retrieval_parts.append(match)
        if score is not None:
            retrieval_parts.append(f"rrf={float(score):.4f}")
        if retrieval_parts:
            parts.append(f"[{', '.join(retrieval_parts)}]")

    if not parts:
        # Fallback to direction + charge when neither source nor retrieval is set
        direction = str(item.get("direction", ""))
        charge = str(item.get("charge", ""))
        if direction:
            segment = direction
            if charge:
                segment = f"{direction} charge={charge}"
            parts.append(segment)

    return " ".join(parts) if parts else "message"


def _knowledge_time_label(item: dict[str, object]) -> str:
    """Best-available time label for a knowledge item (memory / observation / distillation).

    Precedence (memory):  last_referenced_at > created_at
    Precedence (observation): last_reinforced_at > created_at
    Precedence (distillation): updated_at
    """
    source_type = str(item.get("source_type", ""))

    if source_type == "memory":
        t = _time_label(item, "last_referenced_at") or _time_label(item, "created_at")
    elif source_type == "observation":
        t = _time_label(item, "last_reinforced_at") or _time_label(item, "created_at")
    elif source_type == "distillation":
        t = _time_label(item, "updated_at")
    else:
        t = _time_label(item, "created_at") or _time_label(item, "updated_at")

    return t or "unknown"


def _relevant_prior_sort_key(item: dict[str, object]) -> tuple[int, str, str]:
    """Sort key for relevant_prior items (future-proofing for M4 mixed-type prior).

    Orders: dedicated types first in memory→observation→distillation order,
    then message items, then by timestamp descending, then by id for ties.

    **Not applied in M3.**  M3 renders message-only prior most-recent-first
    via ``reversed(hc.relevant_prior)`` (SD3).
    """
    source_type = str(item.get("source_type", ""))
    TYPE_ORDER: dict[str, int] = {"memory": 0, "observation": 1, "distillation": 2}
    type_rank = TYPE_ORDER.get(source_type, 3)

    # Timestamp: prefer sent_at (messages), fall back to created_at / updated_at
    ts = (
        _iso(item.get("sent_at"))
        or _iso(item.get("created_at"))
        or _iso(item.get("updated_at"))
        or ""
    )

    item_id = str(item.get("id", ""))
    return (type_rank, ts, item_id)


def _render_relevant_prior_line(
    item: dict[str, object], clip_limit: int, name_map: dict[str, str]
) -> str:
    """Render a single **relevant_prior** item line.

    Uses the shared ``_source_handle`` for source + retrieval metadata
    and existing content / media / queue-outcome helpers.
    """
    time_str = _time_label(item, "sent_at") or str(item.get("sent_at", "unknown"))
    item_id = _clip_id(item.get("id", ""), clip_limit)
    handle = _source_handle(item, name_map)
    content = _clip(_history_content(item), clip_limit)
    media = _media_label(item)
    outcome = _queue_outcome_label(item)

    return (
        f"- {time_str} id={item_id} {handle}"
        f"{media}: {content}"
        f"{outcome}"
    )


def _render_memory_line(
    item: dict[str, object], clip_limit: int, name_map: dict[str, str]
) -> str:
    """Render a single memory line (shared vocabulary with prior sections)."""
    # Ensure source_type is set so shared helpers (_knowledge_time_label,
    # _source_handle) pick the correct precedence / label even when the
    # upstream item dict omits the field (e.g. test fixtures).
    item = {**item, "source_type": "memory"}
    time_str = _knowledge_time_label(item)
    item_id = _clip_id(item.get("id", ""), clip_limit)
    handle = _source_handle(item, name_map)
    about = _user_label(item.get("about_user_id"), name_map)
    content = _clip(str(item.get("content", "")), clip_limit)
    return f"- id={item_id} time={time_str} {handle} about={about}: {content}"


def _render_observation_line(
    item: dict[str, object], clip_limit: int, name_map: dict[str, str]
) -> str:
    """Render a single observation line (shared vocabulary with prior sections)."""
    # Ensure source_type is set so shared helpers (_knowledge_time_label,
    # _source_handle) pick the correct precedence / label even when the
    # upstream item dict omits the field (e.g. test fixtures).
    item = {**item, "source_type": "observation"}
    time_str = _knowledge_time_label(item)
    item_id = _clip_id(item.get("id", ""), clip_limit)
    handle = _source_handle(item, name_map)
    sig = str(item.get("significance", ""))
    confidence = str(item.get("confidence", ""))
    about = _user_label(item.get("about_user_id"), name_map)
    content = _clip(str(item.get("content", "")), clip_limit)
    return (
        f"- id={item_id} time={time_str} {handle} sig={sig}"
        f" confidence={confidence} about={about}: {content}"
    )


def _render_distillation_line(
    item: dict[str, object], clip_limit: int, name_map: dict[str, str]
) -> str:
    """Render a single distillation line (shared vocabulary with prior sections)."""
    # Ensure source_type is set so shared helpers (_knowledge_time_label,
    # _source_handle) pick the correct precedence / label even when the
    # upstream item dict omits the field (e.g. test fixtures).
    item = {**item, "source_type": "distillation"}
    time_str = _knowledge_time_label(item)
    item_id = _clip_id(item.get("id", ""), clip_limit)
    handle = _source_handle(item, name_map)
    display = item.get("display", "")
    confidence = item.get("confidence", "")
    sensitivity = item.get("sensitivity", "")
    visibility = item.get("visibility", "")
    sources = _clip(
        ", ".join(str(s) for s in item.get("source_user_ids", [])), clip_limit
    )
    content = _clip(str(item.get("content", "")), clip_limit)
    return (
        f"- id={item_id} time={time_str} {handle} display={display}"
        f" confidence={confidence} sensitivity={sensitivity}"
        f" visibility={visibility} sources={sources}: {content}"
    )


def _render_with_counts(
    hc: HotContext, truncations: dict[str, int], clip_limit: int = 240
) -> str:
    lines: list[str] = []
    current_partner_share = _profile_partner_share(hc.current_user)
    partner_partner_share = _profile_partner_share(hc.partner_user)
    lines += [
        "## You",
        f"- id: {_clip(hc.current_user['id'], clip_limit)}",
        f"- name: {_clip(hc.current_user['name'], clip_limit)}",
        f"- timezone: {_clip(hc.current_user['timezone'], clip_limit)}",
        f"- onboarding_state: {_clip(hc.current_user.get('onboarding_state', 'pending'), clip_limit)}",
        f"- partner_share: {_clip(current_partner_share or 'unset', clip_limit)}",
        f"- partner_sharing_state: {_clip(hc.current_user.get('partner_sharing_state', 'unavailable'), clip_limit)}",
        f"- style_notes: {_clip(hc.current_user.get('style_notes', ''), clip_limit)}",
    ]
    open_asks = render_open_asks(
        _get_bot_asks(hc.bot_id),
        {
            "pregnancy_edd": hc.current_user.get("pregnancy_edd"),
            "partner_share": current_partner_share,
            "has_partner": bool(hc.partner_user),
            "partner_name": hc.partner_user.get("name") if hc.partner_user else None,
        },
    )
    if open_asks:
        lines += ["", open_asks]
    lines += [
        "",
        "## Your Partner",
        f"- id: {_clip(hc.partner_user['id'], clip_limit)}",
        f"- name: {_clip(hc.partner_user['name'], clip_limit)}",
        f"- timezone: {_clip(hc.partner_user['timezone'], clip_limit)}",
        f"- onboarding_state: {_clip(hc.partner_user.get('onboarding_state', 'pending'), clip_limit)}",
        f"- partner_share: {_clip(partner_partner_share or 'unset', clip_limit)}",
        f"- partner_sharing_state: {_clip(hc.partner_user.get('partner_sharing_state', 'unavailable'), clip_limit)}",
        f"- style_notes: {_clip(hc.partner_user.get('style_notes', ''), clip_limit)}",
    ]
    partner_pregnancy = _render_partner_pregnancy_state(
        hc.partner_user, hc.partner_user.get("name", ""), clip_limit
    )
    if partner_pregnancy is not None:
        lines += [
            "",
            "## Partner state",
            partner_pregnancy,
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
    lines += [
        "",
        "## Partner sharing",
        f"- current_user: {_clip(current_partner_share or 'unset', clip_limit)}",
        f"- current_user_state: {_clip(hc.current_user.get('partner_sharing_state', 'unavailable'), clip_limit)}",
        f"- partner: {_clip(partner_partner_share or 'unset', clip_limit)}",
        f"- partner_state: {_clip(hc.partner_user.get('partner_sharing_state', 'unavailable'), clip_limit)}",
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
    if hc.pending_live_conversations:
        lines += ["", "## Pending live conversations"]
        for item in hc.pending_live_conversations:
            status = item.get("status") or ""
            title = item.get("title") or "Untitled"
            conv_id = item.get("id") or ""
            line = f"- [{status}] {_clip(title, clip_limit)} (id={conv_id})"
            lines.append(line)
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
    if hc.cross_topic_status:
        lines += ["", "## Cross-topic status (injected)"]
        for item in hc.cross_topic_status:
            lines.append(f"- headline: {_clip(item.get('headline'), clip_limit)}")
            body_text = item.get("body") or ""
            if body_text:
                lines.append(f"  body: {_clip(body_text, clip_limit)}")
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
    name_map = _hot_context_name_map(hc)
    lines += ["", "## Memories"]
    lines.extend(
        _render_memory_line(item, clip_limit, name_map)
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
        _render_observation_line(item, clip_limit, name_map)
        for item in hc.observations
    )
    if truncations.get("observations"):
        lines.append(f"- [truncated, {truncations['observations']} more]")
    lines += ["", "## Distillations"]
    if hc.distillations:
        lines.extend(
            _render_distillation_line(item, clip_limit, name_map)
            for item in hc.distillations
        )
        lines.append(
            "- use get_distillations before adding or revising synthesized explanations."
        )
    else:
        lines.append("- none")
    if truncations.get("distillations"):
        lines.append(f"- [truncated, {truncations['distillations']} more]")
    lines += ["", "## Partner shareable summaries"]
    if hc.partner_shareable_summaries:
        lines.extend(
            f"- {_clip(item['provenance'], clip_limit)} {item['kind']} id={_clip_id(item['id'], clip_limit)} time={_clip(_time_label(item, 'occurred_at') or item.get('occurred_at') or 'unknown', clip_limit)}: {_clip(item['shareable_summary'], clip_limit)}"
            for item in hc.partner_shareable_summaries
        )
    else:
        lines.append("- none")
    if truncations.get("partner_shareable_summaries"):
        lines.append(
            f"- [truncated, {truncations['partner_shareable_summaries']} more]"
        )
    lines += ["", "## Bridge candidates"]
    if hc.bridge_candidates:
        lines.extend(
            f"- id={_clip_id(item['id'], clip_limit)} kind={item['kind']} status={item['status']} sensitivity={item['sensitivity']} partner_path={item['partner_path']} source={_clip_id(item['source_user_id'], clip_limit)}: {_clip(item['shareable_summary'], clip_limit)}"
            for item in hc.bridge_candidates
        )
        lines.append(
            "- use list_bridge_candidates for sent, addressed, or non-message_partner bridge candidates."
        )
    else:
        lines.append("- none")
    lines += ["", "## Recent messages"]
    if truncations.get("recent_messages"):
        lines.append(f"- [truncated, {truncations['recent_messages']} older]")
    lines.extend(
        f"- {_time_label(item, 'sent_at') or item['sent_at']} {item['direction']} charge={item['charge']} sender={item['sender_id']} recipient={item['recipient_id']}{_message_content(item, clip_limit)}{_queue_outcome_label(item)}"
        for item in hc.recent_messages
    )
    if hc.relevant_prior:
        lines += ["", "## Previous on this topic"]
        if truncations.get("relevant_prior"):
            lines.append(
                f"- +{truncations['relevant_prior']} more \u2014 search() for older relevant context"
            )
        lines.append(
            "- Use ids below as cursor anchors with read tools to explore further back."
        )
        for item in reversed(hc.relevant_prior):
            lines.append(_render_relevant_prior_line(item, clip_limit, name_map))
    # Silent agent turns since the user's last message — work the agent
    # already did that produced no outbound message. The message timeline
    # cannot show these; this section is the only record.
    lines += ["", "## Your silent turns since the user's last message"]
    if hc.silent_turns:
        for turn in hc.silent_turns:
            started_label = _time_label(turn, "started_at") or turn.get("started_at")
            reasoning_text = (turn.get("reasoning") or "").strip()
            first_line = reasoning_text.splitlines()[0] if reasoning_text else ""
            reasoning = _clip(first_line, clip_limit)
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
    # Suppress raw context dump for partner_nudge so the audit-only
    # `reason` field never leaks into the rendered prompt (invariant 4).
    # The curated `## Incoming nudge from your partner` block below
    # replaces it.
    if trigger_context is not None and not is_partner_nudge:
        lines.append(f"- context: {_clip(trigger_context, clip_limit)}")
    lines.extend(
        f"- trigger_message id={msg['id']} charge={msg['charge']} sent_at={_time_label(msg, 'sent_at') or msg['sent_at']}{_message_content(msg, clip_limit)}"
        for msg in hc.trigger_metadata["messages"]
    )
    if is_partner_nudge:
        originator_name = (
            (hc.partner_user or {}).get("name")
            or trigger_context.get("originating_user_name")
            or "your partner"
        )
        nudge_note = trigger_context.get("nudge_note")
        if not nudge_note:
            nudge_note = f"{originator_name} asked me to check in with you"
        scheduled_for_iso = trigger_context.get("scheduled_for")
        lines += [
            "",
            "## Incoming nudge from your partner",
            f"- from: {_clip(originator_name, clip_limit)}",
            f"- note: {_clip(nudge_note, clip_limit)}",
        ]
        if scheduled_for_iso:
            lines.append(f"- scheduled_for: {_clip(scheduled_for_iso, clip_limit)}")
    return "\n".join(lines).strip()


def _estimated_tokens(text: str) -> int:
    return len(text) // 4


def render_hot_context(hc: HotContext) -> str:
    budget = get_settings().hot_context_token_budget
    working = HotContext(
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
        bridge_candidates=list(hc.bridge_candidates),
        recent_reactions=list(hc.recent_reactions),
        silent_turns=list(hc.silent_turns),
        recent_messages=list(hc.recent_messages),
        relevant_prior=list(hc.relevant_prior),
        partner_shareable_summaries=list(hc.partner_shareable_summaries),
        topic_status=hc.topic_status,
        cross_topic_peek=list(hc.cross_topic_peek),
        cross_topic_status=list(hc.cross_topic_status),
        upcoming_items=list(hc.upcoming_items),
        pending_live_conversations=list(hc.pending_live_conversations),
        time_since_last_message=hc.time_since_last_message,
        trigger_metadata=hc.trigger_metadata,
        bot_id=hc.bot_id,
    )
    truncations = {
        "distillations": 0,
        "observations": 0,
        "memories": 0,
        "relevant_prior": 0,
        "partner_shareable_summaries": 0,
        "recent_messages": 0,
        "conversation_load": 0,
    }
    clip_limit = HOT_CONTEXT_LINE_CLIP_LIMIT
    text = _render_with_counts(working, truncations, clip_limit)
    for name in (
        "distillations",
        "partner_shareable_summaries",
        "observations",
        "memories",
        "relevant_prior",
        "recent_messages",
    ):
        items = getattr(working, name)
        while _estimated_tokens(text) > budget and items:
            # recent_messages and relevant_prior are ordered oldest -> newest,
            # so drop from the front to evict stale backlog and keep the most
            # recent turns (the immediate context the model needs). The other
            # lists are ranked with the least valuable entries last, so they
            # pop from the tail.
            items.pop(0 if name in ("recent_messages", "relevant_prior") else -1)
            truncations[name] += 1
            text = _render_with_counts(working, truncations, clip_limit)
    for clip_limit in (160, 100, 60, 30):
        if _estimated_tokens(text) <= budget:
            break
        text = _render_with_counts(working, truncations, clip_limit)
    for name in ("open_watch_items", "active_themes"):
        items = getattr(working, name)
        while _estimated_tokens(text) > budget and items:
            items.pop()
            text = _render_with_counts(working, truncations, clip_limit)
    for name in ("pending_live_conversations",):
        items = getattr(working, name)
        while _estimated_tokens(text) > budget and items:
            items.pop()
            text = _render_with_counts(working, truncations, clip_limit)
    if _estimated_tokens(text) > budget and not truncations["conversation_load"]:
        truncations["conversation_load"] = 1
        text = _render_with_counts(working, truncations, clip_limit)
    return text
