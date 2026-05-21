"""
Provenance helpers for live debrief durable writes (Sprint 4).

Provides:
  - Provisional artifact lifecycle (ensure / finalize / mark-failed)
  - Evidence validation & normalization (Sprint 4 shape)
  - Complete live-debrief tool → (target_table, relation, output_id_field,
    success_predicate) mapping for every guarded write tool

All functions accept a raw asyncpg.Connection; the caller owns transaction
management.  This module stays local to live debrief and does not touch
the non-chat runner or live-prep paths.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

import asyncpg

from app.services.live.artifacts import (
    ALLOWED_TARGET_TABLES,
    ArtifactRow,
    create_artifact,
)

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

PROVISIONAL_ARTIFACT_TYPE: str = "live_debrief"
"""Artifact type used for the provisional provenance artifact.

Reuses the existing ``live_debrief`` artifact type.  The provisional
artifact is created *before* any guarded durable write and all
successful writes link to this same artifact revision.  On success
the payload is populated with the submitted debrief content; on
failure it is soft-deleted.
"""

# ── Evidence shape (Sprint 4) ────────────────────────────────────────────────

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

ALLOWED_EVIDENCE_FIELDS: frozenset[str] = frozenset({
    "transcript_turn_ids", "quotes", "confidence", "reason",
})

CONFIDENCE_MAP: dict[str, float] = {
    "high": 0.9,
    "medium": 0.6,
    "low": 0.3,
}


def validate_artifact_link_evidence(evidence: dict[str, Any] | None) -> dict[str, Any] | None:
    """Validate evidence dict for artifact links (strict shape).

    Returns the validated dict on success, raises ``ValueError`` on
    invalid shape.  Returns ``None`` when *evidence* is ``None``.

    Rules
    -----
    * Unknown fields → rejected
    * ``transcript_turn_ids`` must be a list of UUID-format strings
    * ``quotes`` must be a list of strings (or absent)
    * ``confidence`` must be ``None`` or a float in [0.0, 1.0]
    * ``reason`` must be ``str | None``
    """
    if evidence is None:
        return None

    if not isinstance(evidence, dict):
        raise ValueError(
            f"artifact link evidence must be a dict, got {type(evidence).__name__}"
        )

    extra = set(evidence.keys()) - ALLOWED_EVIDENCE_FIELDS
    if extra:
        raise ValueError(
            f"unknown evidence fields: {sorted(extra)}. "
            f"Allowed: {sorted(ALLOWED_EVIDENCE_FIELDS)}"
        )

    # transcript_turn_ids
    turn_ids = evidence.get("transcript_turn_ids")
    if turn_ids is not None:
        if not isinstance(turn_ids, list):
            raise ValueError(
                f"transcript_turn_ids must be a list, got {type(turn_ids).__name__}"
            )
        for i, tid in enumerate(turn_ids):
            if not isinstance(tid, str):
                raise ValueError(
                    f"transcript_turn_ids[{i}] must be a string, got {type(tid).__name__}"
                )
            if not _UUID_RE.match(tid):
                raise ValueError(
                    f"transcript_turn_ids[{i}] is not a valid UUID: {tid!r}"
                )

    # quotes
    quotes = evidence.get("quotes")
    if quotes is not None:
        if not isinstance(quotes, list):
            raise ValueError(f"quotes must be a list, got {type(quotes).__name__}")
        for i, q in enumerate(quotes):
            if not isinstance(q, str):
                raise ValueError(
                    f"quotes[{i}] must be a string, got {type(q).__name__}"
                )

    # confidence
    confidence = evidence.get("confidence")
    if confidence is not None:
        if not isinstance(confidence, (int, float)):
            raise ValueError(
                f"confidence must be a number, got {type(confidence).__name__}"
            )
        if confidence < 0.0 or confidence > 1.0:
            raise ValueError(
                f"confidence must be in [0.0, 1.0], got {confidence}"
            )

    # reason
    reason = evidence.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise ValueError(f"reason must be a string or None, got {type(reason).__name__}")

    return evidence


def normalize_artifact_link_evidence(
    evidence: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Normalize Sprint 3 guard-format evidence → Sprint 4 shape.

    Accepts the Sprint 3 guard format (``evidence_refs`` with
    ``transcript_turn_id`` / ``quote`` / ``confidence`` strings) or
    a ``derivation_source`` marker.  Returns the Sprint 4 normalized
    shape, or ``None`` when *evidence* is ``None``.

    Rules
    -----
    * ``evidence_refs`` items:
        - ``transcript_turn_id`` → ``transcript_turn_ids``
        - ``quote`` → ``quotes``
        - ``confidence`` (string) mapped: high→0.9, medium→0.6, low→0.3;
          numeric passed through
    * ``derivation_source`` values:
        - ``transcript_turn_ids`` = [], ``quotes`` = [],
          ``confidence`` = None, ``reason`` = ``"derived_from:<source>"``
    """
    if evidence is None:
        return None

    # Already in Sprint 4 shape?  Validate and return.
    if "transcript_turn_ids" in evidence or "quotes" in evidence or "confidence" in evidence:
        validate_artifact_link_evidence(evidence)
        return evidence

    # Sprint 3 guard format: evidence_refs list
    evidence_refs: list[dict[str, Any]] = evidence.get("evidence_refs") or []
    derivation_source: str | None = evidence.get("derivation_source")

    if evidence_refs:
        turn_ids: list[str] = []
        quotes: list[str] = []
        confidence: float | None = None
        for ref in evidence_refs:
            tid = ref.get("transcript_turn_id", "")
            if tid:
                turn_ids.append(str(tid))
            q = ref.get("quote", "")
            if q:
                quotes.append(str(q))
            c = ref.get("confidence")
            if c is not None and confidence is None:
                if isinstance(c, (int, float)):
                    confidence = float(c)
                elif isinstance(c, str):
                    confidence = CONFIDENCE_MAP.get(c.lower(), None)

        result: dict[str, Any] = {
            "transcript_turn_ids": turn_ids,
            "quotes": quotes,
            "confidence": confidence,
        }
        reason = evidence.get("reason")
        if reason is not None:
            result["reason"] = str(reason)
        validate_artifact_link_evidence(result)
        return result

    if derivation_source:
        result = {
            "transcript_turn_ids": [],
            "quotes": [],
            "confidence": None,
            "reason": f"derived_from:{derivation_source}",
        }
        validate_artifact_link_evidence(result)
        return result

    # Unknown format — reject
    raise ValueError(
        "evidence must contain transcript_turn_ids/quotes/confidence "
        "(Sprint 4 shape), evidence_refs (Sprint 3 guard format), "
        "or derivation_source"
    )


