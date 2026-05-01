"""Per-user inbound burst coalescing."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from resident_chat_runtime.coalescing import AsyncBurstCoalescer, BurstBatch

from app.models.user import User
from app.services.pacer import PacingDecision


@dataclass
class _Burst:
    message_ids: list[UUID]
    user: User
    first_seen_at: float
    source: str = "live"


class BurstCoalescer:
    def __init__(
        self,
        on_burst_complete: Callable[[list[UUID], User], Awaitable[None]],
        *,
        debounce_seconds: float = 10.0,
        max_seconds: float = 30.0,
        pacer: Any | None = None,
        on_paced_answer: Callable[[list[UUID], User, PacingDecision], Awaitable[None]] | None = None,
        on_paced_reaction: Callable[[list[UUID], User, PacingDecision], Awaitable[None]] | None = None,
        on_live_typing: Callable[[User, asyncio.Event], Awaitable[None]] | None = None,
    ) -> None:
        self.on_burst_complete = on_burst_complete
        self.on_paced_answer = on_paced_answer
        self.on_paced_reaction = on_paced_reaction
        self.on_live_typing = on_live_typing
        self.pacer = pacer
        self.debounce_seconds = debounce_seconds
        self.max_seconds = max_seconds
        self._bursts: dict[UUID, _Burst] = {}
        self._locks: dict[UUID, asyncio.Lock] = {}
        self._wait_tasks: dict[UUID, asyncio.Task] = {}
        self._live_typing_tasks: dict[UUID, asyncio.Task[None]] = {}
        self._live_typing_stops: dict[UUID, asyncio.Event] = {}
        self._coalescer: AsyncBurstCoalescer[UUID, UUID] = AsyncBurstCoalescer(
            self._fire_batch,
            idle_delay=debounce_seconds,
            max_delay=max_seconds,
        )

    async def add(self, user_id: UUID, message_id: UUID, user: User, *, source: str = "live") -> None:
        loop = asyncio.get_running_loop()
        lock = self._locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            wait_task = self._wait_tasks.pop(user_id, None)
            if wait_task is not None:
                wait_task.cancel()
            burst = self._bursts.get(user_id)
            if burst is None:
                burst = _Burst(message_ids=[], user=user, first_seen_at=loop.time(), source=source)
                self._bursts[user_id] = burst
            burst.message_ids.append(message_id)
            burst.user = user
            burst.source = self._merge_source(burst.source, source)
            if source == "live" and self.on_live_typing is not None and user_id not in self._live_typing_tasks:
                stop_event = asyncio.Event()
                self._live_typing_stops[user_id] = stop_event
                self._live_typing_tasks[user_id] = asyncio.create_task(self.on_live_typing(user, stop_event))
            await self._coalescer.submit(user_id, message_id)

    async def add_burst(self, user_id: UUID, message_ids: list[UUID], user: User) -> None:
        await self.on_burst_complete(message_ids, user)

    async def _fire(self, user_id: UUID) -> None:
        await self._coalescer.flush(user_id)

    async def _fire_batch(self, batch: BurstBatch[UUID, UUID]) -> None:
        lock = self._locks.setdefault(batch.key, asyncio.Lock())
        async with lock:
            burst = self._bursts.pop(batch.key, None)
            if burst is None:
                return
            await self._handle_ready_burst(batch.key, burst)

    def snapshot(self) -> dict[UUID, Any]:
        return self._bursts

    async def _handle_ready_burst(self, user_id: UUID, burst: _Burst) -> None:
        message_ids = list(burst.message_ids)
        await self._stop_live_typing(user_id)
        if self.pacer is None:
            # Runtime contract: on_burst_complete(triggering_message_ids: list[UUID], user).
            await self.on_burst_complete(message_ids, burst.user)
            return

        decision = await self.pacer.decide_and_record(burst.user, message_ids, source=burst.source)
        if decision.action == "wait":
            self._bursts[user_id] = burst
            self._wait_tasks[user_id] = asyncio.create_task(self._fire_after_wait(user_id, max(0.0, decision.wait_s)))
            return
        if decision.action == "answer":
            await self._call_paced_answer(message_ids, burst.user, decision)
            return
        if decision.action == "react":
            if self.on_paced_reaction is not None:
                await self.on_paced_reaction(message_ids, burst.user, decision)
            await self._mark_processed(message_ids)
            return
        if decision.action == "silence":
            await self._mark_processed(message_ids)
            return
        await self._call_paced_answer(message_ids, burst.user, decision)

    async def _stop_live_typing(self, user_id: UUID) -> None:
        stop_event = self._live_typing_stops.pop(user_id, None)
        task = self._live_typing_tasks.pop(user_id, None)
        if stop_event is not None:
            stop_event.set()
        if task is not None:
            try:
                await task
            except Exception:
                pass

    async def _fire_after_wait(self, user_id: UUID, wait_s: float) -> None:
        try:
            await asyncio.sleep(wait_s)
            lock = self._locks.setdefault(user_id, asyncio.Lock())
            async with lock:
                self._wait_tasks.pop(user_id, None)
                burst = self._bursts.pop(user_id, None)
                if burst is not None:
                    await self._handle_ready_burst(user_id, burst)
        except asyncio.CancelledError:
            raise

    async def _call_paced_answer(self, message_ids: list[UUID], user: User, decision: PacingDecision) -> None:
        if self.on_paced_answer is not None:
            await self.on_paced_answer(message_ids, user, decision)
            return
        await self.on_burst_complete(message_ids, user)

    async def _mark_processed(self, message_ids: list[UUID]) -> None:
        pool = getattr(self.pacer, "pool", None)
        if pool is None or not message_ids:
            return
        await pool.execute(
            "UPDATE messages SET processing_state='processed' WHERE id = ANY($1::uuid[]) AND processing_state='raw'",
            message_ids,
        )

    def _merge_source(self, existing: str, incoming: str) -> str:
        if existing == incoming:
            return existing
        priority = {
            "recovery": 4,
            "catch_up": 4,
            "media": 3,
            "mixed": 2,
            "live": 1,
        }
        existing_priority = priority.get(existing, 2)
        incoming_priority = priority.get(incoming, 2)
        if existing_priority > incoming_priority:
            return existing
        if incoming_priority > existing_priority:
            return incoming
        if existing in {"recovery", "catch_up"}:
            return existing
        if incoming in {"recovery", "catch_up"}:
            return incoming
        return "mixed"
