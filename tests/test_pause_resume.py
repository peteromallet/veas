from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.services import system_state
from app.services.inbound import process_inbound

pytestmark = pytest.mark.anyio


class RecordingCoalescer:
    def __init__(self) -> None:
        self.calls = []

    async def add(self, *args) -> None:
        self.calls.append(args)


def _seed_user(pool, *, name: str, phone: str):
    user_id = uuid4()
    pool.users[user_id] = {
        "id": user_id,
        "name": name,
        "phone": phone,
        "timezone": "UTC",
        "onboarding_state": "welcomed",
        "weekly_summary_enabled": True,
        "weekly_summary_day": 1,
        "weekly_summary_time": "09:00",
    }
    return user_id


def _payload(user, message):
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "contacts": [{"wa_id": user["phone"], "profile": {"name": user["name"]}}],
                            "messages": [message],
                        }
                    }
                ]
            }
        ]
    }


def _message(user, wa_id: str, wa_type: str, body=None):
    base = {"from": user["phone"], "id": wa_id, "timestamp": "1777550400", "type": wa_type}
    if wa_type == "text":
        base["text"] = {"body": body}
    elif wa_type == "audio":
        base["audio"] = {"id": f"media-{wa_id}", "duration": 4}
    elif wa_type == "image":
        base["image"] = {"id": f"media-{wa_id}"}
    return base


async def test_pause_from_either_partner_sets_global_state_supersedes_and_notifies(fake_pool, app_env, monkeypatch):
    user_a_id = _seed_user(fake_pool, name="Maya", phone="15555550100")
    user_b_id = _seed_user(fake_pool, name="Ben", phone="15555550101")
    now = datetime.now(UTC)
    for job_type in ("checkin", "weekly_summary", "watch_item_due", "oob_review", "deferred_turn", "heartbeat"):
        job_id = uuid4()
        fake_pool.scheduled_jobs[job_id] = {
            "id": job_id,
            "user_id": user_a_id if job_type != "heartbeat" else None,
            "job_type": job_type,
            "scheduled_for": now,
            "context": {},
            "status": "pending",
            "claimed_at": None,
            "claimed_by": None,
        }
    sent = []

    async def fake_send(pool, recipient, content, *, template_fallback=None, bot_turn_id=None, ignore_pause=False):
        sent.append((recipient.id, template_fallback.name, template_fallback.params, ignore_pause))
        return uuid4()

    monkeypatch.setattr("app.services.inbound.send_outbound", fake_send)
    coalescer = RecordingCoalescer()
    user_b = fake_pool.users[user_b_id]

    await process_inbound(fake_pool, _payload(user_b, _message(user_b, "wamid.pause", "text", "/pause")), coalescer)

    assert await system_state.is_paused(fake_pool)
    assert fake_pool.system_state["global_pause"]["paused_by_user_id"] == user_b_id
    assert coalescer.calls == []
    assert len(sent) == 2
    assert {row[0] for row in sent} == {user_a_id, user_b_id}
    assert all(row[1] == "pause_confirmation" and row[3] is True for row in sent)
    statuses = {job["job_type"]: job["status"] for job in fake_pool.scheduled_jobs.values()}
    assert statuses["heartbeat"] == "pending"
    for job_type in ("checkin", "weekly_summary", "watch_item_due", "oob_review", "deferred_turn"):
        assert statuses[job_type] == "superseded"


async def test_paused_text_document_voice_and_image_persist_without_enqueue(fake_pool, app_env, monkeypatch):
    user_a_id = _seed_user(fake_pool, name="Maya", phone="15555550100")
    _seed_user(fake_pool, name="Ben", phone="15555550101")
    await system_state.pause(fake_pool, user_a_id)
    user = fake_pool.users[user_a_id]
    coalescer = RecordingCoalescer()
    media_calls = []

    async def fake_voice(pool, message_id, media_id, user_obj, passed_coalescer, duration=None):
        media_calls.append(("voice", message_id, passed_coalescer))
        pool.messages[message_id]["content"] = "transcribed"

    async def fake_image(pool, message_id, media_id, user_obj, passed_coalescer):
        media_calls.append(("image", message_id, passed_coalescer))
        pool.messages[message_id]["media_analysis"] = {"description": "image"}

    monkeypatch.setattr("app.services.inbound.handle_voice", fake_voice)
    monkeypatch.setattr("app.services.inbound.handle_image", fake_image)

    for wa_id, wa_type, body in (
        ("paused.text", "text", "hello while paused"),
        ("paused.document", "document", None),
        ("paused.voice", "audio", None),
        ("paused.image", "image", None),
    ):
        await process_inbound(fake_pool, _payload(user, _message(user, wa_id, wa_type, body)), coalescer)

    assert {message["whatsapp_message_id"] for message in fake_pool.messages.values()} >= {
        "paused.text",
        "paused.document",
        "paused.voice",
        "paused.image",
    }
    assert coalescer.calls == []
    assert [call[0] for call in media_calls] == ["voice", "image"]
    assert all(call[2] is None for call in media_calls)
    assert all(message["processing_state"] == "raw" for message in fake_pool.messages.values() if message["direction"] == "inbound")


async def test_resume_clears_pause_seeds_weekly_and_does_not_replay_backlog(fake_pool, app_env):
    user_a_id = _seed_user(fake_pool, name="Maya", phone="15555550100")
    _seed_user(fake_pool, name="Ben", phone="15555550101")
    user = fake_pool.users[user_a_id]
    await system_state.pause(fake_pool, user_a_id)
    coalescer = RecordingCoalescer()

    await process_inbound(fake_pool, _payload(user, _message(user, "paused.backlog", "text", "stored only")), coalescer)
    assert coalescer.calls == []

    await process_inbound(fake_pool, _payload(user, _message(user, "resume.command", "text", "/resume")), coalescer)

    assert not await system_state.is_paused(fake_pool)
    assert coalescer.calls == []
    assert any(job["job_type"] == "weekly_summary" and job["status"] == "pending" for job in fake_pool.scheduled_jobs.values())

    await process_inbound(fake_pool, _payload(user, _message(user, "after.resume", "text", "new work")), coalescer)
    assert len(coalescer.calls) == 1
