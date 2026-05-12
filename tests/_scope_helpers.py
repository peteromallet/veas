"""S2a test scope helpers — shared fixtures for stamping, dual-key, and stamp-pool tests.

Provides:
- make_mediator_ctx: builds a TurnContext with mediator defaults
- make_resolved_scope: creates a mock ResolvedScope
- StampingFakePool: FakePool subclass using SUBSTRING MATCHING only for
  tracking INSERTS into artifact_tables and artifact_topics.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

# Import from the *real* modules (not dirty files we must not touch).
from app.bots.base import BotSpec
from app.services.turn_context import TurnContext
from app.models.user import User


def _bot_spec_mediator() -> BotSpec:
    """Return a minimal BotSpec instance with mediator defaults."""
    return BotSpec(
        bot_id="mediator",
        prompt_renderer=lambda *args, **kwargs: "system prompt",
        step_instructions={"respond": "You are a helpful mediator."},
    )


def make_mediator_ctx(
    bot_spec: BotSpec | None = None,
    **overrides: Any,
) -> TurnContext:
    """Build a TurnContext with mediator defaults, including dyad_id.

    Override any field via keyword arguments.
    """
    bs = bot_spec or _bot_spec_mediator()
    user = User(
        id=uuid4(),
        name="TestUser",
        phone="15555550100",
        timezone="UTC",
    )
    partner = User(
        id=uuid4(),
        name="TestPartner",
        phone="15555550101",
        timezone="UTC",
    )
    topic_id = uuid4()
    dyad_id = uuid4()
    binding_id = uuid4()

    defaults: dict[str, Any] = dict(
        turn_id=uuid4(),
        pool=None,
        user=user,
        partner=partner,
        triggering_message_ids=[uuid4()],
        bot_id=bs.bot_id,
        bot_spec=bs,
        binding_id=binding_id,
        dyad_id=dyad_id,
        participants_shape=bs.participants_shape,
        primary_topic_id=topic_id,
        primary_topic_slug=bs.primary_topic_slug,
        channel_id=None,
        read_scopes=None,
        write_scopes=None,
        cross_topic_policy=None,
    )
    defaults.update(overrides)
    return TurnContext(**defaults)


def make_resolved_scope(**overrides: Any):
    """Create a mock ResolvedScope with sensible defaults.

    ResolvedScope is defined in app.services.inbound (NamedTuple).
    We import it lazily to avoid circular imports.
    """
    from app.services.inbound import ResolvedScope

    defaults: dict[str, Any] = dict(
        bot_id="mediator",
        topic_id=uuid4(),
        channel_id=None,
        binding_id=uuid4(),
        dyad_id=uuid4(),
    )
    defaults.update(overrides)
    return ResolvedScope(**defaults)


# ---------------------------------------------------------------------------
# StampingFakePool — substring-only matching, no structural CTE parsing
# ---------------------------------------------------------------------------

class StampingFakePool:
    """FakePool subclass using SUBSTRING MATCHING to track artifact INSERTs.

    Records rows into side dicts when an INSERT INTO <artifact_table> and
    INSERT INTO artifact_topics appear in the same SQL string.  This is a
    substring check — we intentionally do NOT parse the CTE structure.
    """

    _ARTIFACT_TABLES: set[str] = {
        "memories", "themes", "observations", "watch_items",
        "distillations", "out_of_bounds",
    }

    def __init__(self, real_fake_pool: Any) -> None:
        """Wrap an existing FakePool instance."""
        self._pool = real_fake_pool

        # Side dicts for stamp tracking
        self.artifact_rows: dict[str, list[dict[str, Any]]] = {
            t: [] for t in self._ARTIFACT_TABLES
        }
        self.artifact_topics_rows: list[dict[str, Any]] = []
        self.scope_stamp_rows: dict[str, list[dict[str, Any]]] = {
            "messages": [],
            "bot_turns": [],
            "scheduled_jobs": [],
            "feedback": [],
            "bridge_candidates": [],
        }
        # Track the last SQL + args for substring-based assertions
        self._last_sql: str = ""
        self._last_args: tuple[Any, ...] = ()

    # --- Delegate everything except fetchrow to the real FakePool ---

    def __getattr__(self, name: str) -> Any:
        """Fall through to the wrapped FakePool for any attribute we don't override."""
        return getattr(self._pool, name)

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        """Intercept fetchrow to record stamp insertions via substring matching."""
        self._last_sql = sql
        self._last_args = args
        compact = " ".join(sql.split())

        # --- Record artifact-table INSERT + artifact_topics pairs ---
        for table in self._ARTIFACT_TABLES:
            # Check if this SQL contains INSERT INTO <table>
            insert_table = f"INSERT INTO {table}"
            insert_topics = "INSERT INTO artifact_topics"
            if insert_table in sql and insert_topics in sql:
                # Record the paired insertion
                self.artifact_rows[table].append({
                    "table": table,
                    "sql": sql,
                    "args": args,
                })
                self.artifact_topics_rows.append({
                    "table": table,
                    "sql": sql,
                    "args": args,
                })
                break

        # --- Record scope-stamp INSERTs ---
        for scope_table in ("messages", "bot_turns", "scheduled_jobs", "feedback", "bridge_candidates"):
            if f"INSERT INTO {scope_table}" in compact:
                row = {"table": scope_table, "sql": sql, "args": args}
                # Parse out the column list
                col_match = _extract_column_list(sql, scope_table)
                if col_match:
                    row["columns"] = col_match
                self.scope_stamp_rows[scope_table].append(row)
                break

        # Delegate actual execution to the real FakePool
        return await self._pool.fetchrow(sql, *args)

    async def fetchval(self, sql: str, *args: Any) -> Any:
        """Delegate fetchval to the real FakePool."""
        self._last_sql = sql
        self._last_args = args
        return await self._pool.fetchval(sql, *args)

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        """Delegate fetch to the real FakePool."""
        self._last_sql = sql
        self._last_args = args
        return await self._pool.fetch(sql, *args)

    async def execute(self, sql: str, *args: Any) -> str:
        """Delegate execute to the real FakePool."""
        self._last_sql = sql
        self._last_args = args
        return await self._pool.execute(sql, *args)

    @property
    def last_sql(self) -> str:
        """Return the most recent SQL string for inspection."""
        return self._last_sql

    @property
    def last_args(self) -> tuple[Any, ...]:
        """Return the most recent SQL arguments for inspection."""
        return self._last_args

    def has_artifact_pair(self, table: str) -> bool:
        """Return True if any INSERT for *table* was paired with artifact_topics."""
        return len(self.artifact_rows.get(table, [])) > 0

    def artifact_pair_args(self, table: str, index: int = 0):
        """Return the args tuple for the Nth paired INSERT for *table*."""
        rows = self.artifact_rows.get(table, [])
        if index < len(rows):
            return rows[index].get("args", ())
        return None

    def scope_args(self, table: str, index: int = 0):
        """Return the args tuple for the Nth scope-stamp INSERT for *table*."""
        rows = self.scope_stamp_rows.get(table, [])
        if index < len(rows):
            return rows[index].get("args", ())
        return None


def _extract_column_list(sql: str, table: str) -> list[str] | None:
    """Extract the column names from ``INSERT INTO <table> (...)``."""
    import re
    pattern = rf"INSERT\s+INTO\s+(?:[\w.]+\.)?{re.escape(table)}\s*\(([^)]*)\)"
    match = re.search(pattern, sql, re.IGNORECASE)
    if not match:
        return None
    cols = match.group(1)
    return [c.strip() for c in cols.split(",")]