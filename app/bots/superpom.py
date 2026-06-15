"""Solo 'superpom' bot profile (superpom topic).

M2 — BotSpec wired with tool allowlist + ReadScopes. Compass-enabled for
orientation-first reading. The prompt renderer delegates to the persona
module (app/bots/prompts/superpom.py).

Registered lazily in _maybe_register_staging_bots (STAGING=1 gate) and via
populate_superpom_spec_from_db when the bots-table row is present.
"""

from __future__ import annotations

from app.bots.base import BotSpec, ReadScopes, WriteScopes
from app.bots.ids import SUPERPOM_BOT_ID
from app.bots.prompts.superpom import render_system_prompt as _persona_render


def _superpom_prompt_renderer(
    assistant_name: str,
    user_name: str,
    partner_name: str | None = None,
    *,
    prompt_version: str = "v1",
    onboarding_state: str | None = None,
    current_user_partner_share: str | None = None,
    partner_partner_share: str | None = None,
    current_user_partner_sharing_state: str | None = None,
    partner_partner_sharing_state: str | None = None,
    **kwargs: object,
) -> str:
    """SuperPOM prompt renderer — delegates to the persona module.

    Accepts partner_name and partner_* via **kwargs from
    BotSpec.render_system_prompt but ignores them — the solo renderer has
    no dyadic concepts.
    """
    return _persona_render(
        assistant_name=assistant_name,
        user_name=user_name,
        prompt_version=prompt_version,
        onboarding_state=onboarding_state,
        partner_share=current_user_partner_share,
        partner_sharing_state=current_user_partner_sharing_state,
    )


SUPERPOM_READ_INSTRUCTION = (
    "Read step: start with the Compass — call `list_orientation_items` to "
    "load the user's principles, goals, priorities, and anti-patterns for "
    "this topic before reading anything else. The Compass is your primary "
    "orientation source and must be consulted first every turn. After the "
    "Compass, gather only the context needed for this SuperPOM turn from "
    "memory, observations, and the hot context. For durable routine "
    "constraints, timing, recurring blockers, or tactics that might need "
    "recording later, read existing `get_memories` and/or `get_observations` "
    "first. Do not repeat a read tool that already returned enough context "
    "or an empty result; move on to the response instead. Do not write "
    "durable state or send user-facing text from this step."
)

SUPERPOM_CONSULT_INSTRUCTION = (
    "Consult step: use only if an outside perspective would materially "
    "improve a tricky SuperPOM reply. Most SuperPOM turns should skip this "
    "step. Do not write durable state or send user-facing text from this "
    "step."
)

SUPERPOM_RESPOND_INSTRUCTION = (
    "Respond step: send the user-facing reply, reaction, or silence. Keep "
    "it plain, specific, and low-key. On transports that support message "
    "parts, use `send_message_part` only when separate chat bubbles make "
    "the reply clearer. Never mention internal phases, memory tools, or "
    "storage mechanics to the user. When the user states or confirms a "
    "principle, goal, priority, or anti-pattern, you may propose an "
    "orientation item via `create_orientation_item` with "
    "source='user_stated' or source='user_confirmed' so it becomes "
    "immediately Compass-visible. When you infer a candidate heading, use "
    "source='bot_proposed' to queue it for user review — bot_proposed "
    "items are hidden from Compass until explicitly reviewed via "
    "`review_orientation_item`."
)

SUPERPOM_RECORD_INSTRUCTION = (
    "Record step: maintain durable SuperPOM state after the reply. Use "
    "memories for stable concrete facts, constraints, preferences, "
    "schedule, and support setup. Use observations for recurring "
    "patterns, blockers, and tactics that seem to help or fail. Use "
    "orientation items for principles, goals, priorities, and "
    "anti-patterns — these are your Compass headings, distinct from "
    "memory facts, observation patterns, and distillation explanations. "
    "When creating orientation items: use source='user_stated' for "
    "headings the user directly stated (immediately Compass-visible), "
    "source='bot_proposed' for inferred headings that need user review "
    "(hidden from Compass until reviewed). Read before durable writes, "
    "prefer updating or reinforcing an existing row over creating a "
    "duplicate, and skip writes when there is no useful future context "
    "to preserve. Do not send user-facing text from this step."
)

