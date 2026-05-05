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


async def _consult_perspective(ctx: TurnContext, args: BaseModel) -> BaseModel:
    from app.services.tools.consult_perspective import consult_perspective

    return await consult_perspective(ctx, args)


TOOL_DESCRIPTIONS: dict[str, str] = {
    "search_messages": "Search prior message text, saved media explanations, or dates. Use for specific prior wording, repeated phrases, media explanations, and thread history; avoid for broad summaries. Example: find prior mentions of 'asked how my day went.' Each hit carries a `charge` label: `routine` (everyday content, low emotional weight), `notable` (emotionally meaningful but not heavy), `charged` (significant emotional weight, conflict, vulnerability, or intensity), `crisis` (meets crisis criteria).",
    "search_emojis": "Search the Unicode emoji dataset by meaning/name before reacting when a precise or unusual emoji would fit better than a generic one. Search by the emotional meaning, metaphor, or exact tone you want to convey. Example: search 'quiet support', 'fragile repair', or 'small but real progress.'",
    "recent_activity": "Summarize recent thread activity by sender for a compact cross-thread digest; avoid when exact wording matters. Example: see what each partner discussed this week.",
    "list_themes": "List known active or archived themes to orient to active life domains; do not create or update themes from this tool. Example: list active domains before deciding whether a new issue fits one.",
    "get_theme": "Fetch one theme's full detail when its specifics matter; do not call for every theme by default. Example: inspect a theme before updating it later.",
    "get_memories": "Read stored memories before adding, updating, or superseding memory; do not add memory without checking nearby existing rows. Example: check whether the family fact is already stored.",
    "list_watch_items": "List open or past watch items before scheduling or when a follow-up may already exist; do not duplicate open follow-ups. Example: check whether a coming conversation is already being tracked.",
    "get_observations": "Read observations before logging or reinforcing a pattern; do not create a new observation when an existing one should be reinforced. Example: search for a pattern before calling `update_observation`.",
    "get_distillations": "Read provisional synthesized explanations before adding or revising a distillation. Distillations are not evidence: they connect memories, observations, themes, or messages into a tentative explanation. Always search existing distillations before `add_distillation` or `revise_distillation`.",
    "get_oob": "Read active out-of-bounds constraints before discussing sensitive topics; do not reveal sensitive cores to the other partner. Example: inspect active boundaries before wording a sensitive reply.",
    "summarize_oob_topics": "Return safe counts and broad topic clusters for a partner's active OOB entries when a user asks what their partner has marked out of bounds. Never quote, paraphrase, or reveal entries; if there is only one entry on a niche topic, stay vague enough that the topic itself is not revealed.",
    "check_oob": "Check proposed outbound text against active OOB on every outbound draft; do not bypass it because in-prompt context seemed enough. If it returns a rewrite suggestion, treat it as advisory and send any revised text only through the normal outbound flow. Example: submit the draft and recipient before sending.",
    "get_self_model": "Read the assistant's compact model of one user when the user asks what you know about them or you need a compact model; do not treat it as the full audit trail. Example: answer 'what do you think I tend to do?'",
    "get_bot_actions": "Audit what the assistant did or why for questions about your own past actions; do not reconstruct from memory. Example: answer 'why did you tell her that?'",
    "list_scheduled_tasks": "List this user's pending agent-managed scheduled tasks, including the stable task_id, concrete job_id, next fire time, brief, and recurrence.",
    "send_message_part": "Send one coherent user-visible Discord message part now when that is conversationally useful — a short acknowledgement before a deeper thought, or when the user explicitly asks for separate messages. Use for natural conversational moves, not process updates or paragraph splitting. The result is the authority for what actually reached the user; if it reports `interrupted`, stop sending user-visible text in that turn.",
    "consult_perspective": "Run a bounded read-only advisory consult from a named or custom perspective before a charged, ambiguous, or possibly one-sided response. It cannot write, send, escalate, or call itself. Treat its output as advice, not authority — you remain responsible for final wording, OOB-safe delivery, and whether to respond at all.",
    "list_bridge_candidates": "List bridge candidates for this dyad to inspect pending/ready/sent bridge material. Target-facing views expose shareable summaries only, not raw private material. Partner paths are exactly `message_partner` (ready/actionable in the target prompt until addressed or declined), `coach_in_person`, `casual_share`, `hold_for_context`, `ask_permission`, and `do_not_bridge` (audit-only). Lifecycle statuses are exactly `pending` (drafted, not yet shareable), `ready` (cleared to send), `sent` (delivered to target), `declined` (source user refused sharing), `blocked` (OOB or sensitivity prevents sending), `addressed` (no longer needs bridging), and `expired` (stale).",
    "update_user_style_notes": "Replace a user's style notes when a durable communication or processing style is observed; avoid for transient mood. Example: update that someone processes by talking through a hard moment.",
    "update_cross_thread_sharing_default": "Set one user's opt-in/opt-out default for cross-thread bridge sharing after they explicitly choose. opt_in means you may use their perspective with the partner when it helps, unless OOB blocks it. opt_out means their thread is private by default; only bridge specific material they explicitly ask or allow you to share. Do not infer the setting from vague comfort or discomfort; get an explicit choice. OOB always overrides opt-in.",
    "create_bridge_candidate": "Create a bridge candidate when a partner says something that materially explains, contradicts, clarifies, softens, or adds important context to something the other partner has said and a shareable version may help. Link source message ids when possible. Write a neutral `shareable_summary`; keep private/raw reasoning in `internal_note`. Set `partner_path` to one of exactly `message_partner` (ready/actionable in the target prompt until addressed or declined; do not proactively send), `coach_in_person`, `casual_share`, `hold_for_context`, `ask_permission`, or `do_not_bridge` (audit-only). If the source user is unset or opt_out, create as `pending` unless they explicitly authorize this specific bridge. Lifecycle statuses are exactly `pending` (drafted, not yet shareable), `ready` (cleared to send), `sent` (delivered to target), `declined` (source user refused sharing), `blocked` (OOB or sensitivity prevents sending), `addressed` (no longer needs bridging), and `expired` (stale); high-sensitivity material should stay pending or blocked until it is safe.",
    "update_bridge_candidate": "Update bridge candidate lifecycle status, partner path, or improve summary/note without exposing raw private material. Partner paths are exactly `message_partner` (ready/actionable in the target prompt until addressed or declined; do not proactively send), `coach_in_person`, `casual_share`, `hold_for_context`, `ask_permission`, and `do_not_bridge` (audit-only). Lifecycle statuses are exactly `pending` (drafted, not yet shareable), `ready` (cleared to send), `sent` (delivered to target), `declined` (source user refused sharing), `blocked` (OOB or sensitivity prevents sending), `addressed` (no longer needs bridging), and `expired` (stale).",
    "send_bridge_candidate": "Explicitly send a `ready` bridge candidate now through the guarded outbound path using only its shareable summary. This is immediate-send behavior only; `message_partner` bridge rows should otherwise stay `ready` in the target prompt/hot context until addressed or declined. Only `ready` candidates are sendable; `pending`, `blocked`, and other lifecycle statuses cannot be sent.",
    "add_memory": "Add a new durable fact after searching existing memories; do not use for patterns. Example: store a concrete family or schedule fact.",
    "update_memory": "Correct or refresh an existing fact when the same fact changed or needs theme links; do not duplicate it. Example: update a changed job status.",
    "supersede_memory": "Replace an old memory with a corrected version when a prior fact is replaced by a new one; do not erase the old row. Example: a previous plan is no longer true.",
    "create_theme": "Create a new durable life-domain theme, including early provisional domains when an issue is clearly organizing the relationship. Keep sentiment/health modest when evidence is thin. Example: create a domain around caregiving responsibilities.",
    "update_theme": "Update an existing theme's summary, status, sentiment, or health when fresh evidence changes it, or reinforce when a new message shows the domain is active. Link related observations/memories with `related_theme_ids`.",
    "add_watch_item": "Create a near-term specific follow-up; do not use for broad themes. Example: check in after a hard conversation.",
    "update_watch_item": "Adjust an open watch item; do not add a duplicate.",
    "address_watch_item": "Mark a watch item handled when it was surfaced, resolved, or no longer applies; include which case in `addressing_note`.",
    "log_observation": "Log a new learned pattern after searching existing observations; do not use to reinforce an existing observation. `confidence` levels: `high` (multiple instances or stated by partner), `medium` (clear pattern, limited evidence), `low` (initial impression).",
    "update_observation": "Reinforce, correct, or retire an existing pattern. `confidence` levels: `high` (multiple instances or stated by partner), `medium` (clear pattern, limited evidence), `low` (initial impression).",
    "add_distillation": "Add a provisional synthesized explanation only after searching existing distillations. Use for a tentative why/how that connects multiple memories, observations, themes, or messages; do not use for concrete facts, grounded patterns, life domains, follow-ups, or style preferences. Keep `source_user_ids` conservative and link supporting evidence. If `visibility` is `dyad_shareable`, provide a deliberately safe `shareable_summary`.",
    "update_distillation": "Make conservative in-place edits to an existing distillation: wording clarification, status, links, source users, or shareable summary. Do not use for a substantive new explanation; use `revise_distillation` so the old row remains auditable.",
    "revise_distillation": "Substantively revise a distillation after searching existing distillations. This supersedes the old synthesis with a new tentative explanation while preserving the old row. Link supporting evidence, keep source attribution conservative, and use `dyad_shareable` only with a safe summary.",
    "add_oob": "Add a new active out-of-bounds constraint when a user sets a new sharing boundary; do not infer OOB silently from discomfort alone.",
    "update_oob": "Revise an existing out-of-bounds constraint when the owner changes severity, wording, review time, or shareable context.",
    "lift_oob": "Lift an out-of-bounds constraint when the owner says it no longer applies.",
    "schedule_checkin": "Schedule one useful follow-up check-in for this user; do not stack multiple competing pending check-ins for the same user.",
    "cancel_scheduled_checkin": "Cancel a pending check-in when it is no longer wanted or relevant.",
    "schedule_task": "Schedule an agent-managed future task brief for the current user. Use `when` for one-shot tasks and `recurrence` for durable daily or weekly repeats.",
    "update_scheduled_task": "Update a pending agent-managed scheduled task by task_id, job_id, or current_task=true during a scheduled_task turn. Use for changing the brief, next fire time, or recurrence.",
    "cancel_scheduled_task": "Cancel a pending agent-managed scheduled task by task_id, job_id, or current_task=true during a scheduled_task turn.",
    "escalate_to_partner": "Send partner escalation only when one of two named gates is true: the triggering message meets the `crisis` charge definition, or the user explicitly asks you to alert their partner. The reason must name which gate fired. Do not use for ordinary friction, even intense friction. Use concise, balanced, non-accusatory wording; do not include protected OOB details, private analysis, pressure, or anything designed to manage the partner's reaction.",
    "edit_outbound_message": "Edit one already-sent bot outbound message when the original wording was materially wrong, unsafe, confusing, too sharp, or likely to land badly and an edit is cleaner than a follow-up. Do not edit to hide accountability; if the correction matters, acknowledge it in conversation when appropriate.",
    "delete_outbound_message": "Delete one already-sent bot outbound message only when it should not remain visible — accidental protected detail, wrong recipient, serious factual mistake, or a message that would predictably worsen the situation. Prefer editing when the message can be safely corrected.",
    "react_to_message": "Add one precise Unicode emoji reaction to a visible Discord message when an emoji is the most natural response or useful alongside a short reply. Call `search_emojis` first when the right reaction is not obvious; choose precise, emotionally apt, sometimes unusual emoji over generic 👍/❤️/👋. Do not overuse, and do not choose cute or obscure emoji when the moment is serious.",
    "explain_media_item": "Explain a stored image and persist the explanation into message memory so `search_messages` can find it later. Use when a stored image needs a fresh durable explanation.",
    "log_feedback": "Record user feedback about a message, turn, or general behavior; do not convert every emotional reaction into feedback.",
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
    "get_distillations": read_tools.get_distillations,
    "get_oob": read_tools.get_oob,
    "summarize_oob_topics": read_tools.summarize_oob_topics,
    "check_oob": read_tools.check_oob,
    "get_self_model": read_tools.get_self_model,
    "get_bot_actions": read_tools.get_bot_actions,
    "send_message_part": read_tools.send_message_part,
    "consult_perspective": _consult_perspective,
    "list_bridge_candidates": read_tools.list_bridge_candidates,
    "list_scheduled_tasks": write_tools.list_scheduled_tasks,
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
    "add_distillation": write_tools.add_distillation,
    "update_distillation": write_tools.update_distillation,
    "revise_distillation": write_tools.revise_distillation,
    "add_oob": write_tools.add_oob,
    "update_oob": write_tools.update_oob,
    "lift_oob": write_tools.lift_oob,
    "schedule_checkin": write_tools.schedule_checkin,
    "cancel_scheduled_checkin": write_tools.cancel_scheduled_checkin,
    "schedule_task": write_tools.schedule_task,
    "update_scheduled_task": write_tools.update_scheduled_task,
    "cancel_scheduled_task": write_tools.cancel_scheduled_task,
    "escalate_to_partner": write_tools.escalate_to_partner,
    "edit_outbound_message": write_tools.edit_outbound_message,
    "delete_outbound_message": write_tools.delete_outbound_message,
    "react_to_message": write_tools.react_to_message,
    "explain_media_item": write_tools.explain_media_item,
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
    "get_distillations",
    "get_oob",
    "summarize_oob_topics",
    "check_oob",
    "get_self_model",
    "get_bot_actions",
    "send_message_part",
    "consult_perspective",
    "list_bridge_candidates",
    "list_scheduled_tasks",
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
    "add_distillation",
    "update_distillation",
    "revise_distillation",
    "add_oob",
    "update_oob",
    "lift_oob",
    "schedule_checkin",
    "cancel_scheduled_checkin",
    "schedule_task",
    "update_scheduled_task",
    "cancel_scheduled_task",
    "escalate_to_partner",
    "edit_outbound_message",
    "delete_outbound_message",
    "react_to_message",
    "explain_media_item",
    "log_feedback",
}

