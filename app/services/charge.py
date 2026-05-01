"""Inbound message charge classification."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Literal, NamedTuple

import anthropic

from app.config import get_settings
from app.services.spend import is_under_cap, record_llm_cost

logger = logging.getLogger(__name__)

ChargeLabel = Literal["routine", "notable", "charged", "crisis"]
CHARGE_PROMPT_VERSION = "v1"
FAILED_CHARGE_PROMPT_VERSION = "v1-failed"
CHARGE_TIMEOUT_SECONDS = 20
CHARGE_LABELS = {"routine", "notable", "charged", "crisis"}

_CHARGE_INSTRUCTIONS = """Classify one inbound relationship-assistant message by emotional charge.

Return JSON only with this shape:
{"charge":"routine","reason":"short explanation"}

Labels:
- routine: logistics, casual updates, low emotional load.
- notable: worth attention or context, but not strongly emotional or conflict-heavy.
- charged: significant emotional weight, conflict, vulnerability, or intensity.
- crisis: self-harm ideation, imminent danger, abuse, or severe acute distress.

Do not diagnose. Choose the lowest label that fits the message.
"""


class ChargeClassification(NamedTuple):
    charge: ChargeLabel
    reason: str
    prompt_version: str


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


def _parse_charge(text: str) -> tuple[ChargeLabel, str]:
    data = json.loads(text)
    charge = str(data["charge"]).strip().lower()
    if charge not in CHARGE_LABELS:
        raise ValueError(f"unknown charge label: {charge}")
    reason = str(data.get("reason") or "").strip()
    if not reason:
        raise ValueError("missing reason")
    return charge, reason  # type: ignore[return-value]


def _placeholder_key(value: str) -> bool:
    lowered = value.lower()
    return "dummy" in lowered or "replace-with-" in lowered


def _heuristic_charge(content: str) -> tuple[ChargeLabel, str] | None:
    lowered = content.lower()
    crisis_terms = (
        "kill myself",
        "suicide",
        "self harm",
        "hurt myself",
        "hurt her",
        "hurt him",
        "hurt them",
        "violent",
        "violence",
        "abuse",
        "abusive",
    )
    charged_terms = (
        "hates me",
        "snaps",
        "rage",
        "cheating",
        "cheat",
        "affair",
        "sex with another",
        "lost trust",
        "lose trust",
        "abandonment",
        "insecure in her relationship",
        "insecure in his relationship",
        "finds fault",
        "scared her",
        "scared him",
        "scared them",
        "miscarriage",
        "volatile",
        "eruptive",
        "cruel",
        "mean-spirited",
        "resentment",
        "derail",
        "disengage",
        "poisons",
        "not respected",
        "not understood",
    )
    if any(term in lowered for term in crisis_terms):
        return "crisis", "keyword fallback matched crisis/safety language"
    if any(term in lowered for term in charged_terms):
        return "charged", "keyword fallback matched relationship conflict/trust rupture language"
    return None


def _fallback(reason: str, content: str = "") -> ChargeClassification:
    heuristic = _heuristic_charge(content)
    if heuristic is not None:
        charge, heuristic_reason = heuristic
        return ChargeClassification(charge, f"{heuristic_reason}; classifier failed: {reason}", FAILED_CHARGE_PROMPT_VERSION)
    return ChargeClassification("routine", f"charge classification failed: {reason}", FAILED_CHARGE_PROMPT_VERSION)


async def classify_charge(
    pool: Any,
    content: str,
    *,
    client: Any | None = None,
) -> ChargeClassification:
    settings = get_settings()
    api_key = settings.anthropic_api_key.get_secret_value()
    if client is None and _placeholder_key(api_key):
        logger.warning("charge classification skipped because Anthropic API key is a placeholder")
        return _fallback("placeholder Anthropic API key", content)
    if not content.strip():
        return ChargeClassification("routine", "empty message", CHARGE_PROMPT_VERSION)
    if client is None:
        client = anthropic.AsyncAnthropic(api_key=api_key)
    try:
        if not await is_under_cap(pool, "text"):
            raise RuntimeError("text LLM spend cap exceeded")
        async with asyncio.timeout(CHARGE_TIMEOUT_SECONDS):
            response = await client.messages.create(
                model=settings.scoring_model,
                max_tokens=200,
                system=[{"type": "text", "text": _CHARGE_INSTRUCTIONS, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": json.dumps({"message": content})}],
            )
        await _record_response_cost(pool, _attr(response, "usage", {}))
        label, reason = _parse_charge(_response_text(response))
    except Exception as exc:
        logger.warning("charge classification failed: %s", exc)
        return _fallback(str(exc), content)
    return ChargeClassification(label, reason, CHARGE_PROMPT_VERSION)
