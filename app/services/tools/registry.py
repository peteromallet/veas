"""Tool registry bridge for Anthropic tool-use calls."""

from __future__ import annotations

from datetime import UTC, datetime
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ValidationError

from evals.capture import record_tool_call
from app.services.turn_audit import record_turn_event
from app.services.turn_plan import TurnStep
from app.services.turn_context import TurnContext
from app.services.tools import read_tools, write_tools
from app.services.tools.audit import log_tool_call as _log_tool_call_audit
from app.services.tools.write_tools import ToolCallRejected
from tool_schemas import (
    TOOL_REGISTRY,
    SetTopicStatusInput,
    SetTopicStatusOutput,
    SetPartnerSharingInput,
    SetPartnerSharingOutput,
    UpdateTurnPlanInput,
    UpdateTurnPlanOutput,
)

TOOL_REGISTRY["set_topic_status"] = (SetTopicStatusInput, SetTopicStatusOutput)
TOOL_REGISTRY["set_partner_sharing"] = (SetPartnerSharingInput, SetPartnerSharingOutput)

ToolFn = Callable[[TurnContext, BaseModel], Awaitable[BaseModel]]


async def _submit_live_brief_handler(
    ctx: TurnContext, args: BaseModel
) -> BaseModel:
    from tool_schemas import SubmitLiveBriefOutput

    ctx.extras["submitted_live_brief"] = args.model_dump()
    return SubmitLiveBriefOutput(ok=True)


async def _submit_live_debrief_handler(
    ctx: TurnContext, args: BaseModel
) -> BaseModel:
    from tool_schemas import SubmitLiveDebriefOutput

    ctx.extras["submitted_live_debrief"] = args.model_dump()
    return SubmitLiveDebriefOutput(ok=True)


async def _consult_perspective(ctx: TurnContext, args: BaseModel) -> BaseModel:
    from app.services.tools.consult_perspective import consult_perspective

    return await consult_perspective(ctx, args)


async def _update_turn_plan(
    ctx: TurnContext, args: UpdateTurnPlanInput
) -> UpdateTurnPlanOutput:
    plan = ctx.turn_plan
    if args.add_steps:
        plan.add_steps(list(args.add_steps))
    if args.remove_steps:
        plan.remove_steps(list(args.remove_steps))
    if args.mark_done:
        for step in args.mark_done:
            plan.mark_done(step)
    if args.note:
        plan.notes.append(args.note)
    return UpdateTurnPlanOutput(
        plan=plan.render_checklist(),
        current=plan.current,
        steps=list(plan.steps),
        completed=list(plan.completed),
        notes=list(plan.notes),
    )


