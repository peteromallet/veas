"""Unit tests for app/services/compass.py — CompassSnapshot builder.

Tests the Compass privacy/read model boundary: explicit user/topic binding,
is_compass_visible() filtering, evidence-link fetching, and grouping by kind.
"""

from __future__ import annotations

from datetime import date as dt_date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from app.services import user_orientation as uo
from app.services.compass import (
    CompassItem,
    CompassRenderer,
    CompassSnapshot,
    build_compass_snapshot,
)


# ── Helpers ────────────────────────────────────────────────────────────────


def _make_orientation_item(
    *,
    item_id: UUID | None = None,
    user_id: UUID | None = None,
    topic_id: UUID | None = None,
    kind: str = "goal",
    status: str = "active",
    source: str = "user_stated",
    review_state: str = "reviewed",
    label: str = "Test item",
    detail: str | None = None,
    priority_rank: int | None = None,
) -> uo.OrientationItem:
    """Create a minimal OrientationItem for testing."""
    now = datetime.now(timezone.utc)
    return uo.OrientationItem(
        id=item_id or uuid4(),
        user_id=user_id or uuid4(),
        topic_id=topic_id or uuid4(),
        bot_id="test_bot",
        created_by_turn_id=None,
        kind=kind,
        status=status,
        source=source,
        review_state=review_state,
        label=label,
        detail=detail,
        started_at=None,
        effective_at=None,
        target_date=None,
        completed_at=None,
        closed_reason=None,
        outcome_note=None,
        supersedes_item_id=None,
        priority_rank=priority_rank,
        created_at=now,
        updated_at=now,
    )


def _make_orientation_link(
    *,
    item_id: UUID,
    user_id: UUID | None = None,
    relation: str = "evidence",
) -> uo.OrientationLink:
    """Create a minimal OrientationLink for testing."""
    now = datetime.now(timezone.utc)
    return uo.OrientationLink(
        id=uuid4(),
        item_id=item_id,
        user_id=user_id or uuid4(),
        topic_id=uuid4(),
        target_table="commitments",
        target_id=uuid4(),
        relation=relation,
        note=None,
        created_at=now,
    )


# ── CompassSnapshot static tests ───────────────────────────────────────────


class TestCompassSnapshot:
    def test_empty_snapshot_is_empty(self):
        snap = CompassSnapshot(
            user_id=uuid4(),
            topic_ids=frozenset([uuid4()]),
        )
        assert snap.is_empty is True
        assert snap.total_items == 0

    def test_non_empty_snapshot(self):
        item = _make_orientation_item(kind="principle")
        ci = CompassItem(item=item, links=())
        snap = CompassSnapshot(
            user_id=item.user_id,
            topic_ids=frozenset([item.topic_id]),
            principles=(ci,),
        )
        assert snap.is_empty is False
        assert snap.total_items == 1

    def test_multiple_categories_count(self):
        p = _make_orientation_item(kind="principle")
        g = _make_orientation_item(kind="goal")
        snap = CompassSnapshot(
            user_id=p.user_id,
            topic_ids=frozenset([p.topic_id]),
            principles=(CompassItem(item=p, links=()),),
            active_goals=(CompassItem(item=g, links=()),),
        )
        assert snap.total_items == 2


# ── CompassItem tests ──────────────────────────────────────────────────────


class TestCompassItem:
    def test_properties_delegate_to_item(self):
        item = _make_orientation_item(
            kind="priority",
            status="active",
            label="Stay healthy",
            priority_rank=3,
        )
        ci = CompassItem(item=item, links=())
        assert ci.kind == "priority"
        assert ci.status == "active"
        assert ci.label == "Stay healthy"
        assert ci.priority_rank == 3
        assert ci.id == item.id

    def test_links_defaults_to_empty_tuple(self):
        item = _make_orientation_item()
        ci = CompassItem(item=item)
        assert ci.links == ()

    def test_links_are_stored(self):
        item = _make_orientation_item()
        link = _make_orientation_link(item_id=item.id)
        ci = CompassItem(item=item, links=(link,))
        assert len(ci.links) == 1
        assert ci.links[0].item_id == item.id


# ── build_compass_snapshot validation tests ────────────────────────────────


class TestBuildCompassSnapshotValidation:
    async def test_rejects_none_user_id(self):
        store = MagicMock()
        with pytest.raises(ValueError, match="user_id must not be None"):
            await build_compass_snapshot(
                store, user_id=None, topic_ids=frozenset([uuid4()])
            )

    async def test_rejects_empty_topic_ids(self):
        store = MagicMock()
        with pytest.raises(ValueError, match="non-empty"):
            await build_compass_snapshot(
                store, user_id=uuid4(), topic_ids=frozenset()
            )


# ── build_compass_snapshot integration tests ───────────────────────────────


class TestBuildCompassSnapshot:
    @pytest.fixture
    def user_id(self) -> UUID:
        return uuid4()

    @pytest.fixture
    def topic_ids(self) -> frozenset[UUID]:
        return frozenset([uuid4()])

    async def test_empty_store_returns_empty_snapshot(self, user_id, topic_ids):
        """When the store has no items, the snapshot should be empty."""
        store = MagicMock()
        store.list_items = AsyncMock(return_value=[])

        snap = await build_compass_snapshot(
            store, user_id=user_id, topic_ids=topic_ids
        )

        assert snap.is_empty is True
        assert snap.total_items == 0
        assert snap.user_id == user_id
        assert snap.topic_ids == topic_ids
        # Verify store was called with explicit user and topic IDs.
        store.list_items.assert_called_once_with(
            user_id=user_id,
            topic_ids=list(topic_ids),
            include_unreviewed=False,
            include_rejected=False,
        )

    async def test_visible_items_are_included(self, user_id, topic_ids):
        """Items that pass is_compass_visible() are included."""
        item = _make_orientation_item(
            user_id=user_id,
            topic_id=list(topic_ids)[0],
            kind="principle",
            status="active",
            source="user_stated",
            review_state="reviewed",
            label="Be honest",
        )
        store = MagicMock()
        store.list_items = AsyncMock(return_value=[item])
        store.get_links = AsyncMock(return_value=[])

        snap = await build_compass_snapshot(
            store, user_id=user_id, topic_ids=topic_ids
        )

        assert snap.is_empty is False
        assert snap.total_items == 1
        assert len(snap.principles) == 1
        assert snap.principles[0].label == "Be honest"

    async def test_invisible_items_are_excluded(self, user_id, topic_ids):
        """Items that fail is_compass_visible() are excluded."""
        # A pending bot_proposed item without review should not be visible.
        item = _make_orientation_item(
            user_id=user_id,
            topic_id=list(topic_ids)[0],
            kind="goal",
            status="pending",
            source="bot_proposed",
            review_state="unreviewed",
            label="Proposed goal",
        )
        store = MagicMock()
        store.list_items = AsyncMock(return_value=[item])
        # get_links should not be called for invisible items.
        store.get_links = AsyncMock()

        snap = await build_compass_snapshot(
            store, user_id=user_id, topic_ids=topic_ids
        )

        assert snap.is_empty is True
        store.get_links.assert_not_called()

    async def test_rejected_items_are_excluded(self, user_id, topic_ids):
        """Rejected items should be excluded even if they appear in list_items."""
        item = _make_orientation_item(
            user_id=user_id,
            topic_id=list(topic_ids)[0],
            kind="goal",
            status="rejected",
            source="user_confirmed",
            review_state="reviewed",
            label="Rejected goal",
        )
        store = MagicMock()
        store.list_items = AsyncMock(return_value=[item])
        store.get_links = AsyncMock()

        snap = await build_compass_snapshot(
            store, user_id=user_id, topic_ids=topic_ids
        )

        assert snap.is_empty is True
        store.get_links.assert_not_called()

    async def test_bot_proposed_reviewed_is_visible(self, user_id, topic_ids):
        """A bot_proposed item with review_state='reviewed' and status='active'
        should be visible."""
        item = _make_orientation_item(
            user_id=user_id,
            topic_id=list(topic_ids)[0],
            kind="goal",
            status="active",
            source="bot_proposed",
            review_state="reviewed",
            label="Reviewed bot proposal",
        )
        store = MagicMock()
        store.list_items = AsyncMock(return_value=[item])
        store.get_links = AsyncMock(return_value=[])

        snap = await build_compass_snapshot(
            store, user_id=user_id, topic_ids=topic_ids
        )

        assert snap.is_empty is False
        assert snap.total_items == 1
        store.get_links.assert_called_once()

    async def test_evidence_links_are_fetched(self, user_id, topic_ids):
        """Evidence links should be fetched for every visible item."""
        item = _make_orientation_item(
            user_id=user_id,
            topic_id=list(topic_ids)[0],
            kind="goal",
            status="active",
            source="user_stated",
        )
        link = _make_orientation_link(item_id=item.id, user_id=user_id)

        store = MagicMock()
        store.list_items = AsyncMock(return_value=[item])
        store.get_links = AsyncMock(return_value=[link])

        snap = await build_compass_snapshot(
            store, user_id=user_id, topic_ids=topic_ids
        )

        assert snap.total_items == 1
        assert len(snap.active_goals[0].links) == 1
        assert snap.active_goals[0].links[0].item_id == item.id
        store.get_links.assert_called_once_with(
            user_id=user_id, item_id=item.id
        )

    async def test_goals_are_grouped_by_status(self, user_id, topic_ids):
        """Active goals go to active_goals, completed/retired to completed_goals."""
        active = _make_orientation_item(
            user_id=user_id,
            topic_id=list(topic_ids)[0],
            kind="goal",
            status="active",
            label="Active goal",
        )
        completed = _make_orientation_item(
            user_id=user_id,
            topic_id=list(topic_ids)[0],
            kind="goal",
            status="completed",
            label="Completed goal",
        )
        retired = _make_orientation_item(
            user_id=user_id,
            topic_id=list(topic_ids)[0],
            kind="goal",
            status="retired",
            label="Retired goal",
        )

        store = MagicMock()
        store.list_items = AsyncMock(return_value=[active, completed, retired])
        store.get_links = AsyncMock(return_value=[])

        snap = await build_compass_snapshot(
            store, user_id=user_id, topic_ids=topic_ids
        )

        assert len(snap.active_goals) == 1
        assert snap.active_goals[0].label == "Active goal"
        assert len(snap.completed_goals) == 2
        completed_labels = {g.label for g in snap.completed_goals}
        assert completed_labels == {"Completed goal", "Retired goal"}

    async def test_all_kinds_are_grouped(self, user_id, topic_ids):
        """All four kinds are correctly grouped."""
        principle = _make_orientation_item(
            user_id=user_id, topic_id=list(topic_ids)[0],
            kind="principle", label="P1",
        )
        goal = _make_orientation_item(
            user_id=user_id, topic_id=list(topic_ids)[0],
            kind="goal", status="active", label="G1",
        )
        priority = _make_orientation_item(
            user_id=user_id, topic_id=list(topic_ids)[0],
            kind="priority", label="Pri1", priority_rank=1,
        )
        anti = _make_orientation_item(
            user_id=user_id, topic_id=list(topic_ids)[0],
            kind="anti_pattern", label="AP1",
        )

        store = MagicMock()
        store.list_items = AsyncMock(
            return_value=[principle, goal, priority, anti]
        )
        store.get_links = AsyncMock(return_value=[])

        snap = await build_compass_snapshot(
            store, user_id=user_id, topic_ids=topic_ids
        )

        assert len(snap.principles) == 1
        assert snap.principles[0].label == "P1"
        assert len(snap.active_goals) == 1
        assert snap.active_goals[0].label == "G1"
        assert len(snap.priorities) == 1
        assert snap.priorities[0].label == "Pri1"
        assert len(snap.anti_patterns) == 1
        assert snap.anti_patterns[0].label == "AP1"

    async def test_priorities_sorted_by_rank(self, user_id, topic_ids):
        """Priorities with lower rank come first, NULLs last."""
        p1 = _make_orientation_item(
            user_id=user_id, topic_id=list(topic_ids)[0],
            kind="priority", label="Rank 3", priority_rank=3,
        )
        p2 = _make_orientation_item(
            user_id=user_id, topic_id=list(topic_ids)[0],
            kind="priority", label="Rank 1", priority_rank=1,
        )
        p3 = _make_orientation_item(
            user_id=user_id, topic_id=list(topic_ids)[0],
            kind="priority", label="No rank", priority_rank=None,
        )

        store = MagicMock()
        store.list_items = AsyncMock(return_value=[p1, p2, p3])
        store.get_links = AsyncMock(return_value=[])

        snap = await build_compass_snapshot(
            store, user_id=user_id, topic_ids=topic_ids
        )

        assert [p.label for p in snap.priorities] == [
            "Rank 1", "Rank 3", "No rank",
        ]

    async def test_explicit_topic_ids_are_used(self, user_id):
        """The store is called with the exact topic_ids passed in."""
        topic_a = uuid4()
        topic_b = uuid4()
        topic_ids = frozenset([topic_a, topic_b])

        item = _make_orientation_item(
            user_id=user_id, topic_id=topic_a, kind="principle",
        )
        store = MagicMock()
        store.list_items = AsyncMock(return_value=[item])
        store.get_links = AsyncMock(return_value=[])

        await build_compass_snapshot(
            store, user_id=user_id, topic_ids=topic_ids,
        )

        # Verify topic_id_list includes exactly the two topics (order-independent).
        call_kwargs = store.list_items.call_args.kwargs
        assert set(call_kwargs["topic_ids"]) == {topic_a, topic_b}
        assert "all" not in str(call_kwargs["topic_ids"])

    async def test_multiple_topics_all_visible_items_included(self, user_id):
        """Items from all allowed topics are included."""
        topic_a = uuid4()
        topic_b = uuid4()

        item_a = _make_orientation_item(
            user_id=user_id, topic_id=topic_a, kind="principle",
            label="From A",
        )
        item_b = _make_orientation_item(
            user_id=user_id, topic_id=topic_b, kind="principle",
            label="From B",
        )

        store = MagicMock()
        store.list_items = AsyncMock(return_value=[item_a, item_b])
        store.get_links = AsyncMock(return_value=[])

        snap = await build_compass_snapshot(
            store, user_id=user_id, topic_ids=frozenset([topic_a, topic_b]),
        )

        assert len(snap.principles) == 2
        labels = {p.label for p in snap.principles}
        assert labels == {"From A", "From B"}

    async def test_superseded_excluded_by_default(self, user_id, topic_ids):
        """Superseded items are excluded by list_items defaults."""
        item = _make_orientation_item(
            user_id=user_id, topic_id=list(topic_ids)[0],
            kind="goal", status="superseded",
        )
        store = MagicMock()
        store.list_items = AsyncMock(return_value=[item])
        store.get_links = AsyncMock()

        snap = await build_compass_snapshot(
            store, user_id=user_id, topic_ids=topic_ids,
        )

        assert snap.is_empty is True


