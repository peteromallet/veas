"""Solo 'hector' bot profile (fitness topic).

Phase 1: BotSpec wired with tool allowlist + ReadScopes per the
sprint brief.  The prompt renderer delegates to a stub — real persona
content lands in T12 (app/bots/prompts/hector.py).

Registered lazily in _maybe_register_staging_bots (STAGING=1 gate), mirroring
the Tante Rosi registration pattern.  Prod path (bots-table row-existence gate)
lands in T4.

TODO(FLAG-001): Channel/binding seeding is deferred to operator.
Hector may be unreachable via real transport until an operator
creates channels and bindings rows.
"""

from __future__ import annotations

from app.bots.base import BotSpec, ReadScopes, WriteScopes
from app.bots.ids import HECTOR_BOT_ID
from app.bots.prompts.hector import render_system_prompt as _persona_render


def _hector_prompt_renderer(
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
    """Hector prompt renderer — delegates to the persona module.

    Accepts partner_name, partner_partner_share, and partner (via
    **kwargs) from BotSpec.render_system_prompt but ignores them.  The
    solo renderer has no dyadic concepts.
    """
    return _persona_render(
        assistant_name=assistant_name,
        user_name=user_name,
        prompt_version=prompt_version,
        onboarding_state=onboarding_state,
        partner_share=current_user_partner_share,
        partner_sharing_state=current_user_partner_sharing_state,
    )


HECTOR_READ_INSTRUCTION = (
    "Read step: gather only the context needed for this fitness turn. "
    "For concrete plans and adherence, use the fitness board tools such as "
    "`list_commitments`, `get_adherence`, or `list_events` when the hot "
    "context is not enough. For durable routine constraints, equipment, "
    "timing, recurring blockers, or tactics that might need recording later, "
    "read existing `get_memories` and/or `get_observations` first. Do not "
    "repeat a read tool that already returned enough context or an empty "
    "result; move on to the response instead. Do not write durable state or "
    "send user-facing text from this step."
)

HECTOR_CONSULT_INSTRUCTION = (
    "Consult step: use only if an outside perspective would materially improve "
    "a tricky fitness reply. Most Hector turns should skip this step. Do not "
    "write durable state or send user-facing text from this step."
)

HECTOR_RESPOND_INSTRUCTION = (
    "Respond step: send the user-facing reply, reaction, or silence. Keep it "
    "plain, specific, and low-key. On transports that support message parts, "
    "use `send_message_part` only when separate chat bubbles make the reply "
    "clearer. If the user reports a completed, missed, or excused fitness slot "
    "and the relevant commitment is already known well enough, `log_event` may "
    "be used here; otherwise leave durable updates for the record step. Never "
    "mention internal phases, memory tools, or storage mechanics to the user."
)

HECTOR_RECORD_INSTRUCTION = (
    "Record step: maintain durable fitness state after the reply. Use memories "
    "for stable concrete facts, constraints, preferences, equipment, schedule, "
    "and support setup. Use observations for recurring patterns, blockers, and "
    "tactics that seem to help or fail. Use commitments only for explicit "
    "concrete plans the user agreed to track. Use events for completed, missed, "
    "or excused adherence reports. Read before durable writes, prefer updating "
    "or reinforcing an existing row over creating a duplicate, and skip writes "
    "when there is no useful future context to preserve. "
    "Never invent IDs — every commitment_id must come from a previous tool "
    "result. Call list_commitments before linking to an existing commitment. "
    "When the user accepts a concrete new plan, call create_commitment. "
    "Use log_event only for actual completed, missed, or excused events "
    "against a real returned commitment_id. Do not send "
    "user-facing text from this step."
)

HECTOR_SCHEDULE_INSTRUCTION = (
    "Schedule step: create a follow-up or scheduled check-in only when it would "
    "clearly help the user's fitness plan survive the week. Read existing "
    "tasks or check-ins first when duplication is plausible. Do not send "
    "user-facing text from this step."
)

HECTOR_DONE_INSTRUCTION = (
    "Done step: stop. Do not call tools or add extra text."
)

HECTOR_STEP_INSTRUCTIONS = {
    "read": HECTOR_READ_INSTRUCTION,
    "consult": HECTOR_CONSULT_INSTRUCTION,
    "respond": HECTOR_RESPOND_INSTRUCTION,
    "record": HECTOR_RECORD_INSTRUCTION,
    "schedule": HECTOR_SCHEDULE_INSTRUCTION,
    "done": HECTOR_DONE_INSTRUCTION,
}

# ── Tool allowlist ─────────────────────────────────────────────────────────
# Exclusions: matching tante_rosi's exclusions (set_topic_status, bridge
# candidates, escalate_to_partner, search_messages, recent_activity) PLUS
# pregnancy tools (set_pregnancy_edd, correct_pregnancy_edd, end_pregnancy)
# since Hector is fitness not pregnancy.  The 7 commitment/event tools are
# added back via _HECTOR_ADDITIONS.

_COACH_EXCLUSIONS = frozenset({
    "set_topic_status",
    # Bridge/escalate (dyad-only):
    "create_bridge_candidate",
    "update_bridge_candidate",
    "send_bridge_candidate",
    "list_bridge_candidates",
    "escalate_to_partner",
    # Dyad-only read tools:
    "recent_activity",
    # Pregnancy tools (Tante Rosi only):
    "set_pregnancy_edd",
    "correct_pregnancy_edd",
    "end_pregnancy",
})

_HECTOR_ADDITIONS = frozenset({
    "list_commitments",
    "create_commitment",
    "update_commitment",
    "close_commitment",
    "log_event",
    "get_adherence",
    "list_events",
})


def build_hector_spec() -> BotSpec:
    """Build the Hector BotSpec.

    Lazy import of TOOL_DISPATCH avoids a circular import (registry imports
    write_tools, which doesn't import hector).  The tool_allowlist is the
    full dispatch table minus dyad-only/bridge/pregnancy tools plus the seven
    commitment/event tools.  Hector does NOT subtract HECTOR_ONLY_TOOLS —
    only other bots do.
    """
    from app.services.tools.registry import TOOL_DISPATCH  # noqa: PLC0415

    return BotSpec(
        bot_id=HECTOR_BOT_ID,
        prompt_renderer=_hector_prompt_renderer,
        step_instructions=HECTOR_STEP_INSTRUCTIONS,
        display_name="Hector",
        primary_topic_slug="fitness",
        participants_shape="solo",
        read_scopes=ReadScopes(
            topics=frozenset({"own"}),
            allow_cross_topic_peek=True,
            allow_cross_topic_status_injection=False,
        ),
        write_scopes=WriteScopes(topics=frozenset({"own"})),
        cross_topic_policy="peek",
        tool_allowlist=(
            frozenset(TOOL_DISPATCH.keys()) - _COACH_EXCLUSIONS
        ) | _HECTOR_ADDITIONS,
        bot_spec_version="1.0.0",
    )
