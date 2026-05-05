"""Discord transport helpers."""

import asyncio
import contextlib
import json
import logging
import random
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

TYPING_DELAY_MIN_S = 0.2
TYPING_DELAY_MAX_S = 1.5


async def _send_typing_after_delay(channel_id: str) -> None:
    await asyncio.sleep(random.uniform(TYPING_DELAY_MIN_S, TYPING_DELAY_MAX_S))
    with contextlib.suppress(Exception):
        await send_typing(channel_id)

import httpx
import websockets
from resident_chat_runtime.discord_gateway import DiscordGatewayLoop, GatewayCallbacks
from resident_chat_runtime.discord_rest import DISCORD_API_BASE, DiscordRestClient

from app.config import get_settings
from app.models.user import upsert_user
from app.services.crypto import encrypt_value
from app.services.whitelist import is_allowed_phone

logger = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None


class _DiscordSessionAdapter:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def request(self, method: str, url: str, **kwargs: Any) -> Any:
        path = url.removeprefix(DISCORD_API_BASE)
        request = getattr(self._client, "request", None)
        if request is not None:
            return await request(method, path, **kwargs)
        method_func = getattr(self._client, method.lower())
        return await method_func(path, **kwargs)


def _token() -> str:
    token = get_settings().discord_bot_token
    if token is None or not token.get_secret_value():
        raise RuntimeError("Discord provider requires DISCORD_BOT_TOKEN")
    return token.get_secret_value()


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bot {_token()}"}


async def _rest_client() -> DiscordRestClient:
    return DiscordRestClient(
        token=_token(),
        session=_DiscordSessionAdapter(await _get_client()),
        user_agent="veas",
    )


def _discord_user_id(value: str) -> str:
    return value.removeprefix("discord:").strip()


def is_allowed_discord_user(user_id: str | None) -> bool:
    return is_allowed_phone(user_id)


def _reaction_sentiment(emoji: str | None) -> str:
    if emoji in {"👍", "❤️"}:
        return "positive"
    if emoji == "👎":
        return "negative"
    return "mixed"


async def init_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(base_url="https://discord.com/api/v10", timeout=get_settings().media_fetch_timeout_s)
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


async def send_text(to: str, body: str, *, send_typing_indicator: bool = True) -> dict[str, Any]:
    """Send a Discord DM and return the existing message-id shaped response."""
    rest = await _rest_client()
    channel_id = await get_dm_channel_id(_discord_user_id(to))
    if send_typing_indicator:
        await send_typing(channel_id)
    message_response = await rest.send_message(channel_id, content=body)
    message_response.raise_for_status()
    return {"messages": [{"id": message_response.json()["id"]}]}


async def add_reaction(to: str, message_id: str, emoji: str) -> None:
    """Add the bot's reaction to a Discord DM message."""
    rest = await _rest_client()
    channel_id = await get_dm_channel_id(_discord_user_id(to))
    response = await rest.request(
        "PUT",
        f"/channels/{channel_id}/messages/{message_id}/reactions/{quote(emoji, safe='')}/@me",
    )
    response.raise_for_status()


async def edit_text(to: str, message_id: str, body: str) -> None:
    """Edit one of the bot's previously sent Discord DM messages."""
    rest = await _rest_client()
    channel_id = await get_dm_channel_id(_discord_user_id(to))
    response = await rest.edit_message(channel_id, message_id, content=body)
    response.raise_for_status()


async def delete_text(to: str, message_id: str) -> None:
    """Delete one of the bot's previously sent Discord DM messages."""
    rest = await _rest_client()
    channel_id = await get_dm_channel_id(_discord_user_id(to))
    response = await rest.delete_message(channel_id, message_id)
    response.raise_for_status()


async def get_dm_channel_id(user_id: str) -> str:
    rest = await _rest_client()
    channel_response = await rest.request(
        "POST",
        "/users/@me/channels",
        json={"recipient_id": user_id},
    )
    channel_response.raise_for_status()
    return channel_response.json()["id"]