# ── Provisional artifact lifecycle ───────────────────────────────────────────


async def ensure_live_debrief_provenance_artifact(
    conn: asyncpg.Connection,
    *,
    conversation_id: str,
    created_by_turn_id: str,
    bot_id: str = "mediator",
    user_id: str = "",
) -> ArtifactRow:
    """Ensure a live-debrief provisional provenance artifact exists.

    Keys **by ``created_by_turn_id``** (not just ``conversation_id``)
    so retries of the same conversation produce distinct provisional
    artifacts — preventing cross-attempt contamination.

    1. If a prior provisional artifact exists for this *turn*, return it.
    2. Otherwise, tombstone any existing non-finalized provisionals for
       this conversation (stale from a prior crashed attempt), then
       create a fresh provisional artifact.

    Parameters
    ----------
    conn:
        Open asyncpg connection (caller owns transaction).
    conversation_id:
        UUID string of the conversation.
    created_by_turn_id:
        UUID string of the producing bot_turn.
    bot_id:
        Bot identifier (default ``"mediator"``).
    user_id:
        User UUID string (optional — inferred from conversation when empty).

    Returns
    -------
    ArtifactRow
        The provisional artifact (ready for links to attach).
    """
    # ── 1. Check for existing provisional artifact keyed by turn_id ──
    existing = await _find_provisional_for_turn(
        conn, conversation_id, created_by_turn_id
    )
    if existing is not None:
        logger.debug(
            "ensure_live_debrief_provenance_artifact: reusing existing "
            "provisional artifact_id=%s for turn_id=%s",
            existing.id, created_by_turn_id,
        )
        return existing

    # ── 2. Tombstone stale provisionals (prior crashed attempt) ──────
    await _tombstone_stale_provisionals(conn, conversation_id)

    # ── 3. Create fresh provisional artifact ─────────────────────────
    logger.debug(
        "ensure_live_debrief_provenance_artifact: creating fresh "
        "provisional for conversation_id=%s turn_id=%s",
        conversation_id, created_by_turn_id,
    )
    artifact = await create_artifact(
        conn,
        conversation_id=conversation_id,
        bot_id=bot_id,
        user_id=user_id,
        artifact_type=PROVISIONAL_ARTIFACT_TYPE,
        payload={"status": "provisional", "created_by_turn_id": created_by_turn_id},
        payload_version=1,
        created_by_turn_id=created_by_turn_id,
    )
    return artifact


async def finalize_live_debrief_artifact(
    conn: asyncpg.Connection,
    *,
    artifact_id: str,
    content: dict[str, Any],
    created_by_turn_id: str,
) -> ArtifactRow:
    """Store submitted debrief content on the provisional artifact.

    Updates the artifact's payload to the submitted content and sets
    ``status`` to ``"finalized"``.  Does **not** create a new revision —
    the same row that collected links is finalized.

    Returns the updated artifact row.
    """
    row = await conn.fetchrow(
        """
        UPDATE mediator.conversation_artifacts
        SET payload = $2,
            payload_version = COALESCE(payload_version, 1) + 1
        WHERE id = $1
          AND artifact_type = $3
          AND deleted_at IS NULL
        RETURNING *
        """,
        artifact_id,
        {"status": "finalized", **content},
        PROVISIONAL_ARTIFACT_TYPE,
    )
    if row is None:
        raise ValueError(
            f"finalize_live_debrief_artifact: no active artifact found "
            f"for artifact_id={artifact_id} type={PROVISIONAL_ARTIFACT_TYPE}"
        )
    logger.info(
        "finalize_live_debrief_artifact: artifact_id=%s finalized turn_id=%s",
        artifact_id, created_by_turn_id,
    )
    return ArtifactRow.from_record(row)


