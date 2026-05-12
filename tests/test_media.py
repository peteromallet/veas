import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from app.models.user import User
from app.services.deletion import purge_expired_deletions
from app.services.inbound import process_inbound
from app.services.transcription import handle_voice
from app.services.vision import handle_image
from app.services.vision import explain_stored_image


pytestmark = pytest.mark.anyio

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "whatsapp"


class Recorder:
    def __init__(self) -> None:
        self.calls = []

    async def add(self, user_id, message_id, user, *, source: str = "live", bot_id: str | None = None):
        self.calls.append((user_id, message_id, user, source))


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
    assert recorder.calls == [(user.id, message_id, user, "media")]


async def test_image_success_persists_analysis_and_enqueues_as_media(fake_pool, monkeypatch) -> None:
    user, message_id = _user_and_message(fake_pool)
    recorder = Recorder()

    async def fetch_media(media_id):
        return b"image", "image/jpeg"

    async def upload_media(bucket, object_path, content, content_type):
        return f"{bucket}/{object_path}"

    async def analyze(image_bytes, content_type):
        return "image description"

    monkeypatch.setattr("app.services.whatsapp.fetch_media", fetch_media)
    monkeypatch.setattr("app.services.storage.upload_media", upload_media)
    monkeypatch.setattr("app.services.vision._openai_analyze", analyze)

    await handle_image(fake_pool, message_id, "media-image", user, recorder)
    message = fake_pool.messages[message_id]

    assert message["media_analysis"]["kind"] == "image"
    assert message["media_analysis"]["provider"] == "openai"
    assert message["media_analysis"]["explanation"] == "image description"
    assert message["media_analysis"]["description"] == "image description"
    assert message["media_url"].endswith(f"image/{message_id}")
    assert recorder.calls == [(user.id, message_id, user, "media")]


async def test_image_openai_failure_falls_back_to_anthropic(fake_pool, monkeypatch) -> None:
    user, message_id = _user_and_message(fake_pool)
    recorder = Recorder()

    async def fetch_media(media_id):
        return b"image", "image/jpeg"

    async def upload_media(bucket, object_path, content, content_type):
        return f"{bucket}/{object_path}"

    async def fail_openai(image_bytes, content_type):
        raise RuntimeError("openai quota")

    async def anthropic_analyze(image_bytes, content_type):
        return {
            "kind": "image",
            "provider": "anthropic",
            "model": "claude-sonnet-4-6",
            "detail": "high",
            "explanation": "fallback image description",
            "description": "fallback image description",
        }

    monkeypatch.setattr("app.services.whatsapp.fetch_media", fetch_media)
    monkeypatch.setattr("app.services.storage.upload_media", upload_media)
    monkeypatch.setattr("app.services.vision._openai_analyze", fail_openai)
    monkeypatch.setattr("app.services.vision._anthropic_analyze", anthropic_analyze)

    await handle_image(fake_pool, message_id, "media-image", user, recorder)
    message = fake_pool.messages[message_id]

    assert message["media_analysis"]["provider"] == "anthropic"
    assert message["media_analysis"]["explanation"] == "fallback image description"
    assert message["processing_state"] == "raw"
    assert recorder.calls == [(user.id, message_id, user, "media")]


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

    async def fake_send(pool, recipient, content, *, template_fallback=None, bot_turn_id=None, ignore_pause=False, bot_id=None, topic_id=None):
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


async def test_voice_spend_over_threshold_still_transcribes(fake_pool, monkeypatch) -> None:
    user, message_id = _user_and_message(fake_pool)
    fake_pool.llm_spend_log["transcription"] = 3
    recorder = Recorder()

    async def fetch_media(media_id):
        return b"audio", "audio/ogg"

    async def upload_media(bucket, object_path, content, content_type):
        return f"{bucket}/{object_path}"

    async def transcribe(audio_bytes, content_type):
        return "still transcribed"

    monkeypatch.setattr("app.services.whatsapp.fetch_media", fetch_media)
    monkeypatch.setattr("app.services.storage.upload_media", upload_media)
    monkeypatch.setattr("app.services.transcription._groq_transcribe", transcribe)

    await handle_voice(fake_pool, message_id, "media-audio", user, recorder)
    message = fake_pool.messages[message_id]

    assert message["content"] == "still transcribed"
    assert message["processing_state"] == "raw"
    assert recorder.calls == [(user.id, message_id, user, "media")]


