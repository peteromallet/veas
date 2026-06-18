"""SuperPOM prompt tests (T4).

Verifies the rendered SuperPOM prompt contains the decision-flow contract,
Compass/orientation source mentions, review contract, calibration label
prefixes, and avoids ideal-self, shame, moral scoring, and perfectionist
framing.
"""

from __future__ import annotations

import pytest

from app.bots.prompts.profiles.superpom import PROFILE
from app.bots.prompts.profile import render_profile


# ── Helpers ──────────────────────────────────────────────────────────────

def _rendered(assistant_name: str = "SuperPOM", user_name: str = "TestUser") -> str:
    """Render the SuperPOM system prompt for a solo user."""
    return render_profile(
        PROFILE,
        assistant_name=assistant_name,
        user_name=user_name,
        partner_share=None,
    )


# ── Identity / role assertions ───────────────────────────────────────────

def test_prompt_contains_assistant_name():
    rendered = _rendered("SuperPOM", "Alice")
    assert "SuperPOM" in rendered
    assert "Alice" in rendered


def test_prompt_frames_as_action_catalyst():
    rendered = _rendered()
    assert "action catalyst" in rendered.lower()
    assert "loyal adviser" in rendered.lower()


def test_prompt_contains_role_summary():
    rendered = _rendered()
    assert "Role And Identity" in rendered
    assert "action catalyst" in rendered.lower()


# ── Decision-flow contract assertions ────────────────────────────────────

def test_prompt_contains_compass_first_instruction():
    rendered = _rendered()
    assert "Compass first" in rendered or "compass first" in rendered.lower()


def test_prompt_mentions_orientation_as_source():
    rendered = _rendered()
    assert "Orientation is source" in rendered or "orientation is source" in rendered.lower()


def test_prompt_mentions_auto_review_for_bot_proposed():
    rendered = _rendered()
    assert "bot_proposed" in rendered
    assert "review_orientation_item" in rendered
    assert "review_state='accepted'" in rendered


def test_prompt_mentions_user_stated_immediate():
    rendered = _rendered()
    assert "user_stated" in rendered
    assert "Compass-visible immediately" in rendered or "compass-visible immediately" in rendered.lower()


def test_prompt_contains_decision_flow_numbered_steps():
    rendered = _rendered()
    # Should have numbered steps 1-7 in the operating principles
    assert "1." in rendered
    assert "7." in rendered


# ── Calibration label prefix assertions ──────────────────────────────────

CALIBRATION_LABEL_PREFIXES = [
    "SuperPOM - Principle:",
    "SuperPOM - Goal:",
    "SuperPOM - Priority:",
    "SuperPOM - Anti-Pattern:",
    "SuperPOM - Strength:",
    "SuperPOM - Tension:",
    "SuperPOM - Question:",
]


@pytest.mark.parametrize("prefix", CALIBRATION_LABEL_PREFIXES)
def test_prompt_contains_calibration_label_prefix(prefix: str):
    rendered = _rendered()
    assert prefix in rendered, f"Missing calibration label prefix: {prefix}"


def test_prompt_has_exactly_seven_calibration_prefixes():
    rendered = _rendered()
    count = sum(1 for p in CALIBRATION_LABEL_PREFIXES if p in rendered)
    assert count == 7, f"Expected 7 calibration prefixes, found {count}"


# ── Avoidance assertions (no ideal-self, shame, moral scoring, perfectionist) ──

# Phrases that must NEVER appear in the prompt in any context.
FORBIDDEN_PHRASES = [
    "crush it",
    "beast mode",
    "grind",
    "no excuses",
    "shame on",
    "moral score",
    "grade yourself",
    "rank yourself",
    "optimize yourself",
    "be the best",
    "be your best",
    "become your best",
]

# Phrases that are acceptable ONLY in negation/rejection contexts
# (e.g. "not an ideal-self trainer", "no 'level up'", "not a moral failure").
# These appear in the prompt as explicit rejections of toxic framing.
NEGATED_PHRASES = [
    ("ideal-self", "trainer"),        # "not an ideal-self trainer"
    ("best self", 'no "best self'),   # 'no "best self"'
    ("better person", "you are not"),  # negation ("you are not\nhere to make...")
    ("level up", 'no "best self'),    # rejected register
    ("moral failure", "not moral"),   # "not moral failures"
    ("you failed", "better than"),    # quoted counterexample
]


