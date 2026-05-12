"""Shared local/relative time formatting for agent context."""

from __future__ import annotations

import calendar
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


LocalDayValue = Literal["today", "yesterday"] | date


def timezone_or_utc(value: Any) -> ZoneInfo:
    try:
        return ZoneInfo(str(value or "UTC"))
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def normalize_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def timezone_label(timezone_name: str | None) -> str:
    if not timezone_name or timezone_name == "UTC":
        return "UTC"
    return str(timezone_name).split("/")[-1].replace("_", " ")


def _local_day_label(target: date, now_local: datetime) -> str:
    delta_days = (target - now_local.date()).days
    if delta_days == 0:
        return "today"
    if delta_days == -1:
        return "yesterday"
    if delta_days == 1:
        return "tomorrow"
    if -6 <= delta_days < -1:
        return f"{abs(delta_days)} days ago"
    if 1 < delta_days <= 6:
        return f"in {delta_days} days"
    return target.isoformat()


def _relative_to_now(value_utc: datetime, now_utc: datetime) -> str:
    seconds = int((value_utc - now_utc).total_seconds())
    if abs(seconds) < 45:
        return "just now"
    future = seconds > 0
    seconds = abs(seconds)
    if seconds < 90:
        unit = "minute"
        amount = 1
    elif seconds < 45 * 60:
        unit = "minutes"
        amount = round(seconds / 60)
    elif seconds < 90 * 60:
        unit = "hour"
        amount = 1
    elif seconds < 36 * 60 * 60:
        unit = "hours"
        amount = round(seconds / 3600)
    elif seconds < 72 * 60 * 60:
        unit = "days"
        amount = round(seconds / 86400)
    else:
        unit = "days"
        amount = seconds // 86400
    phrase = f"about {amount} {unit}" if unit in {"minutes", "hours"} else f"{amount} {unit}"
    if amount == 1 and unit.endswith("s"):
        phrase = phrase[:-1]
    return f"in {phrase}" if future else f"{phrase} ago"


def temporal_reference(value: datetime | None, timezone_name: str | None, *, now: datetime | None = None) -> dict[str, str] | None:
    if value is None:
        return None
    now_utc = normalize_utc(now or datetime.now(UTC))
    value_utc = normalize_utc(value)
    tz = timezone_or_utc(timezone_name)
    local = value_utc.astimezone(tz)
    now_local = now_utc.astimezone(tz)
    day_label = _local_day_label(local.date(), now_local)
    label = timezone_label(timezone_name)
    local_time = local.strftime("%H:%M")
    display_prefix = day_label if day_label != local.date().isoformat() else local.strftime("%Y-%m-%d")
    return {
        "utc": value_utc.isoformat(),
        "local": local.isoformat(),
        "timezone": timezone_name or "UTC",
        "local_date": local.date().isoformat(),
        "local_time": local_time,
        "local_weekday": local.strftime("%A"),
        "local_day_label": day_label,
        "relative_to_now": _relative_to_now(value_utc, now_utc),
        "display": f"{display_prefix} {local_time} {label}",
    }


def add_calendar_months(value: date | datetime, months: int) -> date | datetime:
    """Add calendar months, clamping to the last valid day when needed."""

    if months < 0:
        raise ValueError("months must be non-negative")
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def local_day_bounds_utc(
    local_day: LocalDayValue,
    timezone_name: str | None,
    *,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    tz = timezone_or_utc(timezone_name)
    now_local = normalize_utc(now or datetime.now(UTC)).astimezone(tz)
    if local_day == "today":
        target = now_local.date()
    elif local_day == "yesterday":
        target = now_local.date() - timedelta(days=1)
    elif isinstance(local_day, date):
        target = local_day
    else:
        raise ValueError("local_day must be 'today', 'yesterday', or a date")
    start_local = datetime.combine(target, time.min, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)
