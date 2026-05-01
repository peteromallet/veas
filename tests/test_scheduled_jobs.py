from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.config import get_settings
from app.services.recovery import recover_scheduled_jobs_on_startup
from app.services.scheduled_job_handlers import ScheduledJobHandlers, next_weekly_summary_at, seed_weekly_summaries
from app.services.scheduled_jobs import ScheduledJobWorker, seed_heartbeat
from app.services import system_state

pytestmark = pytest.mark.anyio


def _job(pool, *, job_type: str, user_id=None, scheduled_for=None, status="pending", attempt_count=0, max_attempts=2):
    job_id = uuid4()
    pool.scheduled_jobs[job_id] = {
        "id": job_id,
        "user_id": user_id,
        "job_type": job_type,
        "scheduled_for": scheduled_for or datetime.now(UTC),
        "context": {},
        "status": status,
        "attempt_count": attempt_count,
        "max_attempts": max_attempts,
        "delayed": False,
        "claimed_at": None,
        "claimed_by": None,
        "created_at": datetime.now(UTC),
    }
    return job_id


def _user(pool, *, weekly_summary_day=1, weekly_summary_time="09:00", timezone="UTC"):
    user_id = uuid4()
    pool.users[user_id] = {
        "id": user_id,
        "name": "Maya",
        "phone": "15555550100",
        "timezone": timezone,
        "weekly_summary_enabled": True,
        "weekly_summary_day": weekly_summary_day,
        "weekly_summary_time": weekly_summary_time,
        "onboarding_state": "welcomed",
    }
    return user_id


async def test_worker_claims_concurrently_without_double_firing(fake_pool):
    now = datetime(2026, 4, 30, 12, 0, tzinfo=UTC)
    user_id = _user(fake_pool)
    job_id = _job(fake_pool, job_type="checkin", user_id=user_id, scheduled_for=now)
    fired = []

    async def handler(job):
        fired.append(job["id"])

    settings = SimpleNamespace(scheduler_batch_size=10, heartbeat_interval_hours=24, scheduler_poll_interval_s=1)
    worker_a = ScheduledJobWorker(fake_pool, handlers={"checkin": handler}, settings=settings, worker_id="a")
    worker_b = ScheduledJobWorker(fake_pool, handlers={"checkin": handler}, settings=settings, worker_id="b")

    await asyncio.gather(worker_a.run_due_once(now=now), worker_b.run_due_once(now=now))

    assert fired == [job_id]
    assert fake_pool.scheduled_jobs[job_id]["status"] == "fired"


async def test_worker_retries_once_then_cancels_failed_job(fake_pool):
    now = datetime(2026, 4, 30, 12, 0, tzinfo=UTC)
    user_id = _user(fake_pool)
    job_id = _job(fake_pool, job_type="checkin", user_id=user_id, scheduled_for=now)

    async def failing(_job):
        raise RuntimeError("boom")

    settings = SimpleNamespace(scheduler_batch_size=10, heartbeat_interval_hours=24, scheduler_poll_interval_s=1)
    worker = ScheduledJobWorker(fake_pool, handlers={"checkin": failing}, settings=settings, worker_id="worker")

    first = await worker.run_due_once(now=now)
    assert first.retried == 1
    assert fake_pool.scheduled_jobs[job_id]["status"] == "pending"
    assert fake_pool.scheduled_jobs[job_id]["attempt_count"] == 1

    second = await worker.run_due_once(now=now + timedelta(minutes=11))
    assert second.cancelled == 1
    assert fake_pool.scheduled_jobs[job_id]["status"] == "cancelled"
    assert fake_pool.scheduled_jobs[job_id]["cancellation_reason"] == "handler error after retry"


async def test_pause_claims_only_heartbeat_and_preserves_user_jobs(fake_pool):
    now = datetime(2026, 4, 30, 12, 0, tzinfo=UTC)
    user_id = _user(fake_pool)
    checkin_id = _job(fake_pool, job_type="checkin", user_id=user_id, scheduled_for=now)
    heartbeat_id = _job(fake_pool, job_type="heartbeat", user_id=None, scheduled_for=now)
    fired = []

    async def checkin_handler(job):
        fired.append(("checkin", job["id"]))

    settings = SimpleNamespace(scheduler_batch_size=10, heartbeat_interval_hours=24, scheduler_poll_interval_s=1)
    worker = ScheduledJobWorker(fake_pool, handlers={"checkin": checkin_handler}, settings=settings, worker_id="paused")
    await system_state.pause(fake_pool, user_id, now=now)

    result = await worker.run_due_once(now=now)

    assert result.skipped_paused is True
    assert fired == []
    assert fake_pool.scheduled_jobs[checkin_id]["status"] == "pending"
    assert fake_pool.scheduled_jobs[heartbeat_id]["status"] == "fired"