TOOL_DESCRIPTIONS: dict[str, str] = {
    "submit_live_brief": "Submit the final structured agenda for a live voice session. Call this exactly once when prep is complete — the agenda must pass existing validation (unique item ids, at least one 'must' item, all next_item_ids resolve). This is the required finalization gate for live prep; plain text without this call is not a valid agenda.",
    "submit_live_debrief": "Submit the final structured review for a live voice debrief session. Call this exactly once when debrief is complete. This is the required finalization gate for live debrief; missing this call after the tool cap is a retryable failure. Include review_summary, what_heard, what_decided, still_open, what_to_remember, durable_write_summary, open_questions, and optional evidence references with transcript_turn_id, quote, and confidence.",
    "update_turn_plan": "Adjust the private turn checklist when the initial skeleton is too light or too heavy. Use this instead of hidden scratch notes. This does not send user-facing text or write durable state.",
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
    "check_oob": "Check proposed outbound text when drafting around sensitive cross-thread or OOB material, or when you need a rewrite decision before choosing what to say. Normal final replies and `send_message_part` go through the delivery path's final OOB check.",
    "get_self_model": "Read the assistant's compact model of one user when the user asks what you know about them or you need a compact model; do not treat it as the full audit trail. Example: answer 'what do you think I tend to do?'",
    "get_bot_actions": "Audit what the assistant did or why for questions about your own past actions; do not reconstruct from memory. Use this when the user asks 'did you do X this morning?', 'what have you been up to?', 'why did you tell her that?' — silent turns (scheduled tasks that fired but sent no message) only exist here, not in the message timeline. Each returned turn carries its tool_calls; drill into one with `get_tool_call(tool_call_id)` for full arguments and result.",
    "get_tool_call": "Fetch full arguments + result for one past tool_call by id. Surfaced from the silent-turns hot-context block and from get_bot_actions tool_calls listings. Use when the highlight summary in context isn't specific enough to answer the question — for example to inspect exactly which messages a prior `search_messages` returned, or what brief a prior `schedule_task` set.",
    "list_all_reminders": "Return a unified list of every pending agent-managed task AND user-facing check-in for this (user, bot, topic), ordered ascending by next fire time. Each item includes the scheduled_jobs.id, kind, human-readable recurrence_label, and the canonical recurrence_rule dict (pass it back verbatim to update_scheduled_task when changing recurrence). Use BEFORE booking a new reminder to decide whether to bundle. Scope note: list_scheduled_checkins is scoped to (user, bot); list_all_reminders is scoped to (user, bot, topic). cancel_scheduled_checkin currently has no job_id parameter and cancels at user scope only.",
    "list_scheduled_tasks": "List this user's pending agent-managed scheduled tasks, including the stable task_id, concrete job_id, next fire time, brief, and recurrence.",
    "send_message_part": "Send one coherent user-visible Discord message part now when that is conversationally useful — a short acknowledgement before a deeper thought, or when the user explicitly asks for separate messages. Use for natural conversational moves, not process updates or paragraph splitting. The result is the authority for what actually reached the user; if it reports `interrupted`, stop sending user-visible text in that turn.",
    "consult_perspective": "Use only in the consult step, and only when the user explicitly asks for a second opinion, critique, review, or another perspective. It cannot write, send, escalate, or call itself. Treat its output as advice, not authority.",
    "list_bridge_candidates": "List bridge candidates for this dyad to inspect pending/ready/sent bridge material. Target-facing views expose shareable summaries only, not raw private material. Partner paths are exactly `message_partner` (ready/actionable in the target prompt until addressed or declined), `coach_in_person`, `casual_share`, `hold_for_context`, `ask_permission`, and `do_not_bridge` (audit-only). Lifecycle statuses are exactly `pending` (drafted, not yet shareable), `ready` (cleared to send), `sent` (delivered to target), `declined` (source user refused sharing), `blocked` (OOB or sensitivity prevents sending), `addressed` (no longer needs bridging), and `expired` (stale).",
    "update_user_style_notes": "Replace a user's style notes when a durable communication or processing style is observed; avoid for transient mood. Example: update that someone processes by talking through a hard moment.",
    "set_partner_sharing": "Set the current user's opt-in/opt-out choice for this calling bot after they explicitly choose. opt_in allows this bot's safe dyad_shareable summaries to be shown to the partner; opt_out keeps this bot's rows private by default. Do not infer the setting from vague comfort or discomfort; get an explicit choice.",
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
    "schedule_checkin": "Schedule one useful user-facing future check-in/message/reminder for this user; do not stack multiple competing pending check-ins for the same user. Use this, not `schedule_task`, when the user asks 'message me', 'remind me', or 'check in with me' at a future time. Prefer `delay` by default for simple relative durations like 'in two hours', 'in 10 hours', or 'in two days'. Use `local_when` for local clock phrases like '9pm tonight' or 'Monday at 8'. Use `when` only when you already have an exact timezone-aware instant.",
    "cancel_scheduled_checkin": "Cancel a pending check-in when it is no longer wanted or relevant.",
    "schedule_task": "Schedule an internal agent-managed future task brief for the current user, including recurring/non-message work. Do not use for user-facing 'message/remind/check in with me' requests; use `schedule_checkin` for those. Prefer `delay` by default for simple relative durations like 'in two hours', 'in 10 hours', or 'in two days'. Use `local_when` for local clock phrases like '9pm tonight' or 'Monday at 8'. Use `when` only when you already have an exact timezone-aware instant. Use `recurrence` for durable hourly, daily, or weekly repeats.",
    "update_scheduled_task": "Update a pending agent-managed scheduled task by task_id, job_id, or current_task=true during a scheduled_task turn. Use for changing the brief, next fire time, or recurrence. Prefer `delay` by default for simple relative durations; use `local_when` for local clock phrases; use `when` only for exact timezone-aware instants.",
    "update_scheduled_checkin": "Update a pending user-facing check-in by job_id. Use for changing about_what (the user-facing line), reason, or next fire time. Symmetric to update_scheduled_task but for one-off user-facing reminders.",
    "cancel_scheduled_task": "Cancel a pending agent-managed scheduled task by task_id, job_id, or current_task=true during a scheduled_task turn.",
    "list_scheduled_checkins": "List this user's pending user-facing check-ins for the current bot. Use BEFORE booking another check-in to avoid duplicating a follow-up, and to answer the user's 'what reminders do I have set up?' question. Symmetric to list_scheduled_tasks but for one-off user-facing reminders.",
    "schedule_partner_checkin": "Schedule a future check-in turn on the current user's dyad partner's side, on the same bot. Use ONLY for explicit user requests like 'can you check in on Hannah?' or 'see how my partner is doing.' The partner is resolved server-side from the dyad — you do NOT pass any user id. `nudge_note` is a short neutral note shown to the recipient (never quote the originator). `reason` is audit-only. Rejects when the recipient has not opted in for this bot (`recipient_state` will be `opt_out` or `pending`), when no dyad partner exists, when a recent nudge already happened in the last 24h, or when a pending nudge with the same originator/recipient/bot is still open. Tell the user what was scheduled — and what you'll say to the partner — in the same reply.",
    "cancel_partner_nudge": "Cancel a pending partner-nudge you previously scheduled. Only the original scheduling user can cancel; non-pending statuses cannot be cancelled.",
    "escalate_to_partner": "Send partner escalation only when one of two named gates is true: the triggering message meets the `crisis` charge definition, or the user explicitly asks you to alert their partner. The reason must name which gate fired. Do not use for ordinary friction, even intense friction. Use concise, balanced, non-accusatory wording; do not include protected OOB details, private analysis, pressure, or anything designed to manage the partner's reaction.",
    "edit_outbound_message": "Edit one already-sent bot outbound message when the original wording was materially wrong, unsafe, confusing, too sharp, or likely to land badly and an edit is cleaner than a follow-up. Do not edit to hide accountability; if the correction matters, acknowledge it in conversation when appropriate.",
    "delete_outbound_message": "Delete one already-sent bot outbound message only when it should not remain visible — accidental protected detail, wrong recipient, serious factual mistake, or a message that would predictably worsen the situation. Prefer editing when the message can be safely corrected.",
    "react_to_message": "Add one precise Unicode emoji reaction to a visible Discord message when an emoji is the most natural response or useful alongside a short reply. Call `search_emojis` first when the right reaction is not obvious; choose precise, emotionally apt, sometimes unusual emoji over generic 👍/❤️/👋. Do not overuse, and do not choose cute or obscure emoji when the moment is serious.",
    "explain_media_item": "Explain a stored image and persist the explanation into message memory so `search_messages` can find it later. Use when a stored image needs a fresh durable explanation.",
    "log_feedback": "Record user feedback about a message, turn, or general behavior; do not convert every emotional reaction into feedback.",
    "set_topic_status": "Update or replace the current status for this topic. Headline ≤ 80 chars, body ≤ 300 chars. Use at most once per turn during the record step when status has materially changed.",
    "set_pregnancy_edd": "Capture a pregnancy's estimated due date the first time a user mentions they are pregnant. You MUST call this before any other pregnancy tool. Provide `edd` as an ISO date (e.g. '2026-10-22') and `dating_basis` as 'lmp' (last menstrual period) or 'scan' (dating ultrasound). If you know the LMP or scan date, include those as well; `started_at` defaults to now. This will error if there is already an active pregnancy — use correct_pregnancy_edd to revise instead.",
    "correct_pregnancy_edd": "Revise the EDD mid-pregnancy — for example when a dating scan gives a more precise date. Requires an active pregnancy (set_pregnancy_edd must have been called first). If the dating basis flips to 'scan', the correction timestamp is recorded automatically. This will error with 'no_active_pregnancy' if there is no pregnancy to correct — use set_pregnancy_edd first.",
    "end_pregnancy": "Close the active pregnancy with its outcome: 'birth', 'loss', or 'termination'. Call this when the user tells you the pregnancy has ended. Requires an active pregnancy. If the pregnancy is already ended this will error with 'pregnancy already ended on <date>' — do not retry; the state is already recorded.",
    # commitment/event tools (shared by Hector + Habits)
    "list_commitments": "List active or recently active commitments for the current user and topic. Use before creating, updating, or closing a commitment to avoid duplicates and to answer questions about what is currently tracked.",
    "create_commitment": "Create a new commitment for the current user. Only create a commitment from a concrete plan the user has explicitly described (e.g., 'I'm working out Mon/Wed/Fri' or 'I meditate every morning before coffee'). For vague goals ('I want to get healthier', 'I should be more present'), ask a clarifying question first — do not create a commitment. Set pressure_style to 'very_gentle', 'low_key' (default), or 'firm'. cadence must be one of 'daily', 'weekdays', 'weekly_count', 'custom', or 'custom_days'.",
    "update_commitment": "Update an existing commitment's fields (label, kind, cadence, target_count, days_of_week, schedule_rule, pressure_style, start_date, end_date). Only fields you provide are changed. Call list_commitments first to get the commitment_id.",
    "close_commitment": "Close an active commitment by setting its status to 'paused', 'completed', or 'dropped'. Call this when the user says they are stopping, pausing, or have finished a commitment. Once closed, the commitment is no longer active but remains auditable.",
    "log_event": "Log an event against a commitment (or standalone with a metric_key). Provide at least one of adherence_status ('done', 'missed', 'excused'), value_numeric, or value_text. Use adherence_status to mark whether a scheduled slot was completed, missed, or excused. Events are scoped to the current user, topic, and bot.",
    "list_events": "List recent events for the current user and topic, optionally filtered by commitment_id. Use before logging a corrective event to avoid duplicates, and to answer questions about recent activity.",
    "get_adherence": "Compute this week's adherence status for active commitments. Returns per-day slot status for each commitment (done, missed, excused, unknown, pending). Use this before asking the user about missed days — check the adherence board first so you can distinguish unknown from missed and avoid shaming.",
}


