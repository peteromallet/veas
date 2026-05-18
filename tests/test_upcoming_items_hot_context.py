"""Hot-context Upcoming reminders section: scheduled_jobs surfacing.

Covers the contract the bot reads:
- All pending jobs for today (user-local) are included, even if there are >5.
- After today's set is taken, the section is padded with the earliest
  future jobs until total reaches max_total (default 5).
- Each item has a relative-time label and a brief snippet when present.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.services.hot_context_solo import _fetch_upcoming_items


class _FakeFetcher:
    """Minimal pool-like object exposing only .fetch(sql, *args)."""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows
        self.last_sql: str | None = None
        self.last_args: tuple | None = None

    async def fetch(self, sql: str, *args):
        self.last_sql = sql
        self.last_args = args
        user_id, bot_id, topic_id, scheduled_floor = args
        return [
            r
            for r in self._rows
            if r["user_id"] == user_id
            and r["bot_id"] == bot_id
            and r["topic_id"] == topic_id
            and r["scheduled_for"] >= scheduled_floor
        ]


def _job(
    *,
    user_id: UUID,
    bot_id: str,
    topic_id: UUID,
    scheduled_for: datetime,
    job_type: str = "checkin",
    brief: str | None = None,
) -> dict:
    context: dict = {}
    if brief is not None:
        context["brief"] = brief
    return {
        "id": uuid4(),
        "job_type": job_type,
        "scheduled_for": scheduled_for,
        "context": context,
        "topic_id": topic_id,
        "user_id": user_id,
        "bot_id": bot_id,
        "status": "pending",
    }


def test_returns_empty_when_no_pending_jobs():
    pool = _FakeFetcher([])
    out = asyncio.run(
        _fetch_upcoming_items(
            pool,
            user_id=uuid4(),
            bot_id="hector",
            topic_id=uuid4(),
            now_utc=datetime(2026, 5, 18, 12, 0, tzinfo=UTC),
            tz_name="UTC",
        )
    )
    assert out == []


def test_includes_all_today_even_when_over_max_total():
    user_id, bot_id, topic_id = uuid4(), "hector", uuid4()
    now = datetime(2026, 5, 18, 12, 0, tzinfo=UTC)
    rows = [
        _job(
            user_id=user_id,
            bot_id=bot_id,
            topic_id=topic_id,
            scheduled_for=now + timedelta(hours=h),
            brief=f"item-{h}",
        )
        for h in (1, 2, 3, 4, 5, 6)
    ]
    pool = _FakeFetcher(rows)
    out = asyncio.run(
        _fetch_upcoming_items(
            pool,
            user_id=user_id,
            bot_id=bot_id,
            topic_id=topic_id,
            now_utc=now,
            tz_name="UTC",
            max_total=5,
        )
    )
    # All 6 are today (UTC); the budget is overridden by the "include all
    # of today" rule and we get all 6 back.
    assert len(out) == 6
    assert [item["brief"] for item in out] == [f"item-{h}" for h in (1, 2, 3, 4, 5, 6)]


def test_pads_to_max_total_with_future_items():
    user_id, bot_id, topic_id = uuid4(), "hector", uuid4()
    now = datetime(2026, 5, 18, 12, 0, tzinfo=UTC)
    today_jobs = [
        _job(
            user_id=user_id,
            bot_id=bot_id,
            topic_id=topic_id,
            scheduled_for=now + timedelta(hours=h),
            brief=f"today-{h}",
        )
        for h in (1, 2)
    ]
    future_jobs = [
        _job(
            user_id=user_id,
            bot_id=bot_id,
            topic_id=topic_id,
            scheduled_for=now + timedelta(days=d),
            brief=f"later-{d}",
        )
        for d in (1, 2, 3, 4, 5, 6)
    ]
    pool = _FakeFetcher(today_jobs + future_jobs)
    out = asyncio.run(
        _fetch_upcoming_items(
            pool,
            user_id=user_id,
            bot_id=bot_id,
            topic_id=topic_id,
            now_utc=now,
            tz_name="UTC",
            max_total=5,
        )
    )
    # 2 today + 3 padded future = 5 total
    assert len(out) == 5
    briefs = [item["brief"] for item in out]
    assert briefs == ["today-1", "today-2", "later-1", "later-2", "later-3"]


def test_scopes_to_user_bot_topic():
    user_a, user_b = uuid4(), uuid4()
    topic_a, topic_b = uuid4(), uuid4()
    now = datetime(2026, 5, 18, 12, 0, tzinfo=UTC)
    rows = [
        _job(user_id=user_a, bot_id="hector", topic_id=topic_a, scheduled_for=now + timedelta(hours=1), brief="match"),
        _job(user_id=user_b, bot_id="hector", topic_id=topic_a, scheduled_for=now + timedelta(hours=1), brief="other-user"),
        _job(user_id=user_a, bot_id="tante_rosi", topic_id=topic_a, scheduled_for=now + timedelta(hours=1), brief="other-bot"),
        _job(user_id=user_a, bot_id="hector", topic_id=topic_b, scheduled_for=now + timedelta(hours=1), brief="other-topic"),
    ]
    pool = _FakeFetcher(rows)
    out = asyncio.run(
        _fetch_upcoming_items(
            pool,
            user_id=user_a,
            bot_id="hector",
            topic_id=topic_a,
            now_utc=now,
            tz_name="UTC",
        )
    )
    assert [i["brief"] for i in out] == ["match"]


def test_local_day_label_uses_user_timezone():
    """A job at UTC midnight tomorrow is still 'today' in a positive-offset tz."""
    user_id, bot_id, topic_id = uuid4(), "hector", uuid4()
    # Now: 23:00 UTC May 18. In CET (UTC+2 with DST), local time is May 19 01:00.
    # A job for 00:00 UTC May 19 (in CET that's 02:00 May 19) is "today" locally.
    now = datetime(2026, 5, 18, 23, 0, tzinfo=UTC)
    today_in_cet = _job(
        user_id=user_id,
        bot_id=bot_id,
        topic_id=topic_id,
        scheduled_for=datetime(2026, 5, 19, 0, 0, tzinfo=UTC),
        brief="midnight-utc",
    )
    pool = _FakeFetcher([today_in_cet])
    out = asyncio.run(
        _fetch_upcoming_items(
            pool,
            user_id=user_id,
            bot_id=bot_id,
            topic_id=topic_id,
            now_utc=now,
            tz_name="Europe/Berlin",
        )
    )
    assert len(out) == 1
    # local_day_label is 'today' because user-local "now" is also May 19.
    assert out[0]["local_day_label"] == "today"
    # Relative-time label is present.
    assert out[0]["relative_to_now"]
