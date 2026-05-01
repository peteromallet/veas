from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.oob_check import check_oob_with_policy

pytestmark = pytest.mark.anyio


class FakeMessages:
    def __init__(self, text: str | None = None, exc: Exception | None = None) -> None:
        self.text = text
        self.exc = exc
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc is not None:
            raise self.exc
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self.text)],
            usage=SimpleNamespace(
                input_tokens=100,
                output_tokens=20,
                cache_creation_input_tokens=10,
                cache_read_input_tokens=5,
            ),
        )


def _client(messages: FakeMessages):
    return SimpleNamespace(messages=messages)


def _active_oob(fake_pool, *, severity: str = "hard", owner_id=None):
    oob_id = uuid4()
    owner_id = owner_id or uuid4()
    fake_pool.out_of_bounds[oob_id] = {
        "id": oob_id,
        "owner_id": owner_id,
        "sensitive_core": "protected detail",
        "shareable_context": "general context",
        "severity": severity,
        "status": "active",
    }
    return owner_id, oob_id


async def test_check_oob_short_circuits_without_active_entries(fake_pool, app_env):
    messages = FakeMessages('{"verdict":"block","reason":"unused","triggering_oob_ids":[],"suggested_rewrite":null,"checker_failed":false}')

    result = await check_oob_with_policy(fake_pool, content="hello", recipient_id=uuid4(), client=_client(messages))

    assert result.verdict == "ok"
    assert result.triggering_oob_ids == []
    assert messages.calls == []


async def test_check_oob_calls_sonnet_and_records_cost(fake_pool, app_env):
    recipient_id, oob_id = _active_oob(fake_pool, severity="firm")
    messages = FakeMessages(
        f'{{"verdict":"rewrite","reason":"too specific","triggering_oob_ids":["{oob_id}"],"suggested_rewrite":"safer","checker_failed":false}}'
    )

    result = await check_oob_with_policy(
        fake_pool,
        content="protected detail",
        recipient_id=recipient_id,
        sender_intent="responding supportively",
        client=_client(messages),
    )

    assert result.verdict == "rewrite"
    assert result.suggested_rewrite == "safer"
    assert result.triggering_oob_ids == [oob_id]
    assert messages.calls[0]["model"] == "claude-sonnet-4-6"
    assert messages.calls[0]["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert fake_pool.llm_spend_log["text"] > 0


async def test_check_oob_includes_current_user_oob_from_protected_owner_ids(fake_pool, app_env):
    current_user_id = uuid4()
    recipient_id = uuid4()
    _, current_user_oob_id = _active_oob(fake_pool, severity="hard", owner_id=current_user_id)
    messages = FakeMessages(
        f'{{"verdict":"block","reason":"leaks current-user OOB","triggering_oob_ids":["{current_user_oob_id}"],"suggested_rewrite":null,"checker_failed":false}}'
    )

    result = await check_oob_with_policy(
        fake_pool,
        content="protected detail",
        recipient_id=recipient_id,
        protected_owner_ids=[current_user_id, recipient_id],
        client=_client(messages),
    )

    assert result.verdict == "block"
    payload = messages.calls[0]["messages"][0]["content"]
    assert str(current_user_oob_id) in payload
    assert str(current_user_id) in payload
    assert str(recipient_id) in payload


async def test_check_oob_fail_closes_for_hard_or_firm(fake_pool, app_env):
    recipient_id, oob_id = _active_oob(fake_pool, severity="hard")

    result = await check_oob_with_policy(
        fake_pool,
        content="protected detail",
        recipient_id=recipient_id,
        client=_client(FakeMessages(exc=TimeoutError("timeout"))),
    )

    assert result.verdict == "block"
    assert result.checker_failed is True
    assert result.triggering_oob_ids == [oob_id]


async def test_check_oob_fail_opens_for_soft_only(fake_pool, app_env, caplog):
    recipient_id, oob_id = _active_oob(fake_pool, severity="soft")

    result = await check_oob_with_policy(
        fake_pool,
        content="protected detail",
        recipient_id=recipient_id,
        client=_client(FakeMessages(exc=TimeoutError("timeout"))),
    )

    assert result.verdict == "ok"
    assert result.checker_failed is True
    assert result.triggering_oob_ids == [oob_id]
    assert "failed open" in caplog.text
