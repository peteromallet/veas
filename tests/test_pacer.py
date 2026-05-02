from __future__ import annotations
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.config import get_settings
from app.models.user import User
from app.services.pacer import DiscordPacer


pytestmark = pytest.mark.anyio


def test_typing_defaults_start_quickly(app_env) -> None:
    settings = get_settings()

    assert settings.discord_pacing_initial_typing_min_s == 0.2
    assert settings.discord_pacing_initial_typing_max_s == 1.2
    assert settings.discord_pacing_thinking_typing_start_s == 0.4
    assert settings.discord_pacing_answer_typing_min_s == 0.4
    assert settings.discord_pacing_composition_jitter_ratio == 0.0
    assert settings.discord_pacing_incremental_typing_pulse_min_gap_s == 1.0


def test_composition_duration_uses_length_clamps_cap_and_deterministic_jitter(fake_pool) -> None:
    preferences = {
        "answer_chars_per_s": 10,
        "answer_typing_min_s": 0.5,
        "answer_typing_max_s": 10,
        "max_typing_wait_s": 4,
    }
    pacer = DiscordPacer(fake_pool)

    assert pacer.composition_duration_s("x", preferences) == 0.5
    assert pacer.composition_duration_s("x" * 20, preferences) == 2.0
    assert pacer.composition_duration_s("x" * 100, preferences) == 4.0
    assert pacer.composition_duration_s("x" * 20, preferences | {"answer_chars_per_s": 20}) == 1.0

    settings = get_settings().model_copy(update={"discord_pacing_composition_jitter_ratio": 0.25})
    jittered = DiscordPacer(fake_pool, settings=settings, random_float=lambda: 1.0)
    assert jittered.composition_duration_s("x" * 20, preferences) == 2.5
    jittered_low = DiscordPacer(fake_pool, settings=settings, random_float=lambda: 0.0)
    assert jittered_low.composition_duration_s("x" * 20, preferences) == 1.5


def _seed_user(fake_pool, *, preferences: dict | None = None) -> User:
    user = User(
        id=uuid4(),
        name="Maya",
        phone="15555550100",
        timezone="UTC",
        pacing_preferences=preferences or {},
    )
    fake_pool.users[user.id] = {
        "id": user.id,
        "name": user.name,
        "phone": user.phone,
        "timezone": user.timezone,
        "onboarding_state": "welcomed",
        "pacing_preferences": preferences or {},
    }
    return user


def _seed_message(
    fake_pool,
    user: User,
    *,
    content: str = "hello",
    charge: str = "routine",
    sent_at: datetime,
    media_type: str | None = None,
):
    message_id = uuid4()
    fake_pool.messages[message_id] = {
        "id": message_id,
        "direction": "inbound",
        "sender_id": user.id,
        "recipient_id": None,
        "content": content,
        "processing_state": "raw",
        "sent_at": sent_at,
        "charge": charge,
        "whatsapp_message_id": f"wa-{message_id}",
        "media_type": media_type,
        "media_url": None,
        "media_duration_seconds": None,
        "media_analysis": None,
        "edit_history": None,
        "edited_at": None,
        "deleted_at": None,
    }
    return message_id


class _FakeMessagesClient:
    def __init__(self, text: str, *, usage=None) -> None:
        self.text = text
        self.usage = usage or SimpleNamespace(input_tokens=1000, output_tokens=1000)
        self.calls = []
        self.messages = self

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self.text)],
            usage=self.usage,
        )


async def test_pacer_waits_while_user_is_typing_and_records_event(fake_pool) -> None:
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    user = _seed_user(fake_pool)
    message_id = _seed_message(fake_pool, user, content="one more thought", sent_at=now - timedelta(seconds=10))
    pacer = DiscordPacer(fake_pool, now=lambda: now)
    pacer.mark_user_typing(user.id, channel_id="channel-1", at=now)

    decision = await pacer.decide_and_record(user, [message_id], source="live")

    assert decision.action == "wait"
    assert decision.wait_s == 2.0
    assert "actively composing" in decision.reason
    event = next(iter(fake_pool.pacing_events.values()))
    assert event["decision"] == "wait"
    assert event["wait_ms"] == 2000
    assert event["signal_snapshot"]["typing_active"] is True


async def test_pacer_waits_to_coalesce_recent_live_burst(fake_pool) -> None:
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    user = _seed_user(fake_pool)
    message_id = _seed_message(fake_pool, user, content="first line", sent_at=now - timedelta(seconds=1))
    pacer = DiscordPacer(fake_pool, now=lambda: now)

    decision = await pacer.decide(user, [message_id], source="live")

    assert decision.action == "wait"
    assert decision.wait_s >= decision.preference_snapshot["min_wait_s"]
    assert "burst" in decision.reason