async def send_typing(channel_id: str) -> None:
    rest = await _rest_client()
    response = await rest.send_typing(channel_id)
    response.raise_for_status()


async def send_template(
    to: str,
    template_payload: dict[str, Any],
    *,
    send_typing_indicator: bool = True,
) -> dict[str, Any]:
    params = []
    for component in template_payload.get("components", []):
        for parameter in component.get("parameters", []):
            params.append(str(parameter.get("text", "")))
    return await send_text(
        to,
        " ".join(params) or str(template_payload.get("name", "message")),
        send_typing_indicator=send_typing_indicator,
    )


async def seed_partner_users(pool: Any) -> None:
    settings = get_settings()
    if settings.discord_partner_user_id_a:
        await upsert_user(
            pool,
            settings.discord_partner_name_a,
            _discord_user_id(settings.discord_partner_user_id_a),
            settings.default_user_timezone,
        )
    if settings.discord_partner_user_id_b:
        await upsert_user(
            pool,
            settings.discord_partner_name_b,
            _discord_user_id(settings.discord_partner_user_id_b),
            settings.default_user_timezone,
        )


def _configured_partner_name(user_id: str) -> str | None:
    settings = get_settings()
    normalized = _discord_user_id(user_id)
    if settings.discord_partner_user_id_a and normalized == _discord_user_id(settings.discord_partner_user_id_a):
        return settings.discord_partner_name_a
    if settings.discord_partner_user_id_b and normalized == _discord_user_id(settings.discord_partner_user_id_b):
        return settings.discord_partner_name_b
    return None


def _has_image_attachment(message: dict[str, Any]) -> bool:
    return any(_is_image_attachment(att) for att in (message.get("attachments") or []))


def _has_supported_attachment(message: dict[str, Any]) -> bool:
    return any(
        _is_image_attachment(att) or _is_audio_attachment(att)
        for att in (message.get("attachments") or [])
    )


def _is_image_attachment(attachment: dict[str, Any]) -> bool:
    content_type = (attachment.get("content_type") or "").lower()
    if content_type.startswith("image/"):
        return True
    filename = (attachment.get("filename") or "").lower()
    return filename.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))


def _is_audio_attachment(attachment: dict[str, Any]) -> bool:
    content_type = (attachment.get("content_type") or "").lower()
    if content_type.startswith("audio/"):
        return True
    filename = (attachment.get("filename") or "").lower()
    return filename.endswith((".ogg", ".oga", ".opus", ".mp3", ".m4a", ".wav", ".webm"))


def _duration_seconds(attachment: dict[str, Any]) -> int | None:
    duration = attachment.get("duration_secs")
    if duration is None:
        return None
    try:
        return max(0, round(float(duration)))
    except (TypeError, ValueError):
        return None


def _discord_base_message_id(message_id: str | None) -> str | None:
    if not message_id:
        return None
    return str(message_id).split(":", 1)[0]


