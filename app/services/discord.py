"""Discord transport helpers."""

import asyncio
import contextlib
import json
import logging
import random
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx
import websockets
from resident_chat_runtime.discord_gateway import DiscordGatewayLoop, GatewayCallbacks
from resident_chat_runtime.discord_rest import DISCORD_API_BASE, DiscordRestClient

from app.config import get_settings
from app.models.user import upsert_user
from app.services.crypto import encrypt_value
from app.services.discord_id import _decode_discord_user_id
from app.services.whitelist import is_allowed_phone

TYPING_DELAY_MIN_S = 0.2
TYPING_DELAY_MAX_S = 1.5


async def _send_typing_after_delay(channel_id: str, *, bot_id: str) -> None:
    await asyncio.sleep(random.uniform(TYPING_DELAY_MIN_S, TYPING_DELAY_MAX_S))
    with contextlib.suppress(Exception):
        await send_typing(channel_id, bot_id=bot_id)

logger = logging.getLogger(__name__)

# ── Module registry ─────────────────────────────────────────────────────────
_clients: dict[str, "DiscordClient"] = {}


def register_client(bot_id: str, client: "DiscordClient") -> None:
    """Register a DiscordClient for a bot_id (typically called at startup)."""
    _clients[bot_id] = client


def get_client(bot_id: str) -> "DiscordClient":
    """Return the registered DiscordClient for bot_id."""
    return _clients[bot_id]


def iter_clients():
    """Iterate over (bot_id, DiscordClient) pairs."""
    return iter(_clients.items())


async def close_all_clients() -> None:
    """Close all registered DiscordClient http pools."""
    for client in list(_clients.values()):
        await client.aclose()
    _clients.clear()


# ── _DiscordSessionAdapter ──────────────────────────────────────────────────

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


# ── DiscordClient ───────────────────────────────────────────────────────────

class DiscordClient:
    """Per-bot Discord REST client.

    Owns its httpx.AsyncClient, token, bot_user_id, and all REST methods.
    One instance per logical bot identity.
    """

    def __init__(self, bot_id: str, token: str) -> None:
        self.bot_id = bot_id
        self._token = token
        self._http = httpx.AsyncClient(
            base_url="https://discord.com/api/v10",
            timeout=get_settings().media_fetch_timeout_s,
        )
        self._adapter = _DiscordSessionAdapter(self._http)
        self._rest = DiscordRestClient(
            token=token,
            session=self._adapter,
            user_agent="veas",
        )
        self.bot_user_id = _decode_discord_user_id(token)

    async def aclose(self) -> None:
        """Close the underlying httpx connection pool."""
        await self._http.aclose()

    # ── REST helpers ────────────────────────────────────────────────────

    async def send_text(self, to: str, body: str, *, send_typing_indicator: bool = True) -> dict[str, Any]:
        """Send a Discord DM and return the existing message-id shaped response."""
        channel_id = await self.get_dm_channel_id(_discord_user_id(to))
        if send_typing_indicator:
            await self.send_typing(channel_id)
        message_response = await self._rest.send_message(channel_id, content=body)
        message_response.raise_for_status()
        return {"messages": [{"id": message_response.json()["id"]}]}

    async def add_reaction(self, to: str, message_id: str, emoji: str) -> None:
        """Add the bot's reaction to a Discord DM message."""
        channel_id = await self.get_dm_channel_id(_discord_user_id(to))
        response = await self._rest.request(
            "PUT",
            f"/channels/{channel_id}/messages/{message_id}/reactions/{quote(emoji, safe='')}/@me",
        )
        response.raise_for_status()

    async def edit_text(self, to: str, message_id: str, body: str) -> None:
        """Edit one of the bot's previously sent Discord DM messages."""
        channel_id = await self.get_dm_channel_id(_discord_user_id(to))
        response = await self._rest.edit_message(channel_id, message_id, content=body)
        response.raise_for_status()

    async def delete_text(self, to: str, message_id: str) -> None:
        """Delete one of the bot's previously sent Discord DM messages."""
        channel_id = await self.get_dm_channel_id(_discord_user_id(to))
        response = await self._rest.delete_message(channel_id, message_id)
        response.raise_for_status()

    async def get_dm_channel_id(self, user_id: str) -> str:
        """Resolve the DM channel id for a Discord user."""
        channel_response = await self._rest.request(
            "POST",
            "/users/@me/channels",
            json={"recipient_id": user_id},
        )
        channel_response.raise_for_status()
        return channel_response.json()["id"]

    async def send_typing(self, channel_id: str) -> None:
        """Send a typing indicator to a Discord channel."""
        response = await self._rest.send_typing(channel_id)
        response.raise_for_status()

    async def send_template(
        self,
        to: str,
        template_payload: dict[str, Any],
        *,
        send_typing_indicator: bool = True,
    ) -> dict[str, Any]:
        """Render and send a WhatsApp-style template to a Discord DM."""
        params = []
        for component in template_payload.get("components", []):
            for parameter in component.get("parameters", []):
                params.append(str(parameter.get("text", "")))
        return await self.send_text(
            to,
            " ".join(params) or str(template_payload.get("name", "message")),
            send_typing_indicator=send_typing_indicator,
        )


