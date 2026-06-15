"""Registration tests for the SuperPOM bot.

T2: Assert get_bot_spec('superpom') under STAGING=1 resolves to a solo spec,
and the DB-gated populate function registers the spec when a bots-table
row exists.  Resets _STAGING_BOTS_REGISTERED so each test sees a clean
registry state.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.bots.ids import SUPERPOM_BOT_ID


# ── Helpers ──────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_staging_registered() -> None:
    """Reset the staging guard so each test starts with a clean registry."""
    import app.bots.registry as reg

    reg._STAGING_BOTS_REGISTERED = False


@pytest.fixture
def _clear_staging_bots() -> None:
    """Remove all staging-registered bots from BOT_SPECS after test."""
    yield
    import app.bots.registry as reg

    for bid in list(reg.BOT_SPECS):
        if bid != "mediator":
            del reg.BOT_SPECS[bid]


# ── Staging registration (STAGING=1) ────────────────────────────────────


@pytest.mark.skipif(
    os.environ.get("STAGING", "").lower() not in {"1", "true", "yes"},
    reason="requires STAGING=1",
)
class TestSuperPOMStagingRegistration:
    """get_bot_spec('superpom') under STAGING=1 must resolve correctly."""

    def test_get_bot_spec_superpom_resolves(self, _clear_staging_bots):
        from app.bots.registry import get_bot_spec

        spec = get_bot_spec(SUPERPOM_BOT_ID)
        assert spec is not None
        assert spec.bot_id == "superpom"
        assert spec.display_name == "SuperPOM"

    def test_primary_topic_slug_is_superpom(self, _clear_staging_bots):
        from app.bots.registry import get_bot_spec

        spec = get_bot_spec(SUPERPOM_BOT_ID)
        assert spec.primary_topic_slug == "superpom"

    def test_participants_shape_is_solo(self, _clear_staging_bots):
        from app.bots.registry import get_bot_spec

        spec = get_bot_spec(SUPERPOM_BOT_ID)
        assert spec.participants_shape == "solo"

    def test_read_scopes_own_topic(self, _clear_staging_bots):
        from app.bots.registry import get_bot_spec

        spec = get_bot_spec(SUPERPOM_BOT_ID)
        assert spec.read_scopes.topics == frozenset({"own"})
        assert spec.read_scopes.allow_cross_topic_peek is True
        assert spec.read_scopes.allow_cross_topic_status_injection is False

    def test_write_scopes_own_topic(self, _clear_staging_bots):
        from app.bots.registry import get_bot_spec

        spec = get_bot_spec(SUPERPOM_BOT_ID)
        assert spec.write_scopes.topics == frozenset({"own"})

    def test_cross_topic_policy_is_peek(self, _clear_staging_bots):
        from app.bots.registry import get_bot_spec

        spec = get_bot_spec(SUPERPOM_BOT_ID)
        assert spec.cross_topic_policy == "peek"

    def test_tool_allowlist_includes_orientation_tools(self, _clear_staging_bots):
        from app.bots.registry import get_bot_spec

        spec = get_bot_spec(SUPERPOM_BOT_ID)
        assert spec.tool_allowlist is not None
        expected = {
            "list_orientation_items",
            "get_orientation_item",
            "create_orientation_item",
            "update_orientation_item",
            "review_orientation_item",
            "close_orientation_item",
            "link_orientation_evidence",
        }
        for tool in expected:
            assert tool in spec.tool_allowlist, f"Missing orientation tool: {tool}"

    def test_tool_allowlist_excludes_domain_tools(self, _clear_staging_bots):
        from app.bots.registry import get_bot_spec

        spec = get_bot_spec(SUPERPOM_BOT_ID)
        assert spec.tool_allowlist is not None
        excluded = {
            # dyad/bridge
            "create_bridge_candidate",
            "escalate_to_partner",
            "recent_activity",
            "update_bridge_candidate",
            "send_bridge_candidate",
            "list_bridge_candidates",
            # pregnancy
            "set_pregnancy_edd",
            "correct_pregnancy_edd",
            "end_pregnancy",
            # commitment/event
            "list_commitments",
            "create_commitment",
            "update_commitment",
            "close_commitment",
            "log_event",
            "get_adherence",
            "list_events",
            # live-plan
            "read_conversation_plan",
            "list_conversation_plans",
            "create_conversation_plan",
            "update_conversation_plan",
            # coach-only
            "set_topic_status",
        }
        for tool in excluded:
            assert tool not in spec.tool_allowlist, f"Should be excluded: {tool}"
        assert "search_messages" in spec.tool_allowlist
        assert "add_memory" in spec.tool_allowlist

    def test_bot_spec_version_is_set(self, _clear_staging_bots):
        from app.bots.registry import get_bot_spec

        spec = get_bot_spec(SUPERPOM_BOT_ID)
        assert spec.bot_spec_version == "1.0.0"

    def test_compass_enabled_is_true(self, _clear_staging_bots):
        from app.bots.registry import get_bot_spec

        spec = get_bot_spec(SUPERPOM_BOT_ID)
        assert spec.compass_enabled is True


# ── solo spec under staging ─────────────────────────────────────────────


@pytest.mark.skipif(
    os.environ.get("STAGING", "").lower() not in {"1", "true", "yes"},
    reason="requires STAGING=1",
)
class TestSuperPOMIsSoloSpec:
    """SuperPOM resolves to a solo spec (not dyad) under staging."""

    def test_is_solo_not_dyad(self, _clear_staging_bots):
        from app.bots.registry import get_bot_spec

        spec = get_bot_spec(SUPERPOM_BOT_ID)
        assert spec.participants_shape == "solo"
        assert spec.participants_shape != "dyad"


# ── DB-gated production registration ────────────────────────────────────


class TestPopulateSuperPOMFromDB:
    """populate_superpom_spec_from_db must register spec when row exists."""

    async def test_registers_when_row_exists(self):
        import app.bots.registry as reg

        # Ensure clean state
        reg.BOT_SPECS.pop("superpom", None)

        pool = AsyncMock()
        pool.fetchrow = AsyncMock(return_value={"?column?": 1})

        await reg.populate_superpom_spec_from_db(pool)

        assert "superpom" in reg.BOT_SPECS
        spec = reg.BOT_SPECS["superpom"]
        assert spec.bot_id == "superpom"
        assert spec.display_name == "SuperPOM"
        assert spec.primary_topic_slug == "superpom"
        assert spec.participants_shape == "solo"

    async def test_noop_when_row_missing(self):
        import app.bots.registry as reg

        reg.BOT_SPECS.pop("superpom", None)

        pool = AsyncMock()
        pool.fetchrow = AsyncMock(return_value=None)

        await reg.populate_superpom_spec_from_db(pool)

        assert "superpom" not in reg.BOT_SPECS

    async def test_noop_when_query_fails(self):
        import app.bots.registry as reg

        reg.BOT_SPECS.pop("superpom", None)

        pool = AsyncMock()
        pool.fetchrow = AsyncMock(side_effect=RuntimeError("db down"))

        await reg.populate_superpom_spec_from_db(pool)

        # Should not raise and should not register
        assert "superpom" not in reg.BOT_SPECS


# ── Staging guard reset test ────────────────────────────────────────────


class TestStagingGuardReset:
    """_STAGING_BOTS_REGISTERED reset allows re-registration."""

    def test_reset_allows_reregistration(self):
        import app.bots.registry as reg

        # Simulate: first call registers
        reg._STAGING_BOTS_REGISTERED = False
        # Force STAGING=1 for this test
        old_staging = os.environ.get("STAGING")
        os.environ["STAGING"] = "1"
        try:
            reg._maybe_register_staging_bots()
            assert reg._STAGING_BOTS_REGISTERED is True
            assert "superpom" in reg.BOT_SPECS

            # Clean up: remove superpom
            del reg.BOT_SPECS["superpom"]

            # Reset guard
            reg._STAGING_BOTS_REGISTERED = False

            # Second call should re-register
            reg._maybe_register_staging_bots()
            assert "superpom" in reg.BOT_SPECS
        finally:
            if old_staging is not None:
                os.environ["STAGING"] = old_staging
            else:
                os.environ.pop("STAGING", None)
            # Clean up
            reg.BOT_SPECS.pop("superpom", None)
            reg._STAGING_BOTS_REGISTERED = False


# ── partner=None render safety ──────────────────────────────────────────



# ── Orientation tools in allowlist (T3) ──────────────────────────────


@pytest.mark.skipif(
    os.environ.get("STAGING", "").lower() not in {"1", "true", "yes"},
    reason="requires STAGING=1",
)
class TestSuperPOMOrientationAllowlist:
    """Orientation tools are available and domain tools are excluded."""

    def test_orientation_read_tools_present(self, _clear_staging_bots):
        from app.bots.registry import get_bot_spec

        spec = get_bot_spec(SUPERPOM_BOT_ID)
        assert spec.tool_allowlist is not None
        for tool in ("list_orientation_items", "get_orientation_item"):
            assert tool in spec.tool_allowlist, f"Missing: {tool}"

    def test_orientation_write_tools_present(self, _clear_staging_bots):
        from app.bots.registry import get_bot_spec

        spec = get_bot_spec(SUPERPOM_BOT_ID)
        assert spec.tool_allowlist is not None
        for tool in (
            "create_orientation_item",
            "update_orientation_item",
            "review_orientation_item",
            "close_orientation_item",
            "link_orientation_evidence",
        ):
            assert tool in spec.tool_allowlist, f"Missing: {tool}"

    def test_commitment_tools_excluded(self, _clear_staging_bots):
        from app.bots.registry import get_bot_spec

        spec = get_bot_spec(SUPERPOM_BOT_ID)
        assert spec.tool_allowlist is not None
        for tool in (
            "list_commitments",
            "create_commitment",
            "update_commitment",
            "close_commitment",
            "log_event",
            "get_adherence",
            "list_events",
        ):
            assert tool not in spec.tool_allowlist, (
                f"Commitment tool should be excluded: {tool}"
            )

    def test_live_plan_tools_excluded(self, _clear_staging_bots):
        from app.bots.registry import get_bot_spec

        spec = get_bot_spec(SUPERPOM_BOT_ID)
        assert spec.tool_allowlist is not None
        for tool in (
            "read_conversation_plan",
            "list_conversation_plans",
            "create_conversation_plan",
            "update_conversation_plan",
        ):
            assert tool not in spec.tool_allowlist, (
                f"Live-plan tool should be excluded: {tool}"
            )

    def test_memory_and_observation_tools_present(self, _clear_staging_bots):
        from app.bots.registry import get_bot_spec

        spec = get_bot_spec(SUPERPOM_BOT_ID)
        assert spec.tool_allowlist is not None
        for tool in (
            "add_memory",
            "get_memories",
            "log_observation",
            "get_observations",
            "add_distillation",
            "add_oob",
            "schedule_checkin",
            "search",
        ):
            assert tool in spec.tool_allowlist, f"Missing general tool: {tool}"


class TestSuperPOMRenderPartnerNone:
    """BotSpec.render_system_prompt(partner=None) must succeed."""

    def test_render_partner_none_succeeds(self):
        from uuid import UUID

        from app.bots.superpom import build_superpom_spec
        from app.models.user import User

        spec = build_superpom_spec()
        user = User(
            id=UUID("00000000-0000-4000-8000-000000000020"),
            name="TestUser",
            phone="+155****0200",
            timezone="America/New_York",
            onboarding_state="completed",
        )

        result = spec.render_system_prompt(
            assistant_name="SuperPOM",
            user=user,
            partner=None,
            prompt_version="v1",
        )
        assert isinstance(result, str)
        assert len(result) > 0
