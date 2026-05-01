from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services.charge import FAILED_CHARGE_PROMPT_VERSION, classify_charge

pytestmark = pytest.mark.anyio


USAGE = {
    "input_tokens": 100,
    "cache_creation_input_tokens": 20,
    "cache_read_input_tokens": 10,
    "output_tokens": 5,
}


class FakeMessages:
    def __init__(self, text: str) -> None:
        self.text = text
        self.requests: list[dict] = []

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self.text)],
            usage=USAGE,
        )


class FakeClient:
    def __init__(self, text: str) -> None:
        self.messages = FakeMessages(text)


@pytest.mark.parametrize("label", ["routine", "notable", "charged", "crisis"])
async def test_classify_charge_accepts_all_labels(app_env, fake_pool, label: str) -> None:
    client = FakeClient(f'{{"charge":"{label}","reason":"fixture"}}')

    classification = await classify_charge(fake_pool, "message text", client=client)

    assert classification.charge == label
    assert classification.reason == "fixture"
    assert classification.prompt_version == "v1"
    assert fake_pool.llm_spend_log["text"] == Decimal("0.000121")
    assert client.messages.requests[0]["system"][0]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.parametrize(
    "bad_text",
    [
        "not json",
        '{"charge":"tense","reason":"bad label"}',
        '{"charge":"routine"}',
    ],
)
async def test_classify_charge_malformed_output_falls_back_to_routine(app_env, fake_pool, bad_text: str) -> None:
    classification = await classify_charge(fake_pool, "message text", client=FakeClient(bad_text))

    assert classification.charge == "routine"
    assert classification.prompt_version == FAILED_CHARGE_PROMPT_VERSION
    assert classification.reason.startswith("charge classification failed:")
    assert fake_pool.llm_spend_log["text"] == Decimal("0.000121")


async def test_classify_charge_placeholder_key_falls_back_without_client_call(app_env, fake_pool) -> None:
    classification = await classify_charge(fake_pool, "message text")

    assert classification.charge == "routine"
    assert classification.prompt_version == FAILED_CHARGE_PROMPT_VERSION
    assert "placeholder Anthropic API key" in classification.reason
    assert fake_pool.llm_spend_log == {}


async def test_classify_charge_fallback_uses_keywords_for_charged_content(app_env, fake_pool) -> None:
    classification = await classify_charge(fake_pool, "She snaps at me and I feel like she hates me")

    assert classification.charge == "charged"
    assert classification.prompt_version == FAILED_CHARGE_PROMPT_VERSION
    assert "keyword fallback" in classification.reason


async def test_classify_charge_fallback_catches_miscarriage_and_volatile_language(app_env, fake_pool) -> None:
    classification = await classify_charge(
        fake_pool,
        "The miscarriage is connected to how volatile this feels; everything derails into resentment",
    )

    assert classification.charge == "charged"
    assert classification.prompt_version == FAILED_CHARGE_PROMPT_VERSION
    assert "keyword fallback" in classification.reason


async def test_classify_charge_fallback_uses_keywords_for_crisis_content(app_env, fake_pool) -> None:
    classification = await classify_charge(fake_pool, "I got violent and hurt her")

    assert classification.charge == "crisis"
    assert classification.prompt_version == FAILED_CHARGE_PROMPT_VERSION
    assert "keyword fallback" in classification.reason
