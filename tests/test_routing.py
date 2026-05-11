"""Unit tests for app.services.routing — resolve_bot, resolve_sender, resolve_binding.

Uses a local RoutingFakePool(FakePool) subclass that adds dicts for
channels, user_identities, bot_bindings, dyads, and dyad_members.
Does NOT modify tests/conftest.py (out-of-scope dirty file).
"""

from uuid import uuid4

import pytest

from app.services.routing import BindingResolution, resolve_binding, resolve_bot, resolve_sender

# Import FakePool from conftest without polluting the module scope
from tests.conftest import FakePool


class RoutingFakePool(FakePool):
    """FakePool extended with routing-specific tables for unit tests."""

    def __init__(self) -> None:
        super().__init__()
        self.channels: dict[str, dict] = {}
        self.user_identities: dict[tuple, dict] = {}
        self.bot_bindings: dict[str, dict] = {}
        self.dyads: dict[str, dict] = {}
        self.dyad_members: dict[tuple, dict] = {}

    async def fetchrow(self, sql: str, *args):  # noqa: C901
        compact = " ".join(sql.split())

        # --- resolve_bot: SELECT bot_id FROM channels WHERE transport=$1 AND address=$2 LIMIT 1
        if "SELECT bot_id FROM channels WHERE transport" in compact:
            transport, address = args
            for ch in self.channels.values():
                if ch["transport"] == transport and ch["address"] == address:
                    return {"bot_id": ch["bot_id"]}
            return None

        # --- resolve_sender: SELECT user_id FROM user_identities WHERE transport=$1 AND address=$2
        if "SELECT user_id FROM user_identities WHERE transport" in compact:
            transport, address = args
            key = (transport, address)
            row = self.user_identities.get(key)
            if row is not None:
                return {"user_id": row["user_id"]}
            return None

        # --- resolve_binding: SELECT bb.id, bb.bot_id, bb.dyad_id, bb.user_id
        #     FROM bot_bindings bb LEFT JOIN dyad_members dm ...
        if "FROM bot_bindings bb" in compact and "LEFT JOIN dyad_members dm" in compact:
            bot_id, user_id = args
            for bb in self.bot_bindings.values():
                if bb["bot_id"] != bot_id:
                    continue
                # Direct user match
                if bb.get("user_id") == user_id:
                    return {
                        "binding_id": bb["id"],
                        "bot_id": bb["bot_id"],
                        "dyad_id": bb.get("dyad_id"),
                        "user_id": bb.get("user_id"),
                    }
                # Dyad membership match
                if bb.get("dyad_id") is not None:
                    for dm in self.dyad_members.values():
                        if dm["dyad_id"] == bb["dyad_id"] and dm["user_id"] == user_id:
                            return {
                                "binding_id": bb["id"],
                                "bot_id": bb["bot_id"],
                                "dyad_id": bb.get("dyad_id"),
                                "user_id": bb.get("user_id"),
                            }
            return None

        # Fall back to parent for anything else
        return await super().fetchrow(sql, *args)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def routing_pool() -> RoutingFakePool:
    return RoutingFakePool()


# ---------------------------------------------------------------------------
# resolve_bot tests
# ---------------------------------------------------------------------------


class TestResolveBot:
    async def test_resolve_bot_found(self, routing_pool: RoutingFakePool) -> None:
        channel_id = uuid4()
        routing_pool.channels[str(channel_id)] = {
            "id": channel_id,
            "bot_id": "mediator",
            "transport": "discord",
            "address": "123456",
            "guild_id": None,
            "channel_id": None,
            "config": {},
            "created_at": None,
        }
        result = await resolve_bot(routing_pool, transport="discord", address="123456")
        assert result == "mediator"

    async def test_resolve_bot_not_found(self, routing_pool: RoutingFakePool) -> None:
        result = await resolve_bot(routing_pool, transport="discord", address="nonexistent")
        assert result is None

    async def test_resolve_bot_wrong_transport(self, routing_pool: RoutingFakePool) -> None:
        channel_id = uuid4()
        routing_pool.channels[str(channel_id)] = {
            "id": channel_id,
            "bot_id": "mediator",
            "transport": "discord",
            "address": "123456",
            "guild_id": None,
            "channel_id": None,
            "config": {},
            "created_at": None,
        }
        result = await resolve_bot(routing_pool, transport="whatsapp", address="123456")
        assert result is None

    async def test_resolve_bot_multiple_channels_returns_first(self, routing_pool: RoutingFakePool) -> None:
        c1 = uuid4()
        c2 = uuid4()
        routing_pool.channels[str(c1)] = {
            "id": c1,
            "bot_id": "mediator",
            "transport": "discord",
            "address": "123456",
            "guild_id": None,
            "channel_id": None,
            "config": {},
            "created_at": None,
        }
        routing_pool.channels[str(c2)] = {
            "id": c2,
            "bot_id": "other_bot",
            "transport": "discord",
            "address": "123456",
            "guild_id": None,
            "channel_id": None,
            "config": {},
            "created_at": None,
        }
        result = await resolve_bot(routing_pool, transport="discord", address="123456")
        assert result is not None


