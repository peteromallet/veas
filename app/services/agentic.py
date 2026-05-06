"""Agentic turn lifecycle orchestration."""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from datetime import timedelta
from typing import Any, Mapping
from uuid import UUID

import anthropic

from app.bots.registry import get_bot_spec
from app.config import get_settings
from app.models.user import User, claim_onboarding_welcome
from app.services import discord, hooks, system_state
from app.services.hot_context import build_hot_context, render_hot_context
from app.services.messaging import send_outbound, sent_contents_for_turn
from app.services.spend import is_under_cap, record_llm_cost
from app.services.crypto import encrypt_value
from app.services.text_safety import clean_user_facing_text
from app.services.tools.registry import STEP_ALLOWED_TOOLS, call_tool, to_anthropic_tools
from app.services.turn_plan import TurnPlan, make_turn_plan, orient_summary, pick_default_skeleton
from app.services.turn_context import BeforePacedSend, TurnContext, partner_of

logger = logging.getLogger(__name__)

_pool: Any | None = None


class AgenticTurnError(Exception):
    failure_reason = "crashed"


class SpendCapExceeded(Exception):
    failure_reason = "spend_cap"


class NewerInboundBeforeFinalSend(Exception):
    pass


class LLMPhaseError(Exception):
    failure_reason = "llm_timeout"


class BoundedLoopExceeded(Exception):
    failure_reason = "bounded_loop_exceeded"


REACTION_DIRECTIVE_RE = re.compile(r"^\s*\[react:\s*(?P<emoji>[^\]\s]+)\s*\]\s*$", re.IGNORECASE)
PACING_CONTEXT_KEYS = (
    "action",
    "reason",
    "wait_s",
    "wait_ms",
    "reaction",
    "source",
    "message_count",
    "typing_active",
    "latest_message_age_s",
    "contains_question",
    "contains_ack",
    "contains_closure",
    "has_media",
    "charge",
    "charges",
)
PACING_SIGNAL_KEYS = (
    "source",
    "message_count",
    "typing_active",
    "latest_message_age_s",
    "contains_question",
    "contains_ack",
    "contains_closure",
    "has_media",
    "charge",
    "charges",
)


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _compact_json_value(value: Any, *, text_limit: int = 180) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, str):
        return value if len(value) <= text_limit else value[: text_limit - 3] + "..."
    if isinstance(value, Mapping):
        compact: dict[str, Any] = {}
        for key, item in value.items():
            if item is None:
                continue
            compact[str(key)] = _compact_json_value(item, text_limit=text_limit)
        return compact
    if isinstance(value, (list, tuple, set)):
        return [_compact_json_value(item, text_limit=text_limit) for item in list(value)[:8]]
    return str(value)


def _compact_pacing_context(pacing_context: Any) -> dict[str, Any] | None:
    if pacing_context is None:
        return None

    compact: dict[str, Any] = {}
    for key in PACING_CONTEXT_KEYS:
        value = _attr(pacing_context, key)
        if value is not None:
            compact[key] = _compact_json_value(value)

    signal_snapshot = _attr(pacing_context, "signal_snapshot")
    if isinstance(signal_snapshot, Mapping):
        signal_compact = {
            key: _compact_json_value(signal_snapshot[key])
            for key in PACING_SIGNAL_KEYS
            if key in signal_snapshot and signal_snapshot[key] is not None
        }
        if signal_compact:
            compact["signals"] = signal_compact

    preference_snapshot = _attr(pacing_context, "preference_snapshot")
    if isinstance(preference_snapshot, Mapping):
        preference_keys = ("conversation_pace", "allow_reactions", "min_wait_s", "max_wait_s")
        preferences = {
            key: _compact_json_value(preference_snapshot[key])
            for key in preference_keys
            if key in preference_snapshot and preference_snapshot[key] is not None
        }
        if preferences:
            compact["preferences"] = preferences

    llm_judgement = _attr(pacing_context, "llm_judgement")
    if isinstance(llm_judgement, Mapping):
        judgement_keys = ("action", "reason", "wait_s", "reaction", "fallback")
        judgement = {
            key: _compact_json_value(llm_judgement[key])
            for key in judgement_keys
            if key in llm_judgement and llm_judgement[key] is not None
        }
        if judgement:
            compact["llm"] = judgement

    if not compact and isinstance(pacing_context, Mapping):
        compact = {
            str(key): _compact_json_value(value)
            for key, value in pacing_context.items()
            if key in PACING_CONTEXT_KEYS and value is not None
        }

    return compact or None


