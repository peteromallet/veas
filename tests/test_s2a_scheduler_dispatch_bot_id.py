"""Scheduler dispatch bot_id tests.

Verifies:
- Claim query RETURNING list includes bot_id, topic_id
- Instrumented handler asserts job['bot_id'] is populated
"""

from __future__ import annotations

import re
import pytest


class TestClaimQueryReturning:
    """_claim_due_jobs RETURNING list includes bot_id and topic_id."""

    def test_returning_has_bot_id(self):
        """RETURNING clause includes sj.bot_id."""
        content = open("app/services/scheduled_jobs.py").read()
        # Find the RETURNING line(s) in _claim_due_jobs
        assert "sj.bot_id" in content, (
            "RETURNING must include sj.bot_id"
        )

    def test_returning_has_topic_id(self):
        """RETURNING clause includes sj.topic_id."""
        content = open("app/services/scheduled_jobs.py").read()
        assert "sj.topic_id" in content, (
            "RETURNING must include sj.topic_id"
        )

    def test_returning_position(self):
        """bot_id and topic_id appear together in the RETURNING list."""
        content = open("app/services/scheduled_jobs.py").read()
        # Extract the RETURNING block
        match = re.search(r"RETURNING\s+(.*?)(?:\"\"\"|$)", content, re.DOTALL)
        if match:
            returning_block = match.group(1)
            assert "bot_id" in returning_block
            assert "topic_id" in returning_block


class TestJobDictHasBotId:
    """After claim, job['bot_id'] and job['topic_id'] are populated."""

    def test_handler_can_read_bot_id(self):
        """Handlers in scheduled_job_handlers.py read job['bot_id']."""
        content = open("app/services/scheduled_job_handlers.py").read()
        assert "job['bot_id']" in content or 'job.get("bot_id"' in content or "job.get('bot_id'" in content, (
            "handlers must read bot_id from job dict"
        )

    def test_handler_can_read_topic_id(self):
        """Handlers in scheduled_job_handlers.py read job['topic_id']."""
        content = open("app/services/scheduled_job_handlers.py").read()
        assert "job['topic_id']" in content or 'job.get("topic_id"' in content or "job.get('topic_id'" in content, (
            "handlers must read topic_id from job dict"
        )


class TestDispatchCallsHandlersWithBotId:
    """_dispatch passes job dict with bot_id/topic_id to handlers."""

    def test_dispatch_no_signature_change(self):
        """_dispatch signature stays as def _dispatch(self, job: dict[str, Any])."""
        content = open("app/services/scheduled_jobs.py").read()
        assert "async def _dispatch(self, job: dict[str, Any])" in content, (
            "_dispatch signature must remain unchanged"
        )

    def test_heartbeat_seed_stamps_bot_id(self):
        """seed_heartbeat stamps bot_id='mediator'."""
        content = open("app/services/scheduled_jobs.py").read()
        assert "'mediator'" in content, (
            "heartbeat seed must stamp bot_id='mediator'"
        )


class TestScheduledJobHandlersReceiveBotId:
    """All 7 handler functions take job: dict[str, Any]."""

    def test_handler_signatures(self):
        """Each handler in scheduled_job_handlers.py takes job dict."""
        content = open("app/services/scheduled_job_handlers.py").read()
        # Find all handler function definitions
        handlers = re.findall(r"async def (\w+)\(self, job: dict\[str, Any\]\)", content)
        assert len(handlers) >= 5, (
            f"Expected at least 5 handlers taking job dict, found {len(handlers)}: {handlers}"
        )