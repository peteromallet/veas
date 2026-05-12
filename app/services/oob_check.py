"""Standalone OOB checker service.

Agentic-tool checks are advisory: a `rewrite` verdict gives the loop a safer
candidate, but the loop must decide whether to redraft, stay silent, or send a
new message through the normal send path. Delivery-time checks are safety
gates: `rewrite` and `block` verdicts must be withheld for review/retry rather
than treated as authorization to send altered content.
"""

from __future__ import annotations

import asyncio
from collections import Counter
import json
import logging
from typing import Any
from uuid import UUID

import anthropic

from app.config import get_settings
from app.services.scoring import _record_response_cost as _record_haiku_response_cost
from app.services.spend import is_under_cap, record_llm_cost
from tool_schemas import CheckOOBOutput, OOBTopicCluster, OOBVerdict, SummarizeOOBTopicsOutput

logger = logging.getLogger(__name__)

CHECKER_TIMEOUT_SECONDS = 30
COMMON_COUNTERSUMMARY_TOPICS = {
    "family history",
    "past relationships",
    "health",
    "finances",
    "work",
    "children",
    "parenting",
    "religion",
    "politics",
    "trauma",
}

_STATIC_INSTRUCTIONS = """You are an out-of-bounds safety checker for a relationship mediator.

Check the proposed outbound message against the protected users' active OOB entries.
OOB entries contain protected details. Use them only to decide whether the draft
would reveal, imply, or pressure around protected content.

Return JSON only with this shape:
{"verdict":"ok|rewrite|block","reason":"short explanation","triggering_oob_ids":["uuid"],"suggested_rewrite":"string or null","checker_failed":false}

Verdicts:
- ok: the draft does not violate active OOB entries.
- rewrite: the draft can be made safe by removing or generalizing protected content.
- block: the draft cannot safely be sent as a rewritten message.
"""

_COUNTERSUMMARY_INSTRUCTIONS = """Cluster OOB entries into broad, non-identifying topic categories.

Return JSON only with this shape:
{"topics":["family history","past relationships"]}

Rules:
- Use broad categories only.
- Never quote, paraphrase, or preserve identifying details.
- Prefer general labels such as family history, past relationships, health, finances, work, parenting, religion, politics, trauma, or personal matter.
"""


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _usage_tokens(usage: Any, field: str) -> int:
    return int(_attr(usage, field, 0) or 0)


async def _record_response_cost(pool: Any, usage: Any) -> None:
    settings = get_settings()
    input_price = settings.anthropic_input_usd_per_mtok
    output_price = settings.anthropic_output_usd_per_mtok
    input_tokens = _usage_tokens(usage, "input_tokens")
    cache_create = _usage_tokens(usage, "cache_creation_input_tokens")
    cache_read = _usage_tokens(usage, "cache_read_input_tokens")
    output_tokens = _usage_tokens(usage, "output_tokens")
    regular_input_tokens = max(0, input_tokens - cache_create - cache_read)
    dollars = (
        regular_input_tokens * input_price
        + cache_create * input_price * 1.25
        + cache_read * input_price * 0.10
        + output_tokens * output_price
    ) / 1_000_000
    if dollars > 0:
        await record_llm_cost(pool, "text", dollars)


def _response_text(response: Any) -> str:
    parts: list[str] = []
    for block in _attr(response, "content", []) or []:
        if _attr(block, "type") == "text":
            parts.append(str(_attr(block, "text", "")))
    return "\n".join(part for part in parts if part).strip()


def _protected_owner_ids(recipient_id: UUID, protected_owner_ids: list[UUID] | None) -> list[UUID]:
    if protected_owner_ids is None:
        return [recipient_id]
    return list(dict.fromkeys(protected_owner_ids))


