from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from app.models.user import User
from app.services import inbound
from evals.execution import EvalTurnExecution, run_eval_turn
from evals.scenario import InboundMessage, Scenario
from evals.state import (
    ScenarioSnapshot,
    StateDiff,
    classified_charges,
    diff_snapshots,
    oob_outcome,
    outbound_text,
    persisted_tool_calls,
    snapshot_state,
    withheld_reviews,
)


@dataclass(frozen=True)
class ScenarioSeed:
    user: User
    partner: User
    inbound_message_ids: list[UUID]
    refs: dict[str, UUID]


@dataclass(frozen=True)
class ScenarioCapture:
    scenario: Scenario
    seed: ScenarioSeed
    before: ScenarioSnapshot
    after: ScenarioSnapshot
    diff: StateDiff
    execution: EvalTurnExecution
    outbound_text: str
    persisted_tool_calls: list[dict[str, Any]]
    withheld_reviews: list[dict[str, Any]]
    oob_outcome: str | None
    classified_charges: dict[str, str | None]
    cost_delta_usd: str


class _CollectingCoalescer:
    def __init__(self) -> None:
        self.message_ids: list[UUID] = []

    async def add(self, user_id: UUID, message_id: UUID, user: User, *, source: str = "live") -> None:
        self.message_ids.append(message_id)


async def seed_scenario(pool: Any, scenario: Scenario) -> ScenarioSeed:
    refs: dict[str, UUID] = {}
    user, partner = await _seed_users(pool, scenario.setup.get("users", []), refs)
    await _seed_themes(pool, scenario.setup.get("themes", []), refs)
    await _seed_memories(pool, scenario.setup.get("memories", []), refs, user)
    await _seed_observations(pool, scenario.setup.get("observations", []), refs, user)
    await _seed_distillations(pool, scenario.setup.get("distillations", []), refs, user)
    await _seed_watch_items(pool, scenario.setup.get("watch_items", []), refs, user)
    await _seed_oob_entries(pool, scenario.setup.get("oob_entries", []), refs, user)
    await _seed_scheduled_jobs(pool, scenario.setup.get("scheduled_jobs", []), refs, user)
    inbound_message_ids = await _seed_inbound(pool, scenario, user)
    return ScenarioSeed(user=user, partner=partner, inbound_message_ids=inbound_message_ids, refs=refs)


async def capture_scenario_turn(pool: Any, scenario: Scenario, *, prompt_version: str) -> ScenarioCapture:
    seed = await seed_scenario(pool, scenario)
    before = await snapshot_state(pool)
    execution = await run_eval_turn(pool, seed.inbound_message_ids, seed.user, prompt_version=prompt_version)
    after = await snapshot_state(pool)
    diff = diff_snapshots(before, after)
    return ScenarioCapture(
        scenario=scenario,
        seed=seed,
        before=before,
        after=after,
        diff=diff,
        execution=execution,
        outbound_text=outbound_text(after),
        persisted_tool_calls=persisted_tool_calls(after),
        withheld_reviews=withheld_reviews(after),
        oob_outcome=oob_outcome(after),
        classified_charges=classified_charges(after, seed.inbound_message_ids),
        cost_delta_usd=str(diff.cost_delta_usd),
    )


async def _seed_users(pool: Any, specs: Any, refs: dict[str, UUID]) -> tuple[User, User]:
    if specs is None:
        specs = []
    if not isinstance(specs, list):
        raise ValueError("setup.users must be a list")
    defaults = [
        {"key": "user", "name": "Maya", "phone": "15555550100", "timezone": "UTC", "onboarding_state": "welcomed"},
        {"key": "partner", "name": "Ben", "phone": "15555550101", "timezone": "UTC", "onboarding_state": "welcomed"},
    ]
    merged = [dict(defaults[index], **(specs[index] if index < len(specs) else {})) for index in range(2)]
    users: list[User] = []
    for item in merged:
        user_id = _coerce_uuid(item.get("id")) or uuid4()
        row = {
            "id": user_id,
            "name": str(item["name"]),
            "phone": str(item["phone"]),
            "timezone": str(item.get("timezone") or "UTC"),
            "onboarding_state": str(item.get("onboarding_state") or "welcomed"),
            "style_notes": str(item.get("style_notes") or ""),
        }
        if hasattr(pool, "users"):
            pool.users[user_id] = row
        else:
            await pool.fetchrow(
                """
                INSERT INTO users (id, name, phone, timezone, onboarding_state, style_notes)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (phone) DO UPDATE
                SET name=EXCLUDED.name,
                    timezone=EXCLUDED.timezone,
                    onboarding_state=EXCLUDED.onboarding_state,
                    style_notes=EXCLUDED.style_notes
                RETURNING id
                """,
                user_id,
                row["name"],
                row["phone"],
                row["timezone"],
                row["onboarding_state"],
                row["style_notes"],
            )
        key = str(item.get("key") or ("user" if len(users) == 0 else "partner"))
        refs[key] = user_id
        users.append(User(user_id, row["name"], row["phone"], row["timezone"], row["onboarding_state"]))
    return users[0], users[1]


