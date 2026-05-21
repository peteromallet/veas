"""Centralized live-metrics structured-logging helpers (Sprint 5 T10).

Every live lifecycle event (prep start/success/failure/retry, debrief
start/success/failure/retry) should route through the helpers here so that
(e.g. Datadog, CloudWatch, grep) can extract the same fields from every
emission.

Usage::

    from app.services.live.metrics import (
        log_prep_start,
        log_prep_success,
        log_prep_failure,
        log_prep_retry_result,
        log_debrief_start,
        log_debrief_success,
        log_debrief_failure,
        log_debrief_retry_result,
    )

All helpers accept a ``logger`` kwarg (defaults to the module-level logger)
and emit at INFO (success), WARNING (failure), or DEBUG (start).
"""

from __future__ import annotations

import logging
from typing import Any

_log = logging.getLogger(__name__)

# ── Public helpers ───────────────────────────────────────────────────────────


def log_prep_start(
    conversation_id: str,
    bot_id: str,
    user_id: str,
    *,
    status_transition: str = "preparing->(pending)",
    retry_count: int = 0,
    logger: logging.Logger | None = None,
) -> None:
    (logger or _log).info(
        "live_prep: start conversation_id=%s bot_id=%s user_id=%s "
        "status_transition=%s retry_count=%d",
        conversation_id,
        bot_id,
        user_id,
        status_transition,
        retry_count,
    )


def log_prep_success(
    conversation_id: str,
    bot_id: str,
    *,
    duration_s: float,
    tool_count: int,
    status_transition: str = "preparing->ready",
    artifact_revision: str = "latest",
    logger: logging.Logger | None = None,
) -> None:
    (logger or _log).info(
        "live_prep: success conversation_id=%s bot_id=%s "
        "duration=%.3f tool_count=%d "
        "status_transition=%s artifact_revision=%s",
        conversation_id,
        bot_id,
        duration_s,
        tool_count,
        status_transition,
        artifact_revision,
    )


def log_prep_failure(
    conversation_id: str,
    bot_id: str,
    *,
    duration_s: float,
    tool_count: int,
    failure_reason: str,
    failure_class: str = "prep_failed",
    status_transition: str = "preparing->prep_failed",
    logger: logging.Logger | None = None,
) -> None:
    (logger or _log).warning(
        "live_prep: failure conversation_id=%s bot_id=%s "
        "duration=%.3f tool_count=%d failure_reason=%s "
        "failure_class=%s status_transition=%s",
        conversation_id,
        bot_id,
        duration_s,
        tool_count,
        failure_reason,
        failure_class,
        status_transition,
    )


def log_prep_retry_result(
    conversation_id: str,
    bot_id: str,
    *,
    retry_number: int,
    success: bool,
    duration_s: float,
    tool_count: int,
    failure_reason: str | None = None,
    logger: logging.Logger | None = None,
) -> None:
    if success:
        (logger or _log).info(
            "live_prep_retry: succeeded retry #%d for conversation_id=%s "
            "bot_id=%s duration=%.3f tool_count=%d "
            "status_transition=prep_failed->ready",
            retry_number,
            conversation_id,
            bot_id,
            duration_s,
            tool_count,
        )
    else:
        (logger or _log).warning(
            "live_prep_retry: failed retry #%d for conversation_id=%s "
            "bot_id=%s duration=%.3f tool_count=%d failure_reason=%s "
            "failure_class=prep_failed status_transition=prep_failed->prep_failed",
            retry_number,
            conversation_id,
            bot_id,
            duration_s,
            tool_count,
            failure_reason or "unknown",
        )


def log_debrief_start(
    conversation_id: str,
    bot_id: str,
    *,
    logger: logging.Logger | None = None,
) -> None:
    (logger or _log).info(
        "live_debrief: start conversation_id=%s bot_id=%s",
        conversation_id,
        bot_id,
    )


def log_debrief_success(
    conversation_id: str,
    bot_id: str,
    *,
    duration_s: float,
    tool_count: int,
    durable_write_count: int = 0,
    status_transition: str = "debriefing->review_pending",
    artifact_revision: str = "latest",
    logger: logging.Logger | None = None,
) -> None:
    (logger or _log).info(
        "live_debrief: success conversation_id=%s bot_id=%s "
        "duration=%.3f tool_count=%d durable_write_count=%d "
        "status_transition=%s artifact_revision=%s",
        conversation_id,
        bot_id,
        duration_s,
        tool_count,
        durable_write_count,
        status_transition,
        artifact_revision,
    )


def log_debrief_failure(
    conversation_id: str,
    bot_id: str,
    *,
    duration_s: float,
    tool_count: int,
    failure_reason: str,
    submit_missing: bool = False,
    failure_class: str = "infra_bug",
    durable_write_count: int = 0,
    status_transition: str = "debriefing->debrief_failed",
    logger: logging.Logger | None = None,
) -> None:
    (logger or _log).warning(
        "live_debrief: failure conversation_id=%s bot_id=%s "
        "duration=%.3f tool_count=%d failure_reason=%s "
        "submit_missing=%s failure_class=%s "
        "durable_write_count=%d status_transition=%s",
        conversation_id,
        bot_id,
        duration_s,
        tool_count,
        failure_reason,
        submit_missing,
        failure_class,
        durable_write_count,
        status_transition,
    )


def log_debrief_retry_result(
    conversation_id: str,
    bot_id: str,
    *,
    retry_number: int,
    success: bool,
    duration_s: float,
    tool_count: int,
    failure_reason: str | None = None,
    durable_write_count: int = 0,
    logger: logging.Logger | None = None,
) -> None:
    if success:
        (logger or _log).info(
            "live_debrief_retry: succeeded retry #%d for conversation_id=%s "
            "bot_id=%s duration=%.3f tool_count=%d "
            "status_transition=debrief_failed->review_pending",
            retry_number,
            conversation_id,
            bot_id,
            duration_s,
            tool_count,
        )
    else:
        (logger or _log).warning(
            "live_debrief_retry: failed retry #%d for conversation_id=%s "
            "bot_id=%s duration=%.3f tool_count=%d failure_reason=%s "
            "failure_class=debrief_failed durable_write_count=%d "
            "status_transition=debrief_failed->debrief_failed",
            retry_number,
            conversation_id,
            bot_id,
            duration_s,
            tool_count,
            failure_reason or "unknown",
            durable_write_count,
        )
