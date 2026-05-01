from __future__ import annotations

import io
import json
from copy import deepcopy
from decimal import Decimal
from uuid import uuid4

import pytest

from app.models.user import User
from evals.execution import EvalTurnExecution, FakeWhatsAppSend, OobCheckRecord
from evals.replay import replay_history

pytestmark = pytest.mark.anyio


async def test_replay_history_emits_jsonl_without_mutating_source(monkeypatch, fake_pool) -> None:
    source_pool = fake_pool
    scratch_pool = type(fake_pool)()
    user_id = uuid4()
    partner_id = uuid4()
    message_id = uuid4()
    source_pool.users[user_id] = {
        "id": user_id,
        "name": "Maya",
        "phone": "15555550100",
        "timezone": "UTC",
        "onboarding_state": "welcomed",
    }
    source_pool.users[partner_id] = {
        "id": partner_id,
        "name": "Ben",
        "phone": "15555550101",
        "timezone": "UTC",
        "onboarding_state": "welcomed",
    }
    source_pool.messages[message_id] = {
        "id": message_id,
        "direction": "inbound",
        "sender_id": user_id,
        "recipient_id": None,
        "content": "I keep asking for help and it turns into a fight.",
        "processing_state": "processed",
        "sent_at": "2026-04-01T12:00:00Z",
        "charge": "charged",
        "whatsapp_message_id": "wamid.replay.1",
        "media_type": None,
        "media_url": None,
        "media_duration_seconds": None,
        "media_analysis": None,
    }
    source_messages_before = deepcopy(source_pool.messages)

    async def fake_run_eval_turn(pool, triggering_message_ids, user: User, *, prompt_version: str):
        assert pool is scratch_pool
        assert triggering_message_ids == [message_id]
        assert user.id == user_id
        assert prompt_version == "v1"
        outbound_id = uuid4()
        pool.messages[outbound_id] = {
            "id": outbound_id,
            "direction": "outbound",
            "sender_id": None,
            "recipient_id": user_id,
            "content": "It sounds like this keeps becoming a fight right when you ask for help.",
            "processing_state": "processed",
            "sent_at": "2026-04-01T12:01:00Z",
            "charge": None,
        }
        tool_call = {
            "id": uuid4(),
            "turn_id": uuid4(),
            "tool_name": "log_observation",
            "arguments": {"content": "Requests for help are escalating into fights."},
            "result": {"ok": True},
            "created_at": "2026-04-01T12:01:01Z",
        }
        pool.tool_calls.append(tool_call)
        pool.llm_spend_log["text"] = Decimal("0.12")
        return EvalTurnExecution(
            tool_calls=[
                {
                    "tool_name": "log_observation",
                    "arguments": {"content": "Requests for help are escalating into fights."},
                    "result": {"ok": True},
                    "phase": "write",
                }
            ],
            whatsapp_sends=[
                FakeWhatsAppSend(
                    "text",
                    "15555550100",
                    "It sounds like this keeps becoming a fight right when you ask for help.",
                    "eval-text-1",
                )
            ],
            oob_checks=[OobCheckRecord("safe", str(user_id), {"action": "pass"})],
        )

    monkeypatch.setattr("evals.replay.run_eval_turn", fake_run_eval_turn)

    output = io.StringIO()
    records = await replay_history(
        source_pool,
        scratch_pool,
        since="2026-04-01",
        user_id=str(user_id),
        prompt_version="v1",
        output=output,
    )

    assert source_pool.messages == source_messages_before
    assert len(records) == 1
    line = json.loads(output.getvalue())
    assert line["message_id"] == str(message_id)
    assert line["prompt_version"] == "v1"
    assert line["would_send"].startswith("It sounds like this keeps becoming")
    assert line["would_write"]["tool_calls"][0]["tool_name"] == "log_observation"
    assert line["tool_transcript"][0]["tool_name"] == "log_observation"
    assert line["oob_outcome"] == "pass"
    assert line["charge"] == "charged"
    assert line["cost_usd"] == "0.12"
