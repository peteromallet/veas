"""Per-user inbound burst coalescing with explicit inbound scope."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from resident_chat_runtime.coalescing import AsyncBurstCoalescer, BurstBatch

from app.models.user import User
from app.services import inbound_queue
from app.services.pacer import PacingDecision
from app.services.scope import InboundScope

# Composite key type: (user_id, bot_id).  bot_id is always required.
CompositeKey = tuple[UUID, str]
logger = logging.getLogger(__name__)


@dataclass
class _Burst:
    message_ids: list[UUID]
    user: User
    first_seen_at: float
    scope: InboundScope
    source: str = "live"


class BurstCoalescer:
    def __init__(
        self,
        on_burst_complete: Callable[..., Awaitable[None]],
        *,
        debounce_seconds: float = 10.0,
        max_seconds: float = 30.0,
        pacer: Any | None = None,
        on_paced_answer: Callable[..., Awaitable[None]] | None = None,
        on_paced_reaction: Callable[..., Awaitable[None]] | None = None,
        on_live_typing: Callable[..., Awaitable[None]] | None = None,
    ) -> None:
        self.on_burst_complete = on_burst_complete
        self.on_paced_answer = on_paced_answer
        self.on_paced_reaction = on_paced_reaction
        self.on_live_typing = on_live_typing
        self.pacer = pacer
        self.debounce_seconds = debounce_seconds
        self.max_seconds = max_seconds
        self._bursts: dict[CompositeKey, _Burst] = {}
        self._locks: dict[CompositeKey, asyncio.Lock] = {}
        self._wait_tasks: dict[CompositeKey, asyncio.Task] = {}
        self._live_typing_tasks: dict[UUID, asyncio.Task[None]] = {}
        self._live_typing_stops: dict[UUID, asyncio.Event] = {}
        self._coalescer: AsyncBurstCoalescer[UUID, UUID] = AsyncBurstCoalescer(
            self._fire_batch,
            idle_delay=debounce_seconds,
            max_delay=max_seconds,
        )

    async def add(self, user_id: UUID, message_id: UUID, user: User, *, scope: InboundScope, source: str = "live") -> None:
        key: CompositeKey = (user_id, scope.bot_id)
        loop = asyncio.get_running_loop()
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            wait_task = self._wait_tasks.pop(key, None)
            if wait_task is not None:
                wait_task.cancel()
            burst = self._bursts.get(key)
            if burst is None:
                burst = _Burst(message_ids=[], user=user, first_seen_at=loop.time(), source=source, scope=scope)
                self._bursts[key] = burst
            burst.message_ids.append(message_id)
            burst.user = user
            burst.scope = scope
            burst.source = self._merge_source(burst.source, source)
            if source == "live" and self.on_live_typing is not None and user_id not in self._live_typing_tasks:
                stop_event = asyncio.Event()
                self._live_typing_stops[user_id] = stop_event
                self._live_typing_tasks[user_id] = asyncio.create_task(self.on_live_typing(user, stop_event, scope=scope))
            await self._coalescer.submit(user_id, message_id)

    async def add_burst(self, user_id: UUID, message_ids: list[UUID], user: User, *, scope: InboundScope) -> None:
        await self.on_burst_complete(message_ids, user, scope=scope)

    async def _fire(self, user_id: UUID) -> None:
        await self._coalescer.flush(user_id)

    async def _fire_batch(self, batch: BurstBatch[UUID, UUID]) -> None:
        user_id = batch.key

        # --- Composite-key lookup ---
        # Try to find any burst for this user that carries a specific bot_id.
        # AsyncBurstCoalescer serialises per user_id, so only one batch fires
        # at a time for a given user.
        composite_key: CompositeKey | None = None
        burst: _Burst | None = None
        for (uid, _bot_id), candidate in list(self._bursts.items()):
            if uid == user_id:
                composite_key = (uid, _bot_id)
                burst = candidate
                break

        if composite_key is not None:
            lock = self._locks.setdefault(composite_key, asyncio.Lock())
            async with lock:
                burst = self._bursts.pop(composite_key, None)
                if burst is not None:
                    await self._handle_ready_burst(user_id, burst)
            return

    def snapshot(self) -> dict[UUID, Any]:
        # Maintain backward compat: expose bursts keyed by user_id for callers
        # that iterate snapshot().  Duplicate the last-seen burst when the same
        # user_id appears under multiple bot_ids (unlikely in S2a but safe).
        result: dict[UUID, Any] = {}
        for (uid, _), burst in self._bursts.items():
            result[uid] = burst
        return result

    async def _handle_ready_burst(self, user_id: UUID, burst: _Burst) -> None:
        message_ids = list(burst.message_ids)
        await self._stop_live_typing(user_id)
        if self.pacer is None:
            await self.on_burst_complete(message_ids, burst.user, scope=burst.scope)
            return

        decision = await self.pacer.decide_and_record(burst.user, message_ids, source=burst.source)
        if decision.action == "wait":
            # Re-store under the burst's original composite key so the wait-task
            # can find it later.
            rekey: CompositeKey = (user_id, burst.scope.bot_id)
            self._bursts[rekey] = burst
            self._wait_tasks[rekey] = asyncio.create_task(self._fire_after_wait(rekey, max(0.0, decision.wait_s)))
            return
        if decision.action == "answer":
            await self._call_paced_answer(message_ids, burst.user, decision, scope=burst.scope)
            return
        if decision.action == "react":
            if self.on_paced_reaction is not None:
                await self.on_paced_reaction(message_ids, burst.user, decision, scope=burst.scope)
            await self._mark_processed(message_ids, handling_result="replied", scope=burst.scope)
            return
        if decision.action == "silence":
            await self._mark_processed(message_ids, handling_result="silent", scope=burst.scope)
            return
        await self._call_paced_answer(message_ids, burst.user, decision, scope=burst.scope)

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

    async def _fire_after_wait(self, key: CompositeKey, wait_s: float) -> None:
        user_id = key[0]
        try:
            await asyncio.sleep(wait_s)
            lock = self._locks.setdefault(key, asyncio.Lock())
            async with lock:
                self._wait_tasks.pop(key, None)
                burst = self._bursts.pop(key, None)
                if burst is not None:
                    await self._handle_ready_burst(user_id, burst)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "paced wait flush failed for user_id=%s bot_id=%s",
                key[0],
                key[1],
            )

    async def _call_paced_answer(self, message_ids: list[UUID], user: User, decision: PacingDecision, *, scope: InboundScope | None = None) -> None:
        if self.on_paced_answer is not None:
            await self.on_paced_answer(message_ids, user, decision, scope=scope)
            return
        await self.on_burst_complete(message_ids, user, scope=scope)

    async def _mark_processed(self, message_ids: list[UUID], *, handling_result: str, scope: InboundScope) -> None:
        pool = getattr(self.pacer, "pool", None)
        if pool is None or not message_ids:
            return
        await inbound_queue.complete_messages(
            pool,
            message_ids,
            handling_result=handling_result,
            handled_by_turn_id=None,  # pacer never opens a bot_turns row
            bot_id=scope.bot_id,
            topic_id=scope.topic_id,
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
