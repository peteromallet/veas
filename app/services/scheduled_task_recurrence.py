"""Recurrence helpers for agent-managed scheduled tasks."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any


class RecurrenceError(ValueError):
    """Raised when a scheduled-task recurrence rule is malformed."""


def _aware_datetime(value: Any, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise RecurrenceError(f"{field} must be an ISO datetime") from exc
    else:
        raise RecurrenceError(f"{field} must be an aware datetime")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RecurrenceError(f"{field} must include a timezone")
    return parsed.astimezone(UTC)


def _positive_int(value: Any, field: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise RecurrenceError(f"{field} must be an integer") from exc
    if parsed < 1:
        raise RecurrenceError(f"{field} must be at least 1")
    return parsed


def _nonnegative_int(value: Any, field: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise RecurrenceError(f"{field} must be an integer") from exc
    if parsed < 0:
        raise RecurrenceError(f"{field} must be at least 0")
    return parsed


def normalize_recurrence(recurrence: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Return a canonical v1 recurrence rule, or None for one-shot schedules."""

    if recurrence is None:
        return None
    if not isinstance(recurrence, Mapping):
        raise RecurrenceError("recurrence must be an object")

    kind = str(recurrence.get("type") or recurrence.get("frequency") or "one_shot").strip().lower()
    if kind in {"", "none", "null", "one_shot", "once"}:
        return None
    if kind not in {"hourly", "daily", "weekly"}:
        raise RecurrenceError("recurrence.type must be hourly, daily, or weekly")

    normalized: dict[str, Any] = {
        "version": 1,
        "type": kind,
        "interval": _positive_int(recurrence.get("interval", 1), "recurrence.interval"),
    }
    if recurrence.get("cancelled") or recurrence.get("canceled"):
        normalized["cancelled"] = True
    if "until" in recurrence and recurrence["until"] is not None:
        normalized["until"] = _aware_datetime(recurrence["until"], "recurrence.until").isoformat()
    if "remaining_occurrences" in recurrence and recurrence["remaining_occurrences"] is not None:
        normalized["remaining_occurrences"] = _nonnegative_int(
            recurrence["remaining_occurrences"],
            "recurrence.remaining_occurrences",
        )

    if kind == "weekly":
        weekdays_value = recurrence.get("weekdays")
        if not isinstance(weekdays_value, list) or not weekdays_value:
            raise RecurrenceError("recurrence.weekdays must be a non-empty list for weekly rules")
        weekdays = sorted({_nonnegative_int(day, "recurrence.weekdays") for day in weekdays_value})
        if any(day > 6 for day in weekdays):
            raise RecurrenceError("recurrence.weekdays values must be between 0 and 6")
        normalized["weekdays"] = weekdays

    return normalized


def next_occurrence_utc(current_occurrence: datetime, recurrence: Mapping[str, Any] | None) -> datetime | None:
    """Calculate the next UTC occurrence after the current due time."""

    normalized = normalize_recurrence(recurrence)
    if normalized is None or normalized.get("cancelled"):
        return None
    if normalized.get("remaining_occurrences") is not None and normalized["remaining_occurrences"] <= 1:
        return None

    current = _aware_datetime(current_occurrence, "current_occurrence")
    interval = normalized["interval"]
    if normalized["type"] == "hourly":
        candidate = current + timedelta(hours=interval)
    elif normalized["type"] == "daily":
        candidate = current + timedelta(days=interval)
    else:
        candidate = _next_weekly_occurrence(current, normalized["weekdays"], interval)

    until = normalized.get("until")
    if until is not None and candidate > _aware_datetime(until, "recurrence.until"):
        return None
    return candidate


def recurrence_after_fire(recurrence: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Return the recurrence payload to persist for the next occurrence."""

    normalized = normalize_recurrence(recurrence)
    if normalized is None or normalized.get("cancelled"):
        return None
    remaining = normalized.get("remaining_occurrences")
    if remaining is None:
        return normalized
    if remaining <= 1:
        return None
    updated = dict(normalized)
    updated["remaining_occurrences"] = remaining - 1
    return updated


def _next_weekly_occurrence(current: datetime, weekdays: list[int], interval: int) -> datetime:
    week_start = current - timedelta(days=current.weekday())
    for day_offset in range(1, (interval + 1) * 7 + 1):
        candidate = current + timedelta(days=day_offset)
        week_delta = (candidate.date() - week_start.date()).days // 7
        if candidate.weekday() in weekdays and week_delta % interval == 0:
            return candidate
    raise RecurrenceError("could not calculate next weekly occurrence")
