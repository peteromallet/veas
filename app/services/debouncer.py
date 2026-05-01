"""Per-user inbound burst coalescing."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.models.user import User


@dataclass
class _Burst:
    message_ids: list[UUID]
    user: User
    first_seen_at: float
    timer: asyncio.TimerHandle | None = None


class BurstCoalescer:
    def __init__(
        self,
        on_burst_complete: Callable[[list[UUID], User], Awaitable[None]],
        *,
        debounce_seconds: float = 10.0,
        max_seconds: float = 30.0,
    ) -> None:
        self.on_burst_complete = on_burst_complete
        self.debounce_seconds = debounce_seconds
        self.max_seconds = max_seconds
        self._bursts: dict[UUID, _Burst] = {}
        self._locks: dict[UUID, asyncio.Lock] = {}

    async def add(self, user_id: UUID, message_id: UUID, user: User) -> None:
        loop = asyncio.get_running_loop()
        lock = self._locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            now = loop.time()
            burst = self._bursts.get(user_id)
            if burst is None:
                burst = _Burst(message_ids=[], user=user, first_seen_at=now)
                self._bursts[user_id] = burst
            burst.message_ids.append(message_id)
            burst.user = burst.user or user
            if burst.timer is not None:
                burst.timer.cancel()
            delay = min(self.debounce_seconds, max(0.0, self.max_seconds - (now - burst.first_seen_at)))
            burst.timer = loop.call_later(delay, lambda: asyncio.create_task(self._fire(user_id)))

    async def add_burst(self, user_id: UUID, message_ids: list[UUID], user: User) -> None:
        await self.on_burst_complete(message_ids, user)

    async def _fire(self, user_id: UUID) -> None:
        lock = self._locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            burst = self._bursts.pop(user_id, None)
        if burst is None:
            return
        if burst.timer is not None:
            burst.timer.cancel()
        # Runtime contract: on_burst_complete(triggering_message_ids: list[UUID], user).
        await self.on_burst_complete(burst.message_ids, burst.user)

    def snapshot(self) -> dict[UUID, Any]:
        return self._bursts
