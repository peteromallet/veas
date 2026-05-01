"""Observation significance scoring helpers."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, NamedTuple

import anthropic

from app.config import get_settings
from app.services.spend import is_under_cap, record_llm_cost

logger = logging.getLogger(__name__)

SCORING_PROMPT_VERSION = "v1"
FAILED_SCORING_PROMPT_VERSION = "v1-failed"
SCORING_TIMEOUT_SECONDS = 30

_SCORING_INSTRUCTIONS = """Score a relationship observation's significance on a 1-5 scale.

Return JSON only with this shape:
{"score":1,"reason":"short explanation"}

Anchor examples:
1 - Trivial. Marginal pattern, weak evidence, low relevance even if true.
Example: "He uses slightly more emojis on weekends."
2 - Minor. Real but small. Worth recording, not worth surfacing proactively.
Example: "She tends to send longer messages in the morning."
3 - Notable. Solid pattern with real relevance to how they relate. Worth surfacing when relevant context arises.
Example: "He brings up work frustration before getting sharp with her."
4 - Significant. Strong pattern materially affecting how the relationship functions. Should actively inform engagement.
Example: "Their conflicts cool faster when she initiates the repair; much slower when he does."
5 - Core. Defining pattern of the relationship dynamic. Always-on context.
Example: "Long walks have been their primary reconnection mechanism for years; it is how they consistently repair."
"""


class RescoreReport(NamedTuple):
    scanned: int
    rescored: int
    still_failed: int


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _usage_tokens(usage: Any, field: str) -> int:
    return int(_attr(usage, field, 0) or 0)


async def _record_response_cost(pool: Any, usage: Any) -> None:
    settings = get_settings()
    input_tokens = _usage_tokens(usage, "input_tokens")
    cache_create = _usage_tokens(usage, "cache_creation_input_tokens")
    cache_read = _usage_tokens(usage, "cache_read_input_tokens")
    output_tokens = _usage_tokens(usage, "output_tokens")
    regular_input_tokens = max(0, input_tokens - cache_create - cache_read)
    dollars = (
        regular_input_tokens * settings.anthropic_haiku_input_usd_per_mtok
        + cache_create * settings.anthropic_haiku_input_usd_per_mtok * 1.25
        + cache_read * settings.anthropic_haiku_input_usd_per_mtok * 0.10
        + output_tokens * settings.anthropic_haiku_output_usd_per_mtok
    ) / 1_000_000
    if dollars > 0:
        await record_llm_cost(pool, "text", dollars)


def _response_text(response: Any) -> str:
    parts: list[str] = []
    for block in _attr(response, "content", []) or []:
        if _attr(block, "type") == "text":
            parts.append(str(_attr(block, "text", "")))
    return "\n".join(part for part in parts if part).strip()


def _failure(reason: str) -> tuple[None, str, str]:
    return None, f"scoring failed: {reason}", FAILED_SCORING_PROMPT_VERSION


def _parse_score(text: str) -> tuple[int, str]:
    data = json.loads(text)
    score = int(data["score"])
    if score < 1 or score > 5:
        raise ValueError(f"score out of range: {score}")
    reason = str(data.get("reason") or "").strip()
    if not reason:
        raise ValueError("missing reason")
    return score, reason


async def score_observation(
    pool: Any,
    *,
    content: str,
    client: Any | None = None,
) -> tuple[int | None, str, str]:
    """Return `(score, reason, prompt_version)` or `(None, reason, 'v1-failed')`."""
    settings = get_settings()
    if client is None:
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key.get_secret_value())

    try:
        if not await is_under_cap(pool, "text"):
            raise RuntimeError("text LLM spend cap exceeded")
        async with asyncio.timeout(SCORING_TIMEOUT_SECONDS):
            response = await client.messages.create(
                model=settings.scoring_model,
                max_tokens=300,
                system=[{"type": "text", "text": _SCORING_INSTRUCTIONS, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": json.dumps({"observation": content})}],
            )
        await _record_response_cost(pool, _attr(response, "usage", {}))
        score, reason = _parse_score(_response_text(response))
    except Exception as exc:
        logger.warning("observation scoring failed: %s", exc)
        return _failure(str(exc))
    return score, reason, SCORING_PROMPT_VERSION


async def rescore_observations(
    pool: Any,
    *,
    prompt_version_threshold: str = SCORING_PROMPT_VERSION,
    client: Any | None = None,
) -> RescoreReport:
    """Manually re-score observations with missing, failed, flagged, or older prompt versions."""
    rows = await pool.fetch(
        """
        SELECT id, content
        FROM observations
        WHERE scoring_prompt_version IS NULL
           OR scoring_prompt_version < $1
           OR scoring_prompt_version LIKE '%failed'
           OR needs_rescoring = true
        ORDER BY created_at
        """,
        prompt_version_threshold,
    )
    rescored = 0
    still_failed = 0
    for row in rows:
        score, _reason, prompt_version = await score_observation(pool, content=row["content"], client=client)
        await pool.execute(
            """
            UPDATE observations
            SET significance = $1,
                scoring_prompt_version = $2,
                last_reinforced_at = COALESCE(last_reinforced_at, now()),
                needs_rescoring = CASE WHEN $1::integer IS NULL THEN needs_rescoring ELSE false END
            WHERE id = $3
            """,
            score,
            prompt_version,
            row["id"],
        )
        if score is None:
            still_failed += 1
        else:
            rescored += 1
    return RescoreReport(scanned=len(rows), rescored=rescored, still_failed=still_failed)
