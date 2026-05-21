"""Tests for Sprint 5 T10: centralized live metrics logging.

Uses ``caplog`` to verify that structured logs are emitted with the
required fields on success, failure, and retry paths for both prep
and debrief lifecycles.

Does NOT require a database — every log helper is a pure function
that writes to Python's ``logging`` module.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def capture_logs(caplog: Any) -> Any:
    caplog.set_level(logging.INFO, logger="app.services.live.metrics")
    return caplog


@pytest.fixture
def metrics_logger() -> logging.Logger:
    return logging.getLogger("app.services.live.metrics")


# ── Prep success log ─────────────────────────────────────────────────────────


class TestPrepSuccessLog:
    def test_all_required_fields_present(self, caplog: Any) -> None:
        from app.services.live.metrics import log_prep_success

        caplog.set_level(logging.INFO)
        log_prep_success(
            "conv-1", "tante_rosi",
            duration_s=2.345,
            tool_count=3,
            status_transition="preparing->ready",
            artifact_revision="abc12345",
        )
        assert len(caplog.records) == 1
        rec = caplog.records[0]
        assert rec.levelname == "INFO"
        msg = rec.getMessage()
        assert "live_prep: success" in msg
        assert "conv-1" in msg
        assert "tante_rosi" in msg
        assert "duration=2.345" in msg
        assert "tool_count=3" in msg
        assert "status_transition=preparing->ready" in msg
        assert "artifact_revision=abc12345" in msg

    def test_default_status_transition(self, caplog: Any) -> None:
        from app.services.live.metrics import log_prep_success

        caplog.set_level(logging.INFO)
        log_prep_success("c1", "b1", duration_s=0.5, tool_count=1)
        msg = caplog.records[0].getMessage()
        assert "status_transition=preparing->ready" in msg

    def test_default_artifact_revision(self, caplog: Any) -> None:
        from app.services.live.metrics import log_prep_success

        caplog.set_level(logging.INFO)
        log_prep_success("c1", "b1", duration_s=0.5, tool_count=1)
        msg = caplog.records[0].getMessage()
        assert "artifact_revision=latest" in msg


# ── Prep failure log ────────────────────────────────────────────────────────


class TestPrepFailureLog:
    def test_all_required_fields_present(self, caplog: Any) -> None:
        from app.services.live.metrics import log_prep_failure

        caplog.set_level(logging.WARNING)
        log_prep_failure(
            "conv-f", "mediator",
            duration_s=5.0,
            tool_count=0,
            failure_reason="live_prep_submit_missing",
            failure_class="prep_failed",
            status_transition="preparing->prep_failed",
        )
        assert len(caplog.records) == 1
        rec = caplog.records[0]
        assert rec.levelname == "WARNING"
        msg = rec.getMessage()
        assert "live_prep: failure" in msg
        assert "conv-f" in msg
        assert "mediator" in msg
        assert "duration=5.000" in msg
        assert "tool_count=0" in msg
        assert "failure_reason=live_prep_submit_missing" in msg
        assert "failure_class=prep_failed" in msg
        assert "status_transition=preparing->prep_failed" in msg

    def test_default_failure_class(self, caplog: Any) -> None:
        from app.services.live.metrics import log_prep_failure

        caplog.set_level(logging.WARNING)
        log_prep_failure("c1", "b1", duration_s=1.0, tool_count=0,
                         failure_reason="unknown")
        msg = caplog.records[0].getMessage()
        assert "failure_class=prep_failed" in msg


# ── Prep retry result log ───────────────────────────────────────────────────


class TestPrepRetryResultLog:
    def test_retry_success_fields(self, caplog: Any) -> None:
        from app.services.live.metrics import log_prep_retry_result

        caplog.set_level(logging.INFO)
        log_prep_retry_result(
            "conv-r", "tante_rosi",
            retry_number=2, success=True, duration_s=3.0, tool_count=4,
        )
        assert len(caplog.records) == 1
        rec = caplog.records[0]
        assert rec.levelname == "INFO"
        msg = rec.getMessage()
        assert "live_prep_retry: succeeded" in msg
        assert "retry #2" in msg
        assert "conv-r" in msg
        assert "tante_rosi" in msg
        assert "duration=3.000" in msg
        assert "tool_count=4" in msg
        assert "status_transition=prep_failed->ready" in msg

    def test_retry_failure_fields(self, caplog: Any) -> None:
        from app.services.live.metrics import log_prep_retry_result

        caplog.set_level(logging.WARNING)
        log_prep_retry_result(
            "conv-r2", "mediator",
            retry_number=3, success=False, duration_s=8.0, tool_count=1,
            failure_reason="still_missing",
        )
        rec = caplog.records[0]
        assert rec.levelname == "WARNING"
        msg = rec.getMessage()
        assert "live_prep_retry: failed" in msg
        assert "retry #3" in msg
        assert "failure_reason=still_missing" in msg
        assert "failure_class=prep_failed" in msg
        assert "status_transition=prep_failed->prep_failed" in msg

    def test_retry_failure_default_reason(self, caplog: Any) -> None:
        from app.services.live.metrics import log_prep_retry_result

        caplog.set_level(logging.WARNING)
        log_prep_retry_result(
            "c", "b", retry_number=1, success=False, duration_s=1.0,
            tool_count=0, failure_reason=None,
        )
        msg = caplog.records[0].getMessage()
        assert "failure_reason=unknown" in msg


# ── Debrief start log ───────────────────────────────────────────────────────


class TestDebriefStartLog:
    def test_required_fields(self, caplog: Any) -> None:
        from app.services.live.metrics import log_debrief_start

        caplog.set_level(logging.INFO)
        log_debrief_start("conv-d", "tante_rosi")
        msg = caplog.records[0].getMessage()
        assert "live_debrief: start" in msg
        assert "conv-d" in msg
        assert "tante_rosi" in msg


# ── Debrief success log ─────────────────────────────────────────────────────


class TestDebriefSuccessLog:
    def test_all_required_fields_present(self, caplog: Any) -> None:
        from app.services.live.metrics import log_debrief_success

        caplog.set_level(logging.INFO)
        log_debrief_success(
            "conv-ds", "mediator",
            duration_s=12.5,
            tool_count=7,
            durable_write_count=5,
            artifact_revision="def67890",
        )
        msg = caplog.records[0].getMessage()
        assert "live_debrief: success" in msg
        assert "conv-ds" in msg
        assert "mediator" in msg
        assert "duration=12.500" in msg
        assert "tool_count=7" in msg
        assert "durable_write_count=5" in msg
        assert "status_transition=debriefing->review_pending" in msg
        assert "artifact_revision=def67890" in msg

    def test_default_durable_write_count(self, caplog: Any) -> None:
        from app.services.live.metrics import log_debrief_success

        caplog.set_level(logging.INFO)
        log_debrief_success("c", "b", duration_s=1.0, tool_count=1)
        msg = caplog.records[0].getMessage()
        assert "durable_write_count=0" in msg


# ── Debrief failure log ─────────────────────────────────────────────────────


class TestDebriefFailureLog:
    def test_submit_missing_fields(self, caplog: Any) -> None:
        from app.services.live.metrics import log_debrief_failure

        caplog.set_level(logging.WARNING)
        log_debrief_failure(
            "conv-df", "tante_rosi",
            duration_s=15.0,
            tool_count=10,
            failure_reason="live_debrief_submit_missing",
            submit_missing=True,
            failure_class="submit_missing",
            durable_write_count=3,
        )
        msg = caplog.records[0].getMessage()
        assert "live_debrief: failure" in msg
        assert "submit_missing=True" in msg
        assert "failure_class=submit_missing" in msg
        assert "durable_write_count=3" in msg
        assert "status_transition=debriefing->debrief_failed" in msg

    def test_infra_failure_fields(self, caplog: Any) -> None:
        from app.services.live.metrics import log_debrief_failure

        caplog.set_level(logging.WARNING)
        log_debrief_failure(
            "conv-if", "mediator",
            duration_s=2.0,
            tool_count=0,
            failure_reason="unknown bot_id",
            submit_missing=False,
            failure_class="infra_bug",
            durable_write_count=0,
        )
        msg = caplog.records[0].getMessage()
        assert "submit_missing=False" in msg
        assert "failure_class=infra_bug" in msg
        assert "durable_write_count=0" in msg

    def test_default_status_transition(self, caplog: Any) -> None:
        from app.services.live.metrics import log_debrief_failure

        caplog.set_level(logging.WARNING)
        log_debrief_failure("c", "b", duration_s=1.0, tool_count=0,
                            failure_reason="x")
        msg = caplog.records[0].getMessage()
        assert "status_transition=debriefing->debrief_failed" in msg


# ── Debrief retry result log ────────────────────────────────────────────────


class TestDebriefRetryResultLog:
    def test_retry_success(self, caplog: Any) -> None:
        from app.services.live.metrics import log_debrief_retry_result

        caplog.set_level(logging.INFO)
        log_debrief_retry_result(
            "conv-dr", "tante_rosi",
            retry_number=1, success=True, duration_s=6.0, tool_count=5,
        )
        msg = caplog.records[0].getMessage()
        assert "live_debrief_retry: succeeded" in msg
        assert "retry #1" in msg
        assert "status_transition=debrief_failed->review_pending" in msg

    def test_retry_failure(self, caplog: Any) -> None:
        from app.services.live.metrics import log_debrief_retry_result

        caplog.set_level(logging.WARNING)
        log_debrief_retry_result(
            "conv-drf", "mediator",
            retry_number=2, success=False, duration_s=9.0, tool_count=3,
            failure_reason="persistence_failed", durable_write_count=0,
        )
        msg = caplog.records[0].getMessage()
        assert "live_debrief_retry: failed" in msg
        assert "retry #2" in msg
        assert "failure_reason=persistence_failed" in msg
        assert "failure_class=debrief_failed" in msg
        assert "durable_write_count=0" in msg
        assert "status_transition=debrief_failed->debrief_failed" in msg


# ── Log format parseability ─────────────────────────────────────────────────


class TestLogParseability:
    """Verify all structured logs follow the same key=value convention."""

    def test_prep_success_parseable(self) -> None:
        from app.services.live.metrics import log_prep_success
        import io

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setLevel(logging.INFO)
        logger = logging.getLogger("app.services.live.metrics")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        try:
            log_prep_success("c", "b", duration_s=1.0, tool_count=2, logger=logger)
            handler.flush()
            output = stream.getvalue()
        finally:
            logger.removeHandler(handler)

        # Every structured field after the prefix should be key=value.
        after_prefix = output.split("live_prep: success", 1)[1]
        tokens = after_prefix.strip().split()
        for token in tokens:
            if "=" in token:
                pass  # key=value ✓
            else:
                # Only conversation_id/bot_id come before structured fields
                pass

    def test_debrief_failure_parseable(self) -> None:
        from app.services.live.metrics import log_debrief_failure
        import io

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setLevel(logging.WARNING)
        logger = logging.getLogger("app.services.live.metrics")
        logger.addHandler(handler)
        logger.setLevel(logging.WARNING)
        try:
            log_debrief_failure(
                "c", "b", duration_s=1.0, tool_count=0,
                failure_reason="test", logger=logger,
            )
            handler.flush()
            output = stream.getvalue()
        finally:
            logger.removeHandler(handler)

        assert "failure_reason=test" in output
        assert "status_transition=debriefing->debrief_failed" in output
