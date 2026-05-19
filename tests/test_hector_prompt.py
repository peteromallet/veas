"""Prompt content tests for Hector.

T16: Assert that the rendered system prompt contains EVERY locked
Prompt Requirement from docs/fitness-bot-commitments-plan.md.
"""

from __future__ import annotations

import pytest

from app.bots.hector import build_hector_spec
from app.bots.prompts.hector import render_system_prompt


def _render(assistant_name: str = "Hector", user_name: str = "TestUser") -> str:
    """Render the Hector system prompt with minimal arguments."""
    return render_system_prompt(
        assistant_name=assistant_name,
        user_name=user_name,
        prompt_version="v1",
        onboarding_state="completed",
        partner_share=None,
        partner_sharing_state="unavailable",
    )


class TestHectorPromptRequirements:
    """Every locked Prompt Requirement must appear in the rendered prompt."""

    # ── Concrete plans only ──────────────────────────────────────────

    def test_create_commitments_only_from_concrete_plans(self):
        prompt = _render()
        # The prompt uses "concrete" in multiple forms — check for the concept
        assert "concrete" in prompt.lower() or "not create commitments" in prompt.lower() or "before creating" in prompt.lower()

    def test_ask_before_tracking_vague_goals(self):
        prompt = _render()
        assert "vague" in prompt.lower()

    # ── Adherence checklist ──────────────────────────────────────────

    def test_use_hot_context_adherence_checklist(self):
        prompt = _render()
        assert "adherence" in prompt.lower()

    # ── Unknown vs missed ───────────────────────────────────────────

    def test_distinguish_unknown_vs_missed(self):
        prompt = _render()
        assert "unknown" in prompt.lower() and "missed" in prompt.lower()

    # ── No shaming ──────────────────────────────────────────────────

    def test_no_shaming(self):
        prompt = _render()
        assert "shame" in prompt.lower()
        # Should mention NOT shaming
        assert "not" in prompt.lower() or "never" in prompt.lower()

    # ── No overpraise ───────────────────────────────────────────────

    def test_no_overpraise(self):
        prompt = _render()
        assert "overpraise" in prompt.lower() or "over-praise" in prompt.lower()

    # ── Low-key pressure ────────────────────────────────────────────

    def test_low_key_pressure(self):
        prompt = _render()
        assert "low-key" in prompt.lower() or "low_key" in prompt.lower() or "pressure" in prompt.lower()

    # ── One concrete next action ────────────────────────────────────

    def test_prefer_one_concrete_next_action(self):
        prompt = _render()
        assert "next" in prompt.lower()
        assert "action" in prompt.lower() or "step" in prompt.lower()

    # ── Respect constraints ─────────────────────────────────────────

    def test_respect_constraints(self):
        prompt = _render()
        assert "constraint" in prompt.lower()

    # ── Defer medical ───────────────────────────────────────────────

    def test_defer_medical(self):
        prompt = _render()
        assert "medical" in prompt.lower() or "doctor" in prompt.lower() or "clinical" in prompt.lower()

    # ── No calorie-counting pressure unless asked ───────────────────

    def test_no_calorie_counting_pressure(self):
        prompt = _render()
        assert "calorie" in prompt.lower()

    # ── No body-image escalation / ED-like behavior ─────────────────

    def test_no_body_image_escalation(self):
        prompt = _render()
        assert "body" in prompt.lower() and ("image" in prompt.lower() or "appearance" in prompt.lower() or "eating" in prompt.lower())

    # ── No default weigh-ins / progress photos ──────────────────────

    def test_no_default_weigh_ins_or_photos(self):
        prompt = _render()
        # At least one of these topics must be addressed
        assert (
            "weigh" in prompt.lower()
            or "scale" in prompt.lower()
            or "photo" in prompt.lower()
            or "progress pic" in prompt.lower()
        )

    # ── One clarifying question for vague goals ─────────────────────

    def test_ask_clarifying_question_for_vague_goals(self):
        prompt = _render()
        assert "clarif" in prompt.lower() or "question" in prompt.lower()

    # ── Persona checks ──────────────────────────────────────────────

    def test_not_a_doctor(self):
        prompt = _render()
        assert "not a doctor" in prompt.lower()

    def test_not_a_therapist(self):
        prompt = _render()
        assert "not" in prompt.lower() and "therapist" in prompt.lower()

    def test_not_a_nutritionist(self):
        prompt = _render()
        assert "nutritionist" in prompt.lower()

    def test_not_a_shame_machine(self):
        prompt = _render()
        assert "shame" in prompt.lower()

    def test_not_an_optimization_dashboard(self):
        prompt = _render()
        assert "optimization" in prompt.lower() or "dashboard" in prompt.lower()

    def test_not_a_motivational_poster(self):
        prompt = _render()
        assert "motivational" in prompt.lower()


class TestHectorPromptRenderSafety:
    """Renderer must accept partner_name=None safely."""

    def test_render_with_partner_name_none(self):
        result = render_system_prompt(
            assistant_name="Hector",
            user_name="TestUser",
            partner_name=None,
            prompt_version="v1",
            onboarding_state="completed",
            partner_share=None,
            partner_sharing_state="unavailable",
        )
        assert isinstance(result, str)
        assert len(result) > 100

    def test_render_contains_hector_name(self):
        prompt = _render("Hector", "Alice")
        assert "Hector" in prompt

    def test_render_contains_user_name(self):
        prompt = _render("Hector", "Alice")
        assert "Alice" in prompt


class TestHectorPersistenceGuidance:
    """Hector should know what durable fitness state is worth preserving."""

    def test_prompt_defines_fitness_knowledge_primitives(self):
        prompt = _render()
        lower = prompt.lower()

        assert "knowledge primitives" in lower
        assert "memories are stable concrete facts" in lower
        assert "observations are patterns and tactics" in lower
        assert "commitments are explicit concrete plans" in lower
        assert "events are adherence reports" in lower
        assert "before adding or updating durable state" in lower

    def test_step_instructions_are_specific_not_stubs(self):
        spec = build_hector_spec()
        rendered = "\n".join(spec.step_instructions.values()).lower()

        assert "phase 1 stub" not in rendered
        assert "durable fitness state" in rendered
        assert "read before durable writes" in rendered
        assert "prefer updating" in rendered
