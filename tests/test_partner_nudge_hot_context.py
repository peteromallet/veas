"""Render `## Incoming nudge from your partner` block when a scheduled
partner_nudge task fires.

Invariant 4: the `reason` field is audit-only and MUST NOT appear in
any rendered hot context. The trigger-metadata `context` dump is
suppressed for partner_nudge kinds to prevent leakage.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.bots.registry import get_relationship_topic_id
from app.models.user import User
from app.services.hot_context_solo import (
    build_hot_context_solo,
    render_hot_context_solo,
)
from app.services.hot_context import (
    HotContext,
    build_hot_context,
    render_hot_context,
)

pytestmark = pytest.mark.anyio


SECRET_REASON="PRIVAT...NDER"
NUDGE_NOTE = "Pom asked me to see how you're doing today."
BRIDGE_SUMMARY = "Pom feels unheard when discussions about finances end abruptly."
BRIDGE_INTERNAL = "Pom mentioned that Hannah walked out of the room during the budget talk."


def _seed_bridge_candidate(
    fake_pool,
    bridge_id,
    *,
    source_user_id,
    target_user_id,
    status="ready",
    partner_path="message_partner",
    shareable_summary=BRIDGE_SUMMARY,
    internal_note=BRIDGE_INTERNAL,
    created_at=None,
):
    """Seed a bridge_candidate row in the FakePool."""
    row = {
        "id": bridge_id,
        "source_user_id": source_user_id,
        "target_user_id": target_user_id,
        "kind": "grievance",
        "status": status,
        "sensitivity": "medium",
        "partner_path": partner_path,
        "shareable_summary": shareable_summary,
        "internal_note": internal_note,
        "reason": "partner reported feeling unheard",
        "created_at": created_at or datetime.now(UTC),
    }
    fake_pool.bridge_candidates[bridge_id] = row
    return row


async def _build_solo_partner_nudge_hc(
    fake_pool, user, partner=None, *, nudge_note=NUDGE_NOTE, bot_id="tante_rosi",
    bridge_candidate_id=None,
):
    fake_pool.users[user.id] = {
        "id": user.id,
        "name": user.name,
        "phone": user.phone,
        "timezone": user.timezone,
    }
    if partner is not None:
        fake_pool.users[partner.id] = {
            "id": partner.id,
            "name": partner.name,
            "phone": partner.phone,
            "timezone": partner.timezone,
        }
        fake_pool.dyad_partners[user.id] = partner.id
    ctx = {
        "kind": "partner_nudge",
        "originating_user_id": str(partner.id) if partner else str(uuid4()),
        "originating_user_name": partner.name if partner else None,
        "nudge_note": nudge_note,
        "reason": SECRET_REASON,
        "source": "explicit_user_request",
        "scheduled_for": datetime.now(UTC).isoformat(),
    }
    if bridge_candidate_id is not None:
        ctx["bridge_candidate_id"] = str(bridge_candidate_id)
    trigger_metadata = {
        "kind": "scheduled_task",
        "context": ctx,
    }
    return await build_hot_context_solo(
        fake_pool,
        user,
        triggering_message_ids=[],
        trigger_metadata=trigger_metadata,
        primary_topic_id=get_relationship_topic_id(),
        bot_id=bot_id,
    )


async def _build_dyadic_partner_nudge_hc(
    fake_pool, user, partner, *, nudge_note=NUDGE_NOTE, bot_id="mediator",
    bridge_candidate_id=None,
):
    """Exercise the FULL build_hot_context path (not _render_with_counts shortcut)."""
    fake_pool.users[user.id] = {
        "id": user.id,
        "name": user.name,
        "phone": user.phone,
        "timezone": user.timezone,
    }
    fake_pool.users[partner.id] = {
        "id": partner.id,
        "name": partner.name,
        "phone": partner.phone,
        "timezone": partner.timezone,
    }
    ctx = {
        "kind": "partner_nudge",
        "originating_user_id": str(partner.id),
        "originating_user_name": partner.name,
        "nudge_note": nudge_note,
        "reason": SECRET_REASON,
        "source": "explicit_user_request",
        "scheduled_for": datetime.now(UTC).isoformat(),
    }
    if bridge_candidate_id is not None:
        ctx["bridge_candidate_id"] = str(bridge_candidate_id)
    trigger_metadata = {
        "kind": "scheduled_task",
        "context": ctx,
    }
    return await build_hot_context(
        fake_pool,
        user,
        partner,
        triggering_message_ids=[],
        trigger_metadata=trigger_metadata,
        primary_topic_id=get_relationship_topic_id(),
        bot_id=bot_id,
    )


# ── Existing solo tests (unchanged) ────────────────────────────────────

async def test_solo_partner_nudge_renders_block_with_originator_and_note(fake_pool):
    recipient = User(uuid4(), "Hannah", "15555550101", "UTC")
    originator = User(uuid4(), "Pom", "15555550100", "UTC")
    hc = await _build_solo_partner_nudge_hc(fake_pool, recipient, originator)
    rendered = render_hot_context_solo(hc)
    assert "## Incoming nudge from your partner" in rendered
    assert "Pom" in rendered
    assert NUDGE_NOTE in rendered


async def test_solo_partner_nudge_omits_audit_reason(fake_pool):
    """Invariant 4: `reason` is audit-only and must NEVER render."""
    recipient = User(uuid4(), "Hannah", "15555550101", "UTC")
    originator = User(uuid4(), "Pom", "15555550100", "UTC")
    hc = await _build_solo_partner_nudge_hc(fake_pool, recipient, originator)
    rendered = render_hot_context_solo(hc)
    assert SECRET_REASON not in rendered
    # Also ensure the raw `- context:` jsonb dump did not happen — that
    # dump would have leaked `reason`.
    assert "PRIVATE_AUDIT_ONLY_REASON" not in rendered


async def test_solo_partner_nudge_falls_back_to_generic_note(fake_pool):
    recipient = User(uuid4(), "Hannah", "15555550101", "UTC")
    originator = User(uuid4(), "Pom", "15555550100", "UTC")
    hc = await _build_solo_partner_nudge_hc(
        fake_pool, recipient, originator, nudge_note=None
    )
    rendered = render_hot_context_solo(hc)
    assert "## Incoming nudge from your partner" in rendered
    assert "asked me to check in with you" in rendered


# ── Existing dyadic render shortcut test (unchanged) ──────────────────

def test_dyadic_render_branch_emits_block_and_drops_reason():
    """Mediator renderer in hot_context.py must mirror the solo branch."""
    # Use a minimal HotContext-like dataclass instance — we only need
    # the rendering function to see the trigger_metadata fields.
    from app.services.hot_context import (
        _render_with_counts,
    )

    msg_id = uuid4()
    hc = HotContext(
        current_user={
            "id": uuid4(),
            "name": "Hannah",
            "timezone": "UTC",
            "phone": "15555550101",
            "cross_thread_sharing_default": None,
            "partner_share": None,
            "partner_sharing_state": "unavailable",
            "style_notes": "",
            "onboarding_state": "complete",
        },
        partner_user={
            "id": uuid4(),
            "name": "Pom",
            "timezone": "UTC",
            "phone": "15555550100",
            "cross_thread_sharing_default": None,
            "partner_share": None,
            "partner_sharing_state": "unavailable",
            "style_notes": "",
            "onboarding_state": "complete",
        },
        conversation_load={"period": "today", "total_count": 0, "inbound_count": 0,
                           "outbound_count": 0, "period_start": None,
                           "period_end": None, "timezone": "UTC"},
        active_oob=[],
        memories=[],
        active_themes=[],
        open_watch_items=[],
        observations=[],
        distillations=[],
        bridge_candidates=[],
        recent_reactions=[],
        recent_messages=[],
        partner_shareable_summaries=[],
        topic_status=None,
        cross_topic_peek=[],
        cross_topic_status=[],
        time_since_last_message=None,
        trigger_metadata={
            "kind": "scheduled_task",
            "triggering_message_ids": [msg_id],
            "context": {
                "kind": "partner_nudge",
                "originating_user_id": str(uuid4()),
                "nudge_note": NUDGE_NOTE,
                "reason": SECRET_REASON,
            },
            "messages": [
                {
                    "id": msg_id,
                    "charge": "routine",
                    "sent_at": datetime.now(UTC),
                    "content": "scheduled nudge fire",
                }
            ],
        },
    )
    rendered = _render_with_counts(hc, truncations={})
    assert "## Incoming nudge from your partner" in rendered
    assert NUDGE_NOTE in rendered
    assert SECRET_REASON not in rendered


# ── NEW: Full-path bridge-context tests (dyadic) ──────────────────────

async def test_dyadic_renders_bridge_shareable_summary_full_path(fake_pool):
    """Full build_hot_context → render_hot_context path surfaces shareable_summary."""
    recipient = User(uuid4(), "Hannah", "15555550101", "UTC")
    originator = User(uuid4(), "Pom", "15555550100", "UTC")
    bridge_id = uuid4()
    _seed_bridge_candidate(
        fake_pool,
        bridge_id,
        source_user_id=originator.id,
        target_user_id=recipient.id,
    )
    hc = await _build_dyadic_partner_nudge_hc(
        fake_pool, recipient, originator, bridge_candidate_id=bridge_id,
    )
    rendered = render_hot_context(hc)
    assert "## Incoming nudge from your partner" in rendered
    assert f"- about: {BRIDGE_SUMMARY}" in rendered


async def test_dyadic_bridge_context_never_surfaces_internal_note(fake_pool):
    """Privacy: internal_note text must NEVER appear in rendered output."""
    recipient = User(uuid4(), "Hannah", "15555550101", "UTC")
    originator = User(uuid4(), "Pom", "15555550100", "UTC")
    bridge_id = uuid4()
    _seed_bridge_candidate(
        fake_pool,
        bridge_id,
        source_user_id=originator.id,
        target_user_id=recipient.id,
    )
    hc = await _build_dyadic_partner_nudge_hc(
        fake_pool, recipient, originator, bridge_candidate_id=bridge_id,
    )
    rendered = render_hot_context(hc)
    assert BRIDGE_INTERNAL not in rendered
    # reason must also never appear anywhere (invariant 4)
    assert SECRET_REASON not in rendered


async def test_dyadic_bridge_context_never_surfaces_audit_reason_via_bridge_path(fake_pool):
    """The audit `reason` from the bridge row must never render through linked path."""
    recipient = User(uuid4(), "Hannah", "15555550101", "UTC")
    originator = User(uuid4(), "Pom", "15555550100", "UTC")
    bridge_id = uuid4()
    _seed_bridge_candidate(
        fake_pool,
        bridge_id,
        source_user_id=originator.id,
        target_user_id=recipient.id,
    )
    hc = await _build_dyadic_partner_nudge_hc(
        fake_pool, recipient, originator, bridge_candidate_id=bridge_id,
    )
    rendered = render_hot_context(hc)
    # The raw bridge reason "partner reported feeling unheard" must not appear
    assert "partner reported feeling unheard" not in rendered
    # PRIVATE_AUDIT_ONLY_REASON from nudge context must not appear
    assert "PRIVATE_AUDIT_ONLY_REASON" not in rendered


async def test_dyadic_bridge_no_longer_visible_renders_fallback(fake_pool):
    """A bridge_candidate_id that exists but is no longer target-visible
    at fire time must render the neutral fallback line."""
    recipient = User(uuid4(), "Hannah", "15555550101", "UTC")
    originator = User(uuid4(), "Pom", "15555550100", "UTC")
    bridge_id = uuid4()
    # Seed a bridge that is NOT ready (e.g. blocked) — it will fail
    # bridge_candidate_visible_to_target since that function checks status.
    _seed_bridge_candidate(
        fake_pool,
        bridge_id,
        source_user_id=originator.id,
        target_user_id=recipient.id,
        status="blocked",  # not visible to target
    )
    hc = await _build_dyadic_partner_nudge_hc(
        fake_pool, recipient, originator, bridge_candidate_id=bridge_id,
    )
    rendered = render_hot_context(hc)
    assert "## Incoming nudge from your partner" in rendered
    assert "- about: a previously raised issue (since updated or resolved)" in rendered
    # The blocked bridge's shareable_summary must NOT leak
    assert BRIDGE_SUMMARY not in rendered


async def test_dyadic_bridge_missing_from_pool_renders_fallback(fake_pool):
    """bridge_candidate_id present in trigger but row deleted — neutral fallback."""
    recipient = User(uuid4(), "Hannah", "15555550101", "UTC")
    originator = User(uuid4(), "Pom", "15555550100", "UTC")
    nonexistent_id = uuid4()
    # Do NOT seed a bridge with this id.
    hc = await _build_dyadic_partner_nudge_hc(
        fake_pool, recipient, originator, bridge_candidate_id=nonexistent_id,
    )
    rendered = render_hot_context(hc)
    assert "## Incoming nudge from your partner" in rendered
    assert "- about: a previously raised issue (since updated or resolved)" in rendered


# ── NEW: Full-path bridge-context tests (solo) ────────────────────────

async def test_solo_renders_bridge_shareable_summary_full_path(fake_pool):
    """Full build_hot_context_solo → render_hot_context_solo surfaces shareable_summary."""
    recipient = User(uuid4(), "Hannah", "15555550101", "UTC")
    originator = User(uuid4(), "Pom", "15555550100", "UTC")
    bridge_id = uuid4()
    _seed_bridge_candidate(
        fake_pool,
        bridge_id,
        source_user_id=originator.id,
        target_user_id=recipient.id,
    )
    hc = await _build_solo_partner_nudge_hc(
        fake_pool, recipient, originator, bridge_candidate_id=bridge_id,
    )
    rendered = render_hot_context_solo(hc)
    assert "## Incoming nudge from your partner" in rendered
    assert f"- about: {BRIDGE_SUMMARY}" in rendered


async def test_solo_bridge_context_never_surfaces_internal_note(fake_pool):
    """Privacy: internal_note text must NEVER appear in solo rendered output."""
    recipient = User(uuid4(), "Hannah", "15555550101", "UTC")
    originator = User(uuid4(), "Pom", "15555550100", "UTC")
    bridge_id = uuid4()
    _seed_bridge_candidate(
        fake_pool,
        bridge_id,
        source_user_id=originator.id,
        target_user_id=recipient.id,
    )
    hc = await _build_solo_partner_nudge_hc(
        fake_pool, recipient, originator, bridge_candidate_id=bridge_id,
    )
    rendered = render_hot_context_solo(hc)
    assert BRIDGE_INTERNAL not in rendered
    assert SECRET_REASON not in rendered


async def test_solo_bridge_no_longer_visible_renders_fallback(fake_pool):
    """A bridge_candidate_id that exists but is no longer target-visible
    at fire time must render the neutral fallback line (solo path)."""
    recipient = User(uuid4(), "Hannah", "15555550101", "UTC")
    originator = User(uuid4(), "Pom", "15555550100", "UTC")
    bridge_id = uuid4()
    _seed_bridge_candidate(
        fake_pool,
        bridge_id,
        source_user_id=originator.id,
        target_user_id=recipient.id,
        status="blocked",  # not visible to target
    )
    hc = await _build_solo_partner_nudge_hc(
        fake_pool, recipient, originator, bridge_candidate_id=bridge_id,
    )
    rendered = render_hot_context_solo(hc)
    assert "## Incoming nudge from your partner" in rendered
    assert "- about: a previously raised issue (since updated or resolved)" in rendered
    assert BRIDGE_SUMMARY not in rendered


async def test_solo_bridge_missing_from_pool_renders_fallback(fake_pool):
    """bridge_candidate_id present in trigger but row deleted — neutral fallback (solo)."""
    recipient = User(uuid4(), "Hannah", "15555550101", "UTC")
    originator = User(uuid4(), "Pom", "15555550100", "UTC")
    nonexistent_id = uuid4()
    hc = await _build_solo_partner_nudge_hc(
        fake_pool, recipient, originator, bridge_candidate_id=nonexistent_id,
    )
    rendered = render_hot_context_solo(hc)
    assert "## Incoming nudge from your partner" in rendered
    assert "- about: a previously raised issue (since updated or resolved)" in rendered
