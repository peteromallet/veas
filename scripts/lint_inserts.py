#!/usr/bin/env python3
"""Advisory INSERT linter for S2a — flags missing scope/artifact-topic columns.

Walks ``app/`` files and scans SQL string literals for INSERT statements against
scope-stamp tables (messages, bot_turns, scheduled_jobs, feedback,
bridge_candidates) and artifact tables (memories, themes, observations,
watch_items, distillations, out_of_bounds).

Scope-stamp tables: flags INSERTs missing ``bot_id`` / ``topic_id`` (and
``dyad_id`` for ``bridge_candidates``).

Artifact tables: flags INSERTs lacking an ``INSERT INTO artifact_topics`` clause
in the same SQL string (substring check, not structural).

Exit code is *always* 0 in S2a (advisory-only).  S2b will make this blocking.

# TODO(S2b): make blocking — exit with non-zero when violations exist.

Usage:
    python scripts/lint_inserts.py           # scan app/ directory
    python scripts/lint_inserts.py --help    # show this message
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCOPE_STAMP_TABLES: dict[str, list[str]] = {
    "messages": ["bot_id", "topic_id"],
    "bot_turns": ["bot_id", "topic_id"],
    "scheduled_jobs": ["bot_id", "topic_id"],
    "feedback": ["bot_id", "topic_id"],
    "bridge_candidates": ["bot_id", "topic_id", "dyad_id"],
}

ARTIFACT_TABLES: set[str] = {
    "memories",
    "themes",
    "observations",
    "watch_items",
    "distillations",
    "out_of_bounds",
}

# Regex to find INSERT INTO <table> followed by an opening paren (column list).
# Group 1 = table name (without schema prefix).
_INSERT_RE = re.compile(
    r"""INSERT\s+INTO\s+                              # INSERT INTO
        (?:[\w.]+\.)?                                  # optional schema prefix
        (\w+)                                         # table name (captured)
        \s*\(                                          # opening paren of column list
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Regex to find INSERT INTO artifact_topics (any spacing).
_ARTIFACT_TOPICS_RE = re.compile(
    r"""INSERT\s+INTO\s+(?:[\w.]+\.)?artifact_topics""",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _extract_sql_strings(lines: list[str], target_line_idx: int) -> str | None:
    """Given a line index containing ``INSERT INTO``, try to find the enclosing
    SQL string literal.  Returns the full SQL text if found, else ``None``.

    Handles triple-quoted strings (both ``\"\"\"`` and ``'''``) and single-line
    strings.  For single-line strings, returns just that line's content.
    """
    # Scan backwards to find the opening quote of the enclosing string.
    idx = target_line_idx
    # First, check if the line itself starts a triple-quoted or heredoc string
    # by looking for opening delimiter on this or a prior line.
    opening_line = None
    delimiter: str | None = None

    # Walk backwards from target line to find opening triple-quote or single quote.
    for i in range(target_line_idx, -1, -1):
        line = lines[i]
        # Look for triple-quoted opening
        for delim in ('"""', "'''"):
            pos = line.find(delim)
            if pos != -1:
                # Make sure this isn't a closing delimiter on the same line.
                rest = line[pos + len(delim) :]
                if delim not in rest:  # not a """""" on same line
                    opening_line = i
                    delimiter = delim
                    break
        if delimiter:
            break

    if delimiter is None:
        # No triple-quote found — try single-line string detection.
        # Look for single/double quotes on the target line containing INSERT.
        line = lines[target_line_idx]
        # Simple heuristic: extract content between quotes on this line.
        for q in ('"""', "'''", '"', "'"):
            if q in line:
                # Try to extract content. For triple quotes on one line,
                # strip the quotes.
                if q in ('"""', "'''"):
                    # Triple quote on single line
                    start = line.find(q)
                    end = line.rfind(q)
                    if start != end:
                        content = line[start + len(q) : end]
                        return content
                else:
                    # Single quote — just grab between first and last on line.
                    start = line.find(q)
                    end = line.rfind(q)
                    if start != end:
                        content = line[start + 1 : end]
                        return content
        return None

    # We have a triple-quoted string starting at `opening_line`.
    # Collect lines until the closing delimiter.
    # Start from the opening delimiter position.
    opening_pos = lines[opening_line].find(delimiter) + len(delimiter)

    # Build the SQL content.
    parts: list[str] = []

    # First line (after the opening delimiter — may have text on same line).
    first_line = lines[opening_line][opening_pos:]
    # If the closing delimiter is on the same line, strip it.
    closing_pos = first_line.find(delimiter)
    if closing_pos != -1:
        parts.append(first_line[:closing_pos])
        return "\n".join(parts)

    parts.append(first_line)

    # Subsequent lines until the closing delimiter.
    for i in range(opening_line + 1, len(lines)):
        line = lines[i]
        closing_pos = line.find(delimiter)
        if closing_pos != -1:
            parts.append(line[:closing_pos])
            return "\n".join(parts)
        parts.append(line)

    # Unterminated string — return what we have (best-effort).
    return "\n".join(parts)


def _find_column_list(sql: str) -> str | None:
    """Extract the column-list portion of the first INSERT INTO in *sql*.

    Returns the text between the first ``(`` after ``INSERT INTO`` and its
    matching ``)``, or ``None`` if unparseable.
    """
    match = _INSERT_RE.search(sql)
    if not match:
        return None
    start = match.end() - 1  # position of the '('
    depth = 0
    for i, ch in enumerate(sql[start:], start=start):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return sql[start : i + 1]
    return None


def _check_scope_stamp(
    filepath: str, line_idx: int, table: str, sql: str
) -> list[str]:
    """Check a scope-stamp table INSERT.  Returns list of violation strings."""
    required = SCOPE_STAMP_TABLES.get(table)
    if required is None:
        return []  # not a scope-stamp table

    column_list = _find_column_list(sql)
    if column_list is None:
        # Can't parse — flag as a general warning.
        return [
            f"{filepath}:{line_idx + 1}: scope_stamp: could not parse column list for INSERT INTO {table}"
        ]

    violations: list[str] = []
    columns_lower = column_list.lower()
    for col in required:
        if f",{col}," not in f",{columns_lower[1:-1]},":
            # Check with word-boundary awareness — the column name should appear
            # as a standalone identifier in the column list.
            # Simple approach: split on commas and strip whitespace.
            cols = [c.strip().lower() for c in column_list[1:-1].split(",")]
            if col not in cols:
                violations.append(
                    f"{filepath}:{line_idx + 1}: scope_stamp: missing column '{col}' in INSERT INTO {table}"
                )
    return violations


def _check_artifact(sql: str) -> bool:
    """Return True if *sql* contains an ``INSERT INTO artifact_topics`` clause."""
    return bool(_ARTIFACT_TOPICS_RE.search(sql))


# ---------------------------------------------------------------------------
# Main scanner
# ---------------------------------------------------------------------------

def scan_directory(root: Path) -> list[str]:
    """Walk ``root`` recursively, scanning ``.py`` files for INSERT violations.

    Returns a list of violation strings in format:
        ``path:line: kind: message``
    """
    violations: list[str] = []

    for py_file in sorted(root.rglob("*.py")):
        try:
            source = py_file.read_text()
        except (OSError, UnicodeDecodeError):
            continue

        lines = source.splitlines()
        filepath = str(py_file.relative_to(root.parent))

        # Find every line containing "INSERT INTO"
        for idx, line in enumerate(lines):
            if "INSERT INTO" not in line.upper():
                continue

            # Try to identify the table name and check if it's one we care about.
            match = _INSERT_RE.search(line)
            if not match:
                continue

            table = match.group(1).lower()

            # Check if this is a scope-stamp table.
            if table in SCOPE_STAMP_TABLES:
                # Extract the full SQL string.
                sql = _extract_sql_strings(lines, idx)
                if sql is None:
                    # Fall back to just the current line.
                    sql = line
                violations.extend(
                    _check_scope_stamp(filepath, idx, table, sql)
                )

            # Check if this is an artifact table.
            elif table in ARTIFACT_TABLES:
                sql = _extract_sql_strings(lines, idx)
                if sql is None:
                    sql = line
                # Also check if the line continues into a multi-line string.
                # For artifact tables, we need to check if the same SQL string
                # also contains INSERT INTO artifact_topics.
                if not _check_artifact(sql):
                    violations.append(
                        f"{filepath}:{idx + 1}: artifact_coverage: "
                        f"INSERT INTO {table} missing INSERT INTO artifact_topics "
                        f"in same statement"
                    )

    return violations


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Advisory INSERT linter for S2a — flags missing bot_id/topic_id "
        "in scope-stamp tables and missing artifact_topics links for artifact tables."
    )
    parser.add_argument(
        "--dir",
        default="app",
        help="Directory to scan (default: app)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress output; exit 0 unconditionally",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    root = Path(args.dir)
    if not root.is_dir():
        # Try relative to the script's parent (project root).
        script_dir = Path(__file__).resolve().parent.parent
        root = script_dir / args.dir
        if not root.is_dir():
            print(f"error: directory '{args.dir}' not found", file=sys.stderr)
            # Advisory — exit 0 in S2a.
            # TODO(S2b): make blocking — exit with non-zero.
            sys.exit(0)

    violations = scan_directory(root)

    if not args.quiet:
        for v in violations:
            print(v, file=sys.stderr)

        if not violations:
            print("lint_inserts: no violations found", file=sys.stderr)
        else:
            print(
                f"lint_inserts: {len(violations)} violation(s) found (advisory — S2a)",
                file=sys.stderr,
            )

    # Always exit 0 in S2a (advisory).
    # TODO(S2b): make blocking — sys.exit(1 if violations else 0)
    sys.exit(0)


if __name__ == "__main__":
    main()