def message_to_meta_payload(message: dict[str, Any]) -> dict[str, Any]:
    author = message["author"]
    user_id = str(author["id"])
    name = _configured_partner_name(user_id) or author.get("global_name") or author.get("username") or user_id
    sent_at = datetime.now(UTC)
    if message.get("timestamp"):
        sent_at = datetime.fromisoformat(message["timestamp"].replace("Z", "+00:00"))
    timestamp_str = str(int(sent_at.timestamp()))
    message_id = str(message["id"])

    messages: list[dict[str, Any]] = []
    content = message.get("content") or ""
    if content:
        messages.append(
            {
                "from": user_id,
                "id": message_id,
                "timestamp": timestamp_str,
                "type": "text",
                "text": {"body": content},
            }
        )

    for attachment in message.get("attachments") or []:
        if not (_is_image_attachment(attachment) or _is_audio_attachment(attachment)):
            continue
        url = attachment.get("url") or attachment.get("proxy_url")
        if not url:
            continue
        attachment_id = str(attachment.get("id") or url)
        item = {
            "from": user_id,
            "id": f"{message_id}:{attachment_id}",
            "timestamp": timestamp_str,
        }
        if _is_audio_attachment(attachment):
            item["type"] = "audio"
            item["audio"] = {"id": url}
            duration = _duration_seconds(attachment)
            if duration is not None:
                item["audio"]["duration"] = duration
        else:
            item["type"] = "image"
            item["image"] = {"id": url}
        messages.append(item)

    if not messages:
        messages.append(
            {
                "from": user_id,
                "id": message_id,
                "timestamp": timestamp_str,
                "type": "text",
                "text": {"body": ""},
            }
        )

    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "contacts": [
                                {
                                    "wa_id": user_id,
                                    "profile": {"name": name},
                                }
                            ],
                            "messages": messages,
                        }
                    }
                ]
            }
        ]
    }


class DiscordGatewayBot:
    """Small Discord Gateway client for DM text ingestion."""

    def __init__(self, pool: Any, coalescer: Any | None) -> None:
        self.pool = pool
        self.coalescer = coalescer
        self._closed = asyncio.Event()
        self._heartbeat_task: asyncio.Task | None = None
        self._gateway_loop = DiscordGatewayLoop(
            GatewayCallbacks(
                on_message_create=self._handle_message,
                on_message_update=self._handle_message_update,
                on_message_delete=self._handle_message_delete,
                on_reaction_add=self._handle_reaction_add,
                on_event=self._handle_gateway_event,
            )
        )

    async def close(self) -> None:
        self._closed.set()
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._heartbeat_task

    async def run_forever(self) -> None:
        while not self._closed.is_set():
            try:
                await self._run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("discord gateway loop failed")
                await asyncio.sleep(5)

    async def _run_once(self) -> None:
        async with websockets.connect("wss://gateway.discord.gg/?v=10&encoding=json") as ws:
            hello = json.loads(await ws.recv())
            interval = hello["d"]["heartbeat_interval"] / 1000
            self._heartbeat_task = asyncio.create_task(self._heartbeat(ws, interval))
            await ws.send(
                json.dumps(
                    {
                        "op": 2,
                        "d": {
                            "token": _token(),
                            "intents": (1 << 12) | (1 << 14) | (1 << 15),
                            "properties": {"os": "macos", "browser": "veas", "device": "veas"},
                        },
                    }
                )
            )
            async for raw in ws:
                event = json.loads(raw)
                if event.get("op") == 0:
                    await self._gateway_loop.dispatch_payload(event)
                if self._closed.is_set():
                    break

    async def _heartbeat(self, ws: Any, interval: float) -> None:
        with contextlib.suppress(websockets.exceptions.ConnectionClosed):
            while not self._closed.is_set():
                await asyncio.sleep(interval)
                await ws.send(json.dumps({"op": 1, "d": None}))

    async def _handle_gateway_event(self, payload: dict[str, Any]) -> None:
        if payload.get("t") != "TYPING_START" or self.coalescer is None:
            return
        pacer = getattr(self.coalescer, "pacer", None)
        if pacer is None:
            return
        data = payload.get("d")
        if not isinstance(data, dict):
            return
        user_id = str(data.get("user_id", ""))
        if not user_id or not is_allowed_discord_user(user_id):
            return
        typing_user = await upsert_user(
            self.pool,
            _configured_partner_name(user_id) or user_id,
            _discord_user_id(user_id),
            get_settings().default_user_timezone,
        )
        pacer.mark_user_typing(typing_user.id, channel_id=str(data.get("channel_id") or ""))

    async def _handle_message(self, message: dict[str, Any]) -> None:
        from app.services.inbound import process_inbound

        if message.get("author", {}).get("bot"):
            return
        author_id = str(message.get("author", {}).get("id", ""))
        if not is_allowed_discord_user(author_id):
            logger.warning("dropping non-whitelisted discord user %s", author_id)
            return
        if not message.get("content") and not _has_supported_attachment(message):
            return
        await process_inbound(self.pool, message_to_meta_payload(message), self.coalescer)

    async def _handle_message_update(self, message: dict[str, Any]) -> None:
        author_id = str(message.get("author", {}).get("id", ""))
        if not author_id or not is_allowed_discord_user(author_id):
            return
        if "content" not in message:
            return
        await self.pool.execute(
            """
            UPDATE messages
            SET edit_history = COALESCE(edit_history, '[]'::jsonb)
                    || jsonb_build_array(jsonb_build_object('content', content, 'at', now())),
                content = $1,
                content_encrypted = $2,
                edited_at = now()
            WHERE whatsapp_message_id = $3
            """,
            message.get("content", ""),
            encrypt_value(message.get("content", "")),
            str(message["id"]),
        )

    async def _handle_message_delete(self, message: dict[str, Any]) -> None:
        await self.pool.execute(
            "UPDATE messages SET deleted_at = now() WHERE whatsapp_message_id = $1",
            str(message["id"]),
        )

    async def _handle_reaction_add(self, event: dict[str, Any]) -> None:
        user_id = str(event.get("user_id", ""))
        if not is_allowed_discord_user(user_id):
            return
        target_id = await self.pool.fetchval(
            "SELECT id FROM messages WHERE whatsapp_message_id=$1 AND direction='outbound'",
            str(event.get("message_id", "")),
        )
        if target_id is None:
            logger.info("ignoring discord reaction for unknown outbound message_id=%s", event.get("message_id"))
            return
        emoji = event.get("emoji", {}).get("name")
        reacting_user = await upsert_user(
            self.pool,
            _configured_partner_name(user_id) or user_id,
            _discord_user_id(user_id),
            get_settings().default_user_timezone,
        )
        await self.pool.fetchrow(
            """
            INSERT INTO feedback (from_user_id, target_type, target_id, sentiment, content, source)
            VALUES ($1, 'message', $2, $3, $4, 'reaction')
            RETURNING id
            """,
            reacting_user.id,
            target_id,
            _reaction_sentiment(emoji),
            emoji,
        )


