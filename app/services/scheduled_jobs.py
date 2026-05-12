"""Scheduled job worker and recovery helpers."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from app.config import Settings, get_settings
from app.services.deletion import purge_expired_deletions
from app.services import system_state
from app.bots.registry import get_relationship_topic_id

logger = logging.getLogger(__name__)

JobHandler = Callable[[dict[str, Any]], Awaitable[None]]

USER_FACING_JOB_TYPES = {"weekly_summary", "checkin", "watch_item_due", "oob_review", "scheduled_task"}


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@dataclass(frozen=True)
class RunDueResult:
    claimed: int = 0
    fired: int = 0
    retried: int = 0
    cancelled: int = 0
    skipped_paused: bool = False


class ScheduledJobWorker:
    """Poll and dispatch due scheduled jobs.

    Claiming uses a single UPDATE statement fed by SELECT ... FOR UPDATE SKIP
    LOCKED, so horizontally duplicated workers cannot claim the same job.
    """

    def __init__(
        self,
        pool: Any,
        *,
        handlers: dict[str, JobHandler] | None = None,
        settings: Settings | None = None,
        worker_id: str | None = None,
    ) -> None:
        self.pool = pool
        self.settings = settings or get_settings()
        self.worker_id = worker_id or f"scheduler-{uuid4()}"
        self.handlers = {"heartbeat": self._handle_heartbeat}
        if handlers:
            self.handlers.update(handlers)

    async def run_forever(self) -> None:
        while True:
            try:
                await self.run_due_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("scheduled job worker tick failed")
            await asyncio.sleep(self.settings.scheduler_poll_interval_s)

    async def run_due_once(self, *, now: datetime | None = None) -> RunDueResult:
        now = _aware_utc(now or _utc_now())
        paused = await is_globally_paused(self.pool)
        jobs = await self._claim_due_jobs(now=now, heartbeat_only=paused)
        result = RunDueResult(claimed=len(jobs), skipped_paused=paused)
        fired = retried = cancelled = 0
        for job in jobs:
            try:
                await self._dispatch(job)
            except Exception as exc:
                logger.exception("scheduled job %s failed", job["id"])
                retrying = await self._record_failure(job, exc, now=now)
                if retrying:
                    retried += 1
                else:
                    cancelled += 1
            else:
                await self._mark_fired(job["id"], now=now)
                fired += 1
                if job["job_type"] == "heartbeat":
                    await seed_heartbeat(self.pool, settings=self.settings, now=now)
        return RunDueResult(
            claimed=result.claimed,
            fired=fired,
            retried=retried,
            cancelled=cancelled,
            skipped_paused=result.skipped_paused,
        )

    async def _claim_due_jobs(self, *, now: datetime, heartbeat_only: bool) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            """
            WITH due AS (
                SELECT id
                FROM scheduled_jobs
                WHERE status = 'pending'
                  AND scheduled_for <= $1
                  AND ($3::boolean = false OR job_type = 'heartbeat')
                  AND (claimed_at IS NULL OR claimed_at < $1::timestamptz - interval '10 minutes')
                ORDER BY scheduled_for ASC, created_at ASC
                LIMIT $2
                FOR UPDATE SKIP LOCKED
            )
            UPDATE scheduled_jobs sj
            SET claimed_at = $1,
                claimed_by = $4,
                updated_at = $1
            FROM due
            WHERE sj.id = due.id
            RETURNING sj.id, sj.user_id, sj.job_type, sj.scheduled_for, sj.context,
                      sj.status, sj.attempt_count, sj.max_attempts, sj.delayed,
                      sj.bot_id, sj.topic_id
            """,
            now,
            self.settings.scheduler_batch_size,
            heartbeat_only,
            self.worker_id,
        )
        return [dict(row) for row in rows]

    async def _dispatch(self, job: dict[str, Any]) -> None:
        # pause-check: withhold if globally paused or per-(user, bot) paused
        if (
            job.get("user_id") is not None
            and job.get("bot_id") is not None
            and await system_state.user_bot_paused(self.pool, job["user_id"], job["bot_id"])
        ):
            logger.debug(
                "scheduled job %s withheld: per-(user, bot) pause active for user=%s bot=%s",
                job["id"],
                job["user_id"],
                job["bot_id"],
            )
            await self._mark_withheld(job["id"])
            return
        handler = self.handlers.get(job["job_type"])
        if handler is None:
            raise RuntimeError(f"no scheduled job handler registered for {job['job_type']}")
        await handler(job)

    async def _handle_heartbeat(self, job: dict[str, Any]) -> None:
        logger.info("scheduled heartbeat fired job_id=%s scheduled_for=%s", job["id"], job["scheduled_for"])
        await purge_expired_deletions(self.pool)

    async def _mark_fired(self, job_id: Any, *, now: datetime) -> None:
        await self.pool.execute(
            """
            UPDATE scheduled_jobs
            SET status = 'fired',
                fired_at = $1,
                claimed_at = NULL,
                claimed_by = NULL,
                updated_at = $1
            WHERE id = $2
            """,
            now,
            job_id,
        )

    async def _mark_withheld(self, job_id: Any, *, now: datetime | None = None) -> None:
        now = _aware_utc(now or _utc_now())
        await self.pool.execute(
            """
            UPDATE scheduled_jobs
            SET status = 'withheld',
                claimed_at = NULL,
                claimed_by = NULL,
                updated_at = $1
            WHERE id = $2
            """,
            now,
            job_id,
        )

    async def _record_failure(self, job: dict[str, Any], exc: Exception, *, now: datetime) -> bool:
        next_attempt = int(job.get("attempt_count") or 0) + 1
        max_attempts = int(job.get("max_attempts") or 2)
        error = str(exc)[:1000]
        if next_attempt < max_attempts:
            await self.pool.execute(
                """
                UPDATE scheduled_jobs
                SET attempt_count = $1,
                    last_error = $2,
                    claimed_at = NULL,
                    claimed_by = NULL,
                    updated_at = $3
                WHERE id = $4
                """,
                next_attempt,
                error,
                now,
                job["id"],
            )
            return True
        await self.pool.execute(
            """
            UPDATE scheduled_jobs
            SET status = 'cancelled',
                attempt_count = $1,
                last_error = $2,
                cancellation_reason = $3,
                claimed_at = NULL,
                claimed_by = NULL,
                updated_at = $4
            WHERE id = $5
            """,
            next_attempt,
            error,
            "handler error after retry",
            now,
            job["id"],
        )
        return False


async def is_globally_paused(pool: Any) -> bool:
    return await system_state.is_paused(pool)


async def seed_heartbeat(
    pool: Any,
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> Any | None:
    settings = settings or get_settings()
    now = _aware_utc(now or _utc_now())
    scheduled_for = now + timedelta(hours=settings.heartbeat_interval_hours)
    return await pool.fetchrow(
        """
        INSERT INTO scheduled_jobs (user_id, job_type, scheduled_for, context, status, bot_id, topic_id)
        SELECT NULL, 'heartbeat', $1, '{}'::jsonb, 'pending', 'mediator', $2
        WHERE NOT EXISTS (
            SELECT 1
            FROM scheduled_jobs
            WHERE job_type = 'heartbeat' AND status = 'pending'
        )
        RETURNING id, scheduled_for
        """,
        scheduled_for,
        get_relationship_topic_id(),
    )
