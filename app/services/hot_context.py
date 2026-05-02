"""Hot context construction for the agentic loop."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.config import get_settings
from app.models.user import User
from app.services.cross_thread_privacy import (
    bridge_candidate_visible_to_target,
    normalize_sharing_default,
    raw_message_visibility,
)
from app.services.text_safety import clean_user_facing_text, looks_like_internal_process_text
from app.services.tools.common import media_analysis_text


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
    bridge_candidates: list[dict[str, Any]] = field(default_factory=list)


def _row_dict(row: Any) -> dict[str, Any]:
    return dict(row)


def _clean_list(value: Any) -> list[Any]:
    return list(value or [])


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None and hasattr(value, "isoformat") else None


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


def _history_content(item: dict[str, Any], clip_limit: int) -> str:
    if item.get("raw_content_hidden"):
        return "[raw partner content hidden by sharing_default]"
    content = item.get("content") or media_analysis_text(item)
    if item.get("direction") == "outbound":
        raw_content = str(content or "")
        cleaned = clean_user_facing_text(raw_content)
        content = cleaned if cleaned or looks_like_internal_process_text(raw_content) else content
    return _clip(content, clip_limit)


def _clip_id(value: Any, clip_limit: int) -> str:
    return _clip(value, 14 if clip_limit < 60 else clip_limit)


async def _user_profile(pool: Any, user: User) -> dict[str, Any]:
    row = await pool.fetchrow(
        """
        SELECT id, name, phone, timezone, COALESCE(style_notes, '') AS style_notes,
               COALESCE(onboarding_state, 'pending') AS onboarding_state,
               cross_thread_sharing_default
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
            "cross_thread_sharing_default": user.cross_thread_sharing_default,
        }
    return _row_dict(row)


