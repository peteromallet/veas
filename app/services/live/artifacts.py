"""
Artifact + provenance-link helpers for live voice sessions.

mediator.conversation_artifacts stores typed, immutable, revision-tracked
outputs (prep briefs, debriefs, summaries).  mediator.artifact_links
records provenance to conversation items and durable state rows.

Evidence shape (free-form jsonb, no server-side validation in v1):
    {"quote": "...", "span": {"start": 0, "end": 42},
     "source_turn_id": "<uuid>", "notes": "..."}

* Artifacts are immutable; retries produce new revisions.
* Current artifact = highest revision_number (not max created_at).
* target_table values are unqualified, lower-case canonical strings.
* bot_turns excluded from ALLOWED_TARGET_TABLES (use created_by_turn_id FK).
* bot_id is unvalidated string (consistent with commitments/events).
* Functions take raw asyncpg.Connection; caller owns transaction mgmt.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import asyncpg

from app.services.embeddings import canonical_artifact_embedding_text, content_hash
from app.services.message_embedding_lifecycle import enqueue_content_embed

logger = logging.getLogger(__name__)

# -- constants --

LIVE_PREP_KIND: str = "live_prep"
LIVE_DEBRIEF_KIND: str = "live_debrief"

ARTIFACT_TYPES: frozenset[str] = frozenset({
    "live_prep_brief", "live_debrief", "review_summary",
    "agenda_revision", "transcript_reflection",
})

RELATIONS: frozenset[str] = frozenset({
    "planned_item", "summarized_from", "evidence_quote",
    "extracted_memory", "extracted_observation", "extracted_distillation",
    "extracted_theme",
    "created_commitment", "updated_commitment", "closed_commitment",
    "logged_event", "created_follow_up", "updated_follow_up",
    "updated_topic_status",
    "created_watch_item", "updated_watch_item", "addressed_watch_item",
    "created_oob", "updated_oob", "lifted_oob",
})

ALLOWED_TARGET_TABLES: frozenset[str] = frozenset({
    "conversations", "conversation_items", "transcript_turns",
    "conversation_notes", "messages", "memories", "observations",
    "distillations", "commitments", "events", "scheduled_jobs",
    "topic_status",
    "themes", "watch_items", "out_of_bounds",
})

_MAX_REVISION_RETRIES = 5

# -- return models --


@dataclass
class ArtifactRow:
    id: str
    conversation_id: str
    bot_id: str
    user_id: str
    artifact_type: str
    payload: dict[str, Any]
    payload_version: int = 1
    revision_number: int = 1
    created_by_turn_id: str | None = None
    deleted_at: datetime | None = None
    expires_at: datetime | None = None
    created_at: datetime | None = None

    @classmethod
    def from_record(cls, rec: asyncpg.Record) -> "ArtifactRow":
        return cls(
            id=rec["id"], conversation_id=rec["conversation_id"],
            bot_id=rec["bot_id"], user_id=rec["user_id"],
            artifact_type=rec["artifact_type"], payload=rec["payload"],
            payload_version=rec["payload_version"],
            revision_number=rec["revision_number"],
            created_by_turn_id=rec.get("created_by_turn_id"),
            deleted_at=rec.get("deleted_at"),
            expires_at=rec.get("expires_at"),
            created_at=rec.get("created_at"),
        )


@dataclass
class ArtifactLinkRow:
    id: str
    artifact_id: str
    target_table: str
    target_id: str
    relation: str
    evidence: dict[str, Any] | None = None
    deleted_at: datetime | None = None
    created_at: datetime | None = None

    @classmethod
    def from_record(cls, rec: asyncpg.Record) -> "ArtifactLinkRow":
        return cls(
            id=rec["id"], artifact_id=rec["artifact_id"],
            target_table=rec["target_table"], target_id=rec["target_id"],
            relation=rec["relation"],
            evidence=rec.get("evidence"),
            deleted_at=rec.get("deleted_at"),
            created_at=rec.get("created_at"),
        )


# -- artifact CRUD --


async def create_artifact(
    conn: asyncpg.Connection,
    *,
    conversation_id: str,
    bot_id: str,
    user_id: str,
    artifact_type: str,
    payload: dict[str, Any],
    payload_version: int = 1,
    created_by_turn_id: str | None = None,
    expires_at: datetime | None = None,
    max_attempts: int = _MAX_REVISION_RETRIES,
) -> ArtifactRow:
    """Insert an artifact with auto-incremented revision_number.

    Uses savepoint-per-attempt so a concurrent insert of the same
    (conversation_id, artifact_type, revision_number) does not poison
    the caller's outer transaction.  Raises RuntimeError after
    *max_attempts* UniqueViolationErrors.
    """
    for attempt in range(1, max_attempts + 1):
        sp = f"rev_{attempt}"
        try:
            await conn.execute(f"SAVEPOINT {sp}")
            row = await conn.fetchrow(
                """
                INSERT INTO mediator.conversation_artifacts (
                    conversation_id, bot_id, user_id, artifact_type,
                    payload, payload_version, revision_number,
                    created_by_turn_id, expires_at
                )
                SELECT $1, $2, $3, $4, $5, $6,
                       COALESCE(
                           (SELECT MAX(revision_number)
                            FROM mediator.conversation_artifacts
                            WHERE conversation_id = $1
                              AND artifact_type = $4),
                           0
                       ) + 1,
                       $7, $8
                RETURNING *
                """,
                conversation_id, bot_id, user_id, artifact_type,
                payload, payload_version, created_by_turn_id, expires_at,
            )
            await conn.execute(f"RELEASE SAVEPOINT {sp}")
            artifact = ArtifactRow.from_record(row)
            canonical_text = canonical_artifact_embedding_text(artifact.artifact_type, artifact.payload)
            if canonical_text:
                await enqueue_content_embed(
                    conn,
                    source_type="artifact",
                    source_id=artifact.id,
                    content_hash=content_hash(canonical_text),
                )
            return artifact
        except asyncpg.exceptions.UniqueViolationError:
            await conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
            logger.debug(
                "create_artifact UV attempt %d/%d conv=%s type=%s",
                attempt, max_attempts, conversation_id, artifact_type,
            )
    raise RuntimeError(
        f"create_artifact failed after {max_attempts} attempts "
        f"conv={conversation_id} type={artifact_type}"
    )


async def get_current_artifact(
    conn: asyncpg.Connection,
    *,
    conversation_id: str,
    artifact_type: str,
) -> ArtifactRow | None:
    """Return the highest-revision non-deleted artifact, or None."""
    row = await conn.fetchrow(
        """
        SELECT * FROM mediator.conversation_artifacts
        WHERE conversation_id = $1 AND artifact_type = $2
          AND deleted_at IS NULL
        ORDER BY revision_number DESC LIMIT 1
        """,
        conversation_id, artifact_type,
    )
    return ArtifactRow.from_record(row) if row else None


async def list_artifacts(
    conn: asyncpg.Connection,
    *,
    conversation_id: str,
    artifact_type: str | None = None,
    include_deleted: bool = False,
) -> list[ArtifactRow]:
    """List artifacts for a conversation, with optional type/deleted filters."""
    clauses = ["conversation_id = $1"]
    params: list[Any] = [conversation_id]
    idx = 2
    if artifact_type is not None:
        clauses.append(f"artifact_type = ${idx}")
        params.append(artifact_type)
        idx += 1
    if not include_deleted:
        clauses.append("deleted_at IS NULL")
    rows = await conn.fetch(
        "SELECT * FROM mediator.conversation_artifacts WHERE "
        + " AND ".join(clauses)
        + " ORDER BY artifact_type, revision_number DESC",
        *params,
    )
    return [ArtifactRow.from_record(r) for r in rows]


# -- artifact link helpers --


async def add_artifact_link(
    conn: asyncpg.Connection,
    *,
    artifact_id: str,
    target_table: str,
    target_id: str,
    relation: str,
    evidence: dict[str, Any] | None = None,
    idempotent: bool = False,
) -> ArtifactLinkRow:
    """Insert a provenance link (insert-distinct by default).

    Default behaviour (``idempotent=False``): plain INSERT — each call
    creates a distinct row.  This is required for Sprint 4 debrief
    provenance where multiple evidence rows may point to the same
    (artifact, target_table, target_id, relation) tuple.

    When ``idempotent=True``: attempts INSERT ... ON CONFLICT DO NOTHING
    and returns the existing non-tombstoned row when a conflict matches.
    If the only existing row is soft-deleted a fresh row is inserted —
    never returns a tombstoned row.

    *target_table* and *relation* validated in-process (ValueError) before SQL.
    """
    if target_table not in ALLOWED_TARGET_TABLES:
        raise ValueError(
            f"target_table '{target_table}' not allowed. "
            f"Allowed: {sorted(ALLOWED_TARGET_TABLES)}"
        )
    if relation not in RELATIONS:
        raise ValueError(
            f"relation '{relation}' not allowed. "
            f"Allowed: {sorted(RELATIONS)}"
        )

    if not idempotent:
        # Insert-distinct: plain INSERT, every call creates a new row.
        row = await conn.fetchrow(
            """
            INSERT INTO mediator.artifact_links
                (artifact_id, target_table, target_id, relation, evidence)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING *
            """,
            artifact_id, target_table, target_id, relation, evidence,
        )
        if row is None:
            raise RuntimeError(
                f"add_artifact_link: insert returned nothing for "
                f"artifact={artifact_id} target=({target_table},{target_id})"
                f" relation={relation}"
            )
        return ArtifactLinkRow.from_record(row)

    # Idempotent: SELECT-first pattern — returns existing non-tombstoned row
    # or inserts a fresh one.  Uses SELECT-then-INSERT instead of ON CONFLICT
    # because migration 0054 drops the UNIQUE constraint on artifact_links.
    existing = await conn.fetchrow(
        """
        SELECT * FROM mediator.artifact_links
        WHERE artifact_id = $1 AND target_table = $2
          AND target_id = $3 AND relation = $4
          AND deleted_at IS NULL
        """,
        artifact_id, target_table, target_id, relation,
    )
    if existing is not None:
        return ArtifactLinkRow.from_record(existing)

    # No active row — insert a fresh one (even if a tombstoned row exists).
    fresh = await conn.fetchrow(
        """
        INSERT INTO mediator.artifact_links
            (artifact_id, target_table, target_id, relation, evidence)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING *
        """,
        artifact_id, target_table, target_id, relation, evidence,
    )
    if fresh is None:
        raise RuntimeError(
            f"add_artifact_link: fresh insert returned nothing for "
            f"artifact={artifact_id} target=({target_table},{target_id})"
            f" relation={relation}"
        )
    return ArtifactLinkRow.from_record(fresh)


async def list_artifact_links(
    conn: asyncpg.Connection,
    *,
    artifact_id: str | None = None,
    target_table: str | None = None,
    target_id: str | None = None,
    relation: str | None = None,
    include_deleted: bool = False,
) -> list[ArtifactLinkRow]:
    """List artifact links; supports reverse lookup by (target_table, target_id)."""
    clauses: list[str] = []
    params: list[Any] = []
    idx = 1
    for val, col in [
        (artifact_id, "artifact_id"), (target_table, "target_table"),
        (target_id, "target_id"), (relation, "relation"),
    ]:
        if val is not None:
            clauses.append(f"{col} = ${idx}")
            params.append(val)
            idx += 1
    if not include_deleted:
        clauses.append("deleted_at IS NULL")
    if not clauses:
        raise ValueError("list_artifact_links requires at least one filter")
    rows = await conn.fetch(
        "SELECT * FROM mediator.artifact_links WHERE "
        + " AND ".join(clauses) + " ORDER BY created_at ASC",
        *params,
    )
    return [ArtifactLinkRow.from_record(r) for r in rows]
