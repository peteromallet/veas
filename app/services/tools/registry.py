"""Tool registry bridge for Anthropic tool-use calls."""

from __future__ import annotations

from datetime import UTC, datetime
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ValidationError

from evals.capture import record_tool_call
from app.services.turn_context import TurnContext
from app.services.tools import read_tools, write_tools
from app.services.tools.write_tools import ToolCallRejected
from tool_schemas import TOOL_REGISTRY

ToolFn = Callable[[TurnContext, BaseModel], Awaitable[BaseModel]]


TOOL_DESCRIPTIONS: dict[str, str] = {
    "search_messages": "Search prior message text or dates when exact conversation history matters; avoid for broad summaries.",
    "search_emojis": "Search the Unicode emoji dataset by meaning/name before choosing a precise or unusual reaction.",
    "recent_activity": "Summarize recent thread activity by sender; avoid when a precise quote or row is needed.",
    "list_themes": "List known active or archived themes; avoid when one theme's full detail is needed.",
    "get_theme": "Fetch one theme's full detail; avoid when you do not already have a theme id.",
    "get_memories": "Read stored memories before adding, updating, or superseding memory; avoid for raw message search.",
    "list_watch_items": "List open or past watch items for follow-up state; avoid for durable facts or themes.",
    "get_observations": "Read observations before reinforcing or logging a new observation; avoid for user style notes.",
    "get_oob": "Read active out-of-bounds constraints before sensitive wording; avoid for general preference lookup.",
    "summarize_oob_topics": "Return safe counts and broad topic clusters for a partner's active OOB entries; never reveals OOB contents.",
    "check_oob": "Check proposed outbound text before sending against active OOB for protected owners; rewrite suggestions are advisory and must be sent only through normal outbound flow.",
    "get_self_model": "Read the assistant's compact model of one user; avoid when you need exact source rows.",
    "get_bot_actions": "Audit what the assistant did or why; avoid relying on memory for action history.",
    "send_message_part": "Send one coherent user-visible Discord message part now when that is conversationally useful. The result is the authority for what actually reached the user.",
    "list_bridge_candidates": "List bridge candidates for this dyad; target-facing views must use approved shareable summaries, not raw private material.",
    "update_user_style_notes": "Replace a user's style notes when a stable communication preference changes; avoid for one-off moods.",
    "update_cross_thread_sharing_default": "Set one user's opt-in/opt-out default for cross-thread bridge sharing after they explicitly choose it.",
    "create_bridge_candidate": "Create a bridge candidate linked to source messages and optional memories/observations when cross-thread material may need careful sharing.",
    "update_bridge_candidate": "Update bridge candidate lifecycle status or summary without exposing raw private material.",
    "send_bridge_candidate": "Send a ready bridge candidate through the guarded outbound path using only its shareable summary.",
    "add_memory": "Add a durable fact after searching existing memories; avoid for repeated or uncertain impressions.",
    "update_memory": "Update an existing memory when the same fact changed or needs theme links; avoid creating duplicates.",
    "supersede_memory": "Replace an old memory with a corrected version; avoid when a simple update is enough.",
    "create_theme": "Create a new recurring theme after checking existing themes; avoid for single-message topics.",
    "update_theme": "Update or reinforce an existing theme; avoid when you need a new distinct theme.",
    "add_watch_item": "Create a near-term item to revisit; avoid for durable memories.",
    "update_watch_item": "Adjust an existing watch item; avoid creating another item for the same follow-up.",
    "address_watch_item": "Mark a watch item handled with a short note; avoid if it remains open.",
    "log_observation": "Log a new meaningful observation after searching existing observations; avoid for reinforcement.",
    "update_observation": "Reinforce or revise an existing observation; avoid logging a duplicate observation.",
    "add_oob": "Add a new active out-of-bounds constraint; avoid when the existing constraint only needs editing.",
    "update_oob": "Revise an existing out-of-bounds constraint; avoid adding another copy.",
    "lift_oob": "Lift an out-of-bounds constraint that no longer applies; avoid deleting audit history.",
    "schedule_checkin": "Schedule one follow-up check-in for this user; avoid stacking multiple pending check-ins.",
    "cancel_scheduled_checkin": "Cancel a pending check-in for this user; avoid when the check-in should be rescheduled.",
    "escalate_to_partner": "Send partner escalation only for crisis or explicit request; avoid for ordinary relays.",
    "edit_outbound_message": "Edit one already-sent bot outbound message when correcting wording is better than sending a follow-up; only works for delivered bot messages.",
    "delete_outbound_message": "Delete one already-sent bot outbound message when leaving it visible would be harmful or clearly wrong; only works for delivered bot messages.",
    "react_to_message": "Add one precise Unicode emoji reaction to a visible Discord message; prefer emotionally exact, non-obvious emoji over generic defaults.",
    "log_feedback": "Record user feedback about a message, turn, or general behavior; avoid for inferred preferences.",
}


