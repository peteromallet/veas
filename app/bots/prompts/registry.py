"""Prompt slot registry with audience routing and canonical section ordering.

Section ordering
----------------
The canonical ``SECTION_ORDER`` list defines the exact sequence of sections
in every bot's rendered system prompt.  Each entry is a ``SectionStep`` with
a ``kind`` of ``'field'`` (renders a ``BotProfile`` field) or ``'slot'``
(renders a named registry slot only when ``bot_id`` is in its audiences).

Field steps whose resolved value is falsy (empty string, None) are skipped.
Slot steps are skipped when the bot is not in the slot's audience set; when
it is, the slot body is emitted with a leading and trailing blank line.

Order values are spaced at intervals of ≥20 so future slots can wedge in
without renumbering the whole list.

Bot ids
-------
The ``ALL_BOTS`` constant is the single source of truth for known bot ids.
Profile files reference their own ``bot_id`` once and may import ``ALL_BOTS``
for ergonomic audience declarations.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Literal


# ── Known bot identifiers ──────────────────────────────────────────────────

ALL_BOTS: frozenset[str] = frozenset(
    {"mediator", "coach", "hector", "habits", "tante_rosi"}
)


# ── PromptSlot ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PromptSlot:
    """One named, audience-routed prompt paragraph."""

    name: str
    body: str
    audiences: frozenset[str]
    order: int


# ── SectionStep ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SectionStep:
    """A single entry in ``SECTION_ORDER``.

    ``kind`` is ``'field'`` (resolve via ``getattr(profile, name)``) or
    ``'slot'`` (render the named registry slot when ``bot_id`` is in its
    audiences).

    ``conditional`` is an optional callable that receives the render-kwargs
    dict and returns a bool.  When provided and the callable returns False
    the step is skipped.  This is used *only* for
    ``partner_sharing_opt_in_section`` (mounted when ``partner_share ==
    'opt_in'``).
    """

    kind: Literal["field", "slot"]
    name: str
    order: int
    conditional: Callable[[dict], bool] | None = None


# ── Canonical section order ────────────────────────────────────────────────

SECTION_ORDER: list[SectionStep] = [
    SectionStep(kind="field", name="role_summary", order=100),
    SectionStep(kind="field", name="persona", order=200),
    SectionStep(kind="field", name="voice", order=300),
    SectionStep(kind="field", name="not_a", order=400),
    SectionStep(kind="field", name="domain_safety", order=500),
    SectionStep(kind="field", name="operating_principles", order=600),
    SectionStep(kind="field", name="knowledge_primitives", order=700),
    SectionStep(kind="slot", name="body_image_eating_safety", order=720),
    SectionStep(kind="slot", name="adherence_board_rules", order=740),
    SectionStep(kind="slot", name="knowledge_primitives_rules", order=760),
    SectionStep(kind="slot", name="commitment_flow_rules", order=780),
    SectionStep(kind="slot", name="scheduling", order=800),
    SectionStep(kind="slot", name="reminders_bundling", order=850),
    SectionStep(kind="slot", name="partner_nudge", order=900),
    SectionStep(
        kind="field",
        name="partner_sharing_opt_in_section",
        order=950,
        conditional=lambda ctx: ctx.get("partner_share") == "opt_in",
    ),
    SectionStep(kind="field", name="domain_specific", order=960),
    SectionStep(kind="slot", name="reply_discipline", order=1000),
    SectionStep(kind="field", name="custom_tail", order=1100),
]


# ── Registry ───────────────────────────────────────────────────────────────

_SLOTS: list[PromptSlot] = []


def register(slot: PromptSlot) -> PromptSlot:
    """Add *slot* to the module-level registry.

    Raises ``ValueError`` when a slot with the same ``name`` is already
    registered (import-safe: duplicate imports are surfaced loudly).
    """
    if any(existing.name == slot.name for existing in _SLOTS):
        raise ValueError(f"duplicate PromptSlot name: {slot.name!r}")
    _SLOTS.append(slot)
    return slot


def slots_for(bot_id: str) -> list[PromptSlot]:
    """Return slots whose audience includes *bot_id*, sorted by ``(order, name)``."""
    return sorted(
        [s for s in _SLOTS if bot_id in s.audiences],
        key=lambda s: (s.order, s.name),
    )


def render_slots_for(
    bot_id: str, *, only: Iterable[str] | None = None
) -> str:
    """Concatenate ``"\\n" + slot.body + "\\n"`` for every matching slot.

    When *only* is provided, only slots whose ``name`` is in *only* are
    emitted (used by ``SectionStep`` of kind ``'slot'``).
    """
    allowed: frozenset[str] | None = (
        frozenset(only) if only is not None else None
    )
    parts: list[str] = []
    for slot in slots_for(bot_id):
        if allowed is not None and slot.name not in allowed:
            continue
        parts.append("\n" + slot.body + "\n")
    return "".join(parts)


# ── Populate registry from slot modules ────────────────────────────────────
# Each slot module calls register() at import time.  The slots package
# __init__.py imports every submodule so importing it once is enough.

try:
    from app.bots.prompts import slots  # noqa: E402, F401
except ImportError:
    # The slots package is created in a later batch (T4).  Until then the
    # registry is empty; no bot profile will attempt to render before T4.
    pass
