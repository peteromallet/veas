from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app import staging


pytestmark = pytest.mark.anyio


async def test_staging_replay_prints_would_records_without_writes(fake_pool, capsys) -> None:
    user_id = uuid4()
    partner_id = uuid4()
    message_id = uuid4()
    fake_pool.users[user_id] = {"id": user_id, "name": "Maya", "phone": "15555550100", "timezone": "UTC", "onboarding_state": "welcomed"}
    fake_pool.users[partner_id] = {"id": partner_id, "name": "Ben", "phone": "15555550101", "timezone": "UTC", "onboarding_state": "welcomed"}
    fake_pool.messages[message_id] = {
        "id": message_id,
        "direction": "inbound",
        "sender_id": user_id,
        "recipient_id": None,
        "content": "dry run me",
        "processing_state": "processed",
        "sent_at": datetime.now(UTC),
        "charge": "routine",
        "deleted_at": None,
    }

    await staging._replay(fake_pool, "candidate", "2026-01-01", str(user_id))

    output = capsys.readouterr().out
    assert '"would_send"' in output
    assert '"would_write"' in output
    assert fake_pool.bot_turns == {}
    assert len(fake_pool.messages) == 1
