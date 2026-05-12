"""Dual-key newer-inbound suppression tests.

Verifies:
- EXISTS query includes (bot_id = $ OR bot_id IS NULL) filter
- The IS NULL half catches legacy boundary rows
- Both agentic.py and read_tools.py suppression paths include the filter
"""

from __future__ import annotations

import pytest


class TestSuppressionDualKeyExists:
    """Verify the EXISTS subquery contains the dual-key filter."""

    def test_agentic_suppression_has_bot_id_filter(self):
        """agentic.py _newer_inbound_exists includes (bot_id = $4 OR bot_id IS NULL)."""
        content = open("app/services/agentic.py").read()
        assert "(bot_id = $4 OR bot_id IS NULL)" in content, (
            "agentic.py suppression query must filter by bot_id OR bot_id IS NULL"
        )

    def test_read_tools_suppression_has_bot_id_filter(self):
        """read_tools.py incremental-send suppression includes the filter."""
        content = open("app/services/tools/read_tools.py").read()
        assert "(bot_id = $4 OR bot_id IS NULL)" in content or "(bot_id = $ N OR bot_id IS NULL)" in content, (
            "read_tools.py suppression query must filter by bot_id OR bot_id IS NULL"
        )

    def test_suppression_accepts_bot_id_parameter(self):
        """_newer_inbound_exists in agentic.py accepts bot_id parameter."""
        content = open("app/services/agentic.py").read()
        assert "bot_id: str | None = None" in content, (
            "_newer_inbound_exists must accept bot_id parameter"
        )

    def test_call_sites_pass_bot_id(self):
        """Call sites of _newer_inbound_exists pass bot_id=ctx.bot_id."""
        content = open("app/services/agentic.py").read()
        # At least one call site passes bot_id
        assert "bot_id=ctx.bot_id" in content, (
            "call sites must pass bot_id=ctx.bot_id to _newer_inbound_exists"
        )


class TestLegacyBoundaryRows:
    """IS NULL catches legacy rows written before the stamping deploy."""

    def test_null_bot_id_is_caught(self):
        """Messages with bot_id=NULL must be caught by the IS NULL filter."""
        # This is a semantic test: the SQL filter (bot_id = $4 OR bot_id IS NULL)
        # ensures that rows inserted before the stamping deploy (NULL bot_id)
        # are still considered part of the suppression check.
        #
        # We verify this by checking the actual SQL pattern in the code.
        content = open("app/services/agentic.py").read()
        # The full pattern around the suppression query
        assert "bot_id IS NULL" in content, (
            "IS NULL is required to catch legacy boundary rows"
        )

    def test_both_bot_id_filter_arms_present(self):
        """The filter has both bot_id= and bot_id IS NULL arms."""
        content = open("app/services/agentic.py").read()
        # Should find both parts of the OR
        assert "bot_id =" in content and "bot_id IS NULL" in content, (
            "Both arms of the dual-key filter must be present"
        )