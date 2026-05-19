"""SCHEDULING_CAPABILITY_PROMPT_SLOT (S4) — slot quality + mounting.

Critique flag 3: the forbidden refusal phrases (e.g. "I don't have the
ability") MUST be ABSENT from rendered prompts AND from the slot text
itself. The slot describes the BEHAVIOR to avoid; the forbidden literal
strings live ONLY in this test file as the assert-absent list.
"""

from __future__ import annotations

from uuid import uuid4

from app.bots.prompts.scheduling import SCHEDULING_CAPABILITY_PROMPT_SLOT


SCHEDULING_TOOLS_TO_NAME = [
    "schedule_checkin",
    "schedule_task",
    "list_scheduled_tasks",
    "list_scheduled_checkins",
    "list_all_reminders",
    "update_scheduled_task",
    "update_scheduled_checkin",
    "cancel_scheduled_task",
    "cancel_scheduled_checkin",
]


FORBIDDEN_REFUSAL_PHRASES = [
    "I don't have the ability",
    "I do not have the ability",
    "I can't set up a reminder",
    "set a reminder on your phone",
    "that's not something I can do",
    "that is not something I can do",
]


def test_slot_is_under_two_hundred_active_words() -> None:
    assert len(SCHEDULING_CAPABILITY_PROMPT_SLOT.split()) <= 200


def test_slot_names_every_scheduling_tool() -> None:
    for tool in SCHEDULING_TOOLS_TO_NAME:
        assert tool in SCHEDULING_CAPABILITY_PROMPT_SLOT, tool


def test_slot_text_does_not_quote_forbidden_refusal_phrases() -> None:
    """Critique flag 3: forbidden phrases must not appear in the slot
    itself, because they would survive into the rendered prompt and
    contradict the very behavior the slot teaches.
    """
    for phrase in FORBIDDEN_REFUSAL_PHRASES:
        assert phrase not in SCHEDULING_CAPABILITY_PROMPT_SLOT, phrase


def test_mediator_rendered_prompt_contains_slot_and_no_forbidden_phrases() -> None:
    from app.services.prompts import render_system_prompt

    rendered = render_system_prompt(
        "Veas",
        "Maya",
        "Ben",
        current_user_partner_share="opt_in",
        current_user_partner_sharing_state="opt_in",
    )
    assert SCHEDULING_CAPABILITY_PROMPT_SLOT in rendered
    for phrase in FORBIDDEN_REFUSAL_PHRASES:
        assert phrase not in rendered, phrase
    for tool in SCHEDULING_TOOLS_TO_NAME:
        assert tool in rendered, tool


def test_solo_rendered_prompt_contains_slot() -> None:
    from app.services.prompts_solo import render_solo_system_prompt

    rendered = render_solo_system_prompt("Coach", "Maya")
    assert SCHEDULING_CAPABILITY_PROMPT_SLOT in rendered
    for phrase in FORBIDDEN_REFUSAL_PHRASES:
        assert phrase not in rendered, phrase


def test_tante_rosi_rendered_prompt_contains_slot() -> None:
    from app.bots.prompts.tante_rosi import render_system_prompt

    rendered = render_system_prompt(assistant_name="Tante Rosi", user_name="Anna")
    assert SCHEDULING_CAPABILITY_PROMPT_SLOT in rendered
    for phrase in FORBIDDEN_REFUSAL_PHRASES:
        assert phrase not in rendered, phrase


def test_tante_rosi_botspec_render_includes_slot() -> None:
    """Critique flag 8: build_tante_rosi_spec().render_system_prompt
    must produce the new slots — covers the actual production path
    (BotSpec → _tante_rosi_prompt_renderer → render_system_prompt).
    """
    from app.bots.tante_rosi import build_tante_rosi_spec
    from app.models.user import User

    spec = build_tante_rosi_spec()
    user = User(uuid4(), "Anna", "15555550100", "UTC")
    rendered = spec.render_system_prompt(
        assistant_name="Tante Rosi",
        user=user,
        partner=None,
        prompt_version="v1",
    )
    assert SCHEDULING_CAPABILITY_PROMPT_SLOT in rendered
    for phrase in FORBIDDEN_REFUSAL_PHRASES:
        assert phrase not in rendered, phrase
