from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence


@dataclass(frozen=True)
class OpenAsk:
    key: str
    open_if: Callable[[Mapping[str, Any]], bool]
    example: str
    resolves_with: str


class _SafeFormatDict(dict[str, Any]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def render_open_asks(asks: Sequence[OpenAsk], state: Mapping[str, Any]) -> str:
    open_items = [ask for ask in asks if ask.open_if(state)]
    if not open_items:
        return ""
    lines = [
        "## Open asks",
        (
            "Things you don't know yet that you need to find out from the user. "
            "Work one in when there's a place to. One per turn. "
            "If they deflect or change subject, drop it for this turn."
        ),
        "",
    ]
    format_state = _SafeFormatDict(state)
    for ask in open_items:
        lines.append(f"- `{ask.key}` is not set.")
        lines.append(f'  Example: "{ask.example.format_map(format_state)}"')
        lines.append(f"  Resolves with: `{ask.resolves_with}`")
        lines.append("")
    return "\n".join(lines).rstrip()


# ── SuperPOM calibration asks (seven calibration slots) ──────────────────
# SuperPOM asks the user about each calibration area one at a time.
# Direct user answers → source='user_stated' (Compass-visible immediately).
# Inferred proposals → source='bot_proposed' (hidden until reviewed via
# review_orientation_item).  OOB boundaries use add_oob, not orientation.
# Pacing is handled by _first_missing_superpom_calibration_ask elsewhere.

SUPERPOM_ASKS = [
    OpenAsk(
        key="principle",
        open_if=lambda state: not state.get("principle_filled", False),
        example=(
            "What is a core value or guiding rule you try to live by? "
            "Something you come back to when you are unsure. "
            "If the user states one directly, record it with "
            "create_orientation_item (kind=principle, source='user_stated', "
            "label='SuperPOM - Principle: ...'). If you infer a candidate, "
            "propose it with source='bot_proposed' and ask for review."
        ),
        resolves_with=(
            "create_orientation_item (kind=principle, source=user_stated "
            "or bot_proposed, label prefix 'SuperPOM - Principle:')"
        ),
    ),
    OpenAsk(
        key="goal",
        open_if=lambda state: not state.get("goal_filled", False),
        example=(
            "What is a concrete aim you are working toward? Something with "
            "a rough target date or timeframe. "
            "If the user states one directly, record it with "
            "create_orientation_item (kind=goal, source='user_stated', "
            "label='SuperPOM - Goal: ...', include target_date). "
            "If you infer a candidate, propose it with source='bot_proposed' "
            "and ask for review."
        ),
        resolves_with=(
            "create_orientation_item (kind=goal, source=user_stated "
            "or bot_proposed, label prefix 'SuperPOM - Goal:')"
        ),
    ),
    OpenAsk(
        key="priority",
        open_if=lambda state: not state.get("priority_filled", False),
        example=(
            "What is your top near-term focus right now? The one thing that, "
            "if you could protect it, would make the biggest difference. "
            "If the user states one directly, record it with "
            "create_orientation_item (kind=priority, source='user_stated', "
            "label='SuperPOM - Priority: ...', include priority_rank). "
            "If you infer a candidate, propose it with source='bot_proposed' "
            "and ask for review."
        ),
        resolves_with=(
            "create_orientation_item (kind=priority, source=user_stated "
            "or bot_proposed, label prefix 'SuperPOM - Priority:')"
        ),
    ),
    OpenAsk(
        key="anti_pattern",
        open_if=lambda state: not state.get("anti_pattern_filled", False),
        example=(
            "Is there a recurring behavior or pattern you have noticed in "
            "yourself that you want to watch for? Something you tend to do "
            "that pulls you away from what matters. "
            "If the user states one directly, record it with "
            "create_orientation_item (kind=anti_pattern, source='user_stated', "
            "label='SuperPOM - Anti-Pattern: ...'). "
            "If you infer a candidate, propose it with source='bot_proposed' "
            "and ask for review."
        ),
        resolves_with=(
            "create_orientation_item (kind=anti_pattern, source=user_stated "
            "or bot_proposed, label prefix 'SuperPOM - Anti-Pattern:')"
        ),
    ),
    OpenAsk(
        key="strength",
        open_if=lambda state: not state.get("strength_filled", False),
        example=(
            "What is a capability, resource, or personal strength you "
            "recognize in yourself? Something you can draw on when things "
            "get hard. "
            "If the user states one directly, record it with "
            "create_orientation_item (kind=principle, source='user_stated', "
            "label='SuperPOM - Strength: ...'). "
            "If you infer a candidate, propose it with source='bot_proposed' "
            "and ask for review."
        ),
        resolves_with=(
            "create_orientation_item (kind=principle, source=user_stated "
            "or bot_proposed, label prefix 'SuperPOM - Strength:')"
        ),
    ),
    OpenAsk(
        key="tension",
        open_if=lambda state: not state.get("tension_filled", False),
        example=(
            "Is there a conflict or trade-off you feel between two things "
            "that both matter to you? A tension you keep returning to. "
            "If the user states one directly, record it with "
            "create_orientation_item (kind=anti_pattern, source='user_stated', "
            "label='SuperPOM - Tension: ...'). "
            "If you infer a candidate, propose it with source='bot_proposed' "
            "and ask for review."
        ),
        resolves_with=(
            "create_orientation_item (kind=anti_pattern, source=user_stated "
            "or bot_proposed, label prefix 'SuperPOM - Tension:')"
        ),
    ),
    OpenAsk(
        key="question",
        open_if=lambda state: not state.get("question_filled", False),
        example=(
            "What is a question you are sitting with? Something you have not "
            "figured out yet but keep thinking about. "
            "If the user states one directly, record it with "
            "create_orientation_item (kind=goal, source='user_stated', "
            "label='SuperPOM - Question: ...'). "
            "If you infer a candidate, propose it with source='bot_proposed' "
            "and ask for review."
        ),
        resolves_with=(
            "create_orientation_item (kind=goal, source=user_stated "
            "or bot_proposed, label prefix 'SuperPOM - Question:')"
        ),
    ),
]


def _get_bot_asks(bot_id: str) -> Sequence[OpenAsk]:
    if bot_id == "tante_rosi":
        from app.bots.prompts.tante_rosi import ASKS

        return ASKS
    if bot_id == "mediator":
        from app.services.prompts import VEAS_ASKS

        return VEAS_ASKS
    if bot_id == "superpom":
        return SUPERPOM_ASKS
    from app.services.prompts_solo import ASKS as SOLO_ASKS

    return SOLO_ASKS

# ── SuperPOM calibration state derivation ─────────────────────────────
# Derives *_filled booleans from a CompassSnapshot by checking
# orientation item labels for the seven agreed "SuperPOM - ...:" prefixes.
# treat compass_snapshot=None as an empty snapshot.

_SUPERPOM_LABEL_PREFIXES: dict[str, str] = {
    "principle_filled": "SuperPOM - Principle:",
    "goal_filled": "SuperPOM - Goal:",
    "priority_filled": "SuperPOM - Priority:",
    "anti_pattern_filled": "SuperPOM - Anti-Pattern:",
    "strength_filled": "SuperPOM - Strength:",
    "tension_filled": "SuperPOM - Tension:",
    "question_filled": "SuperPOM - Question:",
}

# Reverse mapping: prefix → state key, for efficient lookup.
_PREFIX_TO_KEY: dict[str, str] = {
    prefix: key for key, prefix in _SUPERPOM_LABEL_PREFIXES.items()
}


def _derive_superpom_calibration_state(compass_snapshot: Any | None) -> dict[str, bool]:
    """Derive SuperPOM calibration *_filled booleans from a CompassSnapshot.

    Scans all Compass-visible items (principles, priorities, anti_patterns,
    active_goals, completed_goals) for labels starting with the seven agreed
    ``SuperPOM - ...:`` prefixes.  A slot is filled when at least one
    Compass-visible item carries the matching label prefix.

    Treats ``compass_snapshot=None`` as an empty snapshot (all False).
    """
    state: dict[str, bool] = {key: False for key in _SUPERPOM_LABEL_PREFIXES}

    if compass_snapshot is None:
        return state

    # Collect all CompassItems from all categories.
    all_items: list[Any] = []
    for attr in (
        "principles",
        "priorities",
        "anti_patterns",
        "active_goals",
        "completed_goals",
    ):
        items = getattr(compass_snapshot, attr, ())
        all_items.extend(items)

    for item in all_items:
        label: str = getattr(item, "label", "") or ""
        for prefix, key in _PREFIX_TO_KEY.items():
            if label.startswith(prefix):
                state[key] = True
                break  # One item can only fill one slot.

    return state


def _first_missing_superpom_calibration_ask(
    state: Mapping[str, Any],
) -> OpenAsk | None:
    """Return the first SuperPOM calibration ask whose slot is not filled.

    Iterates SUPERPOM_ASKS in fixed order and returns the first ask where
    ``open_if(state)`` is True.  Returns None when all seven slots are filled.
    """
    for ask in SUPERPOM_ASKS:
        if ask.open_if(state):
            return ask
    return None

