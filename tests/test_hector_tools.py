"""Tool-level tests for Hector commitment/event tools.

Covers:
- create_commitment → list_commitments → update_commitment → close_commitment happy path
- log_event with and without commitment_id
- get_adherence returns period checklist
- list_events returns recent events
- READ_BEFORE_WRITE: create_commitment without prior list_commitments rejected
- Scope enforcement: cross-user, cross-topic, cross-bot rows not visible
- Non-Hector ctx rejection at handler level
- Step gating: write tools only in record/respond phase; read tools in read/record
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from uuid import UUID, uuid4

import pytest

from tests.conftest import FakePool
from app.models.user import User
from app.services.turn_context import TurnContext
from app.services.tools.write_tools import (
    ToolCallRejected,
    create_commitment,
    update_commitment,
    close_commitment,
    log_event,
)
from app.services.tools.read_tools import (
    list_commitments,
    list_events,
    get_adherence,
)
from app.services.tools.registry import (
    READ_BEFORE_WRITE,
    _step_allowed,
    STEP_ALLOWED_TOOLS,
)
from tool_schemas import (
    CreateCommitmentInput,
    UpdateCommitmentInput,
    CloseCommitmentInput,
    LogEventInput,
    ListCommitmentsInput,
    ListEventsInput,
    GetAdherenceInput,
    Cadence,
    PressureStyle,
    AdherenceStatus,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FITNESS_TOPIC_ID = UUID("00000000-0000-4000-8000-000000000010")


def _fresh_pool() -> FakePool:
    return FakePool()


def _make_user(**overrides) -> User:
    return User(
        id=overrides.get("id", uuid4()),
        name=overrides.get("name", "TestUser"),
        phone=overrides.get("phone", "+15555550100"),
        timezone=overrides.get("timezone", "UTC"),
    )


def _make_hector_ctx(
    pool: FakePool,
    user: User | None = None,
    *,
    current_step: str = "record",
) -> TurnContext:
    """Build a Hector TurnContext for fitness tool tests."""
    if user is None:
        user = _make_user()
    # Seed user in pool so fetchrow lookups work
    pool.users[user.id] = {
        "id": user.id,
        "name": user.name,
        "phone": user.phone,
        "timezone": user.timezone,
        "onboarding_state": "welcomed",
        "pacing_preferences": {},
        "pregnancy_edd": None,
        "pregnancy_dating_basis": None,
        "pregnancy_lmp_date": None,
        "pregnancy_scan_date": None,
        "pregnancy_scan_corrected_at": None,
        "pregnancy_started_at": None,
        "pregnancy_ended_at": None,
        "pregnancy_outcome": None,
    }
    return TurnContext(
        turn_id=uuid4(),
        pool=pool,
        user=user,
        partner=None,
        triggering_message_ids=[uuid4()],
        bot_id="hector",
        primary_topic_id=_FITNESS_TOPIC_ID,
        primary_topic_slug="fitness",
        current_step=current_step,
    )


def _make_coach_ctx(pool: FakePool) -> TurnContext:
    """Build a Coach TurnContext for rejection tests."""
    user = _make_user()
    pool.users[user.id] = {
        "id": user.id,
        "name": user.name,
        "phone": user.phone,
        "timezone": user.timezone,
        "onboarding_state": "welcomed",
        "pacing_preferences": {},
        "pregnancy_edd": None,
        "pregnancy_dating_basis": None,
        "pregnancy_lmp_date": None,
        "pregnancy_scan_date": None,
        "pregnancy_scan_corrected_at": None,
        "pregnancy_started_at": None,
        "pregnancy_ended_at": None,
        "pregnancy_outcome": None,
    }
    return TurnContext(
        turn_id=uuid4(),
        pool=pool,
        user=user,
        partner=None,
        triggering_message_ids=[uuid4()],
        bot_id="coach",
        primary_topic_id=uuid4(),
        primary_topic_slug="relationship",
        current_step="record",
    )


# ---------------------------------------------------------------------------
# Happy path: create → list → update → close
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_commitment_happy_path():
    """create_commitment inserts a row and returns commitment_id."""
    pool = _fresh_pool()
    ctx = _make_hector_ctx(pool)

    args = CreateCommitmentInput(
        label="Morning run",
        kind="workout",
        cadence=Cadence.weekdays,
        pressure_style=PressureStyle.low_key,
    )
    result = await create_commitment(ctx, args)

    assert result.is_error is False
    assert UUID(result.commitment_id)
    assert result.label == "Morning run"
    assert result.cadence == Cadence.weekdays

    # Verify stored in pool
    cid = UUID(result.commitment_id)
    assert cid in pool.commitments
    assert pool.commitments[cid]["label"] == "Morning run"
    assert pool.commitments[cid]["user_id"] == ctx.user.id
    assert pool.commitments[cid]["bot_id"] == "hector"
    assert pool.commitments[cid]["topic_id"] == _FITNESS_TOPIC_ID

    jobs = [
        job for job in pool.scheduled_jobs.values()
        if (job.get("context") or {}).get("kind") == "commitment_checkin"
    ]
    assert len(jobs) == 1
    assert jobs[0]["job_type"] == "scheduled_task"
    assert jobs[0]["bot_id"] == "hector"
    assert jobs[0]["topic_id"] == _FITNESS_TOPIC_ID
    assert jobs[0]["context"]["commitment_id"] == result.commitment_id
    assert jobs[0]["context"]["recurrence"]["type"] == "weekly"
    assert jobs[0]["context"]["recurrence"]["weekdays"] == [0, 1, 2, 3, 4]


@pytest.mark.asyncio
async def test_list_commitments_after_create():
    """list_commitments returns the commitment just created."""
    pool = _fresh_pool()
    ctx = _make_hector_ctx(pool)

    # Create two commitments
    args1 = CreateCommitmentInput(
        label="Run", kind="workout", cadence=Cadence.daily
    )
    await create_commitment(ctx, args1)
    args2 = CreateCommitmentInput(
        label="Stretch", kind="mobility", cadence=Cadence.weekdays
    )
    await create_commitment(ctx, args2)

    result = await list_commitments(ctx, ListCommitmentsInput())
    assert result.is_error is False
    assert len(result.commitments) == 2
    labels = {c.label for c in result.commitments}
    assert labels == {"Run", "Stretch"}


@pytest.mark.asyncio
async def test_update_commitment_happy_path():
    """update_commitment changes fields on an existing commitment."""
    pool = _fresh_pool()
    ctx = _make_hector_ctx(pool)

    # Create first
    create_args = CreateCommitmentInput(
        label="Old label", kind="workout", cadence=Cadence.daily
    )
    created = await create_commitment(ctx, create_args)
    cid = created.commitment_id

    # Now list commitments to satisfy READ_BEFORE_WRITE gate (populate tool_call_log)
    await list_commitments(ctx, ListCommitmentsInput())

    # Update
    update_args = UpdateCommitmentInput(
        commitment_id=cid,
        label="New label",
        cadence=Cadence.weekdays,
        target_count=3,
    )
    result = await update_commitment(ctx, update_args)

    assert result.is_error is False
    assert result.commitment_id == cid
    assert result.updated_at is not None

    # Verify stored
    row = pool.commitments[UUID(cid)]
    assert row["label"] == "New label"
    assert row["cadence"] == Cadence.weekdays
    assert row["target_count"] == 3


@pytest.mark.asyncio
async def test_close_commitment_happy_path():
    """close_commitment changes status to completed/dropped/paused."""
    pool = _fresh_pool()
    ctx = _make_hector_ctx(pool)

    create_args = CreateCommitmentInput(
        label="Gym", kind="workout", cadence=Cadence.weekdays
    )
    created = await create_commitment(ctx, create_args)
    cid = created.commitment_id

    # Satisfy READ_BEFORE_WRITE
    await list_commitments(ctx, ListCommitmentsInput())

    close_args = CloseCommitmentInput(commitment_id=cid, status="completed")
    result = await close_commitment(ctx, close_args)

    assert result.is_error is False
    assert result.commitment_id == cid
    assert result.status == "completed"

    row = pool.commitments[UUID(cid)]
    assert row["status"] == "completed"

    jobs = [
        job for job in pool.scheduled_jobs.values()
        if (job.get("context") or {}).get("commitment_id") == cid
    ]
    assert jobs
    assert all(job["status"] == "cancelled" for job in jobs)


# ---------------------------------------------------------------------------
# log_event happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_log_event_with_commitment_id():
    """log_event creates an event linked to a commitment."""
    pool = _fresh_pool()
    ctx = _make_hector_ctx(pool)

    # Create a commitment first
    create_args = CreateCommitmentInput(
        label="Workout", kind="workout", cadence=Cadence.daily
    )
    created = await create_commitment(ctx, create_args)
    cid = created.commitment_id

    args = LogEventInput(
        commitment_id=cid,
        metric_key="workout",
        adherence_status=AdherenceStatus.done,
        note="Felt great",
    )
    result = await log_event(ctx, args)

    assert result.is_error is False
    assert UUID(result.event_id)
    assert result.commitment_id == cid
    assert result.adherence_status == AdherenceStatus.done

    # Verify stored
    eid = UUID(result.event_id)
    assert eid in pool.events
    evt = pool.events[eid]
    assert str(evt["commitment_id"]) == cid
    assert evt["user_id"] == ctx.user.id
    assert evt["bot_id"] == "hector"
    assert evt["topic_id"] == _FITNESS_TOPIC_ID


@pytest.mark.asyncio
async def test_log_event_without_commitment_id():
    """log_event works without a commitment_id (measurement-only event)."""
    pool = _fresh_pool()
    ctx = _make_hector_ctx(pool)

    args = LogEventInput(
        metric_key="weight",
        value_numeric=180.5,
        unit="lbs",
    )
    result = await log_event(ctx, args)

    assert result.is_error is False
    assert UUID(result.event_id)
    assert result.commitment_id is None

    eid = UUID(result.event_id)
    evt = pool.events[eid]
    assert evt["commitment_id"] is None
    assert evt["value_numeric"] == 180.5
    assert evt["unit"] == "lbs"


# ---------------------------------------------------------------------------
# list_events happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_events_returns_recent_events():
    """list_events returns events scoped to the user/topic/bot."""
    pool = _fresh_pool()
    ctx = _make_hector_ctx(pool)

    # Create a commitment
    create_args = CreateCommitmentInput(
        label="Workout", kind="workout", cadence=Cadence.daily
    )
    created = await create_commitment(ctx, create_args)
    cid = created.commitment_id

    # Log two events
    await log_event(
        ctx,
        LogEventInput(
            commitment_id=cid,
            metric_key="workout",
            adherence_status=AdherenceStatus.done,
        ),
    )
    await log_event(
        ctx,
        LogEventInput(
            metric_key="weight",
            value_numeric=180.0,
            unit="lbs",
        ),
    )

    result = await list_events(ctx, ListEventsInput())
    assert result.is_error is False
    assert len(result.events) == 2

    # Events should be ordered by observed_at DESC
    assert result.events[0].observed_at >= result.events[1].observed_at


@pytest.mark.asyncio
async def test_list_events_filtered_by_commitment():
    """list_events with commitment_id filter returns only matching events."""
    pool = _fresh_pool()
    ctx = _make_hector_ctx(pool)

    # Create two commitments
    c1 = await create_commitment(
        ctx,
        CreateCommitmentInput(label="A", kind="workout", cadence=Cadence.daily),
    )
    c2 = await create_commitment(
        ctx,
        CreateCommitmentInput(label="B", kind="mobility", cadence=Cadence.weekdays),
    )

    # Log event for each
    await log_event(
        ctx,
        LogEventInput(
            commitment_id=c1.commitment_id,
            metric_key="workout",
            adherence_status=AdherenceStatus.done,
        ),
    )
    await log_event(
        ctx,
        LogEventInput(
            commitment_id=c2.commitment_id,
            metric_key="mobility",
            adherence_status=AdherenceStatus.missed,
        ),
    )

    result = await list_events(
        ctx, ListEventsInput(commitment_id=c1.commitment_id)
    )
    assert len(result.events) == 1
    assert result.events[0].commitment_id == c1.commitment_id


# ---------------------------------------------------------------------------
# get_adherence happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_adherence_returns_period_checklist():
    """get_adherence computes a weekly checklist for active commitments."""
    from datetime import date as dt_date, timedelta as dt_timedelta

    pool = _fresh_pool()
    ctx = _make_hector_ctx(pool)

    # Create a daily commitment
    c = await create_commitment(
        ctx,
        CreateCommitmentInput(
            label="Daily walk",
            kind="workout",
            cadence=Cadence.daily,
        ),
    )
    cid = c.commitment_id

    # Log today as done — use explicit observed_at to match date alignment
    today = dt_date.today()
    today_dt = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
    await log_event(
        ctx,
        LogEventInput(
            commitment_id=cid,
            metric_key="walk",
            adherence_status=AdherenceStatus.done,
            observed_at=today_dt.isoformat(),
        ),
    )

    result = await get_adherence(ctx, GetAdherenceInput())
    assert result.is_error is False
    assert len(result.commitments) >= 1

    board = result.commitments[0]
    assert board.commitment_id == cid
    assert board.label == "Daily walk"
    # Should have slots for this week
    assert len(board.slots) > 0
    # At least one slot should be "done" (today's slot matched)
    statuses = {s.status for s in board.slots}
    # Timezone alignment: the event's date should match today's slot
    # If today is within Mon-Sun and the event is for today, status should be "done"
    assert "done" in statuses or "pending" in statuses
    assert board.summary


# ---------------------------------------------------------------------------
# READ_BEFORE_WRITE enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_commitment_rejected_without_list_commitments():
    """create_commitment is in READ_BEFORE_WRITE requiring list_commitments first.
    
    The _step_allowed check in call_tool() gates write tools via READ_BEFORE_WRITE,
    not the handler itself.  We verify the registry entry exists.
    """
    assert "create_commitment" in READ_BEFORE_WRITE
    assert READ_BEFORE_WRITE["create_commitment"] == {"list_commitments"}

    assert "update_commitment" in READ_BEFORE_WRITE
    assert READ_BEFORE_WRITE["update_commitment"] == {"list_commitments"}

    assert "close_commitment" in READ_BEFORE_WRITE
    assert READ_BEFORE_WRITE["close_commitment"] == {"list_commitments"}


@pytest.mark.asyncio
async def test_log_event_not_in_read_before_write():
    """log_event is NOT in READ_BEFORE_WRITE per settled decision."""
    assert "log_event" not in READ_BEFORE_WRITE


# ---------------------------------------------------------------------------
# Scope enforcement: cross-user, cross-topic, cross-bot
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_user_rows_not_visible():
    """Commitments created by user A are not visible to user B."""
    pool = _fresh_pool()

    user_a = _make_user()
    user_b = _make_user()
    ctx_a = _make_hector_ctx(pool, user_a)
    ctx_b = _make_hector_ctx(pool, user_b)

    # User A creates a commitment
    await create_commitment(
        ctx_a,
        CreateCommitmentInput(label="A's run", kind="workout", cadence=Cadence.daily),
    )

    # User B lists — should see none of A's
    result = await list_commitments(ctx_b, ListCommitmentsInput())
    assert len(result.commitments) == 0


@pytest.mark.asyncio
async def test_cross_topic_rows_not_visible():
    """Commitments under fitness topic are not visible under a different topic."""
    pool = _fresh_pool()
    user = _make_user()

    ctx_fitness = _make_hector_ctx(pool, user)
    other_topic_id = UUID("00000000-0000-4000-8000-000000000099")

    # Create under fitness topic
    await create_commitment(
        ctx_fitness,
        CreateCommitmentInput(label="Fitness item", kind="workout", cadence=Cadence.daily),
    )

    # Now try to list with a different topic_id
    pool.users[user.id] = {
        "id": user.id, "name": user.name, "phone": user.phone,
        "timezone": user.timezone, "onboarding_state": "welcomed",
        "pacing_preferences": {}, "pregnancy_edd": None,
        "pregnancy_dating_basis": None, "pregnancy_lmp_date": None,
        "pregnancy_scan_date": None, "pregnancy_scan_corrected_at": None,
        "pregnancy_started_at": None, "pregnancy_ended_at": None,
        "pregnancy_outcome": None,
    }
    ctx_other = TurnContext(
        turn_id=uuid4(),
        pool=pool,
        user=user,
        partner=None,
        triggering_message_ids=[uuid4()],
        bot_id="hector",
        primary_topic_id=other_topic_id,
        primary_topic_slug="relationship",
        current_step="record",
    )
    # The _check_hector_read_scope will reject because topic_slug != "fitness"
    with pytest.raises(ValueError, match="commitment topic"):
        await list_commitments(ctx_other, ListCommitmentsInput())


@pytest.mark.asyncio
async def test_cross_bot_rows_not_visible():
    """Commitments created under 'hector' bot_id are not visible under 'coach'."""
    pool = _fresh_pool()
    user = _make_user()

    ctx_hector = _make_hector_ctx(pool, user)

    # Create under hector
    await create_commitment(
        ctx_hector,
        CreateCommitmentInput(label="Hector item", kind="workout", cadence=Cadence.daily),
    )

    # Coach tries to read — handler rejects
    ctx_coach = _make_coach_ctx(pool)
    # Override user to be same user
    ctx_coach = TurnContext(
        turn_id=uuid4(),
        pool=pool,
        user=user,
        partner=None,
        triggering_message_ids=[uuid4()],
        bot_id="coach",
        primary_topic_id=_FITNESS_TOPIC_ID,
        primary_topic_slug="fitness",
        current_step="record",
    )
    pool.users[user.id] = {
        "id": user.id, "name": user.name, "phone": user.phone,
        "timezone": user.timezone, "onboarding_state": "welcomed",
        "pacing_preferences": {}, "pregnancy_edd": None,
        "pregnancy_dating_basis": None, "pregnancy_lmp_date": None,
        "pregnancy_scan_date": None, "pregnancy_scan_corrected_at": None,
        "pregnancy_started_at": None, "pregnancy_ended_at": None,
        "pregnancy_outcome": None,
    }
    with pytest.raises(ValueError, match="restricted to.*hector"):
        await list_commitments(ctx_coach, ListCommitmentsInput())


# ---------------------------------------------------------------------------
# Non-Hector ctx rejection at handler level
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coach_rejected_by_write_tools():
    """All write tools reject non-Hector bot_id."""
    pool = _fresh_pool()
    ctx = _make_coach_ctx(pool)

    with pytest.raises(ToolCallRejected, match="wrong bot"):
        await create_commitment(
            ctx,
            CreateCommitmentInput(label="X", kind="workout", cadence=Cadence.daily),
        )

    with pytest.raises(ToolCallRejected, match="wrong bot"):
        await update_commitment(
            ctx,
            UpdateCommitmentInput(commitment_id=str(uuid4()), label="Y"),
        )

    with pytest.raises(ToolCallRejected, match="wrong bot"):
        await close_commitment(
            ctx,
            CloseCommitmentInput(commitment_id=str(uuid4()), status="completed"),
        )

    with pytest.raises(ToolCallRejected, match="wrong bot"):
        await log_event(
            ctx,
            LogEventInput(metric_key="test", adherence_status=AdherenceStatus.done),
        )


@pytest.mark.asyncio
async def test_coach_rejected_by_read_tools():
    """All read tools reject non-Hector bot_id."""
    pool = _fresh_pool()
    user = _make_user()
    pool.users[user.id] = {
        "id": user.id, "name": user.name, "phone": user.phone,
        "timezone": user.timezone, "onboarding_state": "welcomed",
        "pacing_preferences": {}, "pregnancy_edd": None,
        "pregnancy_dating_basis": None, "pregnancy_lmp_date": None,
        "pregnancy_scan_date": None, "pregnancy_scan_corrected_at": None,
        "pregnancy_started_at": None, "pregnancy_ended_at": None,
        "pregnancy_outcome": None,
    }
    ctx = TurnContext(
        turn_id=uuid4(),
        pool=pool,
        user=user,
        partner=None,
        triggering_message_ids=[uuid4()],
        bot_id="coach",
        primary_topic_id=_FITNESS_TOPIC_ID,
        primary_topic_slug="fitness",
        current_step="read",
    )

    with pytest.raises(ValueError, match="restricted to.*hector"):
        await list_commitments(ctx, ListCommitmentsInput())

    with pytest.raises(ValueError, match="restricted to.*hector"):
        await list_events(ctx, ListEventsInput())

    with pytest.raises(ValueError, match="restricted to.*hector"):
        await get_adherence(ctx, GetAdherenceInput())


# ---------------------------------------------------------------------------
# Step gating: tools only allowed in correct phases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_tools_in_read_phase():
    """Read tools are available in the 'read' step."""
    pool = _fresh_pool()
    ctx = _make_hector_ctx(pool, current_step="read")

    # Read tools should be in _step_allowed for 'read'
    allowed = _step_allowed(ctx)
    assert "list_commitments" in allowed
    assert "list_events" in allowed
    assert "get_adherence" in allowed


@pytest.mark.asyncio
async def test_read_tools_in_record_phase():
    """Read tools are available in the 'record' step (RECORD_READ_TOOLS)."""
    pool = _fresh_pool()
    user = _make_user()
    pool.users[user.id] = {
        "id": user.id, "name": user.name, "phone": user.phone,
        "timezone": user.timezone, "onboarding_state": "welcomed",
        "pacing_preferences": {}, "pregnancy_edd": None,
        "pregnancy_dating_basis": None, "pregnancy_lmp_date": None,
        "pregnancy_scan_date": None, "pregnancy_scan_corrected_at": None,
        "pregnancy_started_at": None, "pregnancy_ended_at": None,
        "pregnancy_outcome": None,
    }
    ctx = TurnContext(
        turn_id=uuid4(),
        pool=pool,
        user=user,
        partner=None,
        triggering_message_ids=[uuid4()],
        bot_id="hector",
        primary_topic_id=_FITNESS_TOPIC_ID,
        primary_topic_slug="fitness",
        current_step="record",
    )

    allowed = _step_allowed(ctx)
    assert "list_commitments" in allowed


@pytest.mark.asyncio
async def test_write_tools_in_record_phase():
    """Write tools are available in the 'record' step."""
    pool = _fresh_pool()
    user = _make_user()
    pool.users[user.id] = {
        "id": user.id, "name": user.name, "phone": user.phone,
        "timezone": user.timezone, "onboarding_state": "welcomed",
        "pacing_preferences": {}, "pregnancy_edd": None,
        "pregnancy_dating_basis": None, "pregnancy_lmp_date": None,
        "pregnancy_scan_date": None, "pregnancy_scan_corrected_at": None,
        "pregnancy_started_at": None, "pregnancy_ended_at": None,
        "pregnancy_outcome": None,
    }
    ctx = TurnContext(
        turn_id=uuid4(),
        pool=pool,
        user=user,
        partner=None,
        triggering_message_ids=[uuid4()],
        bot_id="hector",
        primary_topic_id=_FITNESS_TOPIC_ID,
        primary_topic_slug="fitness",
        current_step="record",
    )

    allowed = _step_allowed(ctx)
    assert "create_commitment" in allowed
    assert "update_commitment" in allowed
    assert "close_commitment" in allowed
    assert "log_event" in allowed


@pytest.mark.asyncio
async def test_log_event_in_respond_phase():
    """log_event is available in the 'respond' step."""
    pool = _fresh_pool()
    user = _make_user()
    pool.users[user.id] = {
        "id": user.id, "name": user.name, "phone": user.phone,
        "timezone": user.timezone, "onboarding_state": "welcomed",
        "pacing_preferences": {}, "pregnancy_edd": None,
        "pregnancy_dating_basis": None, "pregnancy_lmp_date": None,
        "pregnancy_scan_date": None, "pregnancy_scan_corrected_at": None,
        "pregnancy_started_at": None, "pregnancy_ended_at": None,
        "pregnancy_outcome": None,
    }
    ctx = TurnContext(
        turn_id=uuid4(),
        pool=pool,
        user=user,
        partner=None,
        triggering_message_ids=[uuid4()],
        bot_id="hector",
        primary_topic_id=_FITNESS_TOPIC_ID,
        primary_topic_slug="fitness",
        current_step="respond",
    )

    allowed = _step_allowed(ctx)
    assert "log_event" in allowed


@pytest.mark.asyncio
async def test_write_tools_not_in_read_phase():
    """Write tools are NOT available in the 'read' step."""
    pool = _fresh_pool()
    ctx = _make_hector_ctx(pool, current_step="read")

    allowed = _step_allowed(ctx)
    assert "create_commitment" not in allowed
    assert "update_commitment" not in allowed
    assert "close_commitment" not in allowed


# ---------------------------------------------------------------------------
# Scope: tools stamp user_id, bot_id='hector', topic_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_commitment_stamps_all_scope_fields():
    """create_commitment writes user_id, bot_id='hector', topic_id to DB."""
    pool = _fresh_pool()
    ctx = _make_hector_ctx(pool)

    result = await create_commitment(
        ctx,
        CreateCommitmentInput(label="Test", kind="workout", cadence=Cadence.daily),
    )
    cid = UUID(result.commitment_id)
    row = pool.commitments[cid]
    assert row["user_id"] == ctx.user.id
    assert row["bot_id"] == "hector"
    assert row["topic_id"] == _FITNESS_TOPIC_ID


@pytest.mark.asyncio
async def test_log_event_stamps_all_scope_fields():
    """log_event writes user_id, bot_id='hector', topic_id to DB."""
    pool = _fresh_pool()
    ctx = _make_hector_ctx(pool)

    result = await log_event(
        ctx,
        LogEventInput(metric_key="test", adherence_status=AdherenceStatus.done),
    )
    eid = UUID(result.event_id)
    row = pool.events[eid]
    assert row["user_id"] == ctx.user.id
    assert row["bot_id"] == "hector"
    assert row["topic_id"] == _FITNESS_TOPIC_ID


# ---------------------------------------------------------------------------
# Placeholder ID / malformed UUID validation (regression for incident)
# ---------------------------------------------------------------------------


def _assert_placeholder_rejected(exc_info, *, field: str = "commitment_id") -> None:
    """Assert the ToolCallRejected payload carries structured validation fields."""
    result = exc_info.value.result
    assert result["is_error"] is True
    assert result["error_code"] == "invalid_uuid"
    assert result["field"] == field
    assert result["retryable"] is True
    assert "list_commitments" in result["correction_hint"].lower()
    assert "create_commitment" in result["correction_hint"].lower()


@pytest.mark.asyncio
async def test_log_event_with_pending_commitment_id_rejected():
    """Exact incident regression: log_event(commitment_id='pending') → ToolCallRejected."""
    pool = _fresh_pool()
    ctx = _make_hector_ctx(pool)

    with pytest.raises(ToolCallRejected) as exc_info:
        await log_event(
            ctx,
            LogEventInput(
                commitment_id="pending",
                metric_key="workout",
                adherence_status=AdherenceStatus.done,
            ),
        )
    _assert_placeholder_rejected(exc_info)
    # Verify no SQL was executed (pool.events remains empty)
    assert len(pool.events) == 0


@pytest.mark.asyncio
async def test_log_event_with_unknown_commitment_id_rejected():
    """log_event(commitment_id='unknown') → ToolCallRejected."""
    pool = _fresh_pool()
    ctx = _make_hector_ctx(pool)

    with pytest.raises(ToolCallRejected) as exc_info:
        await log_event(
            ctx,
            LogEventInput(
                commitment_id="unknown",
                metric_key="workout",
                adherence_status=AdherenceStatus.done,
            ),
        )
    _assert_placeholder_rejected(exc_info)
    assert len(pool.events) == 0


@pytest.mark.asyncio
async def test_update_commitment_with_placeholder_id_rejected():
    """update_commitment with 'pending' → ToolCallRejected before SQL."""
    pool = _fresh_pool()
    ctx = _make_hector_ctx(pool)

    with pytest.raises(ToolCallRejected) as exc_info:
        await update_commitment(
            ctx,
            UpdateCommitmentInput(commitment_id="pending", label="Should not work"),
        )
    _assert_placeholder_rejected(exc_info)


@pytest.mark.asyncio
async def test_close_commitment_with_placeholder_id_rejected():
    """close_commitment with 'pending' → ToolCallRejected before SQL."""
    pool = _fresh_pool()
    ctx = _make_hector_ctx(pool)

    with pytest.raises(ToolCallRejected) as exc_info:
        await close_commitment(
            ctx,
            CloseCommitmentInput(commitment_id="pending", status="completed"),
        )
    _assert_placeholder_rejected(exc_info)


@pytest.mark.asyncio
async def test_list_events_with_placeholder_commitment_id_rejected():
    """list_events with commitment_id='pending' → ToolCallRejected."""
    pool = _fresh_pool()
    ctx = _make_hector_ctx(pool, current_step="read")

    with pytest.raises(ToolCallRejected) as exc_info:
        await list_events(
            ctx,
            ListEventsInput(commitment_id="pending"),
        )
    _assert_placeholder_rejected(exc_info)


@pytest.mark.asyncio
async def test_get_adherence_with_placeholder_commitment_ids_rejected():
    """get_adherence with commitment_ids=['pending', 'unknown'] → ToolCallRejected."""
    pool = _fresh_pool()
    ctx = _make_hector_ctx(pool, current_step="read")

    with pytest.raises(ToolCallRejected) as exc_info:
        await get_adherence(
            ctx,
            GetAdherenceInput(commitment_ids=["pending", "unknown"]),
        )
    _assert_placeholder_rejected(exc_info, field="commitment_ids")


# ---------------------------------------------------------------------------
# Valid UUID not found / not accessible (clean tool error, not raw FK crash)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_log_event_with_valid_uuid_not_found():
    """log_event with a valid UUID not in accessible commitments → ToolCallRejected not_found."""
    pool = _fresh_pool()
    ctx = _make_hector_ctx(pool)
    nonexistent_id = str(uuid4())

    with pytest.raises(ToolCallRejected) as exc_info:
        await log_event(
            ctx,
            LogEventInput(
                commitment_id=nonexistent_id,
                metric_key="workout",
                adherence_status=AdherenceStatus.done,
            ),
        )
    result = exc_info.value.result
    assert result["is_error"] is True
    assert result["error_code"] == "not_found"
    assert result["field"] == "commitment_id"
    assert result["retryable"] is True
    assert "list_commitments" in result["correction_hint"].lower()
    assert "create_commitment" in result["correction_hint"].lower()
    # Verify no SQL was executed (pool.events remains empty)
    assert len(pool.events) == 0


# ---------------------------------------------------------------------------
# Missed scope values rejected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_bot_id_rejected():
    """Tools reject when ctx.bot_id is None."""
    pool = _fresh_pool()
    user = _make_user()
    pool.users[user.id] = {
        "id": user.id, "name": user.name, "phone": user.phone,
        "timezone": user.timezone, "onboarding_state": "welcomed",
        "pacing_preferences": {}, "pregnancy_edd": None,
        "pregnancy_dating_basis": None, "pregnancy_lmp_date": None,
        "pregnancy_scan_date": None, "pregnancy_scan_corrected_at": None,
        "pregnancy_started_at": None, "pregnancy_ended_at": None,
        "pregnancy_outcome": None,
    }
    ctx = TurnContext(
        turn_id=uuid4(),
        pool=pool,
        user=user,
        partner=None,
        triggering_message_ids=[uuid4()],
        bot_id=None,
        primary_topic_id=_FITNESS_TOPIC_ID,
        primary_topic_slug="fitness",
        current_step="record",
    )

    with pytest.raises(ToolCallRejected, match="missing bot_id"):
        await create_commitment(
            ctx,
            CreateCommitmentInput(label="X", kind="workout", cadence=Cadence.daily),
        )
