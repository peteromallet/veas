"""Image analysis pipeline."""

import base64
from typing import Any

import httpx

from app.config import get_settings
from app.models.user import User
from app.services import storage, system_state, whatsapp
from app.services.messaging import send_outbound
from app.services.spend import is_under_cap, record_llm_cost
from app.services.templates import TemplateCall


async def _openai_analyze(image_bytes: bytes, content_type: str) -> str:
    settings = get_settings()
    data_url = f"data:{content_type};base64,{base64.b64encode(image_bytes).decode()}"
    async with httpx.AsyncClient(timeout=settings.media_fetch_timeout_s) as client:
        response = await client.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {settings.openai_api_key.get_secret_value()}"},
            json={
                "model": "gpt-4.1-mini",
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "Describe this image for a relationship-mediation assistant."},
                            {"type": "input_image", "image_url": data_url},
                        ],
                    }
                ],
            },
        )
    response.raise_for_status()
    data = response.json()
    if "output_text" in data:
        return data["output_text"]
    return data["output"][0]["content"][0]["text"]


async def handle_image(
    pool: Any,
    message_id,
    media_id: str,
    user: User,
    coalescer: Any | None = None,
) -> None:
    paused = await system_state.is_paused(pool)
    should_enqueue = coalescer is not None and not paused
    image_bytes, content_type = await whatsapp.fetch_media(media_id)
    media_url = await storage.upload_media(
        get_settings().supabase_storage_bucket,
        f"image/{message_id}",
        image_bytes,
        content_type,
    )
    await pool.execute("UPDATE messages SET media_type='image', media_url=$1 WHERE id=$2", media_url, message_id)

    if not await is_under_cap(pool, "vision"):
        await pool.execute(
            "UPDATE messages SET media_analysis=$1, processing_state='expired' WHERE id=$2",
            {"unavailable": "daily_cap"},
            message_id,
        )
        if not paused:
            await send_outbound(
                pool,
                user,
                "I can't analyze images right now -- could you describe it in text?",
                template_fallback=TemplateCall("media_failure", [user.name, "image"]),
            )
        return

    try:
        description = await _openai_analyze(image_bytes, content_type)
    except Exception:
        await pool.execute(
            "UPDATE messages SET media_analysis=$1, processing_state='expired' WHERE id=$2",
            {"error": "vision_failed"},
            message_id,
        )
        if not paused:
            await send_outbound(
                pool,
                user,
                "I couldn't process your last image -- could you try resending or describe it in text?",
                template_fallback=TemplateCall("media_failure", [user.name, "image"]),
            )
    else:
        await pool.execute("UPDATE messages SET media_analysis=$1 WHERE id=$2", {"description": description}, message_id)
        await record_llm_cost(pool, "vision", 0.001)
        if should_enqueue:
            await coalescer.add(user.id, message_id, user)
