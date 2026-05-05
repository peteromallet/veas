"""Concrete scheduled job handlers for Plan 5."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, time, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import Settings, get_settings
from app.models.user import User, fetch_user_by_id
from app.services.agentic import run_agentic_job, run_agentic_turn
from app.services.checkins import schedule_checkin_record
from app.services.decay import run_decay_housekeeping
from app.services.deletion import purge_expired_deletions
from app.services.messaging import send_outbound
from app.services.scheduled_task_recurrence import next_occurrence_utc, recurrence_after_fire
from app.services.templates import TemplateCall

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _zoneinfo(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        logger.warning("unknown user timezone %s; falling back to UTC", name)
        return ZoneInfo("UTC")


def _time_value(value: Any) -> time:
    if isinstance(value, time):
        return value
    if isinstance(value, str):
        hour, minute, *_ = value.split(":")
        return time(int(hour), int(minute))
    return time(9, 0)


def next_weekly_summary_at(user_row: dict[str, Any], *, now: datetime | None = None) -> datetime:
    now = now or _utc_now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    zone = _zoneinfo(user_row.get("timezone") or "UTC")
    local_now = now.astimezone(zone)
    target_day = int(user_row.get("weekly_summary_day", 1))
    current_day = (local_now.weekday() + 1) % 7
    days_ahead = (target_day - current_day) % 7
    target_time = _time_value(user_row.get("weekly_summary_time", "09:00"))
    candidate_date = local_now.date() + timedelta(days=days_ahead)
    candidate = datetime.combine(candidate_date, target_time, zone)
    if candidate <= local_now:
        candidate += timedelta(days=7)
    return candidate.astimezone(UTC)


class ScheduledJobHandlers:
    def __init__(self, pool: Any, *, settings: Settings | None = None) -> None:
        self.pool = pool
        self.settings = settings or get_settings()

    def as_dict(self) -> dict[str, Any]:
        return {
            "weekly_summary": self.handle_weekly_summary,
            "checkin": self.handle_checkin,
            "watch_item_due": self.handle_watch_item_due,
            "oob_review": self.handle_oob_review,
            "heartbeat": self.handle_heartbeat,
            "deferred_turn": self.handle_deferred_turn,
            "scheduled_task": self.handle_scheduled_task,
        }

    async def handle_weekly_summary(self, job: dict[str, Any]) -> None:
        user_row = await self._fetch_user_schedule(job["user_id"])
        user = _user_from_row(user_row)
        summary = await self._weekly_summary_counts(user.id)
        content = (
            f"Hi {user.name}, this week we had {summary['conversation_count']} conversations "
            f"and touched on {summary['ongoing_count']} ongoing things. Want to talk through anything? Just ask."
        )
        await send_outbound(
            self.pool,
            user,
            content,
            template_fallback=TemplateCall(
                "weekly_summary",
                [user.name, str(summary["conversation_count"]), str(summary["ongoing_count"])],
            ),
        )
        await run_decay_housekeeping(self.pool)
        await schedule_next_weekly_summary(self.pool, user_row, now=_utc_now(), source_job_id=job["id"])

    async def handle_checkin(self, job: dict[str, Any]) -> None:
        user = await fetch_user_by_id(self.pool, job["user_id"])
        context = job.get("context") or {}
        metadata = {"kind": "checkin", "context": {**context, "delayed": bool(job.get("delayed"))}}
        if await _can_send_freeform(self.pool, user, self.settings):
            await run_agentic_job(user, metadata)
            return
        await send_outbound(
            self.pool,
            user,
            f"Hi {user.name}, been a bit -- anything on your mind? Just message me back when you're ready.",
            template_fallback=TemplateCall("checkin_nudge", [user.name]),
        )

    async def handle_watch_item_due(self, job: dict[str, Any]) -> None:
        user = await fetch_user_by_id(self.pool, job["user_id"])
        context = job.get("context") or {}
        watch_item = await self._fetch_watch_item(context.get("watch_item_id"))
        metadata = {
            "kind": "watch_item_due",
            "context": {
                **context,
                "watch_item": watch_item,
                "delayed": bool(job.get("delayed")),
            },
        }
        if await _can_send_freeform(self.pool, user, self.settings):
            await run_agentic_job(user, metadata)
            return
        await schedule_checkin_job(
            self.pool,
            user.id,
            scheduled_for=_utc_now() + timedelta(minutes=15),
            context={"kind": "watch_item_due", **metadata["context"]},
        )

    async def handle_oob_review(self, job: dict[str, Any]) -> None:
        user = await fetch_user_by_id(self.pool, job["user_id"])
        context = job.get("context") or {}
        await run_agentic_job(
            user,
            {
                "kind": "oob_review",
                "context": {**context, "delayed": bool(job.get("delayed"))},
            },
        )

    async def handle_heartbeat(self, job: dict[str, Any]) -> None:
        logger.info("scheduled heartbeat fired job_id=%s scheduled_for=%s", job["id"], job["scheduled_for"])
        await purge_expired_deletions(self.pool)

    async def handle_deferred_turn(self, job: dict[str, Any]) -> None:
        user = await fetch_user_by_id(self.pool, job["user_id"])
        context = job.get("context") or {}
        message_ids = [UUID(value) for value in context.get("triggering_message_ids", [])]
        if message_ids:
            await self.pool.execute(
                "UPDATE messages SET processing_state='raw' WHERE id = ANY($1)",
                message_ids,
            )
            await run_agentic_turn(message_ids, user)

    async def handle_scheduled_task(self, job: dict[str, Any]) -> None:
        user = await fetch_user_by_id(self.pool, job["user_id"])
        context = dict(job.get("context") or {})
        await run_agentic_job(
            user,
            {
                "kind": "scheduled_task",
                "context": _scheduled_task_trigger_context(job, context),
            },
        )

        current = await self.pool.fetchrow(
            """
            SELECT id, user_id, scheduled_for, context, status
            FROM scheduled_jobs
            WHERE id = $1
            """,
            job["id"],
        )
        if current is None:
            return
        current_context = dict(current.get("context") or {})
        control = current_context.get("scheduled_task_control") or {}
        if current.get("status") != "pending" or control.get("cancel_after_current_fire"):
            return

        recurrence = current_context.get("recurrence")
        next_scheduled_for = next_occurrence_utc(current["scheduled_for"], recurrence)
        next_recurrence = recurrence_after_fire(recurrence)
        if next_scheduled_for is None or next_recurrence is None:
            return

        next_context = {
            **current_context,
            "recurrence": next_recurrence,
            "source_job_id": str(job["id"]),
        }
        next_context.pop("scheduled_task_control", None)
        await self.pool.fetchrow(
            """
            INSERT INTO scheduled_jobs (user_id, job_type, scheduled_for, context, status)
            SELECT $1, 'scheduled_task', $2, $3::jsonb, 'pending'
            WHERE NOT EXISTS (
                SELECT 1
                FROM scheduled_jobs
                WHERE job_type = 'scheduled_task'
                  AND status = 'pending'
                  AND context->>'source_job_id' = $4
            )
            RETURNING id, scheduled_for
            """,
            current["user_id"],
            next_scheduled_for,
            next_context,
            str(job["id"]),
        )

    async def _fetch_user_schedule(self, user_id: UUID) -> dict[str, Any]:
        row = await self.pool.fetchrow(
            """
            SELECT id, name, phone, timezone, weekly_summary_enabled,
                   weekly_summary_day, weekly_summary_time
            FROM users
            WHERE id = $1
            """,
            user_id,
        )
        if row is None:
            raise ValueError(f"user not found for scheduled job: {user_id}")
        return dict(row)

    async def _weekly_summary_counts(self, user_id: UUID) -> dict[str, int]:
        row = await self.pool.fetchrow(
            """
            SELECT
                (
                    SELECT COUNT(*)
                    FROM messages
                    WHERE deleted_at IS NULL
                      AND sent_at >= now() - interval '7 days'
                      AND (sender_id = $1 OR recipient_id = $1)
                )::int AS conversation_count,
                (
                    (SELECT COUNT(*) FROM themes WHERE status = 'active')
                    +
                    (SELECT COUNT(*) FROM watch_items WHERE owner_user_id = $1 AND status = 'open')
                )::int AS ongoing_count
            """,
            user_id,
        )
        return {
            "conversation_count": int(row["conversation_count"] or 0),
            "ongoing_count": int(row["ongoing_count"] or 0),
        }

    async def _fetch_watch_item(self, watch_item_id: Any) -> dict[str, Any] | None:
        if watch_item_id is None:
            return None
        row = await self.pool.fetchrow(
            """
            SELECT id, owner_user_id, content, due_at, status
            FROM watch_items
            WHERE id = $1
            """,
            watch_item_id,
        )
        return dict(row) if row is not None else None


def _user_from_row(row: dict[str, Any]) -> User:
    return User(id=row["id"], name=row["name"], phone=row["phone"], timezone=row["timezone"])


def _scheduled_task_trigger_context(job: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    return {
        **context,
        "job_id": str(job["id"]),
        "task_id": context.get("task_id"),
        "brief": context.get("brief"),
        "scheduled_for": job["scheduled_for"].isoformat()
        if isinstance(job.get("scheduled_for"), datetime)
        else job.get("scheduled_for"),
        "recurrence": context.get("recurrence"),
        "delayed": bool(job.get("delayed")),
    }


async def _can_send_freeform(pool: Any, user: User, settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    if settings.messaging_provider.strip().lower() == "discord":
        return True
    return await _within_whatsapp_window(pool, user)


async def _within_whatsapp_window(pool: Any, user: User) -> bool:
    last_inbound_at = await pool.fetchval(
        "SELECT MAX(sent_at) FROM messages WHERE sender_id=$1 AND direction='inbound'",
        user.id,
    )
    if last_inbound_at is None:
        return False
    if last_inbound_at.tzinfo is None:
        last_inbound_at = last_inbound_at.replace(tzinfo=UTC)
    return _utc_now() - last_inbound_at < timedelta(hours=24)


async def schedule_next_weekly_summary(
    pool: Any,
    user_row: dict[str, Any],
    *,
    now: datetime | None = None,
    source_job_id: Any | None = None,
) -> Any | None:
    if not user_row.get("weekly_summary_enabled", True):
        return None
    scheduled_for = next_weekly_summary_at(user_row, now=now)
    return await pool.fetchrow(
        """
        INSERT INTO scheduled_jobs (user_id, job_type, scheduled_for, context, status)
        SELECT $1, 'weekly_summary', $2, $3::jsonb, 'pending'
        WHERE NOT EXISTS (
            SELECT 1
            FROM scheduled_jobs
            WHERE user_id = $1
              AND job_type = 'weekly_summary'
              AND status = 'pending'
              AND ($4::uuid IS NULL OR id <> $4::uuid)
        )
        RETURNING id, scheduled_for
        """,
        user_row["id"],
        scheduled_for,
        {"source_job_id": str(source_job_id) if source_job_id is not None else None},
        source_job_id,
    )


async def seed_weekly_summaries(pool: Any, *, now: datetime | None = None) -> list[Any]:
    rows = await pool.fetch(
        """
        SELECT id, name, phone, timezone, weekly_summary_enabled,
               weekly_summary_day, weekly_summary_time
        FROM users
        WHERE weekly_summary_enabled = true
        """
    )
    inserted = []
    for row in rows:
        inserted.append(await schedule_next_weekly_summary(pool, dict(row), now=now))
    return inserted


async def schedule_checkin_job(
    pool: Any,
    user_id: UUID,
    *,
    scheduled_for: datetime,
    context: dict[str, Any],
) -> Any:
    _old, row = await schedule_checkin_record(
        pool,
        user_id,
        scheduled_for=scheduled_for,
        context=context,
    )
    return row
