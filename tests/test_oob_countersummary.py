from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.oob_check import summarize_partner_oob

pytestmark = pytest.mark.anyio


class FakeMessages:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self.text)],
            usage=SimpleNamespace(
                input_tokens=60,
                output_tokens=10,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
            ),
        )


def _client(messages: FakeMessages):
    return SimpleNamespace(messages=messages)


def _add_oob(fake_pool, owner_id, sensitive_core: str, shareable_context: str | None = None):
    oob_id = uuid4()
    fake_pool.out_of_bounds[oob_id] = {
        "id": oob_id,
        "owner_id": owner_id,
        "sensitive_core": sensitive_core,
        "shareable_context": shareable_context,
        "severity": "firm",
        "status": "active",
    }
    return oob_id


async def test_countersummary_vagues_single_niche_topic(fake_pool):
    owner_id = uuid4()
    _add_oob(fake_pool, owner_id, "specific one-off situation")

    result = await summarize_partner_oob(fake_pool, owner_id=owner_id)

    assert result.total_count == 1
    assert [(cluster.count, cluster.topic) for cluster in result.clusters] == [(1, "a personal matter")]
    assert result.narrative == "one entry related to a personal matter"


async def test_countersummary_keeps_common_categories_and_counts(fake_pool):
    owner_id = uuid4()
    _add_oob(fake_pool, owner_id, "her father and childhood history")
    _add_oob(fake_pool, owner_id, "mother conflict")
    _add_oob(fake_pool, owner_id, "former partner details")

    result = await summarize_partner_oob(fake_pool, owner_id=owner_id)

    assert result.total_count == 3
    assert [(cluster.count, cluster.topic) for cluster in result.clusters] == [
        (2, "family history"),
        (1, "past relationships"),
    ]
    assert result.narrative == "two entries related to family history, and one entry related to past relationships"


async def test_countersummary_uses_haiku_for_ambiguous_multi_entry_topics(fake_pool, app_env):
    owner_id = uuid4()
    _add_oob(fake_pool, owner_id, "specific one-off situation")
    _add_oob(fake_pool, owner_id, "another specific one-off situation")
    messages = FakeMessages('{"topics":["work","work"]}')

    result = await summarize_partner_oob(fake_pool, owner_id=owner_id, client=_client(messages))

    assert result.total_count == 2
    assert [(cluster.count, cluster.topic) for cluster in result.clusters] == [(2, "work")]
    assert result.narrative == "two entries related to work"
    assert messages.calls[0]["system"][0]["cache_control"] == {"type": "ephemeral"}