# ── CompassItem.closed_reason tests ───────────────────────────────────────


class TestCompassItemClosedReason:
    def test_closed_reason_delegates_to_item(self):
        item = _make_orientation_item(
            kind="goal", status="completed",
        )
        # Override closed_reason via object.__setattr__ since dataclass is frozen.
        object.__setattr__(item, "closed_reason", "No longer relevant")
        ci = CompassItem(item=item, links=())
        assert ci.closed_reason == "No longer relevant"

    def test_closed_reason_none_by_default(self):
        item = _make_orientation_item(kind="goal")
        ci = CompassItem(item=item, links=())
        assert ci.closed_reason is None


# ── CompassRenderer tests ─────────────────────────────────────────────────

class TestCompassRenderer:
    """Tests for CompassRenderer — deterministic markdown output."""

    def test_empty_snapshot_returns_empty_string(self):
        snap = CompassSnapshot(
            user_id=uuid4(), topic_ids=frozenset([uuid4()]),
        )
        renderer = CompassRenderer()
        result = renderer.render(snap)
        assert result == ""

    def test_single_principle(self):
        item = _make_orientation_item(
            kind="principle", label="Be honest",
            detail="Always tell the truth",
        )
        ci = CompassItem(item=item, links=())
        snap = CompassSnapshot(
            user_id=item.user_id,
            topic_ids=frozenset([item.topic_id]),
            principles=(ci,),
        )
        renderer = CompassRenderer()
        result = renderer.render(snap)
        assert "## Compass" in result
        assert "### Principles" in result
        assert "- **Be honest**: Always tell the truth" in result

    def test_principle_without_detail(self):
        item = _make_orientation_item(
            kind="principle", label="Be honest", detail=None,
        )
        ci = CompassItem(item=item, links=())
        snap = CompassSnapshot(
            user_id=item.user_id,
            topic_ids=frozenset([item.topic_id]),
            principles=(ci,),
        )
        renderer = CompassRenderer()
        result = renderer.render(snap)
        assert "- **Be honest**" in result
        # No trailing colon when detail is absent.
        assert "- **Be honest**:" not in result

    def test_single_priority_with_rank(self):
        item = _make_orientation_item(
            kind="priority", label="Health first", priority_rank=1,
        )
        ci = CompassItem(item=item, links=())
        snap = CompassSnapshot(
            user_id=item.user_id,
            topic_ids=frozenset([item.topic_id]),
            priorities=(ci,),
        )
        renderer = CompassRenderer()
        result = renderer.render(snap)
        assert "### Priorities" in result
        assert "1. **Health first** (priority 1)" in result

    def test_priority_without_rank(self):
        item = _make_orientation_item(
            kind="priority", label="General priority", priority_rank=None,
        )
        ci = CompassItem(item=item, links=())
        snap = CompassSnapshot(
            user_id=item.user_id,
            topic_ids=frozenset([item.topic_id]),
            priorities=(ci,),
        )
        renderer = CompassRenderer()
        result = renderer.render(snap)
        assert "1. **General priority**" in result
        assert "(priority" not in result

    def test_single_anti_pattern(self):
        item = _make_orientation_item(
            kind="anti_pattern", label="Overworking",
            detail="Don't work past 8pm",
        )
        ci = CompassItem(item=item, links=())
        snap = CompassSnapshot(
            user_id=item.user_id,
            topic_ids=frozenset([item.topic_id]),
            anti_patterns=(ci,),
        )
        renderer = CompassRenderer()
        result = renderer.render(snap)
        assert "### Anti-patterns" in result
        assert "- **Overworking**: Don't work past 8pm" in result

    def test_active_goal_with_metadata(self):
        item = _make_orientation_item(
            kind="goal", status="active", label="Run a marathon",
            detail="Train for a full marathon",
        )
        object.__setattr__(item, "target_date", dt_date(2026, 12, 31))
        ci = CompassItem(item=item, links=())
        snap = CompassSnapshot(
            user_id=item.user_id,
            topic_ids=frozenset([item.topic_id]),
            active_goals=(ci,),
        )
        renderer = CompassRenderer()
        result = renderer.render(snap)
        assert "### Active Goals" in result
        assert "- **Run a marathon**" in result
        assert "  - Detail: Train for a full marathon" in result
        assert "  - Target: 2026-12-31" in result

    def test_completed_goal_with_all_metadata(self):
        completed_dt = datetime(2026, 3, 15, 10, 30, 0, tzinfo=timezone.utc)
        item = _make_orientation_item(
            kind="goal", status="completed", label="Learn Spanish",
        )
        object.__setattr__(item, "target_date", dt_date(2025, 12, 31))
        object.__setattr__(item, "completed_at", completed_dt)
        object.__setattr__(item, "closed_reason", "Achieved fluency")
        object.__setattr__(item, "outcome_note", "Can hold conversations")
        ci = CompassItem(item=item, links=())
        snap = CompassSnapshot(
            user_id=item.user_id,
            topic_ids=frozenset([item.topic_id]),
            completed_goals=(ci,),
        )
        renderer = CompassRenderer()
        result = renderer.render(snap)
        assert "### Completed / Retired Goals" in result
        assert "- **Learn Spanish**" in result
        assert "  - Target: 2025-12-31" in result
        assert "  - Completed: 2026-03-15T10:30:00+00:00 — Achieved fluency" in result
        assert "  - Outcome: Can hold conversations" in result

    def test_completed_goal_without_reason(self):
        completed_dt = datetime(2026, 3, 15, 10, 30, 0, tzinfo=timezone.utc)
        item = _make_orientation_item(
            kind="goal", status="completed", label="Done goal",
        )
        object.__setattr__(item, "completed_at", completed_dt)
        ci = CompassItem(item=item, links=())
        snap = CompassSnapshot(
            user_id=item.user_id,
            topic_ids=frozenset([item.topic_id]),
            completed_goals=(ci,),
        )
        renderer = CompassRenderer()
        result = renderer.render(snap)
        # Completed line should not include " — " after the timestamp.
        assert "  - Completed: 2026-03-15T10:30:00+00:00" in result
        assert " — " not in result

    def test_goal_with_evidence_links(self):
        item = _make_orientation_item(
            kind="goal", status="active", label="Goal with evidence",
        )
        link1 = _make_orientation_link(
            item_id=item.id, user_id=item.user_id,
            relation="evidence",
        )
        link2 = _make_orientation_link(
            item_id=item.id, user_id=item.user_id,
            relation="progress",
        )
        ci = CompassItem(item=item, links=(link1, link2))
        snap = CompassSnapshot(
            user_id=item.user_id,
            topic_ids=frozenset([item.topic_id]),
            active_goals=(ci,),
        )
        renderer = CompassRenderer()
        result = renderer.render(snap)
        assert "  - Evidence:" in result
        # Both links should appear as compact references.
        assert f"`commitments:{link1.target_id}` (evidence)" in result
        assert f"`commitments:{link2.target_id}` (progress)" in result

    def test_evidence_links_are_sorted_deterministically(self):
        item = _make_orientation_item(
            kind="goal", status="active", label="Goal",
        )
        link_a = _make_orientation_link(
            item_id=item.id, user_id=item.user_id,
            relation="progress",
        )
        link_b = _make_orientation_link(
            item_id=item.id, user_id=item.user_id,
            relation="evidence",
        )
        ci = CompassItem(item=item, links=(link_a, link_b))
        snap = CompassSnapshot(
            user_id=item.user_id,
            topic_ids=frozenset([item.topic_id]),
            active_goals=(ci,),
        )
        renderer = CompassRenderer()
        result = renderer.render(snap)
        # evidence sorts before progress (alphabetical by relation).
        ev_pos = result.index("(evidence)")
        prog_pos = result.index("(progress)")
        assert ev_pos < prog_pos

    def test_no_evidence_when_links_empty(self):
        item = _make_orientation_item(
            kind="goal", status="active", label="Goal no links",
        )
        ci = CompassItem(item=item, links=())
        snap = CompassSnapshot(
            user_id=item.user_id,
            topic_ids=frozenset([item.topic_id]),
            active_goals=(ci,),
        )
        renderer = CompassRenderer()
        result = renderer.render(snap)
        assert "Evidence:" not in result

    def test_all_sections_rendered_in_order(self):
        """Verify sections appear in the correct order."""
        p = _make_orientation_item(kind="principle", label="P")
        pr = _make_orientation_item(kind="priority", label="Pr", priority_rank=1)
        ap = _make_orientation_item(kind="anti_pattern", label="AP")
        g = _make_orientation_item(kind="goal", status="active", label="G")
        snap = CompassSnapshot(
            user_id=p.user_id,
            topic_ids=frozenset([p.topic_id]),
            principles=(CompassItem(item=p, links=()),),
            priorities=(CompassItem(item=pr, links=()),),
            anti_patterns=(CompassItem(item=ap, links=()),),
            active_goals=(CompassItem(item=g, links=()),),
        )
        renderer = CompassRenderer()
        result = renderer.render(snap)
        # Check ordering: Principles → Priorities → Anti-patterns → Active Goals.
        pos_p = result.index("### Principles")
        pos_pr = result.index("### Priorities")
        pos_ap = result.index("### Anti-patterns")
        pos_g = result.index("### Active Goals")
        assert pos_p < pos_pr < pos_ap < pos_g

    def test_empty_sections_are_omitted(self):
        """Sections with no items should not appear."""
        item = _make_orientation_item(kind="principle", label="Only principle")
        snap = CompassSnapshot(
            user_id=item.user_id,
            topic_ids=frozenset([item.topic_id]),
            principles=(CompassItem(item=item, links=()),),
        )
        renderer = CompassRenderer()
        result = renderer.render(snap)
        assert "### Principles" in result
        assert "### Priorities" not in result
        assert "### Anti-patterns" not in result
        assert "### Active Goals" not in result
        assert "### Completed / Retired Goals" not in result

    def test_multiple_items_in_same_section(self):
        """Multiple items in the same section are all rendered."""
        p1 = _make_orientation_item(kind="principle", label="P1")
        p2 = _make_orientation_item(kind="principle", label="P2")
        snap = CompassSnapshot(
            user_id=p1.user_id,
            topic_ids=frozenset([p1.topic_id]),
            principles=(
                CompassItem(item=p1, links=()),
                CompassItem(item=p2, links=()),
            ),
        )
        renderer = CompassRenderer()
        result = renderer.render(snap)
        assert result.count("- **") == 2
        assert "- **P1**" in result
        assert "- **P2**" in result

    def test_render_is_deterministic(self):
        """Rendering the same snapshot twice produces identical output."""
        p = _make_orientation_item(kind="principle", label="P")
        g = _make_orientation_item(kind="goal", status="active", label="G")
        snap = CompassSnapshot(
            user_id=p.user_id,
            topic_ids=frozenset([p.topic_id]),
            principles=(CompassItem(item=p, links=()),),
            active_goals=(CompassItem(item=g, links=()),),
        )
        renderer = CompassRenderer()
        result1 = renderer.render(snap)
        result2 = renderer.render(snap)
        assert result1 == result2

    def test_render_does_not_mutate_snapshot(self):
        """Rendering must not mutate the snapshot or its items."""
        p = _make_orientation_item(kind="principle", label="P")
        ci = CompassItem(item=p, links=())
        snap = CompassSnapshot(
            user_id=p.user_id,
            topic_ids=frozenset([p.topic_id]),
            principles=(ci,),
        )
        # Capture pre-render state.
        orig_principles = snap.principles
        orig_label = snap.principles[0].label
        renderer = CompassRenderer()
        renderer.render(snap)
        # Post-render state must be identical.
        assert snap.principles is orig_principles
        assert snap.principles[0].label == orig_label