def _trigger_metadata_with_pacing(
    trigger_metadata: Mapping[str, Any] | None,
    pacing_context: Any,
) -> dict[str, Any] | None:
    compact_pacing = _compact_pacing_context(pacing_context)
    if compact_pacing is None:
        return dict(trigger_metadata) if trigger_metadata is not None else None

    metadata = dict(trigger_metadata or {})
    context = dict(metadata.get("context") or {})
    context["pacing"] = compact_pacing
    metadata["context"] = context
    metadata["pacing"] = compact_pacing
    metadata.setdefault("kind", "inbound")
    return metadata


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
    model: str | None = None,
    max_tokens: int = 1200,
) -> Any:
    settings = get_settings()
    last_error: Exception | None = None
    for attempt in range(2):
        if not await is_under_cap(ctx.pool, "text"):
            raise SpendCapExceeded("text LLM spend cap exceeded")
        try:
            response = await client.messages.create(
                model=model or settings.conversational_model,
                max_tokens=max_tokens,
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


async def run_step(
    client: Any,
    ctx: TurnContext,
    system_prompt: str,
    hot_context_rendered: str,
    allowed_tools: set[str],
    seed_messages: list[dict[str, Any]],
    model: str | None = None,
    max_tokens: int = 1200,
    max_tool_iterations: int | None = None,
) -> tuple[str, list[dict[str, Any]], int]:
    settings = get_settings()
    if client is None:
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key.get_secret_value())

    system = _system_blocks(system_prompt, hot_context_rendered)
    tools = _anthropic_tools(allowed_tools)
    messages = list(seed_messages)
    tool_call_count = 0
    tool_iteration_count = 0

    while True:
        response = await _create_message_with_retry(
            client,
            ctx=ctx,
            system=system,
            tools=tools,
            messages=messages,
            model=model,
            max_tokens=max_tokens,
        )
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

        tool_iteration_count += 1
        if max_tool_iterations is not None and tool_iteration_count > max_tool_iterations:
            raise BoundedLoopExceeded(f"tool iteration cap exceeded: {max_tool_iterations}")
        tool_results: list[dict[str, Any]] = []
        for tool_use in tool_uses:
            if tool_use["name"] != "update_turn_plan":
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
    existing = await pool.fetchval("SELECT COALESCE(reasoning, '') FROM bot_turns WHERE id=$1", turn_id)
    updated = f"{existing or ''}\n{note}"
    await pool.execute(
        "UPDATE bot_turns SET reasoning=$1, reasoning_encrypted=$2 WHERE id=$3",
        updated,
        encrypt_value(updated),
        turn_id,
    )


def _extract_reaction_directive(text: str) -> tuple[str | None, str]:
    emoji: str | None = None
    kept_lines: list[str] = []
    for raw_line in text.splitlines():
        match = REACTION_DIRECTIVE_RE.match(raw_line)
        if match and emoji is None:
            emoji = match.group("emoji").strip()
            continue
        kept_lines.append(raw_line)
    return emoji, "\n".join(kept_lines).strip()