async def _seed_themes(pool: Any, specs: Any, refs: dict[str, UUID]) -> None:
    for item in _list_specs(specs, "setup.themes"):
        theme_id = _coerce_uuid(item.get("id")) or uuid4()
        row = {
            "id": theme_id,
            "title": str(item["title"]),
            "description": str(item.get("description") or ""),
            "status": str(item.get("status") or "active"),
            "sentiment": str(item.get("sentiment") or "mixed"),
            "health": str(item.get("health") or "live"),
            "last_reinforced_at": _parse_time(item.get("last_reinforced_at")) or datetime.now(UTC),
            "last_active_at": _parse_time(item.get("last_active_at")) or datetime.now(UTC),
        }
        if hasattr(pool, "themes"):
            pool.themes[theme_id] = row
        else:
            await pool.fetchrow(
                """
                INSERT INTO themes (id, title, description, status, sentiment, health, last_reinforced_at, last_active_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                RETURNING id
                """,
                *row.values(),
            )
        if item.get("key"):
            refs[str(item["key"])] = theme_id


async def _seed_memories(pool: Any, specs: Any, refs: dict[str, UUID], default_user: User) -> None:
    for item in _list_specs(specs, "setup.memories"):
        memory_id = _coerce_uuid(item.get("id")) or uuid4()
        row = {
            "id": memory_id,
            "about_user_id": _ref(item.get("about"), refs) or default_user.id,
            "content": str(item["content"]),
            "related_theme_ids": [_ref(value, refs) for value in item.get("related_themes", [])],
            "status": str(item.get("status") or "active"),
            "supersedes_memory_id": _ref(item.get("supersedes"), refs),
            "created_at": _parse_time(item.get("created_at")) or datetime.now(UTC),
            "last_referenced_at": _parse_time(item.get("last_referenced_at")),
        }
        if hasattr(pool, "memories"):
            pool.memories[memory_id] = row
        else:
            await pool.fetchrow(
                """
                INSERT INTO memories (id, about_user_id, content, related_theme_ids, status, supersedes_memory_id, created_at, last_referenced_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                RETURNING id
                """,
                *row.values(),
            )
        if item.get("key"):
            refs[str(item["key"])] = memory_id


async def _seed_observations(pool: Any, specs: Any, refs: dict[str, UUID], default_user: User) -> None:
    for item in _list_specs(specs, "setup.observations"):
        observation_id = _coerce_uuid(item.get("id")) or uuid4()
        row = {
            "id": observation_id,
            "content": str(item["content"]),
            "about_user_id": _ref(item.get("about"), refs) or default_user.id,
            "confidence": str(item.get("confidence") or "medium"),
            "significance": item.get("significance", 3),
            "scoring_prompt_version": item.get("scoring_prompt_version"),
            "related_theme_ids": [_ref(value, refs) for value in item.get("related_themes", [])],
            "supporting_message_ids": [],
            "status": str(item.get("status") or "active"),
            "created_at": _parse_time(item.get("created_at")) or datetime.now(UTC),
            "last_reinforced_at": _parse_time(item.get("last_reinforced_at")),
            "surfaced_count": int(item.get("surfaced_count") or 0),
        }
        if hasattr(pool, "observations"):
            pool.observations[observation_id] = row
        else:
            await pool.fetchrow(
                """
                INSERT INTO observations (
                    id, content, about_user_id, confidence, significance, scoring_prompt_version,
                    related_theme_ids, supporting_message_ids, status, created_at, last_reinforced_at, surfaced_count
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                RETURNING id
                """,
                *row.values(),
            )
        if item.get("key"):
            refs[str(item["key"])] = observation_id


