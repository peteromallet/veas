import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.config import get_settings
from app.services.discord import (
    DiscordGatewayBot,
    catch_up_recent_messages,
    is_allowed_discord_user,
    message_to_meta_payload,
    seed_partner_users,
)


def test_discord_message_to_meta_payload() -> None:
    payload = message_to_meta_payload(
        {
            "id": "123",
            "content": "hello",
            "timestamp": "2026-04-30T20:00:00.000000+00:00",
            "author": {"id": "456", "username": "maya", "global_name": "Maya"},
        }
    )

    value = payload["entry"][0]["changes"][0]["value"]
    assert value["contacts"][0]["wa_id"] == "456"
    assert value["contacts"][0]["profile"]["name"] == "Maya"
    assert value["messages"][0]["from"] == "456"
    assert value["messages"][0]["id"] == "123"
    assert value["messages"][0]["type"] == "text"
    assert value["messages"][0]["text"]["body"] == "hello"


def test_discord_message_to_meta_payload_uses_configured_name(app_env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_PARTNER_USER_ID_A", "456")
    monkeypatch.setenv("DISCORD_PARTNER_NAME_A", "Partner A")
    get_settings.cache_clear()

    payload = message_to_meta_payload(
        {
            "id": "123",
            "content": "hello",
            "author": {"id": "456", "username": "pom", "global_name": None},
        }
    )

    value = payload["entry"][0]["changes"][0]["value"]
    assert value["contacts"][0]["profile"]["name"] == "Partner A"
    get_settings.cache_clear()


def test_discord_allowlist_uses_discord_partner_ids(app_env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MESSAGING_PROVIDER", "discord")
    monkeypatch.setenv("DISCORD_PARTNER_USER_ID_A", "456")
    monkeypatch.setenv("DISCORD_PARTNER_USER_ID_B", "789")
    get_settings.cache_clear()

    assert is_allowed_discord_user("456")
    assert is_allowed_discord_user("789")
    assert not is_allowed_discord_user("999")

    get_settings.cache_clear()


async def test_discord_gateway_drops_non_partner(fake_pool, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MESSAGING_PROVIDER", "discord")
    monkeypatch.setenv("DISCORD_PARTNER_USER_ID_A", "456")
    monkeypatch.setenv("DISCORD_PARTNER_USER_ID_B", "789")
    get_settings.cache_clear()
    calls = []

    async def process_inbound(pool, payload, coalescer=None):
        calls.append(payload)

    monkeypatch.setattr("app.services.inbound.process_inbound", process_inbound)
    bot = DiscordGatewayBot(fake_pool, None)
    await bot._handle_message(
        {
            "id": "123",
            "content": "hello",
            "channel_id": "channel-1",
            "author": {"id": "999", "username": "stranger"},
        }
    )

    assert calls == []
    get_settings.cache_clear()


async def test_discord_gateway_accepts_partner(fake_pool, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MESSAGING_PROVIDER", "discord")
    monkeypatch.setenv("DISCORD_PARTNER_USER_ID_A", "456")
    monkeypatch.setenv("DISCORD_PARTNER_USER_ID_B", "789")
    get_settings.cache_clear()
    calls = []

    async def process_inbound(pool, payload, coalescer=None):
        calls.append(payload)

    async def send_typing_after_delay(channel_id):
        calls.append({"typing": channel_id})

    monkeypatch.setattr("app.services.inbound.process_inbound", process_inbound)
    monkeypatch.setattr("app.services.discord._send_typing_after_delay", send_typing_after_delay)
    bot = DiscordGatewayBot(fake_pool, None)
    await bot._handle_message(
        {
            "id": "123",
            "content": "hello",
            "channel_id": "channel-1",
            "author": {"id": "456", "username": "maya"},
            }
        )
    await asyncio.sleep(0)

    assert {"typing": "channel-1"} in calls
    assert any("entry" in call for call in calls)
    get_settings.cache_clear()


async def test_seed_partner_users_upserts_configured_discord_ids(fake_pool, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_PARTNER_USER_ID_A", "456")
    monkeypatch.setenv("DISCORD_PARTNER_USER_ID_B", "discord:789")
    monkeypatch.setenv("DISCORD_PARTNER_NAME_A", "Partner A")
    monkeypatch.setenv("DISCORD_PARTNER_NAME_B", "Partner B")
    get_settings.cache_clear()

    await seed_partner_users(fake_pool)

    users = {row["phone"]: row["name"] for row in fake_pool.users.values()}
    assert users == {"456": "Partner A", "789": "Partner B"}
    get_settings.cache_clear()


async def test_catch_up_recent_messages_ingests_partner_history(fake_pool, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_PARTNER_USER_ID_A", "456")
    monkeypatch.setenv("DISCORD_PARTNER_NAME_A", "Partner A")
    monkeypatch.setenv("DISCORD_PARTNER_USER_ID_B", "")
    monkeypatch.setenv("MESSAGING_PROVIDER", "discord")
    get_settings.cache_clear()
    await seed_partner_users(fake_pool)
    calls = []

    async def get_dm_channel_id(user_id):
        assert user_id == "456"
        return "channel-1"

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return [
                {"id": "m2", "content": "second", "author": {"id": "456", "username": "p"}},
                {"id": "m1", "content": "first", "author": {"id": "456", "username": "p"}},
            ]

    class Client:
        async def get(self, path, headers=None, params=None):
            calls.append((path, params))
            return Response()

    monkeypatch.setattr("app.services.discord.get_dm_channel_id", get_dm_channel_id)
    async def get_client():
        return Client()

    monkeypatch.setattr("app.services.discord._get_client", get_client)

    count = await catch_up_recent_messages(fake_pool, None)

    assert count == 2
    assert calls == [("/channels/channel-1/messages", {"limit": 50})]
    inbound_ids = {
        row["whatsapp_message_id"]
        for row in fake_pool.messages.values()
        if row["direction"] == "inbound"
    }
    assert inbound_ids == {"m1", "m2"}
    get_settings.cache_clear()


async def test_discord_gateway_logs_reaction_feedback(fake_pool, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MESSAGING_PROVIDER", "discord")
    monkeypatch.setenv("DISCORD_PARTNER_USER_ID_A", "456")
    monkeypatch.setenv("DISCORD_PARTNER_NAME_A", "Partner A")
    get_settings.cache_clear()
    await seed_partner_users(fake_pool)
    outbound_id = uuid4()
    fake_pool.messages[outbound_id] = {
        "id": outbound_id,
        "direction": "outbound",
        "sender_id": None,
        "recipient_id": next(iter(fake_pool.users)),
        "content": "I hear you.",
        "processing_state": "processed",
        "sent_at": datetime.now(UTC),
        "charge": "routine",
        "whatsapp_message_id": "discord-out-1",
        "media_type": None,
        "media_url": None,
        "media_duration_seconds": None,
        "media_analysis": None,
        "edit_history": None,
        "edited_at": None,
        "deleted_at": None,
    }

    bot = DiscordGatewayBot(fake_pool, None)
    await bot._handle_reaction_add(
        {"user_id": "456", "message_id": "discord-out-1", "emoji": {"name": "👍"}}
    )

    feedback = next(iter(fake_pool.feedback.values()))
    assert feedback["source"] == "reaction"
    assert feedback["target_type"] == "message"
    assert feedback["target_id"] == outbound_id
    assert feedback["sentiment"] == "positive"
    assert feedback["content"] == "👍"
    get_settings.cache_clear()


async def test_discord_gateway_updates_and_deletes_messages(fake_pool, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MESSAGING_PROVIDER", "discord")
    monkeypatch.setenv("DISCORD_PARTNER_USER_ID_A", "456")
    get_settings.cache_clear()
    message_id = uuid4()
    fake_pool.messages[message_id] = {
        "id": message_id,
        "direction": "inbound",
        "sender_id": uuid4(),
        "recipient_id": None,
        "content": "old",
        "processing_state": "processed",
        "sent_at": datetime.now(UTC),
        "charge": "routine",
        "whatsapp_message_id": "discord-in-1",
        "media_type": None,
        "media_url": None,
        "media_duration_seconds": None,
        "media_analysis": None,
        "edit_history": None,
        "edited_at": None,
        "deleted_at": None,
    }

    bot = DiscordGatewayBot(fake_pool, None)
    await bot._handle_message_update(
        {"id": "discord-in-1", "content": "new", "author": {"id": "456"}}
    )
    await bot._handle_message_delete({"id": "discord-in-1"})

    assert fake_pool.messages[message_id]["content"] == "new"
    assert fake_pool.messages[message_id]["edit_history"][0]["content"] == "old"
    assert fake_pool.messages[message_id]["edited_at"] is not None
    assert fake_pool.messages[message_id]["deleted_at"] is not None
    get_settings.cache_clear()
