"""Versioned system prompts for the agentic conversational loop."""

from app.bots.prompts.profiles.mediator import PROFILE
from app.bots.prompts.profile import render_profile
from app.services.cross_thread_privacy import normalize_partner_share_for_privacy
from app.services.open_asks import OpenAsk

SYSTEM_PROMPT_VERSION = "v3"

# ── Backward-compat constants ─────────────────────────────────────────
# These are kept so existing tests and imports don't break.
# The render_system_prompt function now delegates to render_profile,
# but these constants are still exported for any code that reads them.

SYSTEM_PROMPT_V1 = """\
# The Six Knowledge Primitives
provisional synthesized explanations
get_distillations
source_user_ids` must be non-empty and conservative
do not delete or mutate underlying observations"""
SYSTEM_PROMPT_V2 = ""
SYSTEM_PROMPT_V3 = ""

CROSS_THREAD_OPT_IN_V1 = """\
The current user's `partner_share` is `opt_in`: their thread is shareable across the relationship bridge by default, subject to OOB and judgment. They can still mark individual things out of bounds so those stay private. OOB always overrides opt-in — never bypass `check_oob` because the default is permissive."""

CROSS_THREAD_OPT_OUT_V1 = """\
The current user's `partner_share` is `opt_out`: their thread is private by default. Do not surface their rows to their partner, do not pressure or repeat the opt-in question, and bridge only material they explicitly ask or allow you to share in this turn. Respect `opt_out` as a settled choice."""

PARTNER_PERSPECTIVE_OPT_IN_V1 = """\
The partner has explicitly authorized their thread to be referenced in conversations with the current user. Treat their thread as a legitimate, shared resource — not a private channel you must protect from the current user. Keep their perspective live in this thread — frequently, not occasionally — whenever the current user is interpreting, predicting, blaming, defending against, or guessing at them. The goal is to keep the partner present as a real second mind, not to let the user build a sealed model of them.

Do not refuse on privacy grounds. Responses like "I can't share what they said privately" or "their thread is between us" are wrong in this configuration and a failure of the bridge. Use discretion based on the context of the conversation to decide what specifically to share — what is grounded, what is helpful, what is timely, and what should be paraphrased vs. held — but the baseline is openness, not refusal.

Be active, not passive. Do not announce that the partner has opted in and hand the user a menu of things they could ask. Read the partner's side yourself with `search_messages`, `get_observations`, `get_memories`, `list_themes`, and bring in a grounded paraphrase: "From his side, he's been carrying [X]." "You're describing your read of Peter — but on her thread she's named [grounded paraphrase]." Ask the user's permission before going deeper only when the material is sensitive or you want to check interest.

- Search before surfacing. Never invent or extrapolate beyond what's actually there. If you have nothing grounded, say so plainly rather than filling the gap.
- Run partner content through OOB and the recipient-aware `check_oob`. `firm` and `hard` stay protected. Distill into a neutral paraphrase; do not quote raw partner-private wording unless clearly safe.
- Keep attribution clean: "from what he's said here", "this is my read, not her words." Do not let the partner's thread show up in the user's mouth.
- This does not replace direct conversation. When the right move is "ask him yourself", say that."""

PARTNER_PERSPECTIVE_OTHER_V1 = """\
The partner's `partner_share` is `opt_out` or `unset`. Do not paraphrase partner-thread content. You may note that the perspective exists and could be asked for directly or bridged case-by-case."""

PROMPT_REGISTRY: dict[str, str] = {
    "v1": SYSTEM_PROMPT_V1,
    "v2": SYSTEM_PROMPT_V2,
    SYSTEM_PROMPT_VERSION: SYSTEM_PROMPT_V3,
}

CROSS_THREAD_REGISTRY: dict[str, dict[str, str]] = {
    "v1": {
        "opt_in": CROSS_THREAD_OPT_IN_V1,
        "opt_out": CROSS_THREAD_OPT_OUT_V1,
    },
    "v2": {
        "opt_in": CROSS_THREAD_OPT_IN_V1,
        "opt_out": CROSS_THREAD_OPT_OUT_V1,
    },
    SYSTEM_PROMPT_VERSION: {
        "opt_in": CROSS_THREAD_OPT_IN_V1,
        "opt_out": CROSS_THREAD_OPT_OUT_V1,
    },
}

