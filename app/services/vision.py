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


MEDIA_EXPLAIN_PROMPT = """Explain this image for durable future recall by a relationship-mediation assistant.

Write a compact, queryable note. Include:
- visible people, setting, objects, screenshots/text, actions, and emotional tone when apparent
- relationship-relevant context that may matter later
- uncertainty where the image is ambiguous

Do not identify unknown people or infer protected traits. Do not add advice. Return plain text only."""


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
    analysis = _analysis_payload(await _openai_analyze(image_bytes, content_type))
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
        analysis = _analysis_payload(await _openai_analyze(image_bytes, content_type))
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
        await pool.execute("UPDATE messages SET media_analysis=$1 WHERE id=$2", analysis, message_id)
        await record_llm_cost(pool, "vision", 0.001)
        if should_enqueue:
            await coalescer.add(user.id, message_id, user, source="media")
