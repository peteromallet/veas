from __future__ import annotations

from datetime import UTC, date, datetime

from app.services.time_context import local_day_bounds_utc, temporal_reference


def test_temporal_reference_prefers_local_relative_display_for_berlin_today():
    now = datetime(2026, 5, 6, 19, 3, tzinfo=UTC)
    ref = temporal_reference(datetime(2026, 5, 6, 19, 0, tzinfo=UTC), "Europe/Berlin", now=now)

    assert ref is not None
    assert ref["local_day_label"] == "today"
    assert ref["relative_to_now"] == "about 3 minutes ago"
    assert ref["display"] == "today 21:00 Berlin"
    assert ref["utc"] == "2026-05-06T19:00:00+00:00"
    assert ref["local"].startswith("2026-05-06T21:00:00+02:00")


def test_temporal_reference_labels_yesterday_and_older_days():
    now = datetime(2026, 5, 6, 10, 0, tzinfo=UTC)

    yesterday = temporal_reference(datetime(2026, 5, 5, 9, 14, tzinfo=UTC), "Europe/Berlin", now=now)
    older = temporal_reference(datetime(2026, 5, 3, 16, 20, tzinfo=UTC), "Europe/Berlin", now=now)

    assert yesterday is not None and yesterday["display"] == "yesterday 11:14 Berlin"
    assert older is not None and older["display"] == "3 days ago 18:20 Berlin"


def test_temporal_reference_handles_future_times():
    now = datetime(2026, 5, 6, 10, 0, tzinfo=UTC)
    ref = temporal_reference(datetime(2026, 5, 6, 12, 0, tzinfo=UTC), "Europe/Berlin", now=now)

    assert ref is not None
    assert ref["relative_to_now"] == "in about 2 hours"
    assert ref["display"] == "today 14:00 Berlin"


def test_local_day_bounds_use_berlin_calendar_day_at_midnight_boundary():
    now = datetime(2026, 5, 6, 22, 30, tzinfo=UTC)

    today_start, today_end = local_day_bounds_utc("today", "Europe/Berlin", now=now)
    dated_start, dated_end = local_day_bounds_utc(date(2026, 5, 7), "Europe/Berlin", now=now)

    assert today_start == datetime(2026, 5, 6, 22, 0, tzinfo=UTC)
    assert today_end == datetime(2026, 5, 7, 22, 0, tzinfo=UTC)
    assert (dated_start, dated_end) == (today_start, today_end)

