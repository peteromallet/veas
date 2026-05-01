"""Voice-note transcription.

The in-task retry is the only retry path; webhook redelivery is dedup-blocked
by whatsapp_message_id, so two attempts happen inside this background task.
"""

import asyncio
from typing import Any

import httpx

from app.config import get_settings
from app.models.user import User
from app.services import storage, system_state, whatsapp
from app.services.messaging import send_outbound
from app.services.spend import is_under_cap, record_llm_cost
from app.services.templates import TemplateCall


async def _groq_transcribe(audio_bytes: bytes, content_type: str) -> str:
    settings = get_settings()
    async with httpx.AsyncClient(timeout=settings.media_fetch_timeout_s) as client:
        response = await client.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {settings.groq_api_key.get_secret_value()}"},
            files={"file": ("voice.ogg", audio_bytes, content_type)},
            data={"model": "whisper-large-v3"},
        )
    response.raise_for_status()
    return response.json()["text"]


async def handle_voice(
    pool: Any,
    message_id,
    media_id: str,
    user: User,
    coalescer: Any | None = None,
    duration: int | None = None,
) -> None:
    paused = await system_state.is_paused(pool)
    should_enqueue = coalescer is not None and not paused
    audio_bytes, content_type = await whatsapp.fetch_media(media_id)
    media_url = await storage.upload_media(
        get_settings().supabase_storage_bucket,
        f"voice/{message_id}",
        audio_bytes,
        content_type,
    )
    await pool.execute(
        "UPDATE messages SET media_type='voice', media_url=$1, media_duration_seconds=$2 WHERE id=$3",
        media_url,
        duration,
        message_id,
    )

    if not await is_under_cap(pool, "transcription"):
        await pool.execute(
            """
            UPDATE messages
            SET content=$1, media_analysis=$2, processing_state='expired'
            WHERE id=$3
            """,
            "I can't transcribe right now -- can you send it as text?",
            {"unavailable": "daily_cap"},
            message_id,
        )
        if not paused:
            await send_outbound(
                pool,
                user,
                "I can't transcribe right now -- can you send it as text?",
                template_fallback=TemplateCall("media_failure", [user.name, "voice"]),
            )
        return

    last_error: Exception | None = None
    for attempt in range(2):
        try:
            transcript = await _groq_transcribe(audio_bytes, content_type)
            await pool.execute("UPDATE messages SET content=$1 WHERE id=$2", transcript, message_id)
            await record_llm_cost(pool, "transcription", 0.001)
            if should_enqueue:
                await coalescer.add(user.id, message_id, user)
            return
        except Exception as exc:
            last_error = exc
            if attempt == 0:
                await asyncio.sleep(2)

    await pool.execute(
        """
        UPDATE messages
        SET processing_state='expired',
            media_analysis = COALESCE(media_analysis, '{}'::jsonb)
                || jsonb_build_object('_pipeline', jsonb_build_object('attempts', 2, 'last_error', $1))
        WHERE id=$2
        """,
        str(last_error),
        message_id,
    )
    if not paused:
        await send_outbound(
            pool,
            user,
            "I couldn't process your last voice note -- could you try resending or describe it in text?",
            template_fallback=TemplateCall("media_failure", [user.name, "voice"]),
        )