TOOL_DISPATCH: dict[str, ToolFn] = {
    "submit_live_brief": _submit_live_brief_handler,
    "submit_live_debrief": _submit_live_debrief_handler,
    "update_turn_plan": _update_turn_plan,
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
    "get_tool_call": read_tools.get_tool_call,
    "send_message_part": read_tools.send_message_part,
    "consult_perspective": _consult_perspective,
    "list_bridge_candidates": read_tools.list_bridge_candidates,
    "list_scheduled_tasks": write_tools.list_scheduled_tasks,
    "update_user_style_notes": write_tools.update_user_style_notes,
    "set_partner_sharing": write_tools.set_partner_sharing,
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
    "update_scheduled_checkin": write_tools.update_scheduled_checkin,
    "cancel_scheduled_task": write_tools.cancel_scheduled_task,
    "schedule_partner_checkin": write_tools.schedule_partner_checkin,
    "cancel_partner_nudge": write_tools.cancel_partner_nudge,
    "list_scheduled_checkins": read_tools.list_scheduled_checkins,
    "list_all_reminders": read_tools.list_all_reminders,
    "escalate_to_partner": write_tools.escalate_to_partner,
    "edit_outbound_message": write_tools.edit_outbound_message,
    "delete_outbound_message": write_tools.delete_outbound_message,
    "react_to_message": write_tools.react_to_message,
    "explain_media_item": write_tools.explain_media_item,
    "log_feedback": write_tools.log_feedback,
    "set_topic_status": write_tools.set_topic_status,
    "set_pregnancy_edd": write_tools.set_pregnancy_edd,
    "correct_pregnancy_edd": write_tools.correct_pregnancy_edd,
    "end_pregnancy": write_tools.end_pregnancy,
    # hector (commitments/events)
    "create_commitment": write_tools.create_commitment,
    "update_commitment": write_tools.update_commitment,
    "close_commitment": write_tools.close_commitment,
    "log_event": write_tools.log_event,
    "list_commitments": read_tools.list_commitments,
    "list_events": read_tools.list_events,
    "get_adherence": read_tools.get_adherence,
}