TOOL_DISPATCH: dict[str, ToolFn] = {
    "search_messages": read_tools.search_messages,
    "search_emojis": read_tools.search_emojis,
    "recent_activity": read_tools.recent_activity,
    "list_themes": read_tools.list_themes,
    "get_theme": read_tools.get_theme,
    "get_memories": read_tools.get_memories,
    "list_watch_items": read_tools.list_watch_items,
    "get_observations": read_tools.get_observations,
    "get_oob": read_tools.get_oob,
    "summarize_oob_topics": read_tools.summarize_oob_topics,
    "check_oob": read_tools.check_oob,
    "get_self_model": read_tools.get_self_model,
    "get_bot_actions": read_tools.get_bot_actions,
    "send_message_part": read_tools.send_message_part,
    "list_bridge_candidates": read_tools.list_bridge_candidates,
    "update_user_style_notes": write_tools.update_user_style_notes,
    "update_cross_thread_sharing_default": write_tools.update_cross_thread_sharing_default,
    "create_bridge_candidate": write_tools.create_bridge_candidate,
    "update_bridge_candidate": write_tools.update_bridge_candidate,
    "send_bridge_candidate": write_tools.send_bridge_candidate,
    "add_memory": write_tools.add_memory,
    "update_memory": write_tools.update_memory,
    "supersede_memory": write_tools.supersede_memory,
    "create_theme": write_tools.create_theme,
    "update_theme": write_tools.update_theme,
    "add_watch_item": write_tools.add_watch_item,
    "update_watch_item": write_tools.update_watch_item,
    "address_watch_item": write_tools.address_watch_item,
    "log_observation": write_tools.log_observation,
    "update_observation": write_tools.update_observation,
    "add_oob": write_tools.add_oob,
    "update_oob": write_tools.update_oob,
    "lift_oob": write_tools.lift_oob,
    "schedule_checkin": write_tools.schedule_checkin,
    "cancel_scheduled_checkin": write_tools.cancel_scheduled_checkin,
    "escalate_to_partner": write_tools.escalate_to_partner,
    "edit_outbound_message": write_tools.edit_outbound_message,
    "delete_outbound_message": write_tools.delete_outbound_message,
    "react_to_message": write_tools.react_to_message,
    "log_feedback": write_tools.log_feedback,
}

READ_PHASE_TOOLS = {
    "search_messages",
    "search_emojis",
    "recent_activity",
    "list_themes",
    "get_theme",
    "get_memories",
    "list_watch_items",
    "get_observations",
    "get_oob",
    "summarize_oob_topics",
    "check_oob",
    "get_self_model",
    "get_bot_actions",
    "send_message_part",
    "list_bridge_candidates",
}

WRITE_PHASE_TOOLS = {
    "update_user_style_notes",
    "update_cross_thread_sharing_default",
    "create_bridge_candidate",
    "update_bridge_candidate",
    "send_bridge_candidate",
    "add_memory",
    "update_memory",
    "supersede_memory",
    "create_theme",
    "update_theme",
    "add_watch_item",
    "update_watch_item",
    "address_watch_item",
    "log_observation",
    "update_observation",
    "add_oob",
    "update_oob",
    "lift_oob",
    "schedule_checkin",
    "cancel_scheduled_checkin",
    "escalate_to_partner",
    "edit_outbound_message",
    "delete_outbound_message",
    "react_to_message",
    "log_feedback",
}


def to_anthropic_tools(allowed: set[str]) -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "description": TOOL_DESCRIPTIONS[name],
            "input_schema": input_model.model_json_schema(),
        }
        for name, (input_model, _) in TOOL_REGISTRY.items()
        if name in allowed
    ]


def _phase_allowed(ctx: TurnContext) -> set[str]:
    return READ_PHASE_TOOLS if ctx.phase == "read" else WRITE_PHASE_TOOLS


def _tool_error(message: str) -> dict[str, Any]:
    return {"error": message, "is_error": True}


async def call_tool(name: str, raw_args: dict[str, Any], ctx: TurnContext) -> dict[str, Any]:
    started = datetime.now(UTC)
    phase = ctx.phase
    registry_entry = TOOL_REGISTRY.get(name)
    if registry_entry is None:
        result = _tool_error(f"unknown tool: {name}")
        record_tool_call(tool_name=name, args=raw_args, result=result, phase=phase, started_at=started)
        return result
    if name not in _phase_allowed(ctx):
        result = _tool_error(f"phase: tool {name} is not allowed in {ctx.phase} phase")
        record_tool_call(tool_name=name, args=raw_args, result=result, phase=phase, started_at=started)
        return result
    input_model, output_model = registry_entry
    try:
        args = input_model.model_validate(raw_args)
    except ValidationError as exc:
        result = _tool_error(f"validation: {exc}")
        record_tool_call(tool_name=name, args=raw_args, result=result, phase=phase, started_at=started)
        return result
    fn = TOOL_DISPATCH.get(name)
    if fn is None:
        result = _tool_error(f"dispatch: tool {name} is not implemented")
        record_tool_call(tool_name=name, args=args.model_dump(mode="json"), result=result, phase=phase, started_at=started)
        return result
    try:
        result = await fn(ctx, args)
    except ToolCallRejected as exc:
        result = {**exc.result, "is_error": True}
        record_tool_call(tool_name=name, args=args.model_dump(mode="json"), result=result, phase=phase, started_at=started)
        return result
    except Exception as exc:
        result = _tool_error(f"exception: {exc}")
        record_tool_call(tool_name=name, args=args.model_dump(mode="json"), result=result, phase=phase, started_at=started)
        raise
    try:
        validated = output_model.model_validate(result)
    except ValidationError as exc:
        result = _tool_error(f"result_validation: {exc}")
        record_tool_call(tool_name=name, args=args.model_dump(mode="json"), result=result, phase=phase, started_at=started)
        return result
    result_dict = validated.model_dump(mode="json")
    record_tool_call(tool_name=name, args=args.model_dump(mode="json"), result=result_dict, phase=phase, started_at=started)
    return result_dict