async def _active_oob_entries(pool: Any, owner_ids: list[UUID]) -> list[dict[str, Any]]:
    if not owner_ids:
        return []
    rows = await pool.fetch(
        """
        SELECT id, sensitive_core, shareable_context, severity
        FROM out_of_bounds
        WHERE owner_id = ANY($1::uuid[])
          AND status = 'active'
        ORDER BY created_at DESC
        """,
        owner_ids,
    )
    return [
        {
            "id": row["id"],
            "sensitive_core": row["sensitive_core"],
            "shareable_context": row["shareable_context"],
            "severity": row["severity"],
        }
        for row in rows
    ]


def _failure_result(entries: list[dict[str, Any]], error: Exception) -> CheckOOBOutput:
    hard_or_firm = [entry for entry in entries if entry["severity"] in {"firm", "hard"}]
    reason = f"OOB checker failed: {error}"
    if hard_or_firm:
        return CheckOOBOutput(
            verdict=OOBVerdict.block,
            reason=reason,
            triggering_oob_ids=[entry["id"] for entry in hard_or_firm],
            suggested_rewrite=None,
            checker_failed=True,
        )
    # obs N/A: no scope in checker
    logger.warning("OOB checker failed open for soft-only OOB entries: %s", error)
    return CheckOOBOutput(
        verdict=OOBVerdict.ok,
        reason=reason,
        triggering_oob_ids=[entry["id"] for entry in entries],
        suggested_rewrite=None,
        checker_failed=True,
    )


def _parse_checker_output(text: str) -> CheckOOBOutput:
    data = json.loads(text)
    if "triggering_oob_ids" not in data:
        data["triggering_oob_ids"] = []
    if "checker_failed" not in data:
        data["checker_failed"] = False
    return CheckOOBOutput.model_validate(data)