# ── Commitment/event tools (shared by Hector + Habits) ────────────────────
# These tools live on the mediator.commitments / mediator.events substrate.
# They are exclusive to bots that own a commitment-tracking topic (Hector on
# `fitness`, Habits on `habits`). Every other bot (coach, tante_rosi,
# mediator) MUST subtract this set from its tool_allowlist to prevent
# leakage. Name kept as HECTOR_ONLY_TOOLS for backward compatibility with
# tests and other bot specs that already import it.
HECTOR_ONLY_TOOLS: frozenset[str] = frozenset({
    "create_commitment",
    "update_commitment",
    "close_commitment",
    "log_event",
    "list_commitments",
    "list_events",
    "get_adherence",
})

# ── Bot-exclusive tools ────────────────────────────────────────────────────
# Dict mapping a *set* of bot_ids → tools that ONLY those bots may use.
# Any bot whose id is not in the keyed frozenset has these tools removed in
# _step_allowed(). Commitment/event tools are shared by Hector and Habits.
BOT_EXCLUSIVE_TOOLS: dict[frozenset[str], frozenset[str]] = {
    frozenset({"hector", "habits"}): HECTOR_ONLY_TOOLS,
}

# Tools whose implementation already calls audit.log_tool_call themselves
# (everything in write_tools.py + list_scheduled_tasks). The central
# call_tool dispatcher uses this set to avoid double-logging.
_SELF_LOGGING_TOOLS: frozenset[str] = frozenset({
    # write_tools.py tools — all self-log
    "update_user_style_notes",
    "set_partner_sharing",
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
    "update_scheduled_checkin",
    "cancel_scheduled_task",
    "schedule_partner_checkin",
    "cancel_partner_nudge",
    "escalate_to_partner",
    "edit_outbound_message",
    "delete_outbound_message",
    "react_to_message",
    "explain_media_item",
    "log_feedback",
    "set_topic_status",
    "set_pregnancy_edd",
    "correct_pregnancy_edd",
    "end_pregnancy",
    "create_commitment",
    "update_commitment",
    "close_commitment",
    "log_event",
    # read-shaped but lives in write_tools.py and self-logs
    "list_scheduled_tasks",
})


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
    "get_tool_call",
    "send_message_part",
    "consult_perspective",
    "list_bridge_candidates",
    "list_scheduled_tasks",
    "list_scheduled_checkins",
    "list_all_reminders",
    # hector read tools
    "list_commitments",
    "list_events",
    "get_adherence",
}

WRITE_PHASE_TOOLS = {
    "update_user_style_notes",
    "set_partner_sharing",
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
    "update_scheduled_checkin",
    "cancel_scheduled_task",
    "schedule_partner_checkin",
    "cancel_partner_nudge",
    "escalate_to_partner",
    "edit_outbound_message",
    "delete_outbound_message",
    "react_to_message",
    "explain_media_item",
    "log_feedback",
    "set_topic_status",
}

CONSULT_PHASE_TOOLS = READ_PHASE_TOOLS - {"send_message_part", "consult_perspective"}
RECORD_READ_TOOLS = READ_PHASE_TOOLS - {"send_message_part", "consult_perspective"}
SCHEDULE_TOOLS = {
    "list_scheduled_tasks",
    "list_scheduled_checkins",
    "list_all_reminders",
    "schedule_checkin",
    "cancel_scheduled_checkin",
    "schedule_task",
    "update_scheduled_task",
    "update_scheduled_checkin",
    "cancel_scheduled_task",
    "schedule_partner_checkin",
    "cancel_partner_nudge",
}
RECORD_WRITE_TOOLS = WRITE_PHASE_TOOLS - SCHEDULE_TOOLS | {
    "set_pregnancy_edd",
    "correct_pregnancy_edd",
    "end_pregnancy",
    # hector write tools
    "create_commitment",
    "update_commitment",
    "close_commitment",
    "log_event",
}
# Scheduling tools (schedule_checkin, schedule_task, etc.) are deliberately
# excluded from RESPOND_TOOLS.  Check-in scheduling intent is detected by
# CHECKIN_CONFIRM_RE in pick_default_skeleton and routed to the "standard"
# skeleton which includes a dedicated ``schedule`` step.  Adding scheduling
# to respond would let any skeleton (including quick_reply) schedule inline
# but increases the risk of premature scheduling during lightweight turns.
RESPOND_TOOLS = {"send_message_part", "search_emojis", "check_oob", "log_event"}
READ_TOOLS_FOR_STEP = READ_PHASE_TOOLS - {"send_message_part"}
# Live prep tools: read tools minus outbound/OOB plus the required submit gate.
# No write tools, no outbound, no schedule tools — prep is private and read-only
# except for update_turn_plan (always allowed) and submit_live_brief (required gate).
LIVE_PREP_TOOLS = (
    READ_PHASE_TOOLS
    - {"send_message_part", "summarize_oob_topics", "check_oob"}
    | {"submit_live_brief"}
)
STEP_ALLOWED_TOOLS: dict[TurnStep, set[str]] = {
    "read": READ_TOOLS_FOR_STEP,
    "consult": CONSULT_PHASE_TOOLS | {"consult_perspective"},
    "respond": RESPOND_TOOLS,
    "record": RECORD_WRITE_TOOLS | RECORD_READ_TOOLS,
    "schedule": SCHEDULE_TOOLS,
    "done": set(),
    "live_prep": LIVE_PREP_TOOLS,
    "live_debrief": set(),
}
ALWAYS_ALLOWED_TOOLS = {"update_turn_plan"}

