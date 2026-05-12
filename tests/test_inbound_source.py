import time
from typing import NamedTuple

import pytest

from app.services.inbound import process_inbound


pytestmark = pytest.mark.anyio


class _Charge(NamedTuple):
    charge: str


class _Coalescer:
    def __init__(self) -> None:
        self.calls = []

    async def add(self, user_id, message_id, user, *, source: str = "live", bot_id: str | None = None) -> None:
        self.calls.append((user_id, message_id, user, source))


def _payload(message_id: str = "wamid.source") -> dict:
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "contacts": [{"wa_id": "15555550100", "profile": {"name": "Maya"}}],
                            "messages": [
                                {
                                    "from": "15555550100",
                                    "id": message_id,
                                    "timestamp": str(int(time.time())),
                                    "type": "text",
                                    "text": {"body": "hello"},
                                }
                            ],
                        }
                    }
                ]
            }
        ]
    }


async def test_process_inbound_defaults_coalescer_source_to_live(fake_pool, monkeypatch) -> None:
    async def fake_classify_charge(pool, content):
        return _Charge("routine")

    monkeypatch.setattr("app.services.inbound.classify_charge", fake_classify_charge)
    coalescer = _Coalescer()

    await process_inbound(fake_pool, _payload(), coalescer)

    assert len(coalescer.calls) == 1
    assert coalescer.calls[0][3] == "live"


async def test_process_inbound_forwards_explicit_coalescer_source(fake_pool, monkeypatch) -> None:
    async def fake_classify_charge(pool, content):
        return _Charge("routine")

    monkeypatch.setattr("app.services.inbound.classify_charge", fake_classify_charge)
    coalescer = _Coalescer()

    await process_inbound(fake_pool, _payload("wamid.catchup"), coalescer, coalescer_source="catch_up")

    assert len(coalescer.calls) == 1
    assert coalescer.calls[0][3] == "catch_up"
