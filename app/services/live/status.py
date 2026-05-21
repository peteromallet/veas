"""Canonical live-conversation status helpers (Sprint 5).

This module is the single source of truth for status canonicalization.
Every live endpoint that returns a ``conversations.status`` value must
route it through :func:`canonicalize_status` before returning to clients.

Canonical statuses (the public API contract):
    * ``preparing`` — agenda prep is in progress (replaces ``prepping``)
    * ``ready`` — agenda ready, waiting for consent/start
    * ``active`` — conversation is live/in-progress (replaces ``live``)
    * ``debriefing`` — agentic debrief is running
    * ``review_pending`` — review ready for user to inspect/save
    * ``completed`` — review saved, session finished (replaces ``synthesized`` / ``ended``)
    * ``prep_failed`` — prep could not produce an agenda
    * ``debrief_failed`` — debrief could not finish

Legacy compatibility (read path only):
    * ``prepping`` → ``preparing``
    * ``live`` → ``active``
    * ``synthesized`` → ``completed``
    * ``ended`` → ``completed``
    * ``synthesizing`` → ``debriefing``
"""

from __future__ import annotations

from typing import Any

# ── Canonical status constants ─────────────────────────────────────────────

CANONICAL_PREPARING: str = "preparing"
CANONICAL_READY: str = "ready"
CANONICAL_ACTIVE: str = "active"
CANONICAL_DEBRIEFING: str = "debriefing"
CANONICAL_REVIEW_PENDING: str = "review_pending"
CANONICAL_COMPLETED: str = "completed"
CANONICAL_PREP_FAILED: str = "prep_failed"
CANONICAL_DEBRIEF_FAILED: str = "debrief_failed"

# Set of all canonical statuses.
CANONICAL_STATUSES: frozenset[str] = frozenset(
    {
        CANONICAL_PREPARING,
        CANONICAL_READY,
        CANONICAL_ACTIVE,
        CANONICAL_DEBRIEFING,
        CANONICAL_REVIEW_PENDING,
        CANONICAL_COMPLETED,
        CANONICAL_PREP_FAILED,
        CANONICAL_DEBRIEF_FAILED,
    }
)

# ── Legacy → canonical mapping ─────────────────────────────────────────────

# These are read-side only.  Writers should use canonical values once the
# additive migration 0055 is applied.
LEGACY_TO_CANONICAL: dict[str, str] = {
    "prepping": CANONICAL_PREPARING,
    "live": CANONICAL_ACTIVE,
    "synthesized": CANONICAL_COMPLETED,
    "ended": CANONICAL_COMPLETED,
    "synthesizing": CANONICAL_DEBRIEFING,
}

# Inverse mapping for ops metrics / compatibility predicate groups.
CANONICAL_TO_LEGACY: dict[str, list[str]] = {
    CANONICAL_PREPARING: ["prepping"],
    CANONICAL_ACTIVE: ["live"],
    CANONICAL_COMPLETED: ["synthesized", "ended"],
    CANONICAL_DEBRIEFING: ["synthesizing"],
}

# ── Compatibility predicate groups (for metrics / partial indexes) ──────────

# Active sessions: canonical + legacy statuses that represent an
# in-progress or pending session.
ACTIVE_CANONICAL: frozenset[str] = frozenset(
    {CANONICAL_PREPARING, CANONICAL_READY, CANONICAL_ACTIVE, CANONICAL_REVIEW_PENDING}
)
ACTIVE_LEGACY: frozenset[str] = frozenset({"prepping", "live"})

# Completed sessions: canonical + legacy statuses that represent a
# terminal / finished session.
COMPLETED_CANONICAL: frozenset[str] = frozenset({CANONICAL_COMPLETED})
COMPLETED_LEGACY: frozenset[str] = frozenset({"synthesized", "ended"})

# Failed sessions: canonical + legacy statuses that represent a failure.
FAILED_CANONICAL: frozenset[str] = frozenset(
    {CANONICAL_PREP_FAILED, CANONICAL_DEBRIEF_FAILED}
)
FAILED_LEGACY: frozenset[str] = frozenset({"failed", "discarded"})


# ── Public helpers ─────────────────────────────────────────────────────────


def canonicalize_status(status: str) -> str:
    """Return the canonical status for *status*.

    Canonical values pass through unchanged.  Legacy statuses are mapped
    to their canonical equivalents.  Unknown values are returned as-is
    (defensive passthrough).
    """
    return LEGACY_TO_CANONICAL.get(status, status)


def is_active_status(status: str) -> bool:
    """Return True when *status* represents an active/pending session."""
    canonical = canonicalize_status(status)
    return canonical in ACTIVE_CANONICAL


def is_completed_status(status: str) -> bool:
    """Return True when *status* represents a completed session."""
    canonical = canonicalize_status(status)
    return canonical in COMPLETED_CANONICAL


def is_failed_status(status: str) -> bool:
    """Return True when *status* represents a failed session."""
    canonical = canonicalize_status(status)
    return canonical in FAILED_CANONICAL


def normalize_row_status(row: dict[str, Any], status_key: str = "status") -> dict[str, Any]:
    """Canonicalize the ``status`` field in a row dict.

    Returns a **new** dict with the status field canonicalized.  The
    original dict is not mutated.
    """
    normalized = dict(row)
    if status_key in normalized and isinstance(normalized[status_key], str):
        normalized[status_key] = canonicalize_status(normalized[status_key])
    return normalized


def grouped_status_metric(
    rows: list[dict[str, Any]],
    status_key: str = "status",
) -> dict[str, int]:
    """Aggregate *rows* by canonical status, folding in legacy equivalents.

    Returns a dict of ``{canonical_status: count}``.  Rows with unknown
    / unrecognized statuses are grouped under ``"unknown"``.
    """
    counts: dict[str, int] = {}
    for row in rows:
        raw = row.get(status_key)
        if isinstance(raw, str):
            canonical = canonicalize_status(raw)
        else:
            canonical = "unknown"
        counts[canonical] = counts.get(canonical, 0) + 1
    return counts