# ── Live debrief flat tool policy ────────────────────────────────────────
# Outbound messaging tools are forbidden during debrief.  The flat policy
# starts from all registered tools, removes the outbound denylist, then is
# filtered through the bot's tool_allowlist and BOT_EXCLUSIVE_TOOLS at
# _step_allowed() time.  submit_live_debrief is the required finalization
# gate; update_turn_plan is always allowed.
LIVE_DEBRIEF_OUTBOUND_DENYLIST: frozenset[str] = frozenset({
    "send_message_part",
    "send_bridge_candidate",
    "escalate_to_partner",
    "edit_outbound_message",
    "delete_outbound_message",
    "react_to_message",
})

# ── Live debrief durable-write safety gate ───────────────────────────────
# Tools whose debrief calls must pass _debrief_write_guard_ok before
# model_validate.  The guard inspects raw_args for evidence_refs and
# derivation_source, validates transcript references against the
# live_debrief_transcript_policy in ctx.extras, and records write intents.
# This set covers every durable-write tool that may be called during a
# live_debrief job: memory, observation, distillation, theme, watch/OOB,
# commitment/event writes, and schedule create/update.
LIVE_DEBRIEF_GUARDED_WRITE_TOOLS: frozenset[str] = frozenset({
    # memory
    "add_memory",
    "update_memory",
    "supersede_memory",
    # observation
    "log_observation",
    "update_observation",
    # distillation
    "add_distillation",
    "update_distillation",
    "revise_distillation",
    # theme
    "create_theme",
    "update_theme",
    # watch items
    "add_watch_item",
    "update_watch_item",
    "address_watch_item",
    # OOB
    "add_oob",
    "update_oob",
    "lift_oob",
    # commitment / event writes
    "create_commitment",
    "update_commitment",
    "close_commitment",
    "log_event",
    # schedule create / update
    "schedule_checkin",
    "schedule_task",
    "update_scheduled_task",
    "update_scheduled_checkin",
})


def build_live_debrief_tools(bot_spec: Any | None = None) -> set[str]:
    """Return the flat set of tool names allowed during a live debrief job.

    The caller passes this set as TurnContext.flat_allowed_tools so both
    schema rendering (to_anthropic_tools) and dispatch (call_tool) see the
    same policy.
    """
    tools: set[str] = set(TOOL_REGISTRY.keys()) - LIVE_DEBRIEF_OUTBOUND_DENYLIST
    tools.add("submit_live_debrief")
    tools.add("update_turn_plan")
    # The caller is responsible for applying bot_spec.tool_allowlist and
    # BOT_EXCLUSIVE_TOOLS (which _step_allowed handles).  We do an early
    # intersection here so the flat set doesn't include tools the bot
    # can never use, but the authoritative intersection happens in
    # _step_allowed().
    if bot_spec is not None and bot_spec.tool_allowlist is not None:
        tools &= bot_spec.tool_allowlist | ALWAYS_ALLOWED_TOOLS
    return tools