async def _seed_distillations(pool: Any, specs: Any, refs: dict[str, UUID], default_user: User) -> None:
    for item in _list_specs(specs, "setup.distillations"):
        distillation_id = _coerce_uuid(item.get("id")) or uuid4()
        row = {
            "id": distillation_id,
            "content": str(item["content"]),
            "confidence": str(item.get("confidence") or "medium"),
            "status": str(item.get("status") or "active"),
            "sensitivity": str(item.get("sensitivity") or "medium"),
            "visibility": str(item.get("visibility") or "private"),
            "shareable_summary": item.get("shareable_summary"),
            "source_user_ids": [_ref(value, refs) for value in item.get("source_users", [])] or [default_user.id],
            "related_memory_ids": [_ref(value, refs) for value in item.get("related_memories", [])],
            "related_observation_ids": [_ref(value, refs) for value in item.get("related_observations", [])],
            "related_theme_ids": [_ref(value, refs) for value in item.get("related_themes", [])],
            "supporting_message_ids": [_ref(value, refs) for value in item.get("supporting_messages", [])],
            "supersedes_distillation_id": _ref(item.get("supersedes"), refs),
            "superseded_by_distillation_id": _ref(item.get("superseded_by"), refs),
            "revision_note": item.get("revision_note"),
            "revision_count": int(item.get("revision_count") or 0),
            "created_at": _parse_time(item.get("created_at")) or datetime.now(UTC),
            "updated_at": _parse_time(item.get("updated_at")) or datetime.now(UTC),
            "revised_at": _parse_time(item.get("revised_at")),
            "retired_at": _parse_time(item.get("retired_at")),
        }
        if hasattr(pool, "distillations"):
            pool.distillations[distillation_id] = row
        else:
            await pool.fetchrow(
                """
                INSERT INTO distillations (
                    id, content, confidence, status, sensitivity, visibility, shareable_summary,
                    source_user_ids, related_memory_ids, related_observation_ids, related_theme_ids,
                    supporting_message_ids, supersedes_distillation_id, superseded_by_distillation_id,
                    revision_note, revision_count, created_at, updated_at, revised_at, retired_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20)
                RETURNING id
                """,
                *row.values(),
            )
        if item.get("key"):
            refs[str(item["key"])] = distillation_id


async def _seed_watch_items(pool: Any, specs: Any, refs: dict[str, UUID], default_user: User) -> None:
    for item in _list_specs(specs, "setup.watch_items"):
        watch_id = _coerce_uuid(item.get("id")) or uuid4()
        row = {
            "id": watch_id,
            "owner_user_id": _ref(item.get("owner"), refs) or default_user.id,
            "content": str(item["content"]),
            "due_at": _parse_time(item.get("due_at")),
            "related_theme_ids": [_ref(value, refs) for value in item.get("related_themes", [])],
            "status": str(item.get("status") or "open"),
        }
        if hasattr(pool, "watch_items"):
            pool.watch_items[watch_id] = row
        else:
            await pool.fetchrow(
                """
                INSERT INTO watch_items (id, owner_user_id, content, due_at, related_theme_ids, status)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING id
                """,
                *row.values(),
            )
        if item.get("key"):
            refs[str(item["key"])] = watch_id


async def _seed_oob_entries(pool: Any, specs: Any, refs: dict[str, UUID], default_user: User) -> None:
    for item in _list_specs(specs, "setup.oob_entries"):
        oob_id = _coerce_uuid(item.get("id")) or uuid4()
        row = {
            "id": oob_id,
            "owner_id": _ref(item.get("owner"), refs) or default_user.id,
            "sensitive_core": str(item["sensitive_core"]),
            "shareable_context": str(item.get("shareable_context") or ""),
            "severity": str(item.get("severity") or "firm"),
            "review_at": _parse_time(item.get("review_at")),
            "status": str(item.get("status") or "active"),
            "created_at": _parse_time(item.get("created_at")) or datetime.now(UTC),
        }
        if hasattr(pool, "out_of_bounds"):
            pool.out_of_bounds[oob_id] = row
        else:
            await pool.fetchrow(
                """
                INSERT INTO out_of_bounds (id, owner_id, sensitive_core, shareable_context, severity, review_at, status, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                RETURNING id
                """,
                *row.values(),
            )
        if item.get("key"):
            refs[str(item["key"])] = oob_id