CONSULT_PHASE_TOOLS = READ_PHASE_TOOLS - {"send_message_part", "consult_perspective"}

_CONSULT_OWNER_INJECTING_TOOLS = {"check_oob"}


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
    if ctx.phase == "read":
        return READ_PHASE_TOOLS
    if ctx.phase == "write":
        return WRITE_PHASE_TOOLS
    if ctx.phase == "consult":
        return CONSULT_PHASE_TOOLS
    return set()


def _inject_consult_defaults(name: str, raw_args: dict[str, Any], ctx: TurnContext) -> dict[str, Any]:
    if ctx.phase != "consult" or name not in _CONSULT_OWNER_INJECTING_TOOLS or not ctx.protected_owner_ids:
        return raw_args
    merged = dict(raw_args or {})
    existing = merged.get("protected_owner_ids")
    if not existing:
        merged["protected_owner_ids"] = [str(uid) for uid in ctx.protected_owner_ids]
        return merged
    updated = list(existing)
    seen = {str(uid) for uid in updated}
    for uid in ctx.protected_owner_ids:
        if str(uid) not in seen:
            updated.append(str(uid))
            seen.add(str(uid))
    merged["protected_owner_ids"] = updated
    return merged


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
        args = input_model.model_validate(_inject_consult_defaults(name, raw_args, ctx))
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
