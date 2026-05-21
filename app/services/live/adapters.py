"""Adapters that convert live-service artifact payloads into client-visible shapes.

SessionReview shape (the contract the UI expects):
    {
      "session_id": str,
      "bot_id": str | None,
      "status": str,
      "started_at": str | None,
      "ended_at": str | None,
      "prep_summary": str | None,
      "what_heard": list[str] | list[dict],
      "what_decided": list[dict],
      "still_open": list[dict],
      "what_to_remember": list[dict],
      "is_empty": bool,
    }

The ``_debrief_artifact_to_session_review`` adapter converts a raw
``live_debrief`` artifact payload (as produced by ``submit_live_debrief``)
into this shape.  The LLM may emit scalar strings for any of the four
content fields; the adapter coerces each to the array/object contract
the UI depends on.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# ── Per-field coercion helpers ────────────────────────────────────────────────

_SESSION_REVIEW_FIELDS = (
    "what_heard",
    "what_decided",
    "still_open",
    "what_to_remember",
)


def _coerce_review_field(raw: Any, *, field_name: str) -> list[Any]:
    """Coerce a single ``live_debrief`` payload field to the UI contract.

    Rules (per task T5 watch items):
    1. ``None`` → empty list.
    2. Scalar string: trim.  Omit when empty.  Wrap in a one-element list
       with ``source: 'live_debrief'`` metadata.
    3. List of strings: keep as-is, each item gains ``source`` metadata.
    4. List of dicts: validate keys present, add ``source`` if missing.
    5. Any other type: log a warning and return an empty list (defensive).
    """
    if raw is None:
        return []

    # ── Scalar string ────────────────────────────────────────────────────
    if isinstance(raw, str):
        trimmed = raw.strip()
        if not trimmed:
            return []
        # Wrap in a single UI-compatible item.
        return [{"text": trimmed, "source": "live_debrief"}]

    # ── List ─────────────────────────────────────────────────────────────
    if isinstance(raw, list):
        coerced: list[Any] = []
        for item in raw:
            if isinstance(item, str):
                trimmed = item.strip()
                if not trimmed:
                    continue
                coerced.append({"text": trimmed, "source": "live_debrief"})
            elif isinstance(item, dict):
                entry = dict(item)
                entry.setdefault("source", "live_debrief")
                coerced.append(entry)
            else:
                logger.debug(
                    "adapters: skipping non-str/non-dict item in %s",
                    field_name,
                )
        return coerced

    # ── Dict (single object masquerading as a structured item) ───────────
    if isinstance(raw, dict):
        entry = dict(raw)
        entry.setdefault("source", "live_debrief")
        return [entry]

    # ── Unknown type ─────────────────────────────────────────────────────
    logger.warning(
        "adapters: unexpected type %s for field %s — returning empty list",
        type(raw).__name__,
        field_name,
    )
    return []


# ── Public adapter ───────────────────────────────────────────────────────────


def _debrief_artifact_to_session_review(payload: dict[str, Any]) -> dict[str, Any]:
    """Convert a raw ``live_debrief`` artifact ``payload`` into a SessionReview.

    Never passes raw strings/objects directly to the UI — every content
    field is coerced through :func:`_coerce_review_field`.
    """
    result: dict[str, Any] = {}

    # Copy session-level scalar fields when present.
    for key in (
        "session_id",
        "bot_id",
        "status",
        "started_at",
        "ended_at",
        "prep_summary",
    ):
        if key in payload:
            result[key] = payload[key]

    # Coerce the four content fields.
    for field in _SESSION_REVIEW_FIELDS:
        result[field] = _coerce_review_field(
            payload.get(field),
            field_name=field,
        )

    # Derive is_empty from content.
    result["is_empty"] = not any(
        result.get(f) for f in _SESSION_REVIEW_FIELDS
    )

    # Forward additive fields (debrief_pending, debrief_failed,
    # live_debrief, review_summary) already in the payload.
    for key in (
        "debrief_pending",
        "debrief_failed",
        "live_debrief",
        "review_summary",
    ):
        if key in payload:
            result[key] = payload[key]

    return result