SUPERPOM_SCHEDULE_INSTRUCTION = (
    "Schedule step: create a follow-up or scheduled check-in only when it "
    "would clearly help the user's reflection practice survive the week. "
    "Read existing tasks or check-ins first when duplication is plausible. "
    "Do not send user-facing text from this step."
)

SUPERPOM_DONE_INSTRUCTION = (
    "Done step: stop. Do not call tools or add extra text."
)

SUPERPOM_STEP_INSTRUCTIONS = {
    "read": SUPERPOM_READ_INSTRUCTION,
    "consult": SUPERPOM_CONSULT_INSTRUCTION,
    "respond": SUPERPOM_RESPOND_INSTRUCTION,
    "record": SUPERPOM_RECORD_INSTRUCTION,
    "schedule": SUPERPOM_SCHEDULE_INSTRUCTION,
    "done": SUPERPOM_DONE_INSTRUCTION,
}

# ── Tool allowlist ─────────────────────────────────────────────────────────
# SuperPOM is an orientation/Compass-review bot.  The allowlist starts from
# the full TOOL_DISPATCH table and removes tools that belong to other domains:
#
#   dyad-only / bridge:    create_bridge_candidate, update_bridge_candidate,
#                          send_bridge_candidate, list_bridge_candidates,
#                          escalate_to_partner, recent_activity
#   pregnancy:             set_pregnancy_edd, correct_pregnancy_edd,
#                          end_pregnancy
#   commitment / event:    list_commitments, create_commitment,
#                          update_commitment, close_commitment, log_event,
#                          get_adherence, list_events
#   live-plan (mediator):  read_conversation_plan, list_conversation_plans,
#                          create_conversation_plan, update_conversation_plan
#   coach-only:            set_topic_status
#
# Orientation tools (list_orientation_items, get_orientation_item,
# create_orientation_item, update_orientation_item, review_orientation_item,
# close_orientation_item, link_orientation_evidence) are KEPT — they are
# SuperPOM's core tool surface.

_SUPERPOM_EXCLUSIONS = frozenset({
    # dyad / bridge
    "create_bridge_candidate",
    "update_bridge_candidate",
    "send_bridge_candidate",
    "list_bridge_candidates",
    "escalate_to_partner",
    "recent_activity",
    # pregnancy
    "set_pregnancy_edd",
    "correct_pregnancy_edd",
    "end_pregnancy",
    # commitment / event (Hector + Habits domain)
    "list_commitments",
    "create_commitment",
    "update_commitment",
    "close_commitment",
    "log_event",
    "get_adherence",
    "list_events",
    # live-plan (mediator-only)
    "read_conversation_plan",
    "list_conversation_plans",
    "create_conversation_plan",
    "update_conversation_plan",
    # coach-only
    "set_topic_status",
})


def build_superpom_spec() -> BotSpec:
    """Build the SuperPOM BotSpec.

    Lazy import of TOOL_DISPATCH avoids a circular import (registry imports
    write_tools, which doesn't import superpom).  The tool_allowlist is the
    full dispatch table minus tools that belong to other bot domains (dyad,
    pregnancy, commitment/event, live-plan, coach topic-status).
    Orientation tools are kept — they are SuperPOM's core surface.
    """
    from app.services.tools.registry import TOOL_DISPATCH  # noqa: PLC0415

    return BotSpec(
        bot_id=SUPERPOM_BOT_ID,
        prompt_renderer=_superpom_prompt_renderer,
        step_instructions=SUPERPOM_STEP_INSTRUCTIONS,
        display_name="SuperPOM",
        primary_topic_slug="superpom",
        participants_shape="solo",
        read_scopes=ReadScopes(
            topics=frozenset({"own"}),
            allow_cross_topic_peek=True,
            allow_cross_topic_status_injection=False,
        ),
        write_scopes=WriteScopes(topics=frozenset({"own"})),
        cross_topic_policy="peek",
        compass_enabled=True,
        tool_allowlist=(
            frozenset(TOOL_DISPATCH.keys()) - _SUPERPOM_EXCLUSIONS
        ),
        bot_spec_version="1.0.0",
    )
