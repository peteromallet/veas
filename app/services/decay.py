"""Plan 5 decay housekeeping."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.services.scoring import RescoreReport, rescore_observations


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class DecayReport:
    themes_dormant: str
    themes_resolved_by_time: str
    observations_confidence_decayed: str
    observations_stale: str
    watch_items_expired: str
    rescore_report: RescoreReport


async def run_decay_housekeeping(
    pool: Any,
    *,
    now: datetime | None = None,
    scoring_client: Any | None = None,
) -> DecayReport:
    """Apply time-based decay and rescore stale observation scores.

    Theme and observation decay are driven by reinforcement age. Observation
    decay never consults theme/message activity timestamps.
    """

    now = now or _utc_now()
    themes_dormant = await pool.execute(
        """
        UPDATE themes
        SET status = 'dormant',
            updated_at = $1
        WHERE status = 'active'
          AND COALESCE(last_reinforced_at, first_seen_at) <= $1::timestamptz - interval '6 weeks'
        """,
        now,
    )
    themes_resolved = await pool.execute(
        """
        UPDATE themes
        SET status = 'resolved_by_time',
            updated_at = $1
        WHERE status = 'dormant'
          AND updated_at <= $1::timestamptz - interval '4 months'
        """,
        now,
    )
    observations_stale = await pool.execute(
        """
        UPDATE observations
        SET status = 'stale'
        WHERE status = 'active'
          AND COALESCE(last_reinforced_at, created_at) <= $1::timestamptz - interval '6 months'
        """,
        now,
    )
    observations_confidence = await pool.execute(
        """
        UPDATE observations
        SET confidence = CASE confidence
            WHEN 'high' THEN 'medium'
            WHEN 'medium' THEN 'low'
            ELSE confidence
        END
        WHERE status = 'active'
          AND COALESCE(last_reinforced_at, created_at) <= $1::timestamptz - interval '3 months'
          AND COALESCE(last_reinforced_at, created_at) > $1::timestamptz - interval '6 months'
          AND confidence IN ('high', 'medium')
        """,
        now,
    )
    watch_items_expired = await pool.execute(
        """
        UPDATE watch_items
        SET status = 'expired'
        WHERE status = 'open'
          AND due_at IS NOT NULL
          AND addressed_at IS NULL
          AND due_at <= $1::timestamptz - interval '30 days'
        """,
        now,
    )
    rescore_report = await rescore_observations(pool, client=scoring_client)
    return DecayReport(
        themes_dormant=themes_dormant,
        themes_resolved_by_time=themes_resolved,
        observations_confidence_decayed=observations_confidence,
        observations_stale=observations_stale,
        watch_items_expired=watch_items_expired,
        rescore_report=rescore_report,
    )
