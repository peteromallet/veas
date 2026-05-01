from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import anthropic

from app.config import get_settings
from app.services.spend import is_under_cap, record_llm_cost

logger = logging.getLogger(__name__)

RUBRIC_JUDGE_PROMPT_VERSION = "rubric_judge_v1"
RUBRIC_JUDGE_PROMPT_PATH = Path(__file__).with_name("prompts") / f"{RUBRIC_JUDGE_PROMPT_VERSION}.md"
JUDGE_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class JudgeVerdict:
    criterion: str
    passes: bool
    reason: str
    judge_prompt_version: str
    cost_usd: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "criterion": self.criterion,
            "passes": self.passes,
            "reason": self.reason,
            "judge_prompt_version": self.judge_prompt_version,
            "cost_usd": self.cost_usd,
        }


async def judge_outbound_assertions(
    pool: Any,
    outbound_text: str,
    criteria: list[str],
    *,
    client: Any | None = None,
) -> list[dict[str, Any]]:
    verdicts: list[dict[str, Any]] = []
    for criterion in criteria:
        verdict = await judge_outbound_text(pool, outbound_text, criterion, client=client)
        verdicts.append(verdict.as_dict())
    return verdicts


async def judge_outbound_text(
    pool: Any,
    outbound_text: str,
    criterion: str,
    *,
    client: Any | None = None,
) -> JudgeVerdict:
    settings = get_settings()
    if client is None:
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key.get_secret_value())
    cost = Decimal("0")
    try:
        if not await is_under_cap(pool, "text"):
            raise RuntimeError("text LLM spend cap exceeded")
        async with asyncio.timeout(JUDGE_TIMEOUT_SECONDS):
            response = await client.messages.create(
                model=settings.oob_checker_model,
                max_tokens=300,
                system=[
                    {
                        "type": "text",
                        "text": _judge_prompt(),
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "outbound_text": outbound_text,
                                "criterion": criterion,
                            }
                        ),
                    }
                ],
            )
        cost = await _record_response_cost(pool, _attr(response, "usage", {}))
        passes, reason = _parse_judge_json(_response_text(response))
        return JudgeVerdict(criterion, passes, reason, RUBRIC_JUDGE_PROMPT_VERSION, str(cost))
    except Exception as exc:
        logger.warning("rubric judge failed: %s", exc)
        return JudgeVerdict(
            criterion,
            False,
            f"rubric judge failed: {exc}",
            f"{RUBRIC_JUDGE_PROMPT_VERSION}-failed",
            str(cost),
        )


def _judge_prompt() -> str:
    return RUBRIC_JUDGE_PROMPT_PATH.read_text(encoding="utf-8")


def _parse_judge_json(text: str) -> tuple[bool, str]:
    data = json.loads(text)
    passes = data.get("passes")
    reason = data.get("reason")
    if not isinstance(passes, bool):
        raise ValueError("judge verdict must include boolean passes")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("judge verdict must include non-empty reason")
    return passes, reason.strip()


def _response_text(response: Any) -> str:
    parts: list[str] = []
    for block in _attr(response, "content", []) or []:
        if _attr(block, "type") == "text":
            parts.append(str(_attr(block, "text", "")))
    return "\n".join(part for part in parts if part).strip()


async def _record_response_cost(pool: Any, usage: Any) -> Decimal:
    settings = get_settings()
    input_tokens = _usage_tokens(usage, "input_tokens")
    cache_create = _usage_tokens(usage, "cache_creation_input_tokens")
    cache_read = _usage_tokens(usage, "cache_read_input_tokens")
    output_tokens = _usage_tokens(usage, "output_tokens")
    regular_input_tokens = max(0, input_tokens - cache_create - cache_read)
    dollars = Decimal(
        str(
            (
                regular_input_tokens * settings.anthropic_input_usd_per_mtok
                + cache_create * settings.anthropic_input_usd_per_mtok * 1.25
                + cache_read * settings.anthropic_input_usd_per_mtok * 0.10
                + output_tokens * settings.anthropic_output_usd_per_mtok
            )
            / 1_000_000
        )
    )
    if dollars > 0:
        await record_llm_cost(pool, "text", dollars)
    return dollars


def _usage_tokens(usage: Any, field: str) -> int:
    return int(_attr(usage, field, 0) or 0)


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)
