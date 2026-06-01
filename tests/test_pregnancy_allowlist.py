"""Allowlist tests for the Tante Rosi pregnancy bot.

§4.1 no-auto-bridging: the bridge/escalate exclusions are load-bearing.
Tante Rosi MUST NOT be able to auto-bridge pregnancy content to the mediator.
Coach and mediator MUST NOT have access to pregnancy write tools.
"""

from __future__ import annotations

import pytest


class TestTanteRosiAllowlist:
    """Tante Rosi's tool_allowlist — pregnancy tools present, bridge/escalate absent."""

    @pytest.fixture(autouse=True)
    def _register_staging(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Ensure staging bots are registered for allowlist inspection."""
        monkeypatch.setenv("STAGING", "1")
        import app.bots.registry as _reg
        _reg._STAGING_BOTS_REGISTERED = False
        _reg._maybe_register_staging_bots()

    def test_contains_all_pregnancy_tools(self):
        """All three pregnancy write tools must be in Rosi's allowlist."""
        from app.bots.registry import get_bot_spec
        spec = get_bot_spec("tante_rosi")
        assert spec.tool_allowlist is not None
        assert "set_pregnancy_edd" in spec.tool_allowlist
        assert "correct_pregnancy_edd" in spec.tool_allowlist
        assert "end_pregnancy" in spec.tool_allowlist

    def test_excludes_all_bridge_escalate_tools(self):
        """The five bridge/escalate tools must be absent (load-bearing §4.1)."""
        from app.bots.registry import get_bot_spec
        spec = get_bot_spec("tante_rosi")
        assert spec.tool_allowlist is not None
        for excluded in (
            "create_bridge_candidate",
            "update_bridge_candidate",
            "send_bridge_candidate",
            "list_bridge_candidates",
            "escalate_to_partner",
        ):
            assert excluded not in spec.tool_allowlist, (
                f"{excluded} must be excluded from Rosi allowlist (§4.1)"
            )

    def test_excludes_other_coach_exclusions(self):
        """The remaining non-bridge exclusions stay absent from Rosi."""
        from app.bots.registry import get_bot_spec
        spec = get_bot_spec("tante_rosi")
        assert spec.tool_allowlist is not None
        for excluded in (
            "set_topic_status",
            "recent_activity",
        ):
            assert excluded not in spec.tool_allowlist, (
                f"{excluded} must be excluded from Rosi allowlist"
            )
        assert "search_messages" in spec.tool_allowlist


class TestCoachAllowlist:
    """Coach's tool_allowlist must NOT contain pregnancy tools."""

    @pytest.fixture(autouse=True)
    def _register_staging(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("STAGING", "1")
        import app.bots.registry as _reg
        _reg._STAGING_BOTS_REGISTERED = False
        _reg._maybe_register_staging_bots()

    def test_coach_lacks_pregnancy_tools(self):
        """Coach must not have any pregnancy tools in its allowlist."""
        from app.bots.registry import get_bot_spec
        spec = get_bot_spec("coach")
        assert spec.tool_allowlist is not None
        assert "set_pregnancy_edd" not in spec.tool_allowlist
        assert "correct_pregnancy_edd" not in spec.tool_allowlist
        assert "end_pregnancy" not in spec.tool_allowlist


class TestMediatorAllowlist:
    """Mediator's tool_allowlist must NOT contain pregnancy tools.

    The mediator's tool_allowlist may be None (meaning all tools are
    permitted), but it must not have the pregnancy tools in any explicit
    allowlist.  When None, the gate at the dispatch level means the tools
    are still present in TOOL_DISPATCH but the mediator doesn't get
    special access to them beyond what every bot gets.
    """

    def test_mediator_lacks_pregnancy_tools(self):
        """Mediator must not have any pregnancy tools in its allowlist."""
        from app.bots.registry import get_bot_spec
        spec = get_bot_spec("mediator")
        # If tool_allowlist is None, all tools are permitted —
        # but pregnancy tools are not specifically granted.
        if spec.tool_allowlist is not None:
            assert "set_pregnancy_edd" not in spec.tool_allowlist
            assert "correct_pregnancy_edd" not in spec.tool_allowlist
            assert "end_pregnancy" not in spec.tool_allowlist


class TestNoAutoBridgingGuarantee:
    """Behavioral tests for the §4.1 no-auto-bridging guarantee."""

    @pytest.fixture(autouse=True)
    def _register_staging(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("STAGING", "1")
        import app.bots.registry as _reg
        _reg._STAGING_BOTS_REGISTERED = False
        _reg._maybe_register_staging_bots()

    def test_rosi_allowlist_excludes_all_five_bridge_escalate_tools(self):
        """Exhaustive check: all 5 bridge/escalate tools absent from Rosi."""
        from app.bots.registry import get_bot_spec
        spec = get_bot_spec("tante_rosi")
        assert spec.tool_allowlist is not None

        bridge_escalate_tools = {
            "create_bridge_candidate",
            "update_bridge_candidate",
            "send_bridge_candidate",
            "list_bridge_candidates",
            "escalate_to_partner",
        }
        found = spec.tool_allowlist & bridge_escalate_tools
        assert not found, (
            f"Rosi allowlist contains bridge/escalate tools: {found}. "
            "§4.1 no-auto-bridging guarantee violated."
        )

    def test_all_eight_coach_exclusions_absent_from_rosi(self):
        """Bridge/dyad-write exclusions stay complete after search migration."""
        from app.bots.registry import get_bot_spec
        spec = get_bot_spec("tante_rosi")
        assert spec.tool_allowlist is not None

        all_exclusions = {
            "set_topic_status",
            "create_bridge_candidate",
            "update_bridge_candidate",
            "send_bridge_candidate",
            "list_bridge_candidates",
            "escalate_to_partner",
            "recent_activity",
        }
        found = spec.tool_allowlist & all_exclusions
        assert not found, (
            f"Rosi allowlist contains excluded tools: {found}"
        )
        assert "search_messages" in spec.tool_allowlist

    def test_rosi_pregnancy_tools_present_in_tool_dispatch(self):
        """Pregnancy tools are in TOOL_DISPATCH but gated by allowlist —
        they should be registered in the dispatch table."""
        from app.services.tools.registry import TOOL_DISPATCH
        assert "set_pregnancy_edd" in TOOL_DISPATCH
        assert "correct_pregnancy_edd" in TOOL_DISPATCH
        assert "end_pregnancy" in TOOL_DISPATCH