@pytest.mark.parametrize(
    ("source", "charge", "media_type", "reason_fragment"),
    [
        ("live", "crisis", None, "crisis"),
        ("live", "charged", None, "charged"),
        ("media", "routine", "voice", "media"),
        ("catch_up", "routine", None, "stale/offline"),
        ("recovery", "routine", None, "stale/offline"),
    ],
)
async def test_pacer_answer_gates_for_safety_media_and_stale_sources(
    fake_pool,
    source: str,
    charge: str,
    media_type: str | None,
    reason_fragment: str,
) -> None:
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    user = _seed_user(fake_pool)
    message_id = _seed_message(
        fake_pool,
        user,
        content="thanks",
        charge=charge,
        media_type=media_type,
        sent_at=now - timedelta(seconds=10),
    )
    pacer = DiscordPacer(fake_pool, now=lambda: now)

    decision = await pacer.decide(user, [message_id], source=source)

    assert decision.action == "answer"
    assert reason_fragment in decision.reason


async def test_pacer_reacts_sparingly_then_silences_ack_during_cooldown(fake_pool) -> None:
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    user = _seed_user(fake_pool)
    first_id = _seed_message(fake_pool, user, content="thanks", sent_at=now - timedelta(seconds=10))
    second_id = _seed_message(fake_pool, user, content="ok", sent_at=now - timedelta(seconds=9))
    pacer = DiscordPacer(fake_pool, now=lambda: now)

    first = await pacer.decide_and_record(user, [first_id], source="live")
    second = await pacer.decide_and_record(user, [second_id], source="live")

    assert first.action == "react"
    assert first.reaction == "👍"
    assert second.action == "silence"
    assert [event["decision"] for event in fake_pool.pacing_events.values()] == ["react", "silence"]


async def test_answer_typing_suppresses_indicator_while_user_is_typing(fake_pool) -> None:
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    user = _seed_user(fake_pool, preferences={"answer_typing_max_s": 10})
    typing_sent_at = []

    def current_time() -> datetime:
        return now

    async def sleep(seconds: float) -> None:
        nonlocal now
        now += timedelta(seconds=seconds)

    async def send_typing(channel_id: str) -> None:
        typing_sent_at.append((channel_id, now))

    pacer = DiscordPacer(fake_pool, send_typing=send_typing, sleep=sleep, now=current_time)
    pacer.mark_user_typing(user.id, channel_id="channel-1", at=now)

    waited_s = await pacer.perform_answer_typing(user, "channel-1", "x" * 200)

    assert waited_s > 0
    assert len(typing_sent_at) == 1
    assert typing_sent_at[0][1] > datetime(2026, 5, 1, 12, 0, tzinfo=UTC) + timedelta(seconds=4)
    decisions = [event["decision"] for event in fake_pool.pacing_events.values()]
    assert "typing_wait" in decisions
    assert "typing_start" in decisions
    assert "typing_stop" in decisions


async def test_bot_typing_pulses_leave_visible_gaps(fake_pool) -> None:
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    user = _seed_user(
        fake_pool,
        preferences={"answer_typing_max_s": 25, "answer_chars_per_s": 10, "max_typing_wait_s": 25},
    )
    typing_sent_at = []

    def current_time() -> datetime:
        return now

    async def sleep(seconds: float) -> None:
        nonlocal now
        now += timedelta(seconds=seconds)

    async def send_typing(channel_id: str) -> None:
        typing_sent_at.append((channel_id, now))

    pacer = DiscordPacer(fake_pool, send_typing=send_typing, sleep=sleep, now=current_time)
    await pacer.perform_answer_typing(user, "channel-1", "x" * 1000)

    assert len(typing_sent_at) == 3
    assert typing_sent_at[1][1] - typing_sent_at[0][1] >= timedelta(seconds=11)
    assert typing_sent_at[2][1] - typing_sent_at[1][1] >= timedelta(seconds=11)
    typing_starts = [event for event in fake_pool.pacing_events.values() if event["decision"] == "typing_start"]
    assert typing_starts[0]["signal_snapshot"]["composition_s"] == 25
    assert typing_starts[0]["signal_snapshot"]["send_kind"] == "final"
    assert typing_starts[0]["signal_snapshot"]["part_index"] is None
    typing_stops = [event for event in fake_pool.pacing_events.values() if event["decision"] == "typing_stop"]
    assert len(typing_stops) == 2
    assert all(event["signal_snapshot"]["pause_s"] == 3 for event in typing_stops)


async def test_incremental_followup_typing_uses_short_gap(fake_pool) -> None:
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    user = _seed_user(
        fake_pool,
        preferences={"answer_typing_min_s": 0.4, "answer_chars_per_s": 10, "answer_typing_max_s": 10},
    )
    typing_sent_at = []

    def current_time() -> datetime:
        return now

    async def sleep(seconds: float) -> None:
        nonlocal now
        now += timedelta(seconds=seconds)

    async def send_typing(channel_id: str) -> None:
        typing_sent_at.append((channel_id, now))

    pacer = DiscordPacer(fake_pool, send_typing=send_typing, sleep=sleep, now=current_time)
    await pacer.perform_send_typing(user, "channel-1", "Six.", send_kind="incremental_first", part_index=1)
    waited_s = await pacer.perform_send_typing(
        user,
        "channel-1",
        "x" * 30,
        send_kind="incremental_next",
        part_index=2,
    )

    assert len(typing_sent_at) == 2
    assert typing_sent_at[1][1] - typing_sent_at[0][1] < timedelta(seconds=11)
    assert typing_sent_at[1][1] - typing_sent_at[0][1] >= timedelta(seconds=1)
    assert waited_s == pytest.approx(4.1)
    events = list(fake_pool.pacing_events.values())
    assert events[-1]["decision"] == "typing_start"
    assert events[-1]["reason"] == "started paced incremental typing indicator"
    assert events[-1]["signal_snapshot"]["send_kind"] == "incremental_next"
    assert events[-1]["signal_snapshot"]["composition_s"] == 3.0