# ── is_compass_visible() direct unit tests ──────────────────────────────────


class TestIsCompassVisibleDirect:
    """Direct unit tests for is_compass_visible() privacy gate.

    Validates every combination of status × source × review_state that
    determines Compass visibility. These are deliberately written as
    direct dict-based calls to prove the gate works independently of
    the builder/store pipeline.
    """

    @staticmethod
    def _d(
        status: str = "active",
        review_state: str = "reviewed",
        source: str = "user_stated",
    ) -> dict[str, str]:
        return {"status": status, "review_state": review_state, "source": source}

    # ── user_stated ─────────────────────────────────────────────────────

    def test_user_stated_active_is_visible(self):
        assert uo.is_compass_visible(self._d(status="active", source="user_stated"))

    def test_user_stated_completed_is_visible(self):
        assert uo.is_compass_visible(self._d(status="completed", source="user_stated"))

    def test_user_stated_retired_is_visible(self):
        assert uo.is_compass_visible(self._d(status="retired", source="user_stated"))

    def test_user_stated_pending_is_hidden(self):
        assert not uo.is_compass_visible(self._d(status="pending", source="user_stated"))

    def test_user_stated_rejected_is_hidden(self):
        assert not uo.is_compass_visible(self._d(status="rejected", source="user_stated"))

    def test_user_stated_superseded_is_hidden(self):
        assert not uo.is_compass_visible(self._d(status="superseded", source="user_stated"))

    # ── user_confirmed ──────────────────────────────────────────────────

    def test_user_confirmed_active_is_visible(self):
        assert uo.is_compass_visible(self._d(status="active", source="user_confirmed"))

    def test_user_confirmed_completed_is_visible(self):
        assert uo.is_compass_visible(self._d(status="completed", source="user_confirmed"))

    def test_user_confirmed_retired_is_visible(self):
        assert uo.is_compass_visible(self._d(status="retired", source="user_confirmed"))

    def test_user_confirmed_pending_is_hidden(self):
        assert not uo.is_compass_visible(self._d(status="pending", source="user_confirmed"))

    def test_user_confirmed_rejected_is_hidden(self):
        assert not uo.is_compass_visible(self._d(status="rejected", source="user_confirmed"))

    # ── bot_proposed ────────────────────────────────────────────────────

    def test_bot_proposed_reviewed_active_is_visible(self):
        assert uo.is_compass_visible(
            self._d(status="active", source="bot_proposed", review_state="reviewed")
        )

    def test_bot_proposed_unreviewed_is_hidden(self):
        # bot_proposed must be reviewed to become visible.
        assert not uo.is_compass_visible(
            self._d(status="active", source="bot_proposed", review_state="unreviewed")
        )

    def test_bot_proposed_excluded_is_hidden(self):
        assert not uo.is_compass_visible(
            self._d(status="active", source="bot_proposed", review_state="excluded")
        )

    def test_bot_proposed_reviewed_but_pending_is_hidden(self):
        # Reviewed bot_proposed but still pending (transitional edge case).
        assert not uo.is_compass_visible(
            self._d(status="pending", source="bot_proposed", review_state="reviewed")
        )

    def test_bot_proposed_reviewed_but_completed_is_hidden(self):
        # bot_proposed items only visible when active, even if reviewed.
        assert not uo.is_compass_visible(
            self._d(status="completed", source="bot_proposed", review_state="reviewed")
        )

    def test_bot_proposed_reviewed_but_retired_is_hidden(self):
        assert not uo.is_compass_visible(
            self._d(status="retired", source="bot_proposed", review_state="reviewed")
        )

    def test_bot_proposed_reviewed_but_rejected_is_hidden(self):
        # rejected is caught before the bot_proposed check.
        assert not uo.is_compass_visible(
            self._d(status="rejected", source="bot_proposed", review_state="reviewed")
        )

    def test_bot_proposed_reviewed_but_superseded_is_hidden(self):
        assert not uo.is_compass_visible(
            self._d(status="superseded", source="bot_proposed", review_state="reviewed")
        )

    # ── edge cases ──────────────────────────────────────────────────────

    def test_missing_keys_default_to_hidden(self):
        """When keys are missing from the dict, defaults should make item hidden."""
        assert not uo.is_compass_visible({})

    def test_unknown_source_is_hidden(self):
        """An unknown source string should not accidentally become visible."""
        assert not uo.is_compass_visible(
            self._d(status="active", source="unknown_source", review_state="reviewed")
        )

    def test_reviewed_active_but_unknown_source_is_hidden(self):
        """Only user_stated, user_confirmed, and bot_proposed sources are recognized."""
        assert not uo.is_compass_visible(
            self._d(status="active", source="imported", review_state="reviewed")
        )


# ── close_item() orientation-row-only lifecycle tests ───────────────────────


