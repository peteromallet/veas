"""S1 tests: solo hot context surfaces partner identity-only block.

Invariant 1: the `## Your Partner` block contains ONLY identity fields
(name, id, timezone) + the recipient-side `partner_sharing_state` for
this bot. NO content from the partner's thread leaks in.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.bots.registry import get_relationship_topic_id
from app.models.user import User
from app.services.hot_context_solo import (
    build_hot_context_solo,
    render_hot_context_solo,
)

pytestmark = pytest.mark.anyio


async def _build_minimal(fake_pool, user, *, bot_id="tante_rosi"):
    fake_pool.users[user.id] = {
        "id": user.id,
        "name": user.name,
        "phone": user.phone,
        "timezone": user.timezone,
    }
    return await build_hot_context_solo(
        fake_pool,
        user,
        triggering_message_ids=[],
        trigger_metadata={"kind": "inbound"},
        primary_topic_id=get_relationship_topic_id(),
        bot_id=bot_id,
    )


async def test_solo_user_with_dyad_partner_renders_partner_identity(fake_pool):
    user = User(uuid4(), "Pom", "15555550100", "UTC")
    partner = User(uuid4(), "Hannah", "15555550101", "Europe/Berlin")
    fake_pool.users[partner.id] = {
        "id": partner.id,
        "name": partner.name,
        "phone": partner.phone,
        "timezone": partner.timezone,
    }
    fake_pool.dyad_partners[user.id] = partner.id
    # Partner has not yet opted in for tante_rosi → state = pending
    hc = await _build_minimal(fake_pool, user, bot_id="tante_rosi")
    rendered = render_hot_context_solo(hc)
    assert "## Your Partner" in rendered
    assert "Hannah" in rendered
    assert "Europe/Berlin" in rendered
    assert "partner_sharing_state_for_this_bot: pending" in rendered
    # Identity-only — no content. Add some unrelated content to the
    # partner's thread and verify it does NOT appear.
    fake_pool.memories[uuid4()] = {
        "id": uuid4(),
        "about_user_id": partner.id,
        "content": "PARTNER_PRIVATE_MEMORY_CONTENT",
        "related_theme_ids": [],
        "status": "active",
    }
    assert "PARTNER_PRIVATE_MEMORY_CONTENT" not in rendered


async def test_solo_user_with_no_dyad_partner_omits_block(fake_pool):
    user = User(uuid4(), "Pom", "15555550100", "UTC")
    hc = await _build_minimal(fake_pool, user, bot_id="tante_rosi")
    rendered = render_hot_context_solo(hc)
    assert "## Your Partner" not in rendered


async def test_partner_opt_in_surfaces_in_state_field(fake_pool):
    user = User(uuid4(), "Pom", "15555550100", "UTC")
    partner = User(uuid4(), "Hannah", "15555550101", "UTC")
    fake_pool.users[partner.id] = {
        "id": partner.id,
        "name": partner.name,
        "phone": partner.phone,
        "timezone": partner.timezone,
    }
    fake_pool.dyad_partners[user.id] = partner.id
    fake_pool.user_bot_state[(partner.id, "tante_rosi")] = {
        "user_id": partner.id,
        "bot_id": "tante_rosi",
        "partner_share": "opt_in",
    }
    hc = await _build_minimal(fake_pool, user, bot_id="tante_rosi")
    rendered = render_hot_context_solo(hc)
    assert "partner_sharing_state_for_this_bot: opt_in" in rendered


async def test_solo_renders_outgoing_mediated_issues_when_dyad_partner_exists(fake_pool):
    from uuid import uuid4 as _uuid4
    from datetime import datetime, UTC as _UTC
    user = User(_uuid4(), "Pom", "15555550100", "UTC")
    partner = User(_uuid4(), "Hannah", "15555550101", "UTC")
    fake_pool.users[user.id] = {"id": user.id, "name": user.name, "phone": user.phone, "timezone": user.timezone}
    fake_pool.users[partner.id] = {"id": partner.id, "name": partner.name, "phone": partner.phone, "timezone": partner.timezone}
    fake_pool.dyad_partners[user.id] = partner.id
    bridge_id = _uuid4()
    fake_pool.bridge_candidates[bridge_id] = {
        "id": bridge_id,
        "source_user_id": user.id,
        "target_user_id": partner.id,
        "kind": "repair",
        "status": "pending",
        "sensitivity": "low",
        "partner_path": "message_partner",
        "shareable_summary": "Solo outgoing issue",
        "created_at": datetime.now(_UTC),
    }

    hc = await build_hot_context_solo(
        fake_pool,
        user,
        triggering_message_ids=[],
        trigger_metadata={"kind": "inbound"},
        primary_topic_id=get_relationship_topic_id(),
        bot_id="tante_rosi",
    )
    rendered = render_hot_context_solo(hc)

    assert len(hc.outgoing_mediated_issues) == 1
    assert "## Outgoing mediated issues" in rendered
    assert "Solo outgoing issue" in rendered