async def _react_to_triggering_message(pool: Any, user: User, triggering_message_ids: list[UUID], emoji: str) -> bool:
    settings = get_settings()
    if settings.messaging_provider.strip().lower() != "discord" or not triggering_message_ids:
        return False
    row = await pool.fetchrow(
        """
        SELECT whatsapp_message_id
        FROM messages
        WHERE id=$1 AND direction='inbound' AND sender_id=$2
        """,
        triggering_message_ids[-1],
        user.id,
    )
    if row is None or not row.get("whatsapp_message_id"):
        return False
    await discord.add_reaction(user.phone, row["whatsapp_message_id"], emoji)
    return True


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
            system_prompt_version, model_version, prompt_snapshot, prompt_snapshot_encrypted, started_at
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, now())
        RETURNING id, started_at
        """,
        triggering_message_ids[0] if triggering_message_ids else None,
        triggering_message_ids,
        user.id,
        system_prompt_version,
        model_version,
        prompt_snapshot,
        encrypt_value(prompt_snapshot),
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
    existing = await pool.fetchval("SELECT COALESCE(reasoning, '') FROM bot_turns WHERE id=$1", turn_id)
    note = f"\n{reasoning}" if reasoning else ""
    updated_reasoning = f"{existing or ''}{note}"
    await pool.execute(
        """
        UPDATE bot_turns
        SET final_output_message_id=$1,
            reasoning=$2,
            reasoning_encrypted=$3,
            completed_at=now(),
            duration_ms=$4,
            tool_call_count=$5
        WHERE id=$6
        """,
        final_output_message_id,
        updated_reasoning,
        encrypt_value(updated_reasoning),
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
        {"triggering_message_ids": [str(message_id) for message_id in message_ids], "reason": "text_spend_cap"},
    )
    return row is not None


async def _newer_inbound_exists(
    pool: Any,
    user: User,
    triggering_message_ids: list[UUID],
    *,
    fallback_started_at: datetime | None = None,
) -> bool:
    boundary = fallback_started_at
    if triggering_message_ids:
        trigger_boundary = await pool.fetchval(
            "SELECT MAX(sent_at) FROM messages WHERE id = ANY($1::uuid[])",
            triggering_message_ids,
        )
        if trigger_boundary is not None:
            boundary = trigger_boundary
    if boundary is None:
        return False
    return bool(
        await pool.fetchval(
            """
            SELECT EXISTS (
                SELECT 1
                FROM messages
                WHERE direction='inbound'
                  AND sender_id=$1
                  AND sent_at > $2
                  AND NOT (id = ANY($3::uuid[]))
            )
            """,
            user.id,
            boundary,
            triggering_message_ids,
        )
    )


STEP_ITERATION_CAPS = {
    "read": 6,
    "consult": 1,
    "respond": 4,
    "record": 8,
    "schedule": 4,
    "done": 0,
}


def _allowed_tools_for_step(ctx: TurnContext) -> set[str]:
    allowed = set(STEP_ALLOWED_TOOLS.get(ctx.current_step, set())) | {"update_turn_plan"}
    if ctx.current_step == "respond" and not ctx.incremental_sending_enabled:
        allowed.discard("send_message_part")
    return allowed


def _sent_summary(delivered_parts: list[str], assistant_text: str, reaction_emoji: str | None) -> str:
    if delivered_parts:
        return (
            f"You actually sent {len(delivered_parts)} message"
            f"{'' if len(delivered_parts) == 1 else 's'}:\n"
            + "\n\n".join(f"{idx + 1}. {content}" for idx, content in enumerate(delivered_parts))
        )
    return f"You sent: {f'[reaction {reaction_emoji}]' if reaction_emoji else (assistant_text or '[silence]')}"


def _build_hot_context_signals(hot_context: Any) -> dict[str, Any]:
    return {
        "recent_message_count": len(getattr(hot_context, "recent_messages", []) or []),
        "open_watch_item_count": len(getattr(hot_context, "open_watch_items", []) or []),
        "active_oob_count": len(getattr(hot_context, "active_oob", []) or []),
    }


async def _run_agentic(
    triggering_message_ids: list[UUID],
    user: User,
    *,
    trigger_metadata: dict[str, Any] | None = None,
    pool: Any | None = None,
    prompt_version: str | None = None,
    before_paced_send: BeforePacedSend | None = None,
) -> None:
    active_pool = pool or _pool
    if active_pool is not None and await system_state.is_paused(active_pool):
        return
    if active_pool is None:
        raise RuntimeError("agentic pool has not been set")

    settings = get_settings()
    bot_spec = get_bot_spec(settings.bot_id)
    selected_prompt_version = prompt_version or settings.system_prompt_version
    send_typing_indicator = not bool(trigger_metadata and trigger_metadata.get("pacing"))
    turn_id: UUID | None = None
    started_at = datetime.now(UTC)
    responded_to_user = False
    try:
        partner = await partner_of(active_pool, user)
        hot_context = await build_hot_context(active_pool, user, partner, triggering_message_ids, trigger_metadata)
        rendered_hot_context = render_hot_context(hot_context)
        system_prompt = bot_spec.render_system_prompt(
            assistant_name=settings.assistant_name,
            user=user,
            partner=partner,
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
        hot_context_signals = _build_hot_context_signals(hot_context)
        skeleton_name = pick_default_skeleton(
            trigger_metadata=hot_context.trigger_metadata,
            charge=charge,
            hot_context_signals=hot_context_signals,
        )
        turn_plan = make_turn_plan(skeleton_name)
        ctx = TurnContext(
            turn_id,
            active_pool,
            user,
            partner,
            triggering_message_ids,
            current_step=turn_plan.current,
            turn_plan=turn_plan,
            trigger_charge=charge,
            explicit_partner_alert_requested=explicit_partner_alert_requested,
            turn_started_at=started_at,
            incremental_sending_enabled=(
                settings.messaging_provider.strip().lower() == "discord"
                and settings.discord_multi_message_enabled
            ),
            protected_owner_ids=[user.id, partner.id],
            send_typing_indicator=send_typing_indicator,
            before_paced_send=before_paced_send,
            sent_message_parts=[],
            hot_context_rendered=rendered_hot_context,
            trigger_metadata=hot_context.trigger_metadata,
        )
        seed_messages = bot_spec.build_initial_seed(
            trigger_metadata=hot_context.trigger_metadata,
            triggering_message_ids=triggering_message_ids,
            charge=charge,
            orient_header=orient_summary(
                trigger_metadata=hot_context.trigger_metadata,
                charge=charge,
                hot_context_signals=hot_context_signals,
            ),
            plan=turn_plan,
        )
        messages = seed_messages
        tool_call_count = 0
        assistant_text = ""
        respond_text = ""
        reaction_emoji: str | None = None
        sent_summary_for_record: str | None = None
        final_output_message_id: UUID | None = None
        reasoning_parts: list[str] = []
        delivered_parts: list[str] = []

        while turn_plan.current != "done":
            ctx.current_step = turn_plan.current
            step_text, messages, step_tool_count = await run_step(
                None,
                ctx,
                system_prompt,
                rendered_hot_context,
                _allowed_tools_for_step(ctx),
                messages,
                max_tool_iterations=STEP_ITERATION_CAPS.get(ctx.current_step, 4),
            )
            tool_call_count += step_tool_count

            if ctx.current_step == "respond":
                assistant_text = step_text
                if triggering_message_ids:
                    await active_pool.execute(
                        "UPDATE messages SET processing_state='processed' WHERE id = ANY($1) AND processing_state='raw'",
                        triggering_message_ids,
                    )

                sent_parts = ctx.sent_message_parts or []
                final_output_message_id = sent_parts[-1]["message_id"] if sent_parts else final_output_message_id
                responded_to_user = responded_to_user or bool(sent_parts)
                if assistant_text:
                    assistant_text = clean_user_facing_text(assistant_text)
                    reaction_emoji, assistant_text = _extract_reaction_directive(assistant_text)
                    if reaction_emoji is not None:
                        if await _react_to_triggering_message(active_pool, user, triggering_message_ids, reaction_emoji):
                            await _append_reasoning(active_pool, turn_id, f"Reacted to triggering message with {reaction_emoji}.")
                            await claim_onboarding_welcome(active_pool, user.id)
                            responded_to_user = True
                    if assistant_text:
                        dyad_owner_ids = [user.id, partner.id]
                        sendable_text = await _resolve_outbound_text(active_pool, turn_id, user, assistant_text, dyad_owner_ids)
                        already_sent = [part["content"] for part in sent_parts]
                        if sendable_text and sendable_text not in already_sent:
                            if await _newer_inbound_exists(
                                active_pool,
                                user,
                                triggering_message_ids,
                                fallback_started_at=started_at,
                            ):
                                await _append_reasoning(
                                    active_pool,
                                    turn_id,
                                    "Final outbound skipped because a newer inbound message arrived before send.",
                                )
                                assistant_text = ""
                            else:

                                async def before_final_provider_send(text: str = sendable_text) -> None:
                                    if before_paced_send is not None and not send_typing_indicator:
                                        await before_paced_send(text, send_kind="final", part_index=None)
                                    if await _newer_inbound_exists(
                                        active_pool,
                                        user,
                                        triggering_message_ids,
                                        fallback_started_at=started_at,
                                    ):
                                        raise NewerInboundBeforeFinalSend()

                                try:
                                    final_output_message_id = await send_outbound(
                                        active_pool,
                                        user,
                                        sendable_text,
                                        bot_turn_id=turn_id,
                                        protected_owner_ids=dyad_owner_ids,
                                        send_typing_indicator=send_typing_indicator,
                                        before_provider_send=(
                                            before_final_provider_send
                                            if before_paced_send is not None and not send_typing_indicator
                                            else None
                                        ),
                                    )
                                except NewerInboundBeforeFinalSend:
                                    await _append_reasoning(
                                        active_pool,
                                        turn_id,
                                        "Final outbound skipped because a newer inbound message arrived during paced send.",
                                    )
                                    assistant_text = ""
                                else:
                                    await _record_turn_final_output(active_pool, turn_id, final_output_message_id)
                                    await claim_onboarding_welcome(active_pool, user.id)
                                    assistant_text = sendable_text
                                    responded_to_user = True
                        elif sendable_text:
                            assistant_text = sendable_text
                elif charge in {"charged", "crisis"}:
                    await _append_reasoning(active_pool, turn_id, "silence; charged trigger but no justification produced")
                    logger.warning("charged/crisis trigger produced silence without model justification turn_id=%s", turn_id)

                respond_text = assistant_text
                delivered_parts = [part["content"] for part in (ctx.sent_message_parts or [])]
                if not delivered_parts and turn_id is not None:
                    delivered_parts = await sent_contents_for_turn(active_pool, turn_id)
                sent_summary_for_record = _sent_summary(delivered_parts, respond_text, reaction_emoji)

            if step_text:
                reasoning_parts.append(_collect_reasoning(messages, step_text if ctx.current_step == "respond" else ""))

            previous_step = ctx.current_step
            next_step = turn_plan.advance()
            if next_step != "done":
                messages.append(
                    bot_spec.build_step_transition_message(
                        plan=turn_plan,
                        sent_summary=sent_summary_for_record if next_step in {"record", "schedule"} else None,
                    )
                )
            if previous_step == next_step:
                raise BoundedLoopExceeded(f"turn plan did not advance from step {previous_step}")

        reasoning = "\n".join(part for part in reasoning_parts if part)
        executed_plan = f"Executed turn plan ({turn_plan.skeleton_name}): {turn_plan.trace()}"
        reasoning = "\n".join(part for part in (reasoning, executed_plan) if part)
        await _complete_turn(
            active_pool,
            turn_id,
            started_at,
            final_output_message_id,
            tool_call_count,
            reasoning,
        )
    except SpendCapExceeded:
        if turn_id is not None:
            scheduled = await _defer_for_text_cap(active_pool, user, triggering_message_ids)
            final_output_message_id = None
            if scheduled:
                fallback_text = "I'm running into limits today, will catch up tomorrow."

                async def before_fallback_provider_send(text: str = fallback_text) -> None:
                    if before_paced_send is not None and not send_typing_indicator:
                        await before_paced_send(text, send_kind="final", part_index=None)
                    if await _newer_inbound_exists(
                        active_pool,
                        user,
                        triggering_message_ids,
                        fallback_started_at=started_at,
                    ):
                        raise NewerInboundBeforeFinalSend()

                if await _newer_inbound_exists(
                    active_pool,
                    user,
                    triggering_message_ids,
                    fallback_started_at=started_at,
                ):
                    await _append_reasoning(
                        active_pool,
                        turn_id,
                        "Spend cap fallback skipped because a newer inbound message arrived before send.",
                    )
                else:
                    try:
                        final_output_message_id = await send_outbound(
                            active_pool,
                            user,
                            fallback_text,
                            bot_turn_id=turn_id,
                            send_typing_indicator=send_typing_indicator,
                            before_provider_send=(
                                before_fallback_provider_send
                                if before_paced_send is not None and not send_typing_indicator
                                else None
                            ),
                        )
                    except NewerInboundBeforeFinalSend:
                        await _append_reasoning(
                            active_pool,
                            turn_id,
                            "Spend cap fallback skipped because a newer inbound message arrived during paced send.",
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
        if responded_to_user:
            logger.warning("agentic turn failed after outbound was sent: %s", exc)
            return
        raise


async def run_agentic_turn(triggering_message_ids: list[UUID], user: User) -> None:
    if not triggering_message_ids:
        logger.warning("run_agentic_turn called without triggering messages for user_id=%s", user.id)
        return
    await _run_agentic(triggering_message_ids, user)


async def run_agentic_turn_with_metadata(
    triggering_message_ids: list[UUID],
    user: User,
    *,
    pacing_context: Any | None = None,
    trigger_metadata: Mapping[str, Any] | None = None,
    before_paced_send: BeforePacedSend | None = None,
) -> None:
    if not triggering_message_ids:
        logger.warning("run_agentic_turn_with_metadata called without triggering messages for user_id=%s", user.id)
        return
    await _run_agentic(
        triggering_message_ids,
        user,
        trigger_metadata=_trigger_metadata_with_pacing(trigger_metadata, pacing_context),
        before_paced_send=before_paced_send,
    )


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
