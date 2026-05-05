"""Image analysis pipeline."""

import base64
import logging
from typing import Any

import anthropic
import httpx

from app.config import get_settings
from app.models.user import User
from app.services import storage, system_state, whatsapp
from app.services.messaging import send_outbound
from app.services.spend import is_under_cap, record_llm_cost
from app.services.templates import TemplateCall

logger = logging.getLogger(__name__)


MEDIA_EXPLAIN_PROMPT = """Describe this image for durable future recall by a relationship-mediation assistant.

Return plain text in two parts:

1. A compact description paragraph covering: visible people, setting, objects, actions, emotional tone when apparent, and relationship-relevant context that may matter later. Note uncertainty where the image is ambiguous.

2. A line that begins exactly with "Visible text:" followed by a verbatim transcription of every readable string in the image, in natural reading order. This includes UI labels, screenshot message bodies (preserve speaker/sender attributions and message order if discernible), captions, signs, watermarks, and handwriting if legible. Preserve casing, punctuation, line breaks, and emoji as they appear. Do not paraphrase or summarize the text. If a portion is partially obscured or unreadable, transcribe what you can and mark the unclear span with "[unclear]" rather than guessing. If there is no readable text in the image, write "Visible text: none".

Safety rails: do not identify unknown people, do not infer protected traits, do not add advice or interpretation beyond what is visibly evident. Plain text only -- no markdown headers or code fences."""


def _extract_output_text(data: dict[str, Any]) -> str:
    output_text = data.get("output_text")
    if isinstance(output_text, str):
        return output_text.strip()
    pieces: list[str] = []
    for item in data.get("output", []) or []:
        for content in item.get("content", []) or []:
            text = content.get("text")
            if isinstance(text, str):
                pieces.append(text)
    return "\n".join(piece.strip() for piece in pieces if piece.strip()).strip()


def _analysis_payload(result: str | dict[str, Any], *, media_type: str = "image") -> dict[str, Any]:
    if isinstance(result, dict):
        payload = dict(result)
        explanation = payload.get("explanation") or payload.get("description") or payload.get("summary")
    else:
        payload = {}
        explanation = result
    explanation_text = str(explanation or "").strip()
    settings = get_settings()
    payload.update(
        {
            "kind": media_type,
            "provider": payload.get("provider") or "openai",
            "model": payload.get("model") or settings.vision_model,
            "detail": payload.get("detail") or settings.vision_detail,
            "explanation": explanation_text,
            # Backwards-compatible key for older code/tests and existing memories.
            "description": payload.get("description") or explanation_text,
        }
    )
    return payload


async def _openai_analyze(image_bytes: bytes, content_type: str) -> dict[str, Any]:
    settings = get_settings()
    data_url = f"data:{content_type};base64,{base64.b64encode(image_bytes).decode()}"
    async with httpx.AsyncClient(timeout=settings.media_fetch_timeout_s) as client:
        response = await client.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {settings.openai_api_key.get_secret_value()}"},
            json={
                "model": settings.vision_model,
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": MEDIA_EXPLAIN_PROMPT},
                            {"type": "input_image", "image_url": data_url, "detail": settings.vision_detail},
                        ],
                    }
                ],
            },
        )
    response.raise_for_status()
    explanation = _extract_output_text(response.json())
    if not explanation:
        raise ValueError("empty vision explanation")
    return _analysis_payload(explanation)


def _anthropic_text(response: Any) -> str:
    pieces: list[str] = []
    for block in getattr(response, "content", []) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            pieces.append(text)
    return "\n".join(piece.strip() for piece in pieces if piece.strip()).strip()


async def _anthropic_analyze(image_bytes: bytes, content_type: str) -> dict[str, Any]:
    settings = get_settings()
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key.get_secret_value())
    response = await client.messages.create(
        model=settings.conversational_model,
        max_tokens=1200,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": content_type,
                            "data": base64.b64encode(image_bytes).decode(),
                        },
                    },
                    {"type": "text", "text": MEDIA_EXPLAIN_PROMPT},
                ],
            }
        ],
    )
    explanation = _anthropic_text(response)
    if not explanation:
        raise ValueError("empty anthropic vision explanation")
    return _analysis_payload(
        {
            "provider": "anthropic",
            "model": settings.conversational_model,
            "explanation": explanation,
            "description": explanation,
        }
    )


async def _analyze_image(image_bytes: bytes, content_type: str) -> dict[str, Any]:
    try:
        return _analysis_payload(await _openai_analyze(image_bytes, content_type))
    except Exception:
        logger.exception("openai image analysis failed; trying anthropic fallback")
    return await _anthropic_analyze(image_bytes, content_type)


async def explain_stored_image(pool: Any, message_id) -> dict[str, Any]:
    row = await pool.fetchrow(
        """
        SELECT id, media_type, media_url
        FROM messages
        WHERE id=$1 AND deleted_at IS NULL
        """,
        message_id,
    )
    if row is None:
        raise ValueError("media message not found")
    if row["media_type"] != "image" or not row["media_url"]:
        raise ValueError("message is not an image with stored media")
    image_bytes, content_type = await storage.download_media(row["media_url"])
    analysis = await _analyze_image(image_bytes, content_type)
    await pool.execute("UPDATE messages SET media_analysis=$1 WHERE id=$2", analysis, message_id)
    await record_llm_cost(pool, "vision", 0.001)
    return analysis


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
        analysis = await _analyze_image(image_bytes, content_type)
    except Exception as exc:
        logger.exception("image analysis failed message_id=%s", message_id)
        await pool.execute(
            "UPDATE messages SET media_analysis=$1, processing_state='expired' WHERE id=$2",
            {"error": "vision_failed", "detail": type(exc).__name__},
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
        await pool.execute("UPDATE messages SET media_analysis=$1 WHERE id=$2", analysis, message_id)
        await record_llm_cost(pool, "vision", 0.001)
        if should_enqueue:
            await coalescer.add(user.id, message_id, user, source="media")