def _debrief_write_guard_ok(
    ctx: TurnContext, tool_name: str, raw_args: dict[str, Any]
) -> dict[str, Any] | None:
    """Validate that a debrief durable write does not cite unshareable transcript data.

    Called from ``call_tool`` before Pydantic ``model_validate`` so that
    guard-only fields (evidence_refs, derivation_source) are available in
    raw_args even when the target schema lacks them.

    Returns ``None`` when the write is allowed.  Returns a ``_tool_error``
    dict when it should be rejected.
    """
    # Only active during live_debrief steps AND for guarded write tools.
    if ctx.current_step != "live_debrief":
        return None
    if tool_name not in LIVE_DEBRIEF_GUARDED_WRITE_TOOLS:
        return None

    # ── Extract guard-only fields from raw_args ─────────────────────────
    evidence_refs: list[dict[str, Any]] = raw_args.get("evidence_refs") or []
    derivation_source: str | None = raw_args.get("derivation_source")

    # ── Load the transcript policy from ctx.extras ──────────────────────
    transcript_policy: dict[str, Any] = ctx.extras.get(
        "live_debrief_transcript_policy", {}
    )
    # shareable_turn_ids maps turn_id_str -> {text_hash, quote_hashes}
    shareable_turns: dict[str, dict[str, Any]] = transcript_policy.get(
        "shareable_turn_ids", {}
    )
    redacted_turns: set[str] = set(transcript_policy.get("redacted_turn_ids", []))

    # ── Validate evidence references ────────────────────────────────────
    # Track whether we found at least one valid evidence reference with a
    # non-empty transcript_turn_id.  If evidence_refs is present but every
    # item has an empty/missing turn_id, we must still require a valid
    # derivation_source (the elif below would not fire because the ``if``
    # branch was entered, so we need an explicit tracker).
    found_valid_ref = False
    if evidence_refs:
        for ref in evidence_refs:
            turn_id = str(ref.get("transcript_turn_id", ""))
            if not turn_id:
                continue
            # Reject references to redacted turns
            if turn_id in redacted_turns:
                _record_debrief_write_intent(
                    ctx, tool_name, raw_args,
                    outcome="rejected",
                    reason="debrief_unshareable_transcript_reference",
                    evidence_refs=evidence_refs,
                )
                return _tool_error(
                    f"debrief_unshareable_transcript_reference: "
                    f"turn {turn_id} is redacted or not shareable",
                    error_code="debrief_unshareable_transcript_reference",
                    retryable=False,
                )
            # Validate the turn is in the shareable set
            if turn_id not in shareable_turns:
                _record_debrief_write_intent(
                    ctx, tool_name, raw_args,
                    outcome="rejected",
                    reason="debrief_unknown_transcript_reference",
                    evidence_refs=evidence_refs,
                )
                return _tool_error(
                    f"debrief_unknown_transcript_reference: "
                    f"turn {turn_id} is not in the transcript policy",
                    error_code="debrief_unknown_transcript_reference",
                    retryable=False,
                )
            # Quote-matching: compare provided quote against stored hashes
            quote = str(ref.get("quote", "")).strip()
            if quote:
                turn_info = shareable_turns.get(turn_id, {})
                quote_hashes: set[str] = set(turn_info.get("quote_hashes", []))
                text_hash: str | None = turn_info.get("text_hash")
                if quote_hashes:
                    import hashlib as _hashlib
                    provided_hash = _hashlib.sha256(quote.encode()).hexdigest()
                    if provided_hash not in quote_hashes and text_hash and provided_hash != text_hash:
                        _record_debrief_write_intent(
                            ctx, tool_name, raw_args,
                            outcome="rejected",
                            reason="debrief_quote_mismatch",
                            evidence_refs=evidence_refs,
                        )
                        return _tool_error(
                            f"debrief_quote_mismatch: quote does not match "
                            f"stored transcript text for turn {turn_id}",
                            error_code="debrief_quote_mismatch",
                            retryable=False,
                        )
            found_valid_ref = True

    if not found_valid_ref and derivation_source not in (
        "hot_context", "bot_notes", "prep_artifact"
    ):
        # For writes without valid transcript references, require an explicit
        # derivation_source marking it as derived from hot context / bot
        # notes / prep artifact.
        _record_debrief_write_intent(
            ctx, tool_name, raw_args,
            outcome="rejected",
            reason="debrief_missing_derivation_source",
        )
        return _tool_error(
            "debrief_missing_derivation_source: durable write must carry "
            "evidence_refs with transcript_turn_id or derivation_source "
            "in ('hot_context', 'bot_notes', 'prep_artifact')",
            error_code="debrief_missing_derivation_source",
            retryable=False,
        )

    # ── Guard passed ────────────────────────────────────────────────────
    _record_debrief_write_intent(
        ctx, tool_name, raw_args,
        outcome="allowed",
        evidence_refs=evidence_refs,
        derivation_source=derivation_source,
    )
    return None


