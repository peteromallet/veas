from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.scoring import rescore_observations, score_observation

pytestmark = pytest.mark.anyio


class FakeMessages:
    def __init__(self, texts: list[str] | None = None, exc: Exception | None = None) -> None:
        self.texts = texts or []
        self.exc = exc
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc is not None:
            raise self.exc
        text = self.texts.pop(0)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)],
            usage=SimpleNamespace(
                input_tokens=80,
                output_tokens=10,
                cache_creation_input_tokens=20,
                cache_read_input_tokens=0,
            ),
        )


def _client(messages: FakeMessages):
    return SimpleNamespace(messages=messages)


class RescorePool:
    def __init__(self) -> None:
        self.llm_spend_log = {}
        self.observations = {
            uuid4(): {
                "id": uuid4(),
                "content": "old observation",
                "significance": 3,
                "scoring_prompt_version": "v0-stub",
                "created_at": datetime.now(UTC),
            },
            uuid4(): {
                "id": uuid4(),
                "content": "failed observation",
                "significance": None,
                "scoring_prompt_version": "v1-failed",
                "created_at": datetime.now(UTC),
            },
        }
        self.observations = {row["id"]: row for row in self.observations.values()}

    async def fetchval(self, sql: str, *args):
        return self.llm_spend_log.get(args[0], 0)

    async def fetch(self, sql: str, *args):
        threshold = args[0]
        return [
            {"id": row["id"], "content": row["content"]}
            for row in self.observations.values()
            if row.get("scoring_prompt_version") is None
            or row.get("scoring_prompt_version") < threshold
            or str(row.get("scoring_prompt_version", "")).endswith("failed")
        ]

    async def execute(self, sql: str, *args):
        if "INSERT INTO llm_spend_log" in sql:
            provider, dollars = args
            self.llm_spend_log[provider] = self.llm_spend_log.get(provider, 0) + dollars
            return "INSERT 0 1"
        if "UPDATE observations" in sql:
            significance, prompt_version, observation_id = args
            self.observations[observation_id]["significance"] = significance
            self.observations[observation_id]["scoring_prompt_version"] = prompt_version
            return "UPDATE 1"
        raise AssertionError(sql)


async def test_score_observation_returns_valid_score_and_records_cost(fake_pool, app_env):
    messages = FakeMessages(['{"score":4,"reason":"material relationship pattern"}'])

    score, reason, prompt_version = await score_observation(
        fake_pool,
        content="Their repairs work better after walks.",
        client=_client(messages),
    )

    assert score == 4
    assert reason == "material relationship pattern"
    assert prompt_version == "v1"
    assert messages.calls[0]["model"] == "claude-haiku-4-5-20251001"
    assert messages.calls[0]["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert fake_pool.llm_spend_log["text"] > 0


async def test_score_observation_failure_returns_null_and_failed_version(fake_pool, app_env):
    score, reason, prompt_version = await score_observation(
        fake_pool,
        content="bad",
        client=_client(FakeMessages(["not json"])),
    )

    assert score is None
    assert reason.startswith("scoring failed:")
    assert prompt_version == "v1-failed"


async def test_rescore_observations_reports_counts(app_env):
    pool = RescorePool()
    messages = FakeMessages(
        [
            '{"score":5,"reason":"core"}',
            '{"score":2,"reason":"minor"}',
        ]
    )

    report = await rescore_observations(pool, client=_client(messages))

    assert report.scanned == 2
    assert report.rescored == 2
    assert report.still_failed == 0
    assert {row["scoring_prompt_version"] for row in pool.observations.values()} == {"v1"}
    assert {row["significance"] for row in pool.observations.values()} == {2, 5}