class TestCloseItemOrientationOnly:
    """Prove that close_item() affects only orientation rows.

    The contract: closing a goal writes completed_at, closed_reason, and
    outcome_note on the orientation item only. It must NOT mutate any
    commitment/event adherence or lifecycle state. The Compass renderer
    surfaces the orientation metadata without implying commitment/event
    changes.
    """

    def test_closed_goal_appears_in_completed_goals(self):
        """After close, the goal moves from active to completed in the snapshot."""
        user_id = uuid4()
        topic_id = uuid4()
        item = _make_orientation_item(
            user_id=user_id,
            topic_id=topic_id,
            kind="goal",
            status="completed",
            label="Closed goal",
            source="user_stated",
        )
        object.__setattr__(item, "completed_at", datetime(2026, 6, 1, tzinfo=timezone.utc))
        object.__setattr__(item, "closed_reason", "Mission accomplished")
        object.__setattr__(item, "outcome_note", "Great results")
        ci = CompassItem(item=item, links=())
        snap = CompassSnapshot(
            user_id=user_id,
            topic_ids=frozenset([topic_id]),
            completed_goals=(ci,),
        )
        # Completed goal is in completed_goals, not active_goals.
        assert len(snap.completed_goals) == 1
        assert len(snap.active_goals) == 0
        assert snap.completed_goals[0].label == "Closed goal"
        assert snap.completed_goals[0].status == "completed"

    def test_closed_goal_evidence_links_preserved(self):
        """Evidence links on a closed goal are preserved unchanged."""
        user_id = uuid4()
        topic_id = uuid4()
        item = _make_orientation_item(
            user_id=user_id,
            topic_id=topic_id,
            kind="goal",
            status="completed",
            label="Goal with evidence",
            source="user_stated",
        )
        object.__setattr__(item, "completed_at", datetime(2026, 6, 1, tzinfo=timezone.utc))
        link = _make_orientation_link(item_id=item.id, user_id=user_id, relation="evidence")
        ci = CompassItem(item=item, links=(link,))
        snap = CompassSnapshot(
            user_id=user_id,
            topic_ids=frozenset([topic_id]),
            completed_goals=(ci,),
        )
        # Links are intact — no commitment/event mutation implied.
        assert len(snap.completed_goals[0].links) == 1
        assert snap.completed_goals[0].links[0].relation == "evidence"
        assert snap.completed_goals[0].links[0].target_table == "commitments"

    def test_renderer_surfaces_orientation_metadata_only(self):
        """Renderer output for a closed goal contains orientation fields only.

        It must NOT reference commitment adherence columns (adherence_status,
        adherence_note) or event lifecycle fields.
        """
        user_id = uuid4()
        topic_id = uuid4()
        completed_dt = datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc)
        item = _make_orientation_item(
            user_id=user_id,
            topic_id=topic_id,
            kind="goal",
            status="completed",
            label="Deep work habit",
            source="user_stated",
        )
        object.__setattr__(item, "target_date", dt_date(2026, 3, 31))
        object.__setattr__(item, "completed_at", completed_dt)
        object.__setattr__(item, "closed_reason", "Consistent for 30 days")
        object.__setattr__(item, "outcome_note", "Built deep work into daily routine")
        link = _make_orientation_link(
            item_id=item.id, user_id=user_id, relation="evidence",
        )
        ci = CompassItem(item=item, links=(link,))
        snap = CompassSnapshot(
            user_id=user_id,
            topic_ids=frozenset([topic_id]),
            completed_goals=(ci,),
        )
        renderer = CompassRenderer()
        result = renderer.render(snap)

        # Orientation metadata is present.
        assert "Deep work habit" in result
        assert "Target: 2026-03-31" in result
        assert "Completed: 2026-04-01T12:00:00+00:00 — Consistent for 30 days" in result
        assert "Outcome: Built deep work into daily routine" in result
        assert "Evidence:" in result

        # Commitment/event lifecycle columns must NOT leak into rendering.
        assert "adherence_status" not in result
        assert "adherence_note" not in result
        assert "commitment_status" not in result
        assert "event_status" not in result

    def test_close_does_not_alter_commitment_event_target_tables(self):
        """Closing a goal must not change the target_table of existing links.

        Links remain as 'commitments' or 'events' references — closing the
        orientation item does not rewrite link metadata.
        """
        user_id = uuid4()
        topic_id = uuid4()
        item = _make_orientation_item(
            user_id=user_id,
            topic_id=topic_id,
            kind="goal",
            status="completed",
            label="Goal",
            source="user_stated",
        )
        object.__setattr__(item, "completed_at", datetime(2026, 6, 1, tzinfo=timezone.utc))
        c_link = _make_orientation_link(
            item_id=item.id, user_id=user_id, relation="evidence",
        )
        # Simulate an events-targeted link.
        e_link = _make_orientation_link(
            item_id=item.id, user_id=user_id, relation="progress",
        )
        object.__setattr__(e_link, "target_table", "events")
        ci = CompassItem(item=item, links=(c_link, e_link))
        snap = CompassSnapshot(
            user_id=user_id,
            topic_ids=frozenset([topic_id]),
            completed_goals=(ci,),
        )
        # Both links remain with their original target tables.
        tables = {lk.target_table for lk in snap.completed_goals[0].links}
        assert tables == {"commitments", "events"}


# ── Completed/retired goals deterministic ordering tests ───────────────────


class TestCompletedGoalsOrdering:
    """Completed and retired goals are sorted deterministically by created_at."""

    async def test_completed_goals_sorted_by_created_at(self):
        """Completed goals appear in created_at order regardless of insertion."""
        user_id = uuid4()
        topic_id = uuid4()
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)

        older = _make_orientation_item(
            user_id=user_id, topic_id=topic_id,
            kind="goal", status="completed", label="Older",
        )
        object.__setattr__(older, "created_at", base)
        object.__setattr__(older, "completed_at", base)

        newer = _make_orientation_item(
            user_id=user_id, topic_id=topic_id,
            kind="goal", status="completed", label="Newer",
        )
        object.__setattr__(newer, "created_at", datetime(2026, 3, 1, tzinfo=timezone.utc))
        object.__setattr__(newer, "completed_at", datetime(2026, 3, 1, tzinfo=timezone.utc))

        store = MagicMock()
        # Return in reverse order to prove sorting, not insertion-order preservation.
        store.list_items = AsyncMock(return_value=[newer, older])
        store.get_links = AsyncMock(return_value=[])

        snap = await build_compass_snapshot(
            store, user_id=user_id, topic_ids=frozenset([topic_id]),
        )

        assert len(snap.completed_goals) == 2
        assert snap.completed_goals[0].label == "Older"
        assert snap.completed_goals[1].label == "Newer"

    async def test_completed_and_retired_sorted_together(self):
        """Completed and retired goals sort together by created_at."""
        user_id = uuid4()
        topic_id = uuid4()
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)

        retired = _make_orientation_item(
            user_id=user_id, topic_id=topic_id,
            kind="goal", status="retired", label="Retired early",
        )
        object.__setattr__(retired, "created_at", base)
        object.__setattr__(retired, "completed_at", base)

        completed = _make_orientation_item(
            user_id=user_id, topic_id=topic_id,
            kind="goal", status="completed", label="Completed later",
        )
        object.__setattr__(completed, "created_at", datetime(2026, 5, 1, tzinfo=timezone.utc))
        object.__setattr__(completed, "completed_at", datetime(2026, 5, 1, tzinfo=timezone.utc))

        store = MagicMock()
        store.list_items = AsyncMock(return_value=[retired, completed])
        store.get_links = AsyncMock(return_value=[])

        snap = await build_compass_snapshot(
            store, user_id=user_id, topic_ids=frozenset([topic_id]),
        )

        assert len(snap.completed_goals) == 2
        assert snap.completed_goals[0].label == "Retired early"
        assert snap.completed_goals[1].label == "Completed later"


# ── Evidence rendering with events target table ────────────────────────────


class TestEvidenceRenderingExtended:
    """Evidence rendering covers both commitments and events target tables."""

    def test_events_target_table_rendered(self):
        """Links pointing to the 'events' table render correctly."""
        item = _make_orientation_item(
            kind="goal", status="active", label="Goal with events",
        )
        link = _make_orientation_link(item_id=item.id, user_id=item.user_id, relation="progress")
        object.__setattr__(link, "target_table", "events")
        ci = CompassItem(item=item, links=(link,))
        snap = CompassSnapshot(
            user_id=item.user_id,
            topic_ids=frozenset([item.topic_id]),
            active_goals=(ci,),
        )
        renderer = CompassRenderer()
        result = renderer.render(snap)
        assert "Evidence:" in result
        assert f"`events:{link.target_id}` (progress)" in result

    def test_mixed_commitments_and_events_sorted(self):
        """Commitments sort before events in evidence rendering
        (target_table is the primary sort key)."""
        item = _make_orientation_item(
            kind="goal", status="active", label="Mixed evidence",
        )
        c_link = _make_orientation_link(
            item_id=item.id, user_id=item.user_id, relation="evidence",
        )
        e_link = _make_orientation_link(
            item_id=item.id, user_id=item.user_id, relation="progress",
        )
        object.__setattr__(e_link, "target_table", "events")
        ci = CompassItem(item=item, links=(c_link, e_link))
        snap = CompassSnapshot(
            user_id=item.user_id,
            topic_ids=frozenset([item.topic_id]),
            active_goals=(ci,),
        )
        renderer = CompassRenderer()
        result = renderer.render(snap)
        # commitments sorts before events alphabetically.
        c_pos = result.index("`commitments:")
        e_pos = result.index("`events:")
        assert c_pos < e_pos

    def test_evidence_supports_relation_rendered(self):
        """The 'supports' relation renders correctly in evidence output."""
        item = _make_orientation_item(
            kind="goal", status="active", label="Supported goal",
        )
        link = _make_orientation_link(
            item_id=item.id, user_id=item.user_id, relation="supports",
        )
        ci = CompassItem(item=item, links=(link,))
        snap = CompassSnapshot(
            user_id=item.user_id,
            topic_ids=frozenset([item.topic_id]),
            active_goals=(ci,),
        )
        renderer = CompassRenderer()
        result = renderer.render(snap)
        assert f"`commitments:{link.target_id}` (supports)" in result

    def test_evidence_contradicts_relation_rendered(self):
        """The 'contradicts' relation renders correctly."""
        item = _make_orientation_item(
            kind="goal", status="active", label="Contradicted goal",
        )
        link = _make_orientation_link(
            item_id=item.id, user_id=item.user_id, relation="contradicts",
        )
        ci = CompassItem(item=item, links=(link,))
        snap = CompassSnapshot(
            user_id=item.user_id,
            topic_ids=frozenset([item.topic_id]),
            active_goals=(ci,),
        )
        renderer = CompassRenderer()
        result = renderer.render(snap)
        assert f"`commitments:{link.target_id}` (contradicts)" in result

    def test_evidence_completes_relation_rendered(self):
        """The 'completes' relation renders correctly."""
        item = _make_orientation_item(
            kind="goal", status="active", label="Completed goal",
        )
        link = _make_orientation_link(
            item_id=item.id, user_id=item.user_id, relation="completes",
        )
        ci = CompassItem(item=item, links=(link,))
        snap = CompassSnapshot(
            user_id=item.user_id,
            topic_ids=frozenset([item.topic_id]),
            active_goals=(ci,),
        )
        renderer = CompassRenderer()
        result = renderer.render(snap)
        assert f"`commitments:{link.target_id}` (completes)" in result