async def test_late_job_recovery_marks_stale_delayed_and_recent(fake_pool):
    now = datetime(2026, 4, 30, 12, 0, tzinfo=UTC)
    user_id = _user(fake_pool)
    stale = _job(fake_pool, job_type="checkin", user_id=user_id, scheduled_for=now - timedelta(hours=25))
    delayed = _job(fake_pool, job_type="checkin", user_id=user_id, scheduled_for=now - timedelta(hours=2))
    recent = _job(fake_pool, job_type="checkin", user_id=user_id, scheduled_for=now - timedelta(minutes=20))
    fake_pool.scheduled_jobs[stale]["claimed_at"] = now - timedelta(hours=1)
    fake_pool.scheduled_jobs[delayed]["claimed_at"] = now - timedelta(hours=1)
    fake_pool.scheduled_jobs[recent]["claimed_at"] = now - timedelta(hours=1)

    await recover_scheduled_jobs_on_startup(fake_pool, now=now)

    assert fake_pool.scheduled_jobs[stale]["status"] == "cancelled"
    assert fake_pool.scheduled_jobs[stale]["cancellation_reason"] == "too stale"
    assert fake_pool.scheduled_jobs[delayed]["delayed"] is True
    assert fake_pool.scheduled_jobs[delayed]["claimed_at"] is None
    assert fake_pool.scheduled_jobs[recent]["status"] == "pending"
    assert fake_pool.scheduled_jobs[recent]["claimed_at"] is None


async def test_weekly_summary_sends_template_runs_decay_and_reseeds(fake_pool, monkeypatch):
    now = datetime(2026, 4, 30, 12, 0, tzinfo=UTC)
    user_id = _user(fake_pool, weekly_summary_day=4, weekly_summary_time="09:00")
    job_id = _job(fake_pool, job_type="weekly_summary", user_id=user_id, scheduled_for=now)
    fake_pool.scheduled_jobs[job_id]["status"] = "fired"
    sent = []
    decayed = []

    async def fake_send(pool, user, content, *, template_fallback=None, bot_turn_id=None, ignore_pause=False):
        sent.append((user, content, template_fallback))
        return uuid4()

    async def fake_decay(pool):
        decayed.append(pool)

    monkeypatch.setattr("app.services.scheduled_job_handlers.send_outbound", fake_send)
    monkeypatch.setattr("app.services.scheduled_job_handlers.run_decay_housekeeping", fake_decay)

    await ScheduledJobHandlers(fake_pool).handle_weekly_summary(fake_pool.scheduled_jobs[job_id])

    assert sent[0][2].name == "weekly_summary"
    assert decayed == [fake_pool]
    pending_weeklies = [job for job in fake_pool.scheduled_jobs.values() if job["job_type"] == "weekly_summary" and job["status"] == "pending"]
    assert len(pending_weeklies) == 1


async def test_discord_checkin_runs_agentic_job_without_whatsapp_window(fake_pool, monkeypatch):
    monkeypatch.setenv("MESSAGING_PROVIDER", "discord")
    get_settings.cache_clear()
    user_id = _user(fake_pool)
    fake_pool.users[user_id]["phone"] = "456"
    job_id = _job(fake_pool, job_type="checkin", user_id=user_id)
    ran = []
    sent = []

    async def fake_run_agentic_job(user, metadata):
        ran.append((user.id, metadata))

    async def fake_send(*args, **kwargs):
        sent.append((args, kwargs))

    monkeypatch.setattr("app.services.scheduled_job_handlers.run_agentic_job", fake_run_agentic_job)
    monkeypatch.setattr("app.services.scheduled_job_handlers.send_outbound", fake_send)

    await ScheduledJobHandlers(fake_pool).handle_checkin(fake_pool.scheduled_jobs[job_id])

    assert ran == [(user_id, {"kind": "checkin", "context": {"delayed": False}})]
    assert sent == []
    get_settings.cache_clear()


async def test_seed_helpers_use_durable_weekly_timing_and_single_heartbeat(fake_pool):
    now = datetime(2026, 4, 30, 12, 0, tzinfo=UTC)
    user_id = _user(fake_pool, weekly_summary_day=5, weekly_summary_time="09:30", timezone="Europe/Berlin")

    expected = next_weekly_summary_at(fake_pool.users[user_id], now=now)
    await seed_weekly_summaries(fake_pool, now=now)
    await seed_weekly_summaries(fake_pool, now=now)
    weekly = [job for job in fake_pool.scheduled_jobs.values() if job["job_type"] == "weekly_summary"]
    assert len(weekly) == 1
    assert weekly[0]["scheduled_for"] == expected

    await seed_heartbeat(fake_pool, settings=SimpleNamespace(heartbeat_interval_hours=24), now=now)
    await seed_heartbeat(fake_pool, settings=SimpleNamespace(heartbeat_interval_hours=24), now=now)
    assert len([job for job in fake_pool.scheduled_jobs.values() if job["job_type"] == "heartbeat"]) == 1


async def test_heartbeat_purges_expired_deletions(fake_pool):
    now = datetime(2026, 4, 30, 12, 0, tzinfo=UTC)
    message_id = uuid4()
    fake_pool.messages[message_id] = {
        "id": message_id,
        "direction": "inbound",
        "sender_id": uuid4(),
        "recipient_id": None,
        "content": "delete me",
        "processing_state": "processed",
        "sent_at": now - timedelta(days=2),
        "charge": "routine",
        "whatsapp_message_id": "wa-delete",
        "media_type": None,
        "media_url": None,
        "media_duration_seconds": None,
        "media_analysis": None,
        "edit_history": None,
        "edited_at": None,
        "deleted_at": now - timedelta(hours=25),
    }

    await ScheduledJobHandlers(fake_pool).handle_heartbeat({"id": uuid4(), "scheduled_for": now})

    assert fake_pool.messages[message_id]["content"] == "[deleted]"