# ── Module-level facades ────────────────────────────────────────────────────

async def send_text(to: str, body: str, *, send_typing_indicator: bool = True, bot_id: str) -> dict[str, Any]:
    """Send a Discord DM and return the existing message-id shaped response.

    pause-check N/A: routes through send_outbound.
    """
    return await get_client(bot_id).send_text(to, body, send_typing_indicator=send_typing_indicator)


async def add_reaction(to: str, message_id: str, emoji: str, *, bot_id: str) -> None:
    """Add the bot's reaction to a Discord DM message."""
    await get_client(bot_id).add_reaction(to, message_id, emoji)


async def edit_text(to: str, message_id: str, body: str, *, bot_id: str) -> None:
    """Edit one of the bot's previously sent Discord DM messages."""
    await get_client(bot_id).edit_text(to, message_id, body)


async def delete_text(to: str, message_id: str, *, bot_id: str) -> None:
    """Delete one of the bot's previously sent Discord DM messages."""
    await get_client(bot_id).delete_text(to, message_id)


async def get_dm_channel_id(user_id: str, *, bot_id: str) -> str:
    """Resolve a DM channel id for user_id."""
    return await get_client(bot_id).get_dm_channel_id(user_id)


async def send_typing(channel_id: str, *, bot_id: str) -> None:
    """Send a typing indicator to channel_id."""
    await get_client(bot_id).send_typing(channel_id)


async def send_template(
    to: str,
    template_payload: dict[str, Any],
    *,
    send_typing_indicator: bool = True,
    bot_id: str,
) -> dict[str, Any]:
    """Render and send a template to a Discord DM."""
    return await get_client(bot_id).send_template(to, template_payload, send_typing_indicator=send_typing_indicator)


# ── Utility helpers ─────────────────────────────────────────────────────────

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


# ── Partner user seeding ────────────────────────────────────────────────────

def _configured_partner_name(user_id: str) -> str | None:
    """Look up partner name from DISCORD_PARTNER_USER_ID_A/B settings.

    Scoped to mediator only — these env vars represent the mediator bot's
    two partner user ids and their configured names.  Future bots (e.g.
    Tante Rosi) do not use this mechanism.
    """
    settings = get_settings()
    normalized = _discord_user_id(user_id)
    if settings.discord_partner_user_id_a and normalized == _discord_user_id(settings.discord_partner_user_id_a):
        return settings.discord_partner_name_a
    if settings.discord_partner_user_id_b and normalized == _discord_user_id(settings.discord_partner_user_id_b):
        return settings.discord_partner_name_b
    return None


def message_to_meta_payload(message: dict[str, Any]) -> dict[str, Any]:
    author = message.get("author", {})
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
                            "channel_id": str(message.get("channel_id")) if message.get("channel_id") is not None else None,
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


async def seed_partner_users(pool: Any) -> None:
    """Seed partner users from DISCORD_PARTNER_USER_ID_A/B settings.

    Mediator-scoped only — called once during lifespan for the mediator gateway.
    Future bots (e.g. Tante Rosi) do not use partner-based seeding.
    """
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


# ── DiscordGatewayBot ───────────────────────────────────────────────────────