async def test_image_spend_over_threshold_still_runs_vision(fake_pool, monkeypatch) -> None:
    user, message_id = _user_and_message(fake_pool)
    fake_pool.llm_spend_log["vision"] = 3
    recorder = Recorder()

    async def fetch_media(media_id):
        return b"image", "image/jpeg"

    async def upload_media(bucket, object_path, content, content_type):
        return f"{bucket}/{object_path}"

    async def analyze(image_bytes, content_type):
        return "still analyzed"

    monkeypatch.setattr("app.services.whatsapp.fetch_media", fetch_media)
    monkeypatch.setattr("app.services.storage.upload_media", upload_media)
    monkeypatch.setattr("app.services.vision._openai_analyze", analyze)

    await handle_image(fake_pool, message_id, "media-image", user, recorder)
    message = fake_pool.messages[message_id]

    assert message["media_url"].endswith(f"image/{message_id}")
    assert message["media_analysis"]["explanation"] == "still analyzed"
    assert message["media_analysis"]["description"] == "still analyzed"
    assert message["processing_state"] == "raw"
    assert recorder.calls == [(user.id, message_id, user, "media")]


async def test_image_vision_failure_retains_media_and_keeps_raw(fake_pool, monkeypatch) -> None:
    user, message_id = _user_and_message(fake_pool)
    sent = []

    async def fetch_media(media_id):
        return b"image", "image/jpeg"

    async def upload_media(bucket, object_path, content, content_type):
        return f"{bucket}/{object_path}"

    async def fail(image_bytes, content_type):
        raise RuntimeError("vision down")

    async def fake_send(pool, recipient, content, *, template_fallback=None, bot_turn_id=None, ignore_pause=False, bot_id=None, topic_id=None):
        sent.append((recipient, content, template_fallback))
        return uuid4()

    monkeypatch.setattr("app.services.whatsapp.fetch_media", fetch_media)
    monkeypatch.setattr("app.services.storage.upload_media", upload_media)
    monkeypatch.setattr("app.services.vision._openai_analyze", fail)
    monkeypatch.setattr("app.services.vision._anthropic_analyze", fail)
    monkeypatch.setattr("app.services.vision.send_outbound", fake_send)

    await handle_image(fake_pool, message_id, "media-image", user)
    message = fake_pool.messages[message_id]

    assert message["media_url"].endswith(f"image/{message_id}")
    assert message["media_analysis"]["error"] == "vision_failed"
    assert message["media_analysis"]["detail"] == "RuntimeError"
    assert message["processing_state"] == "expired"
    assert sent[0][2].name == "media_failure"


async def test_process_inbound_text_plus_image_waits_for_vision_before_text_add(
    fake_pool, monkeypatch
) -> None:
    """A single inbound payload that bundles text + image must produce one merged
    coalescer burst whose text-driven add fires only after media_analysis is set,
    so the agentic turn already knows about the image (no split reply)."""

    from typing import NamedTuple

    class _Charge(NamedTuple):
        charge: str

    async def fake_classify_charge(pool, content):
        return _Charge("routine")

    async def fetch_media(media_id):
        return b"image", "image/jpeg"

    async def upload_media(bucket, object_path, content, content_type):
        return f"{bucket}/{object_path}"

    captured_at_text_add: dict = {}

    class _Coalescer:
        def __init__(self) -> None:
            self.calls = []

        async def add(self, user_id, message_id, user, *, source="live", bot_id=None):
            row = fake_pool.messages.get(message_id)
            self.calls.append(
                {
                    "message_id": message_id,
                    "source": source,
                    "media_type": row.get("media_type") if row else None,
                    "media_analysis": row.get("media_analysis") if row else None,
                }
            )
            # When the text item lands in the coalescer, snapshot the image
            # message's media_analysis to prove vision finished first.
            if source == "live":
                for mid, mrow in fake_pool.messages.items():
                    if mrow.get("media_type") == "image":
                        captured_at_text_add["image_media_analysis"] = mrow.get("media_analysis")

    async def analyze(image_bytes, content_type):
        return "A screenshot of a chat with Hannah.\nVisible text: Hannah: are we still on for dinner?"

    monkeypatch.setattr("app.services.inbound.classify_charge", fake_classify_charge)
    monkeypatch.setattr("app.services.whatsapp.fetch_media", fetch_media)
    monkeypatch.setattr("app.services.storage.upload_media", upload_media)
    monkeypatch.setattr("app.services.vision._openai_analyze", analyze)

    coalescer = _Coalescer()
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "contacts": [
                                {"wa_id": "15555550100", "profile": {"name": "Maya"}}
                            ],
                            "messages": [
                                {
                                    "from": "15555550100",
                                    "id": "wamid.text-and-image",
                                    "timestamp": "1700000000",
                                    "type": "text",
                                    "text": {"body": "what does this say?"},
                                },
                                {
                                    "from": "15555550100",
                                    "id": "wamid.text-and-image:att1",
                                    "timestamp": "1700000000",
                                    "type": "image",
                                    "image": {"id": "media-image"},
                                },
                            ],
                        }
                    }
                ]
            }
        ]
    }

    await process_inbound(fake_pool, payload, coalescer)

    # Both items should have arrived at the coalescer (one media, one live).
    sources = sorted(call["source"] for call in coalescer.calls)
    assert sources == ["live", "media"], coalescer.calls

    # Image's coalescer add must run before text's: media first.
    assert coalescer.calls[0]["source"] == "media"
    assert coalescer.calls[1]["source"] == "live"

    # By the time the text-driven add fires, the image's media_analysis has
    # already been written -- so the upcoming agentic turn will see it.
    assert captured_at_text_add.get("image_media_analysis") is not None
    assert (
        captured_at_text_add["image_media_analysis"]["explanation"]
        == "A screenshot of a chat with Hannah.\nVisible text: Hannah: are we still on for dinner?"
    )


