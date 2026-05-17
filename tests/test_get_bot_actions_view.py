"""Real-Postgres tests for the ``mediator.v_bot_actions`` view (B.3).

The view (migration 0047_v_bot_actions.sql) replaces the in-Python join +
GROUP BY that previously powered ``get_bot_actions``. These tests:

1. Assert the view returns the right shape for all three Project B
   scenarios (``replied_turn``, ``silent_turn``, ``failed_pre_send_turn``).
2. Pin the view's column set so that adding a new column to
   ``messages``/``bot_turns`` either propagates into the view explicitly
   (with a matching update to the expected set) or fails CI — the
   "GROUP BY regression" guard from the brief.
3. Assert bot-scoping is enforced: a row inserted under a different
   ``bot_id`` is not returned when filtering by ``bot_id = 'mediator'``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

pytestmark = pytest.mark.postgres


# ---------------------------------------------------------------------------
# Shape: the view exposes a stable set of columns.
#
# This guards against the GROUP BY class of regression (commit 221c700):
# adding a new column to ``messages`` or ``bot_turns`` should require a
# deliberate update to ``v_bot_actions`` (or a deliberate decision NOT to
# surface it). If someone adds a column and forgets to wire it through,
# this test fails and forces the conversation.
# ---------------------------------------------------------------------------


EXPECTED_VIEW_COLUMNS = {
    "turn_id",
    "bot_id",
    "topic_id",
    "started_at",
    "user_in_context",
    "triggered_by_message_id",
    "final_output_message_id",
    "failure_reason",
    "reasoning",
    "triggering_content",
    "triggering_handling_result",
    "triggering_processing_error",
    "triggering_failure_class",
    "triggering_next_retry_at",
    "final_outbound_content",
    "tool_calls",
    "audit_events",
}


async def test_view_exposes_expected_columns(pg_pool) -> None:
    """``v_bot_actions`` must expose exactly the documented column set.

    Adding/removing a column changes the audit contract; bump
    ``EXPECTED_VIEW_COLUMNS`` deliberately. This is the GROUP BY
    regression guard.
    """
    rows = await pg_pool.fetch(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'mediator'
          AND table_name = 'v_bot_actions'
        """
    )
    actual = {r["column_name"] for r in rows}
    assert actual == EXPECTED_VIEW_COLUMNS, (
        "v_bot_actions columns drifted from EXPECTED_VIEW_COLUMNS.\n"
        f"  Added:   {sorted(actual - EXPECTED_VIEW_COLUMNS)}\n"
        f"  Removed: {sorted(EXPECTED_VIEW_COLUMNS - actual)}\n"
        "If this is intentional, update EXPECTED_VIEW_COLUMNS in this test "
        "AND update migrations/0047_v_bot_actions.sql to match."
    )


# ---------------------------------------------------------------------------
# Scenario shape tests — replied / silent / failed_pre_send.
# ---------------------------------------------------------------------------


async def test_view_returns_replied_turn(pg_pool, replied_turn) -> None:
    row = await pg_pool.fetchrow(
        "SELECT * FROM v_bot_actions WHERE turn_id = $1",
        replied_turn.turn_id,
    )
    assert row is not None
    assert row["bot_id"] == "mediator"
    assert row["topic_id"] == replied_turn.topic_id
    assert row["triggered_by_message_id"] == replied_turn.inbound_message_id
    assert row["final_output_message_id"] == replied_turn.outbound_message_id
    assert row["triggering_content"] == "hey, how are things?"
    assert row["final_outbound_content"] == (
        "things are calm — what's coming up for you?"
    )
    assert row["triggering_handling_result"] == "replied"
    assert row["triggering_processing_error"] is None
    assert row["triggering_failure_class"] is None
    assert row["triggering_next_retry_at"] is None
    assert row["failure_reason"] is None
    # No tool calls / audit events seeded; LATERAL subqueries should give
    # empty json arrays, not NULL.
    assert list(row["tool_calls"] or []) == []
    assert list(row["audit_events"] or []) == []


async def test_view_returns_silent_turn(pg_pool, silent_turn) -> None:
    row = await pg_pool.fetchrow(
        "SELECT * FROM v_bot_actions WHERE turn_id = $1",
        silent_turn.turn_id,
    )
    assert row is not None
    assert row["triggered_by_message_id"] == silent_turn.inbound_message_id
    assert row["final_output_message_id"] is None
    assert row["final_outbound_content"] is None
    assert row["triggering_handling_result"] == "silent"
    assert row["triggering_processing_error"] is None
    assert row["triggering_failure_class"] is None
    assert row["failure_reason"] is None