async def mark_live_debrief_artifact_failed(
    conn: asyncpg.Connection,
    *,
    artifact_id: str,
    reason: str,
) -> None:
    """Soft-delete a provisional artifact and its links.

    Sets ``deleted_at = now()`` on the artifact and all of its
    ``artifact_links`` rows.  The artifact row remains in the table
    for audit but is excluded from ``get_current_artifact`` and
    ``list_artifacts`` (non-deleted filters).
    """
    now = datetime.now(timezone.utc)
    # Soft-delete links first (FK-constrained; cascading is not
    # configured for soft-deletes).
    link_result = await conn.execute(
        """
        UPDATE mediator.artifact_links
        SET deleted_at = $2
        WHERE artifact_id = $1 AND deleted_at IS NULL
        """,
        artifact_id, now,
    )
    # asyncpg.execute returns a string like "UPDATE N"; parse the count.
    link_tag = str(link_result)
    try:
        link_count = int(link_tag.split()[-1]) if link_tag.split() else 0
    except (ValueError, IndexError):
        link_count = 0

    art_result = await conn.execute(
        """
        UPDATE mediator.conversation_artifacts
        SET deleted_at = $2,
            payload = COALESCE(payload, '{}'::jsonb)
                      || jsonb_build_object('failure_reason', $3::text)
        WHERE id = $1 AND deleted_at IS NULL
        """,
        artifact_id, now, reason,
    )
    art_tag = str(art_result)
    try:
        art_count = int(art_tag.split()[-1]) if art_tag.split() else 0
    except (ValueError, IndexError):
        art_count = 0

    if art_count == 0:
        logger.warning(
            "mark_live_debrief_artifact_failed: artifact_id=%s was already "
            "deleted or does not exist", artifact_id,
        )
    else:
        logger.info(
            "mark_live_debrief_artifact_failed: artifact_id=%s reason=%r "
            "soft_deleted_links=%s",
            artifact_id, reason, link_count,
        )


# ── Internal helpers ─────────────────────────────────────────────────────────


async def _find_provisional_for_turn(
    conn: asyncpg.Connection,
    conversation_id: str,
    created_by_turn_id: str,
) -> ArtifactRow | None:
    """Return a non-deleted provisional artifact created by *created_by_turn_id*."""
    row = await conn.fetchrow(
        """
        SELECT * FROM mediator.conversation_artifacts
        WHERE conversation_id = $1
          AND artifact_type = $2
          AND created_by_turn_id = $3
          AND deleted_at IS NULL
        ORDER BY revision_number DESC
        LIMIT 1
        """,
        conversation_id, PROVISIONAL_ARTIFACT_TYPE, created_by_turn_id,
    )
    return ArtifactRow.from_record(row) if row else None


async def _tombstone_stale_provisionals(
    conn: asyncpg.Connection,
    conversation_id: str,
) -> None:
    """Soft-delete provisional artifacts that are NOT finalized.

    A provisional artifact is considered stale if its payload does not
    contain ``"status": "finalized"``.  This clears artifacts from a
    prior crashed debrief attempt so a fresh retry starts clean.
    """
    now = datetime.now(timezone.utc)
    # Find artifact IDs to tombstone.
    stale_ids = await conn.fetch(
        """
        SELECT id FROM mediator.conversation_artifacts
        WHERE conversation_id = $1
          AND artifact_type = $2
          AND deleted_at IS NULL
          AND (payload ->> 'status' IS DISTINCT FROM 'finalized')
        """,
        conversation_id, PROVISIONAL_ARTIFACT_TYPE,
    )
    for row in stale_ids:
        aid = row["id"]
        await conn.execute(
            "UPDATE mediator.artifact_links SET deleted_at = $2 "
            "WHERE artifact_id = $1 AND deleted_at IS NULL",
            aid, now,
        )
        await conn.execute(
            "UPDATE mediator.conversation_artifacts SET deleted_at = $2 "
            "WHERE id = $1 AND deleted_at IS NULL",
            aid, now,
        )
        logger.debug("_tombstone_stale_provisionals: tombstoned artifact_id=%s", aid)


# ── Complete live-debrief tool → output mapping ──────────────────────────────


@dataclass
class DebriefToolOutputMapping:
    """Maps a guarded write tool to its provenance output shape.

    Attributes
    ----------
    target_table:
        Unqualified canonical table name (must be in ALLOWED_TARGET_TABLES).
    relation:
        Relation label (must be in artifacts.RELATIONS).
    output_id_field:
        The field name on the tool's output model that carries the
        stable ID to link to (e.g. ``"id"``, ``"new_id"``, ``"job_id"``,
        ``"commitment_id"``, ``"event_id"``).
    success_predicate:
        A callable ``(output: dict) -> bool`` that returns ``True`` when
        the output represents a successful durable write (vs. no-op or
        error).  Receives the tool output converted to a plain dict.
    """

    target_table: str
    relation: str
    output_id_field: str
    success_predicate: Callable[[dict[str, Any]], bool]