# ---------------------------------------------------------------------------
# resolve_sender tests
# ---------------------------------------------------------------------------


class TestResolveSender:
    async def test_resolve_sender_found(self, routing_pool: RoutingFakePool) -> None:
        user_id = uuid4()
        routing_pool.user_identities[("discord", "123456")] = {
            "transport": "discord",
            "address": "123456",
            "user_id": user_id,
            "verified_at": None,
            "created_at": None,
        }
        result = await resolve_sender(routing_pool, transport="discord", address="123456")
        assert result == user_id

    async def test_resolve_sender_not_found(self, routing_pool: RoutingFakePool) -> None:
        result = await resolve_sender(routing_pool, transport="discord", address="nonexistent")
        assert result is None

    async def test_resolve_sender_wrong_transport(self, routing_pool: RoutingFakePool) -> None:
        user_id = uuid4()
        routing_pool.user_identities[("discord", "123456")] = {
            "transport": "discord",
            "address": "123456",
            "user_id": user_id,
            "verified_at": None,
            "created_at": None,
        }
        result = await resolve_sender(routing_pool, transport="whatsapp", address="123456")
        assert result is None

    async def test_resolve_sender_legacy_transport(self, routing_pool: RoutingFakePool) -> None:
        user_id = uuid4()
        routing_pool.user_identities[("legacy", "+15555550100")] = {
            "transport": "legacy",
            "address": "+15555550100",
            "user_id": user_id,
            "verified_at": None,
            "created_at": None,
        }
        result = await resolve_sender(routing_pool, transport="legacy", address="+15555550100")
        assert result == user_id


# ---------------------------------------------------------------------------
# resolve_binding tests
# ---------------------------------------------------------------------------


class TestResolveBinding:
    async def test_resolve_binding_direct_user(self, routing_pool: RoutingFakePool) -> None:
        binding_id = uuid4()
        user_id = uuid4()
        routing_pool.bot_bindings[str(binding_id)] = {
            "id": binding_id,
            "bot_id": "mediator",
            "dyad_id": None,
            "user_id": user_id,
            "created_at": None,
        }
        result = await resolve_binding(routing_pool, bot_id="mediator", user_id=user_id)
        assert result is not None
        assert result.binding_id == binding_id
        assert result.bot_id == "mediator"
        assert result.user_id == user_id
        assert result.dyad_id is None

    async def test_resolve_binding_via_dyad(self, routing_pool: RoutingFakePool) -> None:
        binding_id = uuid4()
        dyad_id = uuid4()
        user_id = uuid4()
        routing_pool.dyads[str(dyad_id)] = {"id": dyad_id, "created_at": None}
        routing_pool.dyad_members[(str(dyad_id), str(user_id))] = {
            "dyad_id": dyad_id,
            "user_id": user_id,
            "joined_at": None,
        }
        routing_pool.bot_bindings[str(binding_id)] = {
            "id": binding_id,
            "bot_id": "mediator",
            "dyad_id": dyad_id,
            "user_id": None,
            "created_at": None,
        }
        result = await resolve_binding(routing_pool, bot_id="mediator", user_id=user_id)
        assert result is not None
        assert result.binding_id == binding_id
        assert result.bot_id == "mediator"
        assert result.dyad_id == dyad_id
        assert result.user_id is None

    async def test_resolve_binding_not_found(self, routing_pool: RoutingFakePool) -> None:
        user_id = uuid4()
        result = await resolve_binding(routing_pool, bot_id="mediator", user_id=user_id)
        assert result is None

    async def test_resolve_binding_wrong_bot(self, routing_pool: RoutingFakePool) -> None:
        binding_id = uuid4()
        user_id = uuid4()
        routing_pool.bot_bindings[str(binding_id)] = {
            "id": binding_id,
            "bot_id": "mediator",
            "dyad_id": None,
            "user_id": user_id,
            "created_at": None,
        }
        result = await resolve_binding(routing_pool, bot_id="other_bot", user_id=user_id)
        assert result is None

    async def test_resolve_binding_returns_namedtuple(self, routing_pool: RoutingFakePool) -> None:
        binding_id = uuid4()
        user_id = uuid4()
        routing_pool.bot_bindings[str(binding_id)] = {
            "id": binding_id,
            "bot_id": "mediator",
            "dyad_id": None,
            "user_id": user_id,
            "created_at": None,
        }
        result = await resolve_binding(routing_pool, bot_id="mediator", user_id=user_id)
        assert isinstance(result, BindingResolution)
        assert result.binding_id == binding_id
        assert result.bot_id == "mediator"