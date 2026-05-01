import asyncio
from uuid import uuid4

import pytest

from app.models.user import User
from app.services.debouncer import BurstCoalescer


pytestmark = pytest.mark.anyio


async def test_rapid_messages_coalesce_to_one_burst() -> None:
    calls = []

    async def callback(message_ids, user):
        calls.append((message_ids, user))

    user = User(id=uuid4(), name="Maya", phone="15555550100", timezone="UTC")
    coalescer = BurstCoalescer(callback, debounce_seconds=0.01, max_seconds=0.1)
    ids = [uuid4() for _ in range(5)]
    for message_id in ids:
        await coalescer.add(user.id, message_id, user)

    await asyncio.sleep(0.03)
    assert calls == [(ids, user)]


async def test_max_window_forces_second_burst() -> None:
    calls = []

    async def callback(message_ids, user):
        calls.append(message_ids)

    user = User(id=uuid4(), name="Maya", phone="15555550100", timezone="UTC")
    coalescer = BurstCoalescer(callback, debounce_seconds=0.04, max_seconds=0.06)
    first = [uuid4(), uuid4()]
    await coalescer.add(user.id, first[0], user)
    await asyncio.sleep(0.03)
    await coalescer.add(user.id, first[1], user)
    await asyncio.sleep(0.05)
    second = uuid4()
    await coalescer.add(user.id, second, user)
    await asyncio.sleep(0.06)

    assert calls == [first, [second]]


async def test_add_burst_fires_callback_with_supplied_user() -> None:
    calls = []

    async def callback(message_ids, user):
        calls.append((message_ids, user))

    user = User(id=uuid4(), name="Maya", phone="15555550100", timezone="UTC")
    coalescer = BurstCoalescer(callback)
    ids = [uuid4(), uuid4()]

    await coalescer.add_burst(user.id, ids, user)

    assert calls == [(ids, user)]