def _action_is(*actions: str) -> Callable[[dict[str, Any]], bool]:
    """Predicate: output action field matches one of *actions*."""
    return lambda out: out.get("action") in actions


def _no_error(out: dict[str, Any]) -> bool:
    """Predicate: output has is_error == False or absent."""
    return not out.get("is_error", False)


def _has_field(*fields: str) -> Callable[[dict[str, Any]], bool]:
    """Predicate: output has all named fields with truthy values."""
    def _check(out: dict[str, Any]) -> bool:
        return all(bool(out.get(f)) for f in fields)
    return _check


def _no_error_and_has(*fields: str) -> Callable[[dict[str, Any]], bool]:
    """Predicate: _no_error AND has all named fields with truthy values."""
    def _check(out: dict[str, Any]) -> bool:
        return _no_error(out) and all(bool(out.get(f)) for f in fields)
    return _check


def _scheduled_update_success(out: dict[str, Any]) -> bool:
    """Predicate for update_scheduled_task / update_scheduled_checkin.

    Captures only when action != 'noop' AND a stable job_id is present.
    This explicitly excludes no-op outputs (action='noop') even when they
    include a job_id.
    """
    return out.get("action") == "updated" and bool(out.get("job_id"))


# Every tool in LIVE_DEBRIEF_GUARDED_WRITE_TOOLS is covered.
# Output shapes verified against tool_schemas.py (2026-05-21).
LIVE_DEBRIEF_TOOL_OUTPUT_MAP: dict[str, DebriefToolOutputMapping] = {
    # ── memories ──────────────────────────────────────────────────────
    "add_memory": DebriefToolOutputMapping(
        target_table="memories",
        relation="extracted_memory",
        output_id_field="id",
        success_predicate=_action_is("created"),
    ),
    "update_memory": DebriefToolOutputMapping(
        target_table="memories",
        relation="extracted_memory",
        output_id_field="id",
        success_predicate=_action_is("updated"),
    ),
    "supersede_memory": DebriefToolOutputMapping(
        target_table="memories",
        relation="extracted_memory",
        output_id_field="new_id",          # capture new_id, not old_id
        success_predicate=_action_is("superseded"),
    ),
    # ── observations ──────────────────────────────────────────────────
    "log_observation": DebriefToolOutputMapping(
        target_table="observations",
        relation="extracted_observation",
        output_id_field="id",
        success_predicate=_action_is("created"),
    ),
    "update_observation": DebriefToolOutputMapping(
        target_table="observations",
        relation="extracted_observation",
        output_id_field="id",
        success_predicate=_action_is("updated"),
    ),
    # ── distillations ─────────────────────────────────────────────────
    "add_distillation": DebriefToolOutputMapping(
        target_table="distillations",
        relation="extracted_distillation",
        output_id_field="id",
        success_predicate=_action_is("created"),
    ),
    "update_distillation": DebriefToolOutputMapping(
        target_table="distillations",
        relation="extracted_distillation",
        output_id_field="id",
        success_predicate=_action_is("updated"),
    ),
    "revise_distillation": DebriefToolOutputMapping(
        target_table="distillations",
        relation="extracted_distillation",
        output_id_field="new_id",          # capture new_id, not old_id
        success_predicate=_action_is("revised"),
    ),
    # ── themes ────────────────────────────────────────────────────────
    "create_theme": DebriefToolOutputMapping(
        target_table="themes",
        relation="extracted_theme",
        output_id_field="id",
        success_predicate=_action_is("created"),
    ),
    "update_theme": DebriefToolOutputMapping(
        target_table="themes",
        relation="extracted_theme",
        output_id_field="id",
        success_predicate=_action_is("updated"),
    ),
    # ── watch items ───────────────────────────────────────────────────
    "add_watch_item": DebriefToolOutputMapping(
        target_table="watch_items",
        relation="created_watch_item",
        output_id_field="id",
        success_predicate=_action_is("created"),
    ),
    "update_watch_item": DebriefToolOutputMapping(
        target_table="watch_items",
        relation="updated_watch_item",
        output_id_field="id",
        success_predicate=_action_is("updated"),
    ),
    "address_watch_item": DebriefToolOutputMapping(
        target_table="watch_items",
        relation="addressed_watch_item",
        output_id_field="id",
        success_predicate=_action_is("addressed"),
    ),
    # ── OOB (out_of_bounds) ───────────────────────────────────────────
    "add_oob": DebriefToolOutputMapping(
        target_table="out_of_bounds",
        relation="created_oob",
        output_id_field="id",
        success_predicate=_action_is("created"),
    ),
    "update_oob": DebriefToolOutputMapping(
        target_table="out_of_bounds",
        relation="updated_oob",
        output_id_field="id",
        success_predicate=_action_is("updated"),
    ),
    "lift_oob": DebriefToolOutputMapping(
        target_table="out_of_bounds",
        relation="lifted_oob",
        output_id_field="id",
        success_predicate=_action_is("lifted"),
    ),
    # ── commitments ───────────────────────────────────────────────────
    "create_commitment": DebriefToolOutputMapping(
        target_table="commitments",
        relation="created_commitment",
        output_id_field="commitment_id",
        success_predicate=_no_error_and_has("commitment_id"),
    ),
    "update_commitment": DebriefToolOutputMapping(
        target_table="commitments",
        relation="updated_commitment",
        output_id_field="commitment_id",
        success_predicate=_no_error_and_has("commitment_id", "updated_at"),
    ),
    "close_commitment": DebriefToolOutputMapping(
        target_table="commitments",
        relation="closed_commitment",
        output_id_field="commitment_id",
        # Must confirm a status transition occurred: status + closed_at present.
        success_predicate=_no_error_and_has("status", "closed_at"),
    ),
    # ── events ────────────────────────────────────────────────────────
    "log_event": DebriefToolOutputMapping(
        target_table="events",
        relation="logged_event",
        output_id_field="event_id",
        success_predicate=_no_error,
    ),
    # ── scheduled jobs ────────────────────────────────────────────────
    "schedule_checkin": DebriefToolOutputMapping(
        target_table="scheduled_jobs",
        relation="created_follow_up",
        output_id_field="job_id",
        success_predicate=_action_is("scheduled"),
    ),
    "schedule_task": DebriefToolOutputMapping(
        target_table="scheduled_jobs",
        relation="created_follow_up",
        output_id_field="job_id",
        success_predicate=_action_is("scheduled"),
    ),
    "update_scheduled_task": DebriefToolOutputMapping(
        target_table="scheduled_jobs",
        relation="updated_follow_up",
        output_id_field="job_id",
        # "noop" means no write happened — only link on actual updates.
        # Also requires a stable job_id to be present (no-op outputs may
        # include a job_id but action='noop' excludes them).
        success_predicate=_scheduled_update_success,
    ),
    "update_scheduled_checkin": DebriefToolOutputMapping(
        target_table="scheduled_jobs",
        relation="updated_follow_up",
        output_id_field="job_id",
        success_predicate=_scheduled_update_success,
    ),
}