async def build_hot_context(
    pool: Any,
    user: User,
    partner: User,
    triggering_message_ids: list[UUID],
    trigger_metadata: dict[str, Any] | None = None,
) -> HotContext:
    current_user = await _user_profile(pool, user)
    partner_user = await _user_profile(pool, partner)
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
        GROUP BY bounds.period_start, bounds.period_end
        """,
        user.id,
        current_user.get("timezone") or user.timezone,
    )
    conversation_load = {
        "period": "today",
        "timezone": current_user.get("timezone") or user.timezone,
        "period_start": _iso(conversation_load_row["period_start"]) if conversation_load_row else None,
        "period_end": _iso(conversation_load_row["period_end"]) if conversation_load_row else None,
        "inbound_count": int(conversation_load_row["inbound_count"] or 0) if conversation_load_row else 0,
        "outbound_count": int(conversation_load_row["outbound_count"] or 0) if conversation_load_row else 0,
        "total_count": int(conversation_load_row["total_count"] or 0) if conversation_load_row else 0,
    }
    active_oob = [
        {
            "id": row["id"],
            "owner_id": row["owner_id"],
            "severity": row["severity"],
            "shareable_context": row["shareable_context"],
            "protected_summary": row["shareable_context"] or "[protected]",
            "review_at": _iso(row["review_at"]),
        }
        for row in await pool.fetch(
            """
            SELECT id, owner_id, shareable_context, severity, review_at
            FROM out_of_bounds
            WHERE status = 'active' AND owner_id = ANY($1::uuid[])
            ORDER BY CASE severity WHEN 'hard' THEN 1 WHEN 'firm' THEN 2 ELSE 3 END, created_at DESC
            """,
            [user.id, partner.id],
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
        }
        for row in await pool.fetch(
            """
            SELECT id, about_user_id, content, COALESCE(related_theme_ids, '{}'::uuid[]) AS related_theme_ids,
                   last_referenced_at, created_at
            FROM memories
            WHERE status = 'active' AND (about_user_id = ANY($1::uuid[]) OR about_user_id IS NULL)
            ORDER BY COALESCE(last_referenced_at, created_at) DESC
            LIMIT 80
            """,
            [user.id, partner.id],
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
        }
        for row in await pool.fetch(
            """
            SELECT id, title, description, status, sentiment, health, last_reinforced_at, last_active_at
            FROM themes
            WHERE status = 'active'
            ORDER BY COALESCE(last_reinforced_at, first_seen_at) DESC
            LIMIT 10
            """
        )
    ]
    open_watch_items = [
        {
            "id": row["id"],
            "owner_user_id": row["owner_user_id"],
            "content": row["content"],
            "due_at": _iso(row["due_at"]),
            "related_theme_ids": _clean_list(row["related_theme_ids"]),
        }
        for row in await pool.fetch(
            """
            SELECT id, owner_user_id, content, due_at, COALESCE(related_theme_ids, '{}'::uuid[]) AS related_theme_ids
            FROM watch_items
            WHERE status = 'open' AND owner_user_id = $1
            ORDER BY COALESCE(due_at, created_at) ASC
            """,
            user.id,
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
        }
        for row in await pool.fetch(
            """
            SELECT id, about_user_id, content, confidence, significance,
                   COALESCE(related_theme_ids, '{}'::uuid[]) AS related_theme_ids,
                   last_reinforced_at, created_at
            FROM observations
            WHERE status = 'active' AND significance >= 3
            ORDER BY recency_weighted_score(significance, last_reinforced_at, created_at) DESC NULLS LAST,
                     COALESCE(last_reinforced_at, created_at) DESC
            LIMIT 80
            """
        )
    ]
    message_rows = await pool.fetch(
        """
        SELECT id, direction, sender_id, recipient_id, content, media_type, media_analysis,
               sent_at, COALESCE(charge, 'routine') AS charge
        FROM messages
        WHERE deleted_at IS NULL
          AND (sender_id = ANY($1::uuid[]) OR recipient_id = ANY($1::uuid[]))
        ORDER BY sent_at DESC
        LIMIT 20
        """,
        [user.id, partner.id],
    )
    sharing_defaults = {
        user.id: normalize_sharing_default(current_user.get("cross_thread_sharing_default")),
        partner.id: normalize_sharing_default(partner_user.get("cross_thread_sharing_default")),
    }
    recent_messages = [
        {
            "id": row["id"],
            "direction": row["direction"],
            "sender_id": row["sender_id"],
            "recipient_id": row["recipient_id"],
            "content": row["content"] if raw_message_visibility(
                viewer_user_id=user.id,
                thread_owner_user_id=_message_thread_owner_id(row),
                thread_owner_sharing_default=sharing_defaults.get(_message_thread_owner_id(row)),
            ).visible else None,
            "media_type": row["media_type"] if "media_type" in row else None,
            "media_analysis": row["media_analysis"] if "media_analysis" in row else None,
            "raw_content_hidden": not raw_message_visibility(
                viewer_user_id=user.id,
                thread_owner_user_id=_message_thread_owner_id(row),
                thread_owner_sharing_default=sharing_defaults.get(_message_thread_owner_id(row)),
            ).visible,
            "sent_at": _iso(row["sent_at"]),
            "charge": row["charge"],
        }
        for row in reversed(message_rows)
        if _message_thread_owner_id(row) in sharing_defaults
    ]
    bridge_candidate_rows = await pool.fetch(
        """
        SELECT id, source_user_id, target_user_id, kind, status, sensitivity,
               shareable_summary, created_at
        FROM bridge_candidates
        WHERE target_user_id=$1
          AND source_user_id=$2
          AND status IN ('ready', 'sent', 'addressed')
        ORDER BY created_at DESC
        LIMIT 3
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
            "shareable_summary": row["shareable_summary"],
        }
        for row in bridge_candidate_rows
        if bridge_candidate_visible_to_target(row, target_user_id=user.id)
    ]
    latest_sent_at = max((row["sent_at"] for row in message_rows), default=None)
    trigger_rows = await pool.fetch(
        """
        SELECT id, direction, sender_id, recipient_id, COALESCE(charge, 'routine') AS charge,
               sent_at, content, media_type, media_analysis
        FROM messages
        WHERE id = ANY($1::uuid[])
        ORDER BY sent_at ASC
        """,
        triggering_message_ids,
    )
    return HotContext(
        current_user=current_user,
        partner_user=partner_user,
        conversation_load=conversation_load,
        active_oob=active_oob,
        memories=memories,
        active_themes=active_themes,
        open_watch_items=open_watch_items,
        observations=observations,
        bridge_candidates=bridge_candidates,
        recent_messages=recent_messages,
        time_since_last_message=_duration_since(latest_sent_at),
        trigger_metadata={
            **(trigger_metadata or {}),
            "triggering_message_ids": triggering_message_ids,
            "messages": [
                {
                    "id": row["id"],
                    "charge": row["charge"],
                    "sent_at": _iso(row["sent_at"]),
                    "content": row["content"]
                    if "content" in row
                    and raw_message_visibility(
                        viewer_user_id=user.id,
                        thread_owner_user_id=_message_thread_owner_id(row),
                        thread_owner_sharing_default=sharing_defaults.get(_message_thread_owner_id(row)),
                    ).visible
                    else None,
                    "media_type": row["media_type"] if "media_type" in row else None,
                    "media_analysis": row["media_analysis"] if "media_analysis" in row else None,
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


def _render_with_counts(hc: HotContext, truncations: dict[str, int], clip_limit: int = 240) -> str:
    lines: list[str] = [
        "## You",
        f"- id: {_clip(hc.current_user['id'], clip_limit)}",
        f"- name: {_clip(hc.current_user['name'], clip_limit)}",
        f"- timezone: {_clip(hc.current_user['timezone'], clip_limit)}",
        f"- onboarding_state: {_clip(hc.current_user.get('onboarding_state', 'pending'), clip_limit)}",
        f"- sharing_default: {_clip(hc.current_user.get('cross_thread_sharing_default') or 'unset', clip_limit)}",
        f"- style_notes: {_clip(hc.current_user.get('style_notes', ''), clip_limit)}",
        "",
        "## Your Partner",
        f"- id: {_clip(hc.partner_user['id'], clip_limit)}",
        f"- name: {_clip(hc.partner_user['name'], clip_limit)}",
        f"- timezone: {_clip(hc.partner_user['timezone'], clip_limit)}",
        f"- onboarding_state: {_clip(hc.partner_user.get('onboarding_state', 'pending'), clip_limit)}",
        f"- sharing_default: {_clip(hc.partner_user.get('cross_thread_sharing_default') or 'unset', clip_limit)}",
        f"- style_notes: {_clip(hc.partner_user.get('style_notes', ''), clip_limit)}",
    ]
    lines += [
        "",
        "## Sharing defaults",
        f"- current_user: {_clip(hc.current_user.get('cross_thread_sharing_default') or 'unset', clip_limit)}",
        f"- partner: {_clip(hc.partner_user.get('cross_thread_sharing_default') or 'unset', clip_limit)}",
    ]
    if not hc.current_user.get("cross_thread_sharing_default"):
        lines.append(
            "- action_needed: Ask the current user to choose opt_in or opt_out for cross-thread sharing when there is a natural opening."
        )
    if not truncations.get("conversation_load"):
        lines += [
            "",
            "## Conversation load",
            f"- period: {_clip(hc.conversation_load.get('period', 'today'), clip_limit)}",
            f"- timezone: {_clip(hc.conversation_load.get('timezone'), clip_limit)}",
            f"- total_messages: {_clip(hc.conversation_load.get('total_count', 0), clip_limit)}",
            f"- inbound_messages: {_clip(hc.conversation_load.get('inbound_count', 0), clip_limit)}",
            f"- outbound_messages: {_clip(hc.conversation_load.get('outbound_count', 0), clip_limit)}",
        ]
    lines += [
        "",
        "## Active OOB (severity)",
    ]
    if hc.active_oob:
        for item in hc.active_oob:
            lines.append(
                f"- id={_clip_id(item['id'], clip_limit)} {item['severity']} owner={_clip_id(item['owner_id'], clip_limit)} context={_clip(item.get('protected_summary') or item.get('shareable_context') or '[protected]', clip_limit)}"
            )
    else:
        lines.append("- none")
    lines += ["", "## Active themes"]
    lines.extend(
        f"- id={_clip_id(theme['id'], clip_limit)} {_clip(theme['title'], clip_limit)} ({theme['status']}, {theme['sentiment']}, {theme['health']}): {_clip(theme['description'], clip_limit)}"
        for theme in hc.active_themes
    )
    lines += ["", "## Memories"]
    lines.extend(f"- id={_clip_id(item['id'], clip_limit)} about={_clip_id(item['about_user_id'], clip_limit)}: {_clip(item['content'], clip_limit)}" for item in hc.memories)
    if truncations.get("memories"):
        lines.append(f"- [truncated, {truncations['memories']} more]")
    lines += ["", "## Open watch items"]
    lines.extend(f"- id={_clip_id(item['id'], clip_limit)} due={item['due_at']} {_clip(item['content'], clip_limit)}" for item in hc.open_watch_items)
    lines += ["", "## High-significance observations"]
    lines.extend(
        f"- id={_clip_id(item['id'], clip_limit)} sig={item['significance']} confidence={item['confidence']} about={_clip_id(item['about_user_id'], clip_limit)}: {_clip(item['content'], clip_limit)}"
        for item in hc.observations
    )
    if truncations.get("observations"):
        lines.append(f"- [truncated, {truncations['observations']} more]")
    lines += ["", "## Bridge candidates"]
    if hc.bridge_candidates:
        lines.extend(
            f"- id={_clip_id(item['id'], clip_limit)} kind={item['kind']} status={item['status']} sensitivity={item['sensitivity']} source={_clip_id(item['source_user_id'], clip_limit)}: {_clip(item['shareable_summary'], clip_limit)}"
            for item in hc.bridge_candidates
        )
        lines.append("- use list_bridge_candidates for older or filtered bridge candidates.")
    else:
        lines.append("- none")
    lines += ["", "## Recent messages"]
    lines.extend(
        f"- {item['sent_at']} {item['direction']} charge={item['charge']} sender={item['sender_id']} recipient={item['recipient_id']}: {_history_content(item, clip_limit)}"
        for item in hc.recent_messages
    )
    if truncations.get("recent_messages"):
        lines.append(f"- [truncated, {truncations['recent_messages']} more]")
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
        f"- trigger_message id={msg['id']} charge={msg['charge']} sent_at={msg['sent_at']}"
        for msg in hc.trigger_metadata["messages"]
    )
    return "\n".join(lines).strip()


def _estimated_tokens(text: str) -> int:
    return len(text) // 4


def render_hot_context(hc: HotContext) -> str:
    budget = get_settings().hot_context_token_budget
    working = HotContext(
        current_user=hc.current_user,
        partner_user=hc.partner_user,
        conversation_load=hc.conversation_load,
        active_oob=hc.active_oob,
        memories=list(hc.memories),
        active_themes=hc.active_themes,
        open_watch_items=hc.open_watch_items,
        observations=list(hc.observations),
        bridge_candidates=list(hc.bridge_candidates),
        recent_messages=list(hc.recent_messages),
        time_since_last_message=hc.time_since_last_message,
        trigger_metadata=hc.trigger_metadata,
    )
    truncations = {"observations": 0, "memories": 0, "recent_messages": 0, "conversation_load": 0}
    clip_limit = 240
    text = _render_with_counts(working, truncations, clip_limit)
    for name in ("observations", "memories", "recent_messages"):
        items = getattr(working, name)
        while _estimated_tokens(text) > budget and items:
            items.pop()
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
    if _estimated_tokens(text) > budget and not truncations["conversation_load"]:
        truncations["conversation_load"] = 1
        text = _render_with_counts(working, truncations, clip_limit)
    return text
