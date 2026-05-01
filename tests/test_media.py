import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from app.models.user import User
from app.services.deletion import purge_expired_deletions
from app.services.inbound import process_inbound
from app.services.transcription import handle_voice
from app.services.vision import handle_image


pytestmark = pytest.mark.anyio

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "whatsapp"


class Recorder:
    def __init__(self) -> None:
        self.calls = []

    async def add(self, user_id, message_id, user):
        self.calls.append((user_id, message_id, user))


def _user_and_message(fake_pool):
    user = User(id=uuid4(), name="Maya", phone="15555550100", timezone="UTC")
    fake_pool.users[user.id] = {
        "id": user.id,
        "name": user.name,
        "phone": user.phone,
        "timezone": user.timezone,
    }
    message_id = uuid4()
    fake_pool.messages[message_id] = {
        "id": message_id,
        "direction": "inbound",
        "sender_id": user.id,
        "recipient_id": None,
        "content": None,
        "processing_state": "raw",
        "sent_at": datetime.now(UTC),
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
    return user, message_id


async def test_voice_success_persists_transcript_and_keeps_raw(fake_pool, monkeypatch) -> None:
    user, message_id = _user_and_message(fake_pool)
    recorder = Recorder()

    async def fetch_media(media_id):
        return b"audio", "audio/ogg"

    async def upload_media(bucket, object_path, content, content_type):
        return f"{bucket}/{object_path}"

    async def transcribe(audio_bytes, content_type):
        return "clear transcript"

    monkeypatch.setattr("app.services.whatsapp.fetch_media", fetch_media)
    monkeypatch.setattr("app.services.storage.upload_media", upload_media)
    monkeypatch.setattr("app.services.transcription._groq_transcribe", transcribe)

    await handle_voice(fake_pool, message_id, "media-audio", user, recorder, duration=42)
    message = fake_pool.messages[message_id]

    assert message["content"] == "clear transcript"
    assert message["media_type"] == "voice"
    assert message["media_url"].endswith(f"voice/{message_id}")
    assert message["media_duration_seconds"] == 42
    assert message["processing_state"] == "raw"
    assert recorder.calls


async def test_voice_double_failure_expires_with_audio_retained(fake_pool, monkeypatch) -> None:
    user, message_id = _user_and_message(fake_pool)
    attempts = 0
    sent = []

    async def fetch_media(media_id):
        return b"audio", "audio/ogg"

    async def upload_media(bucket, object_path, content, content_type):
        return f"{bucket}/{object_path}"

    async def fail(audio_bytes, content_type):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("no transcript")

    async def no_sleep(seconds):
        return None

    async def fake_send(pool, recipient, content, *, template_fallback=None, bot_turn_id=None, ignore_pause=False):
        sent.append((recipient, content, template_fallback))
        return uuid4()

    monkeypatch.setattr("app.services.whatsapp.fetch_media", fetch_media)
    monkeypatch.setattr("app.services.storage.upload_media", upload_media)
    monkeypatch.setattr("app.services.transcription._groq_transcribe", fail)
    monkeypatch.setattr("app.services.transcription.asyncio.sleep", no_sleep)
    monkeypatch.setattr("app.services.transcription.send_outbound", fake_send)

    await handle_voice(fake_pool, message_id, "media-audio", user)
    message = fake_pool.messages[message_id]

    assert attempts == 2
    assert message["processing_state"] == "expired"
    assert message["media_analysis"]["_pipeline"]["attempts"] == 2
    assert message["media_url"].endswith(f"voice/{message_id}")
    assert sent[0][2].name == "media_failure"


async def test_voice_cap_hit_skips_transcription_and_keeps_raw(fake_pool, monkeypatch) -> None:
    user, message_id = _user_and_message(fake_pool)
    fake_pool.llm_spend_log["transcription"] = Decimal("3")
    recorder = Recorder()
    sent = []

    async def fetch_media(media_id):
        return b"audio", "audio/ogg"

    async def upload_media(bucket, object_path, content, content_type):
        return f"{bucket}/{object_path}"

    async def should_not_run(audio_bytes, content_type):
        raise AssertionError("transcription should be skipped")

    async def fake_send(pool, recipient, content, *, template_fallback=None, bot_turn_id=None, ignore_pause=False):
        sent.append((recipient, content, template_fallback))
        return uuid4()

    monkeypatch.setattr("app.services.whatsapp.fetch_media", fetch_media)
    monkeypatch.setattr("app.services.storage.upload_media", upload_media)
    monkeypatch.setattr("app.services.transcription._groq_transcribe", should_not_run)
    monkeypatch.setattr("app.services.transcription.send_outbound", fake_send)

    await handle_voice(fake_pool, message_id, "media-audio", user, recorder)
    message = fake_pool.messages[message_id]

    assert message["content"] == "I can't transcribe right now -- can you send it as text?"
    assert message["media_analysis"] == {"unavailable": "daily_cap"}
    assert message["processing_state"] == "expired"
    assert recorder.calls == []
    assert sent[0][2].name == "media_failure"


async def test_image_cap_hit_retains_media_and_skips_vision(fake_pool, monkeypatch) -> None:
    user, message_id = _user_and_message(fake_pool)
    fake_pool.llm_spend_log["vision"] = Decimal("3")
    sent = []

    async def fetch_media(media_id):
        return b"image", "image/jpeg"

    async def upload_media(bucket, object_path, content, content_type):
        return f"{bucket}/{object_path}"

    async def should_not_run(image_bytes, content_type):
        raise AssertionError("vision should be skipped")

    async def fake_send(pool, recipient, content, *, template_fallback=None, bot_turn_id=None, ignore_pause=False):
        sent.append((recipient, content, template_fallback))
        return uuid4()

    monkeypatch.setattr("app.services.whatsapp.fetch_media", fetch_media)
    monkeypatch.setattr("app.services.storage.upload_media", upload_media)
    monkeypatch.setattr("app.services.vision._openai_analyze", should_not_run)
    monkeypatch.setattr("app.services.vision.send_outbound", fake_send)

    await handle_image(fake_pool, message_id, "media-image", user)
    message = fake_pool.messages[message_id]

    assert message["media_url"].endswith(f"image/{message_id}")
    assert message["media_analysis"] == {"unavailable": "daily_cap"}
    assert message["processing_state"] == "expired"
    assert sent[0][2].name == "media_failure"


async def test_image_vision_failure_retains_media_and_keeps_raw(fake_pool, monkeypatch) -> None:
    user, message_id = _user_and_message(fake_pool)
    sent = []

    async def fetch_media(media_id):
        return b"image", "image/jpeg"

    async def upload_media(bucket, object_path, content, content_type):
        return f"{bucket}/{object_path}"

    async def fail(image_bytes, content_type):
        raise RuntimeError("vision down")

    async def fake_send(pool, recipient, content, *, template_fallback=None, bot_turn_id=None, ignore_pause=False):
        sent.append((recipient, content, template_fallback))
        return uuid4()

    monkeypatch.setattr("app.services.whatsapp.fetch_media", fetch_media)
    monkeypatch.setattr("app.services.storage.upload_media", upload_media)
    monkeypatch.setattr("app.services.vision._openai_analyze", fail)
    monkeypatch.setattr("app.services.vision.send_outbound", fake_send)

    await handle_image(fake_pool, message_id, "media-image", user)
    message = fake_pool.messages[message_id]

    assert message["media_url"].endswith(f"image/{message_id}")
    assert message["media_analysis"] == {"error": "vision_failed"}
    assert message["processing_state"] == "expired"
    assert sent[0][2].name == "media_failure"


async def test_edit_runs_before_idempotent_insert(fake_pool) -> None:
    await process_inbound(fake_pool, json.loads((FIXTURE_DIR / "inbound_text.json").read_text()))
    await process_inbound(fake_pool, json.loads((FIXTURE_DIR / "inbound_edit.json").read_text()))

    message = next(iter(fake_pool.messages.values()))
    assert message["edited_at"] is not None
    assert message["edit_history"][0]["content"] == "I need to talk this through."
    assert message["content"] == "I need to talk this through carefully."


async def test_delete_sets_deleted_at_and_purge_rewrites_content(fake_pool) -> None:
    await process_inbound(fake_pool, json.loads((FIXTURE_DIR / "inbound_text.json").read_text()))
    await process_inbound(fake_pool, json.loads((FIXTURE_DIR / "inbound_delete.json").read_text()))

    message = next(iter(fake_pool.messages.values()))
    assert message["deleted_at"] is not None
    message["deleted_at"] = datetime.now(UTC) - timedelta(hours=25)

    await purge_expired_deletions(fake_pool)

    assert message["content"] == "[deleted]"