# ── Assertions: every guarded write tool is covered ──────────────────────────

def _verify_mapping_coverage() -> None:
    """Assert that LIVE_DEBRIEF_TOOL_OUTPUT_MAP covers every tool in
    LIVE_DEBRIEF_GUARDED_WRITE_TOOLS (from registry.py).  Called at
    import time — raises AssertionError if the mapping is incomplete.
    """
    from app.services.tools.registry import LIVE_DEBRIEF_GUARDED_WRITE_TOOLS

    missing = LIVE_DEBRIEF_GUARDED_WRITE_TOOLS - set(LIVE_DEBRIEF_TOOL_OUTPUT_MAP.keys())
    extra = set(LIVE_DEBRIEF_TOOL_OUTPUT_MAP.keys()) - LIVE_DEBRIEF_GUARDED_WRITE_TOOLS
    if missing:
        raise AssertionError(
            f"LIVE_DEBRIEF_TOOL_OUTPUT_MAP is missing tools: {sorted(missing)}"
        )
    if extra:
        raise AssertionError(
            f"LIVE_DEBRIEF_TOOL_OUTPUT_MAP has extra tools not in "
            f"LIVE_DEBRIEF_GUARDED_WRITE_TOOLS: {sorted(extra)}"
        )

    # Verify every target_table is in ALLOWED_TARGET_TABLES
    for name, mapping in LIVE_DEBRIEF_TOOL_OUTPUT_MAP.items():
        if mapping.target_table not in ALLOWED_TARGET_TABLES:
            raise AssertionError(
                f"LIVE_DEBRIEF_TOOL_OUTPUT_MAP['{name}'].target_table="
                f"'{mapping.target_table}' is not in ALLOWED_TARGET_TABLES"
            )

    from app.services.live.artifacts import RELATIONS
    for name, mapping in LIVE_DEBRIEF_TOOL_OUTPUT_MAP.items():
        if mapping.relation not in RELATIONS:
            raise AssertionError(
                f"LIVE_DEBRIEF_TOOL_OUTPUT_MAP['{name}'].relation="
                f"'{mapping.relation}' is not in RELATIONS"
            )


# ── Reverse lookup ───────────────────────────────────────────────────────────


