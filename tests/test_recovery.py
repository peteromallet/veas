import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.models.user import User
from app.services import recovery
from app.services.recovery import recover_on_startup


pytestmark = pytest.mark.anyio


class CoalescerRecorder:
    def __init__(self) -> None:
        self.add_calls = []
        self.add_burst_calls = []

    async def add(self, user_id, message_id, user, *, source: str = "live"):
        self.add_calls.append((user_id, message_id, user, source))

    async def add_burst(self, user_id, message_ids, user):
        self.add_burst_calls.append((user_id, message_ids, user))


def _seed_user(fake_pool) -> User:
    user = User(id=uuid4(), name="Maya", phone="15555550100", timezone="UTC")
    fake_pool.users[user.id] = {
        "id": user.id,
        "name": user.name,
        "phone": user.phone,
        "timezone": user.timezone,
    }
    return user


def _seed_message(fake_pool, user: User):
    message_id = uuid4()
    fake_pool.messages[message_id] = {
        "id": message_id,
        "direction": "inbound",
        "sender_id": user.id,
        "recipient_id": None,
        "content": "raw",
        "processing_state": "raw",
        "sent_at": datetime.now(UTC) - timedelta(minutes=1),
        "charge": None,
        "whatsapp_message_id": f"wa-{message_id}",
        "media_type": None,
        "media_url": None,
        "media_duration_seconds": None,
        "media_analysis": None,
        "edit_history": None,
        "edited_at": None,
        "deleted_at": None,
    }
    return message_id


async def test_orphan_raw_message_readded_once(fake_pool) -> None:
    user = _seed_user(fake_pool)
    message_id = _seed_message(fake_pool, user)
    coalescer = CoalescerRecorder()

    await recover_on_startup(fake_pool, coalescer)

    assert coalescer.add_calls == [(user.id, message_id, user, "recovery")]


async def test_crashed_turn_marks_failed_and_requeues_full_burst(fake_pool) -> None:
    user = _seed_user(fake_pool)
    ids = [_seed_message(fake_pool, user), _seed_message(fake_pool, user)]
    turn_id = uuid4()
    fake_pool.bot_turns[turn_id] = {
        "id": turn_id,
        "triggering_message_ids": ids,
        "started_at": datetime.now(UTC) - timedelta(minutes=6),
        "completed_at": None,
        "failure_reason": None,
        "reasoning": "",
    }
    coalescer = CoalescerRecorder()

    await recover_on_startup(fake_pool, coalescer)

    assert fake_pool.bot_turns[turn_id]["failure_reason"] == "crashed"
    assert coalescer.add_burst_calls == [(user.id, ids, user)]
    assert coalescer.add_calls == []


async def test_already_marked_crashed_turn_is_not_requeued_again(fake_pool) -> None:
    user = _seed_user(fake_pool)
    ids = [_seed_message(fake_pool, user)]
    turn_id = uuid4()
    fake_pool.bot_turns[turn_id] = {
        "id": turn_id,
        "triggering_message_ids": ids,
        "started_at": datetime.now(UTC) - timedelta(minutes=6),
        "completed_at": None,
        "failure_reason": "crashed",
        "reasoning": "",
    }
    coalescer = CoalescerRecorder()

    await recover_on_startup(fake_pool, coalescer)

    assert coalescer.add_burst_calls == []
    assert coalescer.add_calls == []


async def test_turn_that_crashed_after_send_is_not_requeued(fake_pool) -> None:
    user = _seed_user(fake_pool)
    ids = [_seed_message(fake_pool, user)]
    outbound_id = uuid4()
    fake_pool.messages[outbound_id] = {
        "id": outbound_id,
        "direction": "outbound",
        "sender_id": None,
        "recipient_id": user.id,
        "content": "Already sent",
        "processing_state": "processed",
        "sent_at": datetime.now(UTC) - timedelta(minutes=6),
        "charge": None,
        "deleted_at": None,
    }
    turn_id = uuid4()
    fake_pool.bot_turns[turn_id] = {
        "id": turn_id,
        "triggering_message_ids": ids,
        "started_at": datetime.now(UTC) - timedelta(minutes=6),
        "completed_at": None,
        "failure_reason": None,
        "reasoning": "",
        "final_output_message_id": outbound_id,
    }
    coalescer = CoalescerRecorder()

    await recover_on_startup(fake_pool, coalescer)

    assert fake_pool.bot_turns[turn_id]["failure_reason"] == "crashed_after_send"
    assert coalescer.add_burst_calls == []


async def test_recovery_loop_rechecks_orphan_raw_messages(fake_pool, monkeypatch) -> None:
    user = _seed_user(fake_pool)
    message_id = _seed_message(fake_pool, user)
    coalescer = CoalescerRecorder()
    calls = 0

    async def fake_sleep(seconds):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(recovery.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await recovery.run_recovery_forever(fake_pool, coalescer, interval_seconds=0)

    assert coalescer.add_calls == [(user.id, message_id, user, "recovery")]