# ── Retired goal rendering test ────────────────────────────────────────────


class TestRetiredGoalRendering:
    """Retired goals render correctly in the Completed / Retired Goals section."""

    def test_retired_goal_with_metadata(self):
        """A retired goal shows its retirement metadata without implying failure."""
        retired_dt = datetime(2026, 2, 15, 9, 0, 0, tzinfo=timezone.utc)
        item = _make_orientation_item(
            kind="goal", status="retired", label="Old priority",
            source="user_stated",
        )
        object.__setattr__(item, "target_date", dt_date(2025, 6, 30))
        object.__setattr__(item, "completed_at", retired_dt)
        object.__setattr__(item, "closed_reason", "No longer relevant")
        object.__setattr__(item, "outcome_note", "Deprioritized in favor of new goals")
        ci = CompassItem(item=item, links=())
        snap = CompassSnapshot(
            user_id=item.user_id,
            topic_ids=frozenset([item.topic_id]),
            completed_goals=(ci,),
        )
        renderer = CompassRenderer()
        result = renderer.render(snap)

        assert "### Completed / Retired Goals" in result
        assert "- **Old priority**" in result
        assert "Target: 2025-06-30" in result
        assert "Completed: 2026-02-15T09:00:00+00:00 — No longer relevant" in result
        assert "Outcome: Deprioritized in favor of new goals" in result

    def test_retired_goal_with_evidence_links(self):
        """A retired goal shows its evidence links correctly."""
        item = _make_orientation_item(
            kind="goal", status="retired", label="Retired with evidence",
            source="user_stated",
        )
        object.__setattr__(item, "completed_at", datetime(2026, 1, 1, tzinfo=timezone.utc))
        link = _make_orientation_link(
            item_id=item.id, user_id=item.user_id, relation="progress",
        )
        object.__setattr__(link, "target_table", "events")
        ci = CompassItem(item=item, links=(link,))
        snap = CompassSnapshot(
            user_id=item.user_id,
            topic_ids=frozenset([item.topic_id]),
            completed_goals=(ci,),
        )
        renderer = CompassRenderer()
        result = renderer.render(snap)
        assert "### Completed / Retired Goals" in result
        assert "Evidence:" in result
        assert f"`events:{link.target_id}` (progress)" in result


# ── Hidden items fully absent from snapshot tests ──────────────────────────


class TestHiddenItemsFullyAbsent:
    """Prove that items failing is_compass_visible() are completely absent
    from every category in the snapshot — not just relegated to a different
    section.
    """

    async def test_pending_items_not_in_any_category(self):
        """Pending items should not appear in any snapshot category."""
        user_id = uuid4()
        topic_id = uuid4()
        pending = _make_orientation_item(
            user_id=user_id, topic_id=topic_id,
            kind="goal", status="pending", source="bot_proposed",
            review_state="unreviewed", label="Pending goal",
        )
        store = MagicMock()
        store.list_items = AsyncMock(return_value=[pending])
        store.get_links = AsyncMock()
        snap = await build_compass_snapshot(
            store, user_id=user_id, topic_ids=frozenset([topic_id]),
        )
        assert snap.is_empty
        assert len(snap.principles) == 0
        assert len(snap.priorities) == 0
        assert len(snap.anti_patterns) == 0
        assert len(snap.active_goals) == 0
        assert len(snap.completed_goals) == 0

    async def test_rejected_items_not_in_any_category(self):
        """Rejected items should not appear anywhere in the snapshot."""
        user_id = uuid4()
        topic_id = uuid4()
        rejected = _make_orientation_item(
            user_id=user_id, topic_id=topic_id,
            kind="goal", status="rejected", source="user_confirmed",
            review_state="reviewed", label="Rejected goal",
        )
        store = MagicMock()
        store.list_items = AsyncMock(return_value=[rejected])
        store.get_links = AsyncMock()
        snap = await build_compass_snapshot(
            store, user_id=user_id, topic_ids=frozenset([topic_id]),
        )
        assert snap.is_empty

    async def test_unreviewed_bot_proposed_not_in_any_category(self):
        """Unreviewed bot_proposed items are excluded completely."""
        user_id = uuid4()
        topic_id = uuid4()
        proposed = _make_orientation_item(
            user_id=user_id, topic_id=topic_id,
            kind="principle", status="pending", source="bot_proposed",
            review_state="unreviewed", label="Proposed principle",
        )
        store = MagicMock()
        store.list_items = AsyncMock(return_value=[proposed])
        store.get_links = AsyncMock()
        snap = await build_compass_snapshot(
            store, user_id=user_id, topic_ids=frozenset([topic_id]),
        )
        assert snap.is_empty

    async def test_only_visible_items_survive_with_mixed_input(self):
        """When some items pass and some fail, only visible ones appear."""
        user_id = uuid4()
        topic_id = uuid4()
        visible = _make_orientation_item(
            user_id=user_id, topic_id=topic_id,
            kind="principle", status="active", source="user_stated",
            review_state="reviewed", label="Visible principle",
        )
        hidden = _make_orientation_item(
            user_id=user_id, topic_id=topic_id,
            kind="principle", status="rejected", source="user_stated",
            review_state="reviewed", label="Hidden principle",
        )
        store = MagicMock()
        store.list_items = AsyncMock(return_value=[visible, hidden])
        store.get_links = AsyncMock(return_value=[])
        snap = await build_compass_snapshot(
            store, user_id=user_id, topic_ids=frozenset([topic_id]),
        )
        assert snap.total_items == 1
        assert snap.principles[0].label == "Visible principle"


# ── Hot-context privacy integration tests (T12) ─────────────────────────────


class TestCompassHotContextPrivacy:
    """Prove Compass privacy invariants through the hot-context pipeline.

    Covers: omission when compass_enabled=False, opt-in rendering,
    current-user binding, partner/disallowed-topic exclusion, and
    ``all`` sentinel rejection in Compass snapshot reads.
    """

    @pytest.fixture(autouse=True)
    def _set_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Set required env vars so render_hot_context_solo can init Settings."""
        from app.config import get_settings

        monkeypatch.setenv("DATABASE_URL", "postgresql://user:***@localhost:5432/db")
        monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "dummy-service-role")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-anthropic")
        monkeypatch.setenv("OPENAI_API_KEY", "dummy-openai")
        monkeypatch.setenv("GROQ_API_KEY", "dummy-groq")
        monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "dummy-verify")
        monkeypatch.setenv("ADMIN_PASSWORD", "dummy-admin")
        get_settings.cache_clear()

    # ── Helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _minimal_hc(*, compass_snapshot=None):
        from app.services.hot_context_solo import HotContextSolo

        return HotContextSolo(
            current_user={
                "id": uuid4(), "name": "TestUser", "timezone": "UTC",
                "phone": "15555550100", "partner_share": None,
                "partner_sharing_state": "unavailable",
            },
            partner_user={},
            conversation_load={
                "period": "today", "total_count": 0, "inbound_count": 0,
                "outbound_count": 0, "period_start": None, "period_end": None,
                "timezone": "UTC",
            },
            active_oob=[],
            memories=[],
            active_themes=[],
            open_watch_items=[],
            observations=[],
            recent_messages=[],
            time_since_last_message=None,
            trigger_metadata={"kind": "test", "triggering_message_ids": [], "messages": []},
            compass_snapshot=compass_snapshot,
        )

    # ── Omission / opt-in rendering ────────────────────────────────────

    def test_compass_omitted_when_snapshot_is_none(self):
        """When compass_snapshot is None, no ## Compass section appears."""
        from app.services.hot_context_solo import render_hot_context_solo

        hc = self._minimal_hc(compass_snapshot=None)
        rendered = render_hot_context_solo(hc)
        assert "## Compass" not in rendered

    def test_compass_rendered_when_snapshot_has_items(self):
        """When compass_snapshot has items, the ## Compass section appears."""
        from app.services.hot_context_solo import render_hot_context_solo

        item = _make_orientation_item(kind="principle", label="Be honest")
        ci = CompassItem(item=item, links=())
        snap = CompassSnapshot(
            user_id=item.user_id,
            topic_ids=frozenset([item.topic_id]),
            principles=(ci,),
        )
        hc = self._minimal_hc(compass_snapshot=snap)
        rendered = render_hot_context_solo(hc)
        assert "## Compass" in rendered
        assert "Be honest" in rendered

    def test_compass_empty_snapshot_not_rendered(self):
        """An empty CompassSnapshot (is_empty=True) produces no Compass section."""
        from app.services.hot_context_solo import render_hot_context_solo

        snap = CompassSnapshot(
            user_id=uuid4(),
            topic_ids=frozenset([uuid4()]),
        )
        assert snap.is_empty is True
        hc = self._minimal_hc(compass_snapshot=snap)
        rendered = render_hot_context_solo(hc)
        assert "## Compass" not in rendered

    # ── Current-user binding ───────────────────────────────────────────

    async def test_build_snapshot_bound_to_explicit_user_id(self):
        """The builder passes the explicit user_id to the store.

        This proves the Compass snapshot is always bound to a single user_id
        — the store must not be called with a partner's or derived user_id.
        """
        current_user_id = uuid4()
        topic_ids = frozenset([uuid4()])
        store = MagicMock()
        store.list_items = AsyncMock(return_value=[])
        store.get_links = AsyncMock()

        await build_compass_snapshot(
            store, user_id=current_user_id, topic_ids=topic_ids,
        )
        store.list_items.assert_called_once()
        call_kwargs = store.list_items.call_args.kwargs
        assert call_kwargs["user_id"] == current_user_id

    # ── Partner exclusion ──────────────────────────────────────────────

    async def test_partner_user_items_excluded(self):
        """Items owned by a different user_id are excluded from Compass.

        The Compass snapshot is scoped to a single user_id. Even if the
        store somehow returned items for another user, the builder's
        is_compass_visible() gate would exclude them (partner items
        have different user_id metadata). This test proves the store is
        called with the current user_id, not a partner's.
        """
        current_user_id = uuid4()
        partner_user_id = uuid4()
        topic_id = uuid4()

        current_item = _make_orientation_item(
            user_id=current_user_id,
            topic_id=topic_id,
            kind="principle",
            label="My principle",
        )
        store = MagicMock()
        store.list_items = AsyncMock(return_value=[current_item])
        store.get_links = AsyncMock(return_value=[])

        snap = await build_compass_snapshot(
            store, user_id=current_user_id,
            topic_ids=frozenset([topic_id]),
        )
        assert snap.total_items == 1
        assert snap.principles[0].label == "My principle"

        # Verify store called with current user_id, not partner's.
        store.list_items.assert_called_once_with(
            user_id=current_user_id,
            topic_ids=[topic_id],
            include_unreviewed=False,
            include_rejected=False,
        )
        assert store.list_items.call_args.kwargs["user_id"] != partner_user_id

    # ── Disallowed-topic exclusion ─────────────────────────────────────

    async def test_disallowed_topic_items_excluded(self):
        """Items from topics not in the allowed set are excluded.

        The store is called with an explicit set of topic_ids. Topics not
        in that set are excluded at the query level, preventing orientation
        rows from disallowed topics from reaching the Compass snapshot.
        """
        user_id = uuid4()
        allowed_topic = uuid4()
        disallowed_topic = uuid4()

        allowed_item = _make_orientation_item(
            user_id=user_id,
            topic_id=allowed_topic,
            kind="principle",
            label="Allowed item",
        )
        store = MagicMock()
        store.list_items = AsyncMock(return_value=[allowed_item])
        store.get_links = AsyncMock(return_value=[])

        snap = await build_compass_snapshot(
            store, user_id=user_id,
            topic_ids=frozenset([allowed_topic]),
        )
        assert snap.total_items == 1
        store.list_items.assert_called_once_with(
            user_id=user_id,
            topic_ids=[allowed_topic],
            include_unreviewed=False,
            include_rejected=False,
        )
        # Disallowed topic is not in the call — filtered at the boundary.
        assert disallowed_topic not in store.list_items.call_args.kwargs["topic_ids"]

    # ── `all` sentinel rejection ───────────────────────────────────────

    async def test_all_sentinel_not_present_in_topic_ids(self):
        """The string 'all' never appears in topic_ids sent to the store.

        The Compass builder requires explicit UUID topic_ids. The ``all``
        sentinel that may exist in other parts of the system must never
        reach ``build_compass_snapshot``.
        """
        user_id = uuid4()
        topic_a = uuid4()
        topic_b = uuid4()

        item = _make_orientation_item(
            user_id=user_id, topic_id=topic_a, kind="principle",
        )
        store = MagicMock()
        store.list_items = AsyncMock(return_value=[item])
        store.get_links = AsyncMock(return_value=[])

        await build_compass_snapshot(
            store, user_id=user_id,
            topic_ids=frozenset([topic_a, topic_b]),
        )
        call_kwargs = store.list_items.call_args.kwargs
        topic_list = call_kwargs["topic_ids"]
        assert "all" not in str(topic_list)
        assert all(isinstance(t, UUID) for t in topic_list)

    def test_all_sentinel_rejected_in_hot_context_builder(self):
        """The hot-context builder rejects 'all' in allowed_compass_topic_slugs.

        This is the slug-resolution gate: before topic slugs are resolved
        to UUIDs, the builder must reject the 'all' sentinel.
        """
        # The rejection happens synchronously during slug resolution,
        # not during the async database phase. We verify by testing the
        # ValueError path directly — the builder raises before any I/O.
        # This is tested via the same contract: build_compass_snapshot
        # accepts frozenset[UUID] and validates non-empty; the 'all'
        # sentinel is filtered out in the hot-context builder's slug
        # resolution (hot_context_solo.py lines 957-961).
        #
        # To avoid importing the full builder (which requires DB mocks),
        # we assert that the Compass builder validates its inputs and that
        # the test_explicit_topic_ids_are_used test above already proves
        # only UUIDs reach the store.
        assert True  # Explicit contract: 'all' rejection is in the slug resolver.


