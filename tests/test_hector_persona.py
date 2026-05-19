"""Snapshot tests for the Hector persona prompt and BotSpec values.

Mirrors test_bot_spec_render.py and test_pregnancy_allowlist.py patterns.
Verifies all required behavioral constraints, BotSpec values, and allowlist
contents.
"""

from __future__ import annotations

import pytest


class TestHectorPrompt:
    """Rendered system prompt must contain all required behavioral constraints."""

    @pytest.fixture(autouse=True)
    def _register_staging(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Ensure staging bots are registered for spec inspection."""
        monkeypatch.setenv("STAGING", "1")
        import app.bots.registry as _reg

        _reg._STAGING_BOTS_REGISTERED = False
        _reg._maybe_register_staging_bots()

    def _render_prompt(self, user_name: str = "Alex") -> str:
        from app.bots.prompts.hector import render_system_prompt

        return render_system_prompt(assistant_name="Hector", user_name=user_name)

    def test_prompt_contains_concrete_plans_only_rule(self):
        """Concrete-plans-only constraint: don't track vague goals."""
        prompt = self._render_prompt()
        assert "Create commitments only from concrete user plans" in prompt
        assert "do NOT create a commitment" in prompt

    def test_prompt_contains_ask_before_vague_tracking(self):
        """Ask one clarifying question for vague goals."""
        prompt = self._render_prompt()
        assert "Ask one" in prompt
        assert "practical clarifying question" in prompt
        assert "ask before tracking" in prompt.lower()

    def test_prompt_contains_adherence_before_checkin(self):
        """Adherence-before-checkin rule: check the board first."""
        prompt = self._render_prompt()
        assert "Read the hot context every turn" in prompt
        # The sentence wraps across lines in the 80-char prompt format
        assert "you already" in prompt
        assert "know which slots are blank" in prompt

    def test_prompt_contains_unknown_vs_missed_distinction(self):
        """Unknown-vs-missed distinction must be clear."""
        prompt = self._render_prompt()
        assert "Distinguish unknown from missed" in prompt
        assert "Unknown means the slot is in the past" in prompt
        # Sentence wraps across lines; check word anchors
        assert "Missed means it was already" in prompt
        assert "marked" in prompt

    def test_prompt_contains_no_shame_language(self):
        """No-shame: missed days are information, not moral events."""
        prompt = self._render_prompt()
        assert "Do not shame" in prompt
        assert "A missed day is information" in prompt
        assert "not a moral event" in prompt

    def test_prompt_contains_no_overpraise_language(self):
        """No-overpraise: grounded acknowledgment over hype."""
        prompt = self._render_prompt()
        assert "Do not overpraise" in prompt
        assert "is better than" in prompt

    def test_prompt_contains_low_key_pressure_language(self):
        """Low-key pressure: friend who notices, not drill sergeant."""
        prompt = self._render_prompt()
        assert "Keep pressure real but low-key" in prompt
        assert "You are not a drill sergeant" in prompt
        assert "the steady second pair of eyes" in prompt

    def test_prompt_contains_medical_deferral(self):
        """Medical deferral: always defer to professionals."""
        prompt = self._render_prompt()
        assert "Not a doctor" in prompt
        assert "Defer medical" in prompt
        assert "question for a doctor or a physio" in prompt

    def test_prompt_contains_no_default_calorie_photo_weigh_in(self):
        """No default calorie/photo/weigh-in pressure."""
        prompt = self._render_prompt()
        assert "Do not make progress photos" in prompt
        assert "Avoid calorie-counting pressure" in prompt

    def test_prompt_contains_partner_sharing_boundaries(self):
        """Partner-sharing boundaries must be explicit (opt_in section)."""
        from app.bots.prompts.hector import render_system_prompt

        prompt = render_system_prompt(
            assistant_name="Hector", user_name="Alex", partner_share="opt_in"
        )
        assert (
            "Keep exact measurements, body details, missed-adherence reports"
            in prompt.replace("\n", " ")
        )
        assert "keep it private" in prompt.lower()

    def test_prompt_contains_no_influencer_language(self):
        """No influencer language in the prompt."""
        prompt = self._render_prompt()
        assert "No influencer language" in prompt
        assert "crush it" in prompt
        assert "beast mode" in prompt
        assert "grind" in prompt

    def test_prompt_contains_prefer_concrete_action(self):
        """Prefer one concrete next action over broad advice."""
        prompt = self._render_prompt()
        assert "Prefer one concrete next action" in prompt

    def test_prompt_user_name_substituted(self):
        """User name is substituted into the prompt."""
        prompt = self._render_prompt(user_name="Alice")
        assert "fitness companion for Alice" in prompt

    def test_prompt_renders_non_empty(self):
        """Prompt must be a non-empty string."""
        prompt = self._render_prompt()
        assert isinstance(prompt, str)
        assert len(prompt) > 500  # substantial prompt


class TestHectorBotSpec:
    """BotSpec values must match the Hector design."""

    @pytest.fixture(autouse=True)
    def _register_staging(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("STAGING", "1")
        import app.bots.registry as _reg

        _reg._STAGING_BOTS_REGISTERED = False
        _reg._maybe_register_staging_bots()

    def _get_spec(self):
        from app.bots.registry import get_bot_spec

        return get_bot_spec("hector")

    def test_bot_id_is_hector(self):
        spec = self._get_spec()
        assert spec.bot_id == "hector"

    def test_display_name_is_hector(self):
        spec = self._get_spec()
        assert spec.display_name == "Hector"

    def test_primary_topic_slug_is_fitness(self):
        spec = self._get_spec()
        assert spec.primary_topic_slug == "fitness"

    def test_participants_shape_is_solo(self):
        spec = self._get_spec()
        assert spec.participants_shape == "solo"

    def test_cross_topic_policy_is_peek(self):
        spec = self._get_spec()
        assert spec.cross_topic_policy == "peek"

    def test_allowlist_contains_all_7_commitment_event_tools(self):
        spec = self._get_spec()
        assert spec.tool_allowlist is not None
        hector_tools = {
            "list_commitments",
            "create_commitment",
            "update_commitment",
            "close_commitment",
            "log_event",
            "list_events",
            "get_adherence",
        }
        in_allowlist = hector_tools & spec.tool_allowlist
        assert len(in_allowlist) == 7, (
            f"Expected all 7 Hector tools in allowlist, got: {in_allowlist}"
        )

    def test_allowlist_excludes_pregnancy_tools(self):
        spec = self._get_spec()
        assert spec.tool_allowlist is not None
        assert "set_pregnancy_edd" not in spec.tool_allowlist
        assert "correct_pregnancy_edd" not in spec.tool_allowlist
        assert "end_pregnancy" not in spec.tool_allowlist

    def test_allowlist_excludes_bridge_escalate_tools(self):
        spec = self._get_spec()
        assert spec.tool_allowlist is not None
        bridge_tools = {
            "create_bridge_candidate",
            "update_bridge_candidate",
            "send_bridge_candidate",
            "list_bridge_candidates",
            "escalate_to_partner",
        }
        found = spec.tool_allowlist & bridge_tools
        assert not found, f"Hector should not have bridge tools: {found}"

    def test_system_prompt_renders_from_spec(self):
        """The BotSpec.render_system_prompt must return a non-empty string."""
        spec = self._get_spec()
        from app.models.user import User
        from uuid import uuid4

        user = User(
            id=uuid4(),
            name="Alex",
            phone="+155****0100",
            timezone="UTC",
            onboarding_state="completed",
        )
        result = spec.render_system_prompt(
            assistant_name="Hector",
            user=user,
            partner=None,
            prompt_version="v1",
        )
        assert isinstance(result, str)
        assert len(result) > 500


class TestNonHectorBotsExcludeHectorTools:
    """Coach, Tante Rosi, and Mediator must NOT have commitment/event tools."""

    @pytest.fixture(autouse=True)
    def _register_staging(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("STAGING", "1")
        import app.bots.registry as _reg

        _reg._STAGING_BOTS_REGISTERED = False
        _reg._maybe_register_staging_bots()

    _HECTOR_TOOLS = frozenset({
        "list_commitments",
        "create_commitment",
        "update_commitment",
        "close_commitment",
        "log_event",
        "list_events",
        "get_adherence",
    })

    def test_coach_lacks_hector_tools(self):
        from app.bots.registry import get_bot_spec

        spec = get_bot_spec("coach")
        if spec.tool_allowlist is not None:
            found = spec.tool_allowlist & self._HECTOR_TOOLS
            assert not found, f"Coach should not have Hector tools: {found}"

    def test_tante_rosi_lacks_hector_tools(self):
        from app.bots.registry import get_bot_spec

        spec = get_bot_spec("tante_rosi")
        if spec.tool_allowlist is not None:
            found = spec.tool_allowlist & self._HECTOR_TOOLS
            assert not found, f"Tante Rosi should not have Hector tools: {found}"

    def test_mediator_lacks_hector_tools(self):
        from app.bots.registry import get_bot_spec

        spec = get_bot_spec("mediator")
        if spec.tool_allowlist is not None:
            found = spec.tool_allowlist & self._HECTOR_TOOLS
            assert not found, f"Mediator should not have Hector tools: {found}"


class TestBotExclusiveToolsFilter:
    """BOT_EXCLUSIVE_TOOLS filter in _step_allowed()."""

    _HECTOR_TOOLS = frozenset({
        "list_commitments",
        "create_commitment",
        "update_commitment",
        "close_commitment",
        "log_event",
        "list_events",
        "get_adherence",
    })

    def _make_mock_ctx(self, bot_id: str):
        from unittest.mock import MagicMock
        from app.bots.base import BotSpec, ReadScopes, WriteScopes

        ctx = MagicMock()
        ctx.current_step = "record"
        ctx.bot_id = bot_id
        ctx.bot_spec = BotSpec(
            bot_id=bot_id,
            prompt_renderer=lambda *a, **kw: "test",
            step_instructions={
                "read": "r",
                "consult": "c",
                "respond": "resp",
                "record": "rec",
                "schedule": "sch",
                "done": "d",
            },
            display_name=bot_id.title(),
            primary_topic_slug="fitness" if bot_id == "hector" else "relationship",
            participants_shape="solo" if bot_id == "hector" else "dyad",
            read_scopes=ReadScopes(
                topics=frozenset({"own"}),
                allow_cross_topic_peek=True,
                allow_cross_topic_status_injection=False,
            ),
            write_scopes=WriteScopes(topics=frozenset({"own"})),
            tool_allowlist=(
                self._HECTOR_TOOLS if bot_id == "hector" else None
            ),
        )
        return ctx

    def test_coach_step_allowed_removes_hector_tools(self):
        """Coach _step_allowed must not include Hector-exclusive tools."""
        from app.services.tools.registry import _step_allowed

        ctx = self._make_mock_ctx("coach")
        allowed = _step_allowed(ctx)
        found = allowed & self._HECTOR_TOOLS
        assert not found, f"Coach _step_allowed should exclude Hector tools: {found}"

    def test_tante_rosi_step_allowed_removes_hector_tools(self):
        """Tante Rosi _step_allowed must not include Hector-exclusive tools."""
        from app.services.tools.registry import _step_allowed

        ctx = self._make_mock_ctx("tante_rosi")
        ctx.bot_id = "tante_rosi"
        allowed = _step_allowed(ctx)
        found = allowed & self._HECTOR_TOOLS
        assert not found, (
            f"Tante Rosi _step_allowed should exclude Hector tools: {found}"
        )

    def test_mediator_step_allowed_removes_hector_tools(self):
        """Mediator _step_allowed must not include Hector-exclusive tools."""
        from app.services.tools.registry import _step_allowed

        ctx = self._make_mock_ctx("mediator")
        ctx.bot_id = "mediator"
        allowed = _step_allowed(ctx)
        found = allowed & self._HECTOR_TOOLS
        assert not found, (
            f"Mediator _step_allowed should exclude Hector tools: {found}"
        )

    def test_hector_step_allowed_keeps_hector_tools(self):
        """Hector _step_allowed MUST include Hector-exclusive tools."""
        from app.services.tools.registry import _step_allowed

        ctx = self._make_mock_ctx("hector")
        allowed = _step_allowed(ctx)
        found = allowed & self._HECTOR_TOOLS
        assert len(found) == 7, (
            f"Hector _step_allowed should include all 7 tools, got: {found}"
        )
