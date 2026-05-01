"""Human-like Discord conversation pacing policy.

This module is intentionally transport-adjacent rather than transport-bound:
Discord gateway code reports typing state, the coalescer asks for a pre-turn
decision, and later wiring performs the selected action.
"""

from __future__ import annotations

import asyncio
import json
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Awaitable, Callable, Literal, Mapping
from uuid import UUID

import anthropic

from app.config import Settings, get_settings
from app.models.user import User, fetch_user_pacing_preferences, record_pacing_event
from app.services.spend import is_under_cap, record_anthropic_haiku_text_response_cost

PacingAction = Literal["wait", "react", "silence", "answer"]
STALE_SOURCES = {"catch_up", "recovery"}
MEDIA_SOURCES = {"media"}
ACK_REACTION = "👍"
CARE_REACTION = "❤️"
LLM_ALLOWED_ACTIONS = {"wait", "react", "silence", "answer"}
LLM_PACING_SYSTEM_PROMPT = """Choose a Discord pacing action before the assistant's full turn.

Return JSON only:
{"action":"answer","reason":"short reason","wait_s":0,"reaction":null}

Allowed actions:
- wait: the user likely has not finished the thought.
- react: only for a tiny warm acknowledgement.
- silence: no response is needed.
- answer: proceed to the full assistant turn.

Prefer answer when uncertain. Never override safety, media, catch-up, or recovery gates.
"""


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except (KeyError, TypeError):
        return default


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _response_text(response: Any) -> str:
    parts: list[str] = []
    for block in _attr(response, "content", []) or []:
        if _attr(block, "type") == "text":
            parts.append(str(_attr(block, "text", "")))
    return "\n".join(part for part in parts if part).strip()


def _placeholder_key(value: str) -> bool:
    lowered = value.lower()
    return "dummy" in lowered or "replace-with-" in lowered


@dataclass(frozen=True)
class MessageSignal:
    id: UUID
    content: str
    charge: str
    sent_at: datetime
    media_type: str | None = None
    media_analysis: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class TypingState:
    user_id: UUID
    channel_id: str | None
    first_seen_at: datetime
    last_seen_at: datetime


@dataclass(frozen=True)
class PacingDecision:
    action: PacingAction
    reason: str
    wait_s: float = 0.0
    reaction: str | None = None
    signal_snapshot: dict[str, Any] = field(default_factory=dict)
    preference_snapshot: dict[str, Any] = field(default_factory=dict)
    llm_judgement: dict[str, Any] | None = None

    @property
    def wait_ms(self) -> int | None:
        if self.wait_s <= 0:
            return None
        return int(round(self.wait_s * 1000))