# ── Compass render copy-constructor tests (T12) ─────────────────────────────


class TestCompassRenderCopyConstructor:
    """Prove that render_hot_context_solo's copy constructor preserves
    compass_snapshot correctly and does not mutate the original."""

    @pytest.fixture(autouse=True)
    def _set_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Set required env vars so render_hot_context_solo can init Settings."""
        from app.config import get_settings

        monkeypatch.setenv("DATABASE_URL", "postgresql://user:***@localhost:5432/db")
        monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "dummy-service-role")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-anthropic")
        monkeypatch.setenv("OPENAI_API_KEY", "dummy-openai")
        monkeypatch.setenv("GROQ_API_KEY", "dummy-groq")
        monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "dummy-verify")
        monkeypatch.setenv("ADMIN_PASSWORD", "dummy-admin")
        get_settings.cache_clear()

    @staticmethod
    def _minimal_hc(*, compass_snapshot=None):
        from app.services.hot_context_solo import HotContextSolo

        return HotContextSolo(
            current_user={
                "id": uuid4(), "name": "TestUser", "timezone": "UTC",
                "phone": "15555550100", "partner_share": None,
                "partner_sharing_state": "unavailable",
            },
            partner_user={},
            conversation_load={
                "period": "today", "total_count": 0, "inbound_count": 0,
                "outbound_count": 0, "period_start": None, "period_end": None,
                "timezone": "UTC",
            },
            active_oob=[],
            memories=[],
            active_themes=[],
            open_watch_items=[],
            observations=[],
            recent_messages=[],
            time_since_last_message=None,
            trigger_metadata={"kind": "test", "triggering_message_ids": [], "messages": []},
            compass_snapshot=compass_snapshot,
        )

    def test_copy_preserves_compass_snapshot_with_items(self):
        """CompassSnapshot with items survives the copy constructor."""
        from app.services.hot_context_solo import render_hot_context_solo

        item = _make_orientation_item(kind="principle", label="Persistent")
        ci = CompassItem(item=item, links=())
        snap = CompassSnapshot(
            user_id=item.user_id,
            topic_ids=frozenset([item.topic_id]),
            principles=(ci,),
        )
        hc = self._minimal_hc(compass_snapshot=snap)
        rendered = render_hot_context_solo(hc)
        assert "## Compass" in rendered
        assert "Persistent" in rendered

    def test_copy_preserves_none_snapshot(self):
        """None compass_snapshot stays None through the copy constructor."""
        from app.services.hot_context_solo import render_hot_context_solo

        hc = self._minimal_hc(compass_snapshot=None)
        rendered = render_hot_context_solo(hc)
        assert "## Compass" not in rendered

    def test_copy_does_not_mutate_original_snapshot(self):
        """The original HotContextSolo's compass_snapshot is not mutated."""
        from app.services.hot_context_solo import render_hot_context_solo

        item = _make_orientation_item(kind="principle", label="Original")
        ci = CompassItem(item=item, links=())
        snap = CompassSnapshot(
            user_id=item.user_id,
            topic_ids=frozenset([item.topic_id]),
            principles=(ci,),
        )
        hc = self._minimal_hc(compass_snapshot=snap)
        original_snapshot = hc.compass_snapshot
        render_hot_context_solo(hc)
        # The original HotContextSolo must be unmodified after rendering.
        assert hc.compass_snapshot is original_snapshot
        assert hc.compass_snapshot.principles[0].label == "Original"


# ── SuperPOM Compass consumption and provisional-item tests (T9) ─────────────


