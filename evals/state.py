from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID


PRIMITIVE_TABLES = ("users", "memories", "themes", "watch_items", "observations", "out_of_bounds")
STATE_TABLES = (
    *PRIMITIVE_TABLES,
    "scheduled_jobs",
    "messages",
    "withheld_outbound_reviews",
    "tool_calls",
)


@dataclass(frozen=True)
class ScenarioSnapshot:
    tables: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    spend_totals: dict[str, Decimal] = field(default_factory=dict)


@dataclass(frozen=True)
class TableDiff:
    inserted: list[dict[str, Any]] = field(default_factory=list)
    updated: list[dict[str, Any]] = field(default_factory=list)
    deleted: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class StateDiff:
    tables: dict[str, TableDiff]
    cost_delta_usd: Decimal

    @property
    def primitive_tables(self) -> dict[str, TableDiff]:
        return {name: diff for name, diff in self.tables.items() if name in PRIMITIVE_TABLES}


async def snapshot_state(pool: Any) -> ScenarioSnapshot:
    tables = {table: await _snapshot_table(pool, table) for table in STATE_TABLES}
    return ScenarioSnapshot(tables=tables, spend_totals=await _snapshot_spend(pool))


def diff_snapshots(before: ScenarioSnapshot, after: ScenarioSnapshot) -> StateDiff:
    table_diffs: dict[str, TableDiff] = {}
    for table in STATE_TABLES:
        before_rows = before.tables.get(table, {})
        after_rows = after.tables.get(table, {})
        before_ids = set(before_rows)
        after_ids = set(after_rows)
        inserted = [after_rows[row_id] for row_id in sorted(after_ids - before_ids)]
        deleted = [before_rows[row_id] for row_id in sorted(before_ids - after_ids)]
        updated = [
            after_rows[row_id]
            for row_id in sorted(before_ids & after_ids)
            if _stable(before_rows[row_id]) != _stable(after_rows[row_id])
        ]
        table_diffs[table] = TableDiff(inserted=inserted, updated=updated, deleted=deleted)
    return StateDiff(tables=table_diffs, cost_delta_usd=_spend_total(after) - _spend_total(before))


def outbound_messages(snapshot: ScenarioSnapshot) -> list[dict[str, Any]]:
    rows = [
        row
        for row in snapshot.tables.get("messages", {}).values()
        if row.get("direction") == "outbound"
    ]
    return sorted(rows, key=lambda row: str(row.get("sent_at") or ""))


def outbound_text(snapshot: ScenarioSnapshot) -> str:
    return "\n\n".join(str(row.get("content") or "") for row in outbound_messages(snapshot))


def persisted_tool_calls(snapshot: ScenarioSnapshot) -> list[dict[str, Any]]:
    return list(snapshot.tables.get("tool_calls", {}).values())


def withheld_reviews(snapshot: ScenarioSnapshot) -> list[dict[str, Any]]:
    rows = list(snapshot.tables.get("withheld_outbound_reviews", {}).values())
    return sorted(rows, key=lambda row: str(row.get("created_at") or ""))


def classified_charges(snapshot: ScenarioSnapshot, message_ids: list[UUID]) -> dict[str, str | None]:
    messages = snapshot.tables.get("messages", {})
    return {str(message_id): messages.get(str(message_id), {}).get("charge") for message_id in message_ids}


def oob_outcome(snapshot: ScenarioSnapshot) -> str | None:
    reviews = withheld_reviews(snapshot)
    if reviews:
        return str(reviews[-1].get("verdict") or "block")
    if any(row.get("processing_state") == "processed" for row in outbound_messages(snapshot)):
        return "pass"
    return None


async def _snapshot_table(pool: Any, table: str) -> dict[str, dict[str, Any]]:
    attr = "out_of_bounds" if table == "out_of_bounds" else table
    if hasattr(pool, attr):
        value = getattr(pool, attr)
        if isinstance(value, dict):
            return _rows_by_id(value.values(), table)
        if isinstance(value, list):
            return _rows_by_id(value, table)
    return _rows_by_id(await _fetch_table(pool, table), table)


async def _fetch_table(pool: Any, table: str) -> list[Any]:
    if table == "users":
        return await pool.fetch(
            "SELECT id, name, phone, timezone, onboarding_state, style_notes FROM users ORDER BY id"
        )
    if table == "memories":
        return await pool.fetch("SELECT * FROM memories ORDER BY id")
    if table == "themes":
        return await pool.fetch("SELECT * FROM themes ORDER BY id")
    if table == "watch_items":
        return await pool.fetch("SELECT * FROM watch_items ORDER BY id")
    if table == "observations":
        return await pool.fetch("SELECT * FROM observations ORDER BY id")
    if table == "out_of_bounds":
        return await pool.fetch("SELECT * FROM out_of_bounds ORDER BY id")
    if table == "scheduled_jobs":
        return await pool.fetch("SELECT * FROM scheduled_jobs ORDER BY id")
    if table == "messages":
        return await pool.fetch("SELECT * FROM messages ORDER BY id")
    if table == "withheld_outbound_reviews":
        return await pool.fetch("SELECT * FROM withheld_outbound_reviews ORDER BY id")
    if table == "tool_calls":
        return await pool.fetch("SELECT * FROM tool_calls ORDER BY id")
    raise ValueError(f"unknown snapshot table: {table}")


async def _snapshot_spend(pool: Any) -> dict[str, Decimal]:
    if hasattr(pool, "llm_spend_log"):
        totals: dict[str, Decimal] = {}
        for provider, value in pool.llm_spend_log.items():
            if isinstance(value, dict):
                total = value.get("total_usd", Decimal("0"))
            else:
                total = value
            totals[str(provider)] = Decimal(str(total))
        return totals
    rows = await pool.fetch("SELECT provider, total_usd FROM llm_spend_log")
    return {str(row["provider"]): Decimal(str(row["total_usd"])) for row in rows}


def _rows_by_id(rows: Any, table: str) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(rows):
        data = _normalize_row(row)
        row_id = data.get("id")
        if row_id is None:
            row_id = f"{table}:{index}"
            data["id"] = row_id
        output[str(row_id)] = data
    return output


def _normalize_row(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        data = dict(row)
    else:
        data = dict(row)
    return {str(key): _normalize_value(value) for key, value in data.items()}


def _normalize_value(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _normalize_value(inner) for key, inner in value.items()}
    if isinstance(value, list):
        return [_normalize_value(inner) for inner in value]
    if isinstance(value, tuple):
        return [_normalize_value(inner) for inner in value]
    return copy.deepcopy(value)


def _stable(row: dict[str, Any]) -> str:
    return json.dumps(row, sort_keys=True, separators=(",", ":"), default=str)


def _spend_total(snapshot: ScenarioSnapshot) -> Decimal:
    return sum(snapshot.spend_totals.values(), Decimal("0"))
