"""Agentic turn lifecycle orchestration."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from datetime import timedelta
from typing import Any
from uuid import UUID

import anthropic

from app.config import get_settings
from app.models.user import User, claim_onboarding_welcome
from app.services import hooks, system_state
from app.services.hot_context import build_hot_context, render_hot_context
from app.services.messaging import send_outbound
from app.services.prompts import render_system_prompt
from app.services.spend import is_under_cap, record_llm_cost
from app.services.text_safety import clean_user_facing_text
from app.services.tools.registry import READ_PHASE_TOOLS, WRITE_PHASE_TOOLS, call_tool, to_anthropic_tools
from app.services.turn_context import TurnContext, partner_of

logger = logging.getLogger(__name__)

_pool: Any | None = None


class AgenticTurnError(Exception):
    failure_reason = "crashed"


class SpendCapExceeded(Exception):
    failure_reason = "spend_cap"


class LLMPhaseError(Exception):
    failure_reason = "llm_timeout"


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _block_to_dict(block: Any) -> dict[str, Any]:
    if isinstance(block, dict):
        return dict(block)
    block_type = _attr(block, "type")
    data: dict[str, Any] = {"type": block_type}
    if block_type == "text":
        data["text"] = _attr(block, "text", "")
    elif block_type == "tool_use":
        data["id"] = _attr(block, "id")
        data["name"] = _attr(block, "name")
        data["input"] = _attr(block, "input", {}) or {}
    return data


def _system_blocks(system_prompt: str, hot_context_rendered: str) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [
        {"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": hot_context_rendered},
    ]
    if len(hot_context_rendered) // 4 >= 1024:
        blocks[1]["cache_control"] = {"type": "ephemeral"}
    return blocks


def _anthropic_tools(allowed_tools: set[str]) -> list[dict[str, Any]]:
    tools = [dict(tool) for tool in to_anthropic_tools(allowed_tools)]
    if tools:
        tools[-1] = {**tools[-1], "cache_control": {"type": "ephemeral"}}
    return tools


def _usage_tokens(usage: Any, field: str) -> int:
    value = _attr(usage, field, 0) or 0
    return int(value)


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
    await record_llm_cost(pool, "text", dollars)


async def _create_message_with_retry(
    client: Any,
    *,
    ctx: TurnContext,
    system: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    messages: list[dict[str, Any]],
) -> Any:
    settings = get_settings()
    last_error: Exception | None = None
    for attempt in range(2):
        if not await is_under_cap(ctx.pool, "text"):
            raise SpendCapExceeded("text LLM spend cap exceeded")
        try:
            response = await client.messages.create(
                model=settings.conversational_model,
                max_tokens=1200,
                system=system,
                messages=messages,
                tools=tools,
            )
        except Exception as exc:  # Anthropic SDK transient subclasses vary by version.
            last_error = exc
            if attempt == 0:
                logger.warning("anthropic message create failed; retrying once: %s", exc)
                continue
            raise LLMPhaseError(str(exc)) from exc
        await _record_response_cost(ctx.pool, _attr(response, "usage", {}))
        return response
    raise LLMPhaseError(str(last_error or "anthropic message create failed"))


async def run_phase(
    client: Any,
    ctx: TurnContext,
    system_prompt: str,
    hot_context_rendered: str,
    allowed_tools: set[str],
    seed_messages: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]], int]:
    settings = get_settings()
    if client is None:
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key.get_secret_value())

    system = _system_blocks(system_prompt, hot_context_rendered)
    tools = _anthropic_tools(allowed_tools)
    messages = list(seed_messages)
    tool_call_count = 0

    while True:
        response = await _create_message_with_retry(client, ctx=ctx, system=system, tools=tools, messages=messages)
        content_blocks = [_block_to_dict(block) for block in (_attr(response, "content", []) or [])]
        messages.append({"role": "assistant", "content": content_blocks})
        tool_uses = [block for block in content_blocks if block.get("type") == "tool_use"]
        if not tool_uses or _attr(response, "stop_reason") != "tool_use":
            final_text = "\n".join(
                str(block.get("text", "")).strip()
                for block in content_blocks
                if block.get("type") == "text" and str(block.get("text", "")).strip()
            )
            return final_text, messages, tool_call_count

        tool_results: list[dict[str, Any]] = []
        for tool_use in tool_uses:
            tool_call_count += 1
            result = await call_tool(tool_use["name"], tool_use.get("input") or {}, ctx)
            is_error = bool(result.get("is_error") or result.get("error"))
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use["id"],
                    "content": json.dumps(result, default=str),
                    "is_error": is_error,
                }
            )
        messages.append({"role": "user", "content": tool_results})


def set_pool(pool: Any) -> None:
    global _pool
    _pool = pool


def _trigger_charge(hot_context: Any) -> str | None:
    messages = hot_context.trigger_metadata.get("messages", [])
    for message in messages:
        charge = message.get("charge")
        if charge in {"crisis", "charged"}:
            return charge
    return messages[0].get("charge") if messages else None


def _explicit_partner_alert_requested(hot_context: Any) -> bool:
    if bool(hot_context.trigger_metadata.get("explicit_partner_alert_requested")):
        return True
    messages = hot_context.trigger_metadata.get("messages", [])
    for message in messages:
        content = str(message.get("content") or "").lower()
        if not content:
            continue
        asks_to_alert = any(phrase in content for phrase in ("tell", "alert", "let", "message", "ask"))
        names_partner = any(phrase in content for phrase in ("partner", "him", "her", "them"))
        if asks_to_alert and names_partner:
            return True
    return False


def _collect_reasoning(messages: list[dict[str, Any]], final_text: str = "") -> str:
    fragments: list[str] = []
    for message in messages:
        if message.get("role") != "assistant":
            continue
        content = message.get("content", "")
        blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                text = str(block.get("text", "")).strip()
                if text and text != final_text:
                    fragments.append(text)
    return "\n".join(fragments)


async def _append_reasoning(pool: Any, turn_id: UUID, note: str) -> None:
    if not note:
        return
    await pool.execute(
        "UPDATE bot_turns SET reasoning = COALESCE(reasoning, '') || $1 WHERE id = $2",
        f"\n{note}",
        turn_id,
    )


async def _check_outbound_oob(
    pool: Any,
    content: str,
    recipient_id: UUID,
    protected_owner_ids: list[UUID] | None = None,
) -> dict[str, Any]:
    hook = hooks.check_oob
    if hook is None:
        return {"verdict": "ok", "reason": "OOB hook disabled", "suggested_rewrite": None, "checker_failed": False}
    try:
        verdict = await hook(pool, content, recipient_id, protected_owner_ids=protected_owner_ids)
    except TypeError:
        try:
            verdict = await hook(pool, content, recipient_id)
        except TypeError:
            verdict = await hook(content, recipient_id)
    if hasattr(verdict, "model_dump"):
        verdict = verdict.model_dump(mode="json")
    verdict.setdefault("suggested_rewrite", verdict.get("rewrite"))
    verdict.setdefault("reason", "")
    verdict.setdefault("checker_failed", False)
    return verdict


async def _resolve_outbound_text(
    pool: Any,
    turn_id: UUID,
    user: User,
    content: str,
    protected_owner_ids: list[UUID] | None = None,
) -> str | None:
    verdict = await _check_outbound_oob(pool, content, user.id, protected_owner_ids)
    if verdict["verdict"] == "ok":
        if verdict.get("checker_failed"):
            await _append_reasoning(pool, turn_id, f"OOB checker failed open before send: {verdict['reason']}")
        return content
    if verdict["verdict"] == "block":
        await _append_reasoning(pool, turn_id, f"Outbound blocked before send by OOB checker: {verdict['reason']}")
        return None
    suggested = (verdict.get("suggested_rewrite") or "").strip()
    if not suggested:
        await _append_reasoning(pool, turn_id, f"Outbound rewrite requested but no rewrite was supplied: {verdict['reason']}")
        return None
    second = await _check_outbound_oob(pool, suggested, user.id, protected_owner_ids)
    if second["verdict"] != "ok":
        await _append_reasoning(
            pool,
            turn_id,
            f"Outbound rewrite was not sendable: first={verdict['reason']} second={second['reason']}",
        )
        return None
    await _append_reasoning(pool, turn_id, f"Outbound rewritten by OOB checker before send: {verdict['reason']}")
    return suggested


async def _open_turn(
    pool: Any,
    triggering_message_ids: list[UUID],
    user: User,
    prompt_snapshot: str,
    model_version: str,
    system_prompt_version: str,
) -> tuple[UUID, datetime]:
    row = await pool.fetchrow(
        """
        INSERT INTO bot_turns (
            triggered_by_message_id, triggering_message_ids, user_in_context,
            system_prompt_version, model_version, prompt_snapshot, started_at
        )
        VALUES ($1, $2, $3, $4, $5, $6, now())
        RETURNING id, started_at
        """,
        triggering_message_ids[0] if triggering_message_ids else None,
        triggering_message_ids,
        user.id,
        system_prompt_version,
        model_version,
        prompt_snapshot,
    )
    try:
        started_at = row["started_at"]
    except KeyError:
        started_at = datetime.now(UTC)
    return row["id"], started_at


async def _complete_turn(
    pool: Any,
    turn_id: UUID,
    started_at: datetime,
    final_output_message_id: UUID | None,
    tool_call_count: int,
    reasoning: str,
) -> None:
    duration_ms = max(0, int((datetime.now(UTC) - started_at).total_seconds() * 1000))
    await pool.execute(
        """
        UPDATE bot_turns
        SET final_output_message_id=$1,
            reasoning=COALESCE(reasoning, '') || $2,
            completed_at=now(),
            duration_ms=$3,
            tool_call_count=$4
        WHERE id=$5
        """,
        final_output_message_id,
        f"\n{reasoning}" if reasoning else "",
        duration_ms,
        tool_call_count,
        turn_id,
    )


async def _record_turn_final_output(pool: Any, turn_id: UUID, final_output_message_id: UUID) -> None:
    await pool.execute(
        """
        UPDATE bot_turns
        SET final_output_message_id=$1
        WHERE id=$2
        """,
        final_output_message_id,
        turn_id,
    )


async def _fail_turn(pool: Any, turn_id: UUID | None, failure_reason: str) -> None:
    if turn_id is None:
        return
    await pool.execute("UPDATE bot_turns SET failure_reason=$1 WHERE id=$2", failure_reason, turn_id)


async def _defer_for_text_cap(pool: Any, user: User, message_ids: list[UUID]) -> bool:
    if message_ids:
        await pool.execute(
            "UPDATE messages SET processing_state='deferred' WHERE id = ANY($1)",
            message_ids,
        )
    row = await pool.fetchrow(
        """
        INSERT INTO scheduled_jobs (user_id, job_type, scheduled_for, context, status)
        SELECT $1, 'deferred_turn', $2, $3::jsonb, 'pending'
        WHERE NOT EXISTS (
            SELECT 1 FROM scheduled_jobs
            WHERE user_id = $1 AND job_type = 'deferred_turn' AND status = 'pending'
        )
        RETURNING id, scheduled_for
        """,
        user.id,
        datetime.now(UTC) + timedelta(days=1),
        json.dumps({"triggering_message_ids": [str(message_id) for message_id in message_ids], "reason": "text_spend_cap"}),
    )
    return row is not None


async def _run_agentic(
    triggering_message_ids: list[UUID],
    user: User,
    *,
    trigger_metadata: dict[str, Any] | None = None,
    pool: Any | None = None,
    prompt_version: str | None = None,
) -> None:
    active_pool = pool or _pool
    if active_pool is not None and await system_state.is_paused(active_pool):
        return
    if active_pool is None:
        raise RuntimeError("agentic pool has not been set")

    settings = get_settings()
    selected_prompt_version = prompt_version or settings.system_prompt_version
    turn_id: UUID | None = None
    started_at = datetime.now(UTC)
    phase_a_sent = False
    try:
        partner = await partner_of(active_pool, user)
        hot_context = await build_hot_context(active_pool, user, partner, triggering_message_ids, trigger_metadata)
        rendered_hot_context = render_hot_context(hot_context)
        system_prompt = render_system_prompt(
            settings.assistant_name,
            user.name,
            partner.name,
            prompt_version=selected_prompt_version,
        )
        prompt_snapshot = f"{system_prompt}\n\n{rendered_hot_context}"
        turn_id, started_at = await _open_turn(
            active_pool,
            triggering_message_ids,
            user,
            prompt_snapshot,
            settings.conversational_model,
            selected_prompt_version,
        )
        charge = _trigger_charge(hot_context)
        explicit_partner_alert_requested = _explicit_partner_alert_requested(hot_context)
        ctx = TurnContext(
            turn_id,
            active_pool,
            user,
            partner,
            triggering_message_ids,
            phase="read",
            trigger_charge=charge,
            explicit_partner_alert_requested=explicit_partner_alert_requested,
        )
        phase_a_seed = [
            {
                "role": "user",
                "content": (
                    f"Trigger: kind={hot_context.trigger_metadata.get('kind', 'inbound')} "
                    f"ids={triggering_message_ids} charge={charge or 'routine'} "
                    f"context={json.dumps(hot_context.trigger_metadata.get('context', {}), default=str)}. "
                    "Phase A: read what you need, then produce only the user-facing reply as plain text. "
                    "Do not include scratch notes, analysis of the message, tool/read decisions, or separators."
                ),
            }
        ]
        assistant_text, phase_a_messages, phase_a_tool_count = await run_phase(
            None, ctx, system_prompt, rendered_hot_context, READ_PHASE_TOOLS, phase_a_seed
        )
        if triggering_message_ids:
            await active_pool.execute(
                "UPDATE messages SET processing_state='processed' WHERE id = ANY($1) AND processing_state='raw'",
                triggering_message_ids,
            )

        final_output_message_id = None
        if assistant_text:
            assistant_text = clean_user_facing_text(assistant_text)
            dyad_owner_ids = [user.id, partner.id]
            sendable_text = await _resolve_outbound_text(active_pool, turn_id, user, assistant_text, dyad_owner_ids)
            if sendable_text:
                final_output_message_id = await send_outbound(
                    active_pool,
                    user,
                    sendable_text,
                    bot_turn_id=turn_id,
                    protected_owner_ids=dyad_owner_ids,
                )
                await _record_turn_final_output(active_pool, turn_id, final_output_message_id)
                await claim_onboarding_welcome(active_pool, user.id)
                assistant_text = sendable_text
                phase_a_sent = True
        elif charge in {"charged", "crisis"}:
            await _append_reasoning(active_pool, turn_id, "silence; charged trigger but no justification produced")
            logger.warning("charged/crisis trigger produced silence without model justification turn_id=%s", turn_id)

        ctx.phase = "write"
        phase_b_seed = list(phase_a_messages)
        phase_b_seed.append(
            {
                "role": "user",
                "content": f"You sent: {assistant_text or '[silence]'}. Now record any state changes (memories, observations, theme updates, watch items) and optionally schedule one follow-up check-in. Do not produce user-facing text.",
            }
        )
        _, phase_b_messages, phase_b_tool_count = await run_phase(
            None, ctx, system_prompt, rendered_hot_context, WRITE_PHASE_TOOLS, phase_b_seed
        )
        reasoning = "\n".join(
            part
            for part in (
                _collect_reasoning(phase_a_messages, assistant_text),
                _collect_reasoning(phase_b_messages),
            )
            if part
        )
        await _complete_turn(
            active_pool,
            turn_id,
            started_at,
            final_output_message_id,
            phase_a_tool_count + phase_b_tool_count,
            reasoning,
        )
    except SpendCapExceeded:
        if turn_id is not None:
            scheduled = await _defer_for_text_cap(active_pool, user, triggering_message_ids)
            final_output_message_id = None
            if scheduled:
                final_output_message_id = await send_outbound(
                    active_pool,
                    user,
                    "I'm running into limits today, will catch up tomorrow.",
                    bot_turn_id=turn_id,
                )
            await _complete_turn(
                active_pool,
                turn_id,
                started_at,
                final_output_message_id,
                0,
                "Text LLM spend cap hit; deferred original trigger messages for next-day retry.",
            )
            return
        raise
    except Exception as exc:
        failure_reason = getattr(exc, "failure_reason", "crashed")
        await _fail_turn(active_pool, turn_id, failure_reason)
        if phase_a_sent:
            logger.warning("agentic phase B failed after outbound was sent: %s", exc)
            return
        raise


async def run_agentic_turn(triggering_message_ids: list[UUID], user: User) -> None:
    if not triggering_message_ids:
        logger.warning("run_agentic_turn called without triggering messages for user_id=%s", user.id)
        return
    await _run_agentic(triggering_message_ids, user)


async def run_agentic_job(user: User, trigger_metadata: dict[str, Any]) -> None:
    await _run_agentic([], user, trigger_metadata=trigger_metadata)


async def run_agentic_turn_with_pool(
    pool: Any,
    triggering_message_ids: list[UUID],
    user: User,
    *,
    prompt_version: str,
) -> None:
    if not triggering_message_ids:
        logger.warning("run_agentic_turn_with_pool called without triggering messages for user_id=%s", user.id)
        return
    await _run_agentic(triggering_message_ids, user, pool=pool, prompt_version=prompt_version)


async def run_agentic_job_with_pool(
    pool: Any,
    user: User,
    trigger_metadata: dict[str, Any],
    *,
    prompt_version: str,
) -> None:
    await _run_agentic([], user, trigger_metadata=trigger_metadata, pool=pool, prompt_version=prompt_version)