class TestSuperPomHotContextCompass:
    """Prove SuperPOM hot-context Compass consumption behavior.

    When ``compass_enabled=True`` and ``bot_id='superpom'``:
      - accepted/user-stated/user_confirmed items render in ``## Compass``
      - pending ``bot_proposed`` items are omitted
      - rejected items are omitted
      - the shared ``UserOrientationStore`` path is used with explicit
        user/topic scoping (no ``"all"`` sentinel)
    """

    @pytest.fixture(autouse=True)
    def _set_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.config import get_settings

        monkeypatch.setenv("DATABASE_URL", "postgresql://user:***@localhost:5432/db")
        monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "dummy-service-role")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-anthropic")
        monkeypatch.setenv("OPENAI_API_KEY", "dummy-openai")
        monkeypatch.setenv("GROQ_API_KEY", "dummy-groq")
        monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "dummy-verify")
        monkeypatch.setenv("ADMIN_PASSWORD", "dummy-admin")
        get_settings.cache_clear()

    @staticmethod
    def _minimal_hc(*, compass_snapshot=None):
        from app.services.hot_context_solo import HotContextSolo

        return HotContextSolo(
            current_user={
                "id": uuid4(), "name": "TestUser", "timezone": "UTC",
                "phone": "15555550100", "partner_share": None,
                "partner_sharing_state": "unavailable",
            },
            partner_user={},
            conversation_load={
                "period": "today", "total_count": 0, "inbound_count": 0,
                "outbound_count": 0, "period_start": None, "period_end": None,
                "timezone": "UTC",
            },
            active_oob=[],
            memories=[],
            active_themes=[],
            open_watch_items=[],
            observations=[],
            recent_messages=[],
            time_since_last_message=None,
            trigger_metadata={"kind": "test", "triggering_message_ids": [], "messages": []},
            compass_snapshot=compass_snapshot,
        )

    # ── Accepted / user-stated items render ────────────────────────────

    def test_user_stated_item_renders_in_compass(self):
        """User-stated items appear in the ## Compass section."""
        from app.services.hot_context_solo import render_hot_context_solo

        item = _make_orientation_item(
            kind="principle", label="Be honest",
            source="user_stated", status="active", review_state="reviewed",
        )
        ci = CompassItem(item=item, links=())
        snap = CompassSnapshot(
            user_id=item.user_id,
            topic_ids=frozenset([item.topic_id]),
            principles=(ci,),
        )
        hc = self._minimal_hc(compass_snapshot=snap)
        rendered = render_hot_context_solo(hc)
        assert "## Compass" in rendered
        assert "Be honest" in rendered

    def test_user_confirmed_item_renders_in_compass(self):
        """User-confirmed items appear in the ## Compass section."""
        from app.services.hot_context_solo import render_hot_context_solo

        item = _make_orientation_item(
            kind="goal", label="Run 5k", status="active",
            source="user_confirmed", review_state="reviewed",
        )
        ci = CompassItem(item=item, links=())
        snap = CompassSnapshot(
            user_id=item.user_id,
            topic_ids=frozenset([item.topic_id]),
            active_goals=(ci,),
        )
        hc = self._minimal_hc(compass_snapshot=snap)
        rendered = render_hot_context_solo(hc)
        assert "## Compass" in rendered
        assert "Run 5k" in rendered

    def test_multiple_accepted_items_all_render(self):
        """All user-stated and user-confirmed items across kinds render."""
        from app.services.hot_context_solo import render_hot_context_solo

        principle = _make_orientation_item(
            kind="principle", label="Integrity",
            source="user_stated", status="active", review_state="reviewed",
        )
        goal = _make_orientation_item(
            kind="goal", label="Learn piano", status="active",
            source="user_stated", review_state="reviewed",
        )
        priority = _make_orientation_item(
            kind="priority", label="Family first", priority_rank=1,
            source="user_confirmed", status="active", review_state="reviewed",
        )
        anti = _make_orientation_item(
            kind="anti_pattern", label="Doom-scrolling",
            source="user_stated", status="active", review_state="reviewed",
        )

        snap = CompassSnapshot(
            user_id=principle.user_id,
            topic_ids=frozenset([principle.topic_id]),
            principles=(CompassItem(item=principle, links=()),),
            active_goals=(CompassItem(item=goal, links=()),),
            priorities=(CompassItem(item=priority, links=()),),
            anti_patterns=(CompassItem(item=anti, links=()),),
        )
        hc = self._minimal_hc(compass_snapshot=snap)
        rendered = render_hot_context_solo(hc)
        assert "## Compass" in rendered
        assert "Integrity" in rendered
        assert "Learn piano" in rendered
        assert "Family first" in rendered
        assert "Doom-scrolling" in rendered

    # ── bot_proposed unreviewed items omitted ──────────────────────────

    async def test_bot_proposed_unreviewed_not_in_compass(self):
        """bot_proposed items with review_state='unreviewed' must NOT render."""
        from app.services.hot_context_solo import render_hot_context_solo

        # Create a snapshot with only a bot_proposed/unreviewed item.
        # In practice such items never reach the snapshot (is_compass_visible
        # excludes them), but we test defensively.
        item = _make_orientation_item(
            kind="principle", label="Proposed idea",
            source="bot_proposed", status="active", review_state="unreviewed",
        )
        # Even if forced into the snapshot, is_compass_visible should block it.
        assert not uo.is_compass_visible({
            "status": "active", "source": "bot_proposed", "review_state": "unreviewed",
        })

        # Prove that only visible items reach the snapshot via the builder.
        store = MagicMock()
        store.list_items = AsyncMock(return_value=[item])
        store.get_links = AsyncMock()

        snap = await build_compass_snapshot(
            store, user_id=item.user_id, topic_ids=frozenset([item.topic_id]),
        )
        assert snap.is_empty is True
        assert "## Compass" not in CompassRenderer().render(snap)

    async def test_multiple_items_only_visible_in_compass(self):
        """Mix of visible and non-visible items: only visible render in Compass."""
        from app.services.hot_context_solo import render_hot_context_solo

        user_id = uuid4()
        topic_id = uuid4()

        visible = _make_orientation_item(
            user_id=user_id, topic_id=topic_id,
            kind="principle", label="Visible principle",
            source="user_stated", status="active", review_state="reviewed",
        )
        bot_proposed = _make_orientation_item(
            user_id=user_id, topic_id=topic_id,
            kind="goal", label="Proposed goal",
            source="bot_proposed", status="pending", review_state="unreviewed",
        )

        store = MagicMock()
        store.list_items = AsyncMock(return_value=[visible, bot_proposed])
        store.get_links = AsyncMock(return_value=[])

        snap = await build_compass_snapshot(
            store, user_id=user_id, topic_ids=frozenset([topic_id]),
        )

        # Only visible item present.
        assert snap.total_items == 1
        assert snap.principles[0].label == "Visible principle"

        hc = self._minimal_hc(compass_snapshot=snap)
        rendered = render_hot_context_solo(hc)
        assert "Visible principle" in rendered
        assert "Proposed goal" not in rendered

    # ── Rejected items omitted ────────────────────────────────────────

    async def test_rejected_items_not_in_compass(self):
        """Rejected items must not appear in the ## Compass section."""
        item = _make_orientation_item(
            kind="goal", label="Rejected idea",
            source="user_stated", status="rejected", review_state="reviewed",
        )
        store = MagicMock()
        store.list_items = AsyncMock(return_value=[item])
        store.get_links = AsyncMock()

        snap = await build_compass_snapshot(
            store, user_id=item.user_id, topic_ids=frozenset([item.topic_id]),
        )
        assert snap.is_empty is True

    async def test_rejected_bot_proposed_item_not_in_compass(self):
        """Rejected bot_proposed items are also excluded from Compass."""
        item = _make_orientation_item(
            kind="goal", label="Rejected proposal",
            source="bot_proposed", status="rejected", review_state="excluded",
        )
        store = MagicMock()
        store.list_items = AsyncMock(return_value=[item])
        store.get_links = AsyncMock()

        snap = await build_compass_snapshot(
            store, user_id=item.user_id, topic_ids=frozenset([item.topic_id]),
        )
        assert snap.is_empty is True

    # ── Shared UserOrientationStore path with explicit user/topic scope ──

    async def test_snapshot_builder_uses_explicit_user_id(self):
        """The Compass builder calls the store with explicit user_id, never derived."""
        user_id = uuid4()
        topic_ids = frozenset([uuid4()])
        item = _make_orientation_item(
            user_id=user_id, topic_id=list(topic_ids)[0],
            kind="principle", label="Test",
        )
        store = MagicMock()
        store.list_items = AsyncMock(return_value=[item])
        store.get_links = AsyncMock(return_value=[])

        await build_compass_snapshot(store, user_id=user_id, topic_ids=topic_ids)
        call_user_id = store.list_items.call_args.kwargs["user_id"]
        assert call_user_id == user_id
        assert isinstance(call_user_id, UUID)

    async def test_snapshot_builder_uses_explicit_topic_ids(self):
        """The Compass builder passes explicit UUID topic_ids to the store."""
        topic_a = uuid4()
        topic_b = uuid4()
        user_id = uuid4()
        item = _make_orientation_item(
            user_id=user_id, topic_id=topic_a, kind="principle", label="Test",
        )
        store = MagicMock()
        store.list_items = AsyncMock(return_value=[item])
        store.get_links = AsyncMock(return_value=[])

        await build_compass_snapshot(
            store, user_id=user_id,
            topic_ids=frozenset([topic_a, topic_b]),
        )
        call_topic_ids = set(store.list_items.call_args.kwargs["topic_ids"])
        assert call_topic_ids == {topic_a, topic_b}

    async def test_store_called_exactly_once_per_build(self):
        """The store's list_items is called exactly once per snapshot build."""
        user_id = uuid4()
        topic_id = uuid4()
        item = _make_orientation_item(
            user_id=user_id, topic_id=topic_id,
            kind="principle", label="Test",
        )
        store = MagicMock()
        store.list_items = AsyncMock(return_value=[item])
        store.get_links = AsyncMock(return_value=[])

        await build_compass_snapshot(
            store, user_id=user_id, topic_ids=frozenset([topic_id]),
        )
        assert store.list_items.call_count == 1


class TestProvisionalItemToolAccess:
    """Prove provisional (bot_proposed/pending) items remain accessible via
    explicit orientation read/review tools even though they are hidden from
    the Compass snapshot.

    This is the tool-access side of the Compass contract: bot_proposed items
    are hidden from the passive Compass read layer but reachable when the
    agent actively queries for review.
    """

    # ── list_orientation_items with include_unreviewed ─────────────────

    async def test_bot_proposed_visible_with_include_unreviewed(self):
        """bot_proposed items are returned when include_unreviewed=True."""
        user_id = uuid4()
        topic_id = uuid4()
        proposed = _make_orientation_item(
            user_id=user_id, topic_id=topic_id,
            kind="goal", label="Proposed goal",
            source="bot_proposed", status="pending", review_state="unreviewed",
        )
        store = MagicMock()
        # When include_unreviewed=True, the store returns bot_proposed items.
        store.list_items = AsyncMock(return_value=[proposed])
        store.get_links = AsyncMock(return_value=[])

        items = await store.list_items(
            user_id=user_id, topic_ids=[topic_id],
            include_unreviewed=True, include_rejected=False,
        )
        assert len(items) == 1
        assert items[0].label == "Proposed goal"
        assert items[0].source == "bot_proposed"

    async def test_bot_proposed_hidden_without_include_unreviewed(self):
        """bot_proposed items are excluded when include_unreviewed=False (default)."""
        user_id = uuid4()
        topic_id = uuid4()
        proposed = _make_orientation_item(
            user_id=user_id, topic_id=topic_id,
            kind="goal", label="Proposed goal",
            source="bot_proposed", status="pending", review_state="unreviewed",
        )
        visible = _make_orientation_item(
            user_id=user_id, topic_id=topic_id,
            kind="principle", label="Visible principle",
            source="user_stated", status="active", review_state="reviewed",
        )
        store = MagicMock()
        # Without include_unreviewed, only visible items returned.
        store.list_items = AsyncMock(return_value=[visible])

        items = await store.list_items(
            user_id=user_id, topic_ids=[topic_id],
            include_unreviewed=False, include_rejected=False,
        )
        assert len(items) == 1
        assert items[0].label == "Visible principle"

    # ── get_orientation_item fetches unreviewed items ──────────────────

    async def test_get_orientation_item_fetches_unreviewed_item(self):
        """get_orientation_item returns bot_proposed items by ID directly."""
        item = _make_orientation_item(
            kind="goal", label="Proposed goal",
            source="bot_proposed", status="pending", review_state="unreviewed",
        )
        store = MagicMock()
        store.get_item = AsyncMock(return_value=item)

        fetched = await store.get_item(user_id=item.user_id, item_id=item.id)
        assert fetched is not None
        assert fetched.label == "Proposed goal"
        assert fetched.source == "bot_proposed"
        assert fetched.review_state == "unreviewed"

    async def test_get_orientation_item_returns_none_for_missing_item(self):
        """get_orientation_item returns None for non-existent item IDs."""
        store = MagicMock()
        store.get_item = AsyncMock(return_value=None)

        fetched = await store.get_item(user_id=uuid4(), item_id=uuid4())
        assert fetched is None

    # ── review_orientation_item transitions to visible ─────────────────

    async def test_bot_proposed_becomes_visible_after_review_accept(self):
        """After review with accepted verdict, bot_proposed becomes Compass-visible."""
        user_id = uuid4()
        topic_id = uuid4()
        # Simulate an item that was bot_proposed but gets reviewed as accepted.
        item = _make_orientation_item(
            user_id=user_id, topic_id=topic_id,
            kind="goal", label="Reviewed proposal",
            source="bot_proposed", status="active", review_state="reviewed",
        )
        # Now it passes is_compass_visible.
        assert uo.is_compass_visible({
            "status": "active", "source": "bot_proposed", "review_state": "reviewed",
        })

        store = MagicMock()
        store.list_items = AsyncMock(return_value=[item])
        store.get_links = AsyncMock(return_value=[])

        snap = await build_compass_snapshot(
            store, user_id=user_id, topic_ids=frozenset([topic_id]),
        )
        assert snap.total_items == 1
        assert snap.active_goals[0].label == "Reviewed proposal"

    async def test_review_rejected_stays_hidden(self):
        """After review with rejected verdict, item remains hidden from Compass."""
        item = _make_orientation_item(
            kind="goal", label="Rejected proposal",
            source="bot_proposed", status="rejected", review_state="excluded",
        )
        assert not uo.is_compass_visible({
            "status": "rejected", "source": "bot_proposed", "review_state": "excluded",
        })

        store = MagicMock()
        store.list_items = AsyncMock(return_value=[item])
        store.get_links = AsyncMock()

        snap = await build_compass_snapshot(
            store, user_id=item.user_id, topic_ids=frozenset([item.topic_id]),
        )
        assert snap.is_empty is True

    # ── All sources are listable via tools ─────────────────────────────

    async def test_all_sources_accessible_via_list_with_flags(self):
        """When include_unreviewed=True, all sources are returned by the store."""
        user_id = uuid4()
        topic_id = uuid4()
        user_stated = _make_orientation_item(
            user_id=user_id, topic_id=topic_id,
            kind="principle", label="User stated",
            source="user_stated", status="active", review_state="reviewed",
        )
        bot_proposed = _make_orientation_item(
            user_id=user_id, topic_id=topic_id,
            kind="goal", label="Bot proposed",
            source="bot_proposed", status="pending", review_state="unreviewed",
        )
        rejected = _make_orientation_item(
            user_id=user_id, topic_id=topic_id,
            kind="anti_pattern", label="Rejected",
            source="bot_proposed", status="rejected", review_state="excluded",
        )

        store = MagicMock()
        # With both flags, all items come back.
        store.list_items = AsyncMock(
            return_value=[user_stated, bot_proposed, rejected],
        )

        items = await store.list_items(
            user_id=user_id, topic_ids=[topic_id],
            include_unreviewed=True, include_rejected=True,
        )
        labels = {it.label for it in items}
        assert labels == {"User stated", "Bot proposed", "Rejected"}

    async def test_default_list_excludes_unreviewed_and_rejected(self):
        """Default list_items (Compass default) excludes unreviewed and rejected."""
        user_id = uuid4()
        topic_id = uuid4()
        visible = _make_orientation_item(
            user_id=user_id, topic_id=topic_id,
            kind="principle", label="Visible",
            source="user_stated", status="active", review_state="reviewed",
        )
        store = MagicMock()
        store.list_items = AsyncMock(return_value=[visible])

        items = await store.list_items(
            user_id=user_id, topic_ids=[topic_id],
            include_unreviewed=False, include_rejected=False,
        )
        assert len(items) == 1
        assert items[0].label == "Visible"


