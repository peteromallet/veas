"""Solo 'habits' bot profile (habits topic).

Mirrors hector.py — same solo shape, same commitment/event tool surface,
same scope structure. The habits bot owns the `habits` topic and shares
the commitment/event substrate with Hector via the multi-bot
BOT_EXCLUSIVE_TOOLS key in app/services/tools/registry.py.

Registered lazily in _maybe_register_staging_bots (STAGING=1 gate) and via
populate_habits_spec_from_db when the bots-table row is present.
"""

from __future__ import annotations

from app.bots.base import BotSpec, ReadScopes, WriteScopes
from app.bots.ids import HABITS_BOT_ID
from app.bots.prompts.habits import render_system_prompt as _persona_render


def _habits_prompt_renderer(
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
    """Habits prompt renderer — delegates to the persona module.

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


HABITS_READ_INSTRUCTION = (
    "Read step: gather only the context needed for this habits turn. "
    "For concrete plans and adherence, use the habits board tools such as "
    "`list_commitments`, `get_adherence`, or `list_events` when the hot "
    "context is not enough. For durable routine constraints, timing, "
    "recurring blockers, or tactics that might need recording later, read "
    "existing `get_memories` and/or `get_observations` first. Do not "
    "repeat a read tool that already returned enough context or an empty "
    "result; move on to the response instead. Do not write durable state "
    "or send user-facing text from this step."
)

HABITS_CONSULT_INSTRUCTION = (
    "Consult step: use only if an outside perspective would materially "
    "improve a tricky habits reply. Most habits turns should skip this "
    "step. Do not write durable state or send user-facing text from this "
    "step."
)

HABITS_RESPOND_INSTRUCTION = (
    "Respond step: send the user-facing reply, reaction, or silence. Keep "
    "it plain, specific, and low-key. On transports that support message "
    "parts, use `send_message_part` only when separate chat bubbles make "
    "the reply clearer. If the user reports a completed, missed, or "
    "excused slot and the relevant commitment is already known well "
    "enough, `log_event` may be used here; otherwise leave durable updates "
    "for the record step. Never mention internal phases, memory tools, or "
    "storage mechanics to the user."
)

HABITS_RECORD_INSTRUCTION = (
    "Record step: maintain durable habits state after the reply. Use "
    "memories for stable concrete facts, constraints, preferences, "
    "schedule, and support setup. Use observations for recurring "
    "patterns, blockers, and tactics that seem to help or fail. Use "
    "commitments only for explicit concrete plans the user agreed to "
    "track. Use events for completed, missed, or excused adherence "
    "reports. Read before durable writes, prefer updating or reinforcing "
    "an existing row over creating a duplicate, and skip writes when "
    "there is no useful future context to preserve. "
    "Never invent IDs — every commitment_id must come from a previous "
    "tool result. Call list_commitments before linking to an existing "
    "commitment. When the user accepts a concrete new plan, call "
    "create_commitment. Use log_event only for actual completed, missed, "
    "or excused events against a real returned commitment_id. Do not send "
    "user-facing text from this step."
)

HABITS_SCHEDULE_INSTRUCTION = (
    "Schedule step: create a follow-up or scheduled check-in only when it "
    "would clearly help the user's practice survive the week. Read "
    "existing tasks or check-ins first when duplication is plausible. Do "
    "not send user-facing text from this step."
)

HABITS_DONE_INSTRUCTION = (
    "Done step: stop. Do not call tools or add extra text."
)

HABITS_STEP_INSTRUCTIONS = {
    "read": HABITS_READ_INSTRUCTION,
    "consult": HABITS_CONSULT_INSTRUCTION,
    "respond": HABITS_RESPOND_INSTRUCTION,
    "record": HABITS_RECORD_INSTRUCTION,
    "schedule": HABITS_SCHEDULE_INSTRUCTION,
    "done": HABITS_DONE_INSTRUCTION,
}

# ── Tool allowlist ─────────────────────────────────────────────────────────
# Same exclusion set as Hector (no bridge/dyad tools, no pregnancy tools)
# plus the same seven commitment/event tools added back via _HABITS_ADDITIONS.
# BOT_EXCLUSIVE_TOOLS in app/services/tools/registry.py keeps the commitment
# tools available to Habits despite the stripping pass in _step_allowed().

_COACH_EXCLUSIONS = frozenset({
    "set_topic_status",
    "create_bridge_candidate",
    "update_bridge_candidate",
    "send_bridge_candidate",
    "list_bridge_candidates",
    "escalate_to_partner",
    "recent_activity",
    "set_pregnancy_edd",
    "correct_pregnancy_edd",
    "end_pregnancy",
})

_HABITS_ADDITIONS = frozenset({
    "list_commitments",
    "create_commitment",
    "update_commitment",
    "close_commitment",
    "log_event",
    "get_adherence",
    "list_events",
})


def build_habits_spec() -> BotSpec:
    """Build the Habits BotSpec.

    Lazy import of TOOL_DISPATCH avoids the same circular import Hector
    sidesteps. The tool_allowlist is the full dispatch table minus
    dyad-only/bridge/pregnancy tools plus the seven commitment/event tools.
    Habits does NOT subtract HECTOR_ONLY_TOOLS — only bots outside the
    BOT_EXCLUSIVE_TOOLS frozenset do.
    """
    from app.services.tools.registry import TOOL_DISPATCH  # noqa: PLC0415

    return BotSpec(
        bot_id=HABITS_BOT_ID,
        prompt_renderer=_habits_prompt_renderer,
        step_instructions=HABITS_STEP_INSTRUCTIONS,
        display_name="Habits",
        primary_topic_slug="habits",
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
        ) | _HABITS_ADDITIONS,
        bot_spec_version="1.0.0",
    )
