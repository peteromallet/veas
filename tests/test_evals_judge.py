from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from evals.judge import RUBRIC_JUDGE_PROMPT_VERSION, judge_outbound_assertions, judge_outbound_text

pytestmark = pytest.mark.anyio


USAGE = {
    "input_tokens": 100,
    "cache_creation_input_tokens": 20,
    "cache_read_input_tokens": 10,
    "output_tokens": 5,
}


class FakeMessages:
    def __init__(self, texts: list[str]) -> None:
        self.texts = list(texts)
        self.requests: list[dict] = []

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self.texts.pop(0))],
            usage=USAGE,
        )


class FakeClient:
    def __init__(self, texts: list[str]) -> None:
        self.messages = FakeMessages(texts)


async def test_judge_calls_once_per_criterion_and_records_cost(app_env, fake_pool) -> None:
    client = FakeClient(
        [
            '{"passes":true,"reason":"names behavior without labels"}',
            '{"passes":false,"reason":"does not ask the requested question"}',
        ]
    )

    verdicts = await judge_outbound_assertions(
        fake_pool,
        "That sounds hard.",
        ["does not diagnose", "asks a question"],
        client=client,
    )

    assert len(client.messages.requests) == 2
    assert [verdict["passes"] for verdict in verdicts] == [True, False]
    assert all(verdict["judge_prompt_version"] == RUBRIC_JUDGE_PROMPT_VERSION for verdict in verdicts)
    assert fake_pool.llm_spend_log["text"] == Decimal("0.000726")
    assert client.messages.requests[0]["system"][0]["cache_control"] == {"type": "ephemeral"}


async def test_judge_fails_deliberately_clinical_outbound(app_env, fake_pool) -> None:
    client = FakeClient(['{"passes":false,"reason":"uses clinical label avoidant"}'])

    verdict = await judge_outbound_text(
        fake_pool,
        "She sounds avoidant and attachment-wounded.",
        "does NOT use clinical language like avoidant or attachment",
        client=client,
    )

    assert verdict.passes is False
    assert "avoidant" in verdict.reason
    assert verdict.judge_prompt_version == RUBRIC_JUDGE_PROMPT_VERSION


async def test_judge_malformed_output_fails_closed_after_recording_cost(app_env, fake_pool) -> None:
    client = FakeClient(["not json"])

    verdict = await judge_outbound_text(fake_pool, "hello", "must be kind", client=client)

    assert verdict.passes is False
    assert verdict.judge_prompt_version == f"{RUBRIC_JUDGE_PROMPT_VERSION}-failed"
    assert verdict.reason.startswith("rubric judge failed:")
    assert fake_pool.llm_spend_log["text"] == Decimal("0.000363")
