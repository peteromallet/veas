"""Advisory INSERT linter tests.

Verifies:
- lint_inserts.py parses with --help
- Scans scope-stamp tables and artifact tables
- Output format is path:line: kind: message
- Detects violations in fixture strings
- Always exits 0 in S2a
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import pytest


LINT_SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts", "lint_inserts.py")


class TestLintScriptHelp:
    """--help parses correctly."""

    def test_help_parses(self):
        """python scripts/lint_inserts.py --help exits 0 and prints usage."""
        result = subprocess.run(
            [sys.executable, LINT_SCRIPT, "--help"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "usage:" in result.stdout.lower() or "usage:" in result.stderr.lower(), (
            f"Help output must contain 'usage:', got stdout={result.stdout}, stderr={result.stderr}"
        )


class TestLintScopeStamp:
    """Detects missing bot_id/topic_id in scope-stamp tables."""

    def test_detects_missing_bot_id(self, tmp_path):
        """Flags INSERT INTO messages without bot_id."""
        test_file = tmp_path / "test_file.py"
        test_file.write_text(textwrap.dedent("""
            INSERT_STMT = \"\"\"
            INSERT INTO messages (direction, sender_id, content)
            VALUES ('inbound', $1, $2)
            \"\"\"
        """))

        result = subprocess.run(
            [sys.executable, LINT_SCRIPT, "--dir", str(tmp_path)],
            capture_output=True, text=True,
        )
        output = result.stderr
        assert "bot_id" in output, f"Should flag missing bot_id, got: {output}"

    def test_detects_missing_topic_id(self, tmp_path):
        """Flags INSERT INTO bot_turns without topic_id."""
        test_file = tmp_path / "test_file.py"
        test_file.write_text(textwrap.dedent("""
            INSERT_STMT = \"\"\"
            INSERT INTO bot_turns (bot_id, triggered_by_message_id)
            VALUES ($1, $2)
            \"\"\"
        """))

        result = subprocess.run(
            [sys.executable, LINT_SCRIPT, "--dir", str(tmp_path)],
            capture_output=True, text=True,
        )
        output = result.stderr
        # Should flag missing topic_id
        assert "topic_id" in output, f"Should flag missing topic_id, got: {output}"

    def test_clean_insert_passes(self, tmp_path):
        """Properly stamped INSERT produces no violations."""
        test_file = tmp_path / "test_file.py"
        test_file.write_text(textwrap.dedent("""
            INSERT_STMT = \"\"\"
            INSERT INTO messages (direction, sender_id, content, bot_id, topic_id)
            VALUES ('inbound', $1, $2, $3, $4)
            \"\"\"
        """))

        result = subprocess.run(
            [sys.executable, LINT_SCRIPT, "--dir", str(tmp_path)],
            capture_output=True, text=True,
        )
        output = result.stderr
        assert "no violations found" in output, f"Should find no violations, got: {output}"


class TestLintArtifactCoverage:
    """Detects missing artifact_topics in artifact table INSERTs."""

    def test_detects_missing_artifact_topics(self, tmp_path):
        """Flags INSERT INTO memories without artifact_topics."""
        test_file = tmp_path / "test_file.py"
        test_file.write_text(textwrap.dedent("""
            INSERT_STMT = \"\"\"
            INSERT INTO memories (about_user_id, content)
            VALUES ($1, $2)
            \"\"\"
        """))

        result = subprocess.run(
            [sys.executable, LINT_SCRIPT, "--dir", str(tmp_path)],
            capture_output=True, text=True,
        )
        output = result.stderr
        assert "artifact_topics" in output, f"Should flag missing artifact_topics, got: {output}"

    def test_paired_insert_passes(self, tmp_path):
        """INSERT with artifact_topics in same string produces no violation."""
        test_file = tmp_path / "test_file.py"
        test_file.write_text(textwrap.dedent("""
            INSERT_STMT = \"\"\"
            WITH new_artifact AS (
                INSERT INTO observations (content, recorded_by_bot_id)
                VALUES ($1, $2) RETURNING id
            ), topic_link AS (
                INSERT INTO artifact_topics (artifact_table, artifact_id, topic_id, tagged_by_bot_id, status)
                SELECT 'observations', new_artifact.id, $3, $2, 'active' FROM new_artifact
            )
            SELECT id FROM new_artifact
            \"\"\"
        """))

        result = subprocess.run(
            [sys.executable, LINT_SCRIPT, "--dir", str(tmp_path)],
            capture_output=True, text=True,
        )
        output = result.stderr
        assert "no violations found" in output, f"Should find no violations, got: {output}"


class TestLintOutputFormat:
    """Output format is path:line: kind: message."""

    def test_output_format(self, tmp_path):
        """Violation output matches path:line: kind: message format."""
        test_file = tmp_path / "test_file.py"
        test_file.write_text(textwrap.dedent("""
            INSERT_STMT = \"\"\"
            INSERT INTO feedback (from_user_id, sentiment, content)
            VALUES ($1, $2, $3)
            \"\"\"
        """))

        result = subprocess.run(
            [sys.executable, LINT_SCRIPT, "--dir", str(tmp_path)],
            capture_output=True, text=True,
        )
        output = result.stderr
        lines = [l for l in output.splitlines() if "scope_stamp" in l]
        assert len(lines) > 0, f"Expected at least one violation, got: {output}"
        for line in lines:
            # Format: path:line: kind: message
            parts = line.split(":", 2)
            assert len(parts) >= 3, f"Expected path:line: kind: message, got: {line}"


class TestLintAlwaysExitsZero:
    """Exit code is always 0 in S2a."""

    def test_exits_zero_with_violations(self, tmp_path):
        """Even with violations, exits 0 in S2a (advisory)."""
        test_file = tmp_path / "test_file.py"
        test_file.write_text('INSERT_STMT = "INSERT INTO messages (direction) VALUES (1)"')

        result = subprocess.run(
            [sys.executable, LINT_SCRIPT, "--dir", str(tmp_path)],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"Should exit 0 in S2a, got {result.returncode}"

    def test_exits_zero_clean(self, tmp_path):
        """Clean scan also exits 0."""
        result = subprocess.run(
            [sys.executable, LINT_SCRIPT, "--dir", str(tmp_path)],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"Should exit 0, got {result.returncode}"

    def test_todo_s2b_comment(self):
        """lint_inserts.py contains # TODO(S2b): make blocking."""
        content = open(LINT_SCRIPT).read()
        assert "TODO(S2b)" in content, (
            "lint_inserts.py must have TODO(S2b) marker for blocking mode"
        )


class TestAllArtifactTables:
    """All 6 artifact tables are checked."""

    @pytest.mark.parametrize("table", [
        "memories", "themes", "watch_items", "observations",
        "distillations", "out_of_bounds",
    ])
    def test_artifact_table_checked(self, table, tmp_path):
        """Each artifact table triggers a violation when missing artifact_topics."""
        test_file = tmp_path / "test_file.py"
        test_file.write_text(textwrap.dedent(f"""
            INSERT_STMT = \"\"\"
            INSERT INTO {table} (content)
            VALUES ($1)
            \"\"\"
        """))

        result = subprocess.run(
            [sys.executable, LINT_SCRIPT, "--dir", str(tmp_path)],
            capture_output=True, text=True,
        )
        output = result.stderr
        violations = [l for l in output.splitlines() if "artifact_coverage" in l]
        assert len(violations) >= 1, f"Expected violation for {table}, got: {output}"