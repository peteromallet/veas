"""Registry-level tests for the prompt slot registry.

Tests: duplicate-name rejection, audience filtering, stable sort,
render concatenation, ALL_BOTS completeness, mediator section ordering,
and new-tool presence in every bot's tool_allowlist.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.bots.prompts.registry import (
    ALL_BOTS,
    PromptSlot,
    register,
    render_slots_for,
    slots_for,
)
from app.bots.prompts.profile import render_profile
from app.models.user import User
from app.services.turn_context import TurnContext

# ── test: duplicate name ──────────────────────────────────────────────────────


def test_register_raises_on_duplicate_name() -> None:
    """Calling register() with a name that is already in the registry raises."""
    # 'scheduling' is always registered by the slots package import.
    dupe = PromptSlot(
        name="scheduling",
        body="bad duplicate body",
        audiences=frozenset({"mediator"}),
        order=9999,
    )
    with pytest.raises(ValueError, match="duplicate PromptSlot name: 'scheduling'"):
        register(dupe)


# ── test: audience filtering ──────────────────────────────────────────────────


def test_slots_for_filters_by_audience() -> None:
    """slots_for(bot_id) returns only slots whose audiences include bot_id."""
    hector_slots = slots_for("hector")
    hector_names = {s.name for s in hector_slots}

    # Hector gets the four extractable shared slots + four universal slots
    assert "body_image_eating_safety" in hector_names
    assert "adherence_board_rules" in hector_names
    assert "knowledge_primitives_rules" in hector_names
    assert "commitment_flow_rules" in hector_names
    assert "scheduling" in hector_names
    assert "reminders_bundling" in hector_names
    assert "partner_nudge" in hector_names
    assert "reply_discipline" in hector_names

    # Tante Rosi does NOT get the hector/habits-only extractable slots
    rosi_slots = slots_for("tante_rosi")
    rosi_names = {s.name for s in rosi_slots}
    assert "body_image_eating_safety" not in rosi_names
    assert "adherence_board_rules" not in rosi_names
    assert "knowledge_primitives_rules" not in rosi_names
    assert "commitment_flow_rules" not in rosi_names
    # But she gets all universal slots
    assert "scheduling" in rosi_names
    assert "reminders_bundling" in rosi_names
    assert "partner_nudge" in rosi_names
    assert "reply_discipline" in rosi_names


# ── test: stable sort ─────────────────────────────────────────────────────────


def test_slots_for_sorts_by_order_then_name() -> None:
    """slots_for returns slots in (order, name) ascending."""
    hector_slots = slots_for("hector")
    orders = [(s.order, s.name) for s in hector_slots]
    assert orders == sorted(orders), f"slots not sorted: {orders}"
    # Spot-check a few known positions
    names_in_order = [s.name for s in hector_slots]
    # body_image_eating_safety (720) before adherence_board_rules (740)
    idx_body = names_in_order.index("body_image_eating_safety")
    idx_adherence = names_in_order.index("adherence_board_rules")
    assert idx_body < idx_adherence
    # scheduling (800) before reminders_bundling (850)
    idx_sched = names_in_order.index("scheduling")
    idx_bundle = names_in_order.index("reminders_bundling")
    assert idx_sched < idx_bundle


# ── test: render concatenation ────────────────────────────────────────────────


def test_render_slots_for_concatenates_with_blank_lines() -> None:
    """render_slots_for emits each slot body with leading+trailing blank line."""
    rendered = render_slots_for("hector", only={"reply_discipline"})
    assert rendered.startswith("\n")
    assert rendered.endswith("\n")
    # Should not produce doubled newlines (just one blank line between slots)
    assert "\n\n\n" not in rendered

    # With multiple slots, each body gets its own wrapper
    rendered_multi = render_slots_for("hector", only={"scheduling", "partner_nudge"})
    stripped = rendered_multi.strip()
    assert stripped
    # The bodies are separated by blank lines
    assert "\n\n" in rendered_multi


# ── test: ALL_BOTS completeness ───────────────────────────────────────────────


def test_every_known_bot_id_is_in_ALL_BOTS() -> None:
    """ALL_BOTS matches the real bot_id values used in the codebase."""
    assert ALL_BOTS == frozenset({"mediator", "coach", "hector", "habits", "tante_rosi"}), (
        f"ALL_BOTS = {ALL_BOTS!r}"
    )


# ── test: mediator section order ──────────────────────────────────────────────


def test_mediator_section_order() -> None:
    """The mediator's rendered system prompt places 10 mid-template marker
    phrases in the original canonical sequence, plus 'One question per reply'
    at the end from the reply_discipline slot."""
    # Import mediator profile and render it with dummy placeholders
    from app.bots.prompts.profiles.mediator import PROFILE as MEDIATOR_PROFILE

    rendered = render_profile(
        MEDIATOR_PROFILE,
        assistant_name="Veas",
        user_name="TestUser",
        partner_share="opt_in",
        partner_a_name="Alice",
        partner_b_name="Bob",
        cross_thread_block="\n[cross thread opt-in]\n",
        partner_perspective_block="\n[partner perspective opt-in]\n",
    )

    # Marker phrases mapped to section names (in the original mediator order).
    # Use the '# ' prefixed heading so earlier inline mentions of a section
    # name (e.g. "Crisis Handling" inside Tool Usage Philosophy) don't
    # produce a false-positive hit that breaks the ordering assertion.
    markers = [
        "# Surfacing The Partner's Perspective",
        "# Partner Bridges",
        "# Tool Usage Philosophy",
        "# Scheduling Judgment",
        "# Multi-Message Handling",
        "# Voice Notes And Transcription Artifacts",
        "# In-Person Redirection",
        "# Conversation Closure",
        "# Crisis Handling",
        "# Output Style",
        "One question per reply",  # from reply_discipline slot, no # heading
    ]

    # All markers must appear
    for marker in markers:
        assert marker in rendered, f"Marker not found: {marker!r}"

    # Verify ordering: each marker's index < next marker's index
    positions = [rendered.index(m) for m in markers]
    for i in range(len(positions) - 1):
        assert positions[i] < positions[i + 1], (
            f"Order violation: {markers[i]!r} (idx {positions[i]}) "
            f"not before {markers[i+1]!r} (idx {positions[i+1]})"
        )


# ── test: new tools in every bot allowlist ─────────────────────────────────────


def _build_all_specs():
    """Build BotSpecs for all five bots, enabling staging if needed."""
    # The build functions are available directly; they don't require STAGING=1
    # because they only import TOOL_DISPATCH when called.
    from app.bots.coach import build_coach_spec
    from app.bots.hector import build_hector_spec
    from app.bots.habits import build_habits_spec
    from app.bots.tante_rosi import build_tante_rosi_spec
    from app.bots.mediator import MEDIATOR_BOT

    specs = {
        "coach": build_coach_spec(),
        "hector": build_hector_spec(),
        "habits": build_habits_spec(),
        "tante_rosi": build_tante_rosi_spec(),
        "mediator": MEDIATOR_BOT,
    }

    # Patch mediator's tool_allowlist (normally done by _maybe_register_staging_bots)
    if specs["mediator"].tool_allowlist is None:
        from app.services.tools.registry import HECTOR_ONLY_TOOLS, TOOL_DISPATCH

        _PREGNANCY_ONLY_TOOLS = frozenset({
            "set_pregnancy_edd", "correct_pregnancy_edd", "end_pregnancy",
        })
        import dataclasses

        specs["mediator"] = dataclasses.replace(
            specs["mediator"],
            tool_allowlist=(
                frozenset(TOOL_DISPATCH.keys())
                - HECTOR_ONLY_TOOLS
                - _PREGNANCY_ONLY_TOOLS
            ),
        )

    return specs


def test_both_new_tools_in_every_bot_allowlist() -> None:
    """Every bot's tool_allowlist includes update_scheduled_checkin and
    list_all_reminders."""
    specs = _build_all_specs()

    for bot_id, spec in specs.items():
        allowlist = spec.tool_allowlist
        assert allowlist is not None, f"{bot_id} has no tool_allowlist"
        assert "update_scheduled_checkin" in allowlist, (
            f"update_scheduled_checkin missing from {bot_id} allowlist"
        )
        assert "list_all_reminders" in allowlist, (
            f"list_all_reminders missing from {bot_id} allowlist"
        )


def _step_allowed_for(bot_id: str):
    from app.services.tools.registry import _step_allowed

    user = User(id=uuid4(), name="Test", phone="+15555550100", timezone="UTC")
    spec = _build_all_specs()[bot_id]
    ctx = TurnContext(
        turn_id=uuid4(),
        pool=None,
        user=user,
        partner=None,
        triggering_message_ids=[],
        bot_id=bot_id,
        primary_topic_id=uuid4(),
        primary_topic_slug=spec.primary_topic_slug,
        current_step="read",
        bot_spec=spec,
    )
    return _step_allowed(ctx)


def test_nav_and_search_read_tools_survive_step_allowed_intersection() -> None:
    expected = {
        "messages_before",
        "messages_after",
        "open_thread",
        "scroll",
        "topic_recent",
        "search",
        "search_messages",
    }

    for bot_id in ("mediator", "coach", "hector", "habits", "tante_rosi"):
        allowed = _step_allowed_for(bot_id)
        missing = expected - allowed
        assert not missing, f"{bot_id} lost read tools after _step_allowed: {missing}"


def test_recent_activity_stays_excluded_from_solo_bot_step_allowed_sets() -> None:
    for bot_id in ("coach", "hector", "habits", "tante_rosi"):
        allowed = _step_allowed_for(bot_id)
        assert "recent_activity" not in allowed, (
            f"{bot_id} should still exclude recent_activity"
        )