async def find_artifact_links_for_target(
    conn: asyncpg.Connection,
    *,
    target_table: str,
    target_id: str,
) -> list[dict[str, Any]]:
    """Find all provenance links pointing to a specific durable row.

    Returns a list of link dicts (including artifact and evidence data)
    for reverse-lookup use cases (e.g., "which debrief produced this
    memory?").
    """
    from app.services.live.artifacts import list_artifact_links

    links = await list_artifact_links(
        conn,
        target_table=target_table,
        target_id=target_id,
    )
    return [
        {
            "link_id": link.id,
            "artifact_id": link.artifact_id,
            "relation": link.relation,
            "evidence": link.evidence,
            "created_at": link.created_at.isoformat() if link.created_at else None,
        }
        for link in links
    ]


# ── Reverse provenance query helpers (Sprint 4 T11) ──────────────────────────


async def get_source_conversations_for_durable_record(
    conn: asyncpg.Connection,
    *,
    target_table: str,
    target_id: str,
    include_deleted: bool = False,
) -> list[dict[str, Any]]:
    """Find all source conversations that produced links to a durable record.

    Reverse lookup: given a durable row (target_table + target_id), returns
    the conversations whose debrief artifacts produced links pointing at it.

    Join path: ``artifact_links → conversation_artifacts → conversations``.

    Parameters
    ----------
    conn:
        Open asyncpg connection (caller owns transaction).
    target_table:
        Unqualified canonical table name (e.g. ``"memories"``).
    target_id:
        UUID string of the target row.
    include_deleted:
        When ``False`` (default), soft-deleted links and soft-deleted
        artifacts are excluded.  When ``True``, all rows are returned
        including deleted/failed state metadata.

    Returns
    -------
    list[dict]
        Each dict contains:
        - ``link_id``: UUID of the artifact_links row
        - ``artifact_id``: UUID of the conversation_artifact
        - ``artifact_type``: e.g. ``"live_debrief"``
        - ``revision_number``: revision of the artifact
        - ``created_turn_id``: bot_turn that created the artifact
        - ``conversation_id``: UUID of the source conversation
        - ``relation``: link relation label
        - ``evidence``: jsonb evidence payload (or None)
        - ``link_deleted``: bool — whether the link is soft-deleted
        - ``artifact_deleted``: bool — whether the artifact is soft-deleted
        - ``link_created_at``: ISO-8601 timestamp of link creation
    """
    deleted_filter = ""
    if not include_deleted:
        deleted_filter = (
            "AND al.deleted_at IS NULL "
            "AND ca.deleted_at IS NULL"
        )

    rows = await conn.fetch(
        f"""
        SELECT
            al.id               AS link_id,
            al.artifact_id      AS artifact_id,
            ca.artifact_type    AS artifact_type,
            ca.revision_number  AS revision_number,
            ca.created_by_turn_id AS created_turn_id,
            ca.conversation_id  AS conversation_id,
            al.relation         AS relation,
            al.evidence         AS evidence,
            al.deleted_at IS NOT NULL AS link_deleted,
            ca.deleted_at IS NOT NULL AS artifact_deleted,
            al.created_at       AS link_created_at
        FROM mediator.artifact_links al
        JOIN mediator.conversation_artifacts ca
            ON ca.id = al.artifact_id
        WHERE al.target_table = $1
          AND al.target_id = $2
          {deleted_filter}
        ORDER BY al.created_at ASC
        """,
        target_table, target_id,
    )

    return [
        {
            "link_id": str(row["link_id"]),
            "artifact_id": str(row["artifact_id"]),
            "artifact_type": row["artifact_type"],
            "revision_number": row["revision_number"],
            "created_turn_id": (
                str(row["created_turn_id"]) if row["created_turn_id"] else None
            ),
            "conversation_id": (
                str(row["conversation_id"]) if row["conversation_id"] else None
            ),
            "relation": row["relation"],
            "evidence": row["evidence"],
            "link_deleted": row["link_deleted"],
            "artifact_deleted": row["artifact_deleted"],
            "link_created_at": (
                row["link_created_at"].isoformat()
                if row["link_created_at"] else None
            ),
        }
        for row in rows
    ]


