#!/usr/bin/env python3
"""Artifact-read lint — flags direct ``FROM <artifact_table>`` reads that are
not scoped through ``artifact_topics``.

Scans ``app/`` for the regex ``\\bFROM\\s+(memories|themes|observations|
watch_items|distillations|out_of_bounds)\\b``.  Every hit must have the
enclosing SQL string contain ``join_artifact_topics`` (the helper call that
emits the JOIN fragment), or be in the explicit whitelist.

Design notes
------------
* FROM-only design: UPDATE statements are NOT checked (deferred to S6).
* ``FROM artifact_topics`` is intentionally NOT in the regex alternation
  because it is the bridge table itself, not an artifact table.
* S6 TODO: add UPDATE checking.
* We detect compliance via ``join_artifact_topics`` in the source rather than
  ``_at_`` in runtime SQL because f-string interpolations don't expand at
  scan time.

Usage:
    python scripts/lint_artifact_reads.py           # scan app/ directory
    python scripts/lint_artifact_reads.py --help    # show this message
    python scripts/lint_artifact_reads.py --dir app # specify scan directory
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# The regex: FROM {memories, themes, observations, watch_items, distillations,
# out_of_bounds} — captures the table name.
# ---------------------------------------------------------------------------
_FROM_ARTIFACT_RE = re.compile(
    r"\bFROM\s+(memories|themes|observations|watch_items|distillations|out_of_bounds)\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Whitelist — files / directories that are allowed to have direct FROM hits.
# ---------------------------------------------------------------------------
WHITELIST_PATHS: set[str] = {
    "app/services/topic_filter.py",  # the helper itself
    "app/routers/admin.py",          # operator read-only views (allowlisted per-site)
    "routers/admin.py",              # alternate relative path form
    "services/topic_filter.py",      # alternate relative path form
    "app/bots/prompts/hector.py",    # natural-language prompt text, not SQL
    "bots/prompts/hector.py",        # alternate relative path form
    "app/bots/prompts/habits.py",    # natural-language prompt text, not SQL
    "bots/prompts/habits.py",        # alternate relative path form
}
WHITELIST_DIR_PREFIXES: tuple[str, ...] = (
    "tests/",
    "migrations/",
    "scripts/",  # includes this script itself
)


def _is_whitelisted(relpath: str) -> bool:
    """Return True if *relpath* is in the whitelist."""
    if relpath in WHITELIST_PATHS:
        return True
    for prefix in WHITELIST_DIR_PREFIXES:
        if relpath.startswith(prefix) or ("/" + relpath).startswith("/" + prefix):
            return True
    return False


def _extract_enclosing_sql(lines: list[str], hit_line_idx: int) -> str | None:
    """Given the line index of a FROM hit, try to find the enclosing SQL string.

    Returns the SQL text if found, else None (meaning we scan just the line).
    """
    opening_line = None
    delimiter: str | None = None

    for i in range(hit_line_idx, -1, -1):
        line = lines[i]
        for delim in ('"""', "'''"):
            pos = line.find(delim)
            if pos != -1:
                rest = line[pos + len(delim):]
                if delim not in rest:
                    opening_line = i
                    delimiter = delim
                    break
        if delimiter:
            break

    if delimiter is None:
        return None

    opening_pos = lines[opening_line].find(delimiter) + len(delimiter)
    parts: list[str] = []
    first_line = lines[opening_line][opening_pos:]

    closing_pos = first_line.find(delimiter)
    if closing_pos != -1:
        parts.append(first_line[:closing_pos])
        return "\n".join(parts)

    parts.append(first_line)

    for i in range(opening_line + 1, len(lines)):
        line = lines[i]
        closing_pos = line.find(delimiter)
        if closing_pos != -1:
            parts.append(line[:closing_pos])
            return "\n".join(parts)
        parts.append(line)

    return "\n".join(parts)


def _has_join_helper(sql: str) -> bool:
    """Return True if *sql* contains a call to join_artifact_topics or the
    ``_at_`` prefix used in direct JOIN literals."""
    return "join_artifact_topics" in sql or "_at_" in sql


def scan_directory(root: Path) -> list[str]:
    """Scan ``root`` recursively for direct artifact FROM clauses.

    Returns a list of violation strings (empty = clean).
    """
    violations: list[str] = []

    for py_file in sorted(root.rglob("*.py")):
        try:
            source = py_file.read_text()
        except (OSError, UnicodeDecodeError):
            continue

        relpath = str(py_file.relative_to(root))
        if _is_whitelisted(relpath):
            continue

        lines = source.splitlines()

        for idx, line in enumerate(lines):
            match = _FROM_ARTIFACT_RE.search(line)
            if not match:
                continue

            sql = _extract_enclosing_sql(lines, idx)
            if sql is None:
                sql = line

            if _has_join_helper(sql):
                continue

            table = match.group(1)
            violations.append(
                f"{relpath}:{idx + 1}: direct FROM {table}"
                f" without join_artifact_topics call"
            )

    return violations


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Lint artifact-table reads — flags direct FROM without"
        " artifact_topics JOIN."
    )
    parser.add_argument(
        "--dir",
        default="app",
        help="Directory to scan (default: app)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress output",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    root = Path(args.dir)
    if not root.is_dir():
        script_dir = Path(__file__).resolve().parent.parent
        root = script_dir / args.dir
        if not root.is_dir():
            print(f"error: directory '{args.dir}' not found", file=sys.stderr)
            sys.exit(1)

    violations = scan_directory(root)

    if not args.quiet:
        for v in violations:
            print(v, file=sys.stderr)

        if not violations:
            print("lint_artifact_reads: no violations found", file=sys.stderr)
        else:
            print(
                f"lint_artifact_reads: {len(violations)} violation(s) found",
                file=sys.stderr,
            )

    sys.exit(1 if violations else 0)


if __name__ == "__main__":
    main()