async def catch_up_recent_messages(pool: Any, coalescer: Any | None, *, limit: int = 50) -> int:
    """Fetch recent partner DM history so messages sent while offline are ingested."""
    from app.services.inbound import process_inbound

    settings = get_settings()
    partner_ids = [
        value
        for value in (settings.discord_partner_user_id_a, settings.discord_partner_user_id_b)
        if value
    ]
    processed = 0
    rest = await _rest_client()
    for partner_id in partner_ids:
        user_id = _discord_user_id(partner_id)
        channel_id = await get_dm_channel_id(user_id)
        last_seen_id = await pool.fetchval(
            """
            SELECT m.whatsapp_message_id
            FROM messages m
            JOIN users u ON u.id = m.sender_id
            WHERE m.direction='inbound'
              AND u.phone=$1
              AND m.whatsapp_message_id IS NOT NULL
            ORDER BY m.sent_at DESC
            LIMIT 1
            """,
            user_id,
        )
        response = await rest.fetch_channel_messages(
            channel_id,
            limit=limit,
            after=_discord_base_message_id(last_seen_id),
        )
        response.raise_for_status()
        for message in reversed(response.json()):
            if str(message.get("author", {}).get("id", "")) != user_id:
                continue
            if message.get("author", {}).get("bot"):
                continue
            if not message.get("content") and not _has_supported_attachment(message):
                continue
            await process_inbound(
                pool,
                message_to_meta_payload(message),
                coalescer,
                coalescer_source="catch_up",
            )
            processed += 1
    if processed:
        logger.info("discord catch-up ingested %s recent message(s)", processed)
    return processed
