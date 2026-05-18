import pytest
from datetime import UTC, datetime, timedelta
from uuid import uuid4
from app.services.inbound_queue import _claim_messages_for_turn_in_tx
from app.bots.registry import get_relationship_topic_id

pytestmark = pytest.mark.anyio


async def test_no_duplicate_inflight_bot_turn(fake_pool) -> None:
    fp = fake_pool
    """A second in-flight bot_turn cannot claim the same message."""
    u = uuid4(); fp.users[u] = {"id": u, "name": "M", "phone": "1", "timezone": "UTC"}
    t = get_relationship_topic_id(); m = uuid4()
    fp.messages[m] = {"id": m, "direction": "inbound", "sender_id": u,
        "content": "hi", "processing_state": "raw", "bot_id": "mediator", "topic_id": t,
        "sent_at": datetime.now(UTC) - timedelta(minutes=1),
        "bot_turn_id": None, "processing_started_at": None, "processing_attempts": 0}
    INS = ("INSERT INTO bot_turns (triggered_by_message_id,triggering_message_ids,"
        "user_in_context,system_prompt_version,model_version,prompt_snapshot,"
        "prompt_snapshot_encrypted,bot_id,topic_id,bot_spec_version,"
        "hot_context_builder_version,tool_schema_version)"
        " VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12) RETURNING id")
    ARGS = ("v1", "m", "s", "e", "mediator", t, "sv", "hc", "ts")
    async with fp.acquire() as c:
        async with c.transaction():
            await c.execute("SELECT set_config('app.lifecycle_writer','inbound_queue',true)")
            T1 = (await c.fetchrow(INS, m, [m], u, *ARGS))["id"]
            assert [m] == await _claim_messages_for_turn_in_tx(
                c, [m], bot_id="mediator", topic_id=t, new_bot_turn_id=T1)
    assert fp.messages[m]["bot_turn_id"] == T1
    async with fp.acquire() as c:
        async with c.transaction():
            await c.execute("SELECT set_config('app.lifecycle_writer','inbound_queue',true)")
            T2 = (await c.fetchrow(INS, m, [m], u, *ARGS))["id"]
            assert [] == await _claim_messages_for_turn_in_tx(
                c, [m], bot_id="mediator", topic_id=t, new_bot_turn_id=T2)
            del fp.bot_turns[T2]
    assert fp.messages[m]["bot_turn_id"] == T1
    assert T1 in fp.bot_turns and T2 not in fp.bot_turns
