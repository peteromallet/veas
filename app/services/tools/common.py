"""Shared helpers for tool implementations."""

from __future__ import annotations

from typing import Any

from tool_schemas import (
    DateRange,
    DistillationRow,
    MemoryRow,
    MessageHit,
    OOBRow,
    ObservationRow,
    ThemeSummary,
    WatchItemRow,
)
from app.services.turn_context import TurnContext


def value(row: Any, key: str, default: Any = None) -> Any:
    try:
        item = row[key]
    except (KeyError, TypeError, IndexError):
        return default
    return default if item is None else item


def list_value(row: Any, key: str) -> list[Any]:
    return list(value(row, key, []))


def add_date_range(clauses: list[str], params: list[Any], column: str, date_range: DateRange | None) -> None:
    if date_range is None:
        return
    if date_range.start is not None:
        params.append(date_range.start)
        clauses.append(f"{column} >= ${len(params)}")
    if date_range.end is not None:
        params.append(date_range.end)
        clauses.append(f"{column} <= ${len(params)}")


def media_analysis_text(row_or_analysis: Any) -> str:
    analysis = value(row_or_analysis, "media_analysis", row_or_analysis)
    if not isinstance(analysis, dict):
        return ""
    text = analysis.get("explanation") or analysis.get("description") or analysis.get("summary")
    if not text:
        return ""
    media_type = analysis.get("kind") or value(row_or_analysis, "media_type", "media")
    return f"[{media_type}] {text}"


def current_scheduled_task(ctx: TurnContext) -> dict[str, Any] | None:
    metadata = ctx.trigger_metadata or {}
    if metadata.get("kind") != "scheduled_task":
        return None
    context = metadata.get("context")
    if not isinstance(context, dict):
        return None
    job_id = context.get("job_id")
    task_id = context.get("task_id")
    if not job_id or not task_id:
        return None
    return {
        "job_id": job_id,
        "task_id": task_id,
        "brief": context.get("brief"),
        "recurrence": context.get("recurrence"),
    }


def message_hit(row: Any) -> MessageHit:
    content = value(row, "content", "") or media_analysis_text(row)
    return MessageHit(
        id=row["id"],
        sender_id=row["sender_id"],
        sent_at=row["sent_at"],
        content=content,
        charge=value(row, "charge", "routine"),
        direction=row["direction"],
    )


def theme_summary(row: Any) -> ThemeSummary:
    return ThemeSummary(
        id=row["id"],
        title=row["title"],
        status=row["status"],
        sentiment=row["sentiment"],
        health=row["health"],
        last_reinforced_at=row["last_reinforced_at"],
        last_active_at=row["last_active_at"],
    )


def memory_row(row: Any) -> MemoryRow:
    return MemoryRow(
        id=row["id"],
        about_user_id=row["about_user_id"],
        content=row["content"],
        status=row["status"],
        related_theme_ids=list_value(row, "related_theme_ids"),
        created_at=row["created_at"],
        last_referenced_at=row["last_referenced_at"],
    )


def watch_item_row(row: Any) -> WatchItemRow:
    return WatchItemRow(
        id=row["id"],
        owner_user_id=row["owner_user_id"],
        content=row["content"],
        due_at=row["due_at"],
        status=row["status"],
        addressing_note=row["addressing_note"],
        created_at=row["created_at"],
        addressed_at=row["addressed_at"],
        related_theme_ids=list_value(row, "related_theme_ids"),
    )


def observation_row(row: Any) -> ObservationRow:
    return ObservationRow(
        id=row["id"],
        content=row["content"],
        about_user_id=row["about_user_id"],
        confidence=row["confidence"],
        significance=row["significance"],
        status=row["status"],
        related_theme_ids=list_value(row, "related_theme_ids"),
        supporting_message_ids=list_value(row, "supporting_message_ids"),
        created_at=row["created_at"],
        last_reinforced_at=row["last_reinforced_at"],
        surfaced_count=value(row, "surfaced_count", 0),
    )


def distillation_row(row: Any) -> DistillationRow:
    return DistillationRow(
        id=row["id"],
        content=row["content"],
        confidence=row["confidence"],
        status=row["status"],
        sensitivity=row["sensitivity"],
        visibility=row["visibility"],
        shareable_summary=value(row, "shareable_summary"),
        source_user_ids=list_value(row, "source_user_ids"),
        related_memory_ids=list_value(row, "related_memory_ids"),
        related_observation_ids=list_value(row, "related_observation_ids"),
        related_theme_ids=list_value(row, "related_theme_ids"),
        supporting_message_ids=list_value(row, "supporting_message_ids"),
        created_from_tool_call_id=value(row, "created_from_tool_call_id"),
        triggering_message_id=value(row, "triggering_message_id"),
        supersedes_distillation_id=value(row, "supersedes_distillation_id"),
        superseded_by_distillation_id=value(row, "superseded_by_distillation_id"),
        revision_note=value(row, "revision_note"),
        revision_count=value(row, "revision_count", 0),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        revised_at=value(row, "revised_at"),
        retired_at=value(row, "retired_at"),
    )


def oob_row(row: Any) -> OOBRow:
    shareable_context = row["shareable_context"]
    return OOBRow(
        id=row["id"],
        owner_id=row["owner_id"],
        protected_summary=shareable_context or "[protected]",
        shareable_context=shareable_context,
        severity=row["severity"],
        status=row["status"],
        created_at=row["created_at"],
        review_at=row["review_at"],
    )
