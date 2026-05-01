"""Supabase Storage upload helper."""

import httpx

from app.config import get_settings


async def upload_media(bucket: str, object_path: str, content: bytes, content_type: str) -> str:
    settings = get_settings()
    key = settings.supabase_service_role_key.get_secret_value()
    url = f"{settings.supabase_url.rstrip('/')}/storage/v1/object/{bucket}/{object_path}"
    async with httpx.AsyncClient(timeout=settings.media_fetch_timeout_s) as client:
        response = await client.post(
            url,
            headers={
                "Authorization": f"Bearer {key}",
                "apikey": key,
                "Content-Type": content_type,
            },
            content=content,
        )
    response.raise_for_status()
    return f"{bucket}/{object_path}"