async def list_durable_writes_for_conversation(
    conn: asyncpg.Connection,
    *,
    conversation_id: str,
    include_deleted: bool = False,
) -> list[dict[str, Any]]:
    """List all durable writes linked to a conversation's debrief artifacts.

    Groups linked durable records by ``(target_table, relation)`` so
    callers can see every durable row touched by debrief artifacts in
    this conversation.

    Join path: ``conversation_artifacts → artifact_links``.

    Parameters
    ----------
    conn:
        Open asyncpg connection (caller owns transaction).
    conversation_id:
        UUID string of the conversation.
    include_deleted:
        When ``False`` (default), soft-deleted links and artifacts are
        excluded.  When ``True``, all rows are returned.

    Returns
    -------
    list[dict]
        Each dict contains:
        - ``link_id``: UUID of the artifact_links row
        - ``artifact_id``: UUID of the conversation_artifact
        - ``artifact_type``: e.g. ``"live_debrief"``
        - ``revision_number``: revision of the artifact
        - ``created_turn_id``: bot_turn that created the artifact
        - ``target_table``: the durable table written to
        - ``target_id``: UUID of the durable row
        - ``relation``: link relation label
        - ``evidence``: jsonb evidence payload (or None)
        - ``link_deleted``: bool
        - ``artifact_deleted``: bool
        - ``link_created_at``: ISO-8601 timestamp
    """
    deleted_filter = ""
    if not include_deleted:
        deleted_filter = (
            "AND al.deleted_at IS NULL "
            "AND ca.deleted_at IS NULL"
        )

    rows = await conn.fetch(
        f"""
        SELECT
            al.id               AS link_id,
            al.artifact_id      AS artifact_id,
            ca.artifact_type    AS artifact_type,
            ca.revision_number  AS revision_number,
            ca.created_by_turn_id AS created_turn_id,
            al.target_table     AS target_table,
            al.target_id        AS target_id,
            al.relation         AS relation,
            al.evidence         AS evidence,
            al.deleted_at IS NOT NULL AS link_deleted,
            ca.deleted_at IS NOT NULL AS artifact_deleted,
            al.created_at       AS link_created_at
        FROM mediator.conversation_artifacts ca
        JOIN mediator.artifact_links al
            ON al.artifact_id = ca.id
        WHERE ca.conversation_id = $1
          {deleted_filter}
        ORDER BY al.target_table, al.relation, al.created_at ASC
        """,
        conversation_id,
    )

    return [
        {
            "link_id": str(row["link_id"]),
            "artifact_id": str(row["artifact_id"]),
            "artifact_type": row["artifact_type"],
            "revision_number": row["revision_number"],
            "created_turn_id": (
                str(row["created_turn_id"]) if row["created_turn_id"] else None
            ),
            "target_table": row["target_table"],
            "target_id": str(row["target_id"]),
            "relation": row["relation"],
            "evidence": row["evidence"],
            "link_deleted": row["link_deleted"],
            "artifact_deleted": row["artifact_deleted"],
            "link_created_at": (
                row["link_created_at"].isoformat()
                if row["link_created_at"] else None
            ),
        }
        for row in rows
    ]


# ── Rollback / deletion helper ───────────────────────────────────────────────

# Per-table cleanup semantics — only uses existing status/soft-delete columns.
# No new columns are introduced for rollback.
#
# Each entry maps ``target_table`` → a dict with:
#   * ``status_column``: name of the status column (or None if none exists)
#   * ``cleanup_status``: status value to set during rollback
#   * ``extra_columns``: mapping of extra column → value (e.g. ``retired_at``)
#   * ``pending_check``: optional SQL predicate to restrict which rows can
#     be cleaned up (e.g. ``"status = 'pending'"`` for scheduled_jobs)
#   * ``cleanup_capable``: whether this table supports automated cleanup
#     (True) or is enumerate-only (False).

_ROLLBACK_TABLE_SEMANTICS: dict[str, dict[str, Any]] = {
    "memories": {
        "status_column": "status",
        "cleanup_status": "invalidated",
        "extra_columns": {},
        "pending_check": None,
        "cleanup_capable": True,
    },
    "observations": {
        "status_column": "status",
        "cleanup_status": "stale",
        "extra_columns": {},
        "pending_check": None,
        "cleanup_capable": True,
    },
    "distillations": {
        "status_column": "status",
        "cleanup_status": "retired",
        "extra_columns": {"retired_at": "now()"},
        "pending_check": None,
        "cleanup_capable": True,
    },
    "commitments": {
        "status_column": "status",
        "cleanup_status": "dropped",
        "extra_columns": {},
        "pending_check": None,
        "cleanup_capable": True,
    },
    "scheduled_jobs": {
        "status_column": "status",
        "cleanup_status": "cancelled",
        "extra_columns": {},
        "pending_check": "status = 'pending'",
        "cleanup_capable": True,
    },
    "themes": {
        "status_column": "status",
        "cleanup_status": "resolved",
        "extra_columns": {},
        "pending_check": None,
        "cleanup_capable": True,
    },
    "watch_items": {
        "status_column": "status",
        "cleanup_status": "cancelled",
        "extra_columns": {},
        "pending_check": None,
        "cleanup_capable": True,
    },
    "out_of_bounds": {
        "status_column": "status",
        "cleanup_status": "lifted",
        "extra_columns": {},
        "pending_check": None,
        "cleanup_capable": True,
    },
    # Enumerate-only tables — no status/soft-delete semantics for rollback.
    "events": {
        "status_column": None,
        "cleanup_status": None,
        "extra_columns": {},
        "pending_check": None,
        "cleanup_capable": False,
    },
    "topic_status": {
        "status_column": None,
        "cleanup_status": None,
        "extra_columns": {},
        "pending_check": None,
        "cleanup_capable": False,
    },
}