async def test_incremental_followup_rechecks_user_typing_after_rhythm(fake_pool) -> None:
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    user = _seed_user(
        fake_pool,
        preferences={"answer_typing_min_s": 0.4, "max_typing_wait_s": 5, "typing_grace_s": 10},
    )
    typing_sent_at = []
    sleep_calls = []

    def current_time() -> datetime:
        return now

    async def sleep(seconds: float) -> None:
        nonlocal now
        sleep_calls.append(seconds)
        now += timedelta(seconds=seconds)
        if len(sleep_calls) == 1:
            pacer.mark_user_typing(user.id, channel_id="channel-1", at=now)

    async def send_typing(channel_id: str) -> None:
        typing_sent_at.append((channel_id, now))

    pacer = DiscordPacer(fake_pool, send_typing=send_typing, sleep=sleep, now=current_time)
    waited_s = await pacer.perform_send_typing(user, "channel-1", "Seven.", send_kind="incremental_next", part_index=2)

    assert waited_s >= 5
    assert typing_sent_at == []
    assert "typing_wait" in [event["decision"] for event in fake_pool.pacing_events.values()]


async def test_llm_judgement_can_silence_ambiguous_live_burst_and_records_cost(fake_pool) -> None:
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    user = _seed_user(fake_pool)
    ids = [
        _seed_message(fake_pool, user, content="I guess", sent_at=now - timedelta(seconds=10)),
        _seed_message(fake_pool, user, content="maybe just leave it", sent_at=now - timedelta(seconds=9)),
    ]
    client = _FakeMessagesClient('{"action":"silence","reason":"user is closing the loop","wait_s":0,"reaction":null}')
    pacer = DiscordPacer(fake_pool, llm_client=client, now=lambda: now)

    decision = await pacer.decide_and_record(user, ids, source="live")

    assert decision.action == "silence"
    assert decision.llm_judgement["action"] == "silence"
    assert len(client.calls) == 1
    assert fake_pool.llm_spend_log["text"] > 0
    event = next(iter(fake_pool.pacing_events.values()))
    assert event["decision"] == "silence"
    assert event["llm_judgement"]["action"] == "silence"


async def test_llm_judgement_continues_when_spend_is_above_cap(fake_pool) -> None:
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    user = _seed_user(fake_pool)
    ids = [
        _seed_message(fake_pool, user, content="I guess", sent_at=now - timedelta(seconds=10)),
        _seed_message(fake_pool, user, content="maybe just leave it", sent_at=now - timedelta(seconds=9)),
    ]
    fake_pool.llm_spend_log["text"] = Decimal("999")
    client = _FakeMessagesClient('{"action":"silence","reason":"unused","wait_s":0,"reaction":null}')
    pacer = DiscordPacer(fake_pool, llm_client=client, now=lambda: now)

    decision = await pacer.decide(user, ids, source="live")

    assert decision.action == "silence"
    assert len(client.calls) == 1


async def test_llm_judgement_invalid_json_records_fallback(fake_pool) -> None:
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    user = _seed_user(fake_pool)
    ids = [
        _seed_message(fake_pool, user, content="I guess", sent_at=now - timedelta(seconds=10)),
        _seed_message(fake_pool, user, content="maybe just leave it", sent_at=now - timedelta(seconds=9)),
    ]
    pacer = DiscordPacer(fake_pool, llm_client=_FakeMessagesClient("not json"), now=lambda: now)

    decision = await pacer.decide(user, ids, source="live")

    assert decision.action == "answer"
    event = next(iter(fake_pool.pacing_events.values()))
    assert event["decision"] == "fallback"
    assert "Expecting value" in event["llm_judgement"]["error"]


async def test_llm_judgement_is_not_called_for_deterministic_crisis_gate(fake_pool) -> None:
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    user = _seed_user(fake_pool)
    message_id = _seed_message(
        fake_pool,
        user,
        content="I might hurt myself tonight",
        charge="crisis",
        sent_at=now - timedelta(seconds=10),
    )
    client = _FakeMessagesClient('{"action":"silence","reason":"unsafe","wait_s":0,"reaction":null}')
    pacer = DiscordPacer(fake_pool, llm_client=client, now=lambda: now)

    decision = await pacer.decide(user, [message_id], source="live")

    assert decision.action == "answer"
    assert len(client.calls) == 0