def _record_debrief_write_intent(
    ctx: TurnContext,
    tool_name: str,
    raw_args: dict[str, Any],
    *,
    outcome: str,
    reason: str | None = None,
    evidence_refs: list[dict[str, Any]] | None = None,
    derivation_source: str | None = None,
) -> None:
    """Record a debrief write intent for later audit."""
    import hashlib as _hashlib
    import json as _json
    text_hash = _hashlib.sha256(
        _json.dumps(raw_args, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]

    intent: dict[str, Any] = {
        "tool_name": tool_name,
        "text_hash": text_hash,
        "outcome": outcome,
    }
    if evidence_refs:
        intent["evidence_refs"] = evidence_refs
    if derivation_source:
        intent["derivation_source"] = derivation_source
    if reason:
        intent["reason"] = reason

    write_intents: list[dict[str, Any]] = ctx.extras.setdefault(
        "live_debrief_write_intents", []
    )
    write_intents.append(intent)


READ_BEFORE_WRITE: dict[str, set[str]] = {
    "add_memory": {"get_memories"},
    "update_memory": {"get_memories"},
    "supersede_memory": {"get_memories"},
    "log_observation": {"get_observations"},
    "update_observation": {"get_observations"},
    "add_distillation": {"get_distillations"},
    "update_distillation": {"get_distillations"},
    "revise_distillation": {"get_distillations"},
    "create_theme": {"list_themes", "get_theme"},
    "update_theme": {"list_themes", "get_theme"},
    "add_oob": {"get_oob", "summarize_oob_topics"},
    "update_oob": {"get_oob", "summarize_oob_topics"},
    "lift_oob": {"get_oob", "summarize_oob_topics"},
    "add_watch_item": {"list_watch_items"},
    "update_watch_item": {"list_watch_items"},
    "address_watch_item": {"list_watch_items"},
    # hector: require list_commitments before creating/updating/closing
    "create_commitment": {"list_commitments"},
    "update_commitment": {"list_commitments"},
    "close_commitment": {"list_commitments"},
}

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


def _step_allowed(ctx: TurnContext) -> set[str]:
    # When flat_allowed_tools is set (non-chat jobs like live_debrief),
    # it is authoritative instead of STEP_ALLOWED_TOOLS.  The flat set
    # still passes through bot_spec.tool_allowlist and BOT_EXCLUSIVE_TOOLS
    # so scope and exclusivity guards remain in effect.
    if ctx.flat_allowed_tools is not None:
        allowed = set(ctx.flat_allowed_tools) | ALWAYS_ALLOWED_TOOLS
    else:
        allowed = (
            set(STEP_ALLOWED_TOOLS.get(ctx.current_step, set())) | ALWAYS_ALLOWED_TOOLS
        )
    if ctx.bot_spec is not None and ctx.bot_spec.tool_allowlist is not None:
        allowed &= ctx.bot_spec.tool_allowlist | ALWAYS_ALLOWED_TOOLS
    # Remove bot-exclusive tools for non-matching bots.
    # This ensures bots outside the keyed bot-id set never see commitment/
    # event tools even when their allowlist is broad or None. Handler-level
    # scope guards in read_tools/write_tools are the backstop.
    for exclusive_bot_ids, exclusive_tools in BOT_EXCLUSIVE_TOOLS.items():
        if getattr(ctx, 'bot_id', None) not in exclusive_bot_ids:
            allowed -= exclusive_tools
    return allowed


def _inject_consult_defaults(
    name: str, raw_args: dict[str, Any], ctx: TurnContext
) -> dict[str, Any]:
    if (
        ctx.current_step != "consult"
        or name not in _CONSULT_OWNER_INJECTING_TOOLS
        or not ctx.protected_owner_ids
    ):
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


def _tool_error(
    message: str,
    *,
    error_code: str | None = None,
    field: str | None = None,
    retryable: bool | None = None,
    failure_class: str | None = None,
    correction_hint: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {"error": message, "is_error": True}
    if error_code is not None:
        result["error_code"] = error_code
    if field is not None:
        result["field"] = field
    if retryable is not None:
        result["retryable"] = retryable
    if failure_class is not None:
        result["failure_class"] = failure_class
    if correction_hint is not None:
        result["correction_hint"] = correction_hint
    return result


def _record_visible_tool_call(
    *,
    tool_name: str,
    args: Any,
    result: dict[str, Any],
    phase: str,
    started_at: datetime,
) -> None:
    if tool_name == "update_turn_plan":
        return
    record_tool_call(
        tool_name=tool_name,
        args=args,
        result=result,
        phase=phase,
        started_at=started_at,
    )


async def call_tool(
    name: str, raw_args: dict[str, Any], ctx: TurnContext
) -> dict[str, Any]:
    started = datetime.now(UTC)
    phase = ctx.current_step
    await record_turn_event(
        ctx.pool,
        ctx.turn_id,
        "tool.requested",
        step=phase,
        actor="tool",
        metadata={"tool_name": name},
    )
    registry_entry = TOOL_REGISTRY.get(name)
    if registry_entry is None:
        result = _tool_error(f"unknown tool: {name}")
        await record_turn_event(
            ctx.pool,
            ctx.turn_id,
            "tool.rejected",
            step=phase,
            severity="warning",
            actor="tool",
            metadata={"tool_name": name, "reason": "unknown_tool"},
        )
        _record_visible_tool_call(
            tool_name=name,
            args=raw_args,
            result=result,
            phase=phase,
            started_at=started,
        )
        return result
    if name not in _step_allowed(ctx):
        result = _tool_error(
            f"step: tool {name} is not allowed in {ctx.current_step} step"
        )
        await record_turn_event(
            ctx.pool,
            ctx.turn_id,
            "tool.rejected",
            step=phase,
            severity="warning",
            actor="tool",
            metadata={"tool_name": name, "reason": "step_not_allowed"},
        )
        _record_visible_tool_call(
            tool_name=name,
            args=raw_args,
            result=result,
            phase=phase,
            started_at=started,
        )
        return result
    if name == "consult_perspective" and ctx.trigger_metadata.get("_inside_consult"):
        result = _tool_error("step: consult_perspective cannot call itself")
        await record_turn_event(
            ctx.pool,
            ctx.turn_id,
            "tool.rejected",
            step=phase,
            severity="warning",
            actor="tool",
            metadata={"tool_name": name, "reason": "recursive_consult"},
        )
        _record_visible_tool_call(
            tool_name=name,
            args=raw_args,
            result=result,
            phase=phase,
            started_at=started,
        )
        return result
    input_model, output_model = registry_entry

    # ── Debrief durable-write safety gate ────────────────────────────────
    # Inspect raw_args BEFORE Pydantic model_validate so evidence_refs and
    # derivation_source survive even when the target schema lacks those fields.
    if ctx.current_step == "live_debrief":
        guard_error = _debrief_write_guard_ok(ctx, name, raw_args)
        if guard_error is not None:
            await record_turn_event(
                ctx.pool,
                ctx.turn_id,
                "tool.rejected",
                step=phase,
                severity="warning",
                actor="tool",
                metadata={
                    "tool_name": name,
                    "reason": guard_error.get("error_code", "debrief_guard_rejected"),
                },
            )
            _record_visible_tool_call(
                tool_name=name,
                args=raw_args,
                result=guard_error,
                phase=phase,
                started_at=started,
            )
            return guard_error
        # Strip guard-only fields from raw_args before model_validate so
        # Pydantic doesn't reject them on schemas without ConfigDict(extra='allow').
        raw_args = {
            k: v
            for k, v in raw_args.items()
            if k not in ("evidence_refs", "derivation_source")
        }

    try:
        args = input_model.model_validate(_inject_consult_defaults(name, raw_args, ctx))
    except ValidationError as exc:
        result = _tool_error(f"validation: {exc}")
        await record_turn_event(
            ctx.pool,
            ctx.turn_id,
            "tool.rejected",
            step=phase,
            severity="warning",
            actor="tool",
            metadata={"tool_name": name, "reason": "validation"},
        )
        _record_visible_tool_call(
            tool_name=name,
            args=raw_args,
            result=result,
            phase=phase,
            started_at=started,
        )
        return result
    required_reads = READ_BEFORE_WRITE.get(name)
    if required_reads and not (required_reads & set(ctx.tool_call_log)):
        required = " or ".join(sorted(required_reads))
        result = _tool_error(f"read_before_write: call {required} before {name}")
        await record_turn_event(
            ctx.pool,
            ctx.turn_id,
            "tool.rejected",
            step=phase,
            severity="warning",
            actor="tool",
            metadata={
                "tool_name": name,
                "reason": "read_before_write",
                "required_reads": sorted(required_reads),
            },
        )
        _record_visible_tool_call(
            tool_name=name,
            args=args.model_dump(mode="json"),
            result=result,
            phase=phase,
            started_at=started,
        )
        return result
    fn = TOOL_DISPATCH.get(name)
    if fn is None:
        result = _tool_error(f"dispatch: tool {name} is not implemented")
        await record_turn_event(
            ctx.pool,
            ctx.turn_id,
            "tool.rejected",
            step=phase,
            severity="warning",
            actor="tool",
            metadata={"tool_name": name, "reason": "dispatch_missing"},
        )
        _record_visible_tool_call(
            tool_name=name,
            args=args.model_dump(mode="json"),
            result=result,
            phase=phase,
            started_at=started,
        )
        return result
    try:
        result = await fn(ctx, args)
    except ToolCallRejected as exc:
        result = {**exc.result, "is_error": True}
        _rejection_meta: dict[str, Any] = {
            "tool_name": name,
            "reason": result.get("error") or "tool_call_rejected",
        }
        for _key in ("error_code", "field", "retryable", "failure_class", "correction_hint"):
            if _key in result:
                _rejection_meta[_key] = result[_key]
        await record_turn_event(
            ctx.pool,
            ctx.turn_id,
            "tool.rejected",
            step=phase,
            severity="warning",
            actor="tool",
            duration_ms=max(
                0, int((datetime.now(UTC) - started).total_seconds() * 1000)
            ),
            metadata=_rejection_meta,
        )
        _record_visible_tool_call(
            tool_name=name,
            args=args.model_dump(mode="json"),
            result=result,
            phase=phase,
            started_at=started,
        )
        return result
    except Exception as exc:
        result = _tool_error(f"exception: {exc}")
        await record_turn_event(
            ctx.pool,
            ctx.turn_id,
            "tool.failed",
            step=phase,
            severity="error",
            actor="tool",
            duration_ms=max(
                0, int((datetime.now(UTC) - started).total_seconds() * 1000)
            ),
            metadata={"tool_name": name, "exception_type": type(exc).__name__},
        )
        _record_visible_tool_call(
            tool_name=name,
            args=args.model_dump(mode="json"),
            result=result,
            phase=phase,
            started_at=started,
        )
        raise
    try:
        validated = output_model.model_validate(result)
    except ValidationError as exc:
        result = _tool_error(f"result_validation: {exc}")
        await record_turn_event(
            ctx.pool,
            ctx.turn_id,
            "tool.rejected",
            step=phase,
            severity="warning",
            actor="tool",
            duration_ms=max(
                0, int((datetime.now(UTC) - started).total_seconds() * 1000)
            ),
            metadata={"tool_name": name, "reason": "result_validation"},
        )
        _record_visible_tool_call(
            tool_name=name,
            args=args.model_dump(mode="json"),
            result=result,
            phase=phase,
            started_at=started,
        )
        return result
    result_dict = validated.model_dump(mode="json")
    await record_turn_event(
        ctx.pool,
        ctx.turn_id,
        "plan.updated" if name == "update_turn_plan" else "tool.completed",
        step=phase,
        actor="tool",
        duration_ms=max(0, int((datetime.now(UTC) - started).total_seconds() * 1000)),
        metadata={
            "tool_name": name,
            "is_error": bool(result_dict.get("is_error") or result_dict.get("error")),
        },
    )
    if name != "update_turn_plan":
        ctx.tool_call_log.append(name)
        _record_visible_tool_call(
            tool_name=name,
            args=args.model_dump(mode="json"),
            result=result_dict,
            phase=phase,
            started_at=started,
        )
        # Persist read-tool calls to mediator.tool_calls so the agent can
        # introspect its own past decisions. Write tools self-log inside
        # their handlers (tracked in _SELF_LOGGING_TOOLS).
        if name not in _SELF_LOGGING_TOOLS:
            try:
                await _log_tool_call_audit(
                    ctx, name, args, started, result_dict, kind="read"
                )
            except Exception:
                # Audit logging must never break tool execution.
                pass
    return result_dict
