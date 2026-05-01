"""WhatsApp Cloud API helpers."""

import base64
import hmac
from hashlib import sha256
from hashlib import sha1
from typing import Any
from urllib.parse import urlparse

import httpx

from app.config import get_settings

_client: httpx.AsyncClient | None = None


def _bearer_token() -> str:
    settings = get_settings()
    token = settings.whatsapp_bearer_token or settings.whatsapp_token
    return token.get_secret_value()


def _messaging_provider() -> str:
    return get_settings().messaging_provider.strip().lower()


def _twilio_auth() -> tuple[str, str]:
    settings = get_settings()
    if settings.twilio_api_key_sid and settings.twilio_api_key_secret:
        return settings.twilio_api_key_sid, settings.twilio_api_key_secret.get_secret_value()
    if settings.twilio_account_sid and settings.twilio_auth_token:
        return settings.twilio_account_sid, settings.twilio_auth_token.get_secret_value()
    raise RuntimeError(
        "Twilio provider requires TWILIO_ACCOUNT_SID plus TWILIO_AUTH_TOKEN, "
        "or TWILIO_API_KEY_SID plus TWILIO_API_KEY_SECRET"
    )


def _twilio_account_sid() -> str:
    account_sid = get_settings().twilio_account_sid
    if not account_sid:
        raise RuntimeError("Twilio provider requires TWILIO_ACCOUNT_SID")
    return account_sid


def _twilio_from() -> str:
    value = get_settings().twilio_whatsapp_from
    if not value:
        raise RuntimeError("Twilio provider requires TWILIO_WHATSAPP_FROM")
    return value if value.startswith("whatsapp:") else f"whatsapp:{value}"


def _twilio_to(phone: str) -> str:
    return phone if phone.startswith("whatsapp:") else f"whatsapp:{phone}"


async def init_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        settings = get_settings()
        base_url = "https://api.twilio.com" if _messaging_provider() == "twilio" else "https://graph.facebook.com"
        _client = httpx.AsyncClient(base_url=base_url, timeout=settings.media_fetch_timeout_s)
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def _get_client() -> httpx.AsyncClient:
    if _client is None:
        return await init_client()
    return _client


def verify_subscription(mode: str | None, token: str | None, challenge: str | None) -> str | None:
    settings = get_settings()
    if mode == "subscribe" and token == settings.whatsapp_verify_token.get_secret_value():
        return challenge
    return None


def verify_signature(raw_body: bytes, header: str | None) -> bool:
    if header is None:
        return False
    supplied = header.removeprefix("sha256=")
    digest = hmac.new(
        get_settings().whatsapp_app_secret.get_secret_value().encode(),
        raw_body,
        sha256,
    ).hexdigest()
    return hmac.compare_digest(supplied, digest)


def verify_twilio_signature(url: str, form: dict[str, str], header: str | None) -> bool:
    if header is None:
        return False
    auth_token = get_settings().twilio_auth_token
    if auth_token is None:
        return False
    signed = url + "".join(f"{key}{form[key]}" for key in sorted(form))
    digest = hmac.new(auth_token.get_secret_value().encode(), signed.encode(), sha1).digest()
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(header, expected)


async def fetch_media(media_id: str) -> tuple[bytes, str]:
    if media_id.startswith("http://") or media_id.startswith("https://"):
        client = await _get_client()
        auth = _twilio_auth() if _messaging_provider() == "twilio" and "twilio.com" in urlparse(media_id).netloc else None
        response = await client.get(media_id, auth=auth)
        response.raise_for_status()
        return response.content, response.headers.get("content-type", "application/octet-stream")

    settings = get_settings()
    client = await _get_client()
    headers = {"Authorization": f"Bearer {_bearer_token()}"}
    media_response = await client.get(f"/{settings.whatsapp_api_version}/{media_id}", headers=headers)
    media_response.raise_for_status()
    media_url = media_response.json()["url"]
    content_response = await client.get(media_url, headers=headers)
    content_response.raise_for_status()
    return content_response.content, content_response.headers.get("content-type", "application/octet-stream")


async def send_text(to: str, body: str) -> dict[str, Any]:
    if _messaging_provider() == "twilio":
        auth = _twilio_auth()
        account_sid = _twilio_account_sid()
        client = await _get_client()
        response = await client.post(
            f"/2010-04-01/Accounts/{account_sid}/Messages.json",
            auth=auth,
            data={"From": _twilio_from(), "To": _twilio_to(to), "Body": body},
        )
        response.raise_for_status()
        data = response.json()
        return {"messages": [{"id": data["sid"]}]}

    settings = get_settings()
    client = await _get_client()
    response = await client.post(
        f"/{settings.whatsapp_api_version}/{settings.whatsapp_phone_number_id}/messages",
        headers={"Authorization": f"Bearer {_bearer_token()}"},
        json={
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": body},
        },
    )
    response.raise_for_status()
    return response.json()


async def send_template(to: str, template_payload: dict[str, Any]) -> dict[str, Any]:
    if _messaging_provider() == "twilio":
        body = _render_twilio_template_body(template_payload)
        return await send_text(to, body)

    settings = get_settings()
    client = await _get_client()
    response = await client.post(
        f"/{settings.whatsapp_api_version}/{settings.whatsapp_phone_number_id}/messages",
        headers={"Authorization": f"Bearer {_bearer_token()}"},
        json={
            "messaging_product": "whatsapp",
            "to": to,
            "type": "template",
            "template": template_payload,
        },
    )
    response.raise_for_status()
    return response.json()


def _render_twilio_template_body(template_payload: dict[str, Any]) -> str:
    params = []
    for component in template_payload.get("components", []):
        for parameter in component.get("parameters", []):
            params.append(str(parameter.get("text", "")))
    name = str(template_payload.get("name", "message"))
    if name == "weekly_summary" and len(params) >= 3:
        return f"Hi {params[0]}, this week we had {params[1]} conversations and touched on {params[2]} ongoing things. Want to talk through anything? Just ask."
    if name == "escalation" and len(params) >= 3:
        return f"Hi {params[0]}, this is your assistant. {params[1]} has shared something I think is worth your attention soon. {params[2]}"
    if name == "checkin_nudge" and params:
        return f"Hi {params[0]}, been a bit -- anything on your mind? Just message me back when you're ready."
    if name == "pause_confirmation" and len(params) >= 2:
        return f"Hi {params[0]}, {params[1]} has paused our conversations for now. I'll be quiet on both threads until either of you messages me again."
    if name == "media_failure" and len(params) >= 2:
        return f"Hi {params[0]}, I couldn't process your last {params[1]} note -- could you try resending or describe it in text?"
    return " ".join(params) or name