class DiscordGatewayBot:
    """Per-bot Discord Gateway client for DM text ingestion.

    Each instance owns one logical bot identity, its own DiscordClient,
    and (optionally) its own DiscordPacer.
    """

    def __init__(
        self,
        bot_id: str,
        client: DiscordClient,
        pool: Any,
        coalescer: Any | None,
        *,
        pacer: Any | None = None,
    ) -> None:
        self.bot_id = bot_id
        self.client = client
        self.pool = pool
        self.coalescer = coalescer
        self.pacer = pacer
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
        logger.info("[gateway:%s] run_forever entered", self.bot_id)
        attempt = 0
        while not self._closed.is_set():
            attempt += 1
            try:
                logger.info("[gateway:%s] connect attempt #%d", self.bot_id, attempt)
                await self._run_once()
                logger.info("[gateway:%s] _run_once returned cleanly (attempt #%d)", self.bot_id, attempt)
            except asyncio.CancelledError:
                logger.info("[gateway:%s] cancelled", self.bot_id)
                raise
            except websockets.exceptions.ConnectionClosed as e:
                logger.warning(
                    "[gateway:%s] WS closed code=%s reason=%r (attempt #%d) — reconnecting in 5s",
                    self.bot_id, getattr(e, "code", "?"), getattr(e, "reason", "?"), attempt,
                )
                await asyncio.sleep(5)
            except Exception as e:
                logger.exception(
                    "[gateway:%s] gateway loop failed (attempt #%d, %s): %s — reconnecting in 5s",
                    self.bot_id, attempt, type(e).__name__, e,
                )
                await asyncio.sleep(5)

    async def _run_once(self) -> None:
        intents = (1 << 12) | (1 << 14) | (1 << 15)
        logger.info("[gateway:%s] dialing wss://gateway.discord.gg/?v=10&encoding=json", self.bot_id)
        async with websockets.connect("wss://gateway.discord.gg/?v=10&encoding=json") as ws:
            logger.info("[gateway:%s] WS connected, awaiting HELLO", self.bot_id)
            hello_raw = await ws.recv()
            hello = json.loads(hello_raw)
            interval = hello["d"]["heartbeat_interval"] / 1000
            logger.info("[gateway:%s] HELLO received op=%s heartbeat=%.1fs", self.bot_id, hello.get("op"), interval)
            self._heartbeat_task = asyncio.create_task(self._heartbeat(ws, interval))
            logger.info("[gateway:%s] sending IDENTIFY intents=0x%x", self.bot_id, intents)
            await ws.send(
                json.dumps(
                    {
                        "op": 2,
                        "d": {
                            "token": self.client._token,
                            "intents": intents,
                            "properties": {"os": "linux", "browser": "veas", "device": "veas"},
                            "presence": {
                                "status": "online",
                                "afk": False,
                                "since": None,
                                "activities": [],
                            },
                        },
                    }
                )
            )
            logger.info("[gateway:%s] IDENTIFY sent, awaiting events", self.bot_id)
            events_seen = 0
            interesting_event_types = {
                "READY", "RESUMED", "INVALID_SESSION",
                "MESSAGE_CREATE", "MESSAGE_UPDATE", "MESSAGE_DELETE",
                "MESSAGE_REACTION_ADD", "MESSAGE_REACTION_REMOVE",
                "TYPING_START", "CHANNEL_CREATE",
                "GUILD_CREATE", "GUILD_DELETE",
            }
            async for raw in ws:
                event = json.loads(raw)
                events_seen += 1
                op = event.get("op")
                t = event.get("t")
                if events_seen <= 5 or t in interesting_event_types or op in {9, 7, 1}:
                    if t == "READY":
                        d = event.get("d", {}) or {}
                        user = d.get("user", {}) or {}
                        logger.info(
                            "[gateway:%s] READY received bot=%s id=%s session=%s guilds=%s",
                            self.bot_id, user.get("username"), user.get("id"),
                            (d.get("session_id") or "")[:20],
                            len(d.get("guilds", []) or []),
                        )
                    elif op == 9:
                        logger.warning("[gateway:%s] INVALID_SESSION op=9 d=%r", self.bot_id, event.get("d"))
                    elif op == 7:
                        logger.info("[gateway:%s] RECONNECT requested by server (op=7)", self.bot_id)
                    elif t == "MESSAGE_CREATE":
                        d = event.get("d", {}) or {}
                        author = d.get("author", {}) or {}
                        is_dm = d.get("guild_id") is None
                        logger.info(
                            "[gateway:%s] MESSAGE_CREATE channel=%s author=%s(%s) guild_id=%s is_dm=%s content=%r",
                            self.bot_id,
                            d.get("channel_id"),
                            author.get("username"),
                            author.get("id"),
                            d.get("guild_id"),
                            is_dm,
                            (d.get("content") or "")[:120],
                        )
                    elif t in {"MESSAGE_UPDATE", "MESSAGE_DELETE"}:
                        d = event.get("d", {}) or {}
                        logger.info(
                            "[gateway:%s] %s channel=%s message_id=%s",
                            self.bot_id, t, d.get("channel_id"), d.get("id"),
                        )
                    elif t == "TYPING_START":
                        d = event.get("d", {}) or {}
                        logger.info(
                            "[gateway:%s] TYPING_START channel=%s user_id=%s",
                            self.bot_id, d.get("channel_id"), d.get("user_id"),
                        )
                    elif t == "CHANNEL_CREATE":
                        d = event.get("d", {}) or {}
                        logger.info(
                            "[gateway:%s] CHANNEL_CREATE channel=%s type=%s",
                            self.bot_id, d.get("id"), d.get("type"),
                        )
                    elif t == "GUILD_CREATE":
                        d = event.get("d", {}) or {}
                        logger.info(
                            "[gateway:%s] GUILD_CREATE guild=%s(%s) members=%s",
                            self.bot_id, d.get("name"), d.get("id"), d.get("member_count"),
                        )
                    else:
                        logger.info("[gateway:%s] event #%d op=%s t=%s", self.bot_id, events_seen, op, t)
                if op == 0:
                    try:
                        await self._gateway_loop.dispatch_payload(event)
                    except Exception:
                        logger.exception(
                            "[gateway:%s] dispatch_payload failed for t=%s", self.bot_id, t,
                        )
                if self._closed.is_set():
                    logger.info("[gateway:%s] closed flag set; breaking event loop after %d events", self.bot_id, events_seen)
                    break

    async def _heartbeat(self, ws: Any, interval: float) -> None:
        ticks = 0
        try:
            while not self._closed.is_set():
                await asyncio.sleep(interval)
                await ws.send(json.dumps({"op": 1, "d": None}))
                ticks += 1
                if ticks in (1, 5, 20) or ticks % 100 == 0:
                    logger.info("[gateway:%s] heartbeat tick #%d", self.bot_id, ticks)
        except websockets.exceptions.ConnectionClosed as e:
            logger.warning(
                "[gateway:%s] heartbeat stopped after %d ticks: WS closed code=%s reason=%r",
                self.bot_id, ticks, getattr(e, "code", "?"), getattr(e, "reason", "?"),
            )

    async def _handle_gateway_event(self, payload: dict[str, Any]) -> None:
        if payload.get("t") != "TYPING_START" or self.coalescer is None:
            return
        if self.pacer is None:
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
        self.pacer.mark_user_typing(typing_user.id, channel_id=str(data.get("channel_id") or ""))

    async def _handle_message(self, message: dict[str, Any]) -> None:
        from app.services.inbound import process_inbound

        if message.get("author", {}).get("bot"):
            return
        author_id = str(message.get("author", {}).get("id", ""))
        if not is_allowed_discord_user(author_id):
            # obs N/A: pre-scope gateway
            logger.warning("dropping non-whitelisted discord user %s", author_id)
            return
        if not message.get("content") and not _has_supported_attachment(message):
            return
        await process_inbound(
            self.pool,
            message_to_meta_payload(message),
            self.coalescer,
            transport="discord",
            bot_id=self.bot_id,
        )

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

        _bot_id = self.bot_id

        target = await self.pool.fetchrow(
            "SELECT id, topic_id FROM messages WHERE whatsapp_message_id=$1 AND direction='outbound' AND bot_id=$2",
            str(event.get("message_id", "")),
            _bot_id,
        )
        if target is None:
            logger.info("ignoring discord reaction for unknown outbound message_id=%s", event.get("message_id"),
                         extra={"bot_id": _bot_id, "topic_id": None})
            return
        _topic_id = target["topic_id"]
        emoji = event.get("emoji", {}).get("name")
        reacting_user = await upsert_user(
            self.pool,
            _configured_partner_name(user_id) or user_id,
            _discord_user_id(user_id),
            get_settings().default_user_timezone,
        )
        await self.pool.fetchrow(
            """
            INSERT INTO feedback (from_user_id, target_type, target_id, sentiment, content, source, bot_id, topic_id)
            VALUES ($1, 'message', $2, $3, $4, 'reaction', $5, $6)
            RETURNING id
            """,
            reacting_user.id,
            target["id"],
            _reaction_sentiment(emoji),
            emoji,
            _bot_id,
            _topic_id,
        )