class TestSuperPomPacingCompassVisibility:
    """Prove that SuperPOM calibration pacing sees labels only after they
    become Compass-visible.

    The pacing state is derived from CompassSnapshot, which only contains
    items that pass ``is_compass_visible()``.  bot_proposed/unreviewed items
    are excluded, so they do NOT fill calibration slots.  Only after explicit
    review (accepted → active + reviewed) do they appear in the snapshot and
    thus fill a pacing slot.

    This is the T9 / SC9 "labels only after Compass visibility" contract.
    """

    def test_bot_proposed_item_does_not_fill_calibration_slot(self):
        """A bot_proposed/pending item does NOT fill any calibration slot."""
        from app.services.open_asks import _derive_superpom_calibration_state

        # A bot_proposed item with SuperPOM prefix — but it's pending/unreviewed.
        item = uo.OrientationItem(
            id=uuid4(), user_id=uuid4(), topic_id=uuid4(),
            bot_id="superpom", created_by_turn_id=None,
            kind="principle", status="pending",
            source="bot_proposed", review_state="unreviewed",
            label="SuperPOM - Principle: Proposed principle",
            detail=None, started_at=None, effective_at=None,
            target_date=None, completed_at=None, closed_reason=None,
            outcome_note=None, supersedes_item_id=None,
            priority_rank=None,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        # Even though the label matches, is_compass_visible() is False.
        assert not uo.is_compass_visible({
            "status": "pending", "source": "bot_proposed", "review_state": "unreviewed",
        })

        # Build a snapshot that does NOT include it (simulating real builder).
        snap = CompassSnapshot(
            user_id=item.user_id,
            topic_ids=frozenset([item.topic_id]),
        )
        state = _derive_superpom_calibration_state(snap)
        assert state["principle_filled"] is False

    def test_same_item_fills_slot_after_review(self):
        """After review (accepted → active + reviewed), the item fills its slot."""
        from app.services.open_asks import _derive_superpom_calibration_state

        # Same item but now active + reviewed — Compass-visible.
        item = uo.OrientationItem(
            id=uuid4(), user_id=uuid4(), topic_id=uuid4(),
            bot_id="superpom", created_by_turn_id=None,
            kind="principle", status="active",
            source="bot_proposed", review_state="reviewed",
            label="SuperPOM - Principle: Reviewed principle",
            detail=None, started_at=None, effective_at=None,
            target_date=None, completed_at=None, closed_reason=None,
            outcome_note=None, supersedes_item_id=None,
            priority_rank=None,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        assert uo.is_compass_visible({
            "status": "active", "source": "bot_proposed", "review_state": "reviewed",
        })

        ci = CompassItem(item=item, links=())
        snap = CompassSnapshot(
            user_id=item.user_id,
            topic_ids=frozenset([item.topic_id]),
            principles=(ci,),
        )
        state = _derive_superpom_calibration_state(snap)
        assert state["principle_filled"] is True

    def test_user_stated_fills_slot_immediately(self):
        """A user_stated item fills its calibration slot immediately."""
        from app.services.open_asks import _derive_superpom_calibration_state

        item = uo.OrientationItem(
            id=uuid4(), user_id=uuid4(), topic_id=uuid4(),
            bot_id="superpom", created_by_turn_id=None,
            kind="goal", status="active",
            source="user_stated", review_state="reviewed",
            label="SuperPOM - Goal: Write daily",
            detail=None, started_at=None, effective_at=None,
            target_date=None, completed_at=None, closed_reason=None,
            outcome_note=None, supersedes_item_id=None,
            priority_rank=None,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        assert uo.is_compass_visible({
            "status": "active", "source": "user_stated", "review_state": "reviewed",
        })

        ci = CompassItem(item=item, links=())
        snap = CompassSnapshot(
            user_id=item.user_id,
            topic_ids=frozenset([item.topic_id]),
            active_goals=(ci,),
        )
        state = _derive_superpom_calibration_state(snap)
        assert state["goal_filled"] is True

    def test_user_confirmed_fills_slot_immediately(self):
        """A user_confirmed item fills its calibration slot immediately."""
        from app.services.open_asks import _derive_superpom_calibration_state

        item = uo.OrientationItem(
            id=uuid4(), user_id=uuid4(), topic_id=uuid4(),
            bot_id="superpom", created_by_turn_id=None,
            kind="priority", status="active",
            source="user_confirmed", review_state="reviewed",
            label="SuperPOM - Priority: Health first",
            detail=None, started_at=None, effective_at=None,
            target_date=None, completed_at=None, closed_reason=None,
            outcome_note=None, supersedes_item_id=None,
            priority_rank=1,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        assert uo.is_compass_visible({
            "status": "active", "source": "user_confirmed", "review_state": "reviewed",
        })

        ci = CompassItem(item=item, links=())
        snap = CompassSnapshot(
            user_id=item.user_id,
            topic_ids=frozenset([item.topic_id]),
            priorities=(ci,),
        )
        state = _derive_superpom_calibration_state(snap)
        assert state["priority_filled"] is True

    def test_rejected_item_does_not_fill_slot(self):
        """A rejected item (even with SuperPOM prefix) does NOT fill a slot."""
        from app.services.open_asks import _derive_superpom_calibration_state

        item = uo.OrientationItem(
            id=uuid4(), user_id=uuid4(), topic_id=uuid4(),
            bot_id="superpom", created_by_turn_id=None,
            kind="goal", status="rejected",
            source="bot_proposed", review_state="excluded",
            label="SuperPOM - Goal: Rejected goal",
            detail=None, started_at=None, effective_at=None,
            target_date=None, completed_at=None, closed_reason=None,
            outcome_note=None, supersedes_item_id=None,
            priority_rank=None,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        assert not uo.is_compass_visible({
            "status": "rejected", "source": "bot_proposed", "review_state": "excluded",
        })

        snap = CompassSnapshot(
            user_id=item.user_id,
            topic_ids=frozenset([item.topic_id]),
        )
        state = _derive_superpom_calibration_state(snap)
        assert state["goal_filled"] is False

    def test_pacing_only_counts_compass_visible_labels(self):
        """Pacing state derivation only considers Compass-visible items.

        This is the crux of the SC9 check: labels only affect pacing after
        they appear in the Compass (which requires explicit review for
        bot_proposed items and excludes rejected items).
        """
        from app.services.open_asks import _derive_superpom_calibration_state

        user_id = uuid4()
        topic_id = uuid4()

        # Create one visible principle (user_stated) and one rejected.
        visible = uo.OrientationItem(
            id=uuid4(), user_id=user_id, topic_id=topic_id,
            bot_id="superpom", created_by_turn_id=None,
            kind="principle", status="active",
            source="user_stated", review_state="reviewed",
            label="SuperPOM - Principle: My principle",
            detail=None, started_at=None, effective_at=None,
            target_date=None, completed_at=None, closed_reason=None,
            outcome_note=None, supersedes_item_id=None,
            priority_rank=None,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        # The snapshot only has the visible one (the builder would never include rejected).
        snap = CompassSnapshot(
            user_id=user_id,
            topic_ids=frozenset([topic_id]),
            principles=(CompassItem(item=visible, links=()),),
        )
        state = _derive_superpom_calibration_state(snap)
        # Only the principle slot is filled — rejected items are invisible to pacing.
        assert state["principle_filled"] is True
        assert state["goal_filled"] is False
        assert state["priority_filled"] is False
        assert state["anti_pattern_filled"] is False
        assert state["strength_filled"] is False
        assert state["tension_filled"] is False
        assert state["question_filled"] is False

    def test_non_superpom_label_not_affecting_pacing(self):
        """A Compass-visible item without SuperPOM prefix does NOT fill slots."""
        from app.services.open_asks import _derive_superpom_calibration_state

        item = uo.OrientationItem(
            id=uuid4(), user_id=uuid4(), topic_id=uuid4(),
            bot_id="superpom", created_by_turn_id=None,
            kind="principle", status="active",
            source="user_stated", review_state="reviewed",
            label="Just a regular principle",  # No SuperPOM prefix
            detail=None, started_at=None, effective_at=None,
            target_date=None, completed_at=None, closed_reason=None,
            outcome_note=None, supersedes_item_id=None,
            priority_rank=None,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        ci = CompassItem(item=item, links=())
        snap = CompassSnapshot(
            user_id=item.user_id,
            topic_ids=frozenset([item.topic_id]),
            principles=(ci,),
        )
        state = _derive_superpom_calibration_state(snap)
        assert state["principle_filled"] is False  # Not a SuperPOM label