async def test_view_returns_failed_pre_send_turn(
    pg_pool, failed_pre_send_turn
) -> None:
    row = await pg_pool.fetchrow(
        "SELECT * FROM v_bot_actions WHERE turn_id = $1",
        failed_pre_send_turn.turn_id,
    )
    assert row is not None
    assert row["triggered_by_message_id"] == failed_pre_send_turn.inbound_message_id
    assert row["final_output_message_id"] is None
    assert row["final_outbound_content"] is None
    assert row["triggering_handling_result"] == "failed"
    assert row["triggering_processing_error"] == "anthropic 529 overloaded"
    assert row["triggering_failure_class"] == "retryable_pre_send"
    assert row["triggering_next_retry_at"] is not None
    assert row["failure_reason"] == failed_pre_send_turn.failure_reason


async def test_view_composes_all_three_scenarios(
    pg_pool, replied_turn, silent_turn, failed_pre_send_turn
) -> None:
    """All three fixtures should coexist and be visible in one query."""
    rows = await pg_pool.fetch(
        """
        SELECT turn_id, triggering_handling_result
        FROM v_bot_actions
        WHERE turn_id = ANY($1::uuid[])
        ORDER BY started_at DESC
        """,
        [replied_turn.turn_id, silent_turn.turn_id, failed_pre_send_turn.turn_id],
    )
    by_id = {r["turn_id"]: r["triggering_handling_result"] for r in rows}
    assert by_id == {
        replied_turn.turn_id: "replied",
        silent_turn.turn_id: "silent",
        failed_pre_send_turn.turn_id: "failed",
    }


# ---------------------------------------------------------------------------
# Bot-scoping discipline: a different bot_id is invisible.
# ---------------------------------------------------------------------------


async def test_view_is_bot_scoped(pg_pool, replied_turn) -> None:
    """When you filter by bot_id you only see rows for that bot.

    Seeds a second bot + a second turn for the same user/topic and asserts
    that ``bot_id = 'mediator'`` does NOT return the foreign-bot turn.
    """
    async with pg_pool.acquire() as conn:
        # Register a second bot to satisfy the FK from bot_turns.bot_id.
        await conn.execute(
            "INSERT INTO mediator.bots (id, display_name) "
            "VALUES ('test_other_bot', 'Other Bot') "
            "ON CONFLICT (id) DO NOTHING;"
        )
        # Hector's binding pattern: bot + dyad.
        await conn.execute(
            "INSERT INTO mediator.bot_bindings (bot_id, dyad_id) "
            "SELECT 'test_other_bot', id FROM mediator.dyads LIMIT 1 "
            "ON CONFLICT DO NOTHING;"
        )
        other_inbound = await conn.fetchval(
            """
            INSERT INTO mediator.messages
                (direction, sender_id, content, sent_at,
                 processing_state, bot_id, topic_id, processing_attempts)
            VALUES
                ('inbound', $1, 'hello other bot', now(),
                 'processed', 'test_other_bot', $2, 1)
            RETURNING id
            """,
            replied_turn.user_id,
            replied_turn.topic_id,
        )
        other_turn = await conn.fetchval(
            """
            INSERT INTO mediator.bot_turns
                (triggered_by_message_id, triggering_message_ids,
                 user_in_context, prompt_snapshot, system_prompt_version,
                 model_version, started_at, bot_id, topic_id)
            VALUES
                ($1, ARRAY[$1]::uuid[], $2,
                 'other prompt', 'v', 'm', now(), 'test_other_bot', $3)
            RETURNING id
            """,
            other_inbound,
            replied_turn.user_id,
            replied_turn.topic_id,
        )

    mediator_rows = await pg_pool.fetch(
        "SELECT turn_id FROM v_bot_actions WHERE bot_id = 'mediator'"
    )
    mediator_ids = {r["turn_id"] for r in mediator_rows}
    assert replied_turn.turn_id in mediator_ids
    assert other_turn not in mediator_ids

    other_rows = await pg_pool.fetch(
        "SELECT turn_id FROM v_bot_actions WHERE bot_id = $1",
        "test_other_bot",
    )
    other_ids = {r["turn_id"] for r in other_rows}
    assert other_turn in other_ids
    assert replied_turn.turn_id not in other_ids
