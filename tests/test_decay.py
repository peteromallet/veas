from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.services.decay import run_decay_housekeeping

pytestmark = pytest.mark.anyio


async def test_decay_housekeeping_transitions_against_synthetic_time(fake_pool):
    now = datetime(2026, 4, 30, 12, 0, tzinfo=UTC)
    active_old = uuid4()
    dormant_old = uuid4()
    active_recent = uuid4()
    fake_pool.themes[active_old] = {
        "id": active_old,
        "status": "active",
        "last_reinforced_at": now - timedelta(weeks=7),
        "first_seen_at": now - timedelta(weeks=10),
        "updated_at": now - timedelta(weeks=7),
    }
    fake_pool.themes[dormant_old] = {
        "id": dormant_old,
        "status": "dormant",
        "last_reinforced_at": now - timedelta(days=200),
        "first_seen_at": now - timedelta(days=220),
        "updated_at": now - timedelta(days=130),
    }
    fake_pool.themes[active_recent] = {
        "id": active_recent,
        "status": "active",
        "last_reinforced_at": now - timedelta(days=10),
        "first_seen_at": now - timedelta(days=10),
        "updated_at": now - timedelta(days=10),
    }

    decays_to_low = uuid4()
    goes_stale = uuid4()
    stays_high = uuid4()
    fake_pool.observations[decays_to_low] = {
        "id": decays_to_low,
        "content": "old but not stale",
        "status": "active",
        "confidence": "medium",
        "significance": 3,
        "created_at": now - timedelta(days=100),
        "last_reinforced_at": None,
        "scoring_prompt_version": "v1",
    }
    fake_pool.observations[goes_stale] = {
        "id": goes_stale,
        "content": "stale",
        "status": "active",
        "confidence": "high",
        "significance": 4,
        "created_at": now - timedelta(days=200),
        "last_reinforced_at": None,
        "scoring_prompt_version": "v1",
    }
    fake_pool.observations[stays_high] = {
        "id": stays_high,
        "content": "fresh",
        "status": "active",
        "confidence": "high",
        "significance": 5,
        "created_at": now - timedelta(days=20),
        "last_reinforced_at": None,
        "scoring_prompt_version": "v1",
    }

    expired_watch = uuid4()
    fresh_watch = uuid4()
    fake_pool.watch_items[expired_watch] = {
        "id": expired_watch,
        "owner_user_id": uuid4(),
        "content": "expired",
        "status": "open",
        "due_at": now - timedelta(days=31),
        "addressed_at": None,
    }
    fake_pool.watch_items[fresh_watch] = {
        "id": fresh_watch,
        "owner_user_id": uuid4(),
        "content": "fresh",
        "status": "open",
        "due_at": now - timedelta(days=5),
        "addressed_at": None,
    }

    report = await run_decay_housekeeping(fake_pool, now=now)

    assert fake_pool.themes[active_old]["status"] == "dormant"
    assert fake_pool.themes[dormant_old]["status"] == "resolved_by_time"
    assert fake_pool.themes[active_recent]["status"] == "active"
    assert fake_pool.observations[decays_to_low]["confidence"] == "low"
    assert fake_pool.observations[goes_stale]["status"] == "stale"
    assert fake_pool.observations[stays_high]["confidence"] == "high"
    assert fake_pool.watch_items[expired_watch]["status"] == "expired"
    assert fake_pool.watch_items[fresh_watch]["status"] == "open"
    assert report.rescore_report.scanned == 0
