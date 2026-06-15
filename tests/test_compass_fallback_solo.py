"""T5 tests: Compass fallback in solo hot context rendering.

When Compass building fails (unresolvable topic, builder error), the solo
hot context renderer must log a warning, continue without `## Compass`, and
preserve explicit topic scoping without using an 'all' sentinel.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from app.models.user import User
from app.services.hot_context_solo import (
    build_hot_context_solo,
    render_hot_context_solo,
)
from app.bots.registry import get_relationship_topic_id


pytestmark = pytest.mark.anyio


async def _build_compass_enabled(
    fake_pool,
    user,
    *,
    bot_id: str = "superpom",
    primary_topic_slug: str = "superpom",
) -> tuple:
    """Build solo hot context with compass_enabled=True."""
    fake_pool.users[user.id] = {
        "id": user.id,
        "name": user.name,
        "phone": user.phone,
        "timezone": user.timezone,
    }
    hc = await build_hot_context_solo(
        fake_pool,
        user,
        triggering_message_ids=[],
        trigger_metadata={"kind": "inbound"},
        primary_topic_id=get_relationship_topic_id(),
        bot_id=bot_id,
        compass_enabled=True,
        allowed_compass_topic_slugs=frozenset({"own"}),
        primary_topic_slug=primary_topic_slug,
    )
    rendered = render_hot_context_solo(hc)
    return hc, rendered


class TestCompassFallbackSolo:
    """Compass fallback in solo hot context — T5."""

    async def test_no_all_sentinel_raises_value_error(self, fake_pool):
        """'all' sentinel must still raise ValueError (not silently suppressed)."""
        user = User(uuid4(), "Pom", "15555550100", "UTC")
        fake_pool.users[user.id] = {
            "id": user.id,
            "name": user.name,
            "phone": user.phone,
            "timezone": user.timezone,
        }
        with pytest.raises(ValueError, match="all.*sentinel"):
            await build_hot_context_solo(
                fake_pool,
                user,
                triggering_message_ids=[],
                trigger_metadata={"kind": "inbound"},
                primary_topic_id=get_relationship_topic_id(),
                bot_id="superpom",
                compass_enabled=True,
                allowed_compass_topic_slugs=frozenset({"all"}),
                primary_topic_slug="superpom",
            )

    async def test_compass_builder_error_does_not_raise(self, fake_pool):
        """When Compass building fails, rendering continues without raising."""
        user = User(uuid4(), "Pom", "15555550100", "UTC")
        fake_pool.users[user.id] = {
            "id": user.id,
            "name": user.name,
            "phone": user.phone,
            "timezone": user.timezone,
        }

        # Simulate a Compass builder failure at the source:
        # patch build_compass_snapshot in its original module.
        with patch(
            "app.services.compass.build_compass_snapshot",
            side_effect=RuntimeError("Compass builder error"),
        ):
            hc, rendered = await _build_compass_enabled(fake_pool, user)

        # Rendering must succeed — no exception propagates.
        assert isinstance(rendered, str)
        assert len(rendered) > 0

        # Compass section must be absent when building failed.
        assert "## Compass" not in rendered

        # The rest of the hot context must still be intact.
        assert "## You" in rendered
        assert "Pom" in rendered

    async def test_compass_builder_error_logs_warning(self, fake_pool):
        """When Compass building fails, a warning is logged."""
        user = User(uuid4(), "Pom", "15555550100", "UTC")
        fake_pool.users[user.id] = {
            "id": user.id,
            "name": user.name,
            "phone": user.phone,
            "timezone": user.timezone,
        }

        with patch(
            "app.services.compass.build_compass_snapshot",
            side_effect=RuntimeError("Compass builder error"),
        ):
            with patch("app.services.hot_context_solo.logging.getLogger") as mock_get_logger:
                mock_logger = MagicMock()
                mock_get_logger.return_value = mock_logger

                hc, rendered = await _build_compass_enabled(fake_pool, user)

                # Warning must have been logged.
                mock_logger.warning.assert_called_once()
                call_args = mock_logger.warning.call_args
                assert "Compass snapshot build failed" in call_args[0][0]

    async def test_compass_snapshot_none_when_builder_fails(self, fake_pool):
        """compass_snapshot is None when Compass building fails."""
        user = User(uuid4(), "Pom", "15555550100", "UTC")
        fake_pool.users[user.id] = {
            "id": user.id,
            "name": user.name,
            "phone": user.phone,
            "timezone": user.timezone,
        }

        with patch(
            "app.services.compass.build_compass_snapshot",
            side_effect=RuntimeError("Compass builder error"),
        ):
            hc, _ = await _build_compass_enabled(fake_pool, user)

        assert hc.compass_snapshot is None

    async def test_own_sentinel_requires_primary_topic_slug(self, fake_pool):
        """'own' sentinel without primary_topic_slug raises ValueError."""
        user = User(uuid4(), "Pom", "15555550100", "UTC")
        fake_pool.users[user.id] = {
            "id": user.id,
            "name": user.name,
            "phone": user.phone,
            "timezone": user.timezone,
        }
        with pytest.raises(ValueError, match="own.*primary_topic_slug"):
            await build_hot_context_solo(
                fake_pool,
                user,
                triggering_message_ids=[],
                trigger_metadata={"kind": "inbound"},
                primary_topic_id=get_relationship_topic_id(),
                bot_id="superpom",
                compass_enabled=True,
                allowed_compass_topic_slugs=frozenset({"own"}),
                primary_topic_slug=None,
            )