async def _seed_scheduled_jobs(pool: Any, specs: Any, refs: dict[str, UUID], default_user: User) -> None:
    for item in _list_specs(specs, "setup.scheduled_jobs"):
        job_id = _coerce_uuid(item.get("id")) or uuid4()
        row = {
            "id": job_id,
            "user_id": _ref(item.get("user"), refs) or default_user.id,
            "job_type": str(item.get("job_type") or "checkin"),
            "scheduled_for": _parse_time(item.get("scheduled_for")) or datetime.now(UTC),
            "context": dict(item.get("context") or {}),
            "status": str(item.get("status") or "pending"),
            "attempt_count": int(item.get("attempt_count") or 0),
            "max_attempts": int(item.get("max_attempts") or 2),
            "delayed": bool(item.get("delayed") or False),
        }
        if hasattr(pool, "scheduled_jobs"):
            pool.scheduled_jobs[job_id] = row
        else:
            await pool.fetchrow(
                """
                INSERT INTO scheduled_jobs (id, user_id, job_type, scheduled_for, context, status, attempt_count, max_attempts, delayed)
                VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9)
                RETURNING id
                """,
                row["id"],
                row["user_id"],
                row["job_type"],
                row["scheduled_for"],
                row["context"],
                row["status"],
                row["attempt_count"],
                row["max_attempts"],
                row["delayed"],
            )
        if item.get("key"):
            refs[str(item["key"])] = job_id


async def _seed_inbound(pool: Any, scenario: Scenario, user: User) -> list[UUID]:
    classify = bool(scenario.setup.get("classify_inbound") or "charge" in scenario.tags)
    seeded_charge = scenario.setup.get("inbound_charge")
    if classify:
        coalescer = _CollectingCoalescer()
        for index, message in enumerate(scenario.inbound):
            await inbound.process_inbound(pool, _payload(user, message, index), coalescer)
        return coalescer.message_ids
    ids: list[UUID] = []
    for index, message in enumerate(scenario.inbound):
        row = await pool.fetchrow(
            """
            INSERT INTO messages
                (direction, sender_id, content, processing_state, whatsapp_message_id, sent_at,
                 media_type, media_url, media_duration_seconds, media_analysis, charge)
            VALUES ('inbound', $1, $2, 'raw', $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT (whatsapp_message_id) DO NOTHING
            RETURNING id
            """,
            user.id,
            message.text,
            f"eval-inbound-{scenario.name}-{index}",
            datetime.now(UTC) + timedelta(milliseconds=index),
            message.media_type,
            message.media_url,
            message.media_duration_seconds,
            None,
            seeded_charge or scenario.expectations.expected_charge or "routine",
        )
        if row is not None:
            ids.append(row["id"])
    return ids


def _payload(user: User, message: InboundMessage, index: int) -> dict[str, Any]:
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "contacts": [{"wa_id": user.phone, "profile": {"name": user.name}}],
                            "messages": [
                                {
                                    "id": f"eval-inbound-{index}",
                                    "from": user.phone,
                                    "timestamp": str(int(datetime.now(UTC).timestamp()) + index),
                                    "type": "text",
                                    "text": {"body": message.text},
                                }
                            ],
                        }
                    }
                ]
            }
        ]
    }


def _list_specs(value: Any, label: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError(f"{label} must be a list of mappings")
    return value


def _coerce_uuid(value: Any) -> UUID | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    return UUID(str(value))


def _ref(value: Any, refs: dict[str, UUID]) -> UUID | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    text = str(value)
    return refs.get(text) or UUID(text)


def _parse_time(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, dict):
        now = datetime.now(UTC)
        return now + timedelta(
            days=int(value.get("in_days") or 0),
            hours=int(value.get("in_hours") or 0),
            minutes=int(value.get("in_minutes") or 0),
        )
    text = str(value)
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed
