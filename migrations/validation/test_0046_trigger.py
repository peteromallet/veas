"""Integration test: migration 0042 BEFORE UPDATE trigger raises on direct
writes to ``mediator.messages.next_retry_at`` / ``failure_class`` outside the
``app.lifecycle_writer = 'inbound_queue'`` writer marker, and accepts the
mutator path.

FakePool cannot exercise the real PL/pgSQL trigger; this test applies the
migration against a live Postgres instance and round-trips an UPDATE through
both paths.  Skipped unless ``RECOVERY_V2_TRIGGER_TEST_DB_URL`` is set.

Marked ``requires_postgres`` per the plan; the marker is unregistered (no
pyproject change per SD-008), which produces a benign PytestUnknownMarkWarning
that the runner tolerates.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest


pytestmark = [pytest.mark.anyio, pytest.mark.requires_postgres]


_LIVE_DB_URL = os.environ.get("RECOVERY_V2_TRIGGER_TEST_DB_URL")
_MIGRATION = Path(__file__).resolve().parents[1] / "0046_message_lifecycle_columns.sql"
_DOWN = Path(__file__).resolve().parents[1] / "0046_message_lifecycle_columns.down.sql"


@pytest.mark.skipif(
    not _LIVE_DB_URL,
    reason="set RECOVERY_V2_TRIGGER_TEST_DB_URL to a disposable Postgres URL",
)
async def test_lifecycle_columns_trigger_enforces_writer_marker():
    """Bypass the mutator → trigger RAISEs; use the mutator → succeeds."""
    asyncpg = pytest.importorskip("asyncpg")

    conn = await asyncpg.connect(_LIVE_DB_URL)
    try:
        await conn.execute(_MIGRATION.read_text())

        message_id = uuid.uuid4()
        # Seed a minimal inbound row using a transaction with the writer
        # marker (the row insert itself doesn't touch lifecycle columns, but
        # we keep the seed self-contained).
        await conn.execute(
            """
            INSERT INTO mediator.messages
              (id, direction, processing_state, content, sent_at, processing_attempts)
            VALUES ($1, 'inbound', 'failed', 'trigger probe', now(), 1)
            """,
            message_id,
        )

        # 1) Direct UPDATE outside the writer marker: trigger MUST RAISE.
        with pytest.raises(asyncpg.exceptions.RaiseError):
            await conn.execute(
                "UPDATE mediator.messages SET failure_class = 'retryable_pre_send' WHERE id=$1",
                message_id,
            )

        # 2) Same UPDATE inside a transaction with the writer marker set
        #    via set_config('app.lifecycle_writer', 'inbound_queue', true)
        #    on the same connection: MUST succeed.
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.lifecycle_writer', 'inbound_queue', true)"
            )
            await conn.execute(
                "UPDATE mediator.messages SET failure_class = 'retryable_pre_send' WHERE id=$1",
                message_id,
            )
        row = await conn.fetchrow(
            "SELECT failure_class FROM mediator.messages WHERE id=$1",
            message_id,
        )
        assert row["failure_class"] == "retryable_pre_send"

        # Cleanup: tear down the migration so re-runs are idempotent.
        await conn.execute(
            "DELETE FROM mediator.messages WHERE id=$1", message_id
        )
        await conn.execute(_DOWN.read_text())
    finally:
        await conn.close()