async def enumerate_linked_durable_records(
    conn: asyncpg.Connection,
    *,
    conversation_id: str,
) -> list[dict[str, Any]]:
    """Dry-run: enumerate all linked durable records for a conversation.

    Returns every linked durable row with its cleanup capability so
    callers can preview what a rollback would touch before committing.

    Each returned dict contains:
        - ``target_table``: the durable table
        - ``target_id``: UUID of the durable row
        - ``relation``: link relation label
        - ``link_id``: UUID of the artifact_links row
        - ``artifact_id``: UUID of the artifact
        - ``cleanup_capable``: bool — whether automated cleanup is supported
        - ``cleanup_action``: description of the planned status change (or
          ``"enumerate-only"`` for unsupported tables)

    This is a read-only helper — no mutations are performed.
    """
    # Reuse the existing list helper to get all linked records.
    links = await list_durable_writes_for_conversation(
        conn,
        conversation_id=conversation_id,
    )

    result: list[dict[str, Any]] = []
    for link in links:
        target_table = link["target_table"]
        semantics = _ROLLBACK_TABLE_SEMANTICS.get(target_table, {})

        cleanup_capable = bool(semantics.get("cleanup_capable", False))
        if cleanup_capable:
            cleanup_action = (
                f"SET {semantics['status_column']} = "
                f"'{semantics['cleanup_status']}'"
            )
            extra = semantics.get("extra_columns", {})
            if extra:
                for col, val in extra.items():
                    if val == "now()":
                        cleanup_action += f", {col} = now()"
                    else:
                        cleanup_action += f", {col} = {val!r}"
            pending_check = semantics.get("pending_check")
            if pending_check:
                cleanup_action += f" WHERE {pending_check}"
        else:
            cleanup_action = "enumerate-only"

        result.append({
            "target_table": target_table,
            "target_id": link["target_id"],
            "relation": link["relation"],
            "link_id": link["link_id"],
            "artifact_id": link["artifact_id"],
            "cleanup_capable": cleanup_capable,
            "cleanup_action": cleanup_action,
        })

    return result


async def rollback_linked_durable_records(
    conn: asyncpg.Connection,
    *,
    conversation_id: str,
    dry_run: bool = True,
) -> list[dict[str, Any]]:
    """Rollback (soft-delete/status-change) durable records linked to a
    conversation's debrief artifacts.

    Uses only existing status/soft-delete semantics — never adds columns.

    Parameters
    ----------
    conn:
        Open asyncpg connection (caller owns transaction).
    conversation_id:
        UUID string of the conversation whose linked records to roll back.
    dry_run:
        When ``True`` (default), enumerates and returns planned actions
        without performing mutations.  When ``False``, executes the
        mutations.

    Returns
    -------
    list[dict]
        Each dict describes a linked record and contains:
        - ``target_table``, ``target_id``, ``relation``
        - ``cleanup_capable``: whether a mutation was/would-be applied
        - ``cleanup_action``: description of the action
        - ``rolled_back``: ``True`` if a mutation was actually applied
          (always ``False`` in dry-run mode)
        - ``error``: error message if the update failed (``None`` on success)
    """
    enumeration = await enumerate_linked_durable_records(
        conn, conversation_id=conversation_id,
    )

    if dry_run:
        for item in enumeration:
            item["rolled_back"] = False
            item["error"] = None
        return enumeration

    # ── Execute mutations ────────────────────────────────────────────

    for item in enumeration:
        item["error"] = None
        item["rolled_back"] = False

        if not item["cleanup_capable"]:
            continue

        target_table = item["target_table"]
        target_id = item["target_id"]
        semantics = _ROLLBACK_TABLE_SEMANTICS.get(target_table, {})
        if not semantics:
            continue

        status_col = semantics["status_column"]
        cleanup_status = semantics["cleanup_status"]
        extra_cols = semantics.get("extra_columns", {})
        pending_check = semantics.get("pending_check")

        # Build SET clause — use now() inline for timestamp columns
        # to avoid parameter-count mismatches.
        set_parts = [f"{status_col} = '{cleanup_status}'"]
        for col, val in extra_cols.items():
            if val == "now()":
                set_parts.append(f"{col} = now()")
            else:
                set_parts.append(f"{col} = '{val}'")

        set_clause = ", ".join(set_parts)

        # Build WHERE clause — $1 is the only parameter.
        where_clause = "id = $1"
        if pending_check:
            where_clause += f" AND {pending_check}"

        sql = (
            f"UPDATE mediator.{target_table} "
            f"SET {set_clause} WHERE {where_clause}"
        )

        try:
            result_row = await conn.execute(sql, target_id)

            result_tag = str(result_row)
            try:
                update_count = int(result_tag.split()[-1]) if result_tag.split() else 0
            except (ValueError, IndexError):
                update_count = 0
            if update_count > 0:
                item["rolled_back"] = True
                logger.info(
                    "rollback_linked_durable_records: %s/%s → %s",
                    target_table, target_id, cleanup_status,
                )
            else:
                logger.debug(
                    "rollback_linked_durable_records: %s/%s no rows affected "
                    "(may already be in target state or filtered by pending_check)",
                    target_table, target_id,
                )
        except Exception as exc:
            item["error"] = str(exc)
            logger.error(
                "rollback_linked_durable_records: failed to update %s/%s: %s",
                target_table, target_id, exc,
            )

    return enumeration
