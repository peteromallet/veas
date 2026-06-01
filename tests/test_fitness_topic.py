"""Migration + registration tests for the fitness topic and Hector bot.

T17: Assert that after migrations apply, the fitness topic row and the
hector bot row both exist in the DB. Assert get_bot_spec('hector') under
STAGING=1 resolves correctly, and BotSpec.render_system_prompt(partner=None)
succeeds for both Hector and Tante Rosi.
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID

import pytest

from app.bots.base import BotSpec
from app.bots.ids import HECTOR_BOT_ID, TANTE_ROSI_BOT_ID
from app.models.user import User

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def _read_migration(num: int, suffix: str = "") -> str:
    pattern = f"{num:04d}_fitness_topic{suffix}.sql"
    return (MIGRATIONS_DIR / pattern).read_text()


def _make_test_user(name: str = "TestUser") -> User:
    return User(
        id=UUID("00000000-0000-4000-8000-000000000010"),
        name=name,
        phone="+15555550100",
        timezone="America/New_York",
        onboarding_state="completed",
    )


# ── Migration file content checks ────────────────────────────────────────

class TestFitnessTopicMigration:
    """0037_fitness_topic.sql must insert the fitness topic and Hector bot row."""

    def test_migration_file_exists(self):
        assert (MIGRATIONS_DIR / "0037_fitness_topic.sql").exists()

    def test_down_migration_exists(self):
        assert (MIGRATIONS_DIR / "0037_fitness_topic.down.sql").exists()

    def test_inserts_fitness_topic(self):
        sql = _read_migration(37)
        assert "INSERT INTO mediator.topics" in sql
        assert "'fitness'" in sql
        assert "ON CONFLICT (slug) DO NOTHING" in sql

    def test_inserts_hector_bot_row(self):
        sql = _read_migration(37)
        assert "INSERT INTO mediator.bots" in sql
        assert "'hector'" in sql
        assert "ON CONFLICT (id) DO NOTHING" in sql

    def test_down_deletes_in_fk_order(self):
        sql = _read_migration(37, ".down")
        # Bot row must be deleted before topic row (FK order)
        assert "DELETE FROM mediator.bots" in sql
        assert "DELETE FROM mediator.topics" in sql
        # Bot delete must come first in the text
        bot_pos = sql.index("DELETE FROM mediator.bots")
        topic_pos = sql.index("DELETE FROM mediator.topics")
        assert bot_pos < topic_pos, "Bot row must be deleted before topic (FK order)"


# ── BotSpec registration checks (STAGING=1) ──────────────────────────────

@pytest.mark.skipif(
    os.environ.get("STAGING", "").lower() not in {"1", "true", "yes"},
    reason="requires STAGING=1",
)
class TestHectorBotSpecRegistration:
    """get_bot_spec('hector') under STAGING=1 must resolve correctly."""

    def test_get_bot_spec_hector_resolves(self):
        from app.bots.registry import get_bot_spec

        spec = get_bot_spec(HECTOR_BOT_ID)
        assert spec is not None
        assert spec.bot_id == "hector"
        assert spec.display_name == "Hector"

    def test_primary_topic_slug_is_fitness(self):
        from app.bots.registry import get_bot_spec

        spec = get_bot_spec(HECTOR_BOT_ID)
        assert spec.primary_topic_slug == "fitness"

    def test_participants_shape_is_solo(self):
        from app.bots.registry import get_bot_spec

        spec = get_bot_spec(HECTOR_BOT_ID)
        assert spec.participants_shape == "solo"

    def test_read_scopes_own_topic(self):
        from app.bots.registry import get_bot_spec

        spec = get_bot_spec(HECTOR_BOT_ID)
        assert spec.read_scopes.topics == frozenset({"own"})
        assert spec.read_scopes.allow_cross_topic_peek is True
        assert spec.read_scopes.allow_cross_topic_status_injection is False

    def test_write_scopes_own_topic(self):
        from app.bots.registry import get_bot_spec

        spec = get_bot_spec(HECTOR_BOT_ID)
        assert spec.write_scopes.topics == frozenset({"own"})

    def test_tool_allowlist_includes_all_seven(self):
        from app.bots.registry import get_bot_spec

        spec = get_bot_spec(HECTOR_BOT_ID)
        assert spec.tool_allowlist is not None
        expected = {
            "create_commitment",
            "update_commitment",
            "close_commitment",
            "log_event",
            "list_commitments",
            "list_events",
            "get_adherence",
        }
        for tool in expected:
            assert tool in spec.tool_allowlist, f"Missing tool: {tool}"

    def test_tool_allowlist_excludes_bridge_escalate(self):
        from app.bots.registry import get_bot_spec

        spec = get_bot_spec(HECTOR_BOT_ID)
        assert spec.tool_allowlist is not None
        excluded = {
            "create_bridge_candidate",
            "escalate_to_partner",
            "recent_activity",
            "set_pregnancy_edd",
            "correct_pregnancy_edd",
            "end_pregnancy",
        }
        for tool in excluded:
            assert tool not in spec.tool_allowlist, f"Should be excluded: {tool}"
        assert "search_messages" in spec.tool_allowlist


# ── partner=None render safety (integration check) ───────────────────────

class TestSoloRenderPartnerNone:
    """BotSpec.render_system_prompt(partner=None) must succeed for solo bots."""

    def test_hector_render_partner_none_succeeds(self):
        from app.bots.hector import build_hector_spec

        spec = build_hector_spec()
        user = _make_test_user("Alice")

        result = spec.render_system_prompt(
            assistant_name="Hector",
            user=user,
            partner=None,
            prompt_version="v1",
        )
        assert isinstance(result, str)
        assert len(result) > 0

    def test_tante_rosi_render_partner_none_succeeds(self):
        from app.bots.tante_rosi import build_tante_rosi_spec

        spec = build_tante_rosi_spec()
        user = _make_test_user("Anna")

        result = spec.render_system_prompt(
            assistant_name="Tante Rosi",
            user=user,
            partner=None,
            prompt_version="v1",
        )
        assert isinstance(result, str)
        assert len(result) > 0