# ── Catch-up ────────────────────────────────────────────────────────────────

async def catch_up_recent_messages(pool: Any, coalescer: Any | None, *, client: DiscordClient, bot_id: str, limit: int = 50) -> int:
    """Fetch recent partner DM history so messages sent while offline are ingested.

    Accepts a per-bot DiscordClient so catch-up uses the correct token, plus an
    explicit bot_id so the inbound writes are correctly attributed.
    """
    from app.services.inbound import process_inbound

    settings = get_settings()
    partner_ids = [
        value
        for value in (settings.discord_partner_user_id_a, settings.discord_partner_user_id_b)
        if value
    ]
    processed = 0
    for partner_id in partner_ids:
        user_id = _discord_user_id(partner_id)
        channel_id = await client.get_dm_channel_id(user_id)
        last_seen_id = await pool.fetchval(
            """
            SELECT m.whatsapp_message_id
            FROM messages m
            JOIN user_identities ui ON ui.user_id = m.sender_id
            WHERE m.direction='inbound'
              AND ui.transport='legacy'
              AND ui.address=$1
              AND m.whatsapp_message_id IS NOT NULL
            ORDER BY m.sent_at DESC
            LIMIT 1
            """,
            user_id,
        )
        response = await client._rest.fetch_channel_messages(
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
                transport="discord",
                bot_id=bot_id,
                coalescer_source="catch_up",
            )
            processed += 1
    if processed:
        # obs N/A: no scope in catch-up
        logger.info("discord catch-up ingested %s recent message(s)", processed)
    return processed