@pytest.mark.parametrize("phrase", FORBIDDEN_PHRASES)
def test_prompt_avoids_forbidden_phrases(phrase: str):
    rendered = _rendered().lower()
    assert phrase.lower() not in rendered, (
        f"Forbidden phrase found in prompt: {phrase!r}"
    )


@pytest.mark.parametrize("phrase,context_hint", NEGATED_PHRASES)
def test_negated_phrases_only_in_rejection_context(phrase: str, context_hint: str):
    """Phrases like 'ideal-self' may appear ONLY in negation/rejection contexts."""
    rendered = _rendered().lower()
    # The phrase may appear, but when it does it must be near the context hint
    idx = rendered.find(phrase.lower())
    if idx != -1:
        # Check that the context hint is nearby (within 60 chars before)
        nearby = rendered[max(0, idx - 60):idx + len(phrase) + 10]
        assert context_hint.lower() in nearby.lower(), (
            f"Phrase {phrase!r} appears but not in expected negation context. "
            f"Nearby text: {nearby!r}"
        )


# ── Compass / orientation tool mentions ──────────────────────────────────

ORIENTATION_TOOLS = [
    "list_orientation_items",
    "get_orientation_item",
    "create_orientation_item",
    "update_orientation_item",
    "review_orientation_item",
    "close_orientation_item",
    "link_orientation_evidence",
]


@pytest.mark.parametrize("tool", ORIENTATION_TOOLS)
def test_prompt_mentions_orientation_tool(tool: str):
    rendered = _rendered()
    assert tool in rendered, f"Missing orientation tool mention: {tool}"


# ── Review tool surface assertions ───────────────────────────────────────

def test_prompt_distinguishes_orientation_from_memory():
    rendered = _rendered()
    assert "not memory facts" in rendered.lower() or "memory" in rendered.lower()


def test_prompt_distinguishes_orientation_from_oob():
    rendered = _rendered()
    assert "OOB" in rendered or "out-of-bounds" in rendered.lower()


def test_prompt_mentions_commitments_belong_elsewhere():
    rendered = _rendered()
    assert "hector" in rendered.lower() or "habits" in rendered.lower()
    assert "commitment" in rendered.lower()


def test_prompt_states_no_commitment_event_tools():
    rendered = _rendered()
    assert "do NOT have" in rendered or "do not have" in rendered.lower()
    # The custom_tail lists excluded tools
    assert "list_commitments" in rendered or "commitment" in rendered.lower()


# ── Profile field completeness ───────────────────────────────────────────

def test_profile_has_all_required_fields():
    assert PROFILE.bot_id == "superpom"
    assert PROFILE.assistant_name_default == "SuperPOM"
    assert len(PROFILE.role_summary) > 0
    assert len(PROFILE.persona) > 0
    assert len(PROFILE.voice) > 0
    assert len(PROFILE.not_a) > 0
    assert len(PROFILE.domain_safety) > 0
    assert len(PROFILE.operating_principles) > 0
    assert len(PROFILE.knowledge_primitives) > 0
    assert len(PROFILE.domain_specific) > 0
    assert len(PROFILE.custom_tail) > 0


def test_profile_domain_specific_contains_calibration_table():
    assert "Calibration Labels" in PROFILE.domain_specific
    assert "SuperPOM - Principle:" in PROFILE.domain_specific


# ── Integration with the persona renderer ────────────────────────────────

def test_persona_renderer_produces_non_empty_prompt():
    from app.bots.prompts.superpom import render_system_prompt

    result = render_system_prompt(
        assistant_name="SuperPOM",
        user_name="TestUser",
    )
    assert isinstance(result, str)
    assert len(result) > 100
    assert "SuperPOM" in result
    assert "TestUser" in result


def test_persona_renderer_ignores_partner_kwargs():
    from app.bots.prompts.superpom import render_system_prompt

    result = render_system_prompt(
        assistant_name="SuperPOM",
        user_name="TestUser",
        partner_name="ShouldBeIgnored",
        partner="some_partner_object",
    )
    assert isinstance(result, str)
    assert "ShouldBeIgnored" not in result
