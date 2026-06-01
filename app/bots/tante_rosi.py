"""Solo 'tante_rosi' bot profile (pregnancy topic).

Phase 1 placeholder: BotSpec wired with tool allowlist + ReadScopes per the
sprint brief §2.1.  The prompt renderer delegates to the phase-1 placeholder
in app.bots.prompts.tante_rosi — real persona content lands in Phase 2.

Registered lazily in _maybe_register_staging_bots (STAGING=1 gate), mirroring
the coach registration pattern.  Prod path (bots-table row-existence gate)
lands in T11.
"""

from __future__ import annotations

from app.bots.base import BotSpec, ReadScopes, WriteScopes
from app.bots.ids import TANTE_ROSI_BOT_ID
from app.bots.prompts.tante_rosi import render_system_prompt as _persona_render


def _tante_rosi_prompt_renderer(
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
    """Tante Rosi prompt renderer — delegates to the persona module.

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


TANTE_ROSI_READ_INSTRUCTION = (
    "Read step: gather only the pregnancy context needed for this turn. "
    "Use hot context first: gestational age, EDD, recent loss/birth state, "
    "open asks, memories, observations, and OOB items. If a durable fact, "
    "pattern, support need, or state change may need recording later, read "
    "existing `get_memories` and/or `get_observations` first. Do not write "
    "durable state or send user-facing text from this step."
)

TANTE_ROSI_CONSULT_INSTRUCTION = (
    "Consult step: use only if an outside perspective would materially improve "
    "a sensitive pregnancy reply. Most Tante Rosi turns should skip this step. "
    "Do not write durable state or send user-facing text from this step."
)

TANTE_ROSI_RESPOND_INSTRUCTION = (
    "Respond step: send the user-facing reply, reaction, or silence. Keep it "
    "plain, warm, careful, and language-matched according to the prompt. For "
    "symptoms, clinical questions, red flags, loss, or high-risk situations, "
    "follow the medical and safety rules exactly. Never mention internal "
    "phases, memory tools, or storage mechanics to the user."
)

TANTE_ROSI_RECORD_INSTRUCTION = (
    "Record step: maintain durable pregnancy state after the reply. Use "
    "`set_pregnancy_edd` only when the user explicitly gives a confirmed due "
    "date or enough dating information; use `correct_pregnancy_edd` for a "
    "scan-corrected or otherwise revised EDD; use `end_pregnancy` when the "
    "user explicitly says the pregnancy ended by birth, loss, or termination. "
    "Use memories for stable pregnancy facts, appointment logistics, support "
    "setup, preferences, and practical constraints. Use observations for "
    "recurring emotional patterns, worries, coping tactics, or support needs. "
    "Read before durable writes, prefer updating or reinforcing an existing "
    "row over creating a duplicate, and skip writes when there is no useful "
    "future context to preserve. Do not send user-facing text from this step."
)

TANTE_ROSI_SCHEDULE_INSTRUCTION = (
    "Schedule step: create a follow-up or scheduled check-in only when it "
    "would clearly help the pregnancy support relationship, such as after an "
    "appointment, scan, difficult symptom conversation, unresolved open ask, "
    "or explicitly requested reminder. Read existing tasks or check-ins first "
    "when duplication is plausible. Do not send user-facing text from this step."
)

TANTE_ROSI_DONE_INSTRUCTION = (
    "Done step: stop. Do not call tools or add extra text."
)

TANTE_ROSI_STEP_INSTRUCTIONS = {
    "read": TANTE_ROSI_READ_INSTRUCTION,
    "consult": TANTE_ROSI_CONSULT_INSTRUCTION,
    "respond": TANTE_ROSI_RESPOND_INSTRUCTION,
    "record": TANTE_ROSI_RECORD_INSTRUCTION,
    "schedule": TANTE_ROSI_SCHEDULE_INSTRUCTION,
    "done": TANTE_ROSI_DONE_INSTRUCTION,
}

# ── Tool allowlist ─────────────────────────────────────────────────────────
# §4.1 no-auto-bridging: the bridge/escalate exclusions below are load-bearing.
# Tante Rosi MUST NOT be able to auto-bridge pregnancy content to the mediator.
_COACH_EXCLUSIONS = frozenset(
    {
        "set_topic_status",
        # Bridge/escalate (load-bearing for §4.1 no-auto-bridging):
        "create_bridge_candidate",
        "update_bridge_candidate",
        "send_bridge_candidate",
        "list_bridge_candidates",
        "escalate_to_partner",
        # Dyad-only read tools:
        "recent_activity",
    }
)

_TANTE_ROSI_ADDITIONS = frozenset(
    {
        "set_pregnancy_edd",
        "correct_pregnancy_edd",
        "end_pregnancy",
    }
)


def build_tante_rosi_spec() -> BotSpec:
    """Build the Tante Rosi BotSpec.

    Lazy import of TOOL_DISPATCH avoids a circular import (registry imports
    write_tools, which doesn't import tante_rosi).  The tool_allowlist is the
    full dispatch table minus dyad-only/bridge tools plus the three pregnancy
    write tools.
    """
    from app.services.tools.registry import TOOL_DISPATCH, HECTOR_ONLY_TOOLS

    return BotSpec(
        bot_id=TANTE_ROSI_BOT_ID,
        prompt_renderer=_tante_rosi_prompt_renderer,
        step_instructions=TANTE_ROSI_STEP_INSTRUCTIONS,
        display_name="Tante Rosi",
        primary_topic_slug="pregnancy",
        participants_shape="solo",
        read_scopes=ReadScopes(
            topics=frozenset({"own"}),
            allow_cross_topic_peek=True,
            allow_cross_topic_status_injection=False,
        ),
        write_scopes=WriteScopes(topics=frozenset({"own"})),
        cross_topic_policy="peek",
        tool_allowlist=(
            frozenset(TOOL_DISPATCH.keys()) - _COACH_EXCLUSIONS - HECTOR_ONLY_TOOLS
        )
        | _TANTE_ROSI_ADDITIONS,
        bot_spec_version="1.0.0",
    )