async def check_oob_with_policy(
    pool: Any,
    *,
    content: str,
    recipient_id: UUID,
    protected_owner_ids: list[UUID] | None = None,
    sender_intent: str | None = None,
    client: Any | None = None,
) -> CheckOOBOutput:
    """Check outbound text against active OOB for protected owners.

    By default, only the recipient's OOB is protected to preserve existing
    caller behavior. Final outbound paths can pass both dyad owner ids.
    """
    owner_ids = _protected_owner_ids(recipient_id, protected_owner_ids)
    entries = await _active_oob_entries(pool, owner_ids)
    if not entries:
        return CheckOOBOutput(
            verdict=OOBVerdict.ok,
            reason="no active OOB entries for protected owners",
            triggering_oob_ids=[],
            suggested_rewrite=None,
            checker_failed=False,
        )

    settings = get_settings()
    if client is None:
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key.get_secret_value())

    payload = {
        "recipient_id": str(recipient_id),
        "protected_owner_ids": [str(owner_id) for owner_id in owner_ids],
        "sender_intent": sender_intent or "",
        "draft_outbound": content,
        "active_oob_entries": [
            {
                "id": str(entry["id"]),
                "severity": entry["severity"],
                "sensitive_core": entry["sensitive_core"],
                "shareable_context": entry["shareable_context"],
            }
            for entry in entries
        ],
    }
    try:
        if not await is_under_cap(pool, "text"):
            raise RuntimeError("text LLM spend cap exceeded")
        async with asyncio.timeout(CHECKER_TIMEOUT_SECONDS):
            response = await client.messages.create(
                model=settings.oob_checker_model,
                max_tokens=600,
                system=[{"type": "text", "text": _STATIC_INSTRUCTIONS, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": json.dumps(payload, default=str)}],
            )
        await _record_response_cost(pool, _attr(response, "usage", {}))
        result = _parse_checker_output(_response_text(response))
    except Exception as exc:
        return _failure_result(entries, exc)

    if result.checker_failed:
        return _failure_result(entries, RuntimeError(result.reason))
    return result


def _count_word(count: int) -> str:
    words = {
        0: "zero",
        1: "one",
        2: "two",
        3: "three",
        4: "four",
        5: "five",
        6: "six",
        7: "seven",
        8: "eight",
        9: "nine",
        10: "ten",
    }
    return words.get(count, str(count))


def _broad_topic(entry: dict[str, Any]) -> str:
    text = f"{entry.get('shareable_context') or ''} {entry.get('sensitive_core') or ''}".lower()
    keyword_topics = [
        ("family history", ("family", "mother", "father", "parent", "sibling", "childhood")),
        ("past relationships", ("ex", "past relationship", "former partner", "dating")),
        ("health", ("health", "medical", "illness", "therapy", "diagnosis")),
        ("finances", ("money", "debt", "salary", "finance", "financial")),
        ("work", ("work", "job", "boss", "career")),
        ("children", ("child", "kid", "children")),
        ("parenting", ("parenting", "co-parent")),
        ("religion", ("religion", "faith", "church", "spiritual")),
        ("politics", ("politics", "political", "election")),
        ("trauma", ("trauma", "abuse", "assault")),
    ]
    for topic, keywords in keyword_topics:
        if any(keyword in text for keyword in keywords):
            return topic
    return "personal matter"


def _safe_topic(topic: str, count: int) -> str:
    if count == 1 and topic not in COMMON_COUNTERSUMMARY_TOPICS:
        return "a personal matter"
    if count == 1 and topic == "personal matter":
        return "a personal matter"
    return topic


async def _cluster_topics_with_haiku(pool: Any, entries: list[dict[str, Any]], client: Any | None) -> list[str] | None:
    payload = {
        "entries": [
            {
                "severity": entry["severity"],
                "sensitive_core": entry["sensitive_core"],
                "shareable_context": entry["shareable_context"],
            }
            for entry in entries
        ]
    }
    try:
        if client is None:
            settings = get_settings()
            client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key.get_secret_value())
        else:
            settings = get_settings()
        if not await is_under_cap(pool, "text"):
            raise RuntimeError("text LLM spend cap exceeded")
        async with asyncio.timeout(CHECKER_TIMEOUT_SECONDS):
            response = await client.messages.create(
                model=settings.scoring_model,
                max_tokens=300,
                system=[{"type": "text", "text": _COUNTERSUMMARY_INSTRUCTIONS, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": json.dumps(payload, default=str)}],
            )
        await _record_haiku_response_cost(pool, _attr(response, "usage", {}))
        data = json.loads(_response_text(response))
        topics = [str(topic).strip().lower() for topic in data.get("topics", []) if str(topic).strip()]
    except Exception as exc:
        # obs N/A: no scope in checker
        logger.warning("OOB countersummary clustering failed; falling back to deterministic categories: %s", exc)
        return None
    if len(topics) != len(entries):
        return None
    return topics


async def summarize_partner_oob(
    pool: Any,
    *,
    owner_id: UUID,
    client: Any | None = None,
) -> SummarizeOOBTopicsOutput:
    """Return non-identifying counts and broad topic clusters for active OOB entries."""
    entries = await _active_oob_entries(pool, [owner_id])
    if not entries:
        return SummarizeOOBTopicsOutput(total_count=0, clusters=[], narrative="no active out-of-bounds entries")

    deterministic_topics = [_broad_topic(entry) for entry in entries]
    topics = deterministic_topics
    if len(entries) > 1 and any(topic == "personal matter" for topic in deterministic_topics):
        topics = await _cluster_topics_with_haiku(pool, entries, client) or deterministic_topics

    raw_counts = Counter(topics)
    safe_counts: Counter[str] = Counter()
    for topic, count in raw_counts.items():
        safe_counts[_safe_topic(topic, count)] += count

    clusters = [
        OOBTopicCluster(count=count, topic=topic)
        for topic, count in sorted(safe_counts.items(), key=lambda item: (-item[1], item[0]))
    ]
    parts = [f"{_count_word(cluster.count)} {'entry' if cluster.count == 1 else 'entries'} related to {cluster.topic}" for cluster in clusters]
    if len(parts) == 1:
        narrative = parts[0]
    else:
        narrative = ", ".join(parts[:-1]) + f", and {parts[-1]}"
    return SummarizeOOBTopicsOutput(total_count=len(entries), clusters=clusters, narrative=narrative)