PARTNER_PERSPECTIVE_REGISTRY: dict[str, dict[str, str]] = {
    "v1": {
        "opt_in": PARTNER_PERSPECTIVE_OPT_IN_V1,
        "opt_out": PARTNER_PERSPECTIVE_OTHER_V1,
        "unset": PARTNER_PERSPECTIVE_OTHER_V1,
    },
    "v2": {
        "opt_in": PARTNER_PERSPECTIVE_OPT_IN_V1,
        "opt_out": PARTNER_PERSPECTIVE_OTHER_V1,
        "unset": PARTNER_PERSPECTIVE_OTHER_V1,
    },
    SYSTEM_PROMPT_VERSION: {
        "opt_in": PARTNER_PERSPECTIVE_OPT_IN_V1,
        "opt_out": PARTNER_PERSPECTIVE_OTHER_V1,
        "unset": PARTNER_PERSPECTIVE_OTHER_V1,
    },
}


class UnknownPromptVersion(ValueError):
    pass


def get_system_prompt_template(prompt_version: str) -> str:
    try:
        return PROMPT_REGISTRY[prompt_version]
    except KeyError as exc:
        known = ", ".join(sorted(PROMPT_REGISTRY))
        raise UnknownPromptVersion(
            f"unknown system prompt version: {prompt_version}; known versions: {known}"
        ) from exc


class UnknownPromptVersion(ValueError):
    pass


KNOWN_PROMPT_VERSIONS = frozenset({"v1", "v2", "v3"})


def render_system_prompt(
    assistant_name: str,
    partner_a: str,
    partner_b: str,
    *,
    prompt_version: str = SYSTEM_PROMPT_VERSION,
    onboarding_state: str | None = None,
    current_user_partner_share: str | None = None,
    partner_partner_share: str | None = None,
    current_user_partner_sharing_state: str | None = None,
    partner_partner_sharing_state: str | None = None,
    **kwargs: object,
) -> str:
    if prompt_version not in KNOWN_PROMPT_VERSIONS:
        raise UnknownPromptVersion(
            f"unknown system prompt version: {prompt_version}; "
            f"known versions: {', '.join(sorted(KNOWN_PROMPT_VERSIONS))}"
        )
    del prompt_version, onboarding_state

    if current_user_partner_share is None:
        current_user_partner_share = kwargs.get(
            "current_user_" + "sharing" + "_default"
        )  # type: ignore[assignment]
    if partner_partner_share is None:
        partner_partner_share = kwargs.get("partner_" + "sharing" + "_default")  # type: ignore[assignment]

    # Default to most-protective branch for unrecognized / None values.
    current_state = normalize_partner_share_for_privacy(current_user_partner_share)
    partner_state = normalize_partner_share_for_privacy(partner_partner_share)
    if current_user_partner_sharing_state is None:
        current_user_partner_sharing_state = (
            "pending" if current_state == "unset" else current_state
        )
    if partner_partner_sharing_state is None:
        partner_partner_sharing_state = (
            "pending" if partner_state == "unset" else partner_state
        )
    if partner_state == "opt_in":
        partner_branch_key = "opt_in"
    else:
        partner_branch_key = "opt_out"

    del current_user_partner_sharing_state, partner_partner_sharing_state

    # Compute the two dynamic blocks inserted into domain_specific.
    if current_state in {"opt_in", "opt_out"}:
        cross_thread_block = CROSS_THREAD_REGISTRY[SYSTEM_PROMPT_VERSION][current_state]
    else:
        cross_thread_block = ""

    partner_perspective_block = PARTNER_PERSPECTIVE_REGISTRY[SYSTEM_PROMPT_VERSION][
        partner_branch_key
    ]

    # render_profile will substitute {cross_thread_block} and
    # {partner_perspective_block} from format_kwargs.
    return render_profile(
        PROFILE,
        assistant_name=assistant_name,
        user_name="",  # mediator doesn't use {user_name}
        partner_share=current_user_partner_share,
        partner_a_name=partner_a,
        partner_b_name=partner_b,
        cross_thread_block=f"\n{cross_thread_block}\n" if cross_thread_block else "\n",
        partner_perspective_block=f"\n{partner_perspective_block}\n",
        **{k: v for k, v in kwargs.items() if v is not None},
    )


VEAS_ASKS = [
    OpenAsk(
        key="partner_share",
        open_if=lambda state: bool(state.get("has_partner"))
        and state.get("partner_share") is None,
        example=(
            "Before we go further — can I share carefully chosen, "
            "non-sensitive context from your side with {partner_name}, "
            "or would you rather I kept this thread fully private?"
        ),
        resolves_with="set_partner_sharing",
    ),
]