class DiscordPacer:
    """Deterministic pacing policy plus observable side effects."""

    def __init__(
        self,
        pool: Any,
        *,
        settings: Settings | None = None,
        send_typing: Callable[[str], Awaitable[None]] | None = None,
        llm_client: Any | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.pool = pool
        self.settings = settings or get_settings()
        self._send_typing = send_typing
        self._llm_client = llm_client
        self._sleep = sleep
        self._now = now or (lambda: datetime.now(UTC))
        self._typing: dict[UUID, TypingState] = {}
        self._last_reaction_at: dict[UUID, datetime] = {}
        self._reaction_counts: dict[tuple[UUID, datetime.date], int] = {}

    def mark_user_typing(
        self,
        user_id: UUID,
        *,
        channel_id: str | None = None,
        at: datetime | None = None,
    ) -> None:
        now = at or self._now()
        current = self._typing.get(user_id)
        first_seen_at = current.first_seen_at if current is not None else now
        self._typing[user_id] = TypingState(
            user_id=user_id,
            channel_id=channel_id,
            first_seen_at=first_seen_at,
            last_seen_at=now,
        )

    def clear_user_typing(self, user_id: UUID) -> None:
        self._typing.pop(user_id, None)

    def typing_state(self, user_id: UUID, preferences: Mapping[str, Any] | None = None) -> TypingState | None:
        state = self._typing.get(user_id)
        if state is None:
            return None
        prefs = preferences or {}
        grace_s = float(prefs.get("typing_grace_s", self.settings.discord_pacing_typing_grace_s))
        if self._now() - state.last_seen_at <= timedelta(seconds=grace_s):
            return state
        self._typing.pop(user_id, None)
        return None

    async def fetch_message_signals(self, message_ids: list[UUID]) -> list[MessageSignal]:
        if not message_ids:
            return []
        rows = await self.pool.fetch(
            """
            SELECT id, content, charge, sent_at, media_type, media_analysis
            FROM messages
            WHERE id = ANY($1::uuid[])
            ORDER BY sent_at ASC
            """,
            message_ids,
        )
        by_id = {
            row["id"]: MessageSignal(
                id=row["id"],
                content=_row_get(row, "content") or "",
                charge=(_row_get(row, "charge") or "routine"),
                sent_at=row["sent_at"],
                media_type=_row_get(row, "media_type"),
                media_analysis=_row_get(row, "media_analysis"),
            )
            for row in rows
        }
        return [by_id[message_id] for message_id in message_ids if message_id in by_id]

    async def decide(
        self,
        user: User,
        message_ids: list[UUID],
        *,
        source: str = "live",
    ) -> PacingDecision:
        preferences = await fetch_user_pacing_preferences(self.pool, user.id)
        signals = await self.fetch_message_signals(message_ids)
        snapshot = self._signal_snapshot(signals, source)

        if not preferences["enabled"]:
            return self._decision("answer", "pacing disabled for user", snapshot, preferences)
        if not signals:
            return self._decision("answer", "no persisted message signals found", snapshot, preferences)

        charges = {signal.charge for signal in signals}
        has_media = source in MEDIA_SOURCES or any(signal.media_type for signal in signals)
        if "crisis" in charges:
            return self._decision("answer", "crisis message requires immediate substantive answer", snapshot, preferences)
        if "charged" in charges:
            return self._decision("answer", "charged message should not be delayed or minimized", snapshot, preferences)
        if has_media:
            return self._decision("answer", "media-derived message requires substantive handling", snapshot, preferences)
        if source in STALE_SOURCES:
            return self._decision("answer", f"{source} source is stale/offline work", snapshot, preferences)

        typing_state = self.typing_state(user.id, preferences)
        if typing_state is not None:
            wait_s = self._typing_wait_s(typing_state, preferences)
            if wait_s > 0:
                return self._decision(
                    "wait",
                    "user is actively composing; do not type over them",
                    snapshot | {"typing_active": True},
                    preferences,
                    wait_s=wait_s,
                )

        burst_wait_s = self._burst_wait_s(signals, preferences)
        if burst_wait_s > 0:
            return self._decision(
                "wait",
                "recent live burst may still be in progress",
                snapshot,
                preferences,
                wait_s=burst_wait_s,
            )

        reaction = self._reaction_for(user.id, signals, preferences)
        if reaction is not None:
            return self._decision(
                "react",
                "short low-stakes acknowledgement fits sparse reaction policy",
                snapshot,
                preferences,
                reaction=reaction,
            )

        if self._is_low_content_ack(signals):
            return self._decision(
                "silence",
                "low-content acknowledgement does not need a bot reply",
                snapshot,
                preferences,
            )

        llm_decision = await self._maybe_llm_decision(user, message_ids, source, signals, snapshot, preferences)
        if llm_decision is not None:
            return llm_decision

        return self._decision("answer", "substantive answer is appropriate", snapshot, preferences)

    async def decide_and_record(
        self,
        user: User,
        message_ids: list[UUID],
        *,
        source: str = "live",
    ) -> PacingDecision:
        decision = await self.decide(user, message_ids, source=source)
        await self.record_decision(user, message_ids, source, decision)
        if decision.action == "react":
            self._note_reaction(user.id)
        return decision

    async def record_decision(
        self,
        user: User,
        message_ids: list[UUID],
        source: str,
        decision: PacingDecision,
    ) -> UUID:
        return await record_pacing_event(
            self.pool,
            user_id=user.id,
            message_ids=message_ids,
            source=source,
            decision=decision.action,
            reason=decision.reason,
            signal_snapshot=decision.signal_snapshot,
            preference_snapshot=decision.preference_snapshot,
            wait_ms=decision.wait_ms,
            reaction=decision.reaction,
            llm_judgement=decision.llm_judgement,
        )

    def answer_typing_delay_s(self, answer_text: str, preferences: Mapping[str, Any]) -> float:
        chars_per_s = max(1.0, float(preferences["answer_chars_per_s"]))
        estimated = len(answer_text) / chars_per_s
        return min(
            max(estimated, float(preferences["answer_typing_min_s"])),
            float(preferences["answer_typing_max_s"]),
        )

    async def perform_answer_typing(self, user: User, channel_id: str, answer_text: str) -> float:
        """Emit human-feeling typing pulses, suppressing them while the user types."""
        preferences = await fetch_user_pacing_preferences(self.pool, user.id)
        if not preferences["enabled"] or self._send_typing is None:
            return 0.0

        waited_s = 0.0
        max_typing_wait_s = float(preferences["max_typing_wait_s"])
        while self.typing_state(user.id, preferences) is not None and waited_s < max_typing_wait_s:
            wait_s = min(float(self.settings.discord_pacing_typing_extend_s), max_typing_wait_s - waited_s)
            if wait_s <= 0:
                break
            await record_pacing_event(
                self.pool,
                user_id=user.id,
                message_ids=[],
                source="live",
                decision="typing_wait",
                reason="suppressed bot typing while user was composing",
                signal_snapshot={"typing_active": True, "channel_id": channel_id},
                preference_snapshot=preferences,
                wait_ms=int(round(wait_s * 1000)),
            )
            await self._sleep(wait_s)
            waited_s += wait_s

        if self.typing_state(user.id, preferences) is not None:
            return waited_s

        delay_s = self.answer_typing_delay_s(answer_text, preferences)
        remaining_s = delay_s
        while remaining_s > 0:
            await self._send_typing(channel_id)
            pulse_s = min(remaining_s, 7.0)
            await record_pacing_event(
                self.pool,
                user_id=user.id,
                message_ids=[],
                source="live",
                decision="typing_start",
                reason="started paced answer typing indicator",
                signal_snapshot={"channel_id": channel_id, "pulse_s": pulse_s},
                preference_snapshot=preferences,
                wait_ms=int(round(pulse_s * 1000)),
            )
            await self._sleep(pulse_s)
            waited_s += pulse_s
            remaining_s -= pulse_s
            if remaining_s > 0:
                pause_s = min(0.5, remaining_s)
                await record_pacing_event(
                    self.pool,
                    user_id=user.id,
                    message_ids=[],
                    source="live",
                    decision="typing_stop",
                    reason="briefly paused paced typing before next pulse",
                    signal_snapshot={"channel_id": channel_id, "pause_s": pause_s},
                    preference_snapshot=preferences,
                    wait_ms=int(round(pause_s * 1000)),
                )
                await self._sleep(pause_s)
                waited_s += pause_s
                remaining_s -= pause_s
        return waited_s

    async def perform_thinking_typing_until_stopped(
        self,
        user: User,
        channel_id: str,
        stop_event: asyncio.Event,
    ) -> None:
        """Emit Discord typing pulses while the agent is preparing a live answer."""
        preferences = await fetch_user_pacing_preferences(self.pool, user.id)
        if not preferences["enabled"] or self._send_typing is None:
            return

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=1.0)
            return
        except TimeoutError:
            pass

        pulse_interval_s = 6.5
        while not stop_event.is_set():
            if self.typing_state(user.id, preferences) is None:
                await self._send_typing(channel_id)
                await record_pacing_event(
                    self.pool,
                    user_id=user.id,
                    message_ids=[],
                    source="live",
                    decision="typing_start",
                    reason="started paced thinking typing indicator",
                    signal_snapshot={"channel_id": channel_id, "pulse_s": pulse_interval_s},
                    preference_snapshot=preferences,
                    wait_ms=int(round(pulse_interval_s * 1000)),
                )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=pulse_interval_s)
            except TimeoutError:
                pass

    async def perform_initial_typing_until_stopped(
        self,
        user: User,
        channel_id: str,
        stop_event: asyncio.Event,
    ) -> None:
        """Emit the first visible typing cue while live messages coalesce."""
        preferences = await fetch_user_pacing_preferences(self.pool, user.id)
        if not preferences["enabled"] or self._send_typing is None:
            return

        min_s = self.settings.discord_pacing_initial_typing_min_s
        max_s = max(min_s, self.settings.discord_pacing_initial_typing_max_s)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=random.uniform(min_s, max_s))
            return
        except TimeoutError:
            pass

        pulse_interval_s = 6.5
        while not stop_event.is_set():
            if self.typing_state(user.id, preferences) is None:
                await self._send_typing(channel_id)
                await record_pacing_event(
                    self.pool,
                    user_id=user.id,
                    message_ids=[],
                    source="live",
                    decision="typing_start",
                    reason="started paced initial typing indicator",
                    signal_snapshot={"channel_id": channel_id, "pulse_s": pulse_interval_s},
                    preference_snapshot=preferences,
                    wait_ms=int(round(pulse_interval_s * 1000)),
                )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=pulse_interval_s)
            except TimeoutError:
                pass

    async def _maybe_llm_decision(
        self,
        user: User,
        message_ids: list[UUID],
        source: str,
        signals: list[MessageSignal],
        signal_snapshot: dict[str, Any],
        preferences: dict[str, Any],
    ) -> PacingDecision | None:
        if not self.settings.discord_pacing_llm_judgement_enabled:
            return None
        ambiguity = self._ambiguity_score(signals, source)
        if ambiguity < self.settings.discord_pacing_llm_min_ambiguity:
            return None
        if not await is_under_cap(self.pool, "text"):
            await self._record_llm_fallback(user, message_ids, source, signal_snapshot, preferences, "text spend cap exceeded")
            return self._decision(
                "answer",
                "LLM pacing judgement skipped because text spend cap is exhausted",
                signal_snapshot | {"ambiguity": ambiguity},
                preferences,
                llm_judgement={"fallback": "spend_cap"},
            )

        try:
            response = await self._llm_response(signals, source, signal_snapshot, preferences)
            await record_anthropic_haiku_text_response_cost(self.pool, _attr(response, "usage", {}))
            judgement = self._parse_llm_judgement(_response_text(response))
            judgement["ambiguity"] = ambiguity
            return self._decision_from_llm(judgement, user, signals, signal_snapshot, preferences)
        except Exception as exc:
            await self._record_llm_fallback(user, message_ids, source, signal_snapshot, preferences, str(exc))
            return self._decision(
                "answer",
                "LLM pacing judgement failed; falling back to substantive answer",
                signal_snapshot | {"ambiguity": ambiguity},
                preferences,
                llm_judgement={"fallback": "error", "error": str(exc)},
            )

    async def _llm_response(
        self,
        signals: list[MessageSignal],
        source: str,
        signal_snapshot: dict[str, Any],
        preferences: Mapping[str, Any],
    ) -> Any:
        client = self._llm_client
        if client is None:
            api_key = self.settings.anthropic_api_key.get_secret_value()
            if _placeholder_key(api_key):
                raise RuntimeError("placeholder Anthropic API key")
            client = anthropic.AsyncAnthropic(api_key=api_key)
        payload = {
            "source": source,
            "signals": signal_snapshot,
            "preferences": preferences,
            "messages": [
                {
                    "charge": signal.charge,
                    "content": signal.content,
                    "sent_at": signal.sent_at.isoformat(),
                    "media_type": signal.media_type,
                }
                for signal in signals
            ],
        }
        return await client.messages.create(
            model=self.settings.scoring_model,
            max_tokens=180,
            system=[{"type": "text", "text": LLM_PACING_SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": json.dumps(payload, default=str)}],
        )

    def _parse_llm_judgement(self, text: str) -> dict[str, Any]:
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("LLM pacing judgement was not an object")
        action = str(data.get("action", "")).strip().lower()
        if action not in LLM_ALLOWED_ACTIONS:
            raise ValueError(f"invalid LLM pacing action: {action}")
        reason = str(data.get("reason", "")).strip()
        if not reason:
            raise ValueError("LLM pacing judgement missing reason")
        wait_s = data.get("wait_s", 0)
        try:
            wait_s = float(wait_s or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("LLM pacing wait_s was not numeric") from exc
        reaction = data.get("reaction")
        if reaction is not None:
            reaction = str(reaction).strip() or None
        return {"action": action, "reason": reason, "wait_s": wait_s, "reaction": reaction}

    def _decision_from_llm(
        self,
        judgement: dict[str, Any],
        user: User,
        signals: list[MessageSignal],
        signal_snapshot: dict[str, Any],
        preferences: dict[str, Any],
    ) -> PacingDecision:
        action = judgement["action"]
        reason = f"LLM pacing judgement: {judgement['reason']}"
        if action == "wait":
            wait_s = min(max(float(judgement["wait_s"]), float(preferences["min_wait_s"])), float(preferences["max_wait_s"]))
            return self._decision("wait", reason, signal_snapshot, preferences, wait_s=wait_s, llm_judgement=judgement)
        if action == "react":
            reaction = judgement.get("reaction")
            allowed_reaction = self._reaction_for(user.id, signals, preferences)
            if reaction not in {ACK_REACTION, CARE_REACTION} or allowed_reaction is None:
                raise ValueError("LLM reaction failed deterministic sparse reaction policy")
            return self._decision("react", reason, signal_snapshot, preferences, reaction=reaction, llm_judgement=judgement)
        if action == "silence":
            return self._decision("silence", reason, signal_snapshot, preferences, llm_judgement=judgement)
        return self._decision("answer", reason, signal_snapshot, preferences, llm_judgement=judgement)

    async def _record_llm_fallback(
        self,
        user: User,
        message_ids: list[UUID],
        source: str,
        signal_snapshot: dict[str, Any],
        preferences: dict[str, Any],
        error: str,
    ) -> None:
        await record_pacing_event(
            self.pool,
            user_id=user.id,
            message_ids=message_ids,
            source=source,
            decision="fallback",
            reason="LLM pacing judgement fallback",
            signal_snapshot=signal_snapshot,
            preference_snapshot=preferences,
            llm_judgement={"error": error},
        )

    def _decision(
        self,
        action: PacingAction,
        reason: str,
        signal_snapshot: dict[str, Any],
        preference_snapshot: dict[str, Any],
        *,
        wait_s: float = 0.0,
        reaction: str | None = None,
        llm_judgement: dict[str, Any] | None = None,
    ) -> PacingDecision:
        return PacingDecision(
            action=action,
            reason=reason,
            wait_s=wait_s,
            reaction=reaction,
            signal_snapshot=signal_snapshot,
            preference_snapshot=dict(preference_snapshot),
            llm_judgement=llm_judgement,
        )

    def _signal_snapshot(self, signals: list[MessageSignal], source: str) -> dict[str, Any]:
        return {
            "source": source,
            "message_count": len(signals),
            "charges": sorted({signal.charge for signal in signals}),
            "has_media": any(signal.media_type for signal in signals),
            "latest_message_at": max((signal.sent_at for signal in signals), default=None),
            "content_chars": sum(len(signal.content) for signal in signals),
        }

    def _typing_wait_s(self, state: TypingState, preferences: Mapping[str, Any]) -> float:
        elapsed_s = max(0.0, (self._now() - state.first_seen_at).total_seconds())
        remaining_cap_s = float(preferences["max_typing_wait_s"]) - elapsed_s
        return min(
            float(self.settings.discord_pacing_typing_extend_s),
            float(preferences["max_wait_s"]),
            max(0.0, remaining_cap_s),
        )

    def _burst_wait_s(self, signals: list[MessageSignal], preferences: Mapping[str, Any]) -> float:
        latest_at = max(signal.sent_at for signal in signals)
        age_s = max(0.0, (self._now() - latest_at).total_seconds())
        burst_window_s = float(preferences["burst_window_s"])
        if age_s >= burst_window_s:
            return 0.0
        return min(
            float(preferences["max_wait_s"]),
            max(float(preferences["min_wait_s"]), burst_window_s - age_s),
        )

    def _reaction_for(self, user_id: UUID, signals: list[MessageSignal], preferences: Mapping[str, Any]) -> str | None:
        if not preferences["reactions_enabled"]:
            return None
        if not self.settings.discord_pacing_reactions_enabled:
            return None
        if not self._reaction_budget_available(user_id, preferences):
            return None
        if len(signals) != 1 or signals[0].charge != "routine":
            return None
        text = signals[0].content.strip().lower()
        if not text or "?" in text or len(text) > 80:
            return None
        if text in {"thanks", "thank you", "ty", "got it", "ok", "okay", "sounds good", "makes sense"}:
            return ACK_REACTION
        if text in {"love this", "i appreciate you", "appreciate you"}:
            return CARE_REACTION
        return None

    def _reaction_budget_available(self, user_id: UUID, preferences: Mapping[str, Any]) -> bool:
        now = self._now()
        day_limit = int(preferences["reaction_daily_limit"])
        if day_limit <= 0:
            return False
        if self._reaction_counts.get((user_id, now.date()), 0) >= day_limit:
            return False
        last = self._last_reaction_at.get(user_id)
        if last is not None and (now - last).total_seconds() < self.settings.discord_pacing_reaction_cooldown_s:
            return False
        return True

    def _note_reaction(self, user_id: UUID) -> None:
        now = self._now()
        self._last_reaction_at[user_id] = now
        key = (user_id, now.date())
        self._reaction_counts[key] = self._reaction_counts.get(key, 0) + 1

    def _is_low_content_ack(self, signals: list[MessageSignal]) -> bool:
        if len(signals) != 1 or signals[0].charge != "routine":
            return False
        text = signals[0].content.strip().lower()
        return text in {"thanks", "thank you", "ty", "got it", "ok", "okay", "sounds good", "makes sense"}

    def _ambiguity_score(self, signals: list[MessageSignal], source: str) -> float:
        if source != "live" or not signals:
            return 0.0
        if any(signal.charge != "routine" or signal.media_type for signal in signals):
            return 0.0
        text = "\n".join(signal.content.strip() for signal in signals if signal.content.strip())
        if not text or self._is_low_content_ack(signals):
            return 0.0
        score = 0.0
        if len(signals) > 1:
            score += 0.35
        if "?" not in text:
            score += 0.20
        if len(text) < 240:
            score += 0.15
        if text.endswith(("...", ",", " and", " but", " so")):
            score += 0.20
        return min(score, 1.0)