async def test_process_inbound_text_plus_image_still_replies_when_vision_fails(
    fake_pool, monkeypatch
) -> None:
    """If vision fails, the text-driven add must still fire (no deadlock), and
    the image's media_analysis must reflect the failure so the LLM can see it."""

    from typing import NamedTuple

    class _Charge(NamedTuple):
        charge: str

    async def fake_classify_charge(pool, content):
        return _Charge("routine")

    async def fetch_media(media_id):
        return b"image", "image/jpeg"

    async def upload_media(bucket, object_path, content, content_type):
        return f"{bucket}/{object_path}"

    async def fail(image_bytes, content_type):
        raise RuntimeError("vision down")

    async def fake_send(pool, recipient, content, *, template_fallback=None, bot_turn_id=None, ignore_pause=False, bot_id=None, topic_id=None):
        return uuid4()

    monkeypatch.setattr("app.services.inbound.classify_charge", fake_classify_charge)
    monkeypatch.setattr("app.services.whatsapp.fetch_media", fetch_media)
    monkeypatch.setattr("app.services.storage.upload_media", upload_media)
    monkeypatch.setattr("app.services.vision._openai_analyze", fail)
    monkeypatch.setattr("app.services.vision._anthropic_analyze", fail)
    monkeypatch.setattr("app.services.vision.send_outbound", fake_send)

    coalescer = Recorder()
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "contacts": [
                                {"wa_id": "15555550100", "profile": {"name": "Maya"}}
                            ],
                            "messages": [
                                {
                                    "from": "15555550100",
                                    "id": "wamid.text-and-image-fail",
                                    "timestamp": "1700000000",
                                    "type": "text",
                                    "text": {"body": "what's this?"},
                                },
                                {
                                    "from": "15555550100",
                                    "id": "wamid.text-and-image-fail:att1",
                                    "timestamp": "1700000000",
                                    "type": "image",
                                    "image": {"id": "media-image"},
                                },
                            ],
                        }
                    }
                ]
            }
        ]
    }

    await process_inbound(fake_pool, payload, coalescer)

    # Vision failure path does not enqueue the image into the coalescer, but
    # the text item still must, so the user gets a reply.
    sources = [call[3] for call in coalescer.calls]
    assert "live" in sources

    # The image row exists with a vision_failed marker -- agentic turn can see it.
    image_rows = [m for m in fake_pool.messages.values() if m.get("media_type") == "image"]
    assert len(image_rows) == 1
    assert image_rows[0]["media_analysis"]["error"] == "vision_failed"
    assert image_rows[0]["media_analysis"]["detail"] == "RuntimeError"


async def test_explain_stored_image_downloads_analyzes_and_persists(fake_pool, monkeypatch) -> None:
    user, message_id = _user_and_message(fake_pool)
    fake_pool.messages[message_id]["media_type"] = "image"
    fake_pool.messages[message_id]["media_url"] = f"mediator-media/image/{message_id}"

    async def download_media(storage_path):
        return b"image", "image/png"

    async def analyze(image_bytes, content_type):
        return {"explanation": "A screenshot of travel plans and a tense message bubble."}

    monkeypatch.setattr("app.services.storage.download_media", download_media)
    monkeypatch.setattr("app.services.vision._openai_analyze", analyze)

    analysis = await explain_stored_image(fake_pool, message_id)

    assert analysis["explanation"] == "A screenshot of travel plans and a tense message bubble."
    assert fake_pool.messages[message_id]["media_analysis"]["description"] == analysis["explanation"]


